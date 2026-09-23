"""Grounding pulled from the target repository's own architecture docs.

The area/* labels map onto real Gradle modules. Rather than describing those
modules in a hand-written prompt that will silently rot, we read them out of the
repo's ARCHITECTURE.md so the grounding tracks the code.

Source: $MUSICMETA_PATH/ARCHITECTURE.md when that env var points at a local
checkout, otherwise the file on GitHub via `gh api`.
"""

import functools
import os
import subprocess
from pathlib import Path

ARCH_REPO = "famesjranko/musicmeta"


class _Unavailable(Exception):
    pass


def _read_architecture() -> str:
    local = os.environ.get("MUSICMETA_PATH")
    if local:
        doc = Path(local) / "ARCHITECTURE.md"
        if doc.is_file():
            return doc.read_text()
    args = [
        "api", f"repos/{ARCH_REPO}/contents/ARCHITECTURE.md",
        "-H", "Accept: application/vnd.github.raw",
    ]
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _Unavailable(f"gh {' '.join(args)} failed: {exc}") from exc
    if result.returncode != 0:
        raise _Unavailable(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


@functools.cache
def read_module_map() -> str:
    """Return the 'Module map' section of the repository's ARCHITECTURE.md.

    Used to ground area/* classification in the repository's real module
    structure instead of the model's guess at it.

    Returns:
        The module map as text, or an explanatory string if it cannot be read.
    """
    try:
        text = _read_architecture()
    except _Unavailable as exc:
        return f"(could not read {ARCH_REPO} ARCHITECTURE.md: {exc})"
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == "## Module map"), None)
    if start is None:
        return "(no '## Module map' section in ARCHITECTURE.md)"
    # Take the first fenced block after the heading — the plain-text tree.
    fence_open = next((i for i in range(start, len(lines)) if lines[i].startswith("```")), None)
    fence_close = (
        None if fence_open is None
        else next((i for i in range(fence_open + 1, len(lines)) if lines[i].startswith("```")), None)
    )
    if fence_close is None:
        return "(no fenced block under '## Module map' in ARCHITECTURE.md)"
    return "\n".join(lines[fence_open + 1 : fence_close])
