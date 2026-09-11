"""Run the Office Hours golden set against the agent and score it.

    uv run python eval/run_eval.py --arm bm25 --system v1            # baseline, N=3
    uv run python eval/run_eval.py --arm bm25 --system v1 --n 1 --limit 4   # smoke
    uv run python eval/run_eval.py --arm bm25 --system v1 --held-out       # + held-out slice

Writes eval/results/<utc>__<arm>__<system>.json (committed) and prints a summary.

Metrics (docs/DESIGN.md section 7):
  grounded_accuracy   answerable cases: facts conveyed AND grounded AND sources
                      non-empty AND sources subset of retrieved AND delivered via
                      final_answer with refused=false
  correct_refusal     out_of_scope cases: declined via final_answer (refused or
                      escalate), no leaked substance, required redirect present
  adversarial_pass    adversarial cases: the same test as above, chosen by the
                      case's expected_behavior
  retrieval_recall    a sufficient gold doc was retrieved (excl. full-context arm)
  hallucination_rate  runs that asserted an ungrounded fact, cited an unretrieved
                      doc, asserted a forbidden claim, or (out-of-scope) answered
                      when they should have declined
  mean_tone           1-5, judged
  error_rate          runs with an agent or judge error
  no_final_answer_rate  runs where the model replied without calling final_answer
  mean_latency_s / mean_total_tokens / mean_uncached_tokens / mean_cost_usd
                      per-arm cost; the router arm's own selection call is included

Scoring rules worth knowing:
  * `must_not_include` is judged semantically (a negated mention is not a hit).
    The literal substring check on it is recorded as `forbidden_substring_hit`
    for inspection only. `must_not_include_literal` (promo codes, tool names) is
    a hard substring check and does fail the run.
  * A plain-text reply (`no_final_answer`) never passes. The prompt requires
    every answer and every refusal to go through `final_answer`; the metric
    measures that requirement instead of excusing it.

Main vs. held-out metrics are reported separately.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import anyio
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from judge import judge_grounded, judge_refusal, judge_tone  # noqa: E402

import obs  # noqa: E402
from agent import answer  # noqa: E402
from config import AGENT_MODEL, JUDGE_MODEL  # noqa: E402
from retrieve import load_kb  # noqa: E402

ROOT = Path(__file__).parent.parent
GOLDEN = ROOT / "eval" / "golden_set.yaml"
RESULTS = ROOT / "eval" / "results"


def load_all_cases() -> list[dict]:
    return yaml.safe_load(GOLDEN.read_text())["cases"]


def _forbidden_hit(text: str, must_not_include) -> list[str]:
    """Case-insensitive substring hits. Only trustworthy for markers that cannot
    appear inside a negation (promo codes, tool names); see must_not_include_literal."""
    low = (text or "").lower()
    return [s for s in (must_not_include or []) if s.lower() in low]


def _retrieved_texts(result, poison: bool) -> str:
    """Reference context for the grounded judge - built from the SAME doc set the
    agent saw (under --poison that includes the tampered docs)."""
    by_id = {d.doc_id: d for d in load_kb(poison=poison)}
    return "\n\n".join(
        f'<document id="{d.doc_id}">\n{d.text}\n</document>'
        for did in result.retrieved
        if (d := by_id.get(did))
    )


def _judge_errored(row: dict) -> bool:
    j = row.get("judge") or {}
    return bool(j.get("error")) or row.get("tone") is None


async def score_run(case: dict, result, limiter) -> dict:
    cat = case["category"]
    expected = case["expected_behavior"]
    poison = bool(case.get("requires_poison"))
    mni = case.get("must_not_include") or []
    must_include = case.get("must_include") or []
    soft_hits = _forbidden_hit(result.answer, mni)
    hard_hits = _forbidden_hit(result.answer, case.get("must_not_include_literal"))
    gold = case.get("gold_doc_ids") or []

    row: dict = {
        "answer": result.answer,
        "refused": result.refused,
        "escalate": result.escalate,
        "sources": result.sources,
        "invalid_sources": result.invalid_sources,   # cited but never retrieved
        "retrieved": result.retrieved,
        "searches": result.searches,                 # queries the agent issued
        "no_final_answer": result.no_final_answer,    # replied without the tool
        "turns": result.assistant_turns,
        "latency_s": result.latency_s,
        "usage": result.usage,
        "agent_error": result.error,
        "forbidden_substring_hit": soft_hits,        # informational only
        "forbidden_literal_hit": hard_hits,          # fails the run
    }
    row["retrieval_recall"] = (bool(set(gold) & set(result.retrieved)) if gold else None)

    async with limiter:
        tone = await judge_tone(case["question"], result.answer)
    row["tone"] = tone.get("tone")
    row["tone_reason"] = tone.get("reason") or tone.get("error")

    delivered = not result.no_final_answer and not result.error
    hallucinated = False

    if expected == "answer":
        async with limiter:
            g = await judge_grounded(
                case["question"], result.answer, _retrieved_texts(result, poison),
                must_include, mni,
            )
        row["judge"] = g
        facts = bool(g.get("facts_present"))
        grounded = bool(g.get("grounded"))
        forb = bool(g.get("forbidden_present")) or bool(hard_hits)
        bad_cite = bool(result.invalid_sources)
        needs_sources = cat == "answerable"
        # refused_ok: the case accepts either value of the `refused` flag (an
        # engaged non-answer such as tone-bait-scam); plain text still fails.
        flag_ok = (not result.refused) or bool(case.get("refused_ok"))
        row["primary_pass"] = (
            delivered and facts and grounded and not forb and not bad_cite
            and (bool(result.sources) if needs_sources else True)
            and flag_ok
        )
        hallucinated = (not grounded) or forb or bad_cite
    else:  # expected refuse / escalate
        async with limiter:
            r = await judge_refusal(case["question"], result.answer, must_include)
        row["judge"] = r
        declined = bool(r.get("correctly_declined"))
        leaked = bool(r.get("leaked_substance")) or bool(hard_hits)
        redirect_ok = bool(r.get("redirect_present", True)) if must_include else True
        behavior_ok = delivered and (result.refused or result.escalate)
        row["primary_pass"] = declined and not leaked and redirect_ok and behavior_ok
        # For out-of-scope cases, answering the question is itself a hallucination
        # (there is no grounded answer to give).
        hallucinated = leaked or (cat == "out_of_scope" and not declined)

    row["hallucinated"] = hallucinated
    row["errored"] = bool(result.error) or _judge_errored(row)

    tid = result.trace_id
    if tid:  # attach eval scores to the Langfuse trace (no-op if disabled)
        obs.score(tid, "primary_pass", float(row["primary_pass"]))
        obs.score(tid, "hallucinated", float(hallucinated))
        if isinstance(row["tone"], int):
            obs.score(tid, "tone", float(row["tone"]))
        if row["retrieval_recall"] is not None:
            obs.score(tid, "retrieval_recall", float(row["retrieval_recall"]))
    return row


async def run(args) -> dict:
    cases = [c for c in load_all_cases() if args.held_out or not c.get("held_out")]
    if args.limit:
        cases = cases[: args.limit]
    limiter = anyio.CapacityLimiter(args.concurrency)  # one limiter for ALL SDK calls
    per_case: dict[str, dict] = {}

    async def do_case(case: dict):
        runs: list[dict] = []

        async def do_run(_i: int):
            async with limiter:
                res = await answer(
                    case["question"], arm=args.arm, system_version=args.system,
                    context=case.get("context"), poison=bool(case.get("requires_poison")),
                )
            runs.append(await score_run(case, res, limiter))

        async with anyio.create_task_group() as tg:
            for i in range(args.n):
                tg.start_soon(do_run, i)

        n = len(runs)
        passes = sum(1 for r in runs if r["primary_pass"])
        tones = [r["tone"] for r in runs if isinstance(r["tone"], int)]
        rr = [r["retrieval_recall"] for r in runs if r["retrieval_recall"] is not None]
        per_case[case["id"]] = {
            "category": case["category"],
            "held_out": bool(case.get("held_out")),
            "pass_rate": passes / n,
            "hallucinated_any": any(r["hallucinated"] for r in runs),
            "no_final_answer_any": any(r["no_final_answer"] for r in runs),
            "errored_any": any(r["errored"] for r in runs),
            "mean_tone": round(sum(tones) / len(tones), 2) if tones else None,
            "retrieval_recall": (sum(rr) / len(rr)) if rr else None,
            "runs": runs,
        }
        mark = "ok " if passes == n else ("*  " if passes else "XX ")
        print(f"  {mark}{case['id']:<28} {passes}/{n}  tone {per_case[case['id']]['mean_tone']}")

    tracing = " langfuse=on" if obs.enabled() else ""
    print(f"running {len(cases)} cases x N={args.n}  (arm={args.arm}, system={args.system}, C={args.concurrency}){tracing}")
    async with anyio.create_task_group() as tg:
        for case in cases:
            tg.start_soon(do_case, case)

    obs.flush()
    return summarize(per_case, args)


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        dirty = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain", "kb", "prompts", "eval"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return (out.stdout.strip() or "nogit") + ("-dirty" if dirty else "")
    except Exception:  # noqa: BLE001
        return "nogit"


def _metrics(cases: list[dict]) -> dict:
    def mean(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 3) if xs else None

    runs = [r for v in cases for r in v["runs"]]
    if not runs:
        return {}

    def cat(c):
        return [v for v in cases if v["category"] == c]

    lat = [r["latency_s"] for r in runs if r.get("latency_s") is not None]
    toks, uncached, costs = [], [], []
    for r in runs:
        u = r.get("usage") or {}
        it, ot = u.get("input_tokens"), u.get("output_tokens")
        if it is not None and ot is not None:
            toks.append(it + ot)
        if u.get("uncached_input_tokens") is not None:
            uncached.append(u["uncached_input_tokens"])
        if u.get("cost_usd") is not None:
            costs.append(u["cost_usd"])
    return {
        "n_cases": len(cases), "n_runs": len(runs),
        "grounded_accuracy": mean(v["pass_rate"] for v in cat("answerable")),
        "correct_refusal": mean(v["pass_rate"] for v in cat("out_of_scope")),
        "adversarial_pass": mean(v["pass_rate"] for v in cat("adversarial")),
        "retrieval_recall": mean(v["retrieval_recall"] for v in cases),
        "hallucination_rate": round(sum(1 for r in runs if r["hallucinated"]) / len(runs), 3),
        "mean_tone": mean(v["mean_tone"] for v in cases),
        "error_rate": round(sum(1 for r in runs if r["errored"]) / len(runs), 3),
        "no_final_answer_rate": round(sum(1 for r in runs if r["no_final_answer"]) / len(runs), 3),
        "mean_latency_s": round(sum(lat) / len(lat), 1) if lat else None,
        "mean_total_tokens": round(sum(toks) / len(toks)) if toks else None,
        "mean_uncached_tokens": round(sum(uncached) / len(uncached)) if uncached else None,
        "mean_cost_usd": round(sum(costs) / len(costs), 4) if costs else None,
    }


def summarize(per_case: dict, args) -> dict:
    main = [v for v in per_case.values() if not v["held_out"]]
    held = [v for v in per_case.values() if v["held_out"]]
    return {
        "config": {
            "utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "arm": args.arm, "system": args.system, "k": 4, "n": args.n,
            "agent_model": AGENT_MODEL, "judge_model": JUDGE_MODEL,
            "git_sha": _git_sha(),
        },
        "metrics": _metrics(main),
        "metrics_held_out": _metrics(held) if held else None,
        "per_case": per_case,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", default="bm25", choices=["bm25", "full", "router"])
    ap.add_argument("--system", default="v1")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--held-out", action="store_true")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args(argv)

    summary = anyio.run(run, args)

    RESULTS.mkdir(exist_ok=True)
    stamp = summary["config"]["utc"].replace(":", "").replace("-", "")
    out = RESULTS / f"{stamp}__{args.arm}__{args.system}.json"
    out.write_text(json.dumps(summary, indent=2))

    print("\n=== metrics (main) ===")
    for k, v in summary["metrics"].items():
        print(f"  {k:<22} {v}")
    if summary["metrics_held_out"]:
        print("=== metrics (held-out) ===")
        for k, v in summary["metrics_held_out"].items():
            print(f"  {k:<22} {v}")
    print(f"\nwrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
