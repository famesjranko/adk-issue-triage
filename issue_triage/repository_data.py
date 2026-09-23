"""Read-side repository data with a live GitHub source and snapshot fallback.

The agent should keep working when the portfolio repository is unavailable,
but a fallback must never make a write look successful. This module owns only
reads. Agent-facing tools adapt its results; ``apply_labels`` stays in the
GitHub tool module and always calls GitHub directly.

``TRIAGE_DATA_SOURCE`` controls selection:

* ``auto`` (default): prefer GitHub, fall back to the committed snapshot.
* ``live``: require GitHub; useful while refreshing or checking integration.
* ``snapshot``: deterministic repository reads without GitHub availability.
"""

from __future__ import annotations

import functools
import json
import os
import re
import urllib.error
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

REPOSITORY = "famesjranko/musicmeta"
SNAPSHOT_PATH = Path(__file__).with_name("data") / "musicmeta.snapshot.json"
RAW_MEDIA_TYPE = "application/vnd.github.raw"
VALID_MODES = {"auto", "live", "snapshot"}

T = TypeVar("T")
Request = Callable[..., Any]


class SnapshotMiss(LookupError):
  """The snapshot is valid but does not contain the requested record."""


class LiveReadUnavailable(RuntimeError):
  """GitHub responded, but its content cannot satisfy the read contract."""


@dataclass(frozen=True)
class ReadResult(Generic[T]):
  value: T
  source: str
  captured_at: str | None = None

  def metadata(self) -> dict[str, str]:
    out = {"source": self.source, "repository": REPOSITORY}
    if self.captured_at:
      out["captured_at"] = self.captured_at
    return out


@functools.cache
def load_snapshot() -> dict:
  """Load and minimally validate the committed snapshot once per process."""
  data = json.loads(SNAPSHOT_PATH.read_text())
  if data.get("schema_version") != 1:
    raise ValueError(f"unsupported snapshot schema in {SNAPSHOT_PATH}")
  if data.get("repository") != REPOSITORY:
    raise ValueError(f"snapshot repository is not {REPOSITORY}")
  if not isinstance(data.get("issues"), list) or not data["issues"]:
    raise ValueError(f"snapshot contains no issues: {SNAPSHOT_PATH}")
  return data


class RepositoryData:
  """A read gateway that keeps source selection out of agent tools."""

  def __init__(self, request: Request, mode: str | None = None):
    self._request = request
    self.mode = (mode or os.environ.get("TRIAGE_DATA_SOURCE", "auto")).lower()
    if self.mode not in VALID_MODES:
      choices = ", ".join(sorted(VALID_MODES))
      raise ValueError(f"TRIAGE_DATA_SOURCE must be one of: {choices}")

  def _read(self, live: Callable[[], T], saved: Callable[[dict], T]) -> ReadResult[T]:
    if self.mode == "snapshot":
      snapshot = load_snapshot()
      return ReadResult(saved(snapshot), "snapshot", snapshot["captured_at"])
    try:
      return ReadResult(live(), "github-live")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
            LiveReadUnavailable) as live_error:
      if self.mode == "live":
        raise
      try:
        snapshot = load_snapshot()
        return ReadResult(saved(snapshot), "snapshot", snapshot["captured_at"])
      except Exception:
        raise live_error

  def issue(self, number: int) -> ReadResult[dict]:
    number = int(number)

    def live() -> dict:
      issue = self._request("GET", f"/repos/{REPOSITORY}/issues/{number}")
      comments = self._request(
          "GET", f"/repos/{REPOSITORY}/issues/{number}/comments?per_page=10")
      return _public_issue(issue, comments)

    def saved(snapshot: dict) -> dict:
      issue = next((i for i in snapshot["issues"] if i["number"] == number), None)
      if issue is None:
        raise SnapshotMiss(f"issue {number} is not in the snapshot")
      return _public_issue(issue, issue.get("comments", []))

    return self._read(live, saved)

  def search(self, query: str) -> ReadResult[list[dict]]:
    query = str(query).strip()

    def live() -> list[dict]:
      q = urllib.parse.quote(f"{query} repo:{REPOSITORY} is:issue")
      data = self._request("GET", f"/search/issues?q={q}&per_page=10")
      return [_issue_match(i) for i in data["items"][:10]]

    def saved(snapshot: dict) -> list[dict]:
      terms = set(re.findall(r"[a-z0-9]+", query.lower()))
      ranked = []
      for issue in snapshot["issues"]:
        title = issue["title"].lower()
        body = (issue.get("body") or "").lower()
        score = sum(3 for term in terms if term in title)
        score += sum(1 for term in terms if term in body)
        if score:
          ranked.append((score, issue["number"], _issue_match(issue)))
      ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
      return [row[2] for row in ranked[:10]]

    return self._read(live, saved)

  def labels(self) -> ReadResult[list[dict]]:
    def live() -> list[dict]:
      data = self._request("GET", f"/repos/{REPOSITORY}/labels?per_page=100")
      if not data:
        raise LiveReadUnavailable("GitHub returned no repository labels")
      return [_label(item) for item in data]

    return self._read(live, lambda snapshot: list(snapshot["labels"]))

  def module_map(self) -> ReadResult[str]:
    def live() -> str:
      document = self._request(
          "GET", f"/repos/{REPOSITORY}/contents/ARCHITECTURE.md",
          accept=RAW_MEDIA_TYPE)
      try:
        return extract_module_map(document)
      except ValueError as exc:
        raise LiveReadUnavailable(str(exc)) from exc

    return self._read(live, lambda snapshot: snapshot["module_map"])

  def labeled_issues(self) -> ReadResult[list[dict]]:
    """Return every issue with labels, for rebuilding the evaluation corpus."""
    def live() -> list[dict]:
      issues = []
      page = 1
      while True:
        batch = self._request(
            "GET", f"/repos/{REPOSITORY}/issues?state=all&per_page=100&page={page}")
        issues.extend(_snapshot_issue(i) for i in batch if "pull_request" not in i)
        if len(batch) < 100:
          if not issues:
            raise LiveReadUnavailable("GitHub returned no repository issues")
          return issues
        page += 1

    return self._read(live, lambda snapshot: list(snapshot["issues"]))


def _public_issue(issue: dict, comments: list[Any]) -> dict:
  return {
      "number": issue["number"],
      "title": issue["title"],
      "body": (issue.get("body") or "")[:8000],
      "state": issue["state"].upper(),
      "comments": [
          ((comment.get("body") if isinstance(comment, dict) else comment) or "")[:2000]
          for comment in comments[:10]
      ],
  }


def _issue_match(issue: dict) -> dict:
  return {
      "number": issue["number"],
      "title": issue["title"],
      "state": issue["state"].upper(),
  }


def _label(item: dict) -> dict:
  return {"name": item["name"], "description": item.get("description") or ""}


def _snapshot_issue(issue: dict) -> dict:
  return {
      "number": issue["number"],
      "title": issue["title"],
      "body": issue.get("body") or "",
      "state": issue["state"].upper(),
      "comments": issue.get("comments") or [],
      "labels": [
          label["name"] if isinstance(label, dict) else label
          for label in issue.get("labels", [])
      ],
  }


def extract_module_map(document: str) -> str:
  lines = document.splitlines()
  start = next((i for i, line in enumerate(lines)
                if line.strip() == "## Module map"), None)
  if start is None:
    raise ValueError("ARCHITECTURE.md has no '## Module map' section")
  fence_open = next((i for i in range(start, len(lines))
                     if lines[i].startswith("```")), None)
  fence_close = (None if fence_open is None else
                 next((i for i in range(fence_open + 1, len(lines))
                       if lines[i].startswith("```")), None))
  if fence_close is None:
    raise ValueError("ARCHITECTURE.md has no fenced module map")
  return "\n".join(lines[fence_open + 1:fence_close])
