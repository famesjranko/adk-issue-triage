"""Grounding pulled from the target repository's own architecture docs.

The area/* labels map onto real Gradle modules. Rather than describing those
modules in a hand-written prompt that will silently rot, we read them out of the
repo's ARCHITECTURE.md so the grounding tracks the code.

Source: $MUSICMETA_PATH/ARCHITECTURE.md when that env var points at a local
checkout, otherwise GitHub with the committed repository snapshot as fallback.
"""

import functools
import os
from pathlib import Path

from issue_triage.repository_data import RepositoryData, extract_module_map
from . import github

@functools.cache
def read_module_map() -> str:
    """Return the 'Module map' section of the repository's ARCHITECTURE.md.

    Used to ground area/* classification in the repository's real module
    structure instead of the model's guess at it.

    Returns:
        The module map as text, or an explanatory string if it cannot be read.
    """
    local = os.environ.get("MUSICMETA_PATH")
    if local:
        doc = Path(local) / "ARCHITECTURE.md"
        if doc.is_file():
            return extract_module_map(doc.read_text())
    try:
        return RepositoryData(github._request).module_map().value
    except Exception as exc:
        return f"(could not read {github.REPO} ARCHITECTURE.md: {github._describe(exc)})"
