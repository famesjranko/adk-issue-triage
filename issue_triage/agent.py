"""Triage agent wiring.

The shape of this file is the point. The control flow — fetch, then analyse
three independent things concurrently, then prioritise, then decide readiness —
is fixed and known in advance, so it is expressed as SequentialAgent and
ParallelAgent rather than described to a model and rediscovered on every run.
Only the judgement steps are LlmAgents.
"""

from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App, ResumabilityConfig
from google.adk.tools import FunctionTool

from google.genai import types

from . import prompts
from .callbacks import guard_tool_call, needs_confirmation
from .tools.github import apply_labels, fetch_issue, list_labels, search_issues

FAST = prompts.FAST_MODEL
SMART = prompts.SMART_MODEL

# Nothing in ADK pins temperature, so every run samples differently. The first
# ablation was unreadable because an unchanged prompt moved 30 points between
# runs — measurement noise, not signal. Deterministic decoding does not remove
# variance entirely, but it removes the part that is free to remove.
DETERMINISTIC = types.GenerateContentConfig(temperature=0.0)


def build_pipeline(area_instruction: str = prompts.AREA_INSTRUCTION) -> SequentialAgent:
  """Assemble the triage pipeline.

  Args:
    area_instruction: which area prompt variant to use. Parameterised so the
      eval set can be run against the grounded and ablated prompts.
  """
  intake = LlmAgent(
      name="intake_agent",
      model=SMART,
      instruction=prompts.INTAKE_INSTRUCTION,
      tools=[fetch_issue],
      generate_content_config=DETERMINISTIC, output_key="issue",
  )

  # These three are independent: none reads another's output, so there is no
  # reason to pay for them serially.
  analysis = ParallelAgent(
      name="analysis",
      sub_agents=[
          LlmAgent(name="kind_agent", model=FAST,
                   instruction=prompts.KIND_INSTRUCTION, generate_content_config=DETERMINISTIC, output_key="kind"),
          LlmAgent(name="area_agent", model=FAST,
                   instruction=area_instruction, generate_content_config=DETERMINISTIC, output_key="area"),
          LlmAgent(name="dupe_agent", model=FAST,
                   instruction=prompts.DUPE_INSTRUCTION,
                   tools=[search_issues], generate_content_config=DETERMINISTIC, output_key="duplicates"),
      ],
  )

  priority = LlmAgent(name="priority_agent", model=FAST,
                      instruction=prompts.PRIORITY_INSTRUCTION,
                      generate_content_config=DETERMINISTIC, output_key="priority")
  readiness = LlmAgent(name="readiness_agent", model=SMART,
                       instruction=prompts.READINESS_INSTRUCTION,
                       generate_content_config=DETERMINISTIC, output_key="readiness")
  synthesis = LlmAgent(name="synthesis_agent", model=FAST,
                       instruction=prompts.SYNTHESIS_INSTRUCTION,
                       generate_content_config=DETERMINISTIC, output_key="triage_result")

  return SequentialAgent(
      name="triage_pipeline",
      sub_agents=[intake, analysis, priority, readiness, synthesis],
  )


# The write is wrapped rather than passed bare. require_confirmation takes a
# callable, so the policy sees the actual arguments: the run suspends before the
# function body executes, and only resumes on a ToolConfirmation. Nothing the
# model emits can substitute for that, because the model is not running while
# the invocation is suspended.
apply_labels_tool = FunctionTool(apply_labels, require_confirmation=needs_confirmation)

root_agent = LlmAgent(
    name="triage_coordinator",
    model=SMART,
    instruction=prompts.ROOT_INSTRUCTION,
    sub_agents=[build_pipeline()],
    tools=[list_labels, apply_labels_tool],
    before_tool_callback=guard_tool_call,
)

app = App(
    name="issue_triage",
    root_agent=root_agent,
    # Suspending mid-run is only possible if the app is resumable.
    resumability_config=ResumabilityConfig(is_resumable=True),
    # Seven agents means seven different system instructions, so every hand-off
    # changes the prompt prefix and misses the model's context cache. Within one
    # triage that is unavoidable — each agent runs exactly once, so there is
    # never a prefix to hit. Across runs it is pure waste: the same seven
    # instructions get re-sent every time. A per-agent cache fixes the second
    # case, which is the one that matters during an eval sweep.
    context_cache_config=ContextCacheConfig(ttl_seconds=1800, min_tokens=512),
)
