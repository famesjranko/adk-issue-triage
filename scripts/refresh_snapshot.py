"""Refresh the committed MusicMeta fallback from authenticated GitHub data.

This is a maintainer command, not part of normal demo execution:

    uv run python scripts/refresh_snapshot.py

It uses authenticated GitHub CLI queries to capture issue bodies, comments and
labels without consuming the unauthenticated REST rate limit.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from issue_triage.repository_data import (  # noqa: E402
    REPOSITORY,
    SNAPSHOT_PATH,
    extract_module_map,
)


def _gh(*args: str) -> str:
  return subprocess.run(
      ["gh", *args], capture_output=True, text=True, check=True
  ).stdout


def main() -> None:
  issues = json.loads(_gh(
      "issue", "list", "-R", REPOSITORY, "--state", "all", "--limit", "500",
      "--json", "number,title,body,state,labels,comments"))
  labels = json.loads(_gh(
      "label", "list", "-R", REPOSITORY, "--limit", "500",
      "--json", "name,description"))
  architecture = _gh(
      "api", f"repos/{REPOSITORY}/contents/ARCHITECTURE.md",
      "-H", "Accept: application/vnd.github.raw")

  if not issues or not labels:
    raise SystemExit("refusing to replace the snapshot with an empty dataset")

  normalized_issues = []
  for issue in sorted(issues, key=lambda item: item["number"]):
    normalized_issues.append({
        "number": issue["number"],
        "title": issue["title"],
        "body": issue.get("body") or "",
        "state": issue["state"].upper(),
        "comments": [comment.get("body") or "" for comment in issue.get("comments", [])],
        "labels": sorted(label["name"] for label in issue.get("labels", [])),
    })

  document = {
      "schema_version": 1,
      "repository": REPOSITORY,
      "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
      "module_map": extract_module_map(architecture),
      "labels": sorted(
          ({"name": item["name"], "description": item.get("description") or ""}
           for item in labels),
          key=lambda item: item["name"],
      ),
      "issues": normalized_issues,
  }
  SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
  SNAPSHOT_PATH.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
  print(f"{len(normalized_issues)} issues -> {SNAPSHOT_PATH}")


if __name__ == "__main__":
  main()
