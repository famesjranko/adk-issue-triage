"""Recognise the free tier's per-day quota in a failed run.

The per-minute 429 is transient: the node's RetryConfig and RateLimitPlugin
absorb it. The per-day 429 is not, and every retry after it fails the same way.
It reaches the caller wrapped by ADK (`_ResourceExhaustedError` chained with
`from`, `DynamicNodeFailError` holding it in `.error`, ExceptionGroup from a
ParallelAgent's TaskGroup), so the walk follows every one of those links.
"""

import re
from collections.abc import Iterator
from typing import Optional

from google.genai.errors import ClientError

_LIMIT = re.compile(r"limit: (\d+)")
_MODEL = re.compile(r"model: ([\w.\-]+)")

# The free tier's per-day request cap. The message text carries only the number;
# the QuotaFailure detail carries the quota id, which names the window.
_FREE_TIER_DAILY_LIMIT = "500"


def _chain(exc: BaseException) -> Iterator[BaseException]:
  """Every exception reachable from exc, each once."""
  seen: set[int] = set()
  stack = [exc]
  while stack:
    e = stack.pop()
    if e is None or id(e) in seen:
      continue
    seen.add(id(e))
    yield e
    stack.extend([e.__cause__, e.__context__])
    wrapped = getattr(e, "error", None)
    if isinstance(wrapped, BaseException):
      stack.append(wrapped)
    if isinstance(e, BaseExceptionGroup):
      stack.extend(e.exceptions)


def _violations(err: ClientError) -> list[dict]:
  details = err.details if isinstance(err.details, dict) else {}
  body = details.get("error", details)
  out = []
  for d in body.get("details") or []:
    if isinstance(d, dict):
      out.extend(v for v in d.get("violations") or [] if isinstance(v, dict))
  return out


def daily_quota_message(exc: BaseException) -> Optional[str]:
  """One line naming the exhausted per-day quota, or None if exc is not that.

  None for a per-minute 429 and for everything else, so those still traceback.
  """
  for e in _chain(exc):
    if not isinstance(e, ClientError) or e.code != 429:
      continue
    text = e.message or ""
    violations = _violations(e)
    quota_ids = [str(v["quotaId"]) for v in violations if v.get("quotaId")]
    per_day = [v for v in violations if "PerDay" in str(v.get("quotaId", ""))]
    limit = _LIMIT.search(text)
    # A quota id names the window outright; the limit number is the fallback
    # for a response that carries no QuotaFailure detail.
    if quota_ids:
      if not per_day:
        continue
    elif not (limit and limit.group(1) == _FREE_TIER_DAILY_LIMIT):
      continue
    model = _MODEL.search(text)
    dims = (per_day[0].get("quotaDimensions") or {}) if per_day else {}
    model_name = model.group(1) if model else dims.get("model", "unknown model")
    limit_value = limit.group(1) if limit else (per_day[0].get("quotaValue") if per_day else "?")
    return (
        f"Gemini free-tier quota exhausted: {limit_value} requests per day for "
        f"{model_name}. Not retryable; the daily cap resets at midnight Pacific. "
        "Wait, or run on Vertex AI."
    )
  return None
