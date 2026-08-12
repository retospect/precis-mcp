"""Persist a autocatpath run artifact onto a `pathway` ref.

Shared by the two write paths so they stay identical:

* the handler's **in-process** `put` (slice 0), and
* the **`autocatpath_explore`** job dispatch (slice 1, runs on the pinned node).

Imports only precis's public Store surface (no autocatpath deps), so it is cheap to
import inside the job dispatcher.
"""

from __future__ import annotations

import logging
import math
from typing import Any

log = logging.getLogger(__name__)

BODY_KIND = "pathway_body"


def _json_finite(obj: Any) -> Any:
    """Recursively replace non-finite floats (NaN/Inf) with None.

    A NaN barrier (EMT can produce one for a toy reaction it has no parameters
    for) is valid Python but **invalid JSON** — psycopg's Jsonb write rejects it
    ("Token NaN is invalid"). NaN here means "no trustworthy value", so null is
    the faithful serialization; the views already render a missing value as n/a.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_finite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_finite(v) for v in obj]
    return obj


def pathway_meta(
    artifact: dict[str, Any], *, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The `refs.meta` payload for a pathway ref: the authoritative config +
    snapshot, the reaction graph, the pooled-uncertainty results, warnings."""
    meta: dict[str, Any] = {
        "content_key": artifact["content_key"],
        "autocatpath_version": artifact["autocatpath_version"],
        "config": artifact["config"],
        "config_snapshot_yaml": artifact["config_snapshot_yaml"],
        "results": artifact["results_json"],
        "graph": artifact["graph_json"],
        "warnings": artifact["warnings"],
        "n_structures": len(artifact["structures_extxyz"]),
        "status": "ready",
    }
    if extra:
        meta.update(extra)
    return _json_finite(meta)


def pathway_title(artifact: dict[str, Any]) -> str:
    r = artifact["results_json"]
    el = artifact["config"].get("slab", {}).get("element", "?")
    return f"{r['substrate']} → {r['target']} on {el}"


def persist_result(
    store: Any,
    ref_id: int,
    artifact: dict[str, Any],
    *,
    pathway_slug: str | None = None,
    ingest: bool = True,
    extra_meta: dict[str, Any] | None = None,
    conn: Any = None,
) -> None:
    """Stamp the pathway ref's meta and (re)write its methods body chunk, and
    (slice 1b) ingest each relaxed intermediate as a `structure` ref linked back
    to the pathway. The ref must already exist. Runs in its own transaction
    unless a `conn` is supplied.

    Structure ingest runs *before* the meta stamp (it opens its own
    transactions via `structure_save`) so the resulting `{state → ref_id}` map
    lands in the same meta. Best-effort — an ingest failure never blocks the
    core result write-back."""
    extra = dict(extra_meta or {})
    if ingest and pathway_slug and artifact.get("structures_extxyz"):
        try:
            from .ingest import ingest_intermediates

            extra["structure_refs"] = ingest_intermediates(
                store,
                ref_id,
                pathway_slug,
                artifact["content_key"],
                artifact["structures_extxyz"],
            )
        except Exception:
            # Native ingest is additive — keep the pathway result regardless —
            # but never silently: a swallowed failure here is exactly how
            # per-state geometry went missing unnoticed (empty structure_refs
            # with no trace). Log loudly so a regression surfaces.
            log.warning(
                "pathway %s: per-state structure ingest failed; "
                "structure_refs left unset",
                pathway_slug,
                exc_info=True,
            )

    meta = pathway_meta(artifact, extra=extra)

    def _do(c: Any) -> None:
        store.stamp_ref_meta(ref_id, meta, conn=c)
        store.replace_body_chunk(
            ref_id, artifact["methods_md"], chunk_kind=BODY_KIND, conn=c
        )

    if conn is not None:
        _do(conn)
    else:
        with store.tx() as c:
            _do(c)
