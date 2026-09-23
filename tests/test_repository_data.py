"""The committed snapshot is a complete, deterministic read fallback."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from issue_triage.repository_data import RepositoryData, load_snapshot  # noqa: E402


def _no_network(*args, **kwargs):
  raise AssertionError("snapshot mode attempted a network request")


def test_snapshot_has_the_demo_issue_without_leaking_labels():
  result = RepositoryData(_no_network, mode="snapshot").issue(231)

  assert result.source == "snapshot"
  assert result.captured_at
  assert result.value["number"] == 231
  assert result.value["state"] in {"OPEN", "CLOSED"}
  assert "labels" not in result.value


def test_snapshot_covers_every_committed_eval_case():
  snapshot_numbers = {item["number"] for item in load_snapshot()["issues"]}
  evalset = json.loads(
      (Path(__file__).parent.parent / "eval" / "triage.evalset.json").read_text())
  eval_numbers = {
      case["conversation"][0]["intermediate_data"]["tool_uses"][0]["args"]["number"]
      for case in evalset["eval_cases"]
  }

  assert eval_numbers <= snapshot_numbers


def test_snapshot_contains_labels_and_module_grounding():
  reader = RepositoryData(_no_network, mode="snapshot")

  assert any(label["name"] == "area/android" for label in reader.labels().value)
  assert "musicmeta-android" in reader.module_map().value


def test_unknown_source_mode_is_rejected():
  with pytest.raises(ValueError, match="TRIAGE_DATA_SOURCE"):
    RepositoryData(_no_network, mode="sometimes")


def test_empty_live_repository_falls_back_to_snapshot():
  result = RepositoryData(lambda *args, **kwargs: [], mode="auto").labeled_issues()

  assert result.source == "snapshot"
  assert len(result.value) == 42


def test_programming_errors_are_not_hidden_by_fallback():
  def broken_response(*args, **kwargs):
    return {"unexpected": "shape"}

  with pytest.raises(KeyError):
    RepositoryData(broken_response, mode="auto").issue(231)
