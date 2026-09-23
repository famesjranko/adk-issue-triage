"""summarise() is the arithmetic behind the spread table, so it is tested alone.

Importing scripts/score.py pulls in ADK but builds no runner, so this needs no
API key and makes no model call.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from score import FIELDS, summarise


def run(area: tuple[int, int], **others: tuple[int, int]) -> dict[str, tuple[int, int]]:
  """One run's graded counts; fields not named are 10/10."""
  return {f: others.get(f, (10, 10)) for f in FIELDS} | {"area": area}


def test_mean_min_max_and_n_per_field():
  summary = summarise([run((6, 10)), run((5, 10)), run((8, 10))])
  area = summary["area"]
  assert area["mean"] == pytest.approx(0.6333, abs=1e-4)
  assert area["min"] == pytest.approx(0.5)
  assert area["max"] == pytest.approx(0.8)
  assert area["n"] == 10


def test_overall_pools_every_field_per_run():
  summary = summarise([run((6, 10)), run((5, 10)), run((8, 10))])
  overall = summary["OVERALL"]
  # Per run: (6 + 30) / 40, (5 + 30) / 40, (8 + 30) / 40.
  assert overall["min"] == pytest.approx(35 / 40)
  assert overall["max"] == pytest.approx(38 / 40)
  assert overall["mean"] == pytest.approx((36 + 35 + 38) / 120)
  assert overall["n"] == 40


def test_a_single_run_has_no_spread():
  area = summarise([run((7, 10))])["area"]
  assert area["mean"] == area["min"] == area["max"] == pytest.approx(0.7)


def test_a_field_without_ground_truth_has_no_accuracy():
  summary = summarise([run((5, 10), priority=(0, 0)), run((6, 10), priority=(0, 0))])
  assert summary["priority"] == {"mean": None, "min": None, "max": None, "n": 0}
  # kind, area and readiness: the ungradeable field adds nothing to the pool.
  assert summary["OVERALL"]["n"] == 30


def test_gradeable_count_must_not_change_between_runs():
  with pytest.raises(ValueError, match="area"):
    summarise([run((5, 10)), run((5, 9))])


def test_no_runs_is_an_error():
  with pytest.raises(ValueError):
    summarise([])
