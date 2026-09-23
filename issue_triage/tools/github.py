"""GitHub read/write operations exposed to the triage agent as tools.

Each function here is a narrow, typed operation — the agent's only route to
GitHub. The docstrings are the model's API contract: they are what Gemini sees
when deciding which tool to call and with what arguments.

Live transport is the GitHub REST API over stdlib urllib, so the agent runs
anywhere Python does (the Cloud Run image has no `gh` CLI). Reads need no
credentials and fall back to the committed public-data snapshot when GitHub is
unavailable. Writes never fall back: apply_labels needs $GITHUB_TOKEN with
issues:write on the target repository or returns an error without requesting.

Every public function returns a dict and never raises: failures come back as
{"error": "..."} so the model can see and react to them.
"""

import json
import os
import urllib.error
import urllib.request

from issue_triage.repository_data import REPOSITORY, RepositoryData

# Free-tier Gemini may retain prompts for product improvement, so the agent is
# hard-limited to repositories that are already public. This is enforced here in
# deterministic code rather than asked for in a prompt.
ALLOWED_REPOS = ("famesjranko/musicmeta", "famesjranko/MediaStack")

REPO = REPOSITORY

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
JSON_MEDIA_TYPE = "application/vnd.github+json"
TIMEOUT_SECONDS = 30


class RepoNotAllowed(RuntimeError):
    pass


def _repo() -> str:
    """The target repository, refused unless it is on the public allowlist."""
    if REPO not in ALLOWED_REPOS:
        raise RepoNotAllowed(f"{REPO} is not in the public-repo allowlist")
    return REPO


def _describe(exc: Exception) -> str:
    """A one-line, model-readable account of a failed request."""
    if isinstance(exc, urllib.error.HTTPError):
        detail = exc.reason
        try:
            payload = json.loads(exc.read() or b"{}")
            detail = payload.get("message") or detail
        except (ValueError, OSError, AttributeError):
            pass
        return f"GitHub API {exc.code} for {exc.url}: {detail}"
    if isinstance(exc, urllib.error.URLError):
        return f"GitHub API unreachable: {exc.reason}"
    if isinstance(exc, TimeoutError):
        return f"GitHub API timed out after {TIMEOUT_SECONDS}s"
    return str(exc)


def fetch_issue(number: int) -> dict:
    """Fetch the text of one GitHub issue so it can be triaged.

    Returns the issue's title, body, state, and comment text. Labels are
    deliberately NOT returned: the agent's job is to predict them, so exposing
    the existing labels would leak the answer.

    Args:
        number: The issue number, e.g. 231.

    Returns:
        A dict with keys: number, title, body, state, comments (list of str),
        and _meta (the live or snapshot source).
        On failure, a dict with an "error" key describing what went wrong.
    """
    try:
        _repo()
        result = RepositoryData(_request).issue(number)
        return result.value | {"_meta": result.metadata()}
    except Exception as exc:
        return {"error": _describe(exc)}


def search_issues(query: str) -> dict:
    """Search the repository's issues by keyword, to find likely duplicates.

    Use short keyword queries drawn from the issue title, not full sentences.

    Args:
        query: Keywords to search for, e.g. "coroutines cache payload".

    Returns:
        A dict with "matches" (up to 10 dicts with number, title and state) and
        _meta (the live or snapshot source). On failure, an "error" dict.
    """
    try:
        _repo()
        result = RepositoryData(_request).search(query)
        return {"matches": result.value, "_meta": result.metadata()}
    except Exception as exc:
        return {"error": _describe(exc)}


def list_labels() -> dict:
    """List every label that exists in the repository.

    Only labels returned here may be applied. Any other label will be rejected
    by the tool layer.

    Returns:
        A dict with "labels" (name and description dicts) and _meta (the live
        or snapshot source).
    """
    try:
        _repo()
        result = RepositoryData(_request).labels()
        return {"labels": result.value, "_meta": result.metadata()}
    except Exception as exc:
        return {"error": _describe(exc)}


def apply_labels(number: int, labels: list[str]) -> dict:
    """Apply the chosen labels to an issue. This WRITES to GitHub.

    This is a privileged operation. The write is wrapped in
    FunctionTool(require_confirmation=needs_confirmation), which suspends the
    invocation until a structured ToolConfirmation arrives. The model cannot
    approve it in text.

    Args:
        number: The issue number to label.
        labels: The label names to apply. Every name must already exist in the
            repository (see list_labels).

    Returns:
        A dict with keys "applied" (the labels written) and "number".
        On failure, a dict with an "error" key.
    """
    try:
        repo = _repo()
        if not os.environ.get("GITHUB_TOKEN"):
            return {
                "error": "apply_labels requires a GitHub token: set GITHUB_TOKEN "
                         f"to a token with issues:write on {repo}"
            }
        _request(
            "POST", f"/repos/{repo}/issues/{int(number)}/labels",
            body={"labels": list(labels)},
        )
    except Exception as exc:
        return {"error": _describe(exc)}
    return {"number": number, "applied": labels}


def _request(method: str, path: str, body: dict | None = None,
             accept: str = JSON_MEDIA_TYPE):
    """Send one GitHub REST request; the single route every call takes.

    Returns the decoded JSON payload, or the raw response text when `accept`
    names a non-JSON media type. Raises urllib.error.HTTPError on a non-2xx
    status and urllib.error.URLError on a network failure; callers turn those
    into error dicts.
    """
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "adk-issue-triage",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        API_ROOT + path, data=data, headers=headers, method=method
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        text = resp.read().decode("utf-8")
    if accept != JSON_MEDIA_TYPE:
        return text
    return json.loads(text) if text else None
