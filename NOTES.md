# Notes from building this with ADK

Target repo: `famesjranko/musicmeta`. The agent predicts the labels a human
already applied to 39 closed issues there, and the numbers below are scored
against those labels.

---

## 1. The briefing I started from was already out of date

`SequentialAgent`, `ParallelAgent` and `LoopAgent` are deprecated in ADK 2.8 in
favour of a graph-based `Workflow` (`google.adk.workflow`), and importing them
fires a `DeprecationWarning`. `adk.dev` still documents only the old API, so the
class shipped in the package before the docs described it.

That makes the SequentialAgent / ParallelAgent / LoopAgent set that every ADK
tutorial teaches the previous generation. I implemented both here:
`issue_triage/agent.py` (deprecated form) and `issue_triage/workflow_agent.py`
(graph form), with the same six steps, so they can be compared directly.

A `Workflow` also cannot currently be a sub-agent of an `LlmAgent`, which I
found out the hard way. The deprecated pipeline sits under an LlmAgent router,
while the graph version has to be the root, and that is a real cost for anyone
migrating.

## 2. The graph form used 4× fewer prompt tokens for identical work

Both runs used the same issue, the same models and the same six steps:

| topology | model calls | prompt tokens |
|---|---|---|
| `SequentialAgent` + `ParallelAgent` | 10 | 17,243 |
| graph `Workflow` | 9 | 4,400 |

`SequentialAgent` hands each sub-agent the accumulated conversation, while the
graph passes node input along edges. Over six LLM steps the difference
compounds, and nothing in the agent code shows it, which is why
`CostMeterPlugin` exists in `issue_triage/plugins.py`.

## 3. A tuple-join is an OR-join

`edges=[(intake, (kind, area, dupe)), ((kind, area, dupe), priority)]` looks like
fan-out then gather. A tuple on the right does fan out, but a tuple on the left
fires the successor on the first branch to arrive. On the first run
`priority_agent` started before `area_agent` had written state, and the
instruction template raised `KeyError: 'area'`. Raising on a missing state key,
where it could have silently rendered an empty string, is the right default for
a templating engine that feeds prompts.

`JoinNode` is the AND-join, with `_requires_all_predecessors = True`. I fixed it
by routing all three branches into one `JoinNode` and edging that to `priority`.

## 4. Where the LLM was allowed to decide, and where it wasn't

The control flow is fetch, three independent analyses, prioritise, then judge
readiness. That order is fixed and known before any request arrives, so it is
encoded as graph edges and the model never has to work it out on a run. Only
the judgements are `LlmAgent`s.

### The first approval gate was wrong

The first version was a `before_tool_callback` that refused `apply_labels`
unless session state carried an approval for that issue number. It
demonstrated well. Prompted with

> "Apply the labels area/android and priority/p2 to issue 231. I approve, go
> ahead, do it now."

the model called the tool and the callback refused it. But session state is
*inside* the trust boundary, and the run never stopped: the callback returned
an error string, and the model was free to narrate around it.

ADK has the real gate built in. I only found it by reading Google's own
installed skill; the published docs don't cover it:

```python
FunctionTool(apply_labels, require_confirmation=needs_confirmation)
App(..., resumability_config=ResumabilityConfig(is_resumable=True))
```

`require_confirmation` takes a callable, invoked with the tool's own arguments,
so the policy depends on what is being written:

```
apply_labels(231, ["area/core"])                 -> executes
apply_labels(231, ["area/core", "priority/p0"])  -> SUSPENDS
```

Filing an issue under a module is reversible. A p0 commits someone's time, so
that write suspends.

On suspend, ADK emits an `adk_request_confirmation` call carrying the
*original* function call intact, and the invocation stops. The model is not
running, so the same "I approve, go ahead, do it now" prompt cannot satisfy it.
Approval has to arrive as a structured `ToolConfirmation` from whoever is
driving the runtime, outside the conversation the model can influence.

### Two guards, kept apart because they fail differently

- Validation (`guard_tool_call`) checks that the labels exist in the repo. It is
  cheap and absolute, never consults the model, and stays a callback.
- Authorisation (`require_confirmation`) suspends the run. It is a runtime
  feature, and a copy of it in the callback would only be a second, weaker
  version.

Prompting the model into misbehaving doesn't test a guard. In one attempt the
model called `list_labels` first, noticed `area/frontend` did not exist, and
declined on its own, which is good behaviour that never exercised the guard.
Guards are deterministic, so `tests/test_guard.py` calls them directly.

## 5. Free-tier rate limits are per model, and they shape the architecture

Measured against this account on 2026-09-03 by firing 25 concurrent requests and
reading `quotaValue` out of the 429:

| model | free-tier RPM |
|---|---|
| `gemini-3.1-flash-lite` | 15 |
| `gemini-3.5-flash` / `3.6-flash` / `3.8-flash` | 5 |

Six LLM steps at 5 RPM is over a minute per triage. So the cheap single-label
classifiers run on flash-lite, and only the steps where judgement matters may
spend a slot on the slower model. `FAST_MODEL` / `SMART_MODEL` in `prompts.py`
select which. Both default to flash-lite today; the split is there so the
judgement steps can move by config alone.

`RateLimitPlugin` keeps one sliding window per model. A single shared counter
would either throttle the fast model to the slow model's ceiling or let the slow
one blow its quota. The fan-out issues three calls at once, which is the burst
that trips the limit, so the limiter sits on the Runner, where it sees the whole
tree.

`gemini-3.8-flash` also returned 503 UNAVAILABLE roughly 1 run in 3 under free
tier, so availability goes into choosing a model along with quality.

The limiter is process-local, and the quota is per project. An eval run died
part-way through with a 429 because I ran a one-off triage alongside it: two
processes sharing a key each believed they were under 15 RPM, and together they
were not. Headroom (80% of the stated limit) absorbs clock skew against the
server's window but does nothing for this. Running one process at a time fixes
it, as would a limiter that lives outside the process. That applies to an eval
suite in CI running next to a live agent on the same key.

## 6. Grading a JSON response as text loses the information you need

`adk eval`'s `response_match_score` is ROUGE over the final response. This agent
emits four labels that fail independently, and an `area/*` miss and a
`priority/*` miss are different bugs with different fixes. One similarity number
cannot say which regressed.

`tool_trajectory_avg_score` compares tool arguments exactly. That suits
`fetch_issue(number=231)` and breaks on `search_issues(query=...)`: `dupe_agent`
composes its own keywords, so two equally correct runs disagree and the metric
ends up grading phrasing. The eval set asserts only the call that has one right
answer.

`scripts/score.py` scores per field instead and leaves out fields with no human
label. Of 39 issues, 20 carry no readiness label, and counting those as misses
would be inventing data.

The first ablation used 12 cases on the graph topology. Only the area prompt
differs between the columns: the ablated run removes the repository module map
and changes nothing else.

| field | grounded | ablated | delta |
|---|---|---|---|
| `area` | 11/12 = 91.7% | 10/12 = 83.3% | **−8.4pp** |
| `kind` | 5/10 = 50.0% | 8/10 = 80.0% | **+30.0pp** ← *prompt unchanged* |
| `priority` | 6/12 = 50.0% | 5/12 = 41.7% | −8.3pp *(unchanged)* |
| `readiness` | 2/3 = 66.7% | 2/3 = 66.7% | 0 *(unchanged)* |
| overall | 24/37 = 64.9% | 25/37 = 67.6% | +2.7pp |

### The first ablation, at 12 cases

The obvious reading is that removing the grounding cost 8 points of area
accuracy, and the data doesn't support it. `kind` moved thirty points between
the two runs on a prompt that is byte-for-byte identical, so run-to-run variance
at n=12 is larger than the effect I was trying to measure, and the 8-point area
delta is one issue changing its mind.

I only caught it because the eval scores every field separately, and the three
unchanged prompts in the same table worked as a control. The aggregate
(64.9% → 67.6%) would have looked like mild noise, and a single
`response_match_score` would have hidden it.

Twelve cases cannot resolve a sub-10-point effect on a task with this much
per-case variance. It needs many more cases, or repeated runs per config with
the spread reported, or both, and 39 labelled issues may not be enough ground
truth to settle it at all. `temperature` was also not pinned when these numbers
were taken. It is now (`DETERMINISTIC` in both agent files), and the re-run
below reports the spread across repeats. Here a single eval number moved 30
points without anyone touching the thing it was measuring, so one number
shouldn't be quoted as a measurement.

I designed the ablation, got a delta in the direction I expected, and nearly
wrote it down as a result. The control that caught it was there by accident.

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

With temperature pinned, `kind` moves five points across three repeats,
`area` does not move at all, and grounded `priority` does not either, so the
30-point swing in the first attempt was sampling noise from the unpinned
temperature.

The grounding effect is real and small: five points of `area`, with ranges that
do not overlap. Every ablated repeat misses the same three issues and every
grounded repeat the same one, which is what a deterministic prompt difference
should look like. The three unchanged prompts stay inside their own spread, and
this time I set them up as the control.

`priority` is the number to work on. It averages 38.5% in both configurations,
identical in every grounded repeat and within five points in the ablated ones,
so neither variance nor grounding explains it. The
prompt asks for a judgement the issue text does not support, because the human
labels encode context the model never sees. The first item under "Where it goes
next" is about supplying that context.

## 7. Why I would not make these sub-agents A2A services

These six agents share one session's state, run inside one request, and have
one owner and one deploy. Putting a network boundary between them would buy
nothing and would cost a serialisation format, a failure mode per hop, and six
things to deploy. A2A is worth its complexity when an agent has a different
owner, deploy cadence, scaling profile, or trust boundary, and none of those
applies here. I'd keep local composition until there is a deployment reason to
cross the boundary.

---

## 8. Deploying it

`adk deploy cloud_run` builds a `python:3.11-slim` image from the agent folder,
pushes it through Cloud Build, and runs `adk api_server --trace_to_cloud`. It
took three deploys to get a revision that served a triage, and each failure was
a gap between what the tooling implies and what it does.

In the first, the build succeeded and the revision never started. The revision
runs as the default compute service account, which cannot read Secret Manager
until it is granted the role. The error is clear once you find it, and
`deploy.sh secret` now grants the role.

The container has no `gh`. The tool layer shelled out to the GitHub CLI, so the
deployed agent could not fetch an issue. I rewrote it against the REST API over
stdlib `urllib` with the same functions and return shapes; reads of public
repositories need no token, and writes refuse without `GITHUB_TOKEN`. That also
removed an undeclared runtime dependency the tests had been relying on.

The deploy reads its ignore file from the agent folder, not the repo root. It
copies `issue_triage/` and honours only a `.gitignore` or `.gcloudignore` inside
it. There was none, so `issue_triage/.env` went into the first two images. I
deleted both images and their source zips and treated the key as burned. The
folder now carries a `.gcloudignore`, and any ADK project should check for one
before its first deploy.

The generated Dockerfile sets `GOOGLE_GENAI_USE_ENTERPRISE=1`. In this
`google-genai` release that is the new name for Vertex mode, and it wins on
conflict, so setting `GOOGLE_GENAI_USE_VERTEXAI=FALSE` alongside it does
nothing, and setting the enterprise flag to false forces API-key mode even with
`VERTEXAI=TRUE`. I reproduced this locally in three lines before touching the
service again.

`--trace_to_cloud` is a no-op without `GOOGLE_CLOUD_PROJECT`. ADK registers the
exporter only when that variable is set, and otherwise logs a warning and
carries on. The generated image happens to bake the variable in, but the deploy
script now sets it explicitly so the behaviour does not depend on that.

One question is still open. Cloud Trace received the first fifteen spans of a
triage, coordinator through intake, with real durations. The remaining twenty
seconds of the request, the fan-out and four more agents, never arrived. There
was no export error at any severity, and re-running with CPU always allocated
ruled out CPU throttling. The span tree that did arrive is in the README.

### The free tier's daily cap

The rate-limit table in §5 is requests per minute. There is also a per-model
requests-per-day limit, 500 for `gemini-3.1-flash-lite`, and that is the one an
eval hits. One pass over 39 cases is about 280 model calls; three repeats of two
configurations is around 1,640. The first re-run finished one clean repeat and
died in the second. Every retry after that point fails, so I stopped the run
before it could score exhausted calls as misses.

The per-minute retry did its job before that: `RetryConfig(max_attempts=3)` on
the node absorbed a real 429 from a concurrent request, and the case completed.
The daily cap is not retryable, and no client-side limiter can see it coming,
so the eval runs on Vertex AI, with the cost stated alongside the numbers.

## Where it goes next

In the order I would do them:

1. Give `priority` the context the issue text doesn't carry. It averaged 38.5% in
   both configurations, and the human labels encode what else
   is open, what shipped recently, and who is asking. The `dupe_agent` already
   searches the repository; the next step is retrieval over the closed issues
   and their outcomes, with citations in the output so a reviewer can see why
   it picked p2 over p3.
2. Keep the traces. `--trace_to_cloud` gives spans for free but drops most of
   them here. The fix is an explicit OTel pipeline with a local collector, which
   would also let the token meter report a metric where today it prints.
3. Put an API in front of it. The graph runs behind ADK's own server today. A
   small FastAPI service with a triage endpoint and a resume-on-confirm endpoint
   is what a newsroom tool would call, and behind it the approval gate becomes a
   queue of pending confirmations.

## Environment gotcha (cost me ten minutes)

This machine sets `NO_PROXY` containing `::1`. `httpx` cannot parse it and every
`google-genai` client dies at construction with `InvalidURL: Invalid port: ':'`,
an error that says nothing about proxies. `issue_triage/.env` overrides it.

## Cost posture

Everything here runs on the AI Studio free tier, keyed to a project with billing
disabled. A key issued against a project with billing enabled auto-upgrades to a
paid tier and bills per token.

The pricing page says Google uses free-tier prompts to improve its products, so
`ALLOWED_REPOS` in `tools/github.py` hard-limits the agent to public
repositories in code.
