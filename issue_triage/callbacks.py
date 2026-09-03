"""Deterministic guards that sit between the model and privileged actions.

The model proposes; this code decides. Two separate concerns, deliberately kept
apart because they fail differently:

1. **Validation** — labels must exist in the repository. Absolute, and cheap:
   it reads the real label list and never consults the model, so there is
   nothing to talk around. That lives here, in `guard_tool_call`.

2. **Authorisation** — a write needs a human to approve it. This does NOT live
   here. ADK ships the primitive: `FunctionTool(fn, require_confirmation=...)`
   suspends the invocation and emits a confirmation request, and the run
   resumes only when a `ToolConfirmation` comes back. See
   `needs_confirmation()` below and its use in `agent.py`.

The first version of this file did authorisation with a flag in session state.
That was the wrong shape and worth recording: session state is inside the trust
boundary, so it demonstrates an approval gate rather than being one. The
built-in suspends the run instead, which means the approving fact never has to
live somewhere the agent can write.
"""

import logging
from typing import Any, Optional

from google.adk.tools import BaseTool, ToolContext

from .tools.github import list_labels

logger = logging.getLogger(__name__)

MAX_LABELS = 6

# Labels that only ever narrow triage are not worth interrupting a human for.
# Anything that asserts urgency, or closes a question, is.
LOW_RISK_PREFIXES = ("area/",)

_label_cache: Optional[set[str]] = None


def known_labels() -> set[str]:
  """The repository's real label vocabulary, fetched once per process."""
  global _label_cache
  if _label_cache is None:
    result = list_labels()
    if "error" in result:
      raise RuntimeError(f"cannot load label vocabulary: {result['error']}")
    _label_cache = {l["name"] for l in result["labels"]}
  return _label_cache


def guard_tool_call(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> Optional[dict[str, Any]]:
  """Gate privileged tool calls. Returning a dict skips the tool entirely."""
  if tool.name != "apply_labels":
    return None

  number = args.get("number")
  labels = args.get("labels") or []

  if len(labels) > MAX_LABELS:
    return {"error": f"refused: {len(labels)} labels exceeds the limit of {MAX_LABELS}"}

  unknown = sorted(set(labels) - known_labels())
  if unknown:
    logger.warning("blocked apply_labels: unknown labels %s", unknown)
    return {
        "error": f"refused: these labels do not exist in the repository: {unknown}. "
                 f"Call list_labels and choose from the real vocabulary."
    }

  # Authorisation is NOT checked here. FunctionTool's require_confirmation
  # suspends the run before the function body executes; a check in this callback
  # would only be a second, weaker copy of it.
  return None


def needs_confirmation(number: int, labels: list[str]) -> bool:
  """Decide whether this specific write has to stop and ask a human.

  Passed to FunctionTool(require_confirmation=...), which accepts a callable and
  invokes it with the tool's own arguments. So the policy is a function of what
  is actually being written, not a blanket setting: filing an issue under
  area/core is reversible and low-stakes, asserting priority/p0 is a claim about
  someone's week.
  """
  del number  # policy depends on what is written, not which issue
  return any(not l.startswith(LOW_RISK_PREFIXES) for l in labels)
