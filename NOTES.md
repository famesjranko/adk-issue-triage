# ADK notes — things observed while building this, not read in a doc

Target repo: `famesjranko/musicmeta`. The agent predicts the labels a human
already applied to 39 closed issues, so every claim below has a number behind it.

---

## 1. The briefing I started from was already out of date

`SequentialAgent`, `ParallelAgent` and `LoopAgent` are **deprecated in ADK 2.8**
in favour of a graph-based `Workflow` (`google.adk.workflow`). The deprecation
fires as a `DeprecationWarning` on import. `adk.dev` still documents the old API
exclusively — the class ships in the package before the docs describe it.

So the "SequentialAgent / ParallelAgent / LoopAgent" triad that every ADK
tutorial teaches is the previous generation. Both are implemented here:
`issue_triage/agent.py` (deprecated form) and `issue_triage/workflow_agent.py`
(graph form), same six steps, so they can be compared directly.

**Constraint found the hard way:** a `Workflow` cannot currently be a sub-agent
of an `LlmAgent`. The deprecated pipeline sits under an LlmAgent router; the
graph version has to be the root. That is a real migration cost, not a detail.

## 2. The graph form used 4× fewer prompt tokens for identical work

Same issue, same models, same six steps:

| topology | model calls | prompt tokens |
|---|---|---|
| `SequentialAgent` + `ParallelAgent` | 10 | 17,243 |
| graph `Workflow` | 9 | 4,400 |

`SequentialAgent` hands each sub-agent the accumulated conversation. The graph
passes node input along edges. At six LLM steps that compounds, and nothing in
the agent code makes it visible — which is the entire reason `CostMeterPlugin`
exists in `issue_triage/plugins.py`.

## 3. A tuple-join is an OR-join, and it bit immediately

`edges=[(intake, (kind, area, dupe)), ((kind, area, dupe), priority)]` looks like
fan-out then gather. It is not. A tuple on the **right** fans out; a tuple on the
**left** fires the successor on the **first** branch to arrive. First run:
`priority_agent` started before `area_agent` had written state, and the
instruction template raised `KeyError: 'area'`.

`JoinNode` is the AND-join — `_requires_all_predecessors = True`. Fixed by
routing all three branches into one `JoinNode` and edging that to `priority`.

Worth stating plainly: **the failure was loud.** A missing state key raised
rather than silently rendering an empty string, which is the right default for a
templating engine that feeds prompts.

## 4. Where the LLM was allowed to decide, and where it wasn't

The control flow — fetch, then three independent analyses, then prioritise, then
judge readiness — is fixed and known before any request arrives. It is encoded
as graph edges. Nothing asks the model to rediscover it on every run.

Only the judgements are `LlmAgent`s. That split is the whole design.

### I built the approval gate wrong first, and the mistake is the lesson

**v1:** a `before_tool_callback` that refused `apply_labels` unless session state
carried an approval for that issue number. It demonstrated well — prompted with

> "Apply the labels area/android and priority/p2 to issue 231. I approve, go
> ahead, do it now."

the model called the tool and the callback refused it. But session state is
*inside* the trust boundary. That gate has the shape of authorisation without
the substance, and the run never actually stopped — it just returned an error
string the model was free to narrate around.

**v2:** ADK ships the real primitive, and I only found it by reading Google's
own installed skill rather than the published docs:

```python
FunctionTool(apply_labels, require_confirmation=needs_confirmation)
App(..., resumability_config=ResumabilityConfig(is_resumable=True))
```

`require_confirmation` takes a **callable**, invoked with the tool's own
arguments, so the policy is a function of what is actually being written:

```
apply_labels(231, ["area/core"])                 -> executes
apply_labels(231, ["area/core", "priority/p0"])  -> SUSPENDS
```

Filing an issue under a module is reversible. Asserting p0 is a claim about
someone's week.

What happens on suspend is the part worth seeing. ADK emits an
`adk_request_confirmation` call carrying the *original* function call intact,
and the invocation stops. The model is not running. So the same "I approve, go
ahead, do it now" prompt cannot satisfy it — approval has to arrive as a
structured `ToolConfirmation`, from whoever is driving the runtime, not as text
inside the conversation the model can influence.

That is the difference between a gate that refuses and a gate that suspends.

### Two guards, kept apart because they fail differently

- **validation** (`guard_tool_call`) — labels must exist in the repo. Cheap,
  absolute, never consults the model. Stays a callback.
- **authorisation** (`require_confirmation`) — suspends the run. Not a callback;
  a runtime feature. Duplicating it in the callback would only add a second,
  weaker copy.

**Testing note:** trying to prove a guard by prompting the model into
misbehaving proves nothing. In one attempt the model called `list_labels` first,
noticed `area/frontend` did not exist, and declined on its own — good behaviour,
zero coverage. Guards are deterministic, so `tests/test_guard.py` calls them
directly.

## 5. Free-tier rate limits are per model, and they shape the architecture

Measured against this account on 2026-09-03 by firing 25 concurrent requests and
reading `quotaValue` out of the 429:

| model | free-tier RPM |
|---|---|
| `gemini-3.1-flash-lite` | 15 |
| `gemini-3.5-flash` / `3.6-flash` / `3.8-flash` | 5 |

Six LLM steps at 5 RPM is over a minute per triage. So the cheap single-label
classifiers run on flash-lite and only the steps where judgement matters may
spend a slot on the slower model — `FAST_MODEL` / `SMART_MODEL` in `prompts.py`.
Both default to flash-lite today; the seam is there so the judgement steps can
move by config alone.

`RateLimitPlugin` keeps **one sliding window per model**, not one per project. A
single shared counter would either throttle the fast model to the slow model's
ceiling or let the slow one blow its quota. And the fan-out issues three calls at
once, which is precisely the burst that trips the limit — so the limiter belongs
on the Runner, where it sees the whole tree, not on any one agent.

`gemini-3.8-flash` also returned 503 UNAVAILABLE roughly 1 run in 3 under free
tier. Model choice is an availability decision, not just a quality one.

**The limiter is process-local; the quota is per project.** An eval run died
part-way through with a 429 because I ran a one-off triage alongside it. Two
processes sharing a key each believed they were under 15 RPM and together were
not. Headroom (80% of the stated limit) absorbs clock skew against the server's
window, but it does not fix this — only one process at a time does, or a limiter
that lives outside the process. Worth knowing before anyone runs an eval suite
in CI next to a live agent on the same key.

## 6. Grading a JSON response as text loses the information you need

`adk eval`'s `response_match_score` is ROUGE over the final response. This agent
emits four independent labels that fail independently: an `area/*` miss and a
`priority/*` miss are different bugs with different fixes. One similarity number
cannot say which regressed.

`tool_trajectory_avg_score` compares tool arguments exactly, which is right for
`fetch_issue(number=231)` and wrong for `search_issues(query=...)` — `dupe_agent`
composes its own keywords, so two equally correct runs disagree and the metric
grades phrasing. The eval set asserts only the call that has one right answer.

`scripts/score.py` scores per field instead, and excludes fields with no human
label rather than counting them as misses — 20 of 39 issues carry no readiness
label, and grading those as failures would be inventing data.

**12 cases, graph topology. Only the area prompt differs between the columns —
the ablated run removes the repository module map and changes nothing else.**

| field | grounded | ablated | delta |
|---|---|---|---|
| `area` | 11/12 — 91.7% | 10/12 — 83.3% | **−8.4pp** |
| `kind` | 5/10 — 50.0% | 8/10 — 80.0% | **+30.0pp** ← *prompt unchanged* |
| `priority` | 6/12 — 50.0% | 5/12 — 41.7% | −8.3pp *(unchanged)* |
| `readiness` | 2/3 — 66.7% | 2/3 — 66.7% | 0 *(unchanged)* |
| overall | 24/37 — 64.9% | 25/37 — 67.6% | +2.7pp |

### The experiment failed, and that is the finding

The tidy story would be "removing the grounding cost 8 points of area accuracy."
It is not supportable. `kind` moved **thirty points** between the two runs on a
prompt that is byte-for-byte identical. So run-to-run variance at n=12 is larger
than the effect I was trying to measure, and the 8-point area delta is one
issue changing its mind.

I only caught it because the eval scores every field separately. Three unchanged
prompts sitting in the same table are an accidental control channel — the
aggregate (64.9% → 67.6%) would have looked like mild noise and told me nothing,
and a single `response_match_score` would have hidden it completely.

What this actually says:

- **12 cases cannot resolve a sub-10-point effect** on a task with this much
  per-case variance. Either many more cases, or repeated runs per config with
  the spread reported, or both — and 39 labelled issues may simply not be enough
  ground truth to settle it at all.
- **`temperature` was not pinned when these numbers were taken.** It is now
  (`DETERMINISTIC` in both agent files), and the re-run below reports spread
  across repeats rather than one sample.
- **Quoting a single eval number as if it were a measurement is the trap.** The
  number moved 30 points without anyone touching the thing it was measuring.

The mistake worth not repeating: I designed the ablation, got a delta in the
direction I expected, and nearly wrote it down as a result. The control channel
was there by accident, not because I planned one.

## 7. Why I would not make these sub-agents A2A services

Six agents that share one session's state, run inside one request, and have one
owner and one deploy. Putting a network boundary between them would buy nothing
and cost a serialisation format, a failure mode per hop, and six things to
deploy. A2A earns its complexity when an agent has a **different owner, deploy
cadence, scaling profile, or trust boundary** — none of which is true here.

Local composition until there is a deployment reason to cross the boundary.

---

## Environment gotcha (cost me ten minutes)

This machine sets `NO_PROXY` containing `::1`. `httpx` cannot parse it and every
`google-genai` client dies at construction with `InvalidURL: Invalid port: ':'`
— an error that says nothing about proxies. Overridden in `issue_triage/.env`.

## Cost posture

Everything here runs on the AI Studio **free tier**, keyed to a project
with billing disabled. A key issued against a project
with billing enabled auto-upgrades to a paid tier and bills per token.

Free tier prompts are used to improve Google's products — the pricing page says
so explicitly. `ALLOWED_REPOS` in `tools/github.py` therefore hard-limits the
agent to public repositories in code, rather than asking a prompt to be careful.
