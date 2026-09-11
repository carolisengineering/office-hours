"""Office Hours agent: retrieve -> reason -> final_answer, on the Claude Agent SDK.

Public API:
    result = await answer("How much is tuition?", arm="bm25", system_version="v1")

CLI:
    uv run python agent.py "how much is tuition"
    uv run python agent.py "how much is tuition" --arm full --poison
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

import obs
from config import AGENT_MODEL
from retrieve import get_retriever

PROMPTS = Path(__file__).parent / "prompts"
DEFAULT_TIMEOUT_S = 150.0

FINAL_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "Reply to the student."},
        "sources": {
            "type": "array",
            "items": {"type": "string"},
            "description": "doc_id values actually relied on; [] if none.",
        },
        "refused": {"type": "boolean"},
        "escalate": {"type": "boolean"},
    },
    "required": ["answer", "sources", "refused", "escalate"],
}


@dataclass
class AgentResult:
    question: str
    answer: str
    sources: list[str]
    refused: bool
    escalate: bool
    retrieved: list[str] = field(default_factory=list)      # doc_ids seen via search_kb
    searches: list[str] = field(default_factory=list)        # queries issued
    invalid_sources: list[str] = field(default_factory=list) # cited but never retrieved
    no_final_answer: bool = False                            # answered as plain text
    assistant_turns: int = 0
    latency_s: float = 0.0
    usage: dict | None = None                                # tokens/cost, see _usage_from()
    arm: str = "bm25"
    system_version: str = "v1"
    model: str = AGENT_MODEL
    trace_id: str | None = None                              # Langfuse, if enabled
    error: str | None = None


def load_system(version: str) -> str:
    path = PROMPTS / f"system_{version}.md"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing - run `uv run python prompts/compose.py`")
    return path.read_text()


def _usage_from(msg: ResultMessage, router_calls: list[dict]) -> dict:
    """Token/cost record for one run, read from the SDK's final ResultMessage only.

    `input_tokens` is the total the model saw (fresh + cache reads + cache writes);
    `uncached_input_tokens` excludes cache reads so the cost-bearing volume is
    visible. The router arm's own selection call runs outside the agent loop, so
    its usage is passed in and added on top (`router_*`), otherwise that arm
    looks cheaper than it is.
    """
    u = msg.usage if isinstance(msg.usage, dict) else {}
    fresh = u.get("input_tokens", 0) or 0
    cache_read = u.get("cache_read_input_tokens", 0) or 0
    cache_write = u.get("cache_creation_input_tokens", 0) or 0
    out = {
        "input_tokens": fresh + cache_read + cache_write,
        "uncached_input_tokens": fresh + cache_write,
        "cache_read_tokens": cache_read,
        "output_tokens": u.get("output_tokens"),
        "cost_usd": msg.total_cost_usd,
    }
    if router_calls:
        r_in = sum(c.get("input_tokens", 0) for c in router_calls)
        r_out = sum(c.get("output_tokens", 0) or 0 for c in router_calls)
        r_cost = sum(c.get("cost_usd", 0) or 0 for c in router_calls)
        out.update({
            "router_calls": len(router_calls),
            "router_input_tokens": r_in,
            "router_output_tokens": r_out,
            "router_cost_usd": r_cost,
            "input_tokens": out["input_tokens"] + r_in,
            "uncached_input_tokens": out["uncached_input_tokens"] + r_in,
            "output_tokens": (out["output_tokens"] or 0) + r_out,
            "cost_usd": (out["cost_usd"] or 0) + r_cost,
        })
    return out


def _build_prompt(question: str, context: list[dict] | None) -> str:
    """Flatten prior turns into one user message. This is pasted context, not a
    stateful session: the model does not see its earlier tool calls."""
    if not context:
        return question
    lines = ["Earlier in this conversation:"]
    for turn in context:
        who = "Student" if turn["role"] == "user" else "You"
        lines.append(f"{who}: {turn['content']}")
    lines += ["", f"Student's new message: {question}"]
    return "\n".join(lines)


async def answer(
    question: str,
    *,
    arm: str = "bm25",
    system_version: str = "v1",
    context: list[dict] | None = None,
    poison: bool = False,
    model: str = AGENT_MODEL,
    k: int = 4,
    max_turns: int = 18,        # tool-discovery hop + several search_kb + final_answer
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> AgentResult:
    retriever = get_retriever(arm, poison=poison)
    state: dict = {
        "retrieved": [], "searches": [], "final": None, "text": [], "tr": obs.Null(),
        "router_usage": [],  # usage of the router arm's own selection calls
    }

    @tool("search_kb", "Search the program knowledge base. Returns matching documents.", {"query": str})
    async def search_kb(args):
        q = args["query"]
        state["searches"].append(q)
        with state["tr"].span("search_kb", input=q, metadata={"arm": arm, "k": k}) as sp:
            hits = await retriever.retrieve(q, k=k, usage=state["router_usage"])
            sp.update(output={
                "doc_ids": [h.doc_id for h in hits],
                "scores": [round(h.score, 3) if h.score is not None else None for h in hits],
            })
        for h in hits:
            if h.doc_id not in state["retrieved"]:
                state["retrieved"].append(h.doc_id)
        if not hits:
            return {"content": [{"type": "text", "text": "No matching documents."}]}
        return {
            "content": [
                {"type": "text", "text": f'<document id="{h.doc_id}" title="{h.title}">\n{h.text}\n</document>'}
                for h in hits
            ]
        }

    @tool("final_answer", "Provide the final response to the student.", FINAL_ANSWER_SCHEMA)
    async def final_answer(args):
        state["final"] = {
            "answer": args.get("answer", ""),
            "sources": list(args.get("sources") or []),
            "refused": bool(args.get("refused", False)),
            "escalate": bool(args.get("escalate", False)),
        }
        return {"content": [{"type": "text", "text": "Recorded."}]}

    server = create_sdk_mcp_server(name="oh", version="1.0.0", tools=[search_kb, final_answer])
    opts = ClaudeAgentOptions(
        model=model,
        system_prompt=load_system(system_version),
        mcp_servers={"oh": server},
        # tools=[] removes Claude Code's built-in tool set (Bash, Edit, Agent, ...)
        # from the model's context. Without it those tools are visible-but-denied,
        # which inflates input tokens and gave the red-team's "list your tools"
        # probe real harness tool names to recite.
        tools=[],
        allowed_tools=["mcp__oh__search_kb", "mcp__oh__final_answer"],
        max_turns=max_turns,
    )

    turns = 0
    err = None
    usage = None
    timed_out = False
    t0 = anyio.current_time()

    with obs.trace(
        "office_hours_agent",
        input=question,
        metadata={"arm": arm, "system_version": system_version, "model": model, "poison": poison},
    ) as tr:
        state["tr"] = tr
        try:
            with anyio.move_on_after(timeout_s) as scope:
                async for msg in query(prompt=_build_prompt(question, context), options=opts):
                    if isinstance(msg, AssistantMessage):
                        turns += 1
                        for block in msg.content:
                            if isinstance(block, TextBlock) and block.text.strip():
                                state["text"].append(block.text.strip())
                    elif isinstance(msg, ResultMessage):
                        usage = _usage_from(msg, state["router_usage"])
            timed_out = scope.cancelled_caught
        except Exception as e:  # noqa: BLE001 - record and return, don't crash the eval
            err = f"{type(e).__name__}: {e}"
        latency = round(anyio.current_time() - t0, 2)
        if timed_out and not err:
            err = f"timeout after {timeout_s}s"

        fin = state["final"]
        fallback_text = "\n\n".join(state["text"])
        base = dict(
            question=question, retrieved=state["retrieved"], searches=state["searches"],
            assistant_turns=turns, latency_s=latency, usage=usage, arm=arm,
            system_version=system_version, model=model, trace_id=tr.trace_id,
        )

        if fin is None:
            # The model answered (usually a refusal) in plain text and stopped
            # without calling final_answer. Keep that text instead of dropping it;
            # the eval records it as no_final_answer.
            out = {"answer": fallback_text, "no_final_answer": bool(fallback_text), "error": err}
            tr.update(output=out)
            return AgentResult(
                answer=fallback_text, sources=[], refused=False, escalate=False,
                no_final_answer=bool(fallback_text),
                error=err or ("no final_answer and no text" if not fallback_text else None),
                **base,
            )

        invalid = [s for s in fin["sources"] if s not in state["retrieved"]]
        tr.update(output={**fin, "invalid_sources": invalid, "error": err})
        return AgentResult(
            answer=fin["answer"], sources=fin["sources"], refused=fin["refused"],
            escalate=fin["escalate"], invalid_sources=invalid, error=err, **base,
        )


def _main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question")
    ap.add_argument("--arm", default="bm25", choices=["bm25", "full", "router"])
    ap.add_argument("--system", default="v1", dest="system_version")
    ap.add_argument("--model", default=AGENT_MODEL)
    ap.add_argument("--k", type=int, default=4)
    ap.add_argument("--poison", action="store_true")
    args = ap.parse_args(argv)

    res = anyio.run(
        partial(
            answer, args.question, arm=args.arm, system_version=args.system_version,
            poison=args.poison, model=args.model, k=args.k,
        )
    )
    print(json.dumps(asdict(res), indent=2))


if __name__ == "__main__":
    _main()
