"""Contract tests for :class:`precis.handlers.component.ComponentHandler`
(docs/proposals/component-kind.md acceptance criteria).
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.component import ComponentHandler


def _handler(store: Any) -> ComponentHandler:
    return ComponentHandler(hub=Hub(store=store))


# ── entity put/get (AC #2) ──────────────────────────────────────────


class TestEntity:
    def test_put_creates_entity_with_category_and_mpn(self, store: Any) -> None:
        h = _handler(store)
        h.put(
            id="m6-a2-bolt",
            title="M6x20 A2 socket cap",
            category="fastener",
            uom="each",
            meta={"mpn": "SCS-M6-20-A2"},
        )
        ref = store.get_ref(kind="component", id="m6-a2-bolt")
        assert ref is not None
        assert ref.title == "M6x20 A2 socket cap"
        assert ref.meta["category"] == "fastener"
        assert ref.meta["uom"] == "each"
        assert ref.meta["mpn"] == "SCS-M6-20-A2"

    def test_get_returns_the_entity(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        resp = h.get(id="m6-a2-bolt")
        assert "m6-a2-bolt" in resp.body
        assert "M6x20 A2 socket cap" in resp.body
        assert "fastener" in resp.body

    def test_put_again_upserts_title_and_merges_meta(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        h.put(id="m6-a2-bolt", meta={"mpn": "SCS-M6-20-A2"})
        ref = store.get_ref(kind="component", id="m6-a2-bolt")
        assert ref is not None
        assert ref.meta["category"] == "fastener"  # survives the second put
        assert ref.meta["mpn"] == "SCS-M6-20-A2"

    def test_entity_create_requires_category(self, store: Any) -> None:
        h = _handler(store)
        with pytest.raises(BadInput):
            h.put(id="no-category", title="No category")

    def test_get_missing_component_raises_not_found(self, store: Any) -> None:
        h = _handler(store)
        with pytest.raises(NotFound):
            h.get(id="does-not-exist")


# ── category mint (AC #3) ────────────────────────────────────────────


class TestCategoryMint:
    def test_unknown_category_mints_proposed(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="widget-1", title="Widget", category="widget")
        cat = store.component_category_get("widget")
        assert cat is not None
        assert cat["status"] == "proposed"

    def test_view_categories_shows_tier(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="widget-1", title="Widget", category="widget")
        resp = h.get(view="categories")
        assert "fastener" in resp.body
        assert "core" in resp.body
        assert "widget" in resp.body
        assert "proposed" in resp.body


# ── value put: canonical unit + applicability (AC #4) ────────────────


class TestValuePut:
    def test_value_with_canonical_unit_is_accepted(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        resp = h.put(id="m6-a2-bolt", spec="thread_pitch", value=1.0, unit="mm")
        assert "recorded" in resp.body
        ref = store.get_ref(kind="component", id="m6-a2-bolt")
        values = store.component_values_for_ref(ref.id)
        assert len(values) == 1
        assert values[0]["spec_id"] == "thread_pitch"
        assert values[0]["value_num"] == 1.0

    def test_non_canonical_unit_is_rejected_and_names_canonical(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        with pytest.raises(BadInput) as excinfo:
            h.put(id="m6-a2-bolt", spec="thread_pitch", value=0.04, unit="in")
        msg = str(excinfo.value)
        assert "in" in msg
        assert "mm" in msg

    def test_spec_not_applicable_to_category_is_rejected_naming_category(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        with pytest.raises(BadInput) as excinfo:
            h.put(id="m6-a2-bolt", spec="bore_diameter", value=10, unit="mm")
        msg = str(excinfo.value)
        assert "hose" in msg
        assert "fastener" in msg

    def test_universal_spec_applies_to_any_category(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        resp = h.put(id="m6-a2-bolt", spec="mass", value=0.01, unit="kg")
        assert "recorded" in resp.body

    def test_value_for_component_without_entity_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        with pytest.raises(NotFound):
            h.put(id="no-such-component", spec="mass", value=1, unit="kg")


# ── kind-scoped ref check ────────────────────────────────────────────


class TestKindScopedCheck:
    def test_component_ref_id_must_resolve_to_kind_component(self, store: Any) -> None:
        store.insert_ref(kind="paper", slug="m6-a2-bolt", title="Not a component")
        h = _handler(store)
        with pytest.raises(NotFound):
            h.put(id="m6-a2-bolt", spec="mass", value=1, unit="kg")


# ── proposed-spec mint scoped to category (AC #4) ─────────────────────


class TestProposedSpecMint:
    def test_unknown_spec_mints_proposed_quantity_scoped_to_category(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="some-hose", title="Some hose", category="hose")
        h.put(id="some-hose", spec="burst_pressure", value=60, unit="MPa")
        spec = store.component_spec_get("burst_pressure")
        assert spec is not None
        assert spec["status"] == "proposed"
        assert spec["canonical_unit"] == "MPa"
        assert spec["value_type"] == "quantity"
        assert spec["category_id"] == "hose"

    def test_proposed_spec_does_not_leak_to_other_categories(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="some-hose", title="Some hose", category="hose")
        h.put(id="some-hose", spec="burst_pressure", value=60, unit="MPa")
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        with pytest.raises(BadInput) as excinfo:
            h.put(id="m6-a2-bolt", spec="burst_pressure", value=10, unit="MPa")
        assert "hose" in str(excinfo.value)

    def test_unknown_spec_with_boolean_value_mints_boolean(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="some-hose", title="Some hose", category="hose")
        h.put(id="some-hose", spec="is_flexible", value=True)
        spec = store.component_spec_get("is_flexible")
        assert spec is not None
        assert spec["value_type"] == "boolean"
        assert spec["canonical_unit"] is None


# ── v1 trim (a): explicit value_type= / allowed_values= mint ────────


class TestExplicitTypeMint:
    def test_runtime_categorical_mint(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        h.put(
            id="m6-a2-bolt",
            spec="coating_color_a",
            value="black",
            value_type="categorical",
            allowed_values=["black", "silver", "gold"],
        )
        spec = store.component_spec_get("coating_color_a")
        assert spec is not None
        assert spec["value_type"] == "categorical"
        assert spec["allowed_values"] == ["black", "silver", "gold"]
        assert spec["canonical_unit"] is None
        ref = store.get_ref(kind="component", id="m6-a2-bolt")
        values = store.component_values_for_ref(ref.id)
        assert values[0]["value_text"] == "black"

    def test_categorical_value_outside_allowed_values_is_rejected_on_mint(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        h.put(
            id="m6-a2-bolt",
            spec="coating_color_b",
            value="black",
            value_type="categorical",
            allowed_values=["black", "silver", "gold"],
        )
        with pytest.raises(BadInput):
            h.put(id="m6-a2-bolt", spec="coating_color_b", value="purple")

    def test_categorical_mint_with_unit_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        with pytest.raises(BadInput):
            h.put(
                id="m6-a2-bolt",
                spec="coating_color_c",
                value="black",
                unit="mm",
                value_type="categorical",
                allowed_values=["black", "silver"],
            )

    def test_categorical_mint_without_allowed_values_is_rejected(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        with pytest.raises(BadInput):
            h.put(
                id="m6-a2-bolt",
                spec="coating_color_d",
                value="black",
                value_type="categorical",
            )

    def test_allowed_values_without_categorical_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        with pytest.raises(BadInput):
            h.put(
                id="m6-a2-bolt",
                spec="brand_new_quantity",
                value=1.5,
                allowed_values=["a", "b"],
            )

    def test_explicit_value_type_overrides_inference_for_boolean_and_text(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        # value=1 would infer 'quantity'; value_type='text' overrides that.
        h.put(id="m6-a2-bolt", spec="batch_code", value=1, value_type="text")
        spec = store.component_spec_get("batch_code")
        assert spec is not None
        assert spec["value_type"] == "text"

        h.put(
            id="m6-a2-bolt",
            spec="qc_pass",
            value="1",
            value_type="boolean",
        )
        spec2 = store.component_spec_get("qc_pass")
        assert spec2 is not None
        assert spec2["value_type"] == "boolean"

    def test_conflicting_value_type_against_existing_spec_is_rejected(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        h.put(id="m6-a2-bolt", spec="mass", value=0.01, unit="kg")
        with pytest.raises(BadInput) as excinfo:
            h.put(
                id="m6-a2-bolt",
                spec="mass",
                value="1",
                value_type="boolean",
            )
        assert "quantity" in str(excinfo.value)

    def test_bad_value_type_name_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        with pytest.raises(BadInput):
            h.put(
                id="m6-a2-bolt",
                spec="brand_new",
                value=1,
                value_type="not_a_real_type",
            )


# ── v1 trim (b): value_low= / value_high= uncertainty band ───────────


class TestBandedValues:
    def test_band_alongside_value(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        h.put(
            id="m6-a2-bolt",
            spec="mass",
            value=0.01,
            unit="kg",
            value_low=0.009,
            value_high=0.011,
        )
        ref = store.get_ref(kind="component", id="m6-a2-bolt")
        values = store.component_values_for_ref(ref.id)
        assert values[0]["value_num"] == 0.01
        assert values[0]["value_low"] == 0.009
        assert values[0]["value_high"] == 0.011

    def test_band_without_value_defaults_to_mean(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        h.put(
            id="m6-a2-bolt",
            spec="mass",
            unit="kg",
            value_low=0.01,
            value_high=0.03,
        )
        ref = store.get_ref(kind="component", id="m6-a2-bolt")
        values = store.component_values_for_ref(ref.id)
        assert values[0]["value_num"] == 0.02
        assert values[0]["value_low"] == 0.01
        assert values[0]["value_high"] == 0.03

    def test_low_greater_than_high_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        with pytest.raises(BadInput):
            h.put(
                id="m6-a2-bolt",
                spec="mass",
                unit="kg",
                value_low=0.03,
                value_high=0.01,
            )

    def test_one_sided_band_without_value_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        with pytest.raises(BadInput):
            h.put(
                id="m6-a2-bolt",
                spec="mass",
                unit="kg",
                value_low=0.01,
            )

    def test_band_on_non_numeric_spec_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        with pytest.raises(BadInput):
            h.put(
                id="m6-a2-bolt",
                spec="grade",
                value="A2",
                value_low=1,
                value_high=2,
            )


# ── band-aware range search ────────────────────────────────────────────


class TestBandSearch:
    def test_banded_value_matches_min_inside_its_band(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="hose-a", title="Hose A", category="hose")
        h.put(
            id="hose-a",
            spec="max_working_pressure",
            value=25,
            unit="MPa",
            value_low=20,
            value_high=30,
        )
        resp = h.search(spec="max_working_pressure", min=28)
        assert "Hose A" in resp.body

    def test_banded_value_matches_max_inside_its_band(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="hose-a", title="Hose A", category="hose")
        h.put(
            id="hose-a",
            spec="max_working_pressure",
            value=25,
            unit="MPa",
            value_low=20,
            value_high=30,
        )
        resp = h.search(spec="max_working_pressure", max=22)
        assert "Hose A" in resp.body

    def test_point_value_still_matches_exactly(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="hose-b", title="Hose B", category="hose")
        h.put(id="hose-b", spec="max_working_pressure", value=5, unit="MPa")
        resp = h.search(spec="max_working_pressure", min=20)
        assert "no component values match" in resp.body


# ── unit_cost + as_of (AC #5) ──────────────────────────────────────────


class TestUnitCost:
    def test_unit_cost_records_with_as_of_and_conditions(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        resp = h.put(
            id="m6-a2-bolt",
            spec="unit_cost",
            value=0.12,
            unit="USD",
            as_of="2026-07-01",
            conditions={"qty_break": 100},
        )
        assert "recorded" in resp.body
        ref = store.get_ref(kind="component", id="m6-a2-bolt")
        values = store.component_values_for_ref(ref.id)
        assert values[0]["spec_id"] == "unit_cost"
        assert values[0]["value_num"] == 0.12
        assert str(values[0]["as_of"]) == "2026-07-01"
        assert values[0]["conditions"] == {"qty_break": 100}


# ── made_of link (AC #6) ──────────────────────────────────────────────


class TestMadeOf:
    def test_made_of_creates_link_visible_on_get(self, store: Any) -> None:
        store.insert_ref(kind="material", slug="6061-t6", title="Aluminum 6061-T6")
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        h.put(id="m6-a2-bolt", made_of="material:6061-t6")
        resp = h.get(id="m6-a2-bolt")
        assert "Aluminum 6061-T6" in resp.body

    def test_made_of_not_resolving_to_material_is_rejected(self, store: Any) -> None:
        store.insert_ref(kind="paper", slug="not-a-material", title="Some paper")
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        with pytest.raises(BadInput):
            h.put(id="m6-a2-bolt", made_of="paper:not-a-material")

    def test_made_of_on_a_value_put(self, store: Any) -> None:
        store.insert_ref(kind="material", slug="6061-t6", title="Aluminum 6061-T6")
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        h.put(
            id="m6-a2-bolt",
            spec="mass",
            value=0.01,
            unit="kg",
            made_of="material:6061-t6",
        )
        resp = h.get(id="m6-a2-bolt")
        assert "Aluminum 6061-T6" in resp.body
        assert "mass" in resp.body


# ── get grouping + views (AC #7) ──────────────────────────────────────


class TestGet:
    def test_component_page_groups_by_spec(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        h.put(id="m6-a2-bolt", spec="mass", value=0.01, unit="kg")
        h.put(
            id="m6-a2-bolt",
            spec="thread_pitch",
            value=1.0,
            unit="mm",
            maturity="commercial",
        )
        resp = h.get(id="m6-a2-bolt")
        assert "mass" in resp.body
        assert "0.01" in resp.body
        assert "thread_pitch" in resp.body
        assert "commercial" in resp.body

    def test_view_table_renders_tidy_rows(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        h.put(id="m6-a2-bolt", spec="mass", value=0.01, unit="kg")
        resp = h.get(id="m6-a2-bolt", view="table")
        assert "mass" in resp.body
        assert "0.01" in resp.body

    def test_view_specs_without_id_lists_the_whole_registry(self, store: Any) -> None:
        h = _handler(store)
        resp = h.get(view="specs")
        assert "unit_cost" in resp.body
        assert "mass" in resp.body
        assert "thread_pitch" in resp.body  # whole registry, no category to scope by

    def test_view_specs_with_id_lists_universal_and_category(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        resp = h.get(id="m6-a2-bolt", view="specs")
        assert "unit_cost" in resp.body
        assert "thread_pitch" in resp.body
        assert "bore_diameter" not in resp.body

    def test_view_categories_lists_registry(self, store: Any) -> None:
        h = _handler(store)
        resp = h.get(view="categories")
        assert "fastener" in resp.body
        assert "hose" in resp.body
        assert "core" in resp.body


# ── source attachment (provenance integrity) ──────────────────────────


class TestSource:
    def test_source_url_surfaces_in_get(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        h.put(
            id="m6-a2-bolt",
            spec="mass",
            value=0.01,
            unit="kg",
            source="https://acme.example/catalog",
        )
        resp = h.get(id="m6-a2-bolt", view="table")
        assert "acme.example" in resp.body

    def test_source_ref_surfaces_as_handle(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        store.insert_ref(
            kind="datasheet", slug="acme-scs-catalog", title="Acme SCS catalog"
        )
        resp = h.put(
            id="m6-a2-bolt",
            spec="mass",
            value=0.01,
            unit="kg",
            source="datasheet:acme-scs-catalog",
        )
        assert "acme-scs-catalog" in resp.body
        table = h.get(id="m6-a2-bolt", view="table")
        assert "da" in table.body  # the datasheet's universal handle prefix

    def test_chunk_without_source_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
        with pytest.raises(BadInput):
            h.put(id="m6-a2-bolt", spec="mass", value=0.01, unit="kg", chunk="5")

    def test_chunk_from_different_paper_than_source_is_rejected(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="m6-a2-bolt", title="M6x20 A2 socket cap", category="fastener")
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
                id="m6-a2-bolt",
                spec="mass",
                value=0.01,
                unit="kg",
                source="paper:paper-b",
                chunk=f"pc{chunk_id}",
            )
        msg = str(excinfo.value)
        assert "paper-a" in msg
        assert "paper-b" in msg


# ── search: range filter with category + q (AC #8) ────────────────────


class TestSearch:
    def test_range_filter_with_category_returns_matching_hoses(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="hose-a", title="Hose A", category="hose")
        h.put(id="hose-a", spec="max_working_pressure", value=25, unit="MPa")
        h.put(id="hose-b", title="Hose B", category="hose")
        h.put(id="hose-b", spec="max_working_pressure", value=5, unit="MPa")

        resp = h.search(spec="max_working_pressure", min=20, category="hose")
        assert "Hose A" in resp.body
        assert "Hose B" not in resp.body

    def test_range_filter_with_maturity(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="foo", title="Foo component", category="fastener")
        h.put(
            id="foo",
            spec="mass",
            value=1.0,
            unit="kg",
            maturity="speculative",
        )
        resp = h.search(spec="mass", min=0, max=2, maturity="commercial")
        assert "no component values match" in resp.body

    def test_q_search_matches_name_mpn(self, store: Any) -> None:
        h = _handler(store)
        h.put(
            id="m6-a2-bolt",
            title="M6x20 A2 socket cap",
            category="fastener",
            meta={"mpn": "SCS-M6-20-A2"},
        )
        resp = h.search(q="M6")
        assert "m6-a2-bolt" in resp.body or "M6x20" in resp.body

    def test_search_requires_q_or_spec(self, store: Any) -> None:
        h = _handler(store)
        with pytest.raises(BadInput):
            h.search()

    def test_range_filter_on_non_numeric_spec_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        with pytest.raises(BadInput) as excinfo:
            h.search(spec="thread_size", max=5)
        assert "categorical" in str(excinfo.value)


# ── tools/core.py wiring (AC #9) ──────────────────────────────────────


class TestCoreParams:
    def test_put_accepts_component_params_without_typeerror(self) -> None:
        import inspect

        from precis.tools.core import put

        sig = inspect.signature(put)
        for name in (
            "spec",
            "category",
            "made_of",
            "uom",
            "as_of",
            "value_type",
            "allowed_values",
            "value_low",
            "value_high",
        ):
            assert name in sig.parameters, name

    def test_search_accepts_component_params_without_typeerror(self) -> None:
        import inspect

        from precis.tools.core import search

        sig = inspect.signature(search)
        for name in ("spec", "category", "min", "max", "maturity"):
            assert name in sig.parameters, name
