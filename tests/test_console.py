"""compact_library_tracebacks turns ADK's logged exceptions into one line."""

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.adk.workflow._errors import DynamicNodeFailError  # noqa: E402

from issue_triage import console  # noqa: E402

NODE_RUNNER = "google_adk.google.adk.workflow._node_runner"


class _Wrapped(Exception):
  pass


class _Outer(Exception):
  pass


class _Innermost(Exception):
  pass


@pytest.fixture
def compacted(caplog):
  original = logging.getLogRecordFactory()
  console.compact_library_tracebacks()
  caplog.set_level(logging.WARNING, logger="google_adk")
  yield caplog
  logging.setLogRecordFactory(original)


def _raise_chained():
  try:
    try:
      raise _Innermost("429 RESOURCE_EXHAUSTED quota\nsecond line of detail")
    except _Innermost as inner:
      raise _Wrapped("wrapped once") from inner
  except _Wrapped as mid:
    raise _Outer("wrapped twice") from mid


def test_adk_internal_errors_are_silent_by_default(caplog):
  logger = logging.getLogger("google_adk")
  original_level = logger.level
  original_factory = logging.getLogRecordFactory()
  caplog.set_level(logging.DEBUG)
  try:
    console.compact_library_tracebacks()
    logging.getLogger(NODE_RUNNER).error("duplicate internal failure")
    assert not caplog.records
  finally:
    logger.setLevel(original_level)
    logging.setLogRecordFactory(original_factory)


def test_exception_record_is_one_line_naming_the_root_cause(compacted):
  try:
    _raise_chained()
  except _Outer:
    logging.getLogger(NODE_RUNNER).exception("Node execution failed with exception")

  assert len(compacted.records) == 1
  record = compacted.records[0]
  assert record.exc_info is None
  assert record.exc_text is None
  message = record.getMessage()
  assert message.startswith("Node execution failed with exception: ")
  assert "_Innermost: 429 RESOURCE_EXHAUSTED quota" in message
  assert "second line" not in message
  assert "_Outer" not in message
  assert "Traceback" not in compacted.text


def test_dynamic_node_failure_names_the_error_it_holds(compacted):
  # A workflow node holds its failure in `.error`, not chained with `from`.
  try:
    raise DynamicNodeFailError(
        message="node failed", error=_Innermost("429 RESOURCE_EXHAUSTED"),
        error_node_path="triage/area")
  except DynamicNodeFailError:
    logging.getLogger("google_adk.google.adk.runners").error(
        "Root node %s failed.", "triage_workflow", exc_info=True)

  assert len(compacted.records) == 1
  assert compacted.records[0].getMessage() == (
      "Root node triage_workflow failed: _Innermost: 429 RESOURCE_EXHAUSTED")
  assert compacted.records[0].exc_info is None


def test_record_without_exc_info_is_untouched(compacted):
  logging.getLogger(NODE_RUNNER).warning("retrying %s", "node")

  assert len(compacted.records) == 1
  record = compacted.records[0]
  assert record.msg == "retrying %s"
  assert record.args == ("node",)
  assert record.getMessage() == "retrying node"
