"""Run one triage end to end with the Runner wired up explicitly.

`adk run` and `adk web` hide the Runner. This script exists so the runtime is
visible: agent tree + session service + plugins, and an event loop you can read.

    uv run python scripts/triage.py 231
    uv run python scripts/triage.py 231 --workflow
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai import types

load_dotenv(Path(__file__).resolve().parent.parent / "issue_triage" / ".env", override=True)

from issue_triage import console  # noqa: E402

console.silence_library_noise()

from issue_triage.agent import root_agent  # noqa: E402
from issue_triage.workflow_agent import build_workflow  # noqa: E402
from issue_triage.plugins import CostMeterPlugin, RateLimitPlugin  # noqa: E402

APP = "issue_triage"
USER = "local"


async def main(number: int, prompt: str | None, workflow: bool) -> None:
  meter = CostMeterPlugin()
  # The graph Workflow cannot yet sit under an LlmAgent router, so it runs as
  # the root directly — one of the real constraints of the 2.8 migration.
  agent = build_workflow() if workflow else root_agent
  runner = InMemoryRunner(
      agent=agent, app_name=APP, plugins=[RateLimitPlugin(), meter]
  )

  session = await runner.session_service.create_session(app_name=APP, user_id=USER)

  text = prompt or f"Triage issue {number}."
  message = types.Content(role="user", parts=[types.Part(text=text)])

  console.heading(f"RUN  {'graph Workflow' if workflow else 'SequentialAgent + ParallelAgent'}")
  console.event_line("", console.dim("you"), " ", console.wrap(text, console.GUTTER + 1))

  t0 = None
  suspended = False
  async for event in runner.run_async(
      user_id=USER, session_id=session.id, new_message=message
  ):
    if t0 is None:
      t0 = event.timestamp
    stamp = f"{event.timestamp - t0:.1f}s"
    who = event.author
    delta = (event.actions.state_delta if event.actions else None) or {}
    key = next((k for k in delta if not k.startswith("_")), "")

    for part in (event.content.parts if event.content else []):
      if part.function_call:
        fc = part.function_call
        console.event_line(stamp, who, " ",
                           console.amber("→ ") + console.bold(fc.name)
                           + console.dim("(") + console.args_str(dict(fc.args or {}))
                           + console.dim(")"))
      elif part.function_response:
        fr = part.function_response
        console.event_line(stamp, who, " ",
                           console.green("← ") + fr.name + "  "
                           + console.summarise(fr.response))
      elif part.text and part.text.strip():
        console.event_line(stamp, who, " ",
                           console.wrap(part.text, console.GUTTER + 3, limit=280),
                           state_key=key)
        key = ""
    if key:
      console.event_line(stamp, who, " ", console.dim("(state only)"), state_key=key)

    if event.actions and getattr(event.actions, "requested_tool_confirmations", None):
      suspended = True
      for call_id, conf in event.actions.requested_tool_confirmations.items():
        print()
        print("  " + console.red(console.bold("■ RUN SUSPENDED — human confirmation required")))
        print("  " + console.dim(console.wrap(conf.hint, 4)))
        print("  " + console.dim(f"call_id {call_id}"))

  final = await runner.session_service.get_session(
      app_name=APP, user_id=USER, session_id=session.id
  )

  console.heading("SESSION STATE" + ("  (incomplete — run is suspended)" if suspended else ""))
  for key in ("kind", "area", "priority", "readiness", "duplicates", "triage_result"):
    if key in final.state:
      console.kv(key, console.summarise(final.state[key], limit=72))
  if not any(k in final.state for k in ("kind", "triage_result")):
    print("  " + console.dim("nothing written — the pipeline did not run"))

  console.heading("COST")
  print(meter.report())


if __name__ == "__main__":
  ap = argparse.ArgumentParser()
  ap.add_argument("number", type=int)
  ap.add_argument("--prompt", help="override the user message")
  ap.add_argument("--workflow", action="store_true",
                  help="use the graph Workflow instead of SequentialAgent")
  args = ap.parse_args()
  asyncio.run(main(args.number, args.prompt, args.workflow))
