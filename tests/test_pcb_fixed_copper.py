"""pcb-pre-place-route-blocks Slice 1 -- the ``pcb_fixed_copper`` storage +
emission seam (docs/backlog/pcb-pre-place-route-blocks.md).

Slice 1 ships with **no generator emitting copper yet** -- these tests
exercise :meth:`precis.store._pcb_ops.PcbMixin._pcb_apply`'s routing of
:attr:`precis.pcb.generators.GeneratorExpansion.copper` in isolation, via a
FAKE generator registered by monkeypatching :func:`precis.pcb.generators.
expand` (the injection route the spec itself names -- real generators stay
untouched). Coverage: the fabric round-trips through
:meth:`~PcbMixin.pcb_fixed_copper_put`/:meth:`~PcbMixin.
pcb_fixed_copper_list` with net names resolved and ``fixed=True``; the
existing ``canonical_params`` idempotency discipline (no-op / retire-and-
reinsert) extends to fixed copper exactly like it already does to
components/nets/features; :meth:`~PcbMixin.pcb_copper_list` unions
authored fixed copper alongside derived (:meth:`~PcbMixin.
pcb_copper_replace`) copper without ever persisting fixed rows into the
derived table; and an envelope mismatch refuses the WHOLE apply, writing
nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.pcb.generators import GeneratorExpansion
from precis.store import Store


def _fake_expansion(
    name: str,
    params: dict[str, Any],
    *,
    version: int = 1,
    copper: list[dict[str, Any]] | None = None,
) -> GeneratorExpansion:
    """A minimal, otherwise-inert expansion -- one net per copper row's
    ``net``, zero components/connections/features -- so the ONLY thing
    ``_pcb_apply`` has to do with it besides bookkeeping is route
    ``copper`` into ``pcb_fixed_copper``."""
    copper = list(copper or [])
    net_names = sorted({str(c["net"]) for c in copper if c.get("net")})
    return GeneratorExpansion(
        refdes=name,
        generator="fake_fixed_copper_gen",
        version=version,
        canonical_params=dict(params),
        components=[],
        nets=[{"name": n} for n in net_names],
        connections=[],
        footprints=[],
        features=[],
        ledger={"summary": {"copper": len(copper)}},
        copper=copper,
    )


@pytest.fixture
def fake_gen(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Registers ``generator='fake_fixed_copper_gen'`` against a
    test-controlled expansion: set ``state['expansion']`` (or
    ``state['by_name'][name]`` for multiple calls in one batch) before
    ``store.pcb_apply(...)``. Monkeypatches :func:`precis.pcb.generators.
    expand` directly -- the module-level function ``_pcb_ops.py`` calls as
    ``pcb_generators.expand(...)``, so patching the module attribute
    intercepts every call site."""
    state: dict[str, Any] = {"expansion": None, "by_name": {}}

    def _expand(generator: str, gen_name: str, params: dict[str, Any]):
        assert generator == "fake_fixed_copper_gen", generator
        exp = state["by_name"].get(gen_name, state["expansion"])
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
    name: str = "GEN1",
    params: dict[str, Any] | None = None,
) -> tuple[int, int]:
    """``put(kind='pcb', generators=[{name, generator, params}])`` at the
    store layer -- returns ``(ref_id, board_id)``."""
    ref, _created, _counts = store.pcb_apply(
        slug=slug,
        title=slug,
        components=[],
        nets=[],
        connections=[],
        generators=[
            {
                "name": name,
                "generator": "fake_fixed_copper_gen",
                "params": dict(params or {}),
            }
        ],
    )
    board_id = store.pcb_ensure_board(ref.id)
    return ref.id, board_id


def _track_row(net: str, *, x: float = 0.0) -> dict[str, Any]:
    return {
        "ctype": "track",
        "layer": "F.Cu",
        "net": net,
        "geom": {
            "segments": [{"shape": "line", "start": [x, 0.0], "end": [x + 1.0, 0.0]}],
            "length_mm": 1.0,
            "width_mm": 0.2,
            "is_dogbone": False,
        },
    }


def _via_row(net: str, *, x: float = 1.0) -> dict[str, Any]:
    return {
        "ctype": "via",
        "layer": "F.Cu",
        "net": net,
        "geom": {
            "x": x,
            "y": 0.0,
            "dia_mm": 0.45,
            "drill_mm": 0.2,
            "span": ["F.Cu", "B.Cu"],
        },
    }


# ── (a) apply -> pcb_fixed_copper_list, net resolved, fixed=True ─────────
def test_generator_copper_lands_in_pcb_fixed_copper(store: Store, fake_gen):
    fake_gen["expansion"] = _fake_expansion(
        "GEN1", {"a": 1}, copper=[_track_row("GEN1_N1"), _via_row("GEN1_N1")]
    )
    ref_id, board_id = _apply(store, "fx-1", params={"a": 1})

    rows = store.pcb_fixed_copper_list(board_id)
    assert len(rows) == 2
    by_ctype = {r["ctype"]: r for r in rows}
    assert by_ctype["track"]["net"] == "GEN1_N1"
    assert by_ctype["track"]["fixed"] is True
    assert by_ctype["track"]["generator_name"] == "GEN1"
    assert by_ctype["track"]["segments"] == [
        {"shape": "line", "start": [0.0, 0.0], "end": [1.0, 0.0]}
    ]
    assert by_ctype["via"]["net"] == "GEN1_N1"
    assert by_ctype["via"]["span"] == ["F.Cu", "B.Cu"]


# ── (b) idempotency: no-op / retire-and-reinsert ─────────────────────────
def test_identical_reapply_is_a_noop(store: Store, fake_gen):
    fake_gen["expansion"] = _fake_expansion(
        "GEN1", {"a": 1}, copper=[_track_row("GEN1_N1"), _via_row("GEN1_N1")]
    )
    ref_id, board_id = _apply(store, "fx-2", params={"a": 1})
    assert len(store.pcb_fixed_copper_list(board_id)) == 2

    # Same params -> the generator-loop no-op branch -- fixed copper is
    # never re-touched, so re-running the SAME fake expansion object
    # (a fresh construction, still `canonical_params == {"a": 1}`) must
    # not duplicate anything.
    fake_gen["expansion"] = _fake_expansion(
        "GEN1", {"a": 1}, copper=[_track_row("GEN1_N1"), _via_row("GEN1_N1")]
    )
    _apply(store, "fx-2", params={"a": 1})
    rows = store.pcb_fixed_copper_list(board_id)
    assert len(rows) == 2


def test_changed_params_retires_old_and_writes_new(store: Store, fake_gen):
    fake_gen["expansion"] = _fake_expansion(
        "GEN1", {"a": 1}, copper=[_track_row("GEN1_N1"), _via_row("GEN1_N1")]
    )
    ref_id, board_id = _apply(store, "fx-3", params={"a": 1})
    assert len(store.pcb_fixed_copper_list(board_id)) == 2

    fake_gen["expansion"] = _fake_expansion(
        "GEN1",
        {"a": 2},
        copper=[_track_row("GEN1_N1"), _via_row("GEN1_N1"), _via_row("GEN1_N1", x=2.0)],
    )
    _apply(store, "fx-3", params={"a": 2})
    rows = store.pcb_fixed_copper_list(board_id)
    assert len(rows) == 3  # old 2 retired, new 3 present -- net count, not additive

    # Directly confirm the OLD rows are soft-deleted, not merely shadowed.
    with store.pool.connection() as conn:
        counts = conn.execute(
            "SELECT retired_at IS NULL, count(*) FROM pcb_fixed_copper "
            "WHERE ref_id = %s GROUP BY 1",
            (ref_id,),
        ).fetchall()
    by_active: dict[bool, int] = dict(counts)
    assert by_active[True] == 3
    assert by_active[False] == 2


# ── (c) pcb_copper_list unions fixed + derived; no duplication ──────────
def test_pcb_copper_list_unions_fixed_and_derived_no_duplication(
    store: Store, fake_gen
):
    fake_gen["expansion"] = _fake_expansion(
        "GEN1", {"a": 1}, copper=[_track_row("GEN1_N1"), _via_row("GEN1_N1")]
    )
    ref_id, board_id = _apply(store, "fx-4", params={"a": 1})

    net_id = store.pcb_net_ids(ref_id)["GEN1_N1"]
    derived_row = {
        "ctype": "track",
        "layer": "B.Cu",
        "net_id": net_id,
        "route_id": None,
        "geom": {
            "segments": [{"shape": "line", "start": [5.0, 5.0], "end": [6.0, 5.0]}],
            "length_mm": 1.0,
            "width_mm": 0.25,
            "is_dogbone": False,
        },
    }
    store.pcb_copper_replace(board_id, [derived_row])

    combined = store.pcb_copper_list(board_id)
    assert len(combined) == 3
    fixed = [r for r in combined if r.get("fixed")]
    derived = [r for r in combined if not r.get("fixed")]
    assert len(fixed) == 2
    assert len(derived) == 1
    assert derived[0]["layer"] == "B.Cu"

    # A second replace with the SAME single derived row (never the union
    # read back -- pcb_copper_replace's own docstring forbids that) must
    # not grow pcb_copper: still exactly 1 derived + 2 fixed.
    store.pcb_copper_replace(board_id, [derived_row])
    combined_again = store.pcb_copper_list(board_id)
    assert len(combined_again) == 3
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM pcb_copper WHERE board_id = %s", (board_id,)
        ).fetchone()
        assert row is not None
        (derived_count,) = row
    assert derived_count == 1  # pcb_copper itself never absorbed a fixed row


# ── (d) envelope mismatch refuses the whole apply, writes nothing ───────
def test_envelope_mismatch_refuses_and_writes_nothing(store: Store, fake_gen):
    bad_row = _track_row("GEN1_N1")
    bad_row["envelope"] = {"layers": 6}  # DEFAULT_STACKUP is 4 layers
    fake_gen["expansion"] = _fake_expansion("GEN1", {"a": 1}, copper=[bad_row])

    with pytest.raises(ValueError, match="envelope mismatch"):
        _apply(store, "fx-5", params={"a": 1})

    # Whole apply rolled back -- the design itself was never committed.
    assert store.get_ref(kind="pcb", id="fx-5") is None
