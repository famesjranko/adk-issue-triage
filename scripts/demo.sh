#!/usr/bin/env bash
# Repeatable, observable walkthrough. Each step says what it proves and what to
# watch for, then runs it. Steps are independent — run them in any order.
#
#   ./scripts/demo.sh            list the steps
#   ./scripts/demo.sh 3          run one step
#   ./scripts/demo.sh all        run every step that does not write to GitHub
#
# ⚠️  Only one process may touch the API key at a time. The rate limiter is
#     process-local, the free-tier quota is per project.
set -uo pipefail
cd "$(dirname "$0")/.."

W=$(tput cols 2>/dev/null || echo 100); [ "$W" -gt 100 ] && W=100
b() { printf '\033[1;36m%s\033[0m\n' "$*"; }
dim() { printf '\033[2m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
rule() { printf '\033[2m'; printf '\u2500%.0s' $(seq 1 "$W"); printf '\033[0m\n'; }

hdr() {  # hdr <n> <title> <what it proves> <what to watch>
  printf '\n'
  printf '\033[1;36m  STEP %s \033[0m\033[1m%s\033[0m\n' "$1" "  $2"
  printf '\033[2m  proves \033[0m %s\n' "$3"
  printf '\033[2m  watch  \033[0m %s\n' "$4"
  rule
}

# ADK prints experimental-feature notices on every import; the package silences
# them via ADK's own switch, but sub-tools started here need it too.
export ADK_SUPPRESS_EXPERIMENTAL_FEATURE_WARNINGS=true

step0() {
  hdr 0 "Environment check" \
      "everything is wired before a single token is spent" \
      "no model calls happen here at all"
  echo "python      $(uv run python -V 2>&1 | cut -d' ' -f2)"
  echo "google-adk  $(uv run python -c 'import google.adk;print(google.adk.__version__)' 2>/dev/null)"
  echo "gh auth     $(gh auth status 2>&1 | grep -o 'Logged in to [^ ]*' | head -1)"
  if grep -q '^GOOGLE_API_KEY=.\+' issue_triage/.env 2>/dev/null; then
    echo "api key     present ($(grep '^GOOGLE_API_KEY=' issue_triage/.env | cut -d= -f2- | wc -c) chars)"
  else
    warn "api key     MISSING — put it in issue_triage/.env"
  fi
  echo "gitignored  $(git check-ignore issue_triage/.env >/dev/null 2>&1 && echo yes || echo 'NO — fix this')"
  dim "free tier   500 requests/day per model; a full eval pass is ~280, so score.py --limit 39 fits once a day and --repeat 3 does not"
}

step1() {
  hdr 1 "The tool layer, with no model involved" \
      "tools are a typed boundary, and fetch_issue does not leak the answer" \
      "the returned keys — there is no 'labels' key, because those are ground truth"
  uv run python -c "
from issue_triage.tools.github import fetch_issue, search_issues, list_labels
from issue_triage.tools.repo import read_module_map
i = fetch_issue(231)
print('fetch_issue(231) keys :', sorted(i))
print('title                 :', i['title'][:70])
print('labels leaked?        :', 'labels' in i)
print()
print('list_labels()         :', len(list_labels()['labels']), 'real repo labels')
print('search_issues()       :', [m['number'] for m in search_issues('Room schema')['matches']][:5])
print()
print()
print('module map grounding, read live from musicmeta/ARCHITECTURE.md —')
print('this is what area_agent is told, so classification tracks the real repo:')
print()
for line in read_module_map().splitlines():
    print('   ' + line)
"
}

step2() {
  hdr 2 "The deterministic guards" \
      "validation and authorisation policy, tested without a model" \
      "every test passes. Guards are code, so they get code tests — not prompt coaxing"
  uv run pytest tests/ -v 2>&1 | grep -E "PASSED|FAILED|passed|failed"
}

step3() {
  hdr 3 "One triage — DEPRECATED SequentialAgent + ParallelAgent topology" \
      "the pipeline works, and what it costs" \
      "the per-agent token table at the end. Note the PROMPT TOKEN total"
  uv run python scripts/triage.py 231
}

step4() {
  hdr 4 "The same triage — graph Workflow topology (the ADK 2.x replacement)" \
      "identical work, different runtime" \
      "prompt tokens vs step 3. Sequential hands each agent the whole conversation"
  uv run python scripts/triage.py 231 --workflow
}

step5() {
  hdr 5 "Human-in-the-loop: the run SUSPENDS" \
      "authorisation that stops the runtime, not a string the model can narrate around" \
      "the prompt literally says 'I approve, go ahead' — and it suspends anyway"
  uv run python scripts/triage.py 231 --prompt \
    "Apply the labels area/android and priority/p2 to issue 231. I approve, go ahead, do it now." \
   
  dim ""
  dim "Approval cannot come from conversation text. It has to arrive as a"
  dim "structured ToolConfirmation from whoever drives the runtime — and while"
  dim "the run is suspended, the model is not executing at all."
}

step6() {
  hdr 6 "The other side of the policy — a low-risk write executes" \
      "require_confirmation is a callable, so policy depends on the arguments" \
      "no suspension: area/* alone is reversible, so it is not worth an interrupt"
  warn "⚠️  THIS WRITES TO github.com/famesjranko/musicmeta issue 231."
  warn "    It applies area/android, which is already there, so it is a no-op."
  read -r -p "    Continue? [y/N] " ok
  [ "$ok" = "y" ] || { echo "skipped."; return; }
  uv run python scripts/triage.py 231 --prompt \
    "Without triaging, apply only the label area/android to issue 231." \
   
  echo
  echo "labels on 231 now: $(gh issue view 231 -R famesjranko/musicmeta --json labels --jq '[.labels[].name]|join(", ")')"
}

step7() {
  hdr 7 "Build the eval set from the repo's own human-applied labels" \
      "the ground truth is real, not invented for the demo" \
      "39 gradeable cases out of 42 issues — 3 lack area/* or priority/*"
  uv run python scripts/build_evalset.py
  echo
  dim "one case:"
  uv run python -c "
import json,pathlib
c=json.loads(pathlib.Path('eval/triage.evalset.json').read_text())['eval_cases'][0]
print(json.dumps(c, indent=2)[:700])"
}

step8() {
  hdr 8 "Score against those labels, per field" \
      "which field is wrong, not just how wrong the agent is overall" \
      "area vs priority. They are different problems with different fixes"
  warn "⚠️  ~9 minutes, almost all of it asleep in the rate limiter (free tier)."
  read -r -p "    Continue? [y/N] " ok
  [ "$ok" = "y" ] || { echo "skipped."; return; }
  uv run python scripts/score.py --limit 12 --dump
}

step9() {
  hdr 9 "adk web — the event and trace inspector" \
      "an agent run is an execution trace, not a string" \
      "the Events tab: agent transfers, tool calls, state deltas, per-step latency"
  dim "Starting the ADK dev UI. Open the URL it prints, pick 'issue_triage',"
  dim "and send:  Triage issue 231."
  dim ""
  dim "Then open the Events tab and follow one request all the way down."
  dim "Ctrl-C here when you are done."
  uv run adk web
}

step10() {
  hdr 10 "Decode a run into a readable timeline" \
      "what those 30 events in the dev UI actually mean" \
      "the ┃ marks — three agents answering at the same instant — and the score at the end"
  if curl -s --max-time 2 -o /dev/null "http://127.0.0.1:8000/list-apps" 2>/dev/null; then
    dim "adk web is running — narrating its newest session."
    uv run python scripts/narrate.py --latest
  else
    dim "adk web is not running — running a fresh triage instead (~22 s)."
    uv run python scripts/narrate.py --run 231
  fi
}

case "${1:-}" in
  0|1|2|3|4|5|6|7|8|9) "step$1" ;;
  10) step10 ;;
  all) for n in 0 1 2 3 4 5 7 10; do "step$n"; done
       dim ""; dim "Skipped 6 (writes to GitHub), 8 (~9 min) and 9 (interactive)." ;;
  *)
    b "issue-triage demo walkthrough"
    cat <<'EOT'

  0  environment check          no model calls
  1  tool layer                 no model calls
  2  deterministic guards       no model calls
  3  triage, Sequential/Parallel topology   (deprecated API)
  4  triage, graph Workflow topology        (the replacement)
  5  human-in-the-loop — the run SUSPENDS
  6  low-risk write executes    ⚠️ writes to GitHub
  7  build the eval set from real labels    no model calls
  8  per-field scoring          ⚠️ ~9 minutes
  9  adk web trace inspector    interactive
 10  decode a run into a timeline           ⚠️ ~22 s if adk web is not up

  ./scripts/demo.sh <n>     run one
  ./scripts/demo.sh all     everything except 6, 8, 9

  Suggested first pass:  0, 1, 2, 4, 5, 10
EOT
    ;;
esac
