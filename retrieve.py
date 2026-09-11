"""Retrieval for Office Hours.

Three switchable arms (docs/DESIGN.md section 5):
  bm25   - lexical BM25 over whole-doc "chunks" (primary)
  full   - return every KB doc (full-context baseline)
  router - one extra model call picks up to k doc ids from a title/summary
           manifest. It is an LLM document selector, nothing more: no bm25
           fallback, no full-context fallback, no breadth heuristic.

Whole-doc chunks with k=4 is a deliberate choice for a 26-doc KB. At a few hundred
docs this would need sub-doc chunking, hybrid (lexical + embedding) retrieval and
a reranker.

Each retriever exposes  async .retrieve(query, k, usage=None) -> list[Retrieved].
`usage` is an optional list; the router appends its own call's token/cost record
so the agent can fold it into the run's total.

CLI:
  uv run python retrieve.py query "how much is tuition" --arm bm25
  uv run python retrieve.py recall --arm bm25       # retrieval recall vs golden set
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import anyio

from config import AGENT_MODEL

ROOT = Path(__file__).parent
KB_DIR = ROOT / "kb"
KB_POISONED_DIR = ROOT / "kb_poisoned"
GOLDEN = ROOT / "eval" / "golden_set.yaml"

_WORD = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


@dataclass
class Doc:
    doc_id: str
    title: str
    text: str


@dataclass
class Retrieved:
    doc_id: str
    title: str
    text: str
    score: float | None = None


def _title_of(md: str, fallback: str) -> str:
    for line in md.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _first_body_line(md: str) -> str:
    for line in md.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("<!--") or s.startswith("|"):
            continue
        return re.sub(r"\s+", " ", s)[:160]
    return ""


_KB_CACHE: dict[bool, list[Doc]] = {}


def load_kb(poison: bool = False) -> list[Doc]:
    """kb/*.md, cached. With poison=True, kb_poisoned/*.md are ADDED under their own
    stems (a contradiction test, not an override). A poisoned file whose stem
    collides with a real doc would still replace it; none currently do."""
    if poison in _KB_CACHE:
        return _KB_CACHE[poison]
    by_id: dict[str, Doc] = {}
    dirs = [KB_DIR] + ([KB_POISONED_DIR] if poison else [])
    for d in dirs:
        for p in sorted(d.glob("*.md")):
            md = p.read_text()
            by_id[p.stem] = Doc(doc_id=p.stem, title=_title_of(md, p.stem), text=md)
    docs = [by_id[k] for k in sorted(by_id)]
    _KB_CACHE[poison] = docs
    return docs


class BM25Retriever:
    name = "bm25"

    def __init__(self, docs: list[Doc]):
        from rank_bm25 import BM25Okapi

        self.docs = docs
        self._bm25 = BM25Okapi([_tokenize(d.text) for d in docs])

    async def retrieve(self, query: str, k: int = 4, usage: list | None = None) -> list[Retrieved]:
        scores = self._bm25.get_scores(_tokenize(query))
        ranked = sorted(zip(self.docs, scores, strict=True), key=lambda p: p[1], reverse=True)
        return [Retrieved(d.doc_id, d.title, d.text, float(s)) for d, s in ranked[:k]]


class FullContextRetriever:
    name = "full"

    def __init__(self, docs: list[Doc]):
        self.docs = docs

    async def retrieve(self, query: str, k: int = 4, usage: list | None = None) -> list[Retrieved]:
        # k ignored on purpose: the baseline is "hand the model everything".
        return [Retrieved(d.doc_id, d.title, d.text, None) for d in self.docs]


class LLMRouterRetriever:
    name = "router"

    def __init__(self, docs: list[Doc], model: str = AGENT_MODEL):
        self.docs = docs
        self.by_id = {d.doc_id: d for d in docs}
        self.model = model

    def _manifest(self) -> str:
        return "\n".join(
            f"- {d.doc_id}: {d.title} - {_first_body_line(d.text)}" for d in self.docs
        )

    async def retrieve(self, query: str, k: int = 4, usage: list | None = None) -> list[Retrieved]:
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock
        from claude_agent_sdk import query as sdk_query

        prompt = (
            "You route a student's question to the most relevant knowledge-base "
            "documents. Document list (id: title - summary):\n\n"
            f"{self._manifest()}\n\n"
            f"Question: {query}\n\n"
            f"Reply with ONLY a JSON array of up to {k} document ids, most relevant "
            "first, drawn verbatim from the list. No prose."
        )
        opts = ClaudeAgentOptions(model=self.model, max_turns=1, tools=[])
        chunks: list[str] = []
        async for msg in sdk_query(prompt=prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        chunks.append(b.text)
            elif isinstance(msg, ResultMessage) and usage is not None:
                u = msg.usage if isinstance(msg.usage, dict) else {}
                usage.append({
                    "input_tokens": (u.get("input_tokens", 0) or 0)
                    + (u.get("cache_read_input_tokens", 0) or 0)
                    + (u.get("cache_creation_input_tokens", 0) or 0),
                    "output_tokens": u.get("output_tokens"),
                    "cost_usd": msg.total_cost_usd,
                })
        ids = [i for i in _parse_id_list("".join(chunks)) if i in self.by_id][:k]
        return [Retrieved(d.doc_id, d.title, d.text, None) for d in (self.by_id[i] for i in ids)]


def _parse_id_list(raw: str) -> list[str]:
    m = re.search(r"\[.*?\]", raw, re.DOTALL)
    if m:
        try:
            val = json.loads(m.group(0))
            if isinstance(val, list):
                return [str(x).strip() for x in val]
        except json.JSONDecodeError:
            pass
    return re.findall(r"[a-z0-9][a-z0-9_-]+", raw)


_RETRIEVER_CACHE: dict[tuple[str, bool], object] = {}


def get_retriever(arm: str, poison: bool = False):
    key = (arm, poison)
    if key in _RETRIEVER_CACHE:
        return _RETRIEVER_CACHE[key]
    docs = load_kb(poison=poison)
    if arm == "bm25":
        r = BM25Retriever(docs)
    elif arm == "full":
        r = FullContextRetriever(docs)
    elif arm == "router":
        r = LLMRouterRetriever(docs)
    else:
        raise ValueError(f"unknown arm: {arm!r}")
    _RETRIEVER_CACHE[key] = r
    return r


def _case_query(case: dict) -> str:
    turns = [t["content"] for t in case.get("context", []) if t.get("role") == "user"]
    return " ".join(turns + [case["question"]])


async def _cmd_query(args):
    r = get_retriever(args.arm, poison=args.poison)
    for hit in await r.retrieve(args.text, k=args.k):
        score = f"{hit.score:.2f}" if hit.score is not None else "-"
        print(f"[{score:>6}] {hit.doc_id:<34} {hit.title}")


async def _cmd_recall(args):
    import yaml

    cases = yaml.safe_load(GOLDEN.read_text())["cases"]
    cases = [c for c in cases if c.get("gold_doc_ids") and not c.get("requires_poison")]
    r = get_retriever(args.arm)

    # any-of semantics: gold_doc_ids lists docs, ANY of which fully answers the case.
    hits = 0
    misses = []
    for c in cases:
        got = {h.doc_id for h in await r.retrieve(_case_query(c), k=args.k)}
        need = set(c["gold_doc_ids"])
        if need & got:
            hits += 1
        else:
            misses.append((c["id"], sorted(need)))

    n = len(cases)
    print(f"arm={args.arm}  k={args.k}  cases={n}")
    print(f"retrieval recall (a sufficient doc retrieved): {hits}/{n} = {hits / n:.0%}")
    for cid, need in misses:
        print(f"  miss  {cid:<26} wanted any of: {', '.join(need)}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("query", help="retrieve for one query")
    q.add_argument("text")
    q.add_argument("--arm", default="bm25", choices=["bm25", "full", "router"])
    q.add_argument("--k", type=int, default=4)
    q.add_argument("--poison", action="store_true")
    q.set_defaults(func=_cmd_query)

    rc = sub.add_parser("recall", help="retrieval recall vs golden set")
    rc.add_argument("--arm", default="bm25", choices=["bm25", "full", "router"])
    rc.add_argument("--k", type=int, default=4)
    rc.set_defaults(func=_cmd_recall)

    args = ap.parse_args(argv)
    anyio.run(args.func, args)


if __name__ == "__main__":
    sys.exit(main())
