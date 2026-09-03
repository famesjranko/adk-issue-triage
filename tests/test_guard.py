"""The guards are deterministic, so they are tested deterministically.

Trying to prove a guard by prompting the model into misbehaving proves nothing:
a well-behaved model simply declines and the guard never runs. In one attempt
the model called list_labels first, noticed the label did not exist, and refused
on its own — good behaviour, zero coverage. These call the guards directly.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from issue_triage.callbacks import MAX_LABELS, guard_tool_call, needs_confirmation

APPLY = SimpleNamespace(name="apply_labels")
READ = SimpleNamespace(name="fetch_issue")
CTX = SimpleNamespace(state={})


# --- validation: labels must exist in the repository -----------------------

def test_reads_are_never_gated():
  assert guard_tool_call(READ, {"number": 231}, CTX) is None


def test_unknown_labels_are_refused():
  out = guard_tool_call(APPLY, {"number": 231, "labels": ["area/core", "area/frontend"]}, CTX)
  assert out is not None
  # Names the offending label and only the offending label.
  assert "area/frontend" in out["error"] and "area/core" not in out["error"]


def test_valid_labels_pass_validation():
  assert guard_tool_call(APPLY, {"number": 231, "labels": ["area/core", "priority/p2"]}, CTX) is None


def test_label_count_is_capped():
  out = guard_tool_call(APPLY, {"number": 231, "labels": ["bug"] * (MAX_LABELS + 1)}, CTX)
  assert out is not None and "exceeds the limit" in out["error"]


# --- authorisation policy: which writes must stop and ask ------------------

def test_area_only_writes_do_not_interrupt_a_human():
  """Filing an issue under a module is reversible and low-stakes."""
  assert needs_confirmation(231, ["area/core"]) is False
  assert needs_confirmation(231, ["area/core", "area/android"]) is False


def test_priority_claims_require_confirmation():
  """Asserting p0 is a claim about someone's week."""
  assert needs_confirmation(231, ["priority/p0"]) is True
  assert needs_confirmation(231, ["area/core", "priority/p2"]) is True


def test_readiness_requires_confirmation():
  assert needs_confirmation(231, ["ready-for-agent"]) is True


def test_one_risky_label_taints_the_whole_write():
  """The call is atomic, so the policy is over the set, not per label."""
  assert needs_confirmation(231, ["area/docs", "area/ci", "priority/p3"]) is True
