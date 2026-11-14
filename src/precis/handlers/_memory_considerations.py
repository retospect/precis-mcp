"""``get(id=N, view='considerations')`` — a scope's consideration ledger
(spec me626852, gr662069), served by the kinds that can scope a verdict
(:data:`precis.store._consideration_ops.CONSIDERATION_LEDGER_KINDS` —
memory and quest today).

Renders the scope's ``considered`` edges in three cohorts — current
verdicts, the stale ``REOPEN`` queue (a material scope/source change since
judgment, or an ``edge-lost`` judgment whose ``considered`` edge died in a
chunk-replacement cascade; reconsider by re-judging through
``rel='considered'`` or withdrawing the edge), and ``depth='none'``
acquisition markers — followed by the append-only judgment history (every
run's verdict; the edge keeps only the latest; a door-withdrawn judgment
shows its ``withdrawn`` stamp). The history pages: ``args={'limit': N,
'offset': M}`` windows it, and every row shows its recorded evidence —
passages, prompt and the revision pins it was judged against.
"""

from __future__ import annotations

from typing import Any

from precis.response import Response
from precis.store import Store
from precis.store.types import Ref
from precis.utils import handle_registry

_HISTORY_PAGE_DEFAULT = 10
_HISTORY_PAGE_MAX = 100


def _handle(kind: str, ref_id: int) -> str:
    return handle_registry.try_format(kind, ref_id) or f"{kind}:{ref_id}"


def _titles(store: Store, ref_ids: list[int]) -> dict[int, str]:
    if not ref_ids:
        return {}
    with store.pool.connection() as c:
        rows = c.execute(
            "SELECT ref_id, title FROM refs WHERE ref_id = ANY(%s)",
            (ref_ids,),
        ).fetchall()
    return {int(r[0]): str(r[1] or "") for r in rows}


def _verdict_line(v: Any, titles: dict[int, str]) -> str:
    handle = _handle(v.dst_kind, v.dst_ref_id)
    title = titles.get(v.dst_ref_id) or ""
    head = f"- {v.verdict} · {handle} {title}".rstrip()
    checked = f"checked {v.checked_at}" if v.checked_at else "checked (unstamped)"
    depth_note = " — acquisition state, not rejection" if v.depth == "none" else ""
    n = (
        f" · {v.judgments} judgment{'s' if v.judgments != 1 else ''}"
        if v.judgments
        else ""
    )
    return f"{head}\n  {checked} at {v.depth or '?'} depth{depth_note}{n}\n  {v.reason}"


def _history_row(j: dict[str, Any]) -> str:
    """One judgment-history row with its recorded evidence: provenance,
    passages, prompt and the revision pins (gr662069 review — the audit
    trail is only auditable if the view can read it back)."""
    src_handle = _handle(j["source_kind"], j["source_ref_id"])
    provenance = ", ".join(f"{k}={j[k]}" for k in ("actor", "model", "run") if j.get(k))
    stamp = j["checked_at"].isoformat() if j.get("checked_at") else "?"
    withdrawn = (
        f" · withdrawn {j['withdrawn_at'].isoformat()}" if j.get("withdrawn_at") else ""
    )
    lines = [
        f"- {stamp} {j['verdict']} · {src_handle} "
        f"[{j['reading_depth']}]{withdrawn}"
        + (f" ({provenance})" if provenance else "")
        + f"\n  {j['reason']}"
    ]
    pins = []
    if j.get("scope_sha") or j.get("source_sha"):
        pins.append(f"scope_sha={j.get('scope_sha')} source_sha={j.get('source_sha')}")
    passages = j.get("passages") or []
    if passages:
        pins.append(f"passages={passages}")
    if j.get("prompt"):
        pins.append(f"prompt={j['prompt']!r}")
    if pins:
        lines.append("  " + " · ".join(pins))
    return "\n".join(lines)


def _stale_line(v: Any, titles: dict[int, str]) -> str:
    head = (
        f"- {v.verdict} · {_handle(v.dst_kind, v.dst_ref_id)} "
        f"{titles.get(v.dst_ref_id) or ''}".rstrip()
    )
    if v.stale == "edge-lost":
        # The edge carrying the verdict died with its chunk (a routine
        # re-ingest cascade or graph maintenance — never a withdrawal:
        # a door delete stamps withdrawn_at and leaves this queue).
        note = (
            "REOPEN — edge-lost: the considered edge carrying this "
            "verdict was deleted by a chunk replacement or graph "
            "maintenance, not withdrawn; re-judge through "
            "rel='considered' to restore it"
        )
    else:
        note = (
            f"REOPEN — {v.stale} changed since the judgment; re-judge "
            "it through rel='considered', or link(mode='remove') to "
            "withdraw"
        )
    return f"{head}\n  {note}"


def render_considerations(
    store: Store,
    ref: Ref,
    *,
    history_limit: int = _HISTORY_PAGE_DEFAULT,
    history_offset: int = 0,
) -> Response:
    """The scope ledger: current verdicts, the reconsider queue, the
    needs-text cohort, then the judgment history (one page)."""
    verdicts = store.scoped_considerations(ref.id)
    handle = _handle(ref.kind, ref.id)
    titles = _titles(store, sorted({v.dst_ref_id for v in verdicts}))
    scope_title = ref.title or ""
    header = f"Considerations for {handle} — {scope_title}".rstrip(" —")

    current = [v for v in verdicts if v.valid and v.depth != "none"]
    stale = [v for v in verdicts if v.stale is not None]
    needs_text = [v for v in verdicts if v.valid and v.depth == "none"]

    sections = [header]
    if not verdicts:
        sections.append(
            "\nNo considered verdicts yet. Record one:\n"
            f"  link(kind={ref.kind!r}, id={handle}, target='<source>', "
            "rel='considered', reason='why it does (not) fit', "
            "meta={'verdict': 'useful|not-useful|uncertain', "
            "'depth': 'abstract'})"
        )
    else:
        if current:
            lines = [
                _verdict_line(v, titles)
                for v in sorted(current, key=lambda v: v.verdict)
            ]
            sections.append(
                f"\nCurrent verdicts ({len(current)}):\n" + "\n".join(lines)
            )
        if needs_text:
            lines = [_verdict_line(v, titles) for v in needs_text]
            sections.append(
                f"\nAwaiting text ({len(needs_text)}) — acquisition state, "
                "never exclusions:\n" + "\n".join(lines)
            )
        if stale:
            lines = [_stale_line(v, titles) for v in stale]
            sections.append(
                f"\nReconsider ({len(stale)}) — reopened for review:\n"
                + "\n".join(lines)
            )

    total = store.consideration_history_count(scope_ref_id=ref.id)
    if total:
        limit = max(1, min(int(history_limit), _HISTORY_PAGE_MAX))
        offset = max(0, int(history_offset))
        history = store.consideration_history(
            scope_ref_id=ref.id, limit=limit, offset=offset
        )
        first = offset + 1
        last = offset + len(history)
        window = f"{first}–{last} of {total}"
        if total > len(history):
            window += (
                f" — page with get(kind={ref.kind!r}, id={handle}, "
                "view='considerations', args={'limit': <n>, 'offset': <m>})"
            )
        sections.append(
            f"\nJudgment history ({window} — every judgment is kept; "
            "the edge shows only the newest):\n"
            + "\n".join(_history_row(j) for j in history)
        )

    return Response(body="\n".join(sections))
