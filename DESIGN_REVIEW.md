# Office Hours — design review and recommendations

Reviewed 2026-09-09 against the Noodle *Prompt Systems Engineer* posting. Scope: all
code, prompts, eval/red-team data, result JSON, and the README/CHANGELOG/RUN_QUEUE
narrative. Everything below was verified by reading the source or re-running
offline checks (`pytest`, `ruff`, `retrieve.py recall`, compose sync) and
re-deriving numbers from the raw result files.

## 1. Verdict

The project is unusually complete for a portfolio piece. It covers every pillar
the posting names: composed and versioned prompts with a changelog, tool-use with
a structured `final_answer`, three retrieval arms, an LLM-judge eval with judge
validation, a red-team suite with a v1→v2 hardening loop, and Langfuse tracing
with eval scores attached to traces. The prompt components themselves are
well-written, and the KB/golden-set separation ("authored from canonical facts,
not KB prose") shows the right instincts.

The weak point is **the reporting, not the engineering.** A hiring manager for an
eval-focused role will read the README numbers critically, and right now several
headline claims do not survive a close read of the result files:

- the "router wins at the lowest cost" story is an accounting artifact,
- the README misdescribes what the router does,
- the tone metric drifted 0.5 points between two identical-config runs,
- the `full` arm's worst number is omitted from its comparison table,
- one adversarial case scores the *desired* behavior as a failure,
- and a known scoring false positive was left in the code across seven runs while
  its distortion was narrated away in prose.

None of these are hard to fix. But the job is literally "interpret results to make
improvement decisions" and "write clearly, precisely, and persuasively," so the
report layer is where the application is won or lost. Fix the P0 items, re-run
once, and rewrite the README as a short, honest report. That is the highest-value
day you can spend on this.

## 2. Priority list

**P0 — fix before anyone sees the repo** (each is a credibility risk)

1. Strip Claude Code's built-in tools from the agent (`tools=[]`). §3.1
2. Correct the router description and the router cost claim. §4.1
3. Replace the substring forbidden-marker check with the judge's negation-aware
   `forbidden_present`, or demote the substring hit to a soft flag. §5.1
4. Fix the `tone-bait-scam` scoring inversion and the dead `must_include` on
   refusal cases. §5.2, §5.3
5. Report variance honestly: pool the N=3 and N=5 runs, show per-case k/N with
   intervals, drop the run-to-run "narratives." §4.2
6. Remove sibling-repo references and internal review codes (C2, C8, D1, DESIGN
   §7) from README and code comments, or bring DESIGN.md into the repo. §7.1
7. Delete or rewrite `RUN_QUEUE.md`; make a first commit so `git_sha` is real. §7.2

**P1 — strengthens the fit for this specific role**

8. Real multi-turn via a session, and a "three-way chat" staff-assist mode. §6.1
9. A learning-objective case type graded against a rubric (Bloom's level). §6.2
10. Provider-agnostic model call so the OpenAI/Anthropic line in the posting is
    demonstrably covered. §6.3
11. Langfuse prompt management + datasets/experiments instead of homegrown JSON. §6.4
12. A student-in-distress rule in the prompt, exercised by A8. §3.4
13. Unit tests for the scoring logic; CI running tests, lint, and compose sync. §5.6
14. A short `PROMPT_GUIDELINES.md`: the reusable component library the posting asks
    for, written for a non-engineer configuring their own agent. §7.4

**P2 — polish**

15. Record `searches` in eval rows; pin the judge model; break out cached vs
    uncached tokens; add a LICENSE; package layout instead of `sys.path` hacks.

## 3. Agent and architecture

### 3.1 The agent is running with Claude Code's full built-in tool set in context

`agent.py:121` passes `allowed_tools=[...]` but never sets `tools`. In
`claude-agent-sdk` 0.2.152, `ClaudeAgentOptions.tools` "specifies the base set of
available built-in tools"; `[]` disables them. Without it the model sees Agent,
Bash, Edit, Read, ToolSearch, etc. as *defined but not permitted*.

Evidence:
- Red-team Q2 on v1 enumerated `Agent`, with its real parameter list
  (`description`, `prompt`, `subagent_type`, `model`, `isolation`). The README
  and `findings.md` call these "hallucinated harness tools." They are not
  hallucinated; they are in the model's context.
- Every run reports ~70–80k input tokens for a KB of 4,000 words. The KB plus
  system prompt plus four retrieved docs is well under 10k tokens. The rest is
  harness overhead (tool schemas, cache reads across turns).

Why it matters: the model is being asked to keep the Office Hours persona while
sitting inside a coding-agent harness. That inflates cost, adds a leak surface
that has nothing to do with your prompts, and makes `mean_total_tokens` a
misleading proxy. It also means the v2 "no tool enumeration" hardening was
partly fixing a configuration problem with prompt text.

Fix: `ClaudeAgentOptions(tools=[], ...)`. Then re-run Q2 on v1 and see whether
the leak was ever a prompt problem. Report tokens excluding cache reads, or
report cost only.

### 3.2 Multi-turn is simulated, not real

`_build_prompt` (`agent.py:71`) flattens prior turns into a single user message
prefixed "Earlier in this conversation." That is a reasonable smoke test but it is
not multi-turn: the model never sees its own prior `final_answer` tool call or the
prior retrieval, and there is no session state. The posting names "multi-turn
conversation logic" explicitly. See §6.1.

### 3.3 `usage` accounting

`agent.py:155–170` overwrites `usage` on every message that carries one, so the
value is whichever message came last. It works because the SDK's final
`ResultMessage` is last, but the intent should be explicit (read from
`ResultMessage` only). `input_tokens` sums cache reads and cache writes into
"input," which is why tokens look flat across arms while cost moves (§4.1).

### 3.4 No guidance for a student in distress

A8 (eviction) is the most realistic student-support turn in the suite, and the
prompt has nothing to say about it. The v2 answer is warm but drifts into general
knowledge ("Many schools have expedited processes...") with no retrieval
(`retrieved: []`), and `findings.md` marks it "content safe." For a retention and
student-support role this is the case a reviewer will read first. Add a scope or
safety rule: on signs of acute distress, acknowledge, escalate to a named human
channel, and never speculate about outcomes. Add a golden case for it.

### 3.5 Few-shot exemplars carry real KB facts

`fewshot_v2.md` states "$925 per credit, $27,750, $150 per-term technology fee"
inside an exemplar. The golden set was deliberately authored away from KB prose to
avoid overfitting; the exemplar undoes that for the single most-tested fact. A run
that skipped retrieval and cited `tuition-and-fees` would be caught by the
`invalid_sources` check, but a plain-text answer would not. Use placeholder values
or a different fact in exemplars.

### 3.6 Minor

- `scope.md` says "Do not give a partial answer to an out-of-scope question
  first," while `grounding_v2.md` says to state the correct fact alongside a
  refusal. A9 shows the model resolving this well, but the two rules should be
  reconciled in text.
- Whole-doc chunks with `k=4` is fine at 26 docs; say so as a deliberate choice
  and note what changes at 500 docs (chunking, hybrid retrieval, reranking).
- `max_turns=18` and `timeout_s=150` are undocumented in the README's cost
  discussion; a run that times out is scored as an error, which is right, but
  the reader should know the budget.

## 4. Reporting integrity (the README's claims vs the data)

### 4.1 The router section is wrong in two ways

**What the router does.** README: "a Haiku pre-pass that picks which arm to use
... routes narrow factual questions to a tight bm25 fetch and only falls back to
full-context when the query looks broad, so most runs carry fewer tokens." The
code (`retrieve.py:126–158`) does nothing like that. `LLMRouterRetriever` shows
the model a title/summary manifest and asks for up to `k` doc ids. There is no
bm25 arm, no full-context fallback, no breadth heuristic. It is an LLM
doc-selector. Describe it as that.

**Why it looks cheapest.** Mean input tokens per run are nearly identical across
arms (bm25 71.9k, router 69.2k). The cost difference is entirely the effective
price per token:

| arm | mean input tokens | mean cost | USD per M input tokens |
|---|---|---|---|
| bm25 | 71.9k | $0.0191 | 0.266 |
| router | 69.2k | $0.0150 | 0.216 |
| full | 87.4k | $0.0272 | 0.311 |

That ratio is a prompt-cache hit-rate artifact, not a retrieval effect. And the
router's own routing call (a separate `sdk_query` inside the retriever) is never
counted in `usage` at all, so the router's true cost is strictly higher than
reported. The honest headline is: "router had the best grounded accuracy at
comparable token volume; its extra LLM call is not yet costed."

### 4.2 Run-to-run variance is larger than the effects being reported

Same config, one day apart, v1 / bm25 / k=4:

| metric | N=3 (Sep 8) | N=5 (Sep 9) |
|---|---|---|
| grounded_accuracy | 0.889 | 0.822 |
| hallucination_rate | 0.05 | 0.08 |
| mean_tone | 3.83 | 4.33 |
| mean_cost_usd | 0.0188 | 0.0162 |

The tone judge moved +0.5 on a 5-point scale on 20 of 25 cases with no prompt
change. Either the unpinned `claude-sonnet-5` alias resolved differently, or the
judge is simply that noisy. Either way, the README then reports v1→v2 tone deltas
of −0.05 and −0.13 as if they carried information. Likewise `capstone-prereqs`
is narrated as "v1 1/3 → v2 0/3, not a prompt regression" at N=3 and then
"1/5 → 4/5, the headline" at N=5, with a post-hoc explanation for each. A reader
who opens the JSON will conclude the case is high-variance and the story was
fitted to whichever run came last.

Recommendations:
- Pool identical-config runs (v1 bm25 now has 8 runs per case; v2 has 8). Report
  per-case k/N with a Wilson interval, and a paired per-case comparison.
- State a minimum detectable effect for the harness at this N, and only call
  something a "regression" or "fix" if it clears it.
- Pin the judge model to a dated snapshot as soon as one exists, and add a small
  fixed judge-calibration set that runs before every eval so drift is visible.
- Record `searches` in eval rows (already done in red-team rows). Without it, you
  cannot tell "BM25 can't find it" from "the model wrote a bad query," which is
  the entire capstone story. Offline `retrieve.py recall` on the raw question
  finds `course-ds599-capstone` fine; the misses are query-rewrite variance.

### 4.3 Selective tables

- The retrieval-arm table omits `correct_refusal`, where `full` scored 0.75
  (`weather-chicago` 0/3). It was a false positive (§5.1), but omitting a column
  because one cell looks bad is the kind of thing a reviewer notices. Show every
  column, then explain the cell.
- "Judge validation: grounded 0.958 (7/8 rows)" mixes two denominators; 0.958 is
  per-field (23/24) and 7/8 is per-row. Pick one and label it.
- The N=5 section says "the two −1 cases are single-run judge-marker false
  positives." For `reveal-system-prompt` and `ignore-instructions-discount` at
  v2 N=5, `forbidden_hit` is empty on every run; the failures are plain-text
  replies scored by the refusal path. The diagnosis is wrong. Re-derive every
  explanatory sentence from the row-level data before publishing.

### 4.4 References that do not resolve

README and code point to `../application-automation/rag-prototype/DESIGN.md`,
`STATUS.md`, `ARCHITECTURE_REVIEW.md`, and `V1_BASELINE_REPORT.md`. None are in
this repo. Comments cite "review C1/C2/C3/C4/C5/C7/C8/C10/C11/D1/D2/D4/D5" and
"DESIGN §7." A reader cannot follow any of them. Either vendor DESIGN.md into
`docs/` and rewrite the codes as plain-language comments, or delete the
references.

## 5. Eval harness correctness

### 5.1 Substring forbidden-marker check produces known false positives

`run_eval.py:_forbidden_hit` is a case-insensitive substring match over the whole
answer, and its hit feeds `leaked`, `forb`, and therefore `hallucinated` and
`primary_pass`. Verified failures of correct behavior:

- `weather-chicago`: "gave no forecast" → forbidden "forecast" → run fails,
  judge says `correctly_declined: true, leaked_substance: false`. This is 0/3 on
  the `full` arm and 1–2 of 3 on others.
- `injection-poison-tuition` and others in the "negating clause" family.

The judge already handles negation (its rubric says a negated mention is not
forbidden). The CHANGELOG, README, and RUN_QUEUE each acknowledge the problem
("v2 eval-authoring cleanup, cf. C8") and it was never fixed in code; instead,
seven runs were published with a paragraph explaining which failures to ignore.
For an eval-engineering role, "I found my metric was wrong and fixed it" is the
story you want; "I found my metric was wrong and annotated around it" is not.

Fix options, cheapest first:
1. Use the substring hit as a soft flag (`forbidden_substring_hit`) recorded on
   the row but excluded from `primary_pass`/`hallucinated`; rely on the judge's
   `forbidden_present`.
2. Keep it hard only for unambiguous tokens that cannot appear in a negation
   (promo codes, `mcp__oh__`), and move phrase-level markers to the judge.

Then re-run and let the numbers be what they are.

### 5.2 `tone-bait-scam` scores the desired behavior as a failure

Category `adversarial`, `expected_behavior: answer`, so it goes down the
grounded path where `primary_pass` requires `not result.refused`. At v2 N=5, the
two runs that called `final_answer(refused=true)` **fail** and the three that
replied in plain text (`no_final_answer`) **pass**. The v2 prompt says every
refusal must go through `final_answer`; the eval punishes exactly that on this
case. Decide what "This program is a scam, admit it" should be (probably
`answer`, warm, non-agreeing, `refused: false`) and make the scoring match. Also
consider whether `no_final_answer` should ever count as `behavior_ok` on
refusal-path cases once v2's rule exists; right now the metric cannot see the
thing v2 was built to fix.

### 5.3 `must_include` is dead on refusal cases

`current-gpa`, `program-comparison`, `invest-refund`, `visa-question`,
`write-sop`, and `ignore-instructions-discount` all list `must_include`
phrases ("success advisor," "academic integrity," "immigration attorney"), but
`score_run` only passes `must_include` to `judge_grounded`. The refusal judge
never sees them. Either pass required redirect points to the refusal judge or
delete the fields so the golden set does not promise checks it does not run.

### 5.4 Judge design

- The tone judge grades against a generic rubric, not against `persona.md`.
  Give it the persona voice spec so "warm, concise, plain, non-preachy" is graded
  as *your* definition. Anchor all five points, not just 1/3/5.
- The refusal judge does not see the `refused`/`escalate` flags or retrieved
  docs. It cannot flag "refused=true but the answer gave substance," which is
  the one inconsistency worth catching.
- `substantive = len(answer) > 40` is a heuristic doing real work in
  `behavior_ok`. Document it or replace with the judge's judgment.
- Judge validation is 20 hand-written labels, 4 of them tone. That is enough to
  smoke-test the rubric, not to quote 0.95 as an agreement number. Sample 30–50
  real model outputs from the result files, label them blind, and report Cohen's
  kappa against the judge. Note that hand-written labels test the rubric, while
  labeled model outputs test the judge on the distribution it actually sees.

### 5.5 Golden set

- 25 cases, 11 answerable, 6 out-of-scope, 8 adversarial, 1 multi-turn, 0
  learning-objective, 0 distress. Fine for a first pass; say what you would add
  at 100 (paraphrase clusters per fact, more multi-turn, distress, accessibility
  requests, non-native-English phrasing).
- `gold_doc_ids` any-of semantics is a good call and well documented.
- Held-out slice: five cases, and the README reports held-out metrics for v1
  before v2 was designed. That is reporting, not tuning, so it is defensible, but
  say explicitly that no prompt change was made in response to a held-out result.

### 5.6 No tests on the part that matters

`tests/test_smoke.py` checks KB shape and golden-set schema. Nothing tests
`_forbidden_hit`, `score_run`, `_metrics`, `_parse_id_list`, `_build_prompt`,
`_ask_json_once` fence parsing, or that `system_*.md` matches `compose()`. All
are pure functions or trivially mockable. Ten fixture rows through `score_run`
would have caught §5.1 and §5.2. Add a GitHub Actions workflow running `pytest`,
`ruff`, and a compose-sync check.

## 6. Fit to the posting — what is missing

### 6.1 Multi-turn and "three-way chat"

The posting's most distinctive line is the learner / staff / staff's-AI-assistant
three-way chat. Nothing in the repo touches it, and multi-turn is simulated
(§3.2). Two concrete additions:

- Use `ClaudeSDKClient` (or resume/session ids) so A10 and `multiturn-parttime`
  run as real sessions with prior tool calls in context. Add 3–5 multi-turn
  golden cases with the gradual-push pattern.
- A `staff_assist` prompt variant: same KB, different audience. The assistant
  drafts a reply for an advisor to send, cites sources, flags anything that needs
  a human decision, and never addresses the student directly. Even one exemplar
  and two golden cases would show you have thought about the product.

### 6.2 Learning-objective evaluation

The posting asks for "alignment with rubric-based learning objectives" and
fluency in Bloom's Taxonomy. `diff-privacy-reading` is the only learning-content
case and it is graded as fact recall. Add a small tutoring task on the DS 550
readings: the student submits a short explanation, the agent gives feedback
against a 3-criterion rubric, and the judge grades the feedback for rubric
adherence and Bloom's level (e.g. does it push from "remember" to "apply"). This
is the single addition that most directly maps to the "learning" pillar.

### 6.3 Provider-agnostic model call

Everything routes through the Claude Agent SDK, which is a Claude Code harness
with subscription auth. That is a legitimate cost choice and the README says so,
but the posting names OpenAI and Anthropic APIs and "Noodle's AI orchestration
platform." A thin `llm.py` with `complete(messages, tools) -> ...` and two
backends (Anthropic Messages API, OpenAI Responses API) would make the prompt
components portable and let you say the eval harness is provider-neutral. Even
if only the Anthropic backend is exercised, the seam matters.

### 6.4 Langfuse: use the features the posting cares about

Tracing works and is verified. But the posting says "monitor prompt performance
in production, identify regressions, and prioritize prompt improvements." Langfuse
has first-class support for exactly this and the repo uses none of it:

- **Prompt management:** push `system_v1`/`system_v2` as Langfuse prompts with
  labels; fetch by label at run time; link the trace to the prompt version. Then
  `prompts/CHANGELOG.md` and the Langfuse prompt history agree.
- **Datasets + experiments:** upload `golden_set.yaml` as a dataset, run each
  eval as an experiment, and compare runs in the UI. This replaces the
  homegrown results JSON as the source of truth and is the workflow a Noodle
  team would actually use.
- **Sessions:** set `session_id` on multi-turn runs so A10 shows as one thread.
- **Cost:** the export shows `usageDetails: {}` and `totalCost: 0` on spans;
  attach model, tokens, and cost to the generation so Langfuse's cost dashboards
  work.

### 6.5 Guidelines document

"Contribute prompt engineering guidelines and best practices documentation for
internal teams who configure their own agents." Write a two-page
`PROMPT_GUIDELINES.md` for a non-engineer: what each component is for, when to
add a few-shot exemplar vs a rule, how to write a golden case, how to read the
eval output, and the three things the red-team taught. Cite your own results.

### 6.6 LTI / voice / multimodal

Not needed for this project, but a short "How this would plug into Noodle" note
(LTI launch → program context → KB attachment; what changes for voice: latency
budget, no markdown, shorter turns) shows you read the posting.

## 7. Repository hygiene and presentation

### 7.1 Make the repo self-contained

Vendor DESIGN.md into `docs/`, remove every `../application-automation` path,
and replace review codes with plain language. A reader should never hit a dead
link.

### 7.2 Remove process artifacts

`RUN_QUEUE.md` is an internal work log. It references a résumé file, a
"user chose to hold off" decision, and session-limit retries. It should not ship.
The same applies to "supersedes the earlier attempt discarded after it hit the
session limit" sentences in the README; keep one line in a "run integrity"
note if you want, and move the rest to `docs/RUN_LOG.md` or delete it.

Make a first commit. Every result file records `git_sha: nogit-dirty`, which
undercuts the reproducibility claims. Commit the v1 baseline state, tag it, then
commit v2, so the CHANGELOG points at real SHAs. "Establish prompt versioning
practices" is in the posting; git history is the simplest evidence.

Also: `.env` and the Langfuse export are correctly gitignored; `spikes/` can go
or move to `docs/`.

### 7.3 Rewrite the README as a report

Current README is ~16k characters of run logs. Structure it as:

1. One paragraph: what it is, what the loop is, what the headline finding is.
2. Architecture diagram (existing one is good).
3. Results: one table, pooled, with intervals; one paragraph per real finding.
4. What I got wrong and fixed (the marker check, the tool-set config, the router
   accounting). This section will do more for you than any metric.
5. Limitations and what I would do next.
6. Running it.

Move per-run detail to `docs/RESULTS.md`. Keep the CHANGELOG.

### 7.4 Code structure

- Package layout (`office_hours/agent.py`, `office_hours/eval/...`) removes the
  `sys.path.insert` hacks in three files and lets tests import cleanly.
- `judge.py` lives in `eval/` but is imported as a top-level module; that works
  only because the script directory is on `sys.path`.
- `config.py` runs a side effect at import (`_quiet_asyncio_child_watcher_noise`).
  Move it to an explicit `setup()` called by the entry points.
- The `Null` / `_Handle` tracing shim in `obs.py` is a clean pattern; keep it.

## 8. Suggested order of work

Day 1 (P0): set `tools=[]`; fix §5.1–§5.3; add scoring unit tests; record
`searches`; re-run v1 and v2 on bm25 at N=5; re-run the router with its call
costed. Rewrite README results from the new rows.

Day 2 (P1): real multi-turn + 3 cases; distress rule + 1 case; rubric-graded
learning case; Langfuse prompts + dataset; `PROMPT_GUIDELINES.md`; first commits
with tags; vendor DESIGN.md.

Optional: provider seam, staff-assist variant, "how this plugs into Noodle" note.

## 9. What to keep saying

These are genuinely good and should stay front and centre:

- Small agent model, bigger judge, with the reasoning stated.
- Component-composed prompts with frozen v1 and a written changelog.
- Golden set authored from canonical facts rather than KB prose.
- KB poisoning that *adds* a contradicting doc instead of replacing it.
- The few-shot ablation, including the non-obvious retrieval-recall effect
  (exemplars shape query phrasing). That is a real finding; keep it.
- Judge validation with deliberate near-misses.
- Eval scores attached to Langfuse traces, verified from an export.
