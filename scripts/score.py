"""Grade the agent against the repository's human-applied labels.

`adk eval` grades the final response as text. That is the wrong unit here: the
agent emits four independent fields and they fail independently. An area/* miss
and a priority/* miss are different bugs with different fixes, and a single
pass/fail per case hides which one moved. So this scores per field.

    uv run python scripts/score.py --limit 12
    uv run python scripts/score.py --limit 12 --ablate-area
    uv run python scripts/score.py --limit 12 --topology sequential
    uv run python scripts/score.py --limit 12 --repeat 3 --dump

Sampling is not fully deterministic even at temperature 0, so one run's accuracy
is a draw, not a measurement. --repeat scores the same cases N times and reports
the mean and the min–max per field, which is what a prompt change has to beat.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / "issue_triage" / ".env", override=True)

from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from issue_triage import console, prompts  # noqa: E402
from issue_triage.plugins import CostMeterPlugin, RateLimitPlugin  # noqa: E402
from issue_triage.quota import daily_quota_message  # noqa: E402

FIELDS = ("kind", "area", "priority", "readiness")


def rpm_override() -> dict[str, int] | None:
  """Per-model request limit from TRIAGE_RPM, or None for the free-tier defaults.

  The defaults in RateLimitPlugin are the AI Studio free-tier numbers. On
  Vertex AI the ceiling is far higher, and running a full eval at 15 RPM
  would take hours for no reason. Applies to both model seams.
  """
  raw = os.environ.get("TRIAGE_RPM")
  if not raw:
    return None
  rpm = int(raw)
  return {prompts.FAST_MODEL: rpm, prompts.SMART_MODEL: rpm}


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


Graded = dict[str, tuple[int, int]]


def grade(case: dict, predicted: dict) -> tuple[Graded, list[str]]:
  """(correct, gradeable) per field for one case, plus its printable marks.

  A field with no human label is not counted either way, rather than counted
  as a miss.
  """
  graded, marks = {}, []
  for field in FIELDS:
    truth = case["expected"].get(field)
    if truth is None:
      graded[field] = (0, 0)
      marks.append(f"{field[:4]}:—")
      continue
    hit = predicted.get(field) == truth
    graded[field] = (int(hit), 1)
    marks.append(f"{field[:4]}:{'✓' if hit else '✗'}")
  return graded, marks


async def score_run(runner, cases: list[dict]) -> tuple[Graded, list[dict]]:
  """Run every case once. Returns (correct, gradeable) per field and the rows."""
  totals = {f: (0, 0) for f in FIELDS}
  rows = []
  for case in cases:
    predicted = await run_case(runner, case)
    graded, marks = grade(case, predicted)
    totals = {f: (totals[f][0] + graded[f][0], totals[f][1] + graded[f][1]) for f in FIELDS}
    rows.append({"id": case["id"], "predicted": predicted, "expected": case["expected"]})
    print(f"  {case['id']:<12} {'  '.join(marks)}")
  return totals, rows


def summarise(runs: list[Graded]) -> dict[str, dict]:
  """Mean, min and max accuracy across repeats, per field and OVERALL.

  The cases are the same every repeat, so the gradeable count per field must be
  too; a difference means the runs are not comparable and is an error. A field
  with nothing gradeable has no accuracy, so its mean/min/max are None.
  """
  if not runs:
    raise ValueError("summarise needs at least one run")
  pooled = [
      run | {"OVERALL": (sum(c for c, _ in run.values()), sum(t for _, t in run.values()))}
      for run in runs
  ]
  summary = {}
  for field in (*FIELDS, "OVERALL"):
    gradeable = {run[field][1] for run in pooled}
    if len(gradeable) != 1:
      raise ValueError(f"{field}: gradeable count differs between runs: {sorted(gradeable)}")
    n = gradeable.pop()
    if n == 0:
      summary[field] = {"mean": None, "min": None, "max": None, "n": 0}
      continue
    accuracy = [run[field][0] / n for run in pooled]
    summary[field] = {
        "mean": sum(accuracy) / len(accuracy),
        "min": min(accuracy),
        "max": max(accuracy),
        "n": n,
    }
  return summary


def print_accuracy(graded: Graded, label: str) -> None:
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


def print_spread(summary: dict[str, dict], repeats: int, label: str) -> None:
  print(f"\n--- spread over {repeats} runs [{label}] ---")
  print(f"{'field':<10} {'mean':>6}  {'min–max':<13}  (n)")
  for field, s in summary.items():
    if s["n"] == 0:
      print(f"{field:<10}   — no ground truth in this slice")
      continue
    spread = f"{s['min']:.1%}–{s['max']:.1%}"
    print(f"{field:<10} {s['mean']:6.1%}  {spread:<13}  ({s['n']})")


async def main(args) -> None:
  if args.repeat < 1:
    raise SystemExit("--repeat must be at least 1")
  area_instruction = (
      prompts.AREA_INSTRUCTION_ABLATED if args.ablate_area else prompts.AREA_INSTRUCTION
  )
  meter = CostMeterPlugin()
  runner = InMemoryRunner(
      agent=build_agent(args.topology, area_instruction),
      app_name="issue_triage",
      plugins=[RateLimitPlugin(limits=rpm_override()), meter],
  )

  cases = load_cases(args.limit)
  grounding = "ablated" if args.ablate_area else "grounded"
  label = f"{args.topology}/{grounding}"
  print(f"scoring {len(cases)} cases × {args.repeat}  [{label}]")

  runs, rows = [], []
  start = time.monotonic()
  for i in range(args.repeat):
    print(f"\nrun {i + 1}/{args.repeat}" if args.repeat > 1 else "")
    graded, run_rows = await score_run(runner, cases)
    print_accuracy(graded, label)
    runs.append(graded)
    rows.append(run_rows)
  elapsed = time.monotonic() - start

  summary = summarise(runs)
  if args.repeat > 1:
    print_spread(summary, args.repeat, label)

  print(f"\nwall clock {elapsed:.0f}s")
  print(meter.report())

  if args.dump:
    out_dir = ROOT / "eval" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{args.topology}-{grounding}-n{len(cases)}-r{args.repeat}.json"
    out.write_text(json.dumps({
        "topology": args.topology,
        "grounding": grounding,
        "cases": len(cases),
        "repeat": args.repeat,
        "runs": [{"graded": g, "rows": r} for g, r in zip(runs, rows)],
        "summary": summary,
    }, indent=2, ensure_ascii=False))
    print(f"\nwrote {out}")


if __name__ == "__main__":
  ap = argparse.ArgumentParser()
  ap.add_argument("--limit", type=int, default=12)
  ap.add_argument("--repeat", type=int, default=1,
                  help="score the same cases N times and report mean and min–max per field")
  ap.add_argument("--ablate-area", action="store_true",
                  help="drop the module map from the area prompt")
  ap.add_argument("--topology", choices=["workflow", "sequential"], default="workflow")
  ap.add_argument("--dump", action="store_true",
                  help="write every run's per-case results and the summary to eval/results/")
  console.compact_library_tracebacks()
  try:
    asyncio.run(main(ap.parse_args()))
  except Exception as exc:
    message = daily_quota_message(exc)
    if message is None:
      raise
    console.notice("QUOTA LIMIT", message)
    raise SystemExit(2) from None
