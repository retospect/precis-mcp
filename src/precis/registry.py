"""Handler registry — maps schemes and file extensions to handlers.

Built-in handlers are registered at import time. External plugins are
discovered via ``precis.schemes`` and ``precis.file_types`` entry points.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from pathlib import Path

from precis.protocol import Handler, PrecisError

log = logging.getLogger(__name__)

# scheme → Handler class (for non-file schemes like paper:)
SCHEMES: dict[str, type[Handler]] = {}

# extension → Handler class (for file: scheme, dispatched by extension)
FILE_TYPES: dict[str, type[Handler]] = {}

_discovered = False


def _register_builtins() -> None:
    """Register built-in handlers (fail gracefully if deps missing)."""
    try:
        from precis.handlers.word import WordHandler
        FILE_TYPES.setdefault(".docx", WordHandler)
    except ImportError:
        log.debug("WordHandler not available (missing python-docx?)")

    try:
        from precis.handlers.tex import TexHandler
        FILE_TYPES.setdefault(".tex", TexHandler)
    except ImportError:
        log.debug("TexHandler not available")

    try:
        from precis.handlers.paper import PaperHandler
        SCHEMES.setdefault("paper", PaperHandler)
    except ImportError:
        log.debug("PaperHandler not available (missing acatome-store?)")


def _discover() -> None:
    """Load built-in handlers and entry-point plugins (once)."""
    global _discovered
    if _discovered:
        return
    _discovered = True

    _register_builtins()

    for ep in entry_points(group="precis.schemes"):
        try:
            cls = ep.load()
            SCHEMES[ep.name] = cls
            log.debug("Registered scheme %s: → %s", ep.name, cls.__name__)
        except Exception:
            log.warning("Failed to load scheme plugin: %s", ep.name, exc_info=True)

    for ep in entry_points(group="precis.file_types"):
        try:
            cls = ep.load()
            FILE_TYPES[ep.name] = cls
            log.debug("Registered file type %s → %s", ep.name, cls.__name__)
        except Exception:
            log.warning("Failed to load file_type plugin: %s", ep.name, exc_info=True)


def register_scheme(name: str, handler_cls: type[Handler]) -> None:
    """Register a scheme handler programmatically."""
    SCHEMES[name] = handler_cls


def register_file_type(ext: str, handler_cls: type[Handler]) -> None:
    """Register a file extension handler programmatically."""
    FILE_TYPES[ext] = handler_cls


def resolve(scheme: str, path: str) -> Handler:
    """Return the appropriate handler instance for a scheme + path.

    For ``file:`` scheme, dispatches by file extension.
    For other schemes, dispatches by scheme name.
    """
    _discover()

    if scheme == "file":
        ext = Path(path).suffix.lower()
        handler_cls = FILE_TYPES.get(ext)
        if not handler_cls:
            supported = ", ".join(sorted(FILE_TYPES.keys())) or "(none)"
            raise PrecisError(
                f"No handler for {ext} files.\n"
                f"Supported extensions: {supported}"
            )
        return handler_cls()

    handler_cls = SCHEMES.get(scheme)
    if not handler_cls:
        supported = ", ".join(sorted(SCHEMES.keys())) or "(none)"
        raise PrecisError(
            f"Unknown scheme: {scheme}:\n"
            f"Supported schemes: file, {supported}"
        )
    return handler_cls()
