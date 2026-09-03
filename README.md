# issue-triage — a Google ADK agent, built to learn ADK properly

Triages GitHub issues on [`famesjranko/musicmeta`](https://github.com/famesjranko/musicmeta):
predicts `kind`, `area/*`, `priority/*` and readiness, then applies the labels —
but only behind a deterministic approval gate.

The repo's 39 already-labelled issues are the ground truth, so the eval loop
grades against labels a human actually applied rather than against a rubric
invented for the demo.

**Read [`NOTES.md`](NOTES.md) first** — it is what the build actually taught,
including where the current ADK documentation is out of date.

## Layout

```
issue_triage/
  agent.py            SequentialAgent + ParallelAgent  (deprecated in ADK 2.8)
  workflow_agent.py   graph Workflow                    (the 2.8 replacement)
  prompts.py          instructions, incl. the ablated variant used by evals
  callbacks.py        label validation + approval gate — runs below the model
  plugins.py          per-model rate limiter, token cost meter
  tools/github.py     fetch_issue / search_issues / list_labels / apply_labels
  tools/repo.py       module map read out of the target repo's ARCHITECTURE.md
scripts/
  triage.py           one triage with the Runner wired explicitly
  score.py            per-field accuracy against the human labels
  build_evalset.py    labelled issues -> ADK EvalSet
  deploy.sh           Cloud Run + Cloud Trace
tests/test_guard.py   the guard, tested deterministically
eval/README.md        why per-field scoring exists alongside `adk eval`
```

Both topologies implement the same six steps, so they can be compared directly.

## Setup

Requires an AI Studio API key on a project with **billing disabled** — see the
cost note at the bottom of `NOTES.md`.

```bash
uv sync
printf 'GOOGLE_GENAI_USE_VERTEXAI=FALSE\nGOOGLE_API_KEY=...\n' > issue_triage/.env
```

## Run

```bash
uv run python scripts/triage.py 231                 # deprecated Sequential topology
uv run python scripts/triage.py 231 --workflow      # graph Workflow topology
uv run adk web                                      # events + trace inspector
uv run pytest tests/ -q                             # the guards
uv run python scripts/narrate.py --run 231          # decode a run into a timeline
uv run python scripts/narrate.py --latest           # ...or decode the newest adk web session
```

To see the run actually suspend for approval:

```bash
uv run python scripts/triage.py 231 --prompt \
  "Apply the labels area/android and priority/p2 to issue 231. I approve, go ahead."
```

It suspends anyway. Approval has to arrive as a structured `ToolConfirmation`
from whoever drives the runtime — text inside the conversation cannot supply it.

An `area/*`-only write does **not** suspend, and does write to the real
repository. That policy lives in `needs_confirmation()` in `callbacks.py`; set
`require_confirmation=True` in `agent.py` if you want every write gated.

⚠️ **Only run one process against the API key at a time.** The rate limiter is
process-local and the free-tier quota is per project — a triage run alongside an
eval run will 429 them both.

## Evaluate

```bash
uv run python scripts/build_evalset.py                      # rebuild from labels
uv run python scripts/score.py --limit 12                   # grounded
uv run python scripts/score.py --limit 12 --ablate-area     # grounding removed
```

The ablation removes the repository module map from one prompt and nothing else.
If `area` accuracy drops while the other fields hold, the grounding is earning
its place — and the number says by how much.

## Deploy

```bash
./scripts/deploy.sh enable
./scripts/deploy.sh secret
./scripts/deploy.sh deploy
./scripts/deploy.sh url
./scripts/deploy.sh traces
```

Teardown is deliberately manual; the command is in the header of `deploy.sh`.
