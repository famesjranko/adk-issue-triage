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
