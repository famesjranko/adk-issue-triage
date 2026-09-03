"""Silence ADK's experimental notices before any agent class is constructed.

ADK checks ADK_SUPPRESS_EXPERIMENTAL_FEATURE_WARNINGS when the decorated class
is instantiated, so setting it inside a script is already too late — agent.py
builds an App at import time. Doing it here covers every entry point.
"""

from .console import silence_library_noise

silence_library_noise()

from . import agent  # noqa: E402
