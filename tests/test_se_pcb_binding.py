"""pcb→se binding, rough box (``docs/backlog/pcb-se-binding.md`` §v0).

:mod:`precis_se.board` is unit-tested as pure arithmetic (mm profile in
→ metres BoardDerived out); the wiring tests exercise the store round
trip — a real ``PcbHandler`` board, an se block bound to its slug, and
the rule that the derivation is **recomputed at load, never stored**
(:func:`precis_se.persist.attach_boards`).

Same fixture shape as ``test_se_catalog_binding.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import precis_se.board as se_board
from precis.cad import dsl as cad_dsl
from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import package_heights
from precis.store import Store
from precis_se import drc as se_drc
from precis_se import persist
from precis_se.handler import SeHandler
from precis_se.ops import SeBlock, SeTree, effective_envelope, effective_ports

# ── a 100 mm × 80 mm fixture board ───────────────────────────────────
# U1 top, own height 3 mm; C1 bottom, C0603 → the package-table row
# (0.9 mm); X1 top, footprint no row matches → the table fallback
# (its largest value). Two mounting holes.
_BOARD: dict[str, Any] = {
    "components": [
        {
            "refdes": "U1",
            "label": "MCU",
            "footprint": "CUSTOM-IC",
            "x": 10.0,
            "y": 20.0,
            "height_mm": 3.0,
            "pins": [{"name": "VDD"}],
        },
        {
            "refdes": "C1",
            "label": "100nF",
            "footprint": "C0603",
            "layer": "bottom",
            "x": 30.0,
            "y": 40.0,
            "pins": [{"name": "1"}, {"name": "2"}],
        },
        {
            "refdes": "X1",
            "label": "widget",
            "footprint": "TOTALLY-UNKNOWN-PKG",
            "x": 60.0,
            "y": 50.0,
            "pins": [{"name": "A"}],
        },
    ],
    "nets": [{"name": "VDD", "class": "power"}],
    "connections": [
        {"net": "VDD", "refdes": "U1", "pin": "VDD"},
        {"net": "VDD", "refdes": "C1", "pin": "1"},
    ],
    "features": [
        {
            "ftype": "outline",
            "geom": {"path": [[0, 0], [100, 0], [100, 80], [0, 80]]},
        },
        {"ftype": "mounting_hole", "x": 3.0, "y": 3.0, "geom": {"diameter": 3.2}},
        {"ftype": "mounting_hole", "x": 97.0, "y": 77.0, "geom": {"diameter": 3.2}},
    ],
}

_TABLE_HEIGHT_MM = package_heights.height_mm("C0603")[0]
_FALLBACK_HEIGHT_MM = package_heights.height_mm("TOTALLY-UNKNOWN-PKG")[0]
_THICKNESS_MM = 1.6
#: the box height: tallest bottom prism + thickness + tallest top prism —
#: and X1's unmapped footprint draws the table's largest value (11.5 mm),
#: which out-tops U1's own 3 mm.
_TOTAL_MM = _TABLE_HEIGHT_MM + _THICKNESS_MM + max(3.0, _FALLBACK_HEIGHT_MM)


def _profile(board: dict[str, Any]) -> dict[str, Any]:
    """The same dict ``mechanical_profile`` returns, built inline so the
    pure tests don't need the exporter's full model."""
    instances = [
        {
            "refdes": c["refdes"],
            "x": c["x"],
            "y": c["y"],
            "layer": c.get("layer", "top"),
            "height_mm": c.get("height_mm"),
            "footprint": c.get("footprint"),
        }
        for c in board["components"]
    ]
    holes = [
        {"x": f["x"], "y": f["y"], "diameter": f["geom"]["diameter"]}
        for f in board["features"]
        if f["ftype"] == "mounting_hole"
    ]
    outline = next(
        f["geom"]["path"] for f in board["features"] if f["ftype"] == "outline"
    )
    return {
        "units": "mm",
        "thickness_mm": _THICKNESS_MM,
        "outline": outline,
        "holes": holes,
        "blocks": [
            {
                "refdes": i["refdes"],
                "x": i["x"],
                "y": i["y"],
                "layer": i["layer"],
                "height_mm": i["height_mm"],
            }
            for i in instances
        ],
    }


class TestPackageHeights:
    def test_known_class(self) -> None:
        h, src = package_heights.height_mm("C0603")
        assert h == pytest.approx(0.9)
        assert "0603" in src

    def test_prefix_variants(self) -> None:
        assert package_heights.height_mm("SOT-23-3_L2.9-W1.3-P1.90")[0] > 0.0
        assert package_heights.height_mm("R0402")[0] == pytest.approx(0.55)

    def test_unmapped_takes_the_table_max(self) -> None:
        h, src = package_heights.height_mm("NO-SUCH-PACKAGE-7")
        assert h == max(row[1] for row in package_heights.HEIGHT_TABLE)
        assert "fallback" in src

    def test_heater_base_test_six_largest_classes_map(self) -> None:
        """The rows must cover the six footprint classes that carry 113
        of heater-base-test's 140 parts (the spec's coverage floor)."""
        for fp in (
            "R0402",
            "C0603",
            "HDR-SMD_6P-P2.54-V-F-R2-C3-LS7.0",
            "LED-SMD_4P-L2.0-W1.3-RD-TL",
            "WSOF-6_L2.6-W1.6-P0.50-TL-EP",
            "SOT-23-3_L2.9-W1.3-P1.90-LS2.4-BR",
        ):
            _h, src = package_heights.height_mm(fp)
            assert "fallback" not in src, fp


class TestDeriveBoard:
    def test_envelope_is_one_metre_box(self) -> None:
        derived = se_board.derive_board(_profile(_BOARD), _BOARD["components"])
        assert derived.envelope == cad_dsl.format_spec(
            cad_dsl.parse(f"box:w100mmd80mmh{_TOTAL_MM:g}mm", require_units=True)
        )
        spec = cad_dsl.parse(derived.envelope)
        assert spec.params["w"] == pytest.approx(0.1)
        assert spec.params["d"] == pytest.approx(0.08)
        assert spec.params["h"] == pytest.approx(_TOTAL_MM / 1000.0)

    def test_heights_user_then_table_then_fallback(self) -> None:
        derived = se_board.derive_board(_profile(_BOARD), _BOARD["components"])
        prisms = {p["refdes"]: p for p in derived.segments[0]["prisms"]}
        assert prisms["U1"]["origin"] == "user"
        assert prisms["U1"]["height_m"] == pytest.approx(0.003)
        assert prisms["C1"]["origin"] == "proposed"
        assert prisms["C1"]["height_m"] == pytest.approx(_TABLE_HEIGHT_MM / 1000.0)
        assert "0603" in (prisms["C1"]["source"] or "")
        assert prisms["X1"]["origin"] == "proposed"
        assert "fallback" in (prisms["X1"]["source"] or "")

    def test_frame_mapping(self) -> None:
        """The spec's pinned example: a part at board (10 mm, 20 mm) on
        top resolves to the expected se point — pcb x/y shifted by the
        fixed in-frame offset (the outline bbox centre, matching the cad
        ``box`` anchor), the slab's top at bottom-extent + thickness."""
        derived = se_board.derive_board(_profile(_BOARD), _BOARD["components"])
        u1 = next(p for p in derived.segments[0]["prisms"] if p["refdes"] == "U1")
        assert u1["x_m"] == pytest.approx(-0.04)
        assert u1["y_m"] == pytest.approx(-0.02)
        slab_top = derived.segments[0]["slab"]["z_m"][1]
        assert u1["z_m"][0] == pytest.approx(slab_top)
        # bottom copper face sits at the tallest bottom-side extent.
        assert derived.segments[0]["slab"]["z_m"][0] == pytest.approx(
            _TABLE_HEIGHT_MM / 1000.0
        )
        c1 = next(p for p in derived.segments[0]["prisms"] if p["refdes"] == "C1")
        assert c1["layer"] == "bottom"
        assert c1["z_m"][0] == pytest.approx(0.0)

    def test_hole_ports(self) -> None:
        derived = se_board.derive_board(_profile(_BOARD), _BOARD["components"])
        assert derived.ports is not None
        assert set(derived.ports) == {"H1", "H2"}
        h1 = derived.ports["H1"]
        assert h1.direction == [0.0, 0.0, 1.0]
        assert h1.pose is not None
        assert h1.pose == pytest.approx([-0.047, -0.037, 0.0009])
        assert h1.annotations["diameter_m"] == pytest.approx(0.0032)
        h2 = derived.ports["H2"]
        assert h2.pose is not None
        assert h2.pose[0] == pytest.approx(0.047)
        assert h2.pose[1] == pytest.approx(0.037)
        # the hole markers ride the segment too, through the thickness.
        holes = {h["name"]: h for h in derived.segments[0]["holes"]}
        assert holes["H1"]["through_m"] == pytest.approx(0.0016)

    def test_no_scale_literal_in_the_derivation_module(self) -> None:
        """The spec's unit ratchet: the mm→m crossing goes through
        ``catalog.to_metres`` and DSL unit tokens only — a stray scale
        literal in this file is the 2000-mm-becomes-2000-m bug."""
        source = Path(se_board.__file__).read_text(encoding="utf-8")
        for needle in ("1e-3", "0.001", "1e3", "1000", "/ 1000"):
            assert needle not in source

    def test_empty_profile_reports_why(self) -> None:
        derived = se_board.derive_board(
            {
                "units": "mm",
                "thickness_mm": 1.6,
                "outline": [],
                "holes": [],
                "blocks": [],
            }
        )
        assert derived.envelope is None
        assert derived.why_not


# ── the store round trip ─────────────────────────────────────────────
@pytest.fixture
def pcb(store: Store) -> PcbHandler:
    return PcbHandler(hub=Hub(store=store))


@pytest.fixture
def se(hub: Hub) -> SeHandler:
    return SeHandler(hub=hub)


def _se_design(name: str, slug: str) -> str:
    return json.dumps(
        {
            "description": "board under a lid",
            "ops": [
                {"op": "add_block", "name": "board"},
                {"op": "set_binding", "block": "board", "kind": "pcb", "design": slug},
            ],
        }
    )


def _load(se: SeHandler, store: Store, name: str, body: str) -> SeTree:
    se.put(id=name, text=body)
    ref = store.get_ref(kind="se", id=name)
    assert ref is not None
    return persist.load_tree(store, ref.id)


class TestLoadPass:
    def test_bound_board_resolves_at_tree_load(
        self, pcb: PcbHandler, se: SeHandler, store: Store
    ) -> None:
        """The spec's guard against write-accepted-never-resolved:
        ``set_binding`` then a plain load — no further ops — must yield
        the derived metre box."""
        pcb.put(id="fixture-board", args=_BOARD)
        tree = _load(se, store, "lid", _se_design("lid", "fixture-board"))
        node = tree.blocks["board"]
        assert node.derived is not None
        env = effective_envelope(tree, node)
        assert env is not None
        spec = cad_dsl.parse(env)
        assert spec.params["w"] == pytest.approx(0.1)
        assert spec.params["h"] == pytest.approx(_TOTAL_MM / 1000.0)

    def test_ports_resolve_at_load(
        self, pcb: PcbHandler, se: SeHandler, store: Store
    ) -> None:
        pcb.put(id="fixture-ports", args=_BOARD)
        tree = _load(se, store, "lid2", _se_design("lid2", "fixture-ports"))
        ports = effective_ports(tree, tree.blocks["board"])
        assert set(ports) >= {"H1", "H2"}

    def test_unknown_slug_is_a_finding_not_an_op_error(
        self, se: SeHandler, store: Store
    ) -> None:
        tree = _load(se, store, "lid3", _se_design("lid3", "no-such-board"))
        node = tree.blocks["board"]
        assert "not found" in (node.derived.why_not or "")
        report = se_drc.drc(tree)
        hit = [f for f in report.findings if f.rule == "pcb_binding_unresolved"]
        assert len(hit) == 1
        assert "no-such-board" in hit[0].detail
        assert hit[0].severity == "warn"

    def test_proposed_heights_finding_names_refdes(
        self, pcb: PcbHandler, se: SeHandler, store: Store
    ) -> None:
        pcb.put(id="fixture-findings", args=_BOARD)
        tree = _load(se, store, "lid4", _se_design("lid4", "fixture-findings"))
        report = se_drc.drc(tree)
        hit = [f for f in report.findings if f.rule == "pcb_height_proposed"]
        assert len(hit) == 1
        assert "C1" in hit[0].detail and "X1" in hit[0].detail
        assert "U1" not in hit[0].detail  # its own height_mm wins
        assert "fallback" in hit[0].detail

    def test_unbound_and_fake_store_are_no_ops(self, store: Store) -> None:
        tree = SeTree()
        tree.blocks["b"] = SeBlock(name="b", bound_kind="pcb", bound="x")
        persist.attach_boards(object(), tree)
        assert tree.blocks["b"].derived is None
        tree.blocks["c"] = SeBlock(name="c", bound_kind="cad", bound="y")
        persist.attach_boards(store, tree)
        assert tree.blocks["c"].derived is None
