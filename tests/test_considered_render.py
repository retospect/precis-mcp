"""Rendering of ``considered`` verdict edges (gr662069, spec me626852).

Judged-out candidates render as a counted group that never crowds the
useful rows: a link row shows the composed verdict why
(``not useful (abstract, 2026-11-14): reason``) instead of a bare reason,
and the eye's ``Considered:`` group caps its rows at three with a
``… +N more`` pointer to ``view='considerations'``. Plain ``reason`` edges
render exactly as before.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from precis.dispatch import Hub
from precis.utils.eye_render import render_eye
from precis.utils.link_tag_ops import LinkWhy, link_why, link_why_suffix

# ── link_why / link_why_suffix ──────────────────────────────────────────


def test_plain_reason() -> None:
    assert link_why({"reason": "x"}) == LinkWhy(text="x", verdict=None)


@pytest.mark.parametrize("meta", [{}, None, {"reason": "   "}, {"reason": ""}])
def test_no_why(meta) -> None:
    assert link_why(meta) is None


def test_verdict_composition() -> None:
    why = link_why(
        {
            "verdict": "not-useful",
            "depth": "abstract",
            "checked_at": "2026-11-14T10:00:00+00:00",
            "reason": "lithium only",
        }
    )
    assert why is not None
    assert why.text == "not useful (abstract, 2026-11-14): lithium only"
    assert why.verdict == "not-useful"


def test_depth_none_reads_no_text_and_missing_reason_is_head_only() -> None:
    why = link_why(
        {
            "verdict": "uncertain",
            "depth": "none",
            "checked_at": "2026-11-14T10:00:00+00:00",
        }
    )
    assert why is not None
    assert why.text == "uncertain (no text, 2026-11-14)"
    assert why.verdict == "uncertain"


def test_why_suffix() -> None:
    assert link_why_suffix({"reason": "x"}) == " — x"
    assert link_why_suffix(None) == ""
    assert link_why_suffix({}) == ""


# ── eye / links rendering ───────────────────────────────────────────────


def _mem(store, title: str) -> int:
    return store.insert_ref(kind="memory", slug=None, title=title).id


def _judge(store, scope: int, cand: int, verdict="not-useful", reason="lithium only"):
    store.add_link(
        src_ref_id=scope,
        dst_ref_id=cand,
        relation="considered",
        meta={"verdict": verdict, "depth": "abstract", "reason": reason},
    )


def test_single_considered_edge_in_scope_eye(hub: Hub) -> None:
    store = hub.live_store
    scope = _mem(store, "the scope question")
    cand = _mem(store, "candidate alpha")
    _judge(store, scope, cand, reason="lithium only")

    out = render_eye(store, f"me{scope}", "fisheye+1hop")

    assert "Considered:" in out
    lines = [ln for ln in out.splitlines() if ln.startswith("  considered: ")]
    assert len(lines) == 1, out
    assert f"me{cand}" in lines[0]
    assert " — not useful (abstract, " in lines[0]
    assert "lithium only" in lines[0]
    assert re.search(r"\(abstract, \d{4}-\d{2}-\d{2}\)", lines[0])
    assert datetime.now(UTC).strftime("%Y-%m-%d") in lines[0]


def test_four_considered_edges_cap_at_three_with_more_pointer(hub: Hub) -> None:
    store = hub.live_store
    scope = _mem(store, "the scope question")
    cands = [_mem(store, f"candidate {i}") for i in range(4)]
    for c in cands[:3]:
        _judge(store, scope, c)
    _judge(store, scope, cands[3], verdict="uncertain", reason="unclear fit")

    out = render_eye(store, f"me{scope}", "fisheye+1hop").splitlines()

    head = "  considered (4: 3 not useful · 1 uncertain):"
    assert head in out, "\n".join(out)
    i = out.index(head)
    rows = out[i + 1 : i + 4]
    assert sum(any(f"me{c}" in r for c in cands) for r in rows) == 3, rows
    assert out[i + 4] == (
        f"    … +1 more · get(kind='memory', id='me{scope}', view='considerations')"
    )


def test_candidate_eye_shows_inbound_considered(hub: Hub) -> None:
    store = hub.live_store
    scope = _mem(store, "the scope question")
    cand = _mem(store, "candidate alpha")
    _judge(store, scope, cand)

    out = render_eye(store, f"me{cand}", "fisheye+1hop")

    assert "Considered:" in out
    rows = [ln for ln in out.splitlines() if ln.startswith("  <-considered: ")]
    assert len(rows) == 1, out
    assert f"me{scope}" in rows[0]
    assert " — not useful (" in rows[0]


def test_links_view_renders_composed_why(hub: Hub) -> None:
    from precis.handlers.memory import MemoryHandler

    store = hub.live_store
    scope = _mem(store, "the scope question")
    cand = _mem(store, "candidate alpha")
    _judge(store, scope, cand, reason="lithium only")

    body = MemoryHandler(hub=hub).get(id=scope, view="links").body

    assert "(considered)" in body
    assert "not useful (abstract, " in body
    assert "lithium only" in body


def test_plain_reason_edge_coexists_with_considered(hub: Hub) -> None:
    store = hub.live_store
    scope = _mem(store, "the scope question")
    cand = _mem(store, "candidate alpha")
    other = _mem(store, "a related note")
    _judge(store, scope, cand)
    store.add_link(
        src_ref_id=scope,
        dst_ref_id=other,
        relation="explains",
        meta={"reason": "the mechanism behind the claim"},
    )

    out = render_eye(store, f"me{scope}", "fisheye+1hop")

    assert " — the mechanism behind the claim" in out
    assert " — not useful (abstract, " in out
