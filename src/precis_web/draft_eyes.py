"""The draft reader's hand-driven working set (ADR 0051 §6, Level-2 by hand).

The planner and dreams place **eyes** automatically; here the *author* does it in
the reader. Two markers per paragraph:

- **🖊 pen** (big) — "edit here": the chunk goes on the edit job's
  *edit-these-at-a-minimum* hint list, and (auto) gets an eye so you always see
  what you're changing.
- **👁 eye** (small) — "see here": the chunk (and, via *around here*, its whole
  fisheye — reading-order neighbourhood **+ the ring targets promoted to real
  eyes**: cited papers, linked notes) becomes read-only grounding.

The set is **sticky with a TTL** — persisted on the draft ref's ``meta`` so it
survives reload and builds across a session, but expiring after
``PRECIS_DRAFT_EYES_TTL_HOURS`` (default 24) so a stale set doesn't haunt a later
edit. On submit the reader copies it onto the change-request todo as
``meta.working_set = {eyes, edit_hint}``; the planner tick renders the whole set
(``workers.planner_prompt._m_fisheye`` → ``render_working_set``) instead of the
single-anchor fisheye.

Everything is addressed by universal handles (ADR 0036): draft chunks as
``dc<id>`` (what ``render_working_set``/``render_eye`` parse), ring targets as
``pa<id>`` / ``me<id>`` / ….
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from precis.utils import handle_registry

#: The ref-meta key the sticky set lives under.
META_KEY = "reader_working_set"

#: Default eye extents by target class — a draft chunk / note gets the full
#: fisheye+1hop; a cited paper gets its cluster *map* (summary) so a big paper
#: doesn't flood the context. (A per-eye extent picker is a later refinement.)
_DEFAULT_DC_EXTENT = "fisheye+1hop"
_DEFAULT_NOTE_EXTENT = "fisheye+1hop"
_DEFAULT_DOC_EXTENT = "summary"

#: Doc kinds whose eye is a cluster map (see ``utils.eye_render._DOC_KINDS``).
_DOC_KINDS = frozenset({"paper", "patent", "web", "datasheet", "cfp"})


def _ttl_hours() -> float:
    raw = os.environ.get("PRECIS_DRAFT_EYES_TTL_HOURS")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return 24.0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _expired(updated_at: str | None) -> bool:
    """True when the set is older than the TTL (or unparseable → treat as
    expired). TTL ``0`` means never-sticky (always expired)."""
    ttl = _ttl_hours()
    if ttl <= 0 or not updated_at:
        return True
    try:
        age = datetime.now(UTC) - datetime.fromisoformat(updated_at)
    except ValueError:
        return True
    return age.total_seconds() > ttl * 3600.0


def _empty() -> dict[str, Any]:
    return {"pens": [], "eyes": {}, "updated_at": None}


def load_marks(store: Any, ref_id: int) -> dict[str, Any]:
    """The live (non-expired) working set for a draft, or an empty one. Shape:
    ``{"pens": [dc…], "eyes": {handle: extent, …}, "updated_at": iso|None}``."""
    ref = store.fetch_refs_by_ids([ref_id]).get(ref_id)
    raw = (getattr(ref, "meta", None) or {}).get(META_KEY) if ref is not None else None
    if not isinstance(raw, dict) or _expired(raw.get("updated_at")):
        return _empty()
    pens = [str(h) for h in (raw.get("pens") or [])]
    eyes_raw = raw.get("eyes") or {}
    eyes = (
        {str(h): str(e) for h, e in eyes_raw.items()}
        if isinstance(eyes_raw, dict)
        else {}
    )
    return {"pens": pens, "eyes": eyes, "updated_at": raw.get("updated_at")}


def save_marks(store: Any, ref_id: int, marks: dict[str, Any]) -> dict[str, Any]:
    """Persist ``marks`` (stamping ``updated_at``) and return the stored form."""
    stored = {
        "pens": list(dict.fromkeys(marks.get("pens") or [])),  # dedup, keep order
        "eyes": dict(marks.get("eyes") or {}),
        "updated_at": _now_iso(),
    }
    store.stamp_ref_meta(ref_id, {META_KEY: stored})
    return stored


def clear_marks(store: Any, ref_id: int) -> dict[str, Any]:
    """Wipe the sticky set (a fresh empty one, stamped now)."""
    return save_marks(store, ref_id, _empty())


def toggle_pen(marks: dict[str, Any], dc: str, *, on: bool | None = None) -> None:
    """Toggle (or set) a pen on a draft chunk. Penning auto-opens its eye; the
    eye is left in place on un-pen (it's harmless context — remove it with the
    eye toggle)."""
    pens: list[str] = marks["pens"]
    want = (dc not in pens) if on is None else on
    if want:
        if dc not in pens:
            pens.append(dc)
        marks["eyes"].setdefault(dc, _DEFAULT_DC_EXTENT)
    elif dc in pens:
        pens.remove(dc)


def toggle_eye(
    marks: dict[str, Any],
    handle: str,
    *,
    on: bool | None = None,
    extent: str | None = None,
) -> None:
    """Toggle (or set) an eye on any handle. Un-eyeing a penned chunk also drops
    the pen (you can't edit-hint what you're no longer looking at)."""
    eyes: dict[str, str] = marks["eyes"]
    want = (handle not in eyes) if on is None else on
    if want:
        eyes[handle] = extent or eyes.get(handle) or _default_extent(handle)
    else:
        eyes.pop(handle, None)
        if handle in marks["pens"]:
            marks["pens"].remove(handle)


def _default_extent(handle: str) -> str:
    parsed = handle_registry.parse(handle)
    if parsed is None:
        return _DEFAULT_DC_EXTENT
    kind = parsed[0]
    if kind in _DOC_KINDS:
        return _DEFAULT_DOC_EXTENT
    return _DEFAULT_DC_EXTENT if kind in ("draft", "plan") else _DEFAULT_NOTE_EXTENT


def expand_around(
    store: Any, ref_id: int, dc_handles: list[str], marks: dict[str, Any]
) -> None:
    """*Around here*: for each selected draft chunk add an eye on it **and
    promote its reference ring to real eyes** — cited papers (as their cluster
    map), cross-refs, and linked notes/memories all become eyes so the edit sees
    what the section points at. Best-effort: a chunk that won't resolve is
    skipped, never fatal."""
    from precis.utils.refeye import collect_ring

    for dc in dc_handles:
        marks["eyes"].setdefault(dc, _DEFAULT_DC_EXTENT)
        try:
            target = store.get_draft_chunk(dc, kind="draft")
            if target is None:
                continue
            chunks = store.reading_order(target.ref_id, kind="draft")
            ring = collect_ring(store, target, chunks)
        except Exception:
            continue
        ring_ids = {rid for group in ring.values() for rid, _label in group}
        if not ring_ids:
            continue
        refs = store.fetch_refs_by_ids(list(ring_ids))
        for rid, r in refs.items():
            if getattr(r, "deleted_at", None) is not None:
                continue
            kind = getattr(r, "kind", None)
            handle = handle_registry.try_format(kind, rid) if kind else None
            if handle and handle not in marks["eyes"]:
                marks["eyes"][handle] = _default_extent(handle)


def to_working_set_meta(marks: dict[str, Any]) -> dict[str, Any]:
    """The change-request payload: ``{eyes: [{handle, extent}], edit_hint: [dc…]}``
    — the shape ``planner_prompt._m_fisheye`` reads."""
    return {
        "eyes": [
            {"handle": h, "extent": e} for h, e in (marks.get("eyes") or {}).items()
        ],
        "edit_hint": list(marks.get("pens") or []),
    }
