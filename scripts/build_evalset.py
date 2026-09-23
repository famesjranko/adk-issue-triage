"""Turn the repository's own labelled issues into an ADK eval set.

The labels on musicmeta's closed issues were applied by a human. That makes them
ground truth we did not have to invent — the single reason this project has a
real eval loop rather than a decorative one.

    uv run python scripts/build_evalset.py

Writes eval/triage.evalset.json.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from issue_triage.repository_data import RepositoryData  # noqa: E402
from issue_triage.tools.github import REPO, _request  # noqa: E402

KINDS = {"bug", "enhancement", "documentation", "question", "invalid"}
READINESS = {"ready-for-agent", "ready-for-human", "needs-info"}


def truth(labels: list[str]) -> dict | None:
  """Project a label set onto the four fields the agent predicts.

  Returns None for issues that lack the two fields we always grade, so the eval
  set never contains a case whose answer we do not actually know.
  """
  area = next((label for label in labels if label.startswith("area/")), None)
  priority = next((label for label in labels if label.startswith("priority/")), None)
  if not area or not priority:
    return None
  return {
      "kind": next((label for label in labels if label in KINDS), None),
      "area": area,
      "priority": priority,
      "readiness": next((label for label in labels if label in READINESS), None),
  }


def main(output: Path | None = None) -> None:
  result = RepositoryData(_request).labeled_issues()
  issues = result.value

  cases, skipped = [], 0
  for issue in sorted(issues, key=lambda i: i["number"]):
    expected = truth([
        label["name"] if isinstance(label, dict) else label
        for label in issue["labels"]
    ])
    if expected is None:
      skipped += 1
      continue
    number = issue["number"]
    cases.append({
        "eval_id": f"issue-{number}",
        "conversation": [{
            "invocation_id": f"issue-{number}",
            "user_content": {"role": "user",
                             "parts": [{"text": f"Triage issue {number}."}]},
            "final_response": {"role": "model",
                               "parts": [{"text": json.dumps(expected)}]},
            # The one tool call we can assert exactly. dupe_agent's search
            # queries are freeform, so pinning them would grade phrasing, not
            # behaviour — see NOTES.md.
            "intermediate_data": {
                "tool_uses": [{"name": "fetch_issue", "args": {"number": number}}],
                "tool_responses": [],
            },
        }],
        # Deny the write path during eval: no issue is approved.
        "session_input": {"app_name": "issue_triage", "user_id": "eval", "state": {}},
    })

  out = output or ROOT / "eval" / "triage.evalset.json"
  out.write_text(json.dumps({
      "eval_set_id": "triage",
      "name": "musicmeta issue triage",
      "description": f"Human-applied labels on {REPO} issues, used as ground truth.",
      "eval_cases": cases,
  }, indent=2))

  print(f"{len(cases)} cases -> {out}")
  source = result.source + (f" · captured {result.captured_at}" if result.captured_at else "")
  print(f"source: {source}")
  print(f"{skipped} issues skipped (no area/* or no priority/*)")
  graded = sum(1 for c in cases
               if json.loads(c["conversation"][0]["final_response"]["parts"][0]["text"])["readiness"])
  print(f"{graded} of those also carry a readiness label")


if __name__ == "__main__":
  parser = argparse.ArgumentParser()
  parser.add_argument(
      "--output", type=Path,
      help="write somewhere other than eval/triage.evalset.json (used by the demo preview)")
  main(parser.parse_args().output)
