"""``get(kind='memory', view='grounding')`` — the claim-level drill-down (gr634490).

The default read's one ``grounding:`` line is the coverage summary; this
view is the evidence behind it: every substantive claim of the latest
audit with its span, kind, effective status (the v5 ``rollup`` verdict when
the audit recorded one), cited evidence and premises, the decisive passage
checked against the live body, the audit history (newest first — old
review acts stay inspectable), known ``disputes`` edges, the exact body
ranges the audit left unchecked, and, when the audit no longer binds, the
drafts and quests that link this memory as the consumers a source change
or edit actually affects. Read-only: displaying an unresolved hypothesis
upgrades nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.response import Response
from precis.utils.handle_registry import try_format

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis.store.types import Ref

#: Claims listed before the visible cap; the cap line names the full count.
_CLAIM_CAP = 40
#: Consumers listed before the visible cap.
_CONSUMER_CAP = 20
#: Dispute edges listed before the visible cap.
_DISPUTE_CAP = 20
#: Unchecked-text ranges listed before the visible cap.
_RANGE_CAP = 20


def _day(at: Any) -> str:
    return at.astimezone().strftime("%Y-%m-%d") if at is not None else "?"


def _current_body(store: Store, ref_id: int) -> str:
    """The memory's live body text — decisive passages and unchecked ranges
    are verified against what the body says now, not what the audit saw."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT coalesce((SELECT string_agg(c.text, E'\\n' "
            "ORDER BY c.ord, c.chunk_id) FROM chunks c WHERE c.ref_id = %s "
            "AND c.chunk_kind = 'memory_body' AND c.ord >= 0 "
            "AND c.retired_at IS NULL), '')",
            (ref_id,),
        ).fetchone()
    return str(row[0]) if row is not None else ""


def render_grounding_view(
    store: Store, ref: Ref, *, audit_id: int | str | None = None
) -> Response:
    """The per-claim grounding drill-down for one live memory.

    ``audit_id`` (``args={'audit': <review_id>}``) re-renders one named
    audit act instead of the latest: the recorded statuses and the
    decisive passage it read, so a stale row stays inspectable rather
    than flattening to a date and a claim count."""
    from precis.graph.garden.grounding import grounding_summary, summary_line

    ref_id = int(ref.id)
    summary = grounding_summary(store, ref_id)
    handle = try_format("memory", ref_id) or str(ref_id)
    if audit_id is not None:
        return _render_audit_view(store, ref_id, handle, summary, audit_id)
    body = _current_body(store, ref_id)
    lines = [f"# {handle} grounding", "", summary_line(summary)]
    if summary.freshness == "none":
        lines += [
            "",
            "no grounding audit on record — the memory-grounding garden pass"
            " writes one; until then every substantive claim here is unchecked.",
        ]
        return Response(body="\n".join(lines))
    lines.append("")
    lines.append(f"## claims ({len(summary.claims)})")
    shown = summary.claims[:_CLAIM_CAP]
    for claim in shown:
        a, b = claim.span
        lines.append(f"[{a}:{b}] {claim.kind}: {claim.text}")
        status = claim.status
        if claim.recorded and claim.recorded != status:
            lines.append(f"  status: {status} (recorded {claim.recorded})")
        else:
            lines.append(f"  status: {status}")
        if claim.evidence:
            lines.append(f"  evidence: {', '.join(claim.evidence)}")
        if claim.premises:
            lines.append(f"  premises: {', '.join(claim.premises)}")
        if claim.stale_pins:
            lines.append(f"  stale pins: {', '.join(claim.stale_pins)}")
        if claim.unpinned:
            lines.append(
                "  unpinned citations: "
                + ", ".join(claim.unpinned)
                + " (did not resolve at audit — no support certified)"
            )
        if claim.reason:
            lines.append(f"  reason: {claim.reason}")
        if claim.rollup:
            lines.append(f"  rollup: {claim.rollup}")
        if claim.passage:
            lines.append(f"  decisive passage: {claim.passage}")
        # The audited span, checked against the live body.
        if body and body[a:b] == claim.text:
            lines.append("  passage: matches the live body at this span")
        elif body:
            lines.append(
                "  passage: span no longer matches the live body (edited since audit)"
            )
    if len(summary.claims) > len(shown):
        lines.append(f"… +{len(summary.claims) - len(shown)} more claims")
    if summary.unaccounted_ranges:
        lines += [
            "",
            f"## unchecked text ({len(summary.unaccounted_ranges)} ranges)",
        ]
        ranges = summary.unaccounted_ranges
        for a, b in ranges[:_RANGE_CAP]:
            excerpt = body[a:b] if 0 <= a <= b <= len(body) else ""
            suffix = f' "{excerpt}"' if excerpt else ""
            lines.append(f"- [{a}:{b}]{suffix}")
        if len(ranges) > _RANGE_CAP:
            lines.append(f"… +{len(ranges) - _RANGE_CAP} more ranges")
    elif summary.truncated:
        lines.append(
            "scan truncated: the audit's count/char bound omitted text —"
            " omitted ranges are unchecked by construction"
        )
    disputes = _known_disputes(store, ref_id)
    if disputes:
        lines += ["", "## known disputes"]
        for kind, cid, title in disputes[:_DISPUTE_CAP]:
            hid = try_format(kind, cid) or str(cid)
            lines.append(f"- {hid} [{kind}] {title}")
        if len(disputes) > _DISPUTE_CAP:
            lines.append(f"… +{len(disputes) - _DISPUTE_CAP} more")
    if summary.history:
        lines += ["", "## audit history (newest first)"]
        for entry in summary.history:
            state = "current" if entry.current else "stale"
            line = (
                f"- review {entry.review_id} · {_day(entry.at)} ·"
                f" {entry.version} · {entry.claims} claims · {state}"
            )
            if not entry.current and entry.stale_pins:
                line += f" (moved: {', '.join(entry.stale_pins[:6])})"
            lines.append(line + f" — inspect: args={{'audit': {entry.review_id}}}")
    if summary.freshness == "stale":
        consumers = _affected_consumers(store, ref_id)
        lines += ["", "## affected consumers"]
        lines.append(
            f"stale pins: {', '.join(summary.stale_pins)} — this memory is"
            " queued for re-audit (stale class); dependent reads below see"
            " the unresolved claims"
        )
        if consumers:
            for kind, cid, title in consumers[:_CONSUMER_CAP]:
                hid = try_format(kind, cid) or str(cid)
                lines.append(f"- {hid} [{kind}] {title}")
            if len(consumers) > _CONSUMER_CAP:
                lines.append(f"… +{len(consumers) - _CONSUMER_CAP} more")
        else:
            lines.append("- none link this memory (drafts/quests only)")
    return Response(body="\n".join(lines))


def _affected_consumers(store: Store, ref_id: int) -> list[tuple[str, int, str]]:
    """Live drafts and quests carrying a dependency link to this memory
    (``cites``/``derived-from``) — the consumers a stale audit actually
    reaches. Other kinds, and arbitrary inbound links, are not
    dependency consumers."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.kind, r.ref_id, left(r.title, 80) FROM links l "
            "JOIN refs r ON r.ref_id = l.src_ref_id "
            "WHERE l.dst_ref_id = %s AND l.relation IN ('cites', 'derived-from') "
            "AND r.kind IN ('draft', 'quest') "
            "AND r.retired_at IS NULL ORDER BY r.ref_id LIMIT %s",
            (ref_id, _CONSUMER_CAP + 1),
        ).fetchall()
    return [(str(kind), int(rid), str(title or "")) for kind, rid, title in rows]


def _render_audit_view(
    store: Store,
    ref_id: int,
    handle: str,
    summary: Any,
    audit_id: int | str,
) -> Response:
    """One named audit act: the recorded judgment, its pins and the
    decisive passages it read — the prior review stays inspectable
    instead of flattening to a date and a count (gr634490)."""
    from precis.errors import BadInput
    from precis.graph.garden.grounding import audit_for_review

    try:
        rid = int(audit_id)
    except (TypeError, ValueError):
        raise BadInput(
            f"audit= must be a review id, got {audit_id!r}",
            next="get(kind='memory', id=N, view='grounding')",
        ) from None
    record = audit_for_review(store, ref_id, rid)
    if record is None:
        raise BadInput(
            f"no grounding audit review {rid} on {handle}",
            next="get(kind='memory', id=N, view='grounding')",
        )
    entry = next((e for e in summary.history if e.review_id == rid), None)
    state = "current" if entry is not None and entry.current else "stale"
    moved = (
        f" — moved pins: {', '.join(entry.stale_pins)}"
        if entry is not None and entry.stale_pins
        else ""
    )
    day = _day(entry.at) if entry is not None else "?"
    lines = [
        f"# {handle} grounding · audit review {rid}",
        "",
        f"recorded {day} · {record.versions.get('prompt', '?')} · "
        f"target sha {record.target_sha[:12]} · now {state}{moved}",
        "",
        "recorded statuses — the judgment the audit wrote, before any later moves:",
    ]
    shown = [c for c in record.claims if c.kind != "nonclaim"][:_CLAIM_CAP]
    for c in shown:
        a, b = c.span
        lines.append(f"[{a}:{b}] {c.kind}: {c.text}")
        rollup = f" (rollup {c.rollup})" if c.rollup else ""
        lines.append(f"  recorded: {c.status}{rollup}")
        if c.reason:
            lines.append(f"  reason: {c.reason}")
        if c.evidence:
            lines.append(f"  evidence: {', '.join(c.evidence)}")
        if c.premises:
            lines.append(f"  premises: {', '.join(c.premises)}")
        pins = [
            f"{h}→{record.pins[h][:12]}"
            for h in (*c.evidence, *c.premises, *c.passages)
            if h in record.pins
        ]
        if pins:
            lines.append(f"  pins: {', '.join(pins)}")
        ppin = [
            f"{h}→{p.get('verdict')}@review {p.get('review_id')}"
            for h, p in record.premise_pins.items()
            if h in c.premises
        ]
        if ppin:
            lines.append(f"  premise judgments: {', '.join(ppin)}")
        passage = (c.support or {}).get("passage") or {}
        if passage.get("chunk_id"):
            lines.append(
                f"  decisive passage: chunk {passage['chunk_id']}"
                f" ord {passage.get('chunk_ord', '?')}"
            )
    rest = sum(1 for c in record.claims if c.kind != "nonclaim") - len(shown)
    if rest > 0:
        lines.append(f"… +{rest} more claims")
    return Response(body="\n".join(lines))


def _known_disputes(store: Store, ref_id: int) -> list[tuple[str, int, str]]:
    """Live ``disputes``-relation edges touching the memory, either
    direction — disagreement already on record, whatever the audit says."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.kind, r.ref_id, left(r.title, 80) FROM links l "
            "JOIN refs r ON r.ref_id = CASE WHEN l.src_ref_id = %s "
            "THEN l.dst_ref_id ELSE l.src_ref_id END "
            "WHERE l.relation = 'disputes' AND r.retired_at IS NULL "
            "AND (l.src_ref_id = %s OR l.dst_ref_id = %s) "
            "ORDER BY l.link_id LIMIT %s",
            (ref_id, ref_id, ref_id, _DISPUTE_CAP + 1),
        ).fetchall()
    return [(str(kind), int(rid), str(title or "")) for kind, rid, title in rows]
