"""Web-layer configuration. Env-driven, frozen, no auth in cut 1.

Distinct from :class:`precis.config.PrecisConfig` (which the runtime
loads for DB / embedder / kinds). This holds only the web-surface
knobs: bind address, the corpus root for PDF streaming, the caller
``source`` stamped onto handler writes, and an *optional* bearer
token (unset = open, the cut-1 default).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: Default corpus root, matching the ``precis watch`` fallback
#: (``cli/watch.py``: ``Path.home() / "work" / "corpus"``).
_DEFAULT_CORPUS = Path.home() / "work" / "corpus"

#: Source identity the web process presents to the handler guards.
#: ``web:*`` is classified as owner by ``precis.handlers._todo_guards``
#: so the owner can edit strategic / tactical tiers the workers can't.
DEFAULT_SOURCE = "web:owner"


@dataclass(frozen=True)
class WebConfig:
    """Frozen web configuration, built from the environment."""

    host: str = "127.0.0.1"
    port: int = 9100
    corpus_dir: Path = _DEFAULT_CORPUS
    #: Additional corpus roots searched after ``corpus_dir`` when
    #: resolving a PDF. Populated from the 2nd+ entries of a
    #: ``os.pathsep``-separated ``PRECIS_CORPUS_DIR``. Empty by default.
    extra_corpus_dirs: tuple[Path, ...] = ()
    source: str = DEFAULT_SOURCE
    auth_token: str | None = None

    @property
    def corpus_dirs(self) -> tuple[Path, ...]:
        """All corpus roots to search, primary first.

        PDFs on the cluster live behind an NFS mount that different
        hosts surface at different paths (``/opt/shared/corpus`` here,
        ``/opt/nas/botshome/papers/corpus`` there). Listing every
        candidate in ``PRECIS_CORPUS_DIR`` (``os.pathsep``-separated)
        lets one web config find the file wherever it's mounted, so a
        per-host mount difference stops being a "PDF not found".
        """
        return (self.corpus_dir, *self.extra_corpus_dirs)

    @classmethod
    def from_env(cls) -> WebConfig:
        """Build from ``PRECIS_WEB_*`` (+ ``PRECIS_CORPUS_DIR``) env vars.

        ``PRECIS_CORPUS_DIR`` may name a single root or an
        ``os.pathsep``-separated list (e.g.
        ``/opt/shared/corpus:/opt/nas/botshome/papers/corpus``); the
        first becomes ``corpus_dir``, the rest ``extra_corpus_dirs``,
        and PDF resolution tries each in order. Falls back to
        ``~/work/corpus``. Everything else is optional.
        """
        raw = os.environ.get("PRECIS_CORPUS_DIR")
        roots = [
            Path(p).expanduser()
            for p in (raw.split(os.pathsep) if raw else [])
            if p.strip()
        ]
        corpus_dir = roots[0] if roots else _DEFAULT_CORPUS
        extra = tuple(roots[1:])
        host = os.environ.get("PRECIS_WEB_HOST", "127.0.0.1")
        port_raw = os.environ.get("PRECIS_WEB_PORT", "9100")
        try:
            port = int(port_raw)
        except ValueError:
            port = 9100
        return cls(
            host=host,
            port=port,
            corpus_dir=corpus_dir,
            extra_corpus_dirs=extra,
            source=os.environ.get("PRECIS_SOURCE", DEFAULT_SOURCE),
            auth_token=os.environ.get("PRECIS_WEB_AUTH_TOKEN") or None,
        )
