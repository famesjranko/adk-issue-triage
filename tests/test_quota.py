"""daily_quota_message() tells a per-day 429 apart from everything else.

The chains are built with ADK's own wrappers the way ADK raises them:
Gemini raises `_ResourceExhaustedError(ce) from ce`, and a workflow node
raises `DynamicNodeFailError(error=...)` with the failure in `.error`, not
chained. No network, no model call.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.adk.models.google_llm import _ResourceExhaustedError
from google.adk.workflow._errors import DynamicNodeFailError
from google.genai.errors import ClientError, ServerError

from issue_triage.quota import daily_quota_message  # noqa: E402

MODEL = "gemini-3.1-flash-lite"


def quota_429(limit: int, quota_id: str | None) -> ClientError:
  """A 429 in the shape the AI Studio free tier returns."""
  details = [{
      "@type": "type.googleapis.com/google.rpc.RetryInfo",
      "retryDelay": "35s",
  }]
  if quota_id:
    details.insert(0, {
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [{
            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
            "quotaId": quota_id,
            "quotaDimensions": {"location": "global", "model": MODEL},
            "quotaValue": str(limit),
        }],
    })
  return ClientError(429, {"error": {
      "code": 429,
      "status": "RESOURCE_EXHAUSTED",
      "message": (
          "You exceeded your current quota, please check your plan and billing "
          "details. For more information on this error, head to: "
          "https://ai.google.dev/gemini-api/docs/rate-limits.\n"
          "* Quota exceeded for metric: generativelanguage.googleapis.com/"
          f"generate_content_free_tier_requests, limit: {limit}, model: {MODEL}\n"
          "Please retry in 35.3s."
      ),
      "details": details,
  }})


def wrapped(err: Exception) -> Exception:
  """Wrap err two levels deep, as ADK does between the model call and main()."""
  try:
    try:
      raise _ResourceExhaustedError(err) from err
    except _ResourceExhaustedError as resource:
      raise DynamicNodeFailError(
          message="Dynamic node area_agent failed",
          error=resource,
          error_node_path="triage/area_agent",
      )
  except DynamicNodeFailError as outer:
    return outer


def detached(err: Exception) -> DynamicNodeFailError:
  """DynamicNodeFailError raised outside any except block: only .error links it."""
  return DynamicNodeFailError(message="failed", error=err, error_node_path="x")


def test_daily_cap_names_model_limit_window_and_reset():
  msg = daily_quota_message(wrapped(quota_429(500, "GenerateRequestsPerDayPerProjectPerModel-FreeTier")))
  assert msg is not None
  assert MODEL in msg
  assert "500" in msg
  assert "per day" in msg
  assert "midnight Pacific" in msg


def test_daily_cap_found_through_dynamic_node_error_attribute():
  msg = daily_quota_message(detached(quota_429(500, "GenerateRequestsPerDayPerProjectPerModel-FreeTier")))
  assert msg is not None and "per day" in msg


def test_daily_cap_found_through_cause_and_context():
  try:
    try:
      raise RuntimeError("explicit") from quota_429(500, None)
    except RuntimeError:
      raise ValueError("implicit")
  except ValueError as outer:
    assert daily_quota_message(outer) is not None


def test_daily_cap_from_message_text_alone():
  msg = daily_quota_message(wrapped(quota_429(500, None)))
  assert msg is not None and MODEL in msg and "per day" in msg


def test_daily_cap_inside_exception_group():
  group = ExceptionGroup("parallel", [ValueError("x"), wrapped(quota_429(500, None))])
  assert daily_quota_message(group) is not None


def test_per_minute_429_is_not_the_daily_cap():
  per_minute = quota_429(15, "GenerateRequestsPerMinutePerProjectPerModel-FreeTier")
  assert daily_quota_message(wrapped(per_minute)) is None
  assert daily_quota_message(wrapped(quota_429(15, None))) is None


def test_quota_id_outranks_the_limit_number():
  # A per-minute quota whose limit happens to be 500 is still per minute.
  assert daily_quota_message(wrapped(quota_429(500, "GenerateRequestsPerMinutePerProjectPerModel"))) is None


def test_non_429_is_not_the_daily_cap():
  assert daily_quota_message(wrapped(ServerError(503, {"error": {"message": "limit: 500"}}))) is None
  assert daily_quota_message(wrapped(ClientError(400, {"error": {"message": "limit: 500"}}))) is None
  assert daily_quota_message(RuntimeError("limit: 500")) is None
