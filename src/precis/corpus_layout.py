"""Canonical on-disk layout of the ingested-PDF corpus — one definition.

A held paper's PDF lives at ``<root>/<letter>/<cite_key>.pdf`` where
``letter`` is the lower-cased first ASCII-alnum char of the cite_key (else
``_``), the layout described in ``docs/design/pip-merge.md`` and laid down
by ``precis watch``. This convention used to be re-derived in three places
(``cli.watch``, ``ingest.remediate``, the web PDF resolver); they now all
call :func:`corpus_pdf_dest` so the shard math lives once.

Also home to the two environment-derived facts the corpus-presence pass
needs and which had no pure-``precis`` home before: the ``os.pathsep``-list
of corpus roots (:func:`corpus_roots_from_env`, mirroring
``precis_web.config.WebConfig.corpus_dirs``) and this node's stable
identity (:func:`host_name`, the same ``PRECIS_HOST_NAME`` / hostname pair
``worker_logs`` + ``host_heartbeat`` key on).
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

#: Default corpus root when ``PRECIS_CORPUS_DIR`` is unset — matches the
#: ``precis watch`` fallback and ``precis_web.config._DEFAULT_CORPUS``.
DEFAULT_CORPUS = Path.home() / "work" / "corpus"


def corpus_pdf_dest(cite_key: str, corpus_dir: Path, *, suffix: str = ".pdf") -> Path:
    """Canonical path for ``cite_key`` under ``corpus_dir``:
    ``<corpus_dir>/<letter>/<cite_key><suffix>``.

    The letter shard is the lower-case first character of ``cite_key``, or
    ``_`` when it isn't ASCII-alphanumeric. Pure path math (no FS reads, no
    move) so callers can probe existence before deciding where a PDF should
    land.
    """
    letter = cite_key[0].lower() if cite_key and cite_key[0].isalnum() else "_"
    return Path(corpus_dir) / letter / f"{cite_key}{suffix}"


def corpus_roots_from_env(env: dict[str, str] | None = None) -> tuple[Path, ...]:
    """Every corpus root to search, primary first — the pure-``precis``
    twin of ``WebConfig.corpus_dirs`` for the worker passes.

    ``PRECIS_CORPUS_DIR`` may name a single root or an ``os.pathsep``-list
    (per-host NFS mounts differ, ADR 0029). Falls back to
    :data:`DEFAULT_CORPUS` when unset so a bare install still resolves.
    """
    src = os.environ if env is None else env
    raw = src.get("PRECIS_CORPUS_DIR")
    roots = [
        Path(p).expanduser()
        for p in (raw.split(os.pathsep) if raw else [])
        if p.strip()
    ]
    return tuple(roots) if roots else (DEFAULT_CORPUS,)


def rebase_onto_local(stored: str, corpus_dirs: tuple[Path, ...]) -> Path | None:
    """Rebase an absolute ``storage_path`` onto this node's own NAS mount.

    The corpus lives on one shared NFS export mounted at a *different*
    prefix per OS (ADR 0029): the Macs see ``/opt/nas/botshome/papers/…``,
    the Linux node ``/nas/botshome/papers/…``. A ``storage_path`` written
    by another host is therefore a valid path on the *wrong* prefix here.
    We split on the common ``/papers/`` pivot and re-anchor the suffix under
    each configured root's own ``papers`` dir, so a Mac-authored path still
    resolves on the Linux node (and vice-versa) with no per-host rewrite.
    """
    marker = "/papers/"
    idx = stored.rfind(marker)
    if idx == -1:
        return None
    suffix = stored[idx + len(marker) :]  # e.g. "corpus/i/foo.pdf"
    for root in corpus_dirs:
        papers = root.parent if root.name in ("corpus", "corpus_pres") else root
        cand = papers / suffix
        if cand.is_file():
            return cand
    return None


def host_name(env: dict[str, str] | None = None) -> str:
    """This node's stable identity: ``PRECIS_HOST_NAME`` or the hostname.

    The same pair ``worker_logs.host`` and ``host_heartbeat.host`` key on
    (see ``utils.db_log_handler._resolve_host_name``), so a
    ``pdf_locations`` row lines up with the node's other telemetry.
    """
    src = os.environ if env is None else env
    return src.get("PRECIS_HOST_NAME") or socket.gethostname()


__all__ = [
    "DEFAULT_CORPUS",
    "corpus_pdf_dest",
    "corpus_roots_from_env",
    "host_name",
    "rebase_onto_local",
]
