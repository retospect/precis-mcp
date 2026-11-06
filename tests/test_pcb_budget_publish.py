"""AC4 of pcb-design-flow: ``workspace.budget.publish`` republishes budget
tables into a draft (figure goes stale and re-renders) and writes each
rail's ``est_current_a`` onto the pcb's nets -- and a bad call writes nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.draft import DraftHandler
from precis.render.figure import needs_render, render_figure_chunk
from precis.workspace.budget import publish

_T1 = "Current by rail"
_T2 = "Night hold"
_OLD1 = {"header": ["rail", "mA"], "rows": [["VBAT", 100]]}
_OLD2 = {"header": ["mode", "uA"], "rows": [["sleep", 20]]}
_NEW1 = {"header": ["rail", "mA"], "rows": [["VBAT", 420]]}
_NEW2 = {"header": ["mode", "uA"], "rows": [["sleep", 35]]}
_SRC = "open(out, 'wb').write(b'\\x89PNG\\r\\n\\x1a\\n' + bytes(len(data['tables'])))\n"


def _tables(hub: Hub, ref_id: int) -> dict[Any, Any]:
    return {
        (c.meta or {}).get("caption"): (c.meta or {}).get("table")
        for c in hub.live_store.drafts.reading_order(ref_id)
        if c.chunk_kind == "table"
    }


def _est(hub: Hub, pcb_ref_id: int, net: str) -> float | None:
    with hub.live_store.pool.connection() as conn:
        row = conn.execute(
            "SELECT est_current_a FROM pcb_nets "
            "WHERE ref_id = %s AND name = %s AND retired_at IS NULL",
            (pcb_ref_id, net),
        ).fetchone()
    assert row is not None
    return None if row[0] is None else float(row[0])


@pytest.fixture
def seeded(hub: Hub) -> tuple[Hub, int, int, int]:
    """(hub, draft ref id, pcb ref id, figure chunk id)."""
    d = DraftHandler(hub=hub)
    proj = hub.live_store.insert_ref(kind="todo", slug=None, title="P").id
    d.put(id="bud", title="Budget", project=proj)
    dref = hub.live_store.get_ref(kind="draft", id="bud")
    assert dref is not None
    d.put(id="bud", chunk_kind="table", table=_OLD1, caption=_T1, at={"last": True})
    d.put(id="bud", chunk_kind="table", table=_OLD2, caption=_T2, at={"last": True})
    dcs = [
        c.dc
        for c in hub.live_store.drafts.reading_order(dref.id)
        if c.chunk_kind == "table"
    ]
    d.put(
        id="bud",
        chunk_kind="figure",
        render=_SRC,
        plots=dcs,
        text="Fig 1. Budget.",
        at={"last": True},
    )
    fig = next(
        c
        for c in hub.live_store.drafts.reading_order(dref.id)
        if c.chunk_kind == "figure"
    )
    out = render_figure_chunk(hub.live_store, fig.chunk_id)
    assert out.ok, out.detail
    hub.live_store.drafts.clear_render_pending(fig.chunk_id)

    pref, _created, _counts = hub.live_store.pcb_apply(
        slug="bud-board",
        title="Budget board",
        components=[],
        nets=[{"name": "VBAT"}, {"name": "GND"}],
        connections=[],
    )
    return hub, dref.id, pref.id, fig.chunk_id


def _fig_stale(hub: Hub, fig_id: int) -> bool:
    fig = hub.live_store.drafts.get_draft_chunk(f"dc{fig_id}")
    assert fig is not None
    return needs_render(hub.live_store, fig)


def test_publish_updates_tables_figure_stale_and_net_current(seeded: tuple) -> None:
    hub, dref, pref, fig_id = seeded
    assert not _fig_stale(hub, fig_id)  # baseline: freshly rendered
    assert _est(hub, pref, "VBAT") is None

    res = publish(
        hub,
        draft="bud",
        pcb="bud-board",
        tables={_T1: _NEW1, _T2: _NEW2},
        nets={"VBAT": 0.42},
    )

    assert res["tables"] == {"created": [], "updated": [_T1, _T2]}
    got = _tables(hub, dref)
    assert got[_T1]["rows"] == _NEW1["rows"]
    assert got[_T2]["rows"] == _NEW2["rows"]
    assert _fig_stale(hub, fig_id)  # plots link survived; render key now stale
    assert _est(hub, pref, "VBAT") == pytest.approx(0.42)

    # re-render from the new tables clears the staleness
    assert render_figure_chunk(hub.live_store, fig_id).ok
    hub.live_store.drafts.clear_render_pending(fig_id)
    assert not _fig_stale(hub, fig_id)


def test_publish_creates_missing_table(seeded: tuple) -> None:
    hub, dref, _pref, _fig = seeded
    res = publish(hub, draft="bud", pcb="bud-board", tables={"New": _NEW1}, nets={})
    assert res["tables"] == {"created": ["New"], "updated": []}
    assert _tables(hub, dref)["New"]["rows"] == _NEW1["rows"]


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"nets": {"NOPE": 1.0}}, "NOPE"),
        ({"draft": "no-such-draft"}, "not found"),
        ({"pcb": "no-such-pcb"}, "not found"),
        ({"tables": {_T1: _NEW1, _T2: {"header": ["x"]}}}, "'header' and 'rows'"),
    ],
)
def test_bad_call_writes_nothing(seeded: tuple, kw: dict, match: str) -> None:
    hub, dref, pref, fig_id = seeded
    args: dict[str, Any] = {
        "draft": "bud",
        "pcb": "bud-board",
        "tables": {_T1: _NEW1, _T2: _NEW2},
        "nets": {"VBAT": 0.42},
    }
    args.update(kw)
    with pytest.raises(BadInput, match=match):
        publish(hub, **args)
    got = _tables(hub, dref)
    assert got[_T1]["rows"] == _OLD1["rows"]
    assert got[_T2]["rows"] == _OLD2["rows"]
    assert _est(hub, pref, "VBAT") is None
    assert not _fig_stale(hub, fig_id)
