"""Rung 2 of docs/backlog/se-print-implementer.md: the printed solid
(:mod:`precis_se.printsolid`), the per-block feature collector and the
abstract-joint list (:mod:`precis_se.fasten`'s new ``features_for``/
``abstract_joints``), and the ``realize`` op that mints a block's first
implementation.

Reuses the seat-clamp fixture's helpers (:mod:`tests.test_se_fasten_seatclamp`)
rather than re-minting the M4 screw + two-part printed assembly by hand —
that fixture IS the worked example se-print-implementer.md names for this
rung's acceptance criteria.

Every design slug in this file is unique **per test** (never a bare
``"seat-clamp"``/``"wheel1"``): the test DB is one shared instance with no
per-test isolation, and `-n auto` runs tests from different files
concurrently — reusing an id another test (in this file or a sibling)
also writes races a `put`/`edit` against a concurrent one on the same row
(the `[[test_db_shared_singleton]]` gotcha)."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import pytest

import precis_se
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.component import ComponentHandler
from precis.store import Store
from precis_se import fasten as se_fasten
from precis_se import persist, printsolid
from precis_se.handler import SeHandler
from precis_se.ops import SeTree, apply_ops
from tests.test_se_fasten_seatclamp import _ensure_fastener_specs, _seat_clamp

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"

#: The one line every ``put(kind='component', series=..., size=...)``
#: response ends with, whatever the rest of the body says — see
#: :func:`_mint_slug`.
_COMPONENT_SLUG_RE = re.compile(r"id='([^']+)'\)\s*$")


def _mint_slug(hub: Hub, series: str, size: str) -> str:
    """The real, bare component slug for one series/size mint.

    Deliberately **not** ``test_se_fasten_seatclamp._mint``, whose return
    value is ``resp.body.splitlines()[0]`` — the *first* line, which is
    only ever a bare slug when the component already existed (an
    idempotent re-mint); on a fresh mint (this file's own tests are the
    first in a session to mint ``iso-10642-m4x12`` when run standalone or
    ahead of that sibling file) it is verbose prose ("created component
    iso-10642-m4x12 (M4x12 hexagon socket countersunk head screw (ISO
    10642))"), which `_seat_clamp`'s `set_binding(design=...)` would then
    store *verbatim* as the bound design — a slug that resolves nothing.
    ``_mint``'s own ``assert "iso-10642-m4x12" in slug`` only checks a
    substring, so it never catches this. The response's LAST line is
    unconditionally ``"Next: get(kind='component', id='<slug>')"``
    regardless of created/idempotent, so that is what this parses."""
    resp = ComponentHandler(hub=hub).put(series=series, size=size)
    assert "skipped" not in resp.body, f"incomplete mint: {resp.body}"
    match = _COMPONENT_SLUG_RE.search(resp.body)
    assert match is not None, f"could not find the minted slug in: {resp.body}"
    return match.group(1)


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    """Same fixture body as ``test_se_fasten_seatclamp``'s (replaying the
    se + fastener-spec migrations against the test DB) — kept local rather
    than imported as a fixture across modules, so this file's fixture
    graph doesn't depend on cross-module pytest fixture discovery; the
    substantial reuse (the mint helper, the seat-clamp ops builder) is
    the plain-function import above."""
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            c.execute(body.replace("BEGIN;", "").replace("COMMIT;", ""))
    _ensure_fastener_specs(store)
    return SeHandler(hub=hub)


def _load(handler: SeHandler, slug: str) -> SeTree:
    ref = handler.store.get_ref(kind="se", id=slug)
    assert ref is not None
    return persist.load_tree(handler.store, ref.id)


def _hole_volume(hole: se_fasten.Hole) -> float:
    """The naive, non-deduplicated volume of one stamped hole — the
    reference the boolean-cut ``printed_solid`` result is checked against
    (se-print-implementer.md's acceptance criterion names exactly this
    "within 1%" comparison)."""
    if hole.kind == "nut-pocket" and hole.across_flats_m:
        r = hole.across_flats_m / math.sqrt(3.0)
        area = (3.0 * math.sqrt(3.0) / 2.0) * r * r
        return area * hole.depth_m
    r = hole.diameter_m / 2.0
    return math.pi * r * r * hole.depth_m


class TestRealize:
    def test_realize_binds_a_cad_design_and_sets_mode(
        self, handler: SeHandler, hub: Hub
    ) -> None:
        screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
        handler.put(id="print2-realize-bind", text=_seat_clamp(screw_slug))
        resp = handler.edit(
            id="print2-realize-bind",
            ops=[{"op": "realize", "block": "clamp", "mode": "fdm/pla"}],
        )
        assert "print2-realize-bind-clamp" in resp.body

        cad_ref = handler.store.get_ref(kind="cad", id="print2-realize-bind-clamp")
        assert cad_ref is not None

        tree = _load(handler, "print2-realize-bind")
        node = tree.blocks["clamp"]
        assert node.bound_kind == "cad"
        assert node.bound == "print2-realize-bind-clamp"
        assert node.mode == "fdm/pla"

    def test_realize_twice_is_refused(self, handler: SeHandler, hub: Hub) -> None:
        screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
        handler.put(id="print2-realize-twice", text=_seat_clamp(screw_slug))
        handler.edit(
            id="print2-realize-twice",
            ops=[{"op": "realize", "block": "clamp", "mode": "fdm/pla"}],
        )
        with pytest.raises(BadInput, match="already bound"):
            handler.edit(
                id="print2-realize-twice",
                ops=[{"op": "realize", "block": "clamp", "mode": "fdm/pla"}],
            )

    def test_realize_on_an_array_member_realizes_the_template(
        self, handler: SeHandler
    ) -> None:
        handler.put(
            id="print2-realize-array",
            text=json.dumps(
                {
                    "ops": [
                        {
                            "op": "add_block",
                            "name": "spoke",
                            "envelope": "box:w0.002d0.002h0.05",
                        },
                        {
                            "op": "array_block",
                            "name": "spokes",
                            "template": "spoke",
                            "polar": {"count": 4, "radius": 0.05},
                        },
                    ]
                }
            ),
        )
        handler.edit(
            id="print2-realize-array",
            ops=[{"op": "realize", "block": "spokes", "mode": "fdm/pla"}],
        )
        tree = _load(handler, "print2-realize-array")
        assert tree.blocks["spoke"].bound_kind == "cad"
        assert tree.blocks["spoke"].bound == "print2-realize-array-spoke"
        # the array node itself never owns a binding of its own — it
        # resolves everything from the template, same as envelope/ports.
        assert tree.blocks["spokes"].bound_kind is None

    def test_realize_needs_an_envelope(self, handler: SeHandler) -> None:
        handler.put(
            id="print2-realize-bare",
            text=json.dumps({"ops": [{"op": "add_block", "name": "ghost"}]}),
        )
        with pytest.raises(BadInput, match="no envelope"):
            handler.edit(
                id="print2-realize-bare",
                ops=[{"op": "realize", "block": "ghost", "mode": "fdm/pla"}],
            )


class TestPrintedSolid:
    def test_volume_and_features_match_the_stamped_holes(
        self, handler: SeHandler, hub: Hub
    ) -> None:
        screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
        handler.put(id="print2-solid-volume", text=_seat_clamp(screw_slug))
        handler.edit(
            id="print2-solid-volume",
            ops=[{"op": "realize", "block": "clamp", "mode": "fdm/pla"}],
        )
        tree = _load(handler, "print2-solid-volume")

        result = printsolid.printed_solid(tree, "clamp", cad_store_reader=handler.store)
        assert result is not None
        assert result.block == "clamp"
        assert result.mode == "fdm/pla"

        holes = se_fasten.features_for(tree, "clamp")
        assert {h.kind for h in holes} == {"clearance", "nut-pocket"}
        assert set(result.features) == {h.name for h in holes}
        assert any("clearance" in name for name in result.features)
        assert any("nut-pocket" in name for name in result.features)

        expected_removed = sum(_hole_volume(h) for h in holes)
        expected_after = result.volume_before_m3 - expected_removed
        tol = 0.01 * result.volume_before_m3
        assert abs(result.volume_after_m3 - expected_after) <= tol
        assert not any(f.rule == "net_empty" for f in result.findings)

    def test_unrealized_block_has_no_printed_solid(
        self, handler: SeHandler, hub: Hub
    ) -> None:
        screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
        handler.put(id="print2-solid-unrealized", text=_seat_clamp(screw_slug))
        tree = _load(handler, "print2-solid-unrealized")
        # fdm-mode, never realize()'d — no cad binding yet.
        assert (
            printsolid.printed_solid(tree, "clamp", cad_store_reader=handler.store)
            is None
        )

    def test_purchase_bound_block_has_no_printed_solid(
        self, handler: SeHandler, hub: Hub
    ) -> None:
        screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
        handler.put(id="print2-solid-purchase", text=_seat_clamp(screw_slug))
        tree = _load(handler, "print2-solid-purchase")
        # "bolt" is purchase-mode, bound_kind='component' — bought, not
        # printed, whatever its mode says.
        assert tree.blocks["bolt"].bound_kind == "component"
        assert (
            printsolid.printed_solid(tree, "bolt", cad_store_reader=handler.store)
            is None
        )

    def test_net_empty_when_a_cut_consumes_the_whole_body(
        self, handler: SeHandler, hub: Hub
    ) -> None:
        screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
        payload: dict[str, Any] = json.loads(_seat_clamp(screw_slug))
        for op in payload["ops"]:
            if op.get("op") == "add_block" and op.get("name") == "clamp":
                # Shrunk to well under the M4 clearance-hole diameter (and
                # the M4 nut's across-flats) in x/y, same axis-centred
                # pose as the fixture — the stamped holes consume the
                # whole footprint.
                op["envelope"] = "box:w0.003d0.003h0.012"
        handler.put(id="print2-solid-net-empty", text=json.dumps(payload))
        handler.edit(
            id="print2-solid-net-empty",
            ops=[{"op": "realize", "block": "clamp", "mode": "fdm/pla"}],
        )
        tree = _load(handler, "print2-solid-net-empty")
        result = printsolid.printed_solid(tree, "clamp", cad_store_reader=handler.store)
        assert result is not None
        assert result.volume_after_m3 <= 0.0
        assert any(f.rule == "net_empty" for f in result.findings)

    def test_a_hole_that_cannot_be_cut_is_an_error_finding(
        self, handler: SeHandler, hub: Hub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stamped hole the solid does not carry is never dropped
        silently — the part would leave without a screw hole (pre-ship
        review finding)."""
        from dataclasses import replace

        screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
        handler.put(id="print2-solid-notcut", text=_seat_clamp(screw_slug))
        handler.edit(
            id="print2-solid-notcut",
            ops=[{"op": "realize", "block": "clamp", "mode": "fdm/pla"}],
        )
        tree = _load(handler, "print2-solid-notcut")
        real = se_fasten.features_for(tree, "clamp")
        assert real
        broken = [replace(real[0], diameter_m=0.0), *real[1:]]
        monkeypatch.setattr(
            printsolid.se_fasten, "features_for", lambda *_a, **_k: broken
        )
        result = printsolid.printed_solid(tree, "clamp", cad_store_reader=handler.store)
        assert result is not None
        not_cut = [f for f in result.findings if f.rule == "feature_not_cut"]
        assert len(not_cut) == 1 and not_cut[0].severity == "error"
        assert real[0].name in not_cut[0].detail
        assert real[0].name not in result.features
        assert len(result.features) == len(real) - 1

    def test_mode_scale_mismatch_on_a_nanoscale_fdm_block(
        self, handler: SeHandler
    ) -> None:
        handler.put(
            id="print2-solid-nano",
            text=json.dumps(
                {
                    "ops": [
                        {
                            "op": "add_block",
                            "name": "nano",
                            "envelope": "box:w5e-9d5e-9h5e-9",
                        }
                    ]
                }
            ),
        )
        handler.edit(
            id="print2-solid-nano",
            ops=[{"op": "realize", "block": "nano", "mode": "fdm/pla"}],
        )
        tree = _load(handler, "print2-solid-nano")
        result = printsolid.printed_solid(tree, "nano", cad_store_reader=handler.store)
        assert result is not None
        assert any(f.rule == "mode_scale_mismatch" for f in result.findings)


class TestAbstractJoints:
    def test_screw_with_no_fastener_named_is_abstract(self) -> None:
        tree = SeTree()
        apply_ops(
            tree,
            [
                {"op": "add_block", "name": "a", "envelope": "box:w0.01d0.01h0.01"},
                {
                    "op": "add_block",
                    "name": "b",
                    "envelope": "box:w0.01d0.01h0.01",
                    "pose": [0, 0, 0.01],
                },
                {"op": "add_port", "block": "a", "name": "top"},
                {"op": "add_port", "block": "b", "name": "bottom"},
                {
                    "op": "connect",
                    "a": "a.top",
                    "b": "b.bottom",
                    "joint": {"class": "rigid", "mechanism": "screw"},
                },
            ],
        )
        found = se_fasten.abstract_joints(tree)
        assert len(found) == 1
        connect, mechanism = found[0]
        assert mechanism == "screw"
        assert {connect.a_block, connect.b_block} == {"a", "b"}

    def test_unbound_fastener_is_abstract(self, handler: SeHandler, hub: Hub) -> None:
        screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
        payload = json.loads(_seat_clamp(screw_slug))
        unbound_ops = [op for op in payload["ops"] if op.get("op") != "set_binding"]
        handler.put(id="print2-abstract-unbound", text=json.dumps({"ops": unbound_ops}))
        tree_unbound = _load(handler, "print2-abstract-unbound")
        found = se_fasten.abstract_joints(tree_unbound)
        assert len(found) == 1
        assert found[0][1] == "screw"

    def test_naming_the_real_fastener_clears_it(
        self, handler: SeHandler, hub: Hub
    ) -> None:
        screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
        handler.put(id="print2-abstract-bound", text=_seat_clamp(screw_slug))
        tree_bound = _load(handler, "print2-abstract-bound")
        assert se_fasten.abstract_joints(tree_bound) == []
