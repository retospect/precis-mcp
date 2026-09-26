"""gr451046 (second defect) -- the ``## route status: ...`` summary
:meth:`precis.store._pcb_ops.PcbMixin._pcb_board_meta` computes must agree
net-for-net with :meth:`precis.store._pcb_ops.PcbMixin.pcb_route_status`
(the ``view='route-status'`` detail list). The summary used to count
``pcb_routes`` rows directly -- no join, no retired-net filter -- so it (a)
kept rows belonging to RETIRED nets (a generator re-apply produces these
routinely: "60 failed, 59 realized" == 119 rows on a real 62-net board) and
(b) omitted live nets with no route row at all (the sibling counts those as
``'unrouted'``). The fix drives the aggregate from ``pcb_nets`` with a LEFT
JOIN on routes and the same ``retired_at IS NULL`` filter the sibling uses.

Uses the same FAKE-generator injection route as
:mod:`tests.test_pcb_generator_reapply_nets` (monkeypatching
:func:`precis.pcb.generators.expand`) to retire a net through the real
store API -- a changed-params re-apply retires a generator's SOLELY-owned
net (see that module's ``test_solely_owned_net_is_retired_and_recreated``)
-- rather than hand-writing ``retired_at`` via SQL.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest

from precis.pcb.generators import GeneratorExpansion
from precis.store import Store

_GENERATOR_TYPE = "fake_route_status_gen"


def _fake_expansion(
    name: str, params: dict[str, Any], *, version: int = 1
) -> GeneratorExpansion:
    """A minimal expansion: one component ``name`` with one pin, wired to a
    net this generator SOLELY owns (``f"{name}_OWN"``) -- the exact shape
    :mod:`tests.test_pcb_generator_reapply_nets` uses to get a net retired
    (never a foreign instance's net, which the fix protects instead)."""
    own_net = f"{name}_OWN"
    return GeneratorExpansion(
        refdes=name,
        generator=_GENERATOR_TYPE,
        version=version,
        canonical_params=dict(params),
        components=[{"refdes": name, "pins": [{"name": "1"}]}],
        nets=[{"name": own_net}],
        connections=[{"net": own_net, "refdes": name, "pin": "1"}],
        footprints=[],
        features=[],
        ledger={},
    )


@pytest.fixture
def fake_gen(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Registers ``generator='fake_route_status_gen'`` against a
    test-controlled expansion -- set ``state['expansion']`` before
    ``store.pcb_apply(...)``. Monkeypatches :func:`precis.pcb.generators.
    expand` directly, the same injection route
    :mod:`tests.test_pcb_fixed_copper` and :mod:`tests.
    test_pcb_generator_reapply_nets` use."""
    state: dict[str, Any] = {"expansion": None}

    def _expand(generator: str, gen_name: str, params: dict[str, Any]):
        assert generator == _GENERATOR_TYPE, generator
        exp = state["expansion"]
        assert exp is not None, "fake_gen: set state['expansion'] first"
        if callable(exp):
            return exp(gen_name, params)
        return exp

    monkeypatch.setattr("precis.pcb.generators.expand", _expand)
    return state


def _build_design(store: Store, fake_gen: dict[str, Any], slug: str) -> int:
    """A design exercising all three summary shapes at once:

    - ``NET_A``: a live net WITH a ``pcb_routes`` row (status ``'realized'``).
    - ``NET_B``: a live net with NO route row at all (must count as
      ``'unrouted'``, not be omitted).
    - ``GEN1_OWN`` (original net_id): a net that gets RETIRED (via a
      changed-params generator re-apply, the real store path) while still
      carrying its OWN ``pcb_routes`` row (status ``'failed'``) -- the
      "60 failed, 59 realized" / 119-on-62 symptom, one row in isolation.
      Must not appear in the summary at all.
    - ``GEN1_OWN`` (re-expanded net_id): the fresh live net the re-apply
      mints under the same name, with no route row of its own (another
      live-net-with-no-route instance).

    Returns the ref_id.
    """
    fake_gen["expansion"] = _fake_expansion("GEN1", {"a": 1})
    ref, _created, _counts = store.pcb_apply(
        slug=slug,
        title=slug,
        components=[
            {"refdes": "C1", "pins": [{"name": "1"}]},
            {"refdes": "C2", "pins": [{"name": "1"}]},
        ],
        nets=[],
        connections=[
            {"net": "NET_A", "refdes": "C1", "pin": "1"},
            {"net": "NET_B", "refdes": "C2", "pin": "1"},
        ],
        generators=[{"name": "GEN1", "generator": _GENERATOR_TYPE, "params": {"a": 1}}],
    )
    ref_id = int(ref.id)
    board_id = store.pcb_ensure_board(ref_id)

    # Write routes for NET_A and the ORIGINAL GEN1_OWN before it gets
    # retired below -- NET_B is deliberately left with no route row.
    n = store.pcb_routes_write(
        ref_id,
        board_id,
        {
            "NET_A": {"status": "realized"},
            "GEN1_OWN": {"status": "failed"},
        },
    )
    assert n == 2

    # Changed-params re-apply: GEN1_OWN is solely-owned by GEN1 (no foreign
    # instance joined it), so the real store retires it and mints a fresh
    # net under the same name -- the retired row's own pcb_routes entry
    # (status 'failed', written above) survives untouched, now hanging
    # off a retired net_id.
    fake_gen["expansion"] = _fake_expansion("GEN1", {"a": 2})
    reapplied_ref, _created2, _counts2 = store.pcb_apply(
        slug=slug,
        title=slug,
        components=[],
        nets=[],
        connections=[],
        generators=[{"name": "GEN1", "generator": _GENERATOR_TYPE, "params": {"a": 2}}],
    )
    assert int(reapplied_ref.id) == ref_id
    return ref_id


def _detail_tally(store: Store, ref_id: int) -> tuple[dict[str, int], int]:
    """``(status -> count, total rows)`` from :meth:`Store.pcb_route_status`
    -- the per-net detail list :meth:`Store._pcb_board_meta`'s summary must
    agree with net-for-net."""
    detail = store.pcb_route_status(ref_id)
    return dict(Counter(row["status"] for row in detail)), len(detail)


def test_summary_matches_detail_across_all_three_shapes(
    store: Store, fake_gen: dict[str, Any]
) -> None:
    ref_id = _build_design(store, fake_gen, "route-status-1")

    tally, total = _detail_tally(store, ref_id)
    # By construction: NET_A (realized), NET_B (unrouted, no route row),
    # GEN1_OWN-fresh (unrouted, no route row). The retired GEN1_OWN-original
    # (failed) must NOT show up in either the detail list or the tally.
    assert tally == {"realized": 1, "unrouted": 2}
    assert total == 3

    loaded = store.pcb_load(ref_id)
    summary = loaded["route_status"]

    # The headline invariant: the summary dict must equal the detail
    # list's own per-status tally exactly, not merely agree on the total.
    assert summary == tally
    assert sum(summary.values()) == total

    # pcb_graph drives the same aggregate through the same helper -- must
    # agree too.
    graphed = store.pcb_graph(ref_id)
    assert graphed["route_status"] == tally


def test_retired_net_route_row_excluded_total_equals_live_net_count(
    store: Store, fake_gen: dict[str, Any]
) -> None:
    ref_id = _build_design(store, fake_gen, "route-status-2")

    with store.pool.connection() as conn:
        live_net_count = conn.execute(
            "SELECT count(*) FROM pcb_nets WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
        retired_route_rows = conn.execute(
            "SELECT count(*) FROM pcb_routes rt "
            "JOIN pcb_nets n ON n.net_id = rt.net_id "
            "WHERE n.ref_id = %s AND n.retired_at IS NOT NULL",
            (ref_id,),
        ).fetchone()
    assert live_net_count is not None and retired_route_rows is not None
    assert live_net_count[0] == 3
    # Sanity: the retired net's route row genuinely still exists in
    # pcb_routes -- otherwise this test would not exercise the symptom.
    assert retired_route_rows[0] == 1

    summary = store.pcb_load(ref_id)["route_status"]
    assert sum(summary.values()) == live_net_count[0]
    assert sum(summary.values()) != live_net_count[0] + retired_route_rows[0]


def test_live_net_with_no_route_row_counts_as_unrouted(
    store: Store, fake_gen: dict[str, Any]
) -> None:
    ref_id = _build_design(store, fake_gen, "route-status-3")

    summary = store.pcb_load(ref_id)["route_status"]
    # NET_B never got a pcb_routes row at all -- it must be folded into
    # 'unrouted', not silently dropped from the summary.
    detail = {row["name"]: row["status"] for row in store.pcb_route_status(ref_id)}
    assert detail["NET_B"] == "unrouted"
    assert summary.get("unrouted", 0) >= 1
    # Every live, routeless net (NET_B and the fresh GEN1_OWN) is folded
    # into 'unrouted' -- the count must include both, not just one.
    unrouted_names = {name for name, status in detail.items() if status == "unrouted"}
    assert unrouted_names == {"NET_B", "GEN1_OWN"}
    assert summary["unrouted"] == len(unrouted_names)
