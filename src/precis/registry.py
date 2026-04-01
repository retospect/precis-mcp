"""Handler registry — maps schemes and file extensions to handlers.

Discovery order:
  1. Built-in handlers (WordHandler, TexHandler, PaperHandler)
  2. ``precis.plugins`` entry points (new — auto-discovers pip-installed plugins)
  3. ``precis.schemes`` / ``precis.file_types`` entry points (legacy compat)

Plugins can be disabled via ``PRECIS_DISABLE_PLUGINS=name1,name2`` env var.
"""

from __future__ import annotations

import logging
import os
from importlib.metadata import entry_points
from pathlib import Path

from precis.protocol import Handler, Plugin, PrecisError

log = logging.getLogger(__name__)

# scheme → Handler class (for non-file schemes like paper:)
SCHEMES: dict[str, type[Handler]] = {}

# extension → Handler class (for file: scheme, dispatched by extension)
FILE_TYPES: dict[str, type[Handler]] = {}

# name → Plugin (all registered plugins)
PLUGINS: dict[str, Plugin] = {}

# corpus_id → Plugin (for write_policy enforcement and corpus dispatch)
CORPUS_PLUGINS: dict[str, Plugin] = {}

_discovered = False


def _disabled_plugins() -> set[str]:
    """Return set of plugin names to skip (from env var)."""
    raw = os.environ.get("PRECIS_DISABLE_PLUGINS", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def _register_plugin(plugin: Plugin) -> None:
    """Register a single plugin's schemes, file_types, and corpus mapping."""
    PLUGINS[plugin.name] = plugin
    for scheme in plugin.schemes:
        SCHEMES[scheme] = plugin.handler_cls
    for ext in plugin.file_types:
        FILE_TYPES[ext] = plugin.handler_cls
    if plugin.corpus_id:
        CORPUS_PLUGINS[plugin.corpus_id] = plugin
    log.debug(
        "Registered plugin '%s': schemes=%s file_types=%s corpus=%s",
        plugin.name,
        plugin.schemes,
        plugin.file_types,
        plugin.corpus_id,
    )


def _register_builtins() -> None:
    """Register built-in handlers as Plugin objects."""
    try:
        from precis.handlers.word import WordHandler

        _register_plugin(
            Plugin(
                name="word",
                handler_cls=WordHandler,
                file_types=[".docx"],
            )
        )
    except ImportError:
        log.debug("WordHandler not available (missing python-docx?)")

    try:
        from precis.handlers.tex import TexHandler

        _register_plugin(
            Plugin(
                name="tex",
                handler_cls=TexHandler,
                file_types=[".tex"],
            )
        )
    except ImportError:
        log.debug("TexHandler not available")

    try:
        from precis.handlers.markdown import MarkdownHandler

        _register_plugin(
            Plugin(
                name="markdown",
                handler_cls=MarkdownHandler,
                file_types=[".md", ".markdown"],
            )
        )
    except ImportError:
        log.debug("MarkdownHandler not available")

    try:
        from precis.handlers.plaintext import PlainTextHandler

        _register_plugin(
            Plugin(
                name="plaintext",
                handler_cls=PlainTextHandler,
                file_types=[".txt", ".text"],
            )
        )
    except ImportError:
        log.debug("PlainTextHandler not available")

    try:
        from precis.handlers.paper import PaperHandler

        _register_plugin(
            Plugin(
                name="papers",
                handler_cls=PaperHandler,
                schemes=["paper", "doi", "arxiv"],
                corpus_id="papers",
                write_policy="ingestion",
            )
        )
    except ImportError:
        log.debug("PaperHandler not available (missing acatome-store?)")

    try:
        from precis.handlers.todo import TodoHandler

        _register_plugin(
            Plugin(
                name="todos",
                handler_cls=TodoHandler,
                schemes=["todo"],
                corpus_id="todos",
                write_policy="direct",
            )
        )
    except ImportError:
        log.debug("TodoHandler not available (missing acatome-store?)")


def _discover() -> None:
    """Load built-in handlers and entry-point plugins (once).

    Discovery sources (in order):
      1. Built-in handlers
      2. ``precis.plugins`` entry points (each returns a Plugin instance)
      3. ``precis.schemes`` / ``precis.file_types`` (legacy compat)
    """
    global _discovered
    if _discovered:
        return
    _discovered = True

    disabled = _disabled_plugins()
    _register_builtins()

    # New plugin entry points
    for ep in entry_points(group="precis.plugins"):
        if ep.name in disabled:
            log.info("Plugin '%s' disabled via PRECIS_DISABLE_PLUGINS", ep.name)
            continue
        try:
            obj = ep.load()
            # Entry point can be a Plugin instance, a Plugin class, or a callable
            if isinstance(obj, Plugin):
                plugin = obj
            elif (isinstance(obj, type) and issubclass(obj, Plugin)) or callable(obj):
                plugin = obj()
            else:
                raise TypeError(
                    f"precis.plugins entry point '{ep.name}' must return a Plugin, "
                    f"got {type(obj).__name__}"
                )
            if not isinstance(plugin, Plugin):
                raise TypeError(
                    f"precis.plugins entry point '{ep.name}' callable returned "
                    f"{type(plugin).__name__}, expected Plugin"
                )
            _register_plugin(plugin)
        except Exception:
            log.warning(
                "Failed to load plugin '%s'",
                ep.name,
                exc_info=True,
            )

    # Legacy entry points (backward compat)
    for ep in entry_points(group="precis.schemes"):
        if ep.name not in SCHEMES:
            try:
                cls = ep.load()
                SCHEMES[ep.name] = cls
                log.debug("Legacy scheme %s → %s", ep.name, cls.__name__)
            except Exception:
                log.warning("Failed to load scheme plugin: %s", ep.name, exc_info=True)

    for ep in entry_points(group="precis.file_types"):
        if ep.name not in FILE_TYPES:
            try:
                cls = ep.load()
                FILE_TYPES[ep.name] = cls
                log.debug("Legacy file type %s → %s", ep.name, cls.__name__)
            except Exception:
                log.warning(
                    "Failed to load file_type plugin: %s", ep.name, exc_info=True
                )


def register_scheme(name: str, handler_cls: type[Handler]) -> None:
    """Register a scheme handler programmatically."""
    SCHEMES[name] = handler_cls


def register_file_type(ext: str, handler_cls: type[Handler]) -> None:
    """Register a file extension handler programmatically."""
    FILE_TYPES[ext] = handler_cls


def register_plugin(plugin: Plugin) -> None:
    """Register a plugin programmatically (for testing or manual setup)."""
    _register_plugin(plugin)


def get_plugin(name: str) -> Plugin | None:
    """Get a registered plugin by name."""
    _discover()
    return PLUGINS.get(name)


def get_corpus_plugin(corpus_id: str) -> Plugin | None:
    """Get the plugin responsible for a corpus."""
    _discover()
    return CORPUS_PLUGINS.get(corpus_id)


def list_plugins() -> list[Plugin]:
    """List all registered plugins."""
    _discover()
    return list(PLUGINS.values())


def resolve(scheme: str, path: str) -> Handler:
    """Return the appropriate handler instance for a scheme + path.

    For ``file:`` scheme, dispatches by file extension.
    For other schemes, dispatches by scheme name.

    Raises:
        PrecisError: If no handler is found for the scheme or file type.
    """
    _discover()

    if scheme == "file":
        ext = Path(path).suffix.lower()
        handler_cls = FILE_TYPES.get(ext)
        if not handler_cls:
            supported = ", ".join(sorted(FILE_TYPES.keys())) or "(none)"
            raise PrecisError(
                f"No handler for {ext} files.\nSupported extensions: {supported}"
            )
        return handler_cls()

    handler_cls = SCHEMES.get(scheme)
    if not handler_cls:
        supported = ", ".join(sorted(SCHEMES.keys())) or "(none)"
        raise PrecisError(
            f"Unknown scheme: {scheme}:\nSupported schemes: file, {supported}"
        )
    return handler_cls()
