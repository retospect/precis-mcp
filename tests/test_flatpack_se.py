"""Build 3 of the flat-pack generator: the ``flatpack_box`` se op, the
``meta['flatpack']`` params home, ``view='cut'``, and the store-aware
``flatpack_*`` checks on ``view='drc'``
(docs/backlog/flatpack-furniture-generator.md).

The pure checks are exercised here through the handler's ``view='drc'``
and directly against ``precis_se.flatpack.checks.findings`` with a stub
store (``get_ref`` is the only read it makes).
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from shapely.geometry import box as shp_box

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import persist
from precis_se.flatpack import checks as checks_mod
from precis_se.flatpack.checks import findings
from precis_se.flatpack.generator import flatpack_box
from precis_se.handler import SeHandler
from precis_se.ops import SeTree

CUBE: dict[str, Any] = {"W": 50, "H": 50, "D": 50, "material": "corrugated-3mm"}


@pytest.fixture
def handler(hub: Hub) -> SeHandler:
    return SeHandler(hub=hub)


def _put_box(handler: SeHandler, slug: str = "cube50", **params: Any) -> None:
    handler.put(
        id=slug,
        ops=[{"op": "flatpack_box", "params": {**CUBE, **params}}],
    )


def _cad_slugs(store: Store, prefix: str) -> list[str]:
    """Live cad slugs under ``<prefix>-*``, via the identifiers index."""
    with store.pool.connection() as c:
        rows = c.execute(
            "SELECT i.id_value FROM ref_identifiers i JOIN refs r "
            "ON r.ref_id = i.ref_id WHERE i.id_kind = 'cite_key' "
            "AND r.kind = 'cad' AND r.retired_at IS NULL "
            "AND i.id_value LIKE %s ORDER BY i.id_value",
            (prefix + "-%",),
        ).fetchall()
    return [r[0] for r in rows]


def _findings_text(handler: SeHandler, slug: str) -> str:
    return handler.get(id=slug, view="drc").body


class TestOp:
    def test_mints_five_blocks_eight_rigid_connects_and_five_cad(
        self, handler: SeHandler, store: Store
    ) -> None:
        _put_box(handler)
        ref = store.get_ref(kind="se", id="cube50")
        assert ref is not None
        assert ref.meta["flatpack"]["W"] == 50
        assert (
            sorted(
                store.get_ref(kind="cad", id=f"cube50-{p}") is not None
                for p in ("side-l", "side-r", "top", "bottom", "back")
            )
            == [True] * 5
        )
        body = handler.get(id="cube50").body
        assert "side-l" in body and "shelf" not in body

    def test_view_tree_shows_connects_and_bindings(
        self, handler: SeHandler, store: Store
    ) -> None:
        _put_box(handler)
        ref = store.get_ref(kind="se", id="cube50")
        assert ref is not None
        tree = persist.load_tree(store, ref.id)
        assert sorted(tree.blocks) == ["back", "bottom", "side-l", "side-r", "top"]
        rigid = [
            c for c in tree.connects if c.joint and c.joint.get("class") == "rigid"
        ]
        assert len(rigid) == 8
        for name, node in tree.blocks.items():
            assert node.bound_kind == "cad"
            assert node.bound == f"cube50-{name}"
            # the bound cad design exists and parses
            assert store.get_ref(kind="cad", id=node.bound) is not None

    def test_cad_design_has_add_strips_and_cut_slots(
        self, handler: SeHandler, store: Store
    ) -> None:
        _put_box(handler, shelves=[22])
        cad_ref = store.get_ref(kind="cad", id="cube50-side-l")
        assert cad_ref is not None
        spec, _ = store.cad_load(cad_ref.id)
        ops = {n.op for n in spec.nodes}
        assert ops == {"add", "cut"}  # outline strips + the two shelf slots

    def test_drc_clean_of_mating_and_interpenetration(self, handler: SeHandler) -> None:
        _put_box(handler)
        body = _findings_text(handler, "cube50")
        assert "flatpack_mating" not in body
        assert "undeclared_interpenetration" not in body
        # the always-on rows are there
        assert "flatpack_utilisation" in body
        assert "flatpack_unrecorded_figure" in body

    def test_rerun_adds_a_shelf_without_suffixes(
        self, handler: SeHandler, store: Store
    ) -> None:
        _put_box(handler)
        handler.edit(
            id="cube50",
            ops=[
                {
                    "op": "flatpack_box",
                    "params": {**CUBE, "shelves": [22]},
                }
            ],
        )
        assert _cad_slugs(store, "cube50") == [
            "cube50-back",
            "cube50-bottom",
            "cube50-shelf-1",
            "cube50-side-l",
            "cube50-side-r",
            "cube50-top",
        ]

    def test_rerun_removing_a_shelf_retires_its_cad(
        self, handler: SeHandler, store: Store
    ) -> None:
        _put_box(handler, shelves=[22])
        handler.edit(id="cube50", ops=[{"op": "flatpack_box", "params": CUBE}])
        assert _cad_slugs(store, "cube50") == [
            "cube50-back",
            "cube50-bottom",
            "cube50-side-l",
            "cube50-side-r",
            "cube50-top",
        ]
        ref = store.get_ref(kind="se", id="cube50")
        assert ref is not None
        tree = persist.load_tree(store, ref.id)
        assert "shelf-1" not in tree.blocks
        assert not any("shelf" in f"{c.a_block}{c.b_block}" for c in tree.connects)

    def test_two_ops_one_list_retire_between_them(
        self, handler: SeHandler, store: Store
    ) -> None:
        # op 2 must retire what op 1 minted even though meta['flatpack']
        # only lands at commit time.
        handler.put(
            id="cube50",
            ops=[
                {
                    "op": "flatpack_box",
                    "params": {**CUBE, "shelves": [22, 35]},
                },
                {"op": "flatpack_box", "params": {**CUBE, "shelves": [22]}},
            ],
        )
        assert _cad_slugs(store, "cube50") == [
            "cube50-back",
            "cube50-bottom",
            "cube50-shelf-1",
            "cube50-side-l",
            "cube50-side-r",
            "cube50-top",
        ]
        ref = store.get_ref(kind="se", id="cube50")
        assert ref is not None
        assert ref.meta["flatpack"]["shelves"] == [22]

    def test_dogbone_cylinders_in_the_cad_design(
        self, handler: SeHandler, store: Store
    ) -> None:
        _put_box(
            handler,
            slug="crate",
            W=80,
            H=60,
            D=60,
            material="plywood-6mm",
            cutter_d=3.175,
        )
        ref = store.get_ref(kind="cad", id="crate-side-l")
        assert ref is not None
        spec, _ = store.cad_load(ref.id)
        bones = [n for n in spec.nodes if n.name.startswith("bone")]
        assert len(bones) >= 4  # four inside corners per carcass edge set
        assert all(n.op == "cut" and n.config.startswith("cyl:r") for n in bones)

    def test_edit_merges_meta_preserving_description(
        self, handler: SeHandler, store: Store
    ) -> None:
        handler.put(
            id="cube50",
            text=json.dumps(
                {
                    "description": "the first cardboard cube",
                    "ops": [{"op": "flatpack_box", "params": CUBE}],
                }
            ),
        )
        handler.edit(
            id="cube50",
            ops=[{"op": "flatpack_box", "params": {**CUBE, "shelves": [22]}}],
        )
        ref = store.get_ref(kind="se", id="cube50")
        assert ref is not None
        assert ref.meta["description"] == "the first cardboard cube"
        assert ref.meta["flatpack"]["shelves"] == [22]

    def test_edit_without_the_op_leaves_meta_alone(
        self, handler: SeHandler, store: Store
    ) -> None:
        handler.put(id="plain", ops=[{"op": "add_block", "name": "x"}])
        handler.edit(id="plain", ops=[{"op": "add_block", "name": "y"}])
        ref = store.get_ref(kind="se", id="plain")
        assert ref is not None
        assert "flatpack" not in (ref.meta or {})

    def test_put_without_the_op_keeps_meta_flatpack(
        self, handler: SeHandler, store: Store
    ) -> None:
        _put_box(handler)
        handler.put(
            id="cube50",
            ops=[{"op": "add_block", "name": "other"}],
            title="replaced",
        )
        ref = store.get_ref(kind="se", id="cube50")
        assert ref is not None
        assert ref.meta["flatpack"]["W"] == 50

    def test_unknown_param_and_missing_params_are_refused(
        self, handler: SeHandler
    ) -> None:
        with pytest.raises(BadInput, match="params"):
            handler.put(id="bad", ops=[{"op": "flatpack_box"}])
        with pytest.raises(BadInput):
            handler.put(
                id="bad",
                ops=[
                    {
                        "op": "flatpack_box",
                        "params": {**CUBE, "bogus": 1},
                    }
                ],
            )

    def test_block_name_collision_is_named(self, handler: SeHandler) -> None:
        with pytest.raises(BadInput, match="side-l"):
            handler.put(
                id="clash",
                ops=[
                    {"op": "add_block", "name": "side-l"},
                    {"op": "flatpack_box", "params": CUBE},
                ],
            )


class TestCutView:
    def test_svg_body_is_the_file(self, handler: SeHandler) -> None:
        _put_box(handler)
        body = handler.get(id="cube50", view="cut", args={"format": "svg"}).body
        assert body.startswith("<?xml")
        root = ET.fromstring(body)
        assert root.tag.endswith("svg")
        assert "parts: 5" in body
        assert "utilisation" in body
        assert "no recorded fit" in body
        assert "colour -> settings" in body

    def test_cricut_body_is_the_file(self, handler: SeHandler) -> None:
        _put_box(handler)
        body = handler.get(id="cube50", view="cut", args={"format": "cricut"}).body
        assert body.startswith("<?xml")
        root = ET.fromstring(body)
        assert root.tag.endswith("svg")
        assert 'data-op="cut"' in body and "data-blade" in body
        assert "pressure" in body and "parts: 5" in body

    def test_dxf_body_is_the_file(self, handler: SeHandler) -> None:
        _put_box(handler)
        body = handler.get(id="cube50", view="cut", args={"format": "dxf"}).body
        assert body.startswith("999\n")
        assert "ENTITIES" in body and body.rstrip().endswith("EOF")
        assert "parts: 5" in body
        assert "no recorded kerf" in body

    def test_needs_a_flatpack_design_and_a_format(self, handler: SeHandler) -> None:
        handler.put(id="plain", ops=[{"op": "add_block", "name": "x"}])
        with pytest.raises(BadInput, match="no flatpack params"):
            handler.get(id="plain", view="cut", args={"format": "svg"})
        _put_box(handler)
        with pytest.raises(BadInput, match="one of svg, dxf"):
            handler.get(id="cube50", view="cut", args={"format": "stl"})
        with pytest.raises(BadInput, match="unknown args"):
            handler.get(id="cube50", view="cut", args={"bogus": 1})


class TestChecks:
    """``checks.findings`` direct — a stub ref carries meta['flatpack']."""

    def _stub(self, params: dict[str, Any] | None) -> Any:
        ref = (
            SimpleNamespace(meta={"flatpack": params})
            if params is not None
            else SimpleNamespace(meta={})
        )
        return SimpleNamespace(get_ref=lambda *, kind, id, **kw: ref)

    def test_no_params_no_findings(self) -> None:
        assert findings(self._stub(None), SeTree(), 1) == []

    def test_cube_is_clean_but_reports(self) -> None:
        out = findings(self._stub(CUBE), SeTree(), 1)
        rules = {f.rule: f.severity for f in out}
        assert "flatpack_mating" not in rules
        assert rules["flatpack_utilisation"] == "info"
        assert rules["flatpack_unrecorded_figure"] == "warn"
        # no shelves → no sag row at all
        assert "flatpack_shelf_sag" not in rules

    def test_sag_skips_say_why(self) -> None:
        out = findings(self._stub({**CUBE, "shelves": [22]}), SeTree(), 1)
        (sag,) = [f for f in out if f.rule == "flatpack_shelf_sag"]
        assert sag.severity == "warn"
        assert "shelf_load_n unset" in sag.detail
        out = findings(
            self._stub({**CUBE, "shelves": [22], "shelf_load_n": 5}), SeTree(), 1
        )
        (sag,) = [f for f in out if f.rule == "flatpack_shelf_sag"]
        assert "no recorded E" in sag.detail

    def test_sag_warns_past_3mm(self) -> None:
        out = findings(
            self._stub(
                {
                    "W": 300,
                    "H": 60,
                    "D": 80,
                    "material": "plywood-6mm",
                    "shelves": [30],
                    "shelf_load_n": 200,
                    "cutter_d": 3.175,
                }
            ),
            SeTree(),
            1,
        )
        (sag,) = [f for f in out if f.rule == "flatpack_shelf_sag"]
        assert sag.severity == "warn"
        # 5·F·L³ / 384·E·I = 5·200·288³ / (384·9000·1332) = 5.189 mm —
        # the figure itself is asserted, not just the limit wording.
        assert "sags 5.189 mm" in sag.detail

    def test_broken_params_are_an_error_not_a_crash(self) -> None:
        out = findings(
            self._stub({"W": 50, "H": 50, "D": 50, "material": "unobtanium"}),
            SeTree(),
            1,
        )
        (f,) = out
        assert f.rule == "flatpack_regen"
        assert f.severity == "error"


class TestChecksNegative:
    """Every error rule, made to fire once by perturbing what the regen
    produces — ``checks.flatpack_box`` is patched so ``findings`` sees a
    broken Flatpack rather than a valid one."""

    def _findings(self, monkeypatch: pytest.MonkeyPatch, mutate: Any) -> list[Any]:
        def fake(**kw: Any) -> Any:
            return mutate(flatpack_box(**kw))

        monkeypatch.setattr(checks_mod, "flatpack_box", fake)
        stub: Any = SimpleNamespace(
            get_ref=lambda *, kind, id, **kw: SimpleNamespace(meta={"flatpack": CUBE})
        )
        return findings(stub, SeTree(), 1)

    def test_mating_fires_when_a_gap_is_filled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fill the first left-edge notch of "top" — side-l's finger now
        # lands inside top's material instead of its gap.
        def mutate(fp: Any) -> Any:
            return replace(
                fp,
                panels=tuple(
                    replace(p, polygon=p.polygon.union(shp_box(0, 0, 3, 10)))
                    if p.id == "top"
                    else p
                    for p in fp.panels
                ),
            )

        out = self._findings(monkeypatch, mutate)
        err = [f for f in out if f.rule == "flatpack_mating"]
        assert err and all(f.severity == "error" for f in err)
        assert any("overlaps top's material" in f.detail for f in err)

    def test_min_width_fires_on_a_thin_panel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def mutate(fp: Any) -> Any:
            return replace(
                fp,
                panels=tuple(
                    replace(p, polygon=shp_box(0, 0, 50, 5)) if p.id == "top" else p
                    for p in fp.panels
                ),
            )

        out = self._findings(monkeypatch, mutate)
        err = [f for f in out if f.rule == "flatpack_min_width"]
        assert [(f.subject, f.severity) for f in err] == [("top", "error")]

    def test_nest_overlap_fires_on_shared_origin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def mutate(fp: Any) -> Any:
            first, second, *rest = fp.nesting.placements
            moved = replace(second, x=first.x, y=first.y, rotated=False)
            return replace(
                fp,
                nesting=replace(fp.nesting, placements=(first, moved, *rest)),
            )

        out = self._findings(monkeypatch, mutate)
        err = [f for f in out if f.rule == "flatpack_nest_overlap"]
        assert err and all(f.severity == "error" for f in err)

    def test_off_sheet_fires_past_the_margin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        moved_part: list[str] = []

        def mutate(fp: Any) -> Any:
            w, _ = fp.nesting.sheet
            first, *rest = fp.nesting.placements
            moved_part.append(first.part)
            pushed = replace(first, x=w - fp.nesting.margin + 5)
            return replace(
                fp,
                nesting=replace(fp.nesting, placements=(pushed, *rest)),
            )

        out = self._findings(monkeypatch, mutate)
        err = [f for f in out if f.rule == "flatpack_off_sheet"]
        assert [(f.subject, f.severity) for f in err] == [(moved_part[0], "error")]
