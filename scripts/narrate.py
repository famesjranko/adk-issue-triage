"""Decode one triage run into a readable timeline.

The dev UI shows 30 raw events; the chat window shows one JSON blob. Neither
makes it obvious that three agents ran at the same instant or which field the
agent got wrong. This prints the run as a timeline, then scores it against the
labels a human actually applied.

    uv run python scripts/narrate.py --run 231        # run one and narrate it
    uv run python scripts/narrate.py --latest         # narrate the newest adk web session
    uv run python scripts/narrate.py --session <id>   # narrate a specific one
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / "issue_triage" / ".env", override=True)

from issue_triage import console  # noqa: E402
from issue_triage.quota import daily_quota_message, transient_unavailable_message  # noqa: E402
from issue_triage.repository_data import RepositoryData  # noqa: E402
from issue_triage.tools.github import _request  # noqa: E402

# This machine's NO_PROXY contains "::1", which httpx cannot parse. The .env
# override lands too late for a client built at import time, so repeat it here.
for var in ("NO_PROXY", "no_proxy"):
  os.environ[var] = "localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,*.local"

DEV_UI = "http://127.0.0.1:8000"
FIELDS = ("kind", "area", "priority", "readiness")
KINDS = {"bug", "enhancement", "documentation", "question", "invalid"}
READINESS = {"ready-for-agent", "ready-for-human", "needs-info"}


# ---------- getting events ----------

def from_server(session_id: str | None) -> dict:
  import urllib.request

  def get(path):
    with urllib.request.urlopen(f"{DEV_UI}{path}", timeout=10) as r:
      return json.load(r)

  base = "/apps/issue_triage/users/user/sessions"
  if session_id is None:
    sessions = get(base)
    if not sessions:
      sys.exit("no sessions on the dev server yet — send a message in adk web first")
    session_id = max(sessions, key=lambda s: s["lastUpdateTime"])["id"]
  return get(f"{base}/{session_id}")


async def run_fresh(number: int, workflow: bool) -> dict:
  from google.adk.runners import InMemoryRunner
  from google.genai import types

  from issue_triage.agent import root_agent
  from issue_triage.plugins import CostMeterPlugin, RateLimitPlugin
  from issue_triage.workflow_agent import build_workflow

  agent = build_workflow() if workflow else root_agent
  runner = InMemoryRunner(agent=agent, app_name="issue_triage",
                          plugins=[RateLimitPlugin(), CostMeterPlugin()])
  session = await runner.session_service.create_session(
      app_name="issue_triage", user_id="local")
  message = types.Content(role="user", parts=[types.Part(text=f"Triage issue {number}.")])

  events = [{"author": "user", "timestamp": None,
             "content": {"parts": [{"text": f"Triage issue {number}."}]}, "actions": {}}]
  async for e in runner.run_async(user_id="local", session_id=session.id,
                                  new_message=message):
    events.append(json.loads(e.model_dump_json(exclude_none=True)))
  final = await runner.session_service.get_session(
      app_name="issue_triage", user_id="local", session_id=session.id)
  if events[0]["timestamp"] is None and len(events) > 1:
    events[0]["timestamp"] = events[1].get("timestamp")
  return {"events": events, "state": dict(final.state)}


# ---------- ground truth ----------

def truth_for(number: int) -> dict | None:
  try:
    issues = RepositoryData(_request).labeled_issues().value
    issue = next(item for item in issues if item["number"] == number)
  except Exception:
    return None
  names = [
      label["name"] if isinstance(label, dict) else label
      for label in issue["labels"]
  ]
  return {
      "kind": next((label for label in names if label in KINDS), None),
      "area": next((label for label in names if label.startswith("area/")), None),
      "priority": next((label for label in names if label.startswith("priority/")), None),
      "readiness": next((label for label in names if label in READINESS), None),
  }


# ---------- rendering ----------

def describe(part: dict) -> str | None:
  if part.get("functionCall") or part.get("function_call"):
    fc = part.get("functionCall") or part["function_call"]
    args = ", ".join(f"{k}={json.dumps(v)[:22]}" for k, v in (fc.get("args") or {}).items())
    return console.amber("→ ") + console.bold(fc["name"]) + console.dim("(") + args + console.dim(")")
  if part.get("functionResponse") or part.get("function_response"):
    fr = part.get("functionResponse") or part["function_response"]
    return console.green("← ") + fr["name"] + "  " + console.summarise(fr.get("response"))
  text = (part.get("text") or "").strip()
  if text:
    return console.wrap(text, console.GUTTER + 3, limit=150)
  return None


def render(data: dict, number: int | None) -> None:
  events = data["events"]
  stamps = [e["timestamp"] for e in events if e.get("timestamp")]
  t0 = min(stamps) if stamps else 0

  console.heading("TIMELINE")

  # Build the rows first, so concurrency can be judged across the whole run.
  rows = []
  for e in events:
    parts = ((e.get("content") or {}).get("parts")) or []
    lines = [d for d in (describe(p) for p in parts) if d]
    if not lines:
      continue
    delta = ((e.get("actions") or {}).get("stateDelta")
             or (e.get("actions") or {}).get("state_delta") or {})
    keys = ",".join(k for k in delta if not k.startswith("__"))
    rows.append({"t": round((e.get("timestamp") or t0) - t0, 1),
                 "who": e.get("author", ""), "lines": lines, "keys": keys})

  # Concurrency means *different agents* at the same instant. An agent's own
  # call-then-result pair shares a timestamp too, and that is not concurrency.
  authors_at = {}
  for r in rows:
    authors_at.setdefault(r["t"], set()).add(r["who"])
  concurrent = {t for t, who in authors_at.items() if len(who - {"user"}) > 1}

  for r in rows:
    mark = console.cyan("┃") if r["t"] in concurrent else " "
    for i, line in enumerate(r["lines"]):
      stamp = f"{r['t']:>5.1f}s" if i == 0 else " " * 6
      console.event_line(stamp, r["who"], mark, line,
                         state_key=r["keys"] if i == 0 else "")

  if concurrent:
    print()
    print("  " + console.cyan("┃") + console.dim(
        " different agents at the same instant — the fan-out running concurrently"))

  state = data["state"]
  expected = truth_for(number) if number else None

  console.heading("RESULT" + ("" if expected else "   (ground truth unavailable)"))
  print(f"  {'field':<11}    {'predicted':<18}   {'human label'}")
  hits = total = 0
  for f in FIELDS:
    got = str(state.get(f, "")).strip() or "—"
    if expected is None:
      print(f"  {f:<11} {got:<18}")
      continue
    want = expected.get(f) or "—"
    if expected.get(f):
      total += 1
      ok = got == want
      hits += ok
      mark = console.green("✓") if ok else console.red("✗")
      paint = console.green if ok else console.red
    else:
      mark = console.dim("·")
      paint = console.dim
    print(f"  {f:<11} {mark}  {paint(f'{got:<18}')}   {console.dim(want)}")
  if total:
    print()
    print(f"  {console.bold(f'{hits}/{total}')} fields match the human labels"
          + console.dim("   · = no human label, not counted either way"))


if __name__ == "__main__":
  ap = argparse.ArgumentParser()
  g = ap.add_mutually_exclusive_group(required=True)
  g.add_argument("--run", type=int, metavar="ISSUE", help="run a fresh triage and narrate it")
  g.add_argument("--latest", action="store_true", help="narrate the newest adk web session")
  g.add_argument("--session", metavar="ID", help="narrate one adk web session")
  ap.add_argument("--sequential", action="store_true",
                  help="with --run, use the deprecated topology instead of the graph")
  args = ap.parse_args()

  if args.run:
    console.compact_library_tracebacks()
    try:
      data = asyncio.run(run_fresh(args.run, workflow=not args.sequential))
    except Exception as exc:
      if message := daily_quota_message(exc):
        console.notice("QUOTA LIMIT", message)
        raise SystemExit(2) from None
      if message := transient_unavailable_message(exc):
        console.notice("UPSTREAM UNAVAILABLE", message)
        raise SystemExit(3) from None
      raise
    render(data, args.run)
  else:
    data = from_server(None if args.latest else args.session)
    text = json.dumps(data)
    import re
    num = None
    m = re.search(r"Triage issue (\d+)", text)
    if m:
      num = int(m.group(1))
    render(data, num)
