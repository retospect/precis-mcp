"""Per-point geometry of a surface coverage scan, stored as job chunks.

gr540872 (Reto 2026-10-31, storage option (a)). The engine's
``autocatpath.coverage.scan(keep_geometry=True)`` returns one record per
scan point under ``coverage["geometries"]``: the final relaxed structure
as extxyz, and for a failed point (unconverged, detached, placement
clash) its relax trajectory subsampled to at most 50 frames.
:func:`store_geometries` turns each into ``chunk_kind='scan_geometry'``
chunks on the job ref -- one ``final`` chunk per point, plus one
``frames`` chunk per failed point -- in the shape of the cad ``field``
chunk (``store/_cad_ops.py::put_field``): ``chunks.text`` is a one-line,
float-free summary, the extxyz sits in ``chunk_blobs``.

The ruling's conditions, and where each is held:

1. Out of embedding and search: the kind is in every worker skip list
   (``EmbedHandler`` / ``RakeLemmaHandler.skip_chunk_kinds``,
   ``chunk_keywords`` / ``llm_summarize.SKIP_KINDS``), each chunk carries
   ``meta.no_index``, and the lexically indexed ``text`` is the summary
   line only -- no coordinates reach the tsvector.
2. Read through ``get(kind='job', view='geometry')`` (:func:`list_points`
   / :func:`fetch_point`), never inline: the default job card renders
   ``job_summary`` / ``job_event`` / ``job_result`` only.
3. Append-only: a re-run appends new chunks (the newest wins on fetch);
   nothing here updates or deletes. They live and retire with the job ref.

``ref_body_bytes`` (0266) sums ``octet_length(text)`` only, so a scan's
extxyz never counts toward the job's body size -- just the summary lines.
"""

from __future__ import annotations

import hashlib
from typing import Any

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

#: The chunk kind (migration 0270).
KIND = "scan_geometry"
#: ``chunk_blobs.mime`` of a stored extxyz.
MIME = "chemical/x-extxyz"
#: The two parts a point can carry.
PARTS = ("final", "frames")
#: A concurrent ``job_event`` append can take the same ord; retried like
#: the dispatch context's ``append_chunk`` (claude_inproc._APPEND_RETRIES).
_RETRIES = 3


def _summary(rec: dict[str, Any], part: str) -> str:
    """The chunk's searchable line: ids and integers, never a float."""
    return (
        f"scan geometry {rec['id']} n={int(rec.get('n') or 0)} {part} "
        f"failed={'yes' if rec.get('failed') else 'no'}"
    )


def _rows(records: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any], bytes]]:
    out = []
    for rec in records:
        if not isinstance(rec, dict) or not rec.get("id"):
            continue
        for part in PARTS:
            text = rec.get(part)
            if not isinstance(text, str) or not text.strip():
                continue
            meta = {
                "no_index": "true",
                "scan_geometry": {
                    "point": str(rec["id"]),
                    "part": part,
                    "facet": rec.get("facet"),
                    "adsorbate": rec.get("adsorbate"),
                    "n": rec.get("n"),
                    "counts": rec.get("counts"),
                    "failed": bool(rec.get("failed")),
                    **(
                        {"n_frames_total": rec.get("n_frames_total")}
                        if part == "frames"
                        else {}
                    ),
                },
            }
            out.append((_summary(rec, part), meta, text.encode("utf-8")))
    return out


def store_geometries(store: Any, ref_id: int, records: list[dict[str, Any]]) -> int:
    """Append every record's parts as ``scan_geometry`` chunks + blobs on
    ``ref_id``, in one transaction; returns how many chunks were written.
    Append-only: an existing chunk is never touched."""
    rows = _rows(records)
    if not rows:
        return 0
    for attempt in range(_RETRIES):
        try:
            with store.tx() as conn:
                for text, meta, payload in rows:
                    row = conn.execute(
                        """
                        INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, meta)
                        VALUES (
                            %s, 'agent',
                            (SELECT COALESCE(MAX(ord), -1) + 1 FROM chunks
                              WHERE ref_id = %s AND ord >= 0),
                            %s, %s, %s
                        )
                        RETURNING chunk_id
                        """,
                        (ref_id, ref_id, KIND, text, Jsonb(meta)),
                    ).fetchone()
                    assert row is not None
                    conn.execute(
                        "INSERT INTO chunk_blobs (chunk_id, bytes, mime, sha256, size_bytes) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (
                            int(row[0]),
                            payload,
                            MIME,
                            hashlib.sha256(payload).hexdigest(),
                            len(payload),
                        ),
                    )
            return len(rows)
        except UniqueViolation:
            if attempt == _RETRIES - 1:
                raise
    return 0  # pragma: no cover - the loop returns or raises


def list_points(store: Any, ref_id: int) -> list[dict[str, Any]]:
    """Every stored point on the job, newest record per point, in scan
    order: ``{point, facet, adsorbate, n, counts, failed, parts}`` with
    ``parts`` = ``{part: size_bytes}``. Never reads the blobs' bytes."""
    with store.pool.connection() as conn:
        found = conn.execute(
            "SELECT c.ord, c.meta, b.size_bytes FROM chunks c "
            "JOIN chunk_blobs b ON b.chunk_id = c.chunk_id "
            "WHERE c.ref_id = %s AND c.chunk_kind = %s AND c.retired_at IS NULL "
            "ORDER BY c.ord",
            (ref_id, KIND),
        ).fetchall()
    points: dict[str, dict[str, Any]] = {}
    for _ord, meta, size in found:
        m = (meta or {}).get("scan_geometry") or {}
        pid = str(m.get("point") or "")
        if not pid:
            continue
        part = str(m.get("part") or "final")
        if part == "final" or pid not in points:
            # each record writes final then frames: a newer final is a
            # re-run's record and replaces the older one (re-inserted so
            # the listing follows the newest run's order)
            points.pop(pid, None)
            points[pid] = {
                "point": pid,
                "facet": m.get("facet"),
                "adsorbate": m.get("adsorbate"),
                "n": m.get("n"),
                "counts": m.get("counts"),
                "failed": bool(m.get("failed")),
                "parts": {},
            }
        points[pid]["parts"][part] = int(size)
        if m.get("n_frames_total") is not None:
            points[pid]["n_frames_total"] = m.get("n_frames_total")
    return list(points.values())


def fetch_point(store: Any, ref_id: int, point: str, part: str = "final") -> str | None:
    """The newest stored extxyz of ``point``'s ``part`` on the job, or
    ``None`` when there is none."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT b.bytes FROM chunks c "
            "JOIN chunk_blobs b ON b.chunk_id = c.chunk_id "
            "WHERE c.ref_id = %s AND c.chunk_kind = %s AND c.retired_at IS NULL "
            "AND c.meta->'scan_geometry'->>'point' = %s "
            "AND c.meta->'scan_geometry'->>'part' = %s "
            "ORDER BY c.ord DESC LIMIT 1",
            (ref_id, KIND, point, part),
        ).fetchone()
    return None if row is None else bytes(row[0]).decode("utf-8")
