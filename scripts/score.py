"""Grade the agent against the repository's human-applied labels.

`adk eval` grades the final response as text. That is the wrong unit here: the
agent emits four independent fields and they fail independently. An area/* miss
and a priority/* miss are different bugs with different fixes, and a single
pass/fail per case hides which one moved. So this scores per field.

    uv run python scripts/score.py --limit 12
    uv run python scripts/score.py --limit 12 --ablate-area
    uv run python scripts/score.py --limit 12 --topology sequential
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / "issue_triage" / ".env", override=True)

from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from issue_triage import prompts  # noqa: E402
from issue_triage.plugins import CostMeterPlugin, RateLimitPlugin  # noqa: E402

FIELDS = ("kind", "area", "priority", "readiness")


def load_cases(limit: int | None) -> list[dict]:
  data = json.loads((ROOT / "eval" / "triage.evalset.json").read_text())
  cases = []
  for case in data["eval_cases"]:
    inv = case["conversation"][0]
    cases.append({
        "id": case["eval_id"],
        "prompt": inv["user_content"]["parts"][0]["text"],
        "expected": json.loads(inv["final_response"]["parts"][0]["text"]),
    })
  return cases[:limit] if limit else cases


def parse_result(text: str) -> dict:
  """Pull the JSON object out of a model response that may be fenced."""
  text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
  try:
    return json.loads(text.strip())
  except json.JSONDecodeError:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
      return {}
    try:
      return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
      return {}


def build_agent(topology: str, area_instruction: str):
  if topology == "workflow":
    from issue_triage.workflow_agent import build_workflow
    return build_workflow(area_instruction)
  from issue_triage.agent import build_pipeline
  return build_pipeline(area_instruction)


async def run_case(runner, case: dict) -> dict:
  session = await runner.session_service.create_session(
      app_name="issue_triage", user_id="eval"
  )
  message = types.Content(role="user", parts=[types.Part(text=case["prompt"])])
  async for _ in runner.run_async(
      user_id="eval", session_id=session.id, new_message=message
  ):
    pass
  final = await runner.session_service.get_session(
      app_name="issue_triage", user_id="eval", session_id=session.id
  )
  return parse_result(str(final.state.get("triage_result", "")))


async def main(args) -> None:
  area_instruction = (
      prompts.AREA_INSTRUCTION_ABLATED if args.ablate_area else prompts.AREA_INSTRUCTION
  )
  meter = CostMeterPlugin()
  runner = InMemoryRunner(
      agent=build_agent(args.topology, area_instruction),
      app_name="issue_triage",
      plugins=[RateLimitPlugin(), meter],
  )

  cases = load_cases(args.limit)
  label = f"{args.topology}{'/ablated' if args.ablate_area else '/grounded'}"
  print(f"scoring {len(cases)} cases  [{label}]\n")

  # graded[field] = (correct, gradeable) — a field with no human label is not
  # counted either way, rather than counted as a miss.
  graded = {f: [0, 0] for f in FIELDS}
  rows = []
  start = time.monotonic()

  for case in cases:
    predicted = await run_case(runner, case)
    marks = []
    for field in FIELDS:
      truth = case["expected"].get(field)
      if truth is None:
        marks.append(f"{field[:4]}:—")
        continue
      graded[field][1] += 1
      hit = predicted.get(field) == truth
      graded[field][0] += hit
      marks.append(f"{field[:4]}:{'✓' if hit else '✗'}")
    rows.append((case["id"], marks, predicted, case["expected"]))
    print(f"  {case['id']:<12} {'  '.join(marks)}")

  elapsed = time.monotonic() - start
  print(f"\n--- per-field accuracy [{label}] ---")
  for field in FIELDS:
    correct, total = graded[field]
    if total:
      print(f"{field:<10} {correct:>3}/{total:<3} {correct / total:6.1%}")
    else:
      print(f"{field:<10}   — no ground truth in this slice")

  overall_c = sum(c for c, _ in graded.values())
  overall_t = sum(t for _, t in graded.values())
  print(f"{'OVERALL':<10} {overall_c:>3}/{overall_t:<3} {overall_c / overall_t:6.1%}")

  print(f"\nwall clock {elapsed:.0f}s")
  print(meter.report())

  if args.dump:
    out = ROOT / "eval" / f"run-{args.topology}-{'ablated' if args.ablate_area else 'grounded'}.json"
    out.write_text(json.dumps(
        [{"id": i, "predicted": p, "expected": e} for i, _, p, e in rows], indent=2))
    print(f"\nwrote {out}")


if __name__ == "__main__":
  ap = argparse.ArgumentParser()
  ap.add_argument("--limit", type=int, default=12)
  ap.add_argument("--ablate-area", action="store_true",
                  help="drop the module map from the area prompt")
  ap.add_argument("--topology", choices=["workflow", "sequential"], default="workflow")
  ap.add_argument("--dump", action="store_true", help="write per-case results to eval/")
  asyncio.run(main(ap.parse_args()))
