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

if [ -t 1 ]; then W=$(tput cols 2>/dev/null || echo 100); else W=${COLUMNS:-100}; fi
[ "$W" -gt 100 ] && W=100

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  RESET=$'\033[0m'; BOLD=$'\033[1m'; MUTED=$'\033[2m'
  CYAN=$'\033[36m'; GREEN=$'\033[32m'; AMBER=$'\033[33m'; BLUE=$'\033[34m'
else
  RESET=""; BOLD=""; MUTED=""; CYAN=""; GREEN=""; AMBER=""; BLUE=""
fi

b() { printf '%s%s%s\n' "$BOLD$CYAN" "$*" "$RESET"; }
dim() { printf '%s%s%s\n' "$MUTED" "$*" "$RESET"; }
warn() { printf '%s%s%s%s\n' "$BOLD" "$AMBER" "$*" "$RESET"; }
ok() { printf '  %s✓%s  %s\n' "$GREEN" "$RESET" "$*"; }
skip() { printf '  %s○%s  %s%s%s\n' "$MUTED" "$RESET" "$MUTED" "$*" "$RESET"; }
item() { printf '  %s│%s  %-17s %s\n' "$MUTED" "$RESET" "$1" "$2"; }
rule() {
  printf '%s' "$MUTED"
  printf '─%.0s' $(seq 1 "$W")
  printf '%s\n' "$RESET"
}

banner() {
  printf '\n%s◆%s %sISSUE TRIAGE%s  %sGoogle ADK workflow case study%s\n' \
    "$CYAN" "$RESET" "$BOLD" "$RESET" "$MUTED" "$RESET"
  rule
}

hdr() {  # hdr <n> <title> <what it proves> <what to watch>
  printf '\n'
  printf '%s◆%s  %sSTEP %02d%s  %s%s%s\n' \
    "$CYAN" "$RESET" "$MUTED" "$1" "$RESET" "$BOLD" "$2" "$RESET"
  rule
  printf '  %sPROVES%s  %s\n' "$CYAN" "$RESET" "$3"
  printf '  %sWATCH %s  %s%s%s\n\n' "$AMBER" "$RESET" "$MUTED" "$4" "$RESET"
}

# ADK prints experimental-feature notices on every import; the package silences
# them via ADK's own switch, but sub-tools started here need it too.
export ADK_SUPPRESS_EXPERIMENTAL_FEATURE_WARNINGS=true

step0() {
  hdr 0 "Environment check" \
      "everything is wired before a single token is spent" \
      "no model calls happen here at all"
  item "Python" "$(uv run python -V 2>&1 | cut -d' ' -f2)"
  item "Google ADK" "$(uv run python -c 'import google.adk;print(google.adk.__version__)' 2>/dev/null)"
  item "GitHub CLI" "$(gh auth status 2>&1 | grep -o 'Logged in to [^ ]*' | head -1)"
  if grep -q '^GOOGLE_API_KEY=.\+' issue_triage/.env 2>/dev/null; then
    item "API key" "${GREEN}present${RESET} ($(grep '^GOOGLE_API_KEY=' issue_triage/.env | cut -d= -f2- | wc -c) chars)"
  else
    item "API key" "${AMBER}missing — put it in issue_triage/.env${RESET}"
  fi
  item "Secret safety" "$(git check-ignore issue_triage/.env >/dev/null 2>&1 && printf '%signored by Git%s' "$GREEN" "$RESET" || printf '%sNOT IGNORED%s' "$AMBER" "$RESET")"
  printf '\n'
  item "Free tier" "500 requests / model / day"
  item "Full eval" "~280 requests · one pass/day fits"
}

step1() {
  hdr 1 "The tool layer, with no model involved" \
      "tools are a typed boundary, and fetch_issue does not leak the answer" \
      "the returned keys — there is no 'labels' key, because those are ground truth"
  uv run python -c "
from issue_triage.tools.github import fetch_issue, search_issues, list_labels
from issue_triage.tools.repo import read_module_map
i = fetch_issue(231)
print('  ISSUE 231')
print('  ├─ title       ', i['title'][:70])
print('  ├─ safe payload', '✓ labels withheld' if 'labels' not in i else '✗ labels leaked')
print('  └─ fields      ', ', '.join(sorted(i)))
print()
print('  TOOL CONTRACTS')
print('  ├─ list_labels ', len(list_labels()['labels']), 'real repository labels')
print('  └─ search      ', [m['number'] for m in search_issues('Room schema')['matches']][:5])
print()
print('  LIVE GROUNDING · musicmeta/ARCHITECTURE.md')
for line in read_module_map().splitlines():
    print('  ' + line)
"
}

step2() {
  hdr 2 "The deterministic guards" \
      "validation and authorisation policy, tested without a model" \
      "every test passes. Guards are code, so they get code tests — not prompt coaxing"
  if uv run pytest tests/ -q --disable-warnings; then
    ok "Deterministic policy and validation checks passed"
  else
    warn "Tests failed"
    return 1
  fi
}

step3() {
  hdr 3 "Legacy topology — SequentialAgent + ParallelAgent" \
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
  b "  SAMPLE CASE"
  uv run python -c "
import json,pathlib
c=json.loads(pathlib.Path('eval/triage.evalset.json').read_text())['eval_cases'][0]
turn=c['conversation'][0]
expected=json.loads(turn['final_response']['parts'][0]['text'])
tool=turn['intermediate_data']['tool_uses'][0]
print(f\"  ├─ id          {c['eval_id']}\")
print(f\"  ├─ prompt      {turn['user_content']['parts'][0]['text']}\")
print(f\"  ├─ tool        {tool['name']}({tool['args']['number']})\")
print('  └─ expected    ' + ' · '.join(f'{k}={v or \"—\"}' for k,v in expected.items()))"
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
  all) banner
       quota_exhausted=0
       for n in 0 1 2 3 4 5 7 10; do
         if [ "$quota_exhausted" -eq 1 ] && [[ "$n" =~ ^(4|5|10)$ ]]; then
           skip "Step $n · Gemini daily quota exhausted"
           continue
         fi
         "step$n"
         status=$?
         if [ "$status" -eq 2 ] && [[ "$n" =~ ^(3|4|5|10)$ ]]; then
           quota_exhausted=1
         fi
       done
       printf '\n'
       if [ "$quota_exhausted" -eq 1 ]; then
         warn "  DEMO PAUSED  ·  model quota exhausted; offline sections completed"
       else
         ok "Non-writing walkthrough complete"
       fi
       skip "Steps 6, 8 and 9 · write, long-running and interactive" ;;
  *)
    banner
    item "00  ENVIRONMENT" "wiring and secret safety ${MUTED}· offline${RESET}"
    item "01  TOOL LAYER" "typed boundaries and live grounding ${MUTED}· offline${RESET}"
    item "02  GUARDS" "deterministic policy tests ${MUTED}· offline${RESET}"
    item "03  LEGACY RUN" "Sequential + Parallel topology"
    item "04  GRAPH RUN" "ADK 2.x Workflow topology"
    item "05  APPROVAL" "a risky write suspends"
    item "06  LOW RISK" "an area-only write executes ${AMBER}· writes GitHub${RESET}"
    item "07  EVAL SET" "ground truth from real labels ${MUTED}· offline${RESET}"
    item "08  SCORE" "per-field evaluation ${AMBER}· ~9 minutes${RESET}"
    item "09  ADK WEB" "interactive trace inspector"
    item "10  TIMELINE" "decode one run visually"
    printf '\n'
    b "  ./scripts/demo.sh all"
    dim "  Runs the complete non-writing walkthrough"
    printf '\n'
    item "Quick tour" "./scripts/demo.sh 4   ${MUTED}then 5, then 10${RESET}"
    ;;
esac
