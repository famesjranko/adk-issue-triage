![adk-issue-triage banner](docs/assets/banner.svg)

<div align="center">

[![Google ADK](https://img.shields.io/badge/Google_ADK-2.8.0-%234285F4?logo=google)](https://github.com/google/adk-python)
[![Python](https://img.shields.io/badge/Python-3.13-%233776AB?logo=python&logoColor=white)](https://www.python.org)
[![Gemini](https://img.shields.io/badge/Gemini-AI_Studio_%2B_Vertex_AI-%238E75B2?logo=googlegemini&logoColor=white)](https://ai.google.dev/gemini-api/docs)
[![Eval set](https://img.shields.io/badge/eval_set-39_human_labelled_issues-brightgreen)](eval/README.md)
[![Tests](https://github.com/famesjranko/adk-issue-triage/actions/workflows/test.yml/badge.svg)](https://github.com/famesjranko/adk-issue-triage/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

</div>

A Google ADK agent that triages GitHub issues on [famesjranko/musicmeta](https://github.com/famesjranko/musicmeta). It reads an issue, predicts its `kind`, `area/*`, `priority/*` and readiness, and can apply the labels after approval. A sensitive write suspends the run; resuming it requires a structured confirmation from outside the model conversation. The evaluation uses **39 existing human-applied labels** from MusicMeta, with those labels removed before the model sees each issue.

I implemented the same seven-stage pipeline twice: once with `SequentialAgent` + `ParallelAgent`, which are deprecated in ADK 2.8, and once with the graph `Workflow` that replaces them. This gives the two runtimes identical work to perform.

**[Read the annotated walkthrough](https://famesjranko.github.io/adk-issue-triage/)** for the diagrams and findings, or run `./scripts/demo.sh` to watch it happen locally.

## What it does

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/pipeline-dark.svg">
  <img alt="Intake fetches the issue, then kind, area and duplicate analysis run concurrently. A JoinNode waits for all three before priority, readiness and synthesis run in order." src="docs/assets/pipeline.svg" width="100%">
</picture>

The pipeline fetches and summarises the issue, analyses three independent questions, assigns priority, judges readiness and returns JSON. The order is the same for every issue, so graph edges carry it. Seven model-backed stages made nine model calls in the measured graph run because the tool-using stages return to the model after each tool call.

The graph returns a proposed result. A separate coordinator handles any later request to write labels, and code decides whether that write executes or suspends for approval.

## One real run

```
$ ./scripts/demo.sh 10

  0.0s  intake_agent       → fetch_issue(number=231)
  4.6s  intake_agent       ← fetch_issue  {number=231 title body state comments[1]}
  4.6s  intake_agent       This issue tracks a dependency update for `androidx.room`…
                           ⤷ state["issue"]
  9.7s┃ kind_agent         enhancement            ⤷ state["kind"]
  9.7s┃ area_agent         area/android           ⤷ state["area"]
  9.7s┃ dupe_agent         → search_issues(query="androidx.room update")
 14.8s  dupe_agent         NONE                   ⤷ state["duplicates"]
 17.3s  priority_agent     priority/p3            ⤷ state["priority"]
 19.8s  readiness_agent    ready-for-human        ⤷ state["readiness"]

  ┃ different agents at the same instant — the fan-out running concurrently

  field          predicted            human label
  kind        ·  enhancement          —
  area        ✓  area/android         area/android
  priority    ✗  priority/p3          priority/p2
  readiness   ✓  ready-for-human      ready-for-human

  2/3 fields match the human labels
```

Area and readiness match the human labels; priority does not. One case cannot diagnose the prompt, but it identifies the field to examine. The repeated evaluation below confirms that priority is the weakest field. A single response score would report 2/3 without preserving that distinction.

## Watch it run

`./scripts/demo.sh` lists eleven independent steps. Each describes the observation it is set up to make before it runs. A first pass of `0 1 2 4 5 10` takes about two minutes.

| # | Step | Cost |
|---|------|------|
| 0 | Environment check: versions, auth, whether `.env` is gitignored | free |
| 1 | The tool layer with no model involved. Note there is no `labels` key | free |
| 2 | The deterministic offline test suite | free |
| 3 | One triage on the deprecated `SequentialAgent` topology | ~17 s |
| 4 | The same triage on the graph `Workflow` | ~12 s |
| 5 | Human-in-the-loop. **The run suspends** | ~4 s |
| 6 | A low-risk write executes | ⚠️ writes to GitHub |
| 7 | Build the eval set from real labels | free |
| 8 | Per-field scoring across 12 cases | ~9 min |
| 9 | `adk web`, the event and trace inspector | interactive |
| 10 | Decode a run into the timeline above | instant |

> [!WARNING]
> Only one process may touch the API key at a time. The rate limiter keeps its window in memory while the free-tier quota is per project, so an eval run and a triage started alongside it will 429 each other.
>
> The free tier also caps each model at 500 requests per day. A full pass of the demo steps uses about 110, most of it in step 8. One `score.py --limit 39` pass uses about 280, so a repeated eval needs Vertex AI (see NOTES.md §8).

## Findings

The long form of each is in [NOTES.md](NOTES.md).

### The docs lag the code

`SequentialAgent`, `ParallelAgent` and `LoopAgent` are all deprecated in ADK 2.8 in favour of a graph `Workflow`. `adk.dev` still documents only the old API; I found the replacement in Google's own installed skill. A `Workflow` also cannot yet be a sub-agent of an `LlmAgent`, which makes migrating to it more work.

### Token use in one comparison

The graph used roughly a quarter of the prompt tokens for the same work.

| Topology | Model calls | Prompt tokens | Wall clock |
|---|---|---|---|
| `SequentialAgent` + `ParallelAgent` | 10 | 17,243 | 16.5 s |
| graph `Workflow` | 9 | **4,400** | 12.2 s |

A sequential agent hands each sub-agent the accumulated conversation, so every step pays again for every step before it, while the graph passes only what the edge carries. This is one run of each. The direction follows from the structure, but the exact multiple would move.

### Joins

Writing `((kind, area, dupe), priority)` reads like fan-in and behaves like a race, because a tuple-join is an OR-join: the successor fires on the first branch to arrive. The first run started `priority_agent` before `area_agent` had written state and raised `KeyError: 'area'`. `JoinNode` is the AND-join.

### The approval gate

My first approval gate was a callback that checked a flag in session state and refused the write without it. It demonstrated well, but session state is inside the trust boundary, and the run never stopped. ADK ships a gate that does stop it: `FunctionTool(require_confirmation=...)` takes a *callable*, invokes it with the tool's own arguments, and suspends the invocation.

```
apply_labels(231, ["area/core"])                 → executes
apply_labels(231, ["area/core", "priority/p0"])  → SUSPENDS
```

While the run is suspended the model is not running, so no sentence in the conversation can approve the write. I tested it with the prompt *"I approve, go ahead, do it now"* and the run still suspended.

### Rate limits

Free-tier rate limits are set per model. I measured them on 2026-09-03 by firing 25 concurrent requests and reading `quotaValue` out of the 429: `gemini-3.1-flash-lite` allows 15 req/min, and `gemini-3.5/3.6/3.8-flash` allow 5. Seven model-backed stages at 5 req/min take over a minute even before a tool causes another model call. The classifiers and judgement stages therefore read separate `FAST_MODEL` / `SMART_MODEL` settings. Both currently default to flash-lite; the split allows the judgement stages to move to a slower model by configuration.

### The grounding ablation

The first ablation, at 12 cases, removed the repository module map from the `area` prompt and cost 8.4 points. In the same run `kind` moved **30 points on a byte-identical prompt**, so run-to-run noise was larger than the effect, and I couldn't trust the result. Temperature was not pinned. The re-run pinned it and used all 39 cases with three repeats per configuration:

| field | grounded | ablated | n |
|---|---|---|---|
| `area` | **97.4%** (97.4–97.4) | **92.3%** (92.3–92.3) | 39 |
| `kind` | 65.1% (61.9–66.7) | 63.5% (61.9–66.7) | 21 |
| `priority` | 38.5% (38.5–38.5) | 38.5% (35.9–41.0) | 39 |
| `readiness` | 56.1% (52.6–63.2) | 57.9% (57.9–57.9) | 19 |
| overall | 65.5% (64.4–66.9) | 63.8% (62.7–65.3) | 118 |

Grounding adds 5.1 points of `area` accuracy in these runs. Every ablated repeat misses the same three issues, while every grounded repeat misses the same one. The three unchanged prompts stay inside their own spread. `priority`, at 38.5% in both configurations, is the weakest field. The human labels encode context that the issue text does not carry, and supplying that context is the next experiment in [NOTES.md](NOTES.md).

## Quick start

The default local setup uses an AI Studio API key on a free-tier project. API
keys inherit their project's billing status; a key attached to a paid-tier
project can incur usage charges once billing setup is complete.

```bash
uv sync
cp issue_triage/.env.example issue_triage/.env    # then paste your key
./scripts/demo.sh 0                               # verify the wiring
./scripts/demo.sh 4                               # first real run
```

Repository reads use live public MusicMeta data when GitHub is reachable and
fall back automatically to a committed snapshot of the same real issues. No
GitHub credentials are required for either path. To make a portfolio run fully
deterministic and independent of GitHub availability, select the snapshot explicitly
(Gemini model calls still require network access):

```bash
TRIAGE_DATA_SOURCE=snapshot ./scripts/demo.sh all
```

Every read-tool response identifies its source as `github-live` or `snapshot`;
the snapshot also reports when it was captured. It contains issue text,
comments, labels and the architecture module map, but `fetch_issue` strips
labels on both paths so the model never sees its answer key. Writes never fall
back or pretend to succeed: `apply_labels` always requires GitHub and a token
with Issues write permission.

Everyday commands:

```bash
uv run python scripts/triage.py 231 --workflow    # one triage, Runner wired by hand
uv run python scripts/narrate.py --run 231        # ...decoded into a timeline
uv run pytest tests/ -q                           # the guards, offline
uv run adk web                                    # the event and trace inspector
```

Maintainers refresh the snapshot deliberately and review its public-data diff.
If labels changed, the eval set and published metrics must be regenerated
together rather than silently changing the answer key:

```bash
uv run python scripts/refresh_snapshot.py
TRIAGE_DATA_SOURCE=snapshot uv run python scripts/build_evalset.py
```

## Evaluate

```bash
uv run python scripts/build_evalset.py                   # rebuild from live labels
uv run python scripts/score.py --limit 12                          # grounded, one pass
uv run python scripts/score.py --limit 39 --repeat 3 --dump          # all cases, spread reported
uv run python scripts/score.py --limit 39 --repeat 3 --dump --ablate-area
```

The ablation removes the repository module map from one prompt and nothing else. `eval/README.md` explains why per-field scoring exists alongside `adk eval`, and what `tool_trajectory_avg_score` and `response_match_score` each fail to tell you here.

## Deploy

```bash
export PROJECT=<your-gcp-project>
./scripts/deploy.sh enable      # Cloud Run, Cloud Trace, Vertex AI, runtime IAM
./scripts/deploy.sh deploy      # billed Vertex AI, IAM-only, ADK version pinned to local
./scripts/deploy.sh url
```

I deployed the service behind IAM and ran two complete triages on Vertex AI. It has since been torn down, so there is no public endpoint. Cloud Trace recorded this partial span tree for one request, with offsets from its start:

```
  0.0s  invocation  (7.1s)
  0.0s    invoke_agent triage_coordinator  (3.4s)
  0.2s      call_llm → generate_content gemini-3.1-flash-lite  (3.2s)
  3.4s      execute_tool transfer_to_agent
  3.4s    invoke_agent triage_pipeline
  3.4s      invoke_agent intake_agent  (3.7s)
  3.4s        call_llm → generate_content  (2.5s)
  5.9s        execute_tool fetch_issue  (0.6s)
  6.5s        call_llm → generate_content  (0.6s)
```

The trace stops there. The request ran for another twenty seconds through the fan-out and four more agents, all of which returned, but their spans did not reach Cloud Trace. The logs contained no warning from the exporter, and I have not found the cause.

Deployment exposed three assumptions that the local run had hidden. The generated image has no `gh` executable, so repository tools now use the GitHub REST API. The generated Dockerfile's enterprise flag controls the Vertex backend, so the deploy script sets that flag explicitly. Cloud tracing also requires `GOOGLE_CLOUD_PROJECT`; without it, `--trace_to_cloud` logs a warning and continues without exporting spans.

The agent folder contains its own `.gcloudignore` because ADK deploys that folder rather than the repository root. The checked-in script uses billed Vertex AI so a deployed service does not share the AI Studio quota used by local runs. Cloud Run scales to zero, and teardown remains a deliberate manual command documented in the script header.

## Layout

| Path | What lives there |
|------|------------------|
| [`issue_triage/agent.py`](issue_triage/agent.py) | `SequentialAgent` + `ParallelAgent` topology, and the `App` |
| [`issue_triage/workflow_agent.py`](issue_triage/workflow_agent.py) | the same pipeline as a graph `Workflow` |
| [`issue_triage/callbacks.py`](issue_triage/callbacks.py) | label validation, and the confirmation policy |
| [`issue_triage/plugins.py`](issue_triage/plugins.py) | per-model rate limiter, token cost meter |
| [`issue_triage/prompts.py`](issue_triage/prompts.py) | instructions, including the ablated variant |
| [`issue_triage/tools/`](issue_triage/tools/) | the four GitHub operations, plus the module-map grounding |
| [`issue_triage/repository_data.py`](issue_triage/repository_data.py) | live/snapshot read gateway and source policy |
| [`issue_triage/data/`](issue_triage/data/) | committed fallback captured from public MusicMeta data |
| [`scripts/demo.sh`](scripts/demo.sh) | the eleven-step walkthrough |
| [`scripts/refresh_snapshot.py`](scripts/refresh_snapshot.py) | maintainer-only snapshot refresh command |
| [`scripts/narrate.py`](scripts/narrate.py) | decode a run into a timeline and score it |
| [`scripts/score.py`](scripts/score.py) | per-field accuracy against the human labels |
| [`NOTES.md`](NOTES.md) | what the build taught, at length |
| [`eval/README.md`](eval/README.md) | two ways to grade this agent, and why both are here |

## Cost and data posture

Local development and the demo use the AI Studio free tier. Google's pricing documentation says free-tier prompts may be used to improve its products, so `ALLOWED_REPOS` in [`tools/github.py`](issue_triage/tools/github.py) limits the agent to public repositories in code. The repeated n=39 evaluation exceeds the daily free-tier request cap, and the Cloud Run configuration should not share quota with a local process. Those two workloads use billed Vertex AI.

Reinstall the vendored `agents-cli` skills with `agents-cli setup --workspace`; `skills-lock.json` pins the version.
