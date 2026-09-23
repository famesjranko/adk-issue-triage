"""Terminal rendering shared by the scripts.

An agent run is a stream of events with wildly uneven shapes — a one-word
classification next to an 8KB issue body. Printing them raw is unreadable, so
this collapses each event to a single aligned line with its payload summarised
rather than dumped.

Colour is off automatically when stdout is not a terminal, or when NO_COLOR is
set.
"""

import json
import os
import shutil
import sys
import textwrap
from typing import Any

_ON = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str):
  return (lambda s: f"\033[{code}m{s}\033[0m") if _ON else (lambda s: s)


dim = _c("2")
bold = _c("1")
cyan = _c("36")
amber = _c("33")
red = _c("31")
green = _c("32")
blue = _c("34")


def width() -> int:
  return min(shutil.get_terminal_size((100, 24)).columns, 104)


def rule(char: str = "─") -> None:
  print(dim(char * width()))


def heading(text: str) -> None:
  print()
  print(bold(cyan(text)))
  rule()


def kv(label: str, value: str, pad: int = 14) -> None:
  print(f"  {label:<{pad}} {value}")


def wrap(text: str, indent: int, limit: int | None = None) -> str:
  """Wrap prose to the terminal, hanging-indented under its first line."""
  body = " ".join(str(text).split())
  if limit and len(body) > limit:
    body = body[:limit].rstrip() + "…"
  return textwrap.fill(
      body, width=width(), initial_indent="", subsequent_indent=" " * indent
  )


def summarise(value: Any, limit: int = 62) -> str:
  """One-line gist of a tool payload — shape first, contents second.

  A raw dict slice cuts mid-token and tells you nothing about what came back.
  The keys, or the number of matches, tell you whether the call worked.
  """
  if isinstance(value, dict):
    if "error" in value:
      return red("error: ") + str(value["error"])[:limit]
    bits = []
    for k, v in value.items():
      if isinstance(v, (list, tuple)):
        bits.append(f"{k}[{len(v)}]")
      elif isinstance(v, str):
        bits.append(k)
      else:
        bits.append(f"{k}={json.dumps(v)[:18]}")
    return dim("{") + " ".join(bits)[:limit] + dim("}")
  if isinstance(value, (list, tuple)):
    return dim(f"[{len(value)} items]")
  text = " ".join(str(value).split())
  return text[:limit] + ("…" if len(text) > limit else "")


def args_str(args: dict, limit: int = 54) -> str:
  out = ", ".join(f"{k}={json.dumps(v)}" for k, v in (args or {}).items())
  return out[:limit] + ("…" if len(out) > limit else "")


AUTHOR_W = 20
GUTTER = 8 + AUTHOR_W  # timestamp column + author column


def event_line(stamp: str, author: str, marker: str, body: str,
               state_key: str = "") -> None:
  """One event: [time][concurrency mark] author  body  → state."""
  print(f"{stamp:>6}{marker} {author[:AUTHOR_W - 1]:<{AUTHOR_W}} {body}")
  if state_key:
    print(" " * (GUTTER + 1) + dim("⤷ ") + blue(f'state["{state_key}"]'))


def silence_library_noise() -> None:
  """Quiet the experimental-feature notices so the run itself is readable.

  ADK ships a supported switch for this rather than needing a warnings filter,
  and it is checked when the decorated class is *constructed* — so it has to be
  set before anything imports an agent. That is why this is called from
  issue_triage/__init__.py and not from each script.

  These notices are worth reading once. They are not worth reading on every run
  of a demo. Note what is NOT hidden here: the context-cache warning was a real
  finding and is fixed properly in agent.py, not filtered away.
  """
  import logging
  import warnings

  os.environ.setdefault("ADK_SUPPRESS_EXPERIMENTAL_FEATURE_WARNINGS", "true")
  warnings.filterwarnings("ignore", category=UserWarning)
  warnings.filterwarnings("ignore", category=DeprecationWarning)
  for name in ("google_adk", "google.adk", "google_genai", "google.genai"):
    logging.getLogger(name).setLevel(logging.ERROR)


_ADK_LOGGER = "google_adk"
_EXC_LINE_LIMIT = 160


def _innermost(exc: BaseException) -> BaseException:
  """The root cause: follow `from` links, then ADK's `.error` wrapper."""
  seen = {id(exc)}
  while True:
    wrapped = getattr(exc, "error", None)
    nxt = exc.__cause__ or (wrapped if isinstance(wrapped, BaseException) else None)
    if nxt is None and not exc.__suppress_context__:
      nxt = exc.__context__
    if nxt is None or id(nxt) in seen:
      return exc
    seen.add(id(nxt))
    exc = nxt


def _one_line(exc: BaseException) -> str:
  lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
  text = f"{type(exc).__name__}: {lines[0]}" if lines else type(exc).__name__
  if len(text) > _EXC_LINE_LIMIT:
    text = text[:_EXC_LINE_LIMIT].rstrip() + "…"
  return text


def compact_library_tracebacks() -> None:
  """Print ADK's logged exceptions as one line each rather than a traceback.

  A failed model call is logged by the node runner, then again by the runner
  for the root node, each with a chained traceback, once per retry and once per
  branch of a fan-out. One exhausted quota came to eight 40-line tracebacks
  above the one line the script prints to explain it. What is kept: every
  record, its message, and the root cause's class and first line, so a 429
  still reads as `ClientError: 429 RESOURCE_EXHAUSTED ...`. What is hidden: the
  stack frames, which are all ADK's and say nothing about this project. A
  failure the scripts do not recognise still raises and prints in full.

  This is a record factory, not a filter on the `google_adk` logger. A logger's
  filters only see records logged on that logger, never ones that propagate up
  from children like `google_adk.google.adk.workflow._node_runner`. A filter on
  each child would miss any logger created after this runs.

  Called from the scripts and not from issue_triage/__init__.py, because the
  deployed service imports the package too. On Cloud Run the traceback is the
  only record of a failure, so its logs keep them in full.
  """
  import logging

  base = logging.getLogRecordFactory()
  if getattr(base, "compacts_adk_tracebacks", False):
    return

  def factory(*args, **kwargs) -> logging.LogRecord:
    record = base(*args, **kwargs)
    name = record.name
    if record.exc_info and record.exc_info[1] is not None and (
        name == _ADK_LOGGER or name.startswith(_ADK_LOGGER + ".")):
      cause = _one_line(_innermost(record.exc_info[1]))
      record.msg = f"{record.getMessage()}: {cause}"
      record.args = None
      record.exc_info = None
      record.exc_text = None
    return record

  factory.compacts_adk_tracebacks = True
  logging.setLogRecordFactory(factory)
