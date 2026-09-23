# Two ways to grade this agent, and why both are here

The checked-in cases were derived from human-applied labels on MusicMeta, not
invented for this demo. Normal runs prefer the live public repository and fall
back to [`issue_triage/data/musicmeta.snapshot.json`](../issue_triage/data/musicmeta.snapshot.json); set
`TRIAGE_DATA_SOURCE=snapshot` to reproduce the corpus without GitHub access.

## `adk eval` — the ADK-native path

```bash
uv run adk eval issue_triage eval/triage.evalset.json \
  --config_file_path eval/eval_config.json --print_detailed_results
```

Two metrics apply, and both have a caveat worth knowing before quoting a number
from them.

**`tool_trajectory_avg_score`** compares tool calls including their arguments,
exactly. That works for `fetch_issue(number=231)` — there is one right answer.
It does not work for `search_issues(query=...)`: `dupe_agent` composes its own
keywords, so two equally correct runs produce different arguments and the metric
scores phrasing rather than behaviour. The eval set therefore asserts only the
`fetch_issue` call, and the threshold is 0.0 — the score is recorded to be
looked at, not to gate.

**`response_match_score`** is ROUGE over the final response. The final response
here is a JSON object of four independent labels, so a single similarity number
cannot say *which* field regressed — and ROUGE will happily score a response
that got every label wrong but formatted them beautifully.

## `scripts/score.py` — what actually gets read

```bash
uv run python scripts/score.py --limit 12                    # grounded
uv run python scripts/score.py --limit 12 --ablate-area      # grounding removed
uv run python scripts/score.py --limit 12 --topology sequential
uv run python scripts/score.py --limit 39 --repeat 3 --dump    # spread, committed to eval/results/
```

Parses the JSON and scores each field separately, so `area/*` and `priority/*`
move independently and a regression points at the prompt that caused it. Fields
with no human label in the ground truth are excluded rather than counted as
misses — 20 of the 39 issues carry no readiness label, and scoring those as
failures would be inventing data.

The ablation is the point: `--ablate-area` removes the repository module map
from one prompt and nothing else. If `area` accuracy drops while the other three
fields hold, the grounding is doing work and the number says how much.
