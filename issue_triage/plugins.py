"""Runner-level plugins: cross-cutting concerns that belong in one place.

Registered once on the Runner, these apply to every agent in the tree. Copying
the same logic into six LlmAgents as callbacks would be the wrong shape.
"""

import asyncio
import logging
import time
from collections import defaultdict
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, LlmResponse
from google.adk.plugins import BasePlugin

logger = logging.getLogger(__name__)


class RateLimitPlugin(BasePlugin):
  """Keep the agent tree under the free tier's per-model requests-per-minute cap.

  Two facts make this necessary rather than decorative. The quota is enforced
  per model, not per project, so one shared counter would either throttle the
  fast model to the slow model's ceiling or let the slow one blow its quota. And
  the ParallelAgent deliberately issues three model calls at once, which is
  exactly the burst that trips the limit.

  So: one sliding window per model, all behind a single lock, applied at the
  Runner. The limit is a property of the runtime, not of any one agent.

  Limits measured against the AI Studio free tier on 2026-09-03. Re-check at
  https://ai.google.dev/gemini-api/docs/rate-limits — they change.
  """

  DEFAULT_LIMITS = {
      "gemini-3.1-flash-lite": 15,
      "gemini-3.5-flash": 5,
      "gemini-3.6-flash": 5,
      "gemini-3.7-flash": 5,
      "gemini-3.8-flash": 5,
  }
  FALLBACK_RPM = 5

  # The window is process-local; the quota is per project. Two processes sharing
  # a key each think they are under the limit and together are not — which is
  # exactly how an eval run died mid-way while a triage was run alongside it.
  # Headroom does not fix that (only one process at a time does) but it absorbs
  # clock skew against the server's own window.
  HEADROOM = 0.8

  def __init__(self, limits: Optional[dict[str, int]] = None):
    super().__init__(name="rate_limit")
    self.limits = {**self.DEFAULT_LIMITS, **(limits or {})}
    self._calls: dict[str, list[float]] = defaultdict(list)
    self._lock = asyncio.Lock()
    self.waited_seconds = 0.0

  def _limit_for(self, model: str) -> int:
    for name, rpm in self.limits.items():
      if model.startswith(name):
        return max(1, int(rpm * self.HEADROOM))
    return max(1, int(self.FALLBACK_RPM * self.HEADROOM))

  async def before_model_callback(
      self, *, callback_context: CallbackContext, llm_request: LlmRequest
  ) -> Optional[LlmResponse]:
    model = llm_request.model or "unknown"
    rpm = self._limit_for(model)
    async with self._lock:
      while True:
        now = time.monotonic()
        window = [t for t in self._calls[model] if now - t < 60.0]
        self._calls[model] = window
        if len(window) < rpm:
          window.append(now)
          return None
        wait = 60.0 - (now - window[0]) + 0.05
        self.waited_seconds += wait
        logger.info("rate limit: %s at %d/%d, sleeping %.1fs", model, len(window), rpm, wait)
        await asyncio.sleep(wait)


class CostMeterPlugin(BasePlugin):
  """Accumulate token usage across every model call in a run.

  Agents are easy to make accidentally expensive: a pipeline of six LlmAgents
  costs six model calls per triage, and nothing in the agent code makes that
  visible. This does.
  """

  def __init__(self):
    super().__init__(name="cost_meter")
    self.prompt_tokens = 0
    self.output_tokens = 0
    self.calls = 0
    self.by_agent: dict[str, int] = {}

  async def after_model_callback(
      self, *, callback_context: CallbackContext, llm_response: LlmResponse
  ) -> Optional[LlmResponse]:
    usage = getattr(llm_response, "usage_metadata", None)
    if usage is None:
      return None
    self.calls += 1
    self.prompt_tokens += usage.prompt_token_count or 0
    self.output_tokens += usage.candidates_token_count or 0
    agent = callback_context.agent_name
    self.by_agent[agent] = self.by_agent.get(agent, 0) + (usage.total_token_count or 0)
    return None

  def report(self) -> str:
    from .console import dim

    lines = [
        f"  {'model calls':<16} {self.calls:>8,}",
        f"  {'prompt tokens':<16} {self.prompt_tokens:>8,}",
        f"  {'output tokens':<16} {self.output_tokens:>8,}",
    ]
    if self.by_agent:
      widest = max(self.by_agent.values())
      lines.append("")
      for agent, total in sorted(self.by_agent.items(), key=lambda kv: -kv[1]):
        bar = "▇" * max(1, round(24 * total / widest))
        lines.append(f"  {agent:<20} {total:>7,}  {dim(bar)}")
    return "\n".join(lines)
