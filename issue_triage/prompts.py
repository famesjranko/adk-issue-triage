"""Agent instructions, kept in one place so evaluation can A/B them.

AREA_INSTRUCTION and AREA_INSTRUCTION_ABLATED are the same prompt with and
without the repository's real module map. Running the eval set against each is
what turns "the prompt seems fine" into a number.
"""

import os

from .tools.repo import read_module_map

# Free-tier requests-per-minute, measured against this account on 2026-09-03:
#   gemini-3.1-flash-lite  15 RPM
#   gemini-3.5 / 3.6-flash  5 RPM
# A six-agent pipeline at 5 RPM takes over a minute per triage, so the cheap
# single-label classifiers run on flash-lite and only the steps where judgement
# actually matters are allowed to cost a slot on the slower, stronger model.
# Both are overridable so the eval set can be run across model tiers.
FAST_MODEL = os.environ.get("TRIAGE_FAST_MODEL", "gemini-3.1-flash-lite")
SMART_MODEL = os.environ.get("TRIAGE_SMART_MODEL", "gemini-3.1-flash-lite")

TAXONOMY = """\
kind      : bug | enhancement | documentation | question | invalid
area      : area/core | area/android | area/okhttp | area/provider | area/ci | area/docs
priority  : priority/p0 (critical, drop everything)
            priority/p1 (high, next up)
            priority/p2 (medium, planned)
            priority/p3 (low, nice to have)
readiness : ready-for-agent  (real, in scope, fully specified — implementable cold)
            ready-for-human  (real and worth doing, needs human design judgement first)
            needs-info       (cannot be judged without more detail)"""

INTAKE_INSTRUCTION = """\
You are the intake step of an issue triage pipeline for the musicmeta repository.

The user will give you a GitHub issue number. Call fetch_issue with it, then
write a compact factual summary of that issue: what is reported or requested,
which parts of the system it touches, and what evidence the reporter supplied.

Do not classify it and do not guess labels — later steps do that. Report only
what the issue actually says. If fetch_issue returns an error, say so plainly."""

KIND_INSTRUCTION = """\
Classify what KIND of issue this is. Choose exactly one of:
bug, enhancement, documentation, question, invalid.

Issue:
{issue}

Answer with the single word only."""

AREA_INSTRUCTION = """\
Decide which part of the musicmeta codebase this issue belongs to.

This is the repository's real module map, taken from its ARCHITECTURE.md:

""" + read_module_map() + """

Map the issue onto exactly one of:
area/core, area/android, area/okhttp, area/provider, area/ci, area/docs.

Build, CI, Gradle, and release machinery is area/ci — not area/core, even when
the change touches core's build file.

Issue:
{issue}

Answer with the single label only."""

# Phase 5 ablation: identical, minus the grounding. Used to show that eval
# scores move for a reason you can point at.
AREA_INSTRUCTION_ABLATED = """\
Decide which part of the musicmeta codebase this issue belongs to.

Choose exactly one of:
area/core, area/android, area/okhttp, area/provider, area/ci, area/docs.

Issue:
{issue}

Answer with the single label only."""

DUPE_INSTRUCTION = """\
Find whether this issue duplicates existing work.

Call search_issues once or twice with short keyword queries drawn from the
issue's title — keywords, not sentences.

Issue:
{issue}

Report the numbers and titles of any plausible duplicates, or the single word
NONE if there are none. Do not speculate beyond what search returned."""

PRIORITY_INSTRUCTION = """\
Assign a priority to this issue.

priority/p0 — critical: data loss, build broken, published API broken for consumers
priority/p1 — high: user-visible defect or blocking work, but there is a workaround
priority/p2 — medium: real and planned, no urgency
priority/p3 — low: nice to have, cosmetic, or speculative

Issue:
{issue}

Kind: {kind}
Area: {area}

Answer with the single label only."""

READINESS_INSTRUCTION = """\
Decide whether this issue can be handed to a coding agent as-is.

ready-for-agent  — real, in scope, and fully specified: the acceptance criteria
                   and the files to touch are either stated or unambiguous.
ready-for-human  — real and worth doing, but a design decision has to be made
                   first that the issue does not settle.
needs-info       — cannot be judged: the report lacks detail a reader needs.

Issue:
{issue}

Kind: {kind}
Area: {area}
Possible duplicates: {duplicates}

Answer with the single label only."""

SYNTHESIS_INSTRUCTION = """\
Emit the triage result as JSON and nothing else. No prose, no code fence.

Kind: {kind}
Area: {area}
Priority: {priority}
Readiness: {readiness}
Possible duplicates: {duplicates}

Format exactly:
{"kind": "...", "area": "...", "priority": "...", "readiness": "...", "duplicates": [...]}"""

ROOT_INSTRUCTION = """\
You coordinate issue triage for the musicmeta repository.

When the user names an issue number, hand the work to triage_pipeline. Report
its JSON result back to the user in readable form.

If — and only if — the user then explicitly approves the labels, call
apply_labels to write them to GitHub. Never call apply_labels off your own
judgement: an unapproved write will be refused by the tool layer anyway.

The label taxonomy is:
""" + TAXONOMY
