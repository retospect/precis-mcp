"""``get(kind='finding', view='judgments')`` — what the refine judges did to
a claim hub's edges, and why — plus the compact counts the default views
carry (``view='evidence'``'s one summary line, the fisheye posture header's
token).

The data is ``meta.reground_log`` (:func:`~precis.taproot.hub.
reground_log_entry`, newest 200) and the two judge memos
(:func:`~precis.taproot.hub.reground_counts`). The default views only ever
show counts; this view is the on-demand detail, newest first, 50 entries a
page.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.response import Response
from precis.store.types import Ref
from precis.taproot.hub import (
    META_REGROUND_LOG,
    cap_removed_meta,
    reground_counts,
)

if TYPE_CHECKING:
    from precis.store import Store

#: Entries per ``view='judgments'`` page.
JUDGMENTS_PAGE_SIZE = 50

#: Optional log-entry keys naming the LLM call that produced a verdict
#: (written by a pass that can reach its ``llm_call_log`` id / request hash).
_CALL_KEYS = ("llm_call_id", "llm_request_hash")


def judging_summary(meta: dict[str, Any] | None) -> str | None:
    """The one summary line ``view='evidence'`` appends under its supporter
    count — ``judging: 37 judged · 2 withheld · 1 removed — view='judgments'
    for detail`` — with zero counts omitted. ``None`` when all three are zero,
    so an untouched hub's evidence view is unchanged."""
    judged, withheld, removed = reground_counts(meta)
    parts = [
        f"{n} {label}"
        for n, label in (
            (judged, "judged"),
            (withheld, "withheld"),
            (removed, "removed"),
        )
        if n
    ]
    if not parts:
        return None
    return f"judging: {' · '.join(parts)} — view='judgments' for detail"


def posture_suffix(meta: dict[str, Any] | None) -> str:
    """The `` · withheld 2 · removed 1`` token the fisheye posture header
    gains, only for the nonzero counts. ``judged`` never shows there. ``""``
    for an untouched hub, so its header is byte-identical to before."""
    _judged, withheld, removed = reground_counts(meta)
    return "".join(
        f" · {label} {n}"
        for n, label in ((withheld, "withheld"), (removed, "removed"))
        if n
    )


def _flag(value: object) -> str:
    return "—" if value is None else str(value).lower()


def render_judgments_view(
    store: Store, ref: Ref, *, page: int | None = None
) -> Response:
    """Render ``view='judgments'``: the hub's ``reground_log`` entries, newest
    first — action, verdict, source, the judge's ``same_setup`` / ``primary``
    / ``terminal`` and both setup texts in full, the reason, the LLM call
    reference when one was stored, and the removed link's own ``meta`` for a
    removal. ``page`` (1-based, from ``args={'page': N}``) windows the list
    :data:`JUDGMENTS_PAGE_SIZE` at a time."""
    meta = ref.meta or {}
    entries = [e for e in (meta.get(META_REGROUND_LOG) or []) if isinstance(e, dict)]
    entries.sort(key=lambda e: str(e.get("at") or ""), reverse=True)
    judged, withheld, removed = reground_counts(meta)

    lines = [
        f"# judgments for finding {ref.id}",
        "",
        ref.title,
        "",
        f"judged {judged} · withheld {withheld} · removed {removed}",
    ]
    if not entries:
        lines += ["", "(no judgments logged yet)"]
        return Response(body="\n".join(lines))

    try:
        pg = max(1, int(page or 1))
    except (TypeError, ValueError) as err:
        raise BadInput(
            f"page must be a positive integer, got {page!r}",
            next="get(kind='finding', id=<id>, view='judgments', args={'page': 2})",
        ) from err
    lo = (pg - 1) * JUDGMENTS_PAGE_SIZE
    shown = entries[lo : lo + JUDGMENTS_PAGE_SIZE]
    if not shown:
        lines += ["", f"(page {pg} is past the end — {len(entries)} logged entries)"]
        return Response(body="\n".join(lines))

    src_ids = [int(e["src_ref_id"]) for e in shown if e.get("src_ref_id") is not None]
    keys = store.ref_cite_keys_bulk(src_ids) if src_ids else {}
    lines += [
        "",
        f"{len(entries)} logged entries, newest first "
        f"(showing {lo + 1}–{lo + len(shown)}).",
    ]
    for i, e in enumerate(shown, start=lo + 1):
        sid = e.get("src_ref_id")
        cite = (keys.get(int(sid)) or []) if sid is not None else []
        source = (cite[0] if cite else f"ref:{sid}") if sid is not None else "—"
        lines += [
            "",
            f"## {i}. {e.get('action') or '—'} · {e.get('verdict') or '—'} · "
            f"{str(e.get('at') or '—')[:16]}",
            f"source: {source} · edge {e.get('edge') or '—'} · relation "
            f"{e.get('relation') or '—'}",
            f"same_setup: {_flag(e.get('same_setup'))} · primary: "
            f"{_flag(e.get('primary'))} · terminal: {_flag(e.get('terminal'))}",
            f"reason: {e.get('reason') or '—'}",
        ]
        for key in ("claim_setup", "passage_setup"):
            if e.get(key):
                lines.append(f"{key}: {e[key]}")
        call = [f"{k}={e[k]}" for k in _CALL_KEYS if e.get(k)]
        if call:
            lines.append("llm: " + " ".join(call))
        removed_meta = cap_removed_meta(e.get("removed_meta"))
        if removed_meta:
            lines.append(
                "removed link meta: "
                + json.dumps(removed_meta, sort_keys=True, default=str)
            )
    more = len(entries) - lo - len(shown)
    if more > 0:
        lines += [
            "",
            f"… {more} older entries — get(kind='finding', id={ref.id}, "
            f"view='judgments', args={{'page': {pg + 1}}})",
        ]
    return Response(body="\n".join(lines))
