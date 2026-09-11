"""Validate the Claude-as-judge scorers against eval/judge_labels.yaml.

    uv run python eval/validate_judge.py

Runs each judge on its hand-labeled examples and reports agreement %. Writes
eval/results/judge_validation.json. Quote the agreement number as a caveat
wherever eval metrics appear.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import anyio
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from judge import judge_grounded, judge_refusal, judge_tone  # noqa: E402

from config import JUDGE_MODEL  # noqa: E402
from retrieve import load_kb  # noqa: E402

ROOT = Path(__file__).parent.parent
LABELS = ROOT / "eval" / "judge_labels.yaml"
RESULTS = ROOT / "eval" / "results"


def _docs_text(doc_ids: list[str]) -> str:
    by_id = {d.doc_id: d for d in load_kb(poison=True)}
    return "\n\n".join(
        f'<document id="{d.doc_id}">\n{d.text}\n</document>'
        for did in doc_ids
        if (d := by_id.get(did))
    )


async def _score(label: dict) -> dict:
    j = label["judge"]
    exp = label["expect"]
    if j == "grounded":
        got = await judge_grounded(
            label["question"], label["answer"], _docs_text(label.get("retrieved", [])),
            label.get("must_include"), label.get("must_not_include"),
        )
        fields = ["facts_present", "grounded", "forbidden_present"]
        agree = {f: (bool(got.get(f)) == bool(exp[f])) for f in fields if f in exp}
    elif j == "refusal":
        got = await judge_refusal(label["question"], label["answer"], label.get("must_include"))
        fields = ["correctly_declined", "leaked_substance", "redirect_present"]
        agree = {f: (bool(got.get(f)) == bool(exp[f])) for f in fields if f in exp}
    else:  # tone
        got = await judge_tone(label["question"], label["answer"])
        t = got.get("tone")
        agree = {"tone": (isinstance(t, int) and exp["tone_min"] <= t <= exp["tone_max"])}
    return {"id": label["id"], "judge": j, "expect": exp, "got": got, "agree": agree}


async def main() -> None:
    labels = yaml.safe_load(LABELS.read_text())["labels"]
    limiter = anyio.CapacityLimiter(3)
    rows: list[dict] = []

    async def one(label):
        async with limiter:
            rows.append(await _score(label))

    async with anyio.create_task_group() as tg:
        for label in labels:
            tg.start_soon(one, label)

    rows.sort(key=lambda r: r["id"])
    by_judge: dict[str, list[bool]] = {}
    for r in rows:
        checks = list(r["agree"].values())
        r["pass"] = all(checks)
        by_judge.setdefault(r["judge"], []).extend(checks)
        mark = "ok" if r["pass"] else "XX"
        print(f"  {mark} {r['id']:<28} {r['agree']}")
        if not r["pass"]:
            print(f"       got: {r['got']}")

    summary = {
        "utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "judge_model": JUDGE_MODEL,
        "n_labels": len(rows),
        "agreement_overall": round(
            sum(1 for r in rows if r["pass"]) / len(rows), 3
        ),
        "field_agreement_by_judge": {
            j: round(sum(c) / len(c), 3) for j, c in by_judge.items()
        },
        "rows": rows,
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "judge_validation.json").write_text(json.dumps(summary, indent=2))
    print("\n=== judge validation ===")
    print(f"  labels passed: {summary['agreement_overall']:.0%}")
    for j, a in summary["field_agreement_by_judge"].items():
        print(f"  {j:<10} field agreement: {a:.0%}")
    print("\nwrote eval/results/judge_validation.json")


if __name__ == "__main__":
    anyio.run(main)
