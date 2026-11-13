"""``get(kind='memory', id=N, view='considerations')`` — the scope's
consideration ledger (spec me626852, gr662069).

Renders the memory's ``considered`` edges in three cohorts — current
verdicts, the stale ``REOPEN`` queue (a material scope/source change since
judgment; reconsider by re-judging through ``rel='considered'`` or
withdrawing the edge), and ``depth='none'`` acquisition markers — followed
by the append-only judgment history (every run's verdict; the edge keeps
only the latest).
"""

from __future__ import annotations

from typing import Any

from precis.response import Response
from precis.store import Store
from precis.store.types import Ref
from precis.utils import handle_registry

_HISTORY_SHOWN = 10


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


def render_considerations(store: Store, ref: Ref) -> Response:
    """The scope ledger: current verdicts, the reconsider queue, the
    needs-text cohort, then the judgment history."""
    verdicts = store.scoped_considerations(ref.id)
    handle = _handle("memory", ref.id)
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
            f"  link(kind='memory', id={handle}, target='<source>', "
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
            lines = [
                f"- {v.verdict} · {_handle(v.dst_kind, v.dst_ref_id)} "
                f"{titles.get(v.dst_ref_id) or ''}".rstrip()
                + f"\n  REOPEN — {v.stale} changed since the judgment; "
                "re-judge it through rel='considered', or "
                "link(mode='remove') to withdraw"
                for v in stale
            ]
            sections.append(
                f"\nReconsider ({len(stale)}) — material change since "
                "judgment:\n" + "\n".join(lines)
            )

    history = store.consideration_history(scope_ref_id=ref.id, limit=_HISTORY_SHOWN)
    if history:
        rows = []
        for j in history:
            src_handle = _handle(j["source_kind"], j["source_ref_id"])
            provenance = ", ".join(
                f"{k}={j[k]}" for k in ("actor", "model", "run") if j.get(k)
            )
            stamp = j["checked_at"].isoformat() if j.get("checked_at") else "?"
            rows.append(
                f"- {stamp} {j['verdict']} · {src_handle} "
                f"[{j['reading_depth']}]"
                + (f" ({provenance})" if provenance else "")
                + f"\n  {j['reason']}"
            )
        sections.append(
            f"\nJudgment history (latest {len(history)} — every judgment "
            "is kept; the edge shows only the newest):\n" + "\n".join(rows)
        )

    return Response(body="\n".join(sections))
