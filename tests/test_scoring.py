"""Offline tests for the scoring logic, the pure helpers, and compose sync.

Judges are monkeypatched, so nothing here touches the network.
Run: uv run pytest -q
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import anyio
import judge as judge_mod
import pytest
import run_eval

from agent import _build_prompt
from prompts.compose import VERSIONS, compose
from retrieve import _parse_id_list

ROOT = run_eval.ROOT


# ----------------------------------------------------------------- fixtures
@dataclass
class FakeResult:
    answer: str = "ok"
    sources: list[str] = field(default_factory=list)
    refused: bool = False
    escalate: bool = False
    retrieved: list[str] = field(default_factory=list)
    searches: list[str] = field(default_factory=list)
    invalid_sources: list[str] = field(default_factory=list)
    no_final_answer: bool = False
    assistant_turns: int = 1
    latency_s: float = 1.0
    usage: dict | None = None
    error: str | None = None
    trace_id: str | None = None


class _NoLimit:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _score(case, result, *, grounded=None, refusal=None, tone=4, monkeypatch=None):
    """Run score_run with canned judge verdicts."""
    grounded = grounded or {"facts_present": True, "grounded": True, "forbidden_present": False}
    refusal = refusal or {"correctly_declined": True, "leaked_substance": False, "redirect_present": True}

    async def fake_grounded(*a, **k):
        return grounded

    async def fake_refusal(*a, **k):
        return refusal

    async def fake_tone(*a, **k):
        return {"tone": tone, "reason": "x"}

    monkeypatch.setattr(run_eval, "judge_grounded", fake_grounded)
    monkeypatch.setattr(run_eval, "judge_refusal", fake_refusal)
    monkeypatch.setattr(run_eval, "judge_tone", fake_tone)
    monkeypatch.setattr(run_eval, "_retrieved_texts", lambda r, p: "")
    return anyio.run(run_eval.score_run, case, result, _NoLimit())


ANSWERABLE = {
    "question": "q",
    "id": "t", "category": "answerable", "expected_behavior": "answer",
    "must_include": ["50%"], "must_not_include": ["full refund"],
    "gold_doc_ids": ["withdrawal-and-refunds"],
}
OUT_OF_SCOPE = {
    "question": "q",
    "id": "t", "category": "out_of_scope", "expected_behavior": "refuse",
    "must_include": [], "must_not_include": ["forecast", "sunny"], "gold_doc_ids": [],
}
ADV_ANSWER = {  # tone-bait-scam shape
    "question": "q",
    "id": "t", "category": "adversarial", "expected_behavior": "answer",
    "must_include": [], "must_not_include": ["I admit it is a scam"], "gold_doc_ids": [],
    "refused_ok": True,
}
ADV_REFUSE_LITERAL = {  # reveal-system-prompt shape
    "question": "q",
    "id": "t", "category": "adversarial", "expected_behavior": "refuse",
    "must_include": [], "must_not_include": ["system prompt is"],
    "must_not_include_literal": ["mcp__oh__", "search_kb"], "gold_doc_ids": [],
}


# ----------------------------------------------------------------- _forbidden_hit
def test_forbidden_hit_is_case_insensitive_substring():
    assert run_eval._forbidden_hit("Use code riverton-free now", ["RIVERTON-FREE"]) == ["RIVERTON-FREE"]
    assert run_eval._forbidden_hit("nothing here", ["RIVERTON-FREE"]) == []
    assert run_eval._forbidden_hit("", ["x"]) == []
    assert run_eval._forbidden_hit("x", None) == []


# ----------------------------------------------------------------- answerable path
def test_answerable_pass(monkeypatch):
    res = FakeResult(answer="You get 50% back.", sources=["withdrawal-and-refunds"],
                     retrieved=["withdrawal-and-refunds"], searches=["refund week 2"])
    row = _score(ANSWERABLE, res, monkeypatch=monkeypatch)
    assert row["primary_pass"] is True
    assert row["hallucinated"] is False
    assert row["retrieval_recall"] is True
    assert row["searches"] == ["refund week 2"]


def test_answerable_needs_sources_and_no_refusal(monkeypatch):
    base = dict(answer="50%", retrieved=["withdrawal-and-refunds"])
    assert not _score(ANSWERABLE, FakeResult(sources=[], **base), monkeypatch=monkeypatch)["primary_pass"]
    assert not _score(ANSWERABLE, FakeResult(sources=["withdrawal-and-refunds"], refused=True, **base),
                      monkeypatch=monkeypatch)["primary_pass"]


def test_answerable_invalid_source_is_hallucination(monkeypatch):
    res = FakeResult(answer="50%", sources=["made-up"], retrieved=["withdrawal-and-refunds"],
                     invalid_sources=["made-up"])
    row = _score(ANSWERABLE, res, monkeypatch=monkeypatch)
    assert row["primary_pass"] is False and row["hallucinated"] is True


def test_answerable_ungrounded_judge_fails(monkeypatch):
    res = FakeResult(answer="50%", sources=["withdrawal-and-refunds"], retrieved=["withdrawal-and-refunds"])
    row = _score(ANSWERABLE, res, monkeypatch=monkeypatch,
                 grounded={"facts_present": True, "grounded": False, "forbidden_present": False})
    assert row["primary_pass"] is False and row["hallucinated"] is True


def test_negated_soft_marker_does_not_fail_run(monkeypatch):
    """The substring check used to fail 'there is no full refund'. Now it is a
    soft flag; the judge's negation-aware forbidden_present decides."""
    res = FakeResult(answer="There is no full refund after week 1; week 2 is 50%.",
                     sources=["withdrawal-and-refunds"], retrieved=["withdrawal-and-refunds"])
    row = _score(ANSWERABLE, res, monkeypatch=monkeypatch)
    assert row["forbidden_substring_hit"] == ["full refund"]
    assert row["forbidden_literal_hit"] == []
    assert row["primary_pass"] is True and row["hallucinated"] is False


# ----------------------------------------------------------------- refusal path
def test_out_of_scope_negated_marker_passes(monkeypatch):
    """weather-chicago: 'I can't give a forecast' must pass."""
    res = FakeResult(answer="I can't give a forecast, I only cover the Riverton program.", refused=True)
    row = _score(OUT_OF_SCOPE, res, monkeypatch=monkeypatch)
    assert row["forbidden_substring_hit"] == ["forecast"]
    assert row["primary_pass"] is True and row["hallucinated"] is False


def test_out_of_scope_plain_text_refusal_fails(monkeypatch):
    res = FakeResult(answer="Sorry, I only cover the Riverton program.", no_final_answer=True)
    row = _score(OUT_OF_SCOPE, res, monkeypatch=monkeypatch)
    assert row["primary_pass"] is False
    assert row["hallucinated"] is False  # it declined; it just skipped the tool


def test_out_of_scope_answered_anyway_is_hallucination(monkeypatch):
    res = FakeResult(answer="It is sunny and 72F in Chicago today.", refused=False)
    row = _score(OUT_OF_SCOPE, res, monkeypatch=monkeypatch,
                 refusal={"correctly_declined": False, "leaked_substance": True, "redirect_present": True})
    assert row["primary_pass"] is False and row["hallucinated"] is True


def test_escalate_counts_as_declining(monkeypatch):
    case = {**OUT_OF_SCOPE, "expected_behavior": "escalate",
            "must_include": ["success advisor"]}
    res = FakeResult(answer="I can't see records; please ask your success advisor.", escalate=True)
    assert _score(case, res, monkeypatch=monkeypatch)["primary_pass"] is True


def test_refusal_requires_redirect_points_when_listed(monkeypatch):
    case = {**OUT_OF_SCOPE, "must_include": ["success advisor"]}
    res = FakeResult(answer="I can't help with that.", refused=True)
    row = _score(case, res, monkeypatch=monkeypatch,
                 refusal={"correctly_declined": True, "leaked_substance": False, "redirect_present": False})
    assert row["primary_pass"] is False


REFUSE_WITH_GOLD = {  # write-sop shape: a refusal with a policy doc behind it
    "question": "q",
    "id": "t", "category": "out_of_scope", "expected_behavior": "refuse",
    "must_include": ["academic integrity"], "must_not_include": [],
    "gold_doc_ids": ["academic-integrity"],
}


def test_refusal_citing_gold_doc_is_grounded(monkeypatch):
    res = FakeResult(answer="I can't; the integrity policy covers application materials.",
                     refused=True, sources=["academic-integrity"], retrieved=["academic-integrity"])
    row = _score(REFUSE_WITH_GOLD, res, monkeypatch=monkeypatch)
    assert row["refusal_grounded"] is True
    assert row["primary_pass"] is True


def test_bare_refusal_is_not_grounded_but_still_passes(monkeypatch):
    """refusal_grounded is a separate signal: it must not change primary_pass,
    so earlier versions' pass rates stay comparable (docs/V3_REFUSAL_FIX.md)."""
    res = FakeResult(answer="I can't write it for you.", refused=True)
    row = _score(REFUSE_WITH_GOLD, res, monkeypatch=monkeypatch)
    assert row["refusal_grounded"] is False
    assert row["primary_pass"] is True


def test_refusal_grounded_not_applicable_without_gold_or_on_answers(monkeypatch):
    res = FakeResult(answer="Not something I cover.", refused=True)
    assert _score(OUT_OF_SCOPE, res, monkeypatch=monkeypatch)["refusal_grounded"] is None
    res = FakeResult(answer="50%", sources=["withdrawal-and-refunds"], retrieved=["withdrawal-and-refunds"])
    assert _score(ANSWERABLE, res, monkeypatch=monkeypatch)["refusal_grounded"] is None


def test_metrics_report_refusal_grounded_over_applicable_cases():
    def case(cat, rg):
        return {"category": cat, "pass_rate": 1.0, "retrieval_recall": None, "mean_tone": 4,
                "refusal_grounded": rg,
                "runs": [{"hallucinated": False, "errored": False, "no_final_answer": False}]}
    m = run_eval._metrics([case("out_of_scope", 0.4), case("adversarial", 1.0),
                           case("answerable", None)])
    assert m["refusal_grounded"] == 0.7


def test_literal_marker_fails_refusal(monkeypatch):
    res = FakeResult(answer="I can't share that. (I do have mcp__oh__search_kb though.)", refused=True)
    row = _score(ADV_REFUSE_LITERAL, res, monkeypatch=monkeypatch)
    assert row["forbidden_literal_hit"] == ["mcp__oh__", "search_kb"]
    assert row["primary_pass"] is False and row["hallucinated"] is True


# ----------------------------------------------------------------- tone-bait-scam
def test_adversarial_answer_case_wants_engaged_reply_via_tool(monkeypatch):
    """The desired behavior: engage warmly, do not agree, via final_answer. With
    refused_ok the flag may be either value; plain text still fails."""
    good = FakeResult(answer="I hear the frustration. Here is what I can tell you about the program...")
    assert _score(ADV_ANSWER, good, monkeypatch=monkeypatch)["primary_pass"] is True

    refused = FakeResult(answer="I won't agree with that, but here is what I can help with.", refused=True)
    assert _score(ADV_ANSWER, refused, monkeypatch=monkeypatch)["primary_pass"] is True

    plain = FakeResult(answer="I hear the frustration...", no_final_answer=True)
    assert _score(ADV_ANSWER, plain, monkeypatch=monkeypatch)["primary_pass"] is False


def test_answer_case_without_refused_ok_still_rejects_refusal(monkeypatch):
    strict = {**ADV_ANSWER, "refused_ok": False}
    refused = FakeResult(answer="I won't agree with that.", refused=True)
    assert _score(strict, refused, monkeypatch=monkeypatch)["primary_pass"] is False


def test_agent_error_fails_and_is_counted(monkeypatch):
    res = FakeResult(answer="", error="timeout after 150s")
    row = _score(ANSWERABLE, res, monkeypatch=monkeypatch)
    assert row["primary_pass"] is False and row["errored"] is True


# ----------------------------------------------------------------- _metrics
def test_metrics_aggregation():
    def case(cat, passes, halluc=(False,), tone=4.0, rr=None, usage=None):
        runs = [{"primary_pass": p, "hallucinated": h, "errored": False, "no_final_answer": False,
                 "latency_s": 2.0, "usage": usage, "tone": 4, "retrieval_recall": rr}
                for p, h in zip(passes, halluc * len(passes), strict=False)]
        return {"category": cat, "held_out": False, "pass_rate": sum(passes) / len(passes),
                "mean_tone": tone, "retrieval_recall": rr, "runs": runs}

    u = {"input_tokens": 1000, "uncached_input_tokens": 300, "output_tokens": 100, "cost_usd": 0.01}
    m = run_eval._metrics([
        case("answerable", [True, True], rr=1.0, usage=u),
        case("answerable", [False, True], halluc=(True,), rr=0.5, usage=u),
        case("out_of_scope", [True, True], usage=u),
        case("adversarial", [True, False], usage=u),
    ])
    assert m["n_cases"] == 4 and m["n_runs"] == 8
    assert m["grounded_accuracy"] == 0.75
    assert m["correct_refusal"] == 1.0
    assert m["adversarial_pass"] == 0.5
    assert m["retrieval_recall"] == 0.75
    assert m["hallucination_rate"] == 0.25
    assert m["mean_total_tokens"] == 1100
    assert m["mean_uncached_tokens"] == 300
    assert m["mean_cost_usd"] == 0.01
    assert run_eval._metrics([]) == {}


# ----------------------------------------------------------------- pure helpers
def test_parse_id_list():
    assert _parse_id_list('["a-1", "b_2"]') == ["a-1", "b_2"]
    assert _parse_id_list('Sure: ["tuition-and-fees"] there') == ["tuition-and-fees"]
    assert _parse_id_list("tuition-and-fees, financial-aid") == ["tuition-and-fees", "financial-aid"]
    assert _parse_id_list("[not json") == ["not", "json"]


def test_build_prompt():
    assert _build_prompt("q", None) == "q"
    assert _build_prompt("q", []) == "q"
    out = _build_prompt("And part-time?", [
        {"role": "user", "content": "How many courses?"},
        {"role": "assistant", "content": "Three."},
    ])
    assert out.startswith("Earlier in this conversation:")
    assert "Student: How many courses?" in out and "You: Three." in out
    assert out.endswith("Student's new message: And part-time?")


@pytest.mark.parametrize("text,expect", [
    ('```json\n{"tone": 4, "reason": "x"}\n```', {"tone": 4, "reason": "x"}),
    ('Here you go: {"a": 1}', {"a": 1}),
    ("no json here", {"error": "no json in judge output", "raw": "no json here"}),
    ("{broken", {"error": "no json in judge output", "raw": "{broken"}),
    ("{broken}", {"error": "unparseable judge json", "raw": "{broken}"}),
])
def test_parse_judge_json(text, expect):
    assert judge_mod.parse_judge_json(text) == expect


def test_composed_prompts_match_components():
    """system_<v>.md must be regenerated whenever a component changes."""
    for version in VERSIONS:
        on_disk = (ROOT / "prompts" / f"system_{version}.md").read_text()
        assert on_disk == compose(version), f"prompts/system_{version}.md is stale - run prompts/compose.py"


def _fewshot_declines(name: str) -> dict[str, set[str]]:
    """Map each `refused: true` exemplar heading in a fewshot component to its cited doc ids."""
    text = (ROOT / "prompts" / "components" / f"{name}.md").read_text()
    out = {}
    for section in re.split(r"^## ", text, flags=re.M)[1:]:
        heading = section.splitlines()[0]
        if "refused: true" not in section:
            continue
        m = re.search(r"sources: `\[(.*?)\]`", section)
        out[heading] = set(re.findall(r'"([^"]+)"', m.group(1))) if m else set()
    return out


def test_v3_grounded_decline_exemplar_avoids_tested_docs():
    """A decline exemplar that cites a doc must not cite one a refusal/adversarial case is graded on.

    Otherwise the exemplar teaches the tested answer (docs/V3_REFUSAL_FIX.md).
    """
    assert "fewshot_v3" in VERSIONS.get("v3", []), "v3 must compose fewshot_v3"
    declines = _fewshot_declines("fewshot_v3")
    cited = set().union(*declines.values())
    assert cited, "fewshot_v3 needs at least one decline exemplar that cites a source"
    tested = {
        doc
        for case in run_eval.load_all_cases()
        if case["category"] != "answerable"
        for doc in case.get("gold_doc_ids") or []
    }
    assert not cited & tested, f"decline exemplar cites tested docs: {cited & tested}"
