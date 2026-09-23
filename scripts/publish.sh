#!/usr/bin/env bash
# Make the repository public, set its homepage and topics, and enable GitHub
# Pages from the docs/ folder on main. Idempotent: safe to run twice.
set -euo pipefail

REPO="famesjranko/adk-issue-triage"
HOME_URL="https://famesjranko.github.io/adk-issue-triage/"

gh api -X PATCH "repos/$REPO" -F private=false -f homepage="$HOME_URL" >/dev/null
gh api -X PUT "repos/$REPO/topics" \
  -f 'names[]=google-adk' -f 'names[]=gemini' -f 'names[]=multi-agent' \
  -f 'names[]=llm-evals' -f 'names[]=cloud-run' -f 'names[]=python' >/dev/null

if gh api "repos/$REPO/pages" >/dev/null 2>&1; then
  gh api -X PUT "repos/$REPO/pages" -f 'source[branch]=main' -f 'source[path]=/docs' >/dev/null
else
  gh api -X POST "repos/$REPO/pages" -f 'source[branch]=main' -f 'source[path]=/docs' >/dev/null
fi

gh repo view "$REPO" --json isPrivate,homepageUrl,repositoryTopics \
  -q '"private: \(.isPrivate)\nhomepage: \(.homepageUrl)\ntopics: \([.repositoryTopics[].name] | join(", "))"'
echo "pages: $HOME_URL (first build takes a minute or two)"
