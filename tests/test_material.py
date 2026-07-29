"""Contract tests for :class:`precis.handlers.material.MaterialHandler`
(docs/proposals/materials-handbook-kind.md acceptance criteria).
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.material import MaterialHandler


def _handler(store: Any) -> MaterialHandler:
    return MaterialHandler(hub=Hub(store=store))


# ── entity put/get (AC #2) ──────────────────────────────────────────


class TestEntity:
    def test_put_creates_entity_with_aliases_and_class(self, store: Any) -> None:
        h = _handler(store)
        h.put(
            id="6061-t6",
            title="Aluminum 6061-T6",
            meta={
                "material_class": "metal",
                "aliases": ["AA6061-T6", "aluminium alloy 6061"],
            },
        )
        ref = store.get_ref(kind="material", id="6061-t6")
        assert ref is not None
        assert ref.title == "Aluminum 6061-T6"
        assert ref.meta["material_class"] == "metal"
        assert "AA6061-T6" in ref.meta["aliases"]

    def test_get_returns_the_entity(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="6061-t6", title="Aluminum 6061-T6", meta={"material_class": "metal"})
        resp = h.get(id="6061-t6")
        assert "6061-t6" in resp.body
        assert "Aluminum 6061-T6" in resp.body
        assert "metal" in resp.body

    def test_put_again_merges_meta_and_updates_title(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="6061-t6", title="Aluminum 6061-T6", meta={"material_class": "metal"})
        h.put(id="6061-t6", meta={"aliases": ["AA6061-T6"]})
        ref = store.get_ref(kind="material", id="6061-t6")
        assert ref is not None
        assert ref.meta["material_class"] == "metal"  # survives the second put
        assert ref.meta["aliases"] == ["AA6061-T6"]

    def test_get_missing_material_raises_not_found(self, store: Any) -> None:
        h = _handler(store)
        with pytest.raises(NotFound):
            h.get(id="does-not-exist")


# ── value put: canonical unit + unknown property (AC #3) ────────────


class TestValuePut:
    def test_value_with_canonical_unit_is_accepted(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="6061-t6", title="Aluminum 6061-T6")
        resp = h.put(id="6061-t6", property="density", value=2700, unit="kg/m3")
        assert "recorded" in resp.body
        ref = store.get_ref(kind="material", id="6061-t6")
        values = store.material_values_for_ref(ref.id)
        assert len(values) == 1
        assert values[0]["property_id"] == "density"
        assert values[0]["value_num"] == 2700

    def test_unknown_property_without_unit_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="6061-t6", title="Aluminum 6061-T6")
        with pytest.raises(NotFound):
            h.put(id="6061-t6", property="totally_unseeded_prop", value=None)

    def test_non_canonical_unit_is_rejected_and_names_canonical(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="6061-t6", title="Aluminum 6061-T6")
        with pytest.raises(BadInput) as excinfo:
            h.put(id="6061-t6", property="density", value=0.0975, unit="lb/in3")
        msg = str(excinfo.value)
        assert "lb/in3" in msg
        assert "kg/m3" in msg

    def test_value_for_material_without_entity_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        with pytest.raises(NotFound):
            h.put(id="no-such-material", property="density", value=100, unit="kg/m3")

    def test_as_of_is_forwarded_to_the_value_row(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="6061-t6", title="Aluminum 6061-T6")
        h.put(
            id="6061-t6",
            property="cost_per_mass",
            value=3.5,
            unit="USD/kg",
            as_of="2026-07-01",
        )
        ref = store.get_ref(kind="material", id="6061-t6")
        values = store.material_values_for_ref(ref.id)
        assert str(values[0]["as_of"]) == "2026-07-01"


# ── kind-scoped ref check (AC #5) ───────────────────────────────────


class TestKindScopedCheck:
    def test_material_ref_id_must_resolve_to_kind_material(self, store: Any) -> None:
        h = _handler(store)
        # A slug that exists, but under a different kind (e.g. a paper).
        store.insert_ref(kind="paper", slug="6061-t6", title="Not a material")
        with pytest.raises(NotFound):
            h.put(id="6061-t6", property="density", value=100, unit="kg/m3")


# ── proposed-tier mint (AC #4) ───────────────────────────────────────


class TestProposedMint:
    def test_unknown_property_with_unit_mints_proposed_quantity(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="6061-t6", title="Aluminum 6061-T6")
        h.put(
            id="6061-t6",
            property="glass_transition_temp",
            value=358,
            unit="K",
        )
        prop = store.material_property_get("glass_transition_temp")
        assert prop is not None
        assert prop["status"] == "proposed"
        assert prop["canonical_unit"] == "K"
        assert prop["value_type"] == "quantity"

    def test_minted_property_flagged_proposed_never_core(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="6061-t6", title="Aluminum 6061-T6")
        h.put(id="6061-t6", property="brand_new_metric", value=1.5, unit="foo/bar")
        resp = h.get(view="properties")
        assert "brand_new_metric" in resp.body
        assert "proposed" in resp.body

    def test_unknown_property_with_boolean_value_mints_boolean(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="6061-t6", title="Aluminum 6061-T6")
        h.put(id="6061-t6", property="is_shiny", value=True)
        prop = store.material_property_get("is_shiny")
        assert prop is not None
        assert prop["value_type"] == "boolean"
        assert prop["canonical_unit"] is None


# ── categorical value_type routing + allowed_values (AC #3) ─────────


class TestCategorical:
    def test_categorical_value_within_allowed_values_accepted(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="cu-single-crystal", title="Copper single crystal")
        h.put(id="cu-single-crystal", property="crystal_structure", value="FCC")
        ref = store.get_ref(kind="material", id="cu-single-crystal")
        values = store.material_values_for_ref(ref.id)
        assert values[0]["value_text"] == "FCC"

    def test_categorical_value_outside_allowed_values_rejected(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="cu-single-crystal", title="Copper single crystal")
        with pytest.raises(BadInput):
            h.put(id="cu-single-crystal", property="crystal_structure", value="XYZ")

    def test_boolean_seeded_property_routes_to_value_bool(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fe-alloy", title="Iron alloy")
        h.put(id="fe-alloy", property="is_magnetic", value=True)
        ref = store.get_ref(kind="material", id="fe-alloy")
        values = store.material_values_for_ref(ref.id)
        assert values[0]["value_bool"] is True


# ── get grouping + view='properties' (AC #7) ─────────────────────────


class TestGet:
    def test_handbook_page_groups_by_property(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="6061-t6", title="Aluminum 6061-T6")
        h.put(id="6061-t6", property="density", value=2700, unit="kg/m3")
        h.put(
            id="6061-t6",
            property="tensile_strength_yield",
            value=276,
            unit="MPa",
            maturity="commercial",
        )
        resp = h.get(id="6061-t6")
        assert "density" in resp.body
        assert "2700" in resp.body
        assert "tensile_strength_yield" in resp.body
        assert "276" in resp.body
        assert "commercial" in resp.body

    def test_view_table_renders_tidy_rows(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="6061-t6", title="Aluminum 6061-T6")
        h.put(id="6061-t6", property="density", value=2700, unit="kg/m3")
        resp = h.get(id="6061-t6", view="table")
        assert "density" in resp.body
        assert "2700" in resp.body

    def test_view_properties_lists_registry(self, store: Any) -> None:
        h = _handler(store)
        resp = h.get(view="properties")
        assert "density" in resp.body
        assert "core" in resp.body
        assert "melting_point" in resp.body
        assert "K" in resp.body


# ── source attachment (AC #6) ───────────────────────────────────────


class TestSource:
    def test_source_url_surfaces_in_get(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="6061-t6", title="Aluminum 6061-T6")
        h.put(
            id="6061-t6",
            property="density",
            value=2700,
            unit="kg/m3",
            source="https://matweb.com/aluminum-6061",
        )
        resp = h.get(id="6061-t6", view="table")
        assert "matweb.com" in resp.body

    def test_source_ref_surfaces_as_handle(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="6061-t6", title="Aluminum 6061-T6")
        store.insert_ref(kind="paper", slug="matweb-alu-2020", title="Alu datasheet")
        resp = h.put(
            id="6061-t6",
            property="density",
            value=2700,
            unit="kg/m3",
            source="paper:matweb-alu-2020",
        )
        assert "matweb-alu-2020" in resp.body
        table = h.get(id="6061-t6", view="table")
        assert "pa" in table.body  # the paper's universal handle prefix

    def test_no_source_renders_dash(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="6061-t6", title="Aluminum 6061-T6")
        h.put(id="6061-t6", property="density", value=2700, unit="kg/m3")
        resp = h.get(id="6061-t6", view="table")
        assert "—" in resp.body

    def test_chunk_without_source_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="6061-t6", title="Aluminum 6061-T6")
        with pytest.raises(BadInput):
            h.put(
                id="6061-t6",
                property="density",
                value=2700,
                unit="kg/m3",
                chunk="5",
            )

    def test_chunk_from_different_paper_than_source_is_rejected(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="6061-t6", title="Aluminum 6061-T6")
        paper_a = store.insert_ref(kind="paper", slug="paper-a", title="Paper A")
        store.insert_ref(kind="paper", slug="paper-b", title="Paper B")
        with store.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO chunks (ref_id, ord, text, chunk_kind) "
                "VALUES (%s, 0, %s, 'paragraph') RETURNING chunk_id",
                (paper_a.id, "chunk body from paper A"),
            ).fetchone()
            conn.commit()
        chunk_id = row[0]
        with pytest.raises(BadInput) as excinfo:
            h.put(
                id="6061-t6",
                property="density",
                value=2700,
                unit="kg/m3",
                source="paper:paper-b",
                chunk=f"pc{chunk_id}",
            )
        msg = str(excinfo.value)
        assert "paper-a" in msg
        assert "paper-b" in msg

    def test_chunk_level_source_without_chunk_param_records_source_chunk(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="6061-t6", title="Aluminum 6061-T6")
        paper = store.insert_ref(
            kind="paper", slug="matweb-alu-2020", title="Alu datasheet"
        )
        with store.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO chunks (ref_id, ord, text, chunk_kind) "
                "VALUES (%s, 0, %s, 'paragraph') RETURNING chunk_id",
                (paper.id, "chunk body"),
            ).fetchone()
            conn.commit()
        chunk_id = row[0]
        h.put(
            id="6061-t6",
            property="density",
            value=2700,
            unit="kg/m3",
            source=f"pc{chunk_id}",
        )
        ref = store.get_ref(kind="material", id="6061-t6")
        values = store.material_values_for_ref(ref.id)
        assert values[0]["source_chunk"] == "matweb-alu-2020~0"


# ── search: range filter (AC #8) ─────────────────────────────────────


class TestSearch:
    def test_range_filter_returns_matching_materials(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="aerogel", title="Silica aerogel")
        h.put(id="aerogel", property="thermal_conductivity", value=0.02, unit="W/(m*K)")
        h.put(id="copper", title="Copper")
        h.put(id="copper", property="thermal_conductivity", value=401, unit="W/(m*K)")

        resp = h.search(property="thermal_conductivity", max=0.05)
        assert "aerogel" in resp.body.lower() or "Silica" in resp.body
        assert "Copper" not in resp.body

    def test_range_filter_with_maturity(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="foo", title="Foo material")
        h.put(
            id="foo",
            property="density",
            value=1000,
            unit="kg/m3",
            maturity="speculative",
        )
        resp = h.search(property="density", min=0, max=2000, maturity="commercial")
        assert "no material values match" in resp.body

    def test_q_search_matches_name_alias_class(self, store: Any) -> None:
        h = _handler(store)
        h.put(
            id="6061-t6",
            title="Aluminum 6061-T6",
            meta={"material_class": "metal", "aliases": ["AA6061-T6"]},
        )
        resp = h.search(q="AA6061-T6")
        assert "6061-t6" in resp.body or "Aluminum 6061-T6" in resp.body

    def test_search_requires_q_or_property(self, store: Any) -> None:
        h = _handler(store)
        with pytest.raises(BadInput):
            h.search()

    def test_range_filter_on_non_numeric_property_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        with pytest.raises(BadInput) as excinfo:
            h.search(property="crystal_structure", max=5)
        assert "categorical" in str(excinfo.value)


# ── tools/core.py wiring (AC #9) ──────────────────────────────────────


class TestCoreParams:
    def test_put_accepts_material_params_without_typeerror(self) -> None:
        import inspect

        from precis.tools.core import put

        sig = inspect.signature(put)
        for name in (
            "property",
            "value",
            "unit",
            "conditions",
            "maturity",
            "source",
            "chunk",
        ):
            assert name in sig.parameters, name

    def test_search_accepts_material_params_without_typeerror(self) -> None:
        import inspect

        from precis.tools.core import search

        sig = inspect.signature(search)
        for name in ("property", "min", "max", "maturity"):
            assert name in sig.parameters, name
