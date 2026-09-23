"""The graph is built offline: inspected, or run against a scripted model.

Retry lives on the node, not the graph, so a RetryConfig that is defined but not
attached to a node compiles, imports and does nothing. This pins it to every
node that calls the model, and checks that the pinned ADK really re-runs an
LlmAgent node that raises.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.adk.workflow import START, Workflow
from google.genai import types

from issue_triage.workflow_agent import MODEL_NODE, RETRY, build_workflow

MODEL_NODES = {
    "intake_agent", "kind_agent", "area_agent", "dupe_agent",
    "priority_agent", "readiness_agent", "synthesis_agent",
}


def model_nodes() -> dict[str, LlmAgent]:
  graph = build_workflow().graph
  return {n.name: n for n in graph.nodes if isinstance(n, LlmAgent)}


def test_every_model_node_is_in_the_graph():
  assert set(model_nodes()) == MODEL_NODES


def test_every_model_node_retries():
  missing = {name for name, n in model_nodes().items() if n.retry_config != RETRY}
  assert not missing, f"no retry on {sorted(missing)}"


class FailsOnce(BaseLlm):
  """Raises on the first call, as a free-tier 503 does, then answers."""

  calls: int = 0

  async def generate_content_async(self, llm_request, stream=False):
    self.calls += 1
    if self.calls == 1:
      raise RuntimeError("503 UNAVAILABLE")
    yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="ok")]))


async def run_one_node(model: BaseLlm) -> dict:
  node = LlmAgent(name="node", model=model, instruction="answer", output_key="out", **MODEL_NODE)
  runner = InMemoryRunner(agent=Workflow(name="graph", edges=[(START, node)]), app_name="t")
  session = await runner.session_service.create_session(app_name="t", user_id="u")
  message = types.Content(role="user", parts=[types.Part(text="go")])
  async for _ in runner.run_async(user_id="u", session_id=session.id, new_message=message):
    pass
  final = await runner.session_service.get_session(app_name="t", user_id="u", session_id=session.id)
  return dict(final.state)


def test_a_failed_model_call_is_retried():
  model = FailsOnce(model="fails-once")
  state = asyncio.run(run_one_node(model))
  assert model.calls == 2
  assert state.get("out") == "ok"
