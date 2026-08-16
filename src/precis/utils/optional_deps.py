"""Helpers for guarding lazily-imported dependency imports inside handlers.

Several cache-backed handlers (web, youtube, perplexity) lazy-import
``httpx`` / ``trafilatura`` / ``youtube_transcript_api`` from inside
``_fetch`` so module import stays cheap. The ``try: import x; except
ImportError: raise Upstream(...)`` boilerplate was duplicated five
times with subtly different hint wording; this module gives them one
shared call site:

    httpx = require_optional("httpx")

The handler keeps the local-binding shape it already had, and the
error surfaces the canonical pip-install hint. Since the 2026-08-16
extras promotion the network-client deps are core, so most call sites
omit ``extra`` — the hint then points at reinstalling ``precis-mcp``
itself (a missing core dep means a broken venv). Pass ``extra`` only
for a dep that still ships in a real ``[project.optional-dependencies]``
group. ``register_optional`` folds in the matching probe entry on
:data:`SkillHandler._OPTIONAL_DEP_PROBES` so adding a new optional
doesn't drift the two lists.
"""

from __future__ import annotations

import importlib
from types import ModuleType

from precis.errors import PrecisError, Upstream


def require_optional(
    module: str,
    *,
    extra: str | None = None,
    error_cls: type[PrecisError] = Upstream,
) -> ModuleType:
    """Import ``module`` or raise a typed error with an install hint.

    Centralises the exact wording — the previous ad-hoc copies in
    ``web.py`` (twice), ``perplexity.py``, and ``youtube.py`` had
    minor drift (extra-name capitalisation, period vs no period,
    trailing whitespace) that made debugging "why is the hint
    different?" a recurring chore.

    Parameters
    ----------
    module:
        Top-level module name to import (e.g. ``"httpx"``,
        ``"trafilatura"``, ``"youtube_transcript_api"``). Use the
        Python import name, not the PyPI distribution name — the
        distinction matters for ``python-epo-ops-client`` →
        ``epo_ops`` etc.
    extra:
        The pyproject ``[project.optional-dependencies]`` group that
        ships the missing dep, surfacing in the recovery hint as
        ``pip install 'precis-mcp[<extra>]'`` — or ``None`` (the
        default) for a core dep, where the hint becomes a reinstall
        of ``precis-mcp`` itself. Never name an extra that no longer
        exists in pyproject; the hint must stay copy-pasteable.
    error_cls:
        The :class:`~precis.errors.PrecisError` subclass to raise when
        the module is missing. Defaults to :class:`Upstream` for
        backwards compatibility, but a *missing local optional
        dependency* is really a "feature unavailable on this
        deployment" condition, not a downstream/network failure — the
        ``web`` kind passes :class:`~precis.errors.Unsupported` so the
        rendered error isn't mislabelled ``[error:Upstream]`` (gripe
        #39241).

    Raises
    ------
    error_cls
        When the module isn't importable. The exception's ``next``
        field carries the canonical pip-install command so an MCP
        client renders an actionable recovery suggestion.
    """
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        hint = (
            f"pip install 'precis-mcp[{extra}]'"
            if extra is not None
            else "pip install --force-reinstall 'precis-mcp'"
        )
        raise error_cls(
            f"{module} is not installed",
            next=hint,
        ) from exc


__all__ = ["require_optional"]
