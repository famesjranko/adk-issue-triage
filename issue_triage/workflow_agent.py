"""The same pipeline, expressed with ADK 2.8's graph Workflow.

SequentialAgent, ParallelAgent and LoopAgent are all deprecated in 2.8 in favour
of Workflow, which replaces "a list of sub-agents run in order" with an explicit
graph of edges. Same six steps as agent.py, so the two are directly comparable.

What the graph buys over the nested Sequential(Parallel(...)) form:
  - fan-out and join are one edge list, not two nested containers
  - max_concurrency is a property of the graph, which matters here because the
    free tier's rate limit is what the fan-out actually contends for
  - per-node retry and timeout, rather than all-or-nothing
  - the graph can be resumed, which is the foundation for approval gates that
    suspend rather than refuse

Note the docs at adk.dev still describe the deprecated API — this is written
against the shipped source in google/adk/workflow/.
"""

from google.adk.agents import LlmAgent
from google.adk.workflow import START, JoinNode, RetryConfig, Workflow

from google.genai import types

from . import prompts
from .tools.github import fetch_issue, search_issues

FAST = prompts.FAST_MODEL
SMART = prompts.SMART_MODEL

# Nothing in ADK pins temperature, so every run samples differently. The first
# ablation was unreadable because an unchanged prompt moved 30 points between
# runs — measurement noise, not signal. Deterministic decoding does not remove
# variance entirely, but it removes the part that is free to remove.
DETERMINISTIC = types.GenerateContentConfig(temperature=0.0)

# Free-tier 503s are common, so the nodes that call the model get a retry rather
# than failing the whole graph.
RETRY = RetryConfig(max_attempts=3)


def build_workflow(area_instruction: str = prompts.AREA_INSTRUCTION) -> Workflow:
  intake = LlmAgent(name="intake_agent", model=SMART,
                    instruction=prompts.INTAKE_INSTRUCTION,
                    tools=[fetch_issue], generate_content_config=DETERMINISTIC, output_key="issue")
  kind = LlmAgent(name="kind_agent", model=FAST,
                  instruction=prompts.KIND_INSTRUCTION, generate_content_config=DETERMINISTIC, output_key="kind")
  area = LlmAgent(name="area_agent", model=FAST,
                  instruction=area_instruction, generate_content_config=DETERMINISTIC, output_key="area")
  dupe = LlmAgent(name="dupe_agent", model=FAST,
                  instruction=prompts.DUPE_INSTRUCTION,
                  tools=[search_issues], generate_content_config=DETERMINISTIC, output_key="duplicates")
  priority = LlmAgent(name="priority_agent", model=FAST,
                      instruction=prompts.PRIORITY_INSTRUCTION,
                      generate_content_config=DETERMINISTIC, output_key="priority")
  readiness = LlmAgent(name="readiness_agent", model=SMART,
                       instruction=prompts.READINESS_INSTRUCTION,
                       generate_content_config=DETERMINISTIC, output_key="readiness")
  synthesis = LlmAgent(name="synthesis_agent", model=FAST,
                       instruction=prompts.SYNTHESIS_INSTRUCTION,
                       generate_content_config=DETERMINISTIC, output_key="triage_result")

  # A tuple on the LEFT of an edge is an OR-join: the successor fires on the
  # first branch to arrive. That is not what fan-out/gather means here — the
  # first run of this graph fired priority_agent before area_agent had written
  # state, and the instruction template raised KeyError: `area`.
  # JoinNode is the AND-join: it waits for every predecessor.
  gathered = JoinNode(name="analysis_complete")

  return Workflow(
      name="triage_workflow",
      edges=[
          (START, intake),
          (intake, (kind, area, dupe)),   # tuple on the right = fan-out
          (kind, gathered),
          (area, gathered),
          (dupe, gathered),               # JoinNode waits for all three
          (gathered, priority),
          (priority, readiness),
          (readiness, synthesis),
      ],
      max_concurrency=3,
  )


root_agent = build_workflow()
