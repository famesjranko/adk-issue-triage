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

### The re-run: n=39, three repeats, temperature pinned

Run on Vertex AI because the free tier's daily cap cannot fit it (§8). About
1.2M prompt tokens and 80k output tokens across the six passes on flash-lite,
which is cents. Per-case results for every repeat are in `eval/results/`.

| field | grounded mean (min–max) | ablated mean (min–max) | n |
|---|---|---|---|
| `area` | 97.4% (97.4–97.4) | 92.3% (92.3–92.3) | 39 |
| `kind` | 65.1% (61.9–66.7) | 63.5% (61.9–66.7) | 21 |
| `priority` | 38.5% (38.5–38.5) | 38.5% (35.9–41.0) | 39 |
| `readiness` | 56.1% (52.6–63.2) | 57.9% (57.9–57.9) | 19 |
| overall | 65.5% (64.4–66.9) | 63.8% (62.7–65.3) | 118 |

Three things fall out of it.

**The 30-point swing was the temperature.** With it pinned, `kind` moves five
points across three repeats and `area` and `priority` do not move at all. The
first attempt was not measuring the prompt; it was sampling.

**The grounding effect is real and small.** Five points of `area`, and the
ranges do not overlap: every ablated repeat misses the same three issues and
every grounded repeat the same one, which is exactly the shape a deterministic
prompt difference should have. The three
unchanged prompts stay inside their own spread, so this time the control was
designed rather than accidental.

**`priority` is the number to work on.** 38.5%, flat across every repeat and
both configurations. Variance is not the explanation and neither is grounding;
the prompt asks for a judgement the issue text does not support, and the human
labels encode context the model never sees. That is the next experiment, and
it wants a different input, not a better prompt.

## 7. Why I would not make these sub-agents A2A services

Six agents that share one session's state, run inside one request, and have one
owner and one deploy. Putting a network boundary between them would buy nothing
and cost a serialisation format, a failure mode per hop, and six things to
deploy. A2A earns its complexity when an agent has a **different owner, deploy
cadence, scaling profile, or trust boundary** — none of which is true here.

Local composition until there is a deployment reason to cross the boundary.

---

## 8. Deploying it found four things the local run could not

`adk deploy cloud_run` builds a `python:3.11-slim` image from the agent folder,
pushes it through Cloud Build, and runs `adk api_server --trace_to_cloud`. It
took three deploys to get a revision that served a triage. Each failure was a
gap between what the tooling implies and what it does.

**The build succeeds and the revision never starts.** The revision runs as the
default compute service account, which cannot read Secret Manager until told
so. Error is clear once you find it; `deploy.sh secret` now grants the role.

**The container has no `gh`.** The tool layer shelled out to the GitHub CLI,
which meant the deployed agent could not fetch an issue. Rewritten against the
REST API over stdlib `urllib`: same functions, same return shapes, reads of
public repositories need no token, writes refuse without `GITHUB_TOKEN`. That
also removed an undeclared runtime dependency the tests had been quietly
relying on.

**The ignore file is read from the agent folder, not the repo root.** The
deploy copies `issue_triage/` and honours only a `.gitignore` or
`.gcloudignore` inside it. There was none, so `issue_triage/.env` went into the
first two images. Both images and their source zips were deleted and the key
treated as burned; the folder now carries a `.gcloudignore`. Worth checking on any ADK
project before the first deploy.

**Two flags that look independent are not.** The generated Dockerfile sets
`GOOGLE_GENAI_USE_ENTERPRISE=1`. In this `google-genai` release that is the
new name for Vertex mode and wins on conflict, so setting
`GOOGLE_GENAI_USE_VERTEXAI=FALSE` alongside it does nothing, and setting the
enterprise flag to false forces API-key mode even with `VERTEXAI=TRUE`.
Reproduced locally in three lines before touching the service again.

**`--trace_to_cloud` is a no-op without `GOOGLE_CLOUD_PROJECT`.** ADK registers
the exporter only when that variable is set, otherwise it logs a warning and
carries on. The generated image happens to bake the variable in, but the
deploy script now sets it explicitly so the behaviour does not depend on that.

**And one open question.** Cloud Trace received the first fifteen spans of a
triage, coordinator through intake, with real durations. The remaining twenty
seconds of the request, the fan-out and four more agents, never arrived. No
export error at any severity, CPU throttling ruled out by re-running with CPU
always allocated. Recorded here rather than papered over; the span tree that
did arrive is in the README.

### The free tier has a daily cap, and it is the number that matters

The rate-limit table in §5 is requests per minute. There is also a per-model
**requests per day** limit, 500 for `gemini-3.1-flash-lite`, and it is the one
an eval hits. One pass over 39 cases is about 280 model calls; three repeats of
two configurations is around 1,640. The first re-run finished one clean repeat
and died in the second. Every retry after that point fails, so the run was
stopped rather than let it score exhausted calls as misses.

The per-minute retry did its job before that: a real 429 from a concurrent
request was absorbed by `RetryConfig(max_attempts=3)` on the node and the case
completed. The daily cap is not retryable, and no client-side limiter can see
it coming. The eval runs on Vertex AI for that reason, cost stated alongside
the numbers.

## Where it goes next

Three things the numbers point at, in the order I would do them.

**`priority` needs a different input, not a better prompt.** 38.5% in every
repeat of both configurations. The human labels encode context the issue text
does not carry: what else is open, what shipped recently, who is asking. The
`dupe_agent` already searches the repository; the next step is retrieval over
the closed issues and their outcomes, with citations in the output so a
reviewer can see why p2 rather than p3.

**Keep the traces.** `--trace_to_cloud` gives spans for free but drops most
of them here. The fix is an explicit OTel pipeline with a local collector,
which also makes the token meter a metric rather than a print.

**Put an API in front of it.** The graph runs behind ADK's own server today. A
small FastAPI service with a triage endpoint and a resume-on-confirm endpoint
is what a newsroom tool would actually call, and it is where the approval gate
stops being a demo and becomes a queue.

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
