"""GitHub read/write operations exposed to the triage agent as tools.

Each function here is a narrow, typed operation — the agent's only route to
GitHub. The docstrings are the model's API contract: they are what Gemini sees
when deciding which tool to call and with what arguments.
"""

import json
import subprocess

# Free-tier Gemini may retain prompts for product improvement, so the agent is
# hard-limited to repositories that are already public. This is enforced here in
# deterministic code rather than asked for in a prompt.
ALLOWED_REPOS = ("famesjranko/musicmeta", "famesjranko/MediaStack")

REPO = "famesjranko/musicmeta"


class RepoNotAllowed(RuntimeError):
    pass


def _gh(*args: str) -> str:
    if REPO not in ALLOWED_REPOS:
        raise RepoNotAllowed(f"{REPO} is not in the public-repo allowlist")
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def fetch_issue(number: int) -> dict:
    """Fetch the text of one GitHub issue so it can be triaged.

    Returns the issue's title, body, state, and comment text. Labels are
    deliberately NOT returned: the agent's job is to predict them, so exposing
    the existing labels would leak the answer.

    Args:
        number: The issue number, e.g. 231.

    Returns:
        A dict with keys: number, title, body, state, comments (list of str).
        On failure, a dict with an "error" key describing what went wrong.
    """
    try:
        raw = _gh(
            "issue", "view", str(number), "-R", REPO,
            "--json", "number,title,body,state,comments",
        )
    except Exception as exc:
        return {"error": str(exc)}
    data = json.loads(raw)
    return {
        "number": data["number"],
        "title": data["title"],
        "body": (data.get("body") or "")[:8000],
        "state": data["state"],
        "comments": [c["body"][:2000] for c in data.get("comments", [])][:10],
    }


def search_issues(query: str) -> dict:
    """Search the repository's issues by keyword, to find likely duplicates.

    Use short keyword queries drawn from the issue title, not full sentences.

    Args:
        query: Keywords to search for, e.g. "coroutines cache payload".

    Returns:
        A dict with key "matches": a list of up to 10 dicts, each with number,
        title and state. On failure, a dict with an "error" key.
    """
    try:
        raw = _gh(
            "issue", "list", "-R", REPO, "--search", query,
            "--state", "all", "--limit", "10", "--json", "number,title,state",
        )
    except Exception as exc:
        return {"error": str(exc)}
    return {"matches": json.loads(raw)}


def list_labels() -> dict:
    """List every label that exists in the repository.

    Only labels returned here may be applied. Any other label will be rejected
    by the tool layer.

    Returns:
        A dict with key "labels": a list of dicts with name and description.
    """
    try:
        raw = _gh("label", "list", "-R", REPO, "--limit", "100", "--json", "name,description")
    except Exception as exc:
        return {"error": str(exc)}
    return {"labels": json.loads(raw)}


def apply_labels(number: int, labels: list[str]) -> dict:
    """Apply the chosen labels to an issue. This WRITES to GitHub.

    This is a privileged operation. It is gated by an approval check outside the
    model: the write only proceeds once a human has approved the triage result.

    Args:
        number: The issue number to label.
        labels: The label names to apply. Every name must already exist in the
            repository (see list_labels).

    Returns:
        A dict with keys "applied" (the labels written) and "number".
        On failure, a dict with an "error" key.
    """
    try:
        _gh("issue", "edit", str(number), "-R", REPO,
            *[arg for label in labels for arg in ("--add-label", label)])
    except Exception as exc:
        return {"error": str(exc)}
    return {"number": number, "applied": labels}
