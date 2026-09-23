"""The GitHub tool layer, tested offline against canned REST payloads.

Every public tool goes through tools.github._request, so replacing that one
function covers the whole surface. The transport test below replaces urlopen
instead, to pin the headers _request itself sends.
"""

import io
import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from issue_triage.tools import github, repo

ISSUE = {"number": 231, "title": "Room schema drift", "body": "x" * 9000,
         "state": "open", "labels": [{"name": "bug"}]}
COMMENTS = [{"body": "first"}, {"body": None}]
SEARCH = {"total_count": 2, "items": [
    {"number": 231, "title": "Room schema drift", "state": "open"},
    {"number": 12, "title": "Room migration", "state": "closed"},
]}
LABELS = [{"name": "bug", "description": "Something is broken"},
          {"name": "area/core", "description": None}]


class FakeGitHub:
  """Stands in for _request: canned payloads by path, and a log of calls."""

  def __init__(self, routes):
    self.routes = routes
    self.calls = []

  def __call__(self, method, path, body=None, accept=github.JSON_MEDIA_TYPE):
    self.calls.append((method, path, body, accept))
    for prefix, payload in self.routes.items():
      if path.startswith(prefix):
        if isinstance(payload, Exception):
          raise payload
        return payload
    raise AssertionError(f"unexpected request {method} {path}")


@pytest.fixture
def fake(monkeypatch):
  def install(routes):
    f = FakeGitHub(routes)
    monkeypatch.setattr(github, "_request", f)
    return f
  return install


def http_error(code, message):
  return urllib.error.HTTPError(
      "https://api.github.com/x", code, "reason", {},
      io.BytesIO(json.dumps({"message": message}).encode()))


def test_fetch_issue_shape_and_no_label_leak(fake):
  f = fake({"/repos/famesjranko/musicmeta/issues/231/comments": COMMENTS,
            "/repos/famesjranko/musicmeta/issues/231": ISSUE})
  out = github.fetch_issue(231)
  assert set(out) == {"number", "title", "body", "state", "comments"}
  assert out["number"] == 231 and out["title"] == "Room schema drift"
  assert len(out["body"]) == 8000
  assert out["state"] == "OPEN"
  assert out["comments"] == ["first", ""]
  assert [c[:2] for c in f.calls] == [
      ("GET", "/repos/famesjranko/musicmeta/issues/231"),
      ("GET", "/repos/famesjranko/musicmeta/issues/231/comments?per_page=10"),
  ]


def test_search_issues_shape_and_scoped_query(fake):
  f = fake({"/search/issues": SEARCH})
  out = github.search_issues("room schema")
  assert out == {"matches": [
      {"number": 231, "title": "Room schema drift", "state": "OPEN"},
      {"number": 12, "title": "Room migration", "state": "CLOSED"},
  ]}
  (method, path, _, _), = f.calls
  assert method == "GET"
  assert "q=room%20schema%20repo%3Afamesjranko/musicmeta%20is%3Aissue" in path


def test_list_labels_shape(fake):
  fake({"/repos/famesjranko/musicmeta/labels": LABELS})
  assert github.list_labels() == {"labels": [
      {"name": "bug", "description": "Something is broken"},
      {"name": "area/core", "description": ""},
  ]}


def test_apply_labels_posts_labels(fake, monkeypatch):
  monkeypatch.setenv("GITHUB_TOKEN", "test-token")
  f = fake({"/repos/famesjranko/musicmeta/issues/231/labels": [{"name": "bug"}]})
  out = github.apply_labels(231, ["bug"])
  assert out == {"number": 231, "applied": ["bug"]}
  assert f.calls == [("POST", "/repos/famesjranko/musicmeta/issues/231/labels",
                      {"labels": ["bug"]}, github.JSON_MEDIA_TYPE)]


def test_apply_labels_refuses_repo_outside_allowlist(fake, monkeypatch):
  monkeypatch.setenv("GITHUB_TOKEN", "test-token")
  monkeypatch.setattr(github, "REPO", "someone/private-repo")
  f = fake({})
  out = github.apply_labels(1, ["bug"])
  assert "allowlist" in out["error"]
  assert f.calls == []


def test_apply_labels_without_token_never_requests(fake, monkeypatch):
  monkeypatch.delenv("GITHUB_TOKEN", raising=False)
  f = fake({})
  out = github.apply_labels(231, ["bug"])
  assert "GITHUB_TOKEN" in out["error"] and "issues:write" in out["error"]
  assert f.calls == []


@pytest.mark.parametrize("call", [
    lambda: github.fetch_issue(999),
    lambda: github.search_issues("room"),
    lambda: github.list_labels(),
])
def test_http_error_becomes_error_dict(fake, call):
  fake({"/": http_error(404, "Not Found")})
  out = call()
  assert set(out) == {"error"}
  assert "404" in out["error"] and "Not Found" in out["error"]


def test_network_error_becomes_error_dict(fake, monkeypatch):
  monkeypatch.setenv("GITHUB_TOKEN", "test-token")
  fake({"/": urllib.error.URLError("no route to host")})
  out = github.apply_labels(231, ["bug"])
  assert set(out) == {"error"} and "no route to host" in out["error"]


class FakeResponse(io.BytesIO):
  def __enter__(self):
    return self

  def __exit__(self, *exc):
    self.close()


def test_request_sends_versioned_headers_and_optional_auth(monkeypatch):
  sent = []

  def urlopen(req, timeout):
    sent.append((req, timeout))
    return FakeResponse(b'{"ok": true}')

  monkeypatch.setattr(github.urllib.request, "urlopen", urlopen)
  monkeypatch.delenv("GITHUB_TOKEN", raising=False)
  assert github._request("GET", "/repos/a/b") == {"ok": True}
  monkeypatch.setenv("GITHUB_TOKEN", "test-token")
  github._request("POST", "/repos/a/b/issues/1/labels", body={"labels": ["x"]})

  (anon, timeout), (authed, _) = sent
  assert timeout == 30
  assert anon.full_url == "https://api.github.com/repos/a/b"
  assert anon.get_header("Accept") == "application/vnd.github+json"
  assert anon.get_header("X-github-api-version") == "2022-11-28"
  assert anon.get_header("Authorization") is None
  assert authed.get_method() == "POST"
  assert authed.get_header("Authorization") == "Bearer test-token"
  assert json.loads(authed.data) == {"labels": ["x"]}


def test_module_map_rest_fallback(fake, monkeypatch):
  monkeypatch.delenv("MUSICMETA_PATH", raising=False)
  doc = "# Arch\n\n## Module map\n\n```\ncore/\nandroid/\n```\n"
  f = fake({"/repos/famesjranko/musicmeta/contents/ARCHITECTURE.md": doc})
  repo.read_module_map.cache_clear()
  try:
    assert repo.read_module_map() == "core/\nandroid/"
  finally:
    repo.read_module_map.cache_clear()
  assert f.calls == [("GET", "/repos/famesjranko/musicmeta/contents/ARCHITECTURE.md",
                      None, "application/vnd.github.raw")]


def test_module_map_rest_failure_never_raises(fake, monkeypatch):
  monkeypatch.delenv("MUSICMETA_PATH", raising=False)
  fake({"/": http_error(403, "API rate limit exceeded")})
  repo.read_module_map.cache_clear()
  try:
    out = repo.read_module_map()
  finally:
    repo.read_module_map.cache_clear()
  assert out.startswith("(could not read famesjranko/musicmeta ARCHITECTURE.md:")
  assert "rate limit" in out


def test_module_map_prefers_local_checkout(fake, monkeypatch, tmp_path):
  (tmp_path / "ARCHITECTURE.md").write_text("## Module map\n```\nlocal/\n```\n")
  monkeypatch.setenv("MUSICMETA_PATH", str(tmp_path))
  f = fake({})
  repo.read_module_map.cache_clear()
  try:
    assert repo.read_module_map() == "local/"
  finally:
    repo.read_module_map.cache_clear()
  assert f.calls == []
