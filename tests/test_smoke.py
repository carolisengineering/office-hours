"""Fast offline checks - no network. Run: uv run pytest -q"""

from pathlib import Path

import pytest
import yaml

import retrieve
from retrieve import _tokenize, load_kb

ROOT = Path(__file__).parent.parent
KB = ROOT / "kb"
KB_POISONED = ROOT / "kb_poisoned"
GOLDEN = ROOT / "eval" / "golden_set.yaml"

VALID_CATEGORIES = {"answerable", "out_of_scope", "adversarial"}
VALID_BEHAVIORS = {"answer", "refuse", "escalate"}
REQUIRED_KEYS = {"id", "question", "category", "expected_behavior",
                 "must_include", "must_not_include", "gold_doc_ids"}


def test_tokenize_basic():
    assert _tokenize("DS 510: Machine-Learning!") == ["ds", "510", "machine", "learning"]
    assert _tokenize("") == []


def test_load_kb_shape():
    retrieve._KB_CACHE.clear()
    docs = load_kb()
    ids = [d.doc_id for d in docs]
    assert len(docs) >= 20
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))
    for d in docs:
        assert d.text.strip() and d.title.strip()
        assert (KB / f"{d.doc_id}.md").exists()


def test_poison_adds_alongside():
    retrieve._KB_CACHE.clear()
    clean = {d.doc_id for d in load_kb(poison=False)}
    retrieve._KB_CACHE.clear()
    poisoned = {d.doc_id for d in load_kb(poison=True)}
    added = poisoned - clean
    expected = {p.stem for p in KB_POISONED.glob("*.md")}
    assert added == expected
    for pid in expected:  # every poison doc shadows a real doc that is still present
        assert pid.replace("__POISONED", "") in poisoned
    retrieve._KB_CACHE.clear()


@pytest.fixture(scope="module")
def cases():
    return yaml.safe_load(GOLDEN.read_text())["cases"]


def test_golden_schema(cases):
    seen = set()
    kb_ids = {p.stem for p in KB.glob("*.md")} | {p.stem for p in KB_POISONED.glob("*.md")}
    for c in cases:
        missing = REQUIRED_KEYS - c.keys()
        assert not missing, f"{c.get('id')}: missing {missing}"
        assert c["id"] not in seen, f"duplicate id {c['id']}"
        seen.add(c["id"])
        assert c["category"] in VALID_CATEGORIES, c["id"]
        assert c["expected_behavior"] in VALID_BEHAVIORS, c["id"]
        assert isinstance(c["must_include"], list) and isinstance(c["must_not_include"], list), c["id"]
        assert isinstance(c.get("must_not_include_literal", []), list), c["id"]
        for did in c["gold_doc_ids"]:
            assert did in kb_ids, f"{c['id']}: gold_doc_id {did!r} has no kb file"
        for turn in c.get("context", []):
            assert turn.get("role") in {"user", "assistant"} and turn.get("content"), c["id"]


def test_golden_counts(cases):
    assert len(cases) == 25
    assert sum(1 for c in cases if c.get("held_out")) == 5
    assert sum(1 for c in cases if c.get("requires_poison")) >= 1


def test_system_prompt_composed():
    assert (ROOT / "prompts" / "system_v1.md").read_text().strip()


def test_answer_cases_that_expect_engagement_are_not_refusals(cases):
    # expected_behavior drives the scoring path; category is reporting only.
    for c in cases:
        if c["expected_behavior"] == "answer":
            assert c["category"] in {"answerable", "adversarial"}, c["id"]
        else:
            assert c["category"] in {"out_of_scope", "adversarial"}, c["id"]
