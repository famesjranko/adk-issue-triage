"""Grounding pulled from the target repository's own architecture docs.

The area/* labels map onto real Gradle modules. Rather than describing those
modules in a hand-written prompt that will silently rot, we read them out of the
repo's ARCHITECTURE.md so the grounding tracks the code.
"""

from pathlib import Path

REPO_PATH = Path("/home/andy/dev/musicmeta")


def read_module_map() -> str:
    """Return the 'Module map' section of the repository's ARCHITECTURE.md.

    Used to ground area/* classification in the repository's real module
    structure instead of the model's guess at it.

    Returns:
        The module map as text, or an explanatory string if it cannot be read.
    """
    doc = REPO_PATH / "ARCHITECTURE.md"
    if not doc.is_file():
        return f"(no ARCHITECTURE.md found at {doc})"
    lines = doc.read_text().splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "## Module map")
    except StopIteration:
        return "(no '## Module map' section in ARCHITECTURE.md)"
    # Take the first fenced block after the heading — the plain-text tree.
    fence_open = next(i for i in range(start, len(lines)) if lines[i].startswith("```"))
    fence_close = next(i for i in range(fence_open + 1, len(lines)) if lines[i].startswith("```"))
    return "\n".join(lines[fence_open + 1 : fence_close])
