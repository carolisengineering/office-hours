# Local LLM support

**Goal.** Add a *backend* axis to the agent so the golden set can be run against
a model served locally by Ollama, and compared against the existing Claude runs.
Local models become a third comparison dimension alongside the retrieval arms
(`bm25 | full | router`) and the prompt versions (`v1 | v1_nofs | v2`).

**Non-goals.** Running the judge locally. Replacing the Claude path. Making the
agent offline-capable end to end. The judge stays on `claude-sonnet-5` — moving
the agent and the grader in the same change would make every resulting number
uninterpretable.

Decisions taken up front: runtime is **Ollama** (OpenAI-compatible server on
`http://localhost:11434/v1`); the reference backend stays **Claude via
`claude-agent-sdk`**, unchanged.

---

## 1. Why this is not a config flag

The Claude path does not talk to an HTTP API from this repo. `claude-agent-sdk`
spawns the `claude` CLI as a subprocess; the CLI speaks the Anthropic Messages
API and wraps our prompt in the Claude Code harness. Ollama speaks the OpenAI
chat-completions API. Nothing bridges those two shapes for free.

Verified against the installed versions (`uv sync --frozen`, 2026-09-11):

- `claude-agent-sdk` 0.2.152 exposes `ClaudeAgentOptions.env: dict[str, str]`,
  documented as "environment variables to pass to the Claude Code subprocess"
  (`types.py:2084`). Per-call env injection is therefore possible without
  touching the ambient process environment.
- `ClaudeAgentOptions.extra_args` and `settings` exist too, so the subprocess is
  configurable well beyond `model`.
- The `claude` CLI on this machine is 2.1.269.

That gives two candidate routes.

### Route A — point the subprocess at a translating proxy

Set `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_MODEL` in
`ClaudeAgentOptions.env`, and run a proxy that accepts Anthropic Messages
requests and re-emits them as OpenAI chat completions against Ollama.

Roughly ten lines of repo change. It is still the wrong thing to ship, for four
reasons:

1. **It measures the wrong prompt.** The local model receives the entire Claude
   Code harness preamble, not `prompts/system_v1.md`. The whole point of this
   repo is attributing behaviour to a versioned prompt; a harness we do not
   control sits in front of it and changes between CLI releases.
2. **Cost and token columns break.** `_usage_from()` reads `total_cost_usd` and
   the cache fields off the SDK's `ResultMessage`. A proxy has no real values for
   those, so `mean_cost_usd` and `cache_read_tokens` become fiction rather than
   an honest zero.
3. **Three-way opacity.** A failure is the proxy, the harness, or the model, and
   nothing in the result row tells you which.
4. **MCP tool plumbing** is a heavy ask for a small model on top of everything
   else.

**Route A turned out to be unnecessary even as a spike.** The question it was
meant to answer — can a local model hold a multi-turn tool loop — is answered
more directly by calling Ollama's OpenAI endpoint with our real schemas, which
is Route B's own premise. See section 6.

### Route B — a native local backend (recommended)

Own the loop. `openai>=2` pointed at Ollama's `/v1`, our tool schemas, our system
prompt, our turn cap, returning the same `AgentResult`. More code, but every
input the local model sees is one we chose, token accounting is real, and the
Claude path is untouched.

Note for future readers: the Claude backend must keep using `claude-agent-sdk`.
Do not "unify" the two behind the OpenAI-compatible client — an OpenAI-shaped
shim is the right tool for Ollama and the wrong tool for Claude.

---

## 2. Shape of the change

Today `search_kb` and `final_answer` are defined inside `answer()` in `agent.py`
as SDK-decorated closures over run state. Two backends cannot share that. The
refactor that makes everything else possible is to separate *what the tools do*
from *how a given SDK declares them*.

```
tools.py         tool behaviour + JSON schemas, no SDK import
  ├── backends/claude.py   wraps them with @tool / create_sdk_mcp_server   (today's code)
  └── backends/local.py    renders them as OpenAI tool specs, runs the loop (new)
agent.py         answer(...) dispatches on backend, owns AgentResult
```

One definition of the tools means the two backends genuinely test the same tool
surface, which is the only way the comparison means anything.

### `tools.py` (new)

Lift `FINAL_ANSWER_SCHEMA` and the two tool bodies out of `agent.py` into plain
async functions taking explicit run state instead of closing over it. No
behaviour change — the existing tests must stay green across this step alone.

### `backends/local.py` (new)

- Client: `openai.AsyncOpenAI(base_url=LOCAL_BASE_URL, api_key="ollama")`.
- Render the two schemas into the OpenAI `tools` array; call with
  `tool_choice="auto"`.
- Loop: request, execute any `tool_calls`, append results as `role: "tool"`
  messages, repeat until `final_answer` fires or `max_turns` is hit.
- Parse tool arguments with `json.loads` and record a parse failure as a run
  error rather than crashing the eval — the existing "record and return" posture
  in `answer()` is the model to follow.
- Usage: `prompt_tokens` / `completion_tokens` from the response;
  `cost_usd = None`. Null, not zero: zero reads as a measured result.

### `config.py`

```python
BACKENDS = ("claude", "local")
LOCAL_BASE_URL = os.getenv("OFFICE_HOURS_LOCAL_BASE_URL", "http://localhost:11434/v1")
LOCAL_MODEL    = os.getenv("OFFICE_HOURS_LOCAL_MODEL", "<pinned tag>")
LOCAL_TIMEOUT_S = 600.0
```

Pin the exact Ollama tag, quantization included, the same way `AGENT_MODEL` is
pinned to a dated snapshot. `qwen3:8b` is not a reproducible identifier across
re-pulls; the digest is.

### `agent.py`

`answer()` gains `backend: str = "claude"` and dispatches. `AgentResult` gains
`backend: str = "claude"` and `model` continues to hold whichever model actually
ran. Everything else in the dataclass is backend-neutral already, which is why
this fits.

---

## 3. Three couplings that are easy to miss

**The router arm makes its own model call.** `LLMRouterRetriever.retrieve()`
calls the SDK directly at `retrieve.py:139`. Under a local backend that call
would silently stay on Claude, so a "local" run would smuggle a Claude call into
its own retrieval and into its token totals. The router must take a
backend-supplied completion function. Until it does, `--backend local` should
reject `--arm router` outright rather than report a mixed run.

**Context is a server setting, not a model property.** The installed models
declare large windows — 262k for `qwen3-coder:30b`, 131k for `gpt-oss:20b` and
`llama3.3:70b` — so the `full` arm is not obviously out of reach. The trap is
that Ollama serves a much smaller `num_ctx` by default and truncates silently to
fit it. A truncated prompt that still returns a plausible answer is the worst
outcome available here. So the local backend must set `num_ctx` explicitly on
every request, record the value it used in the result row, and fail the run
loudly when the rendered prompt exceeds it. Never let the server decide.

**The 150s timeout was tuned for Haiku.** Less alarming than expected:
`qwen3-coder:30b` completed a two-search run in 16.5s, against a 15.4s published
mean for Haiku on `bm25`. But that is one warm run of one case on one model, and
a 70B model on the same hardware is a different story. Local runs still get
their own `LOCAL_TIMEOUT_S`, and the first full local eval should watch the
latency spread before it is fixed. Critically: **do not re-run the Claude
numbers under a different timeout** to make them match. The timeout is part of
what the published v1/v2 results mean.

---

## 4. Measurement integrity

| Concern | Decision |
|---|---|
| Judge | Stays `claude-sonnet-5`. Never moves in this change. |
| Result filename | Add the backend segment only when it is not `claude`, so the committed result files and the README's references to them stay valid. |
| Result header | Always record `backend`, `agent_model`, and the local base URL. |
| `mean_cost_usd` | `null` for local runs. |
| Tool-call failures | No new metric. `no_final_answer_rate` already measures exactly the failure local models exhibit — replying in prose instead of calling the tool. Observed on `gpt-oss:20b` in the step-0 spike on the easiest case in the set. Expect it to be the headline difference. |
| Comparison validity | Only same-arm, same-version, same-timeout pairs are comparable. A local run is not a v1-vs-v2 datapoint. |

The honest framing for the eventual README section is "how much of this prompt's
behaviour survives on a small local model", not "local model vs Haiku". The
prompt was iterated against Haiku, so Haiku has a home-field advantage that no
amount of care removes.

---

## 5. Tests

All offline, no network, consistent with `tests/test_smoke.py`:

- The shared schema renders to both the SDK shape and the OpenAI shape, and the
  required-field sets match.
- A fake OpenAI client drives the local loop through `search_kb` then
  `final_answer` and yields an `AgentResult` with the same fields populated as
  the Claude path produces.
- Malformed tool-call JSON from the fake client produces a recorded error, not
  an exception.
- A prompt that exceeds a stubbed context limit fails the run explicitly.
- `get_backend("nope")` raises, mirroring `get_retriever`'s behaviour.

---

## 6. Order of work

0. **Spike — done, 2026-09-11, passed.** A 60-line script posted the real
   `search_kb` and `final_answer` schemas and the composed `v1` system prompt to
   Ollama's `/v1/chat/completions`, and ran the tool loop by hand.
   `qwen3-coder:30b` issued two distinct `search_kb` calls, read the returned
   documents, and closed with a schema-valid `final_answer` carrying the correct
   tuition figure, in 16.5s. Tool-calling reliability is therefore not the
   blocker this plan assumed, and the loop in `backends/local.py` is a tidied
   version of that script rather than new territory.

   Two other models were probed on the same case, and both failures are useful:

   | model | result |
   |---|---|
   | `qwen3-coder:30b` | 2 searches, valid `final_answer`, 16.5s |
   | `gpt-oss:20b` | 1 search, then printed the answer JSON as prose instead of calling `final_answer`, 13.4s |
   | `llama3.3:70b` | no response within 600s |

   `gpt-oss:20b` produced exactly the `no_final_answer` failure this plan
   predicted, and produced it on the easiest case in the set. `llama3.3:70b` is
   not viable on this hardware at all. Start with `qwen3-coder:30b`.
1. **Extract `tools.py`.** No behaviour change. Existing tests green.
2. **Backend protocol; move the Claude path into `backends/claude.py`.** Prove
   it with one smoke eval that matches a committed result row.
3. **`backends/local.py` plus Ollama.** New tests.
4. **Router, timeout, and context-overflow handling** (section 3).
5. **Eval plumbing:** `--backend`, result header, filename rule.
6. **Run it:** `bm25`, v1 and v2, N=5, local and Claude side by side.
7. **Write up** in the README and `prompts/CHANGELOG.md`.

Steps 1 and 2 are worth doing regardless of whether the local backend ever
ships — they are the refactor that makes `agent.py` testable without a
subprocess.

## 7. Risks

- **The model cannot tool-call reliably.** Retired by step 0 for
  `qwen3-coder:30b`. Still live for any model swapped in later, so keep the
  spike script as a gate. Note that `gemma4:26b` advertises no tool capability
  at all and cannot serve as an agent backend, only as a judge or a summariser.
- **Latency makes N=5 over 25 cases impractical** on a laptop. At the measured
  16.5s per run a 25-case N=5 sweep is roughly 35 minutes of wall clock, which
  is tolerable. A 70B model is not: `llama3.3:70b` returned nothing within 600s
  on a single case. Mitigation if needed: run the main
  slice at N=3 locally and say so, rather than quietly changing N everywhere.
- **Ollama tag drift** silently changes the model under a fixed name. Pin the
  digest and record it in the result header.
- **Scope creep into "offline mode".** A local judge, local embeddings and an
  offline eval are each a separate decision. This plan deliberately buys one
  axis, not independence from the API.
