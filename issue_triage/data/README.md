# Repository snapshot

`musicmeta.snapshot.json` is generated from the public
`famesjranko/musicmeta` repository. It is runtime fallback data, not a synthetic
test fixture and not an editable source of truth.

The snapshot contains:

- issue titles, bodies, states and up to the captured comments;
- repository labels, used to rebuild the evaluation corpus; and
- the module map extracted from `ARCHITECTURE.md`, used to ground area labels.

Agent tool responses never expose an issue's captured labels. The same
projection removes them from live and snapshot reads before Gemini sees the
issue.

Refresh it only through:

```bash
uv run python scripts/refresh_snapshot.py
```

Review the public-data diff before committing. If human labels changed, rebuild
the eval set and rerun any published metrics before updating their answer key.
The JSON carries its schema version, repository identity and UTC capture time.
