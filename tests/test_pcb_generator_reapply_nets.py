"""gr451046 -- a changed-params generator re-apply must never retire a net a
FOREIGN (non-generator) instance is still connected to.

:meth:`precis.store._pcb_ops.PcbMixin._pcb_generator_retire_expansion` used
to retire every net matching ``f"{generator_name}_%"`` on a changed-params
re-apply, while its neighbouring connection cleanup only deleted netconns for
the generator's OWN instances. ``pcb_netconns`` has no ``retired_at`` -- a
connection is live exactly while its instance row and its net row both are
-- so retiring such a net left a foreign component's connection pointing at
a retired net, and the re-expansion then inserted a fresh net row under the
SAME NAME that only the generator's own connections joined. Net identity is
``net_id``; the name being equal is exactly what hid the damage (a route
view then reported the foreign component's net as "realized (dangling net,
nothing to route)").

Uses the same FAKE-generator injection route as
:mod:`tests.test_pcb_fixed_copper` (monkeypatching
:func:`precis.pcb.generators.expand` -- real generators stay untouched).
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.pcb.generators import GeneratorExpansion
from precis.store import Store

_GENERATOR_TYPE = "fake_reapply_gen"


def _fake_expansion(
    name: str,
    params: dict[str, Any],
    *,
    version: int = 1,
    shared_net: str | None = None,
) -> GeneratorExpansion:
    """A minimal expansion: one component ``name`` with two pins -- pin
    ``1`` on a net this generator SOLELY owns (``f"{name}_OWN"``), pin ``2``
    on ``shared_net`` when given (a net a foreign instance may also join)."""
    own_net = f"{name}_OWN"
    nets = [{"name": own_net}]
    connections = [{"net": own_net, "refdes": name, "pin": "1"}]
    if shared_net:
        nets.append({"name": shared_net})
        connections.append({"net": shared_net, "refdes": name, "pin": "2"})
    return GeneratorExpansion(
        refdes=name,
        generator=_GENERATOR_TYPE,
        version=version,
        canonical_params=dict(params),
        components=[{"refdes": name, "pins": [{"name": "1"}, {"name": "2"}]}],
        nets=nets,
        connections=connections,
        footprints=[],
        features=[],
        ledger={},
    )


@pytest.fixture
def fake_gen(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Registers ``generator='fake_reapply_gen'`` against a test-controlled
    expansion -- set ``state['expansion']`` before ``store.pcb_apply(...)``.
    Monkeypatches :func:`precis.pcb.generators.expand` directly, the same
    injection route :mod:`tests.test_pcb_fixed_copper` uses."""
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


def _apply(
    store: Store,
    slug: str,
    *,
    gen_name: str = "GEN1",
    gen_params: dict[str, Any] | None = None,
    components: list[dict[str, Any]] | None = None,
    connections: list[dict[str, Any]] | None = None,
) -> int:
    """One batch: the generator plus optional hand-authored components/
    connections in the SAME apply (mirroring how a foreign instance's wiring
    to a generator-declared net is authored in practice). Returns ref_id."""
    ref, _created, _counts = store.pcb_apply(
        slug=slug,
        title=slug,
        components=list(components or []),
        nets=[],
        connections=list(connections or []),
        generators=[
            {
                "name": gen_name,
                "generator": _GENERATOR_TYPE,
                "params": dict(gen_params or {}),
            }
        ],
    )
    return int(ref.id)


# ── 1. THE HEADLINE: a foreign instance's connection survives ───────────
def test_foreign_connection_survives_changed_params_reapply(
    store: Store, fake_gen: dict[str, Any]
) -> None:
    fake_gen["expansion"] = _fake_expansion("GEN1", {"a": 1}, shared_net="GEN1_SHARED")
    ref_id = _apply(
        store,
        "reapply-1",
        gen_params={"a": 1},
        components=[{"refdes": "R1", "pins": [{"name": "1"}]}],
        connections=[{"net": "GEN1_SHARED", "refdes": "R1", "pin": "1"}],
    )

    shared_id_before = store.pcb_net_ids(ref_id)["GEN1_SHARED"]
    members_before = store.pcb_net_members(ref_id, "GEN1_SHARED")
    assert members_before is not None
    refdes_before = {m["refdes"] for m in members_before["members"]}
    assert refdes_before == {"GEN1", "R1"}

    # Changed params -- retire-and-reinsert the generator's OWN expansion.
    # R1 is never re-authored here: its wiring must survive untouched.
    fake_gen["expansion"] = _fake_expansion("GEN1", {"a": 2}, shared_net="GEN1_SHARED")
    reapplied_id = _apply(store, "reapply-1", gen_params={"a": 2})
    assert reapplied_id == ref_id

    shared_id_after = store.pcb_net_ids(ref_id)["GEN1_SHARED"]
    # Net IDENTITY preserved -- a same-named-but-different net_id would mean
    # the old row was retired and a fresh one minted under the same name,
    # exactly the bug: name equality must never stand in for row identity.
    assert shared_id_after == shared_id_before

    members_after = store.pcb_net_members(ref_id, "GEN1_SHARED")
    assert members_after is not None
    refdes_after = {m["refdes"] for m in members_after["members"]}
    assert "R1" in refdes_after, "foreign instance's connection was dropped"
    assert refdes_after == {"GEN1", "R1"}
    # Live member count must not have dropped (R1's pin is still wired,
    # and the generator's own new instance re-joined the same net_id).
    assert len(members_after["members"]) == len(members_before["members"])

    # R1 itself was never retired.
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT retired_at IS NULL FROM pcb_instances "
            "WHERE ref_id = %s AND refdes = %s",
            (ref_id, "R1"),
        ).fetchone()
    assert row is not None
    assert row[0] is True


# ── 2. THE OTHER DIRECTION: a solely-owned net IS still retired ─────────
def test_solely_owned_net_is_retired_and_recreated(
    store: Store, fake_gen: dict[str, Any]
) -> None:
    fake_gen["expansion"] = _fake_expansion("GEN1", {"a": 1})
    ref_id = _apply(store, "reapply-2", gen_params={"a": 1})

    own_id_before = store.pcb_net_ids(ref_id)["GEN1_OWN"]

    fake_gen["expansion"] = _fake_expansion("GEN1", {"a": 2})
    _apply(store, "reapply-2", gen_params={"a": 2})

    own_id_after = store.pcb_net_ids(ref_id)["GEN1_OWN"]
    # A fresh live net -- the old row is gone from the live map and a new
    # net_id took over the name (nothing foreign was ever attached to it,
    # so the fix's NOT EXISTS guard must not protect it).
    assert own_id_after != own_id_before

    with store.pool.connection() as conn:
        retired = conn.execute(
            "SELECT retired_at IS NOT NULL FROM pcb_nets WHERE net_id = %s",
            (own_id_before,),
        ).fetchone()
    assert retired is not None
    assert retired[0] is True


# ── 3. The generator's own instance is not duplicated by a re-apply ─────
def test_generator_own_instance_not_duplicated_by_reapply(
    store: Store, fake_gen: dict[str, Any]
) -> None:
    fake_gen["expansion"] = _fake_expansion("GEN1", {"a": 1})
    ref_id = _apply(store, "reapply-3", gen_params={"a": 1})

    fake_gen["expansion"] = _fake_expansion("GEN1", {"a": 2})
    _apply(store, "reapply-3", gen_params={"a": 2})

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT count(*) FROM pcb_instances "
            "WHERE ref_id = %s AND refdes = %s AND retired_at IS NULL",
            (ref_id, "GEN1"),
        ).fetchone()
    assert rows is not None
    assert rows[0] == 1

    with store.pool.connection() as conn:
        total_rows = conn.execute(
            "SELECT count(*) FROM pcb_instances WHERE ref_id = %s AND refdes = %s",
            (ref_id, "GEN1"),
        ).fetchone()
    assert total_rows is not None
    # One live + one retired -- the changed-params re-apply retired the old
    # instance and inserted exactly one fresh one, never left both live.
    assert total_rows[0] == 2
