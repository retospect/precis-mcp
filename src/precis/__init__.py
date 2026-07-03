"""precis-mcp v8 — MCP server for paper / document / state / tool access."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

#: The one source of truth for the running version is the installed
#: distribution metadata (``pyproject.toml``'s ``version``, baked into
#: ``*.dist-info`` at install time). Reading it here means ``__version__``
#: can never drift from the packaged version the way a hand-maintained
#: literal did (it lagged at 8.17.0 while the package shipped 8.20.0).
#: The literal below is only a fallback for a bare source tree with no
#: install metadata (rare — even editable installs write a ``.dist-info``).
try:
    __version__ = _dist_version("precis-mcp")
except PackageNotFoundError:  # pragma: no cover — uninstalled source tree
    __version__ = "8.20.0"
