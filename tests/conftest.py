"""The suite never touches the network.

Importing the package builds the prompts, which reads the module map from
GitHub when $MUSICMETA_PATH is unset. Blocking urlopen here, before any test
module imports the package, makes that read fail fast and deterministically
instead of depending on connectivity. Tests that exercise the transport
monkeypatch urlopen or tools.github._request themselves.
"""

import urllib.error
import urllib.request


def _network_disabled(*args, **kwargs):
  raise urllib.error.URLError("network disabled in tests")


urllib.request.urlopen = _network_disabled
