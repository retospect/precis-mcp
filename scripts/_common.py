"""Shared helpers for `precis-mcp/scripts/` utilities.

Each `paper-*` shell wrapper re-exec's itself with `uv run --project=<pkg>`,
so by the time these helpers run, the `precis` package and its `[paper]`
extras (acatome-extract, sentence-transformers, …) are importable.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.config import PrecisConfig
    from precis.embedder import Embedder

# Default location of the inbox watched by `paper-monitor-ingest-dir`.
# Override with `--dir` on the command line.
DEFAULT_INGEST_DIR = Path(
    "/Users/bots/Documents/openclaw-cluster/paper-ingest"
)

# Default DSN baked into the wrapper scripts; overridable via
# `PRECIS_DATABASE_URL` in the environment. Matches the canonical
# precis-mcp database configured in `~/.codeium/windsurf/mcp_config.json`.
DEFAULT_DSN = "postgresql://acatome:acatome@127.0.0.1:5432/precis"


def open_store() -> tuple[Any, "PrecisConfig"]:
    """Connect to the precis store. Returns ``(store, cfg)``.

    Caller is responsible for `store.close()`.
    """
    from precis.config import load_config
    from precis.store import Store

    cfg = load_config()
    dsn = cfg.database_url or os.environ.get("PRECIS_DATABASE_URL") or DEFAULT_DSN
    store = Store.connect(dsn)
    return store, cfg


def make_embedder_for(store: Any, cfg: "PrecisConfig") -> "Embedder":
    """Build the active embedder, sized to the store's vector dim."""
    from precis.embedder import make_embedder

    return make_embedder(cfg.embedder, dim=store.embedding_dim())
