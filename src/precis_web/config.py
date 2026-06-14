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
    source: str = DEFAULT_SOURCE
    auth_token: str | None = None

    @classmethod
    def from_env(cls) -> WebConfig:
        """Build from ``PRECIS_WEB_*`` (+ ``PRECIS_CORPUS_DIR``) env vars.

        Precedence for the corpus root: ``PRECIS_CORPUS_DIR`` →
        ``~/work/corpus``. Everything else is optional with the
        dataclass defaults.
        """
        corpus = os.environ.get("PRECIS_CORPUS_DIR")
        host = os.environ.get("PRECIS_WEB_HOST", "127.0.0.1")
        port_raw = os.environ.get("PRECIS_WEB_PORT", "9100")
        try:
            port = int(port_raw)
        except ValueError:
            port = 9100
        return cls(
            host=host,
            port=port,
            corpus_dir=Path(corpus).expanduser() if corpus else _DEFAULT_CORPUS,
            source=os.environ.get("PRECIS_SOURCE", DEFAULT_SOURCE),
            auth_token=os.environ.get("PRECIS_WEB_AUTH_TOKEN") or None,
        )
