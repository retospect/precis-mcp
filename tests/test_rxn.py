"""Contract tests for :class:`precis.handlers.rxn.RxnHandler` — the sourced
reaction-fact store (``rxn`` kind, deliberately the ``material`` star schema:
entity ref + property registry + sourced-value fact table).

Identity-key derivation itself (``uid_strict``/``uid_transform``) is pinned in
``tests/test_rxn_ids.py``; this file covers the handler/store behaviour that
sits on top of it.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.handlers._rxn_ids import rdkit_available
from precis.handlers.rxn import RxnHandler, _display_value

_FISCHER = "CC(=O)O.OCC>>CC(=O)OCC.O"

needs_rdkit = pytest.mark.skipif(
    not rdkit_available(), reason="rdkit ([chem] extra) not installed"
)


def _handler(store: Any) -> RxnHandler:
    return RxnHandler(hub=Hub(store=store))


# ── init / display formatting (no DB needed) ──────────────────────────


class TestInit:
    def test_missing_store_raises_init_error(self) -> None:
        with pytest.raises(InitError):
            RxnHandler(hub=Hub(store=None))


class TestDisplayValue:
    def test_numeric_with_band_renders_range(self) -> None:
        shown = _display_value(
            {"value_num": 80.0, "value_low": 75.0, "value_high": 85.0}
        )
        assert shown == "80 (75–85)"

    def test_boolean_value_renders_as_str(self) -> None:
        assert _display_value({"value_bool": True}) == "True"

    def test_text_value_renders_as_is(self) -> None:
        assert _display_value({"value_text": "toluene"}) == "toluene"

    def test_all_none_renders_em_dash(self) -> None:
        assert _display_value({}) == "—"


# ── entity create/update ────────────────────────────────────────────


class TestEntity:
    def test_put_with_no_id_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        with pytest.raises(BadInput):
            h.put(property="yield", value=83)

    def test_create_without_rxn_smiles_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        with pytest.raises(BadInput):
            h.put(id="fischer-etoac")

    @needs_rdkit
    def test_create_with_rxn_smiles_canonicalises_and_derives_keys(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        ref = store.get_ref(kind="rxn", id="fischer-etoac")
        assert ref is not None
        assert ref.meta["rxn_smiles"]
        assert ref.meta["uid_strict"]
        assert ref.meta["uid_transform"]
        assert ref.meta["desired_product"]

    @needs_rdkit
    def test_second_put_merges_meta_and_keeps_identity_keys(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        ref_before = store.get_ref(kind="rxn", id="fischer-etoac")
        assert ref_before is not None
        uid_before = ref_before.meta["uid_transform"]

        h.put(id="fischer-etoac", reaction_class="RXNO:0000024")
        ref_after = store.get_ref(kind="rxn", id="fischer-etoac")
        assert ref_after is not None
        assert ref_after.meta["uid_transform"] == uid_before
        assert ref_after.meta["uid_strict"]
        assert ref_after.meta["rxn_smiles"]
        assert ref_after.meta["reaction_class"] == "RXNO:0000024"

    @needs_rdkit
    def test_bad_rxn_smiles_raises_bad_input(self, store: Any) -> None:
        h = _handler(store)
        with pytest.raises(BadInput):
            h.put(id="bogus", rxn_smiles="not-a-smiles>>CCO")

    @needs_rdkit
    def test_same_transform_different_slug_emits_precedent_note(
        self, store: Any
    ) -> None:
        """A second rxn whose transform key matches an existing one (here:
        same chemistry, byproduct recorded differently) should point at the
        existing slug rather than silently look like a fresh, unrelated row."""
        h = _handler(store)
        h.put(id="rxn-a", rxn_smiles="CCBr.N#C[Na]>>CCC#N")
        resp = h.put(id="rxn-b", rxn_smiles="CCBr.N#C[Na]>>CCC#N.[Na]Br")
        assert "rxn-a" in resp.body
        assert "already recorded" in resp.body


# ── value append ─────────────────────────────────────────────────────


class TestValuePut:
    def test_value_append_inserts_row_and_mentions_it_in_response(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        resp = h.put(
            id="fischer-etoac",
            property="yield",
            value=83,
            unit="%",
            conditions={"solvent": "toluene"},
            method="measured",
        )
        assert "83" in resp.body
        ref = store.get_ref(kind="rxn", id="fischer-etoac")
        assert ref is not None
        values = store.rxn_values_for_ref(ref.id)
        assert len(values) == 1
        assert values[0]["property_id"] == "yield"
        assert values[0]["value_num"] == 83
        assert values[0]["method"] == "measured"

    def test_value_for_missing_reaction_raises_not_found(self, store: Any) -> None:
        h = _handler(store)
        with pytest.raises(NotFound):
            h.put(id="no-such-rxn", property="yield", value=83, unit="%")

    def test_wrong_unit_is_rejected_and_names_canonical(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        # Probe the MIGRATION-SEEDED core row directly — no prior write to
        # establish it. This was previously softened by writing a '%' value
        # first, because conftest's TRUNCATE wiped the 0157 seed and the
        # property would otherwise be minted 'proposed' with whatever unit the
        # first caller passed. That gap is fixed (rxn_properties is now in
        # _PRESERVE_TABLES, guarded by tests/test_rxn_seed.py), so assert
        # against the real seeded definition.
        seeded = store.rxn_property_get("yield")
        assert seeded is not None and seeded["status"] == "core"
        with pytest.raises(BadInput) as excinfo:
            h.put(id="fischer-etoac", property="yield", value=83, unit="percent")
        msg = str(excinfo.value)
        assert "percent" in msg
        assert "%" in msg

    def test_invalid_method_is_rejected_naming_options(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput) as excinfo:
            h.put(
                id="fischer-etoac",
                property="yield",
                value=83,
                unit="%",
                method="guessed",
            )
        msg = str(excinfo.value)
        assert "guessed" in msg
        assert "measured" in msg
        assert "predicted" in msg

    def test_invalid_maturity_is_rejected_naming_options(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput) as excinfo:
            h.put(
                id="fischer-etoac",
                property="yield",
                value=83,
                unit="%",
                maturity="hopeful",
            )
        msg = str(excinfo.value)
        assert "hopeful" in msg
        assert "commercial" in msg
        assert "lab" in msg
        assert "speculative" in msg

    def test_conditions_must_be_a_dict(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput):
            h.put(
                id="fischer-etoac",
                property="yield",
                value=83,
                unit="%",
                conditions="toluene",  # type: ignore[arg-type]
            )

    def test_invalid_value_type_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput):
            h.put(
                id="fischer-etoac",
                property="yield",
                value=83,
                unit="%",
                value_type="bogus",
            )

    def test_allowed_values_without_categorical_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput):
            h.put(
                id="fischer-etoac",
                property="yield",
                value=83,
                unit="%",
                allowed_values=["a", "b"],
            )

    def test_value_type_conflicting_with_registered_property_is_rejected(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput) as excinfo:
            h.put(
                id="fischer-etoac",
                property="yield",
                value=83,
                unit="%",
                value_type="quantity",
            )
        msg = str(excinfo.value)
        assert "ratio" in msg
        assert "quantity" in msg

    def test_url_source_renders_source_url_in_response(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        resp = h.put(
            id="fischer-etoac",
            property="yield",
            value=83,
            unit="%",
            source="https://example.com/paper",
        )
        assert "source_url=" in resp.body
        assert "https://example.com/paper" in resp.body


# ── central behaviour: many rows per (reaction, property) ────────────


class TestManyValuesIsTheFeature:
    def test_multiple_yields_all_appear_never_averaged(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        h.put(
            id="fischer-etoac",
            property="yield",
            value=83,
            unit="%",
            conditions={"solvent": "toluene"},
        )
        h.put(
            id="fischer-etoac",
            property="yield",
            value=91,
            unit="%",
            conditions={"solvent": "DCM"},
        )
        h.put(
            id="fischer-etoac",
            property="yield",
            value=67,
            unit="%",
            conditions={"solvent": "neat"},
        )
        resp = h.get(id="fischer-etoac")
        assert "83" in resp.body
        assert "91" in resp.body
        assert "67" in resp.body
        # not collapsed into an average of the three (80.33...)
        assert "80.3" not in resp.body
        assert "sourced: 0 of 3" in resp.body


# ── unit checking ──────────────────────────────────────────────────────


class TestCheckUnit:
    def test_unit_omitted_is_accepted_without_comparison(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        resp = h.put(id="fischer-etoac", property="solvent", value="toluene")
        assert "toluene" in resp.body

    def test_unit_on_dimensionless_property_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput) as excinfo:
            h.put(id="fischer-etoac", property="solvent", value="toluene", unit="M")
        assert "dimensionless" in str(excinfo.value)


# ── minting an unknown property: type inference ────────────────────────


class TestMintInference:
    def test_bool_value_mints_boolean_type(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        h.put(id="fischer-etoac", property="is_photostable", value=True)
        prop = store.rxn_property_get("is_photostable")
        assert prop is not None
        assert prop["value_type"] == "boolean"

    def test_string_value_mints_text_type(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        h.put(id="fischer-etoac", property="colour_change", value="yellow to clear")
        prop = store.rxn_property_get("colour_change")
        assert prop is not None
        assert prop["value_type"] == "text"

    def test_categorical_mint_without_allowed_values_is_rejected(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput):
            h.put(
                id="fischer-etoac",
                property="grade",
                value="A",
                value_type="categorical",
            )

    def test_categorical_mint_with_unit_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput):
            h.put(
                id="fischer-etoac",
                property="grade",
                value="A",
                value_type="categorical",
                allowed_values=["A", "B"],
                unit="M",
            )

    def test_boolean_mint_with_unit_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput):
            h.put(id="fischer-etoac", property="is_stable", value=True, unit="K")

    def test_text_mint_with_unit_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput):
            h.put(id="fischer-etoac", property="notes_field", value="hi", unit="K")


# ── value routing by value_type ─────────────────────────────────────────


class TestRouteValueMatrix:
    def test_missing_value_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput):
            h.put(id="fischer-etoac", property="yield", value=None, unit="%")

    def test_boolean_property_rejects_non_bool_value(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        h.put(id="fischer-etoac", property="is_novel", value=True)
        with pytest.raises(BadInput):
            h.put(id="fischer-etoac", property="is_novel", value="yes")

    def test_boolean_property_accepts_bool_value(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        h.put(id="fischer-etoac", property="is_novel", value=True)
        resp = h.put(id="fischer-etoac", property="is_novel", value=False)
        assert "False" in resp.body

    def test_categorical_value_not_in_allowed_values_is_rejected(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        h.put(
            id="fischer-etoac",
            property="grade",
            value="A",
            value_type="categorical",
            allowed_values=["A", "B"],
        )
        with pytest.raises(BadInput):
            h.put(id="fischer-etoac", property="grade", value="C")

    def test_categorical_value_in_allowed_values_is_stored(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        h.put(
            id="fischer-etoac",
            property="grade",
            value="A",
            value_type="categorical",
            allowed_values=["A", "B"],
        )
        resp = h.put(id="fischer-etoac", property="grade", value="B")
        assert "B" in resp.body

    def test_text_property_stores_the_value(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        h.put(id="fischer-etoac", property="workup_note", value="first note")
        resp = h.put(id="fischer-etoac", property="workup_note", value="second note")
        assert "second note" in resp.body

    def test_non_numeric_string_for_quantity_property_is_rejected(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput):
            h.put(id="fischer-etoac", property="yield", value="not-a-number", unit="%")

    def test_value_low_without_value_high_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput):
            h.put(
                id="fischer-etoac", property="yield", value=80, unit="%", value_low=70
            )

    def test_value_high_without_value_low_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput):
            h.put(
                id="fischer-etoac", property="yield", value=80, unit="%", value_high=90
            )

    def test_value_low_above_value_high_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput):
            h.put(
                id="fischer-etoac",
                property="yield",
                value=80,
                unit="%",
                value_low=90,
                value_high=70,
            )

    def test_valid_band_stores_both_bounds(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        h.put(
            id="fischer-etoac",
            property="yield",
            value=80,
            unit="%",
            value_low=75,
            value_high=85,
        )
        ref = store.get_ref(kind="rxn", id="fischer-etoac")
        assert ref is not None
        values = store.rxn_values_for_ref(ref.id)
        assert values[0]["value_low"] == 75
        assert values[0]["value_high"] == 85


# ── source resolution ─────────────────────────────────────────────────


class TestResolveSource:
    def test_chunk_without_source_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput):
            h.put(
                id="fischer-etoac",
                property="yield",
                value=83,
                unit="%",
                chunk="pc5",
            )

    def test_chunk_with_url_source_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput):
            h.put(
                id="fischer-etoac",
                property="yield",
                value=83,
                unit="%",
                source="https://example.com/paper",
                chunk="pc5",
            )

    def test_source_without_colon_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput):
            h.put(
                id="fischer-etoac",
                property="yield",
                value=83,
                unit="%",
                source="just-a-string",
            )

    def test_source_paper_not_found_raises_not_found(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(NotFound):
            h.put(
                id="fischer-etoac",
                property="yield",
                value=83,
                unit="%",
                source="paper:no-such-paper",
            )

    def test_source_paper_resolves(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        store.insert_ref(kind="paper", slug="fischer-1895", title="Fischer paper")
        resp = h.put(
            id="fischer-etoac",
            property="yield",
            value=83,
            unit="%",
            source="paper:fischer-1895",
        )
        assert "fischer-1895" in resp.body
        ref = store.get_ref(kind="rxn", id="fischer-etoac")
        assert ref is not None
        values = store.rxn_values_for_ref(ref.id)
        assert values[0]["source_ref_id"] is not None

    def test_unknown_source_kind_is_rejected_naming_allowed_kinds(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(BadInput) as excinfo:
            h.put(
                id="fischer-etoac",
                property="yield",
                value=83,
                unit="%",
                source="todo:5",
            )
        msg = str(excinfo.value)
        assert "todo" in msg
        assert "paper" in msg
        assert "patent" in msg
        assert "datasheet" in msg


# ── unknown property mint ─────────────────────────────────────────────


class TestProposedMint:
    def test_unknown_property_with_value_mints_proposed(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        h.put(
            id="fischer-etoac",
            property="turnover_number",
            value=1200,
            unit="1/h",
        )
        prop = store.rxn_property_get("turnover_number")
        assert prop is not None
        assert prop["status"] == "proposed"
        resp = h.get(view="properties")
        assert "turnover_number" in resp.body
        assert "proposed" in resp.body

    def test_unknown_property_without_value_raises_not_found(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        with pytest.raises(NotFound):
            h.put(id="fischer-etoac", property="never_heard_of_it", value=None)


# ── get: view / id validation ───────────────────────────────────────────


class TestGet:
    def test_unknown_view_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        with pytest.raises(BadInput):
            h.get(view="bogus")

    def test_missing_id_raises_not_found(self, store: Any) -> None:
        h = _handler(store)
        with pytest.raises(NotFound):
            h.get(id="no-such-rxn")


# ── reaction page: head + source-cell formatting ────────────────────────


class TestPageHead:
    def test_page_shows_reaction_class_when_set(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER, reaction_class="RXNO:0000024")
        body = h.get(id="fischer-etoac").body
        assert "class: RXNO:0000024" in body

    def test_page_warns_when_identity_keys_absent(self, store: Any) -> None:
        # Bypass put()/rdkit entirely: write the entity meta directly with no
        # uid_transform, the shape a degraded (no-rdkit) write leaves behind.
        store.rxn_entity_upsert(
            slug="no-keys-rxn",
            title="No keys",
            meta_patch={"rxn_smiles": "CCO>>CCO"},
        )
        h = _handler(store)
        body = h.get(id="no-keys-rxn").body
        assert "no identity keys" in body


class TestPageSourceCell:
    def test_ref_source_with_chunk_renders_kind_id_and_chunk(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        store.insert_ref(kind="paper", slug="src-paper", title="Src paper")
        h.put(
            id="fischer-etoac",
            property="yield",
            value=83,
            unit="%",
            source="paper:src-paper",
            chunk="pc9",
        )
        body = h.get(id="fischer-etoac").body
        assert "paper:" in body
        assert "~pc9" in body

    def test_url_only_source_renders_url_in_page(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        h.put(
            id="fischer-etoac",
            property="yield",
            value=83,
            unit="%",
            source="https://example.com/data",
        )
        body = h.get(id="fischer-etoac").body
        assert "https://example.com/data" in body


# ── search ──────────────────────────────────────────────────────────


class TestSearch:
    def test_property_range_search_returns_high_yield_only(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        h.put(id="fischer-etoac", property="yield", value=91, unit="%")
        h.put(id="low-yield-rxn", rxn_smiles="CCO.CCBr>>CCOCC")
        h.put(id="low-yield-rxn", property="yield", value=42, unit="%")

        resp = h.search(property="yield", min=80)
        assert "91" in resp.body
        assert "42" not in resp.body

    def test_q_search_matches_title_and_smiles(self, store: Any) -> None:
        h = _handler(store)
        h.put(
            id="fischer-etoac",
            title="Fischer esterification -> ethyl acetate",
            rxn_smiles=_FISCHER,
        )
        resp = h.search(q="Fischer esterification")
        assert "fischer-etoac" in resp.body or "Fischer" in resp.body

        resp2 = h.search(q="CCO")
        assert "fischer-etoac" in resp2.body or "Fischer" in resp2.body

    def test_search_requires_q_or_property(self, store: Any) -> None:
        h = _handler(store)
        with pytest.raises(BadInput):
            h.search()

    def test_q_search_with_no_hits_says_so(self, store: Any) -> None:
        h = _handler(store)
        resp = h.search(q="nonexistent-reaction-xyz")
        assert "no rxn matches" in resp.body

    def test_search_unknown_property_raises_not_found(self, store: Any) -> None:
        h = _handler(store)
        with pytest.raises(NotFound):
            h.search(property="not-a-real-property")

    def test_search_invalid_maturity_is_rejected(self, store: Any) -> None:
        h = _handler(store)
        with pytest.raises(BadInput):
            h.search(property="yield", maturity="bogus")

    def test_search_property_with_no_matches_says_so(self, store: Any) -> None:
        h = _handler(store)
        resp = h.search(property="yield")
        assert "no yield values match" in resp.body

    def test_search_reaction_class_appears_in_header(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER, reaction_class="RXNO:0000024")
        h.put(id="fischer-etoac", property="yield", value=83, unit="%")
        resp = h.search(property="yield", reaction_class="RXNO:0000024")
        assert "RXNO:0000024" in resp.body


class TestSmilesAreMarkdownSafe:
    """A SMILES must not be readable as markdown when rendered.

    ``C[C@H](N)C(=O)O`` (alanine) matches GFM's inline-link grammar
    ``[text](target)`` — a stereocentre followed by a branch, which describes a
    large share of drug-like molecules. Rendered raw it becomes a *link* with
    text ``C@H`` pointing at ``N``, and the structure silently vanishes from
    what the agent reads. The same collision reddened this repo's doc-link test.
    """

    #: alanine — stereocentre immediately followed by a branch
    CHIRAL = "C[C@H](N)C(=O)O.OCC>>C[C@H](N)C(=O)OCC.O"

    @staticmethod
    def _has_md_link(text: str) -> bool:
        """Is there an inline link OUTSIDE a code span?

        Code spans must be stripped first, because that is exactly what a
        markdown renderer does — `[C@H](N)` inside backticks is literal text,
        and counting it would make the escaping look broken when it is working.
        """
        import re

        outside_code = re.sub(r"`[^`]*`", "", text)
        return bool(re.search(r"\[[^\]]*\]\([^)]*\)", outside_code))

    @needs_rdkit
    def test_put_response_does_not_emit_a_markdown_link(self, store: Any) -> None:
        h = _handler(store)
        body = h.put(id="ala-ester", rxn_smiles=self.CHIRAL).body
        assert "`" in body, "SMILES should be rendered inside a code span"
        assert not self._has_md_link(body), body

    @needs_rdkit
    def test_page_and_list_do_not_emit_a_markdown_link(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="ala-ester", rxn_smiles=self.CHIRAL)
        assert not self._has_md_link(h.get(id="ala-ester").body)
        assert not self._has_md_link(h.get().body)

    @needs_rdkit
    def test_stored_smiles_stays_bare(self, store: Any) -> None:
        """Escaping is display-only — what we persist must remain the plain
        canonical string, or rdkit and the identity keys break."""
        h = _handler(store)
        h.put(id="ala-ester", rxn_smiles=self.CHIRAL)
        ref = store.get_ref(kind="rxn", id="ala-ester")
        assert ref is not None
        assert "`" not in ref.meta["rxn_smiles"]
        assert "[C@H]" in ref.meta["rxn_smiles"]

    @needs_rdkit
    def test_condition_values_are_also_escaped(self, store: Any) -> None:
        """Conditions carry chemistry too. Pd[P(t-Bu)3](OAc)2 is a real ligand
        notation and collides with the inline-link grammar exactly as a SMILES
        stereocentre does - measured before the fix."""
        h = _handler(store)
        h.put(id="ala-ester", rxn_smiles=self.CHIRAL)
        h.put(
            id="ala-ester",
            property="yield",
            value=77,
            unit="%",
            conditions={"catalyst": "Pd[P(t-Bu)3](OAc)2", "solvent": "toluene"},
        )
        body = h.get(id="ala-ester").body
        assert "Pd[P(t-Bu)3](OAc)2" in body, body
        assert not self._has_md_link(body), body


# ── store ops: axis validation + explicit-connection branches ──────────


class TestStoreOpsDirect:
    def test_find_by_uid_rejects_unknown_axis(self, store: Any) -> None:
        with pytest.raises(ValueError):
            store.rxn_find_by_uid("some-uid", axis="bogus")

    def test_property_get_accepts_an_explicit_connection(self, store: Any) -> None:
        with store.pool.connection() as conn:
            prop = store.rxn_property_get("yield", conn=conn)
        assert prop is not None
        assert prop["prop_id"] == "yield"

    def test_property_mint_accepts_an_explicit_connection(self, store: Any) -> None:
        with store.pool.connection() as conn:
            prop = store.rxn_property_mint(
                prop_id="custom_metric",
                name="Custom Metric",
                canonical_unit=None,
                dimension="dimensionless",
                value_type="text",
                conn=conn,
            )
            conn.commit()
        assert prop["prop_id"] == "custom_metric"
        assert store.rxn_property_get("custom_metric") is not None

    def test_search_values_filters_by_reaction_class(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="rxn-a", rxn_smiles=_FISCHER, reaction_class="RXNO:0000024")
        h.put(id="rxn-a", property="yield", value=80, unit="%")
        h.put(id="rxn-b", rxn_smiles="CCO.CCBr>>CCOCC", reaction_class="RXNO:9999999")
        h.put(id="rxn-b", property="yield", value=50, unit="%")

        rows = store.rxn_search_values(
            property_id="yield", reaction_class="RXNO:0000024"
        )
        assert len(rows) == 1
        assert rows[0]["value_num"] == 80
        assert rows[0]["rxn_title"]


# ── wire-level: search(kind='rxn', reaction_class=…) through precis.tools.core
# ---------------------------------------------------------------------------
#
# Mirrors test_component.py's ``test_mcp_get_tool_carries_spec_to_bom_
# consistency_query`` — the tool-function layer builds its own payload dict
# and only forwards ``reaction_class=`` into it when set; a call that never
# goes through this layer can't prove the forwarding line runs.


def test_mcp_search_tool_carries_reaction_class_to_handler(
    monkeypatch: Any, hub: Hub, runtime_with_store: Any
) -> None:
    import precis.tools.core as core

    monkeypatch.setattr(core, "_runtime", runtime_with_store)

    h = RxnHandler(hub=hub)
    h.put(id="fischer-etoac", rxn_smiles=_FISCHER, reaction_class="RXNO:0000024")
    h.put(id="fischer-etoac", property="yield", value=83, unit="%")

    out = core.search(kind="rxn", property="yield", reaction_class="RXNO:0000024")
    assert isinstance(out, str)
    assert "RXNO:0000024" in out


class TestPropertyRowMapping:
    """The registry row->dict mapping in ``_rxn_ops._row_to_property`` is a
    hand-maintained positional index list. Mutation testing showed the
    odd-numbered fields (name, dimension, allowed_values) were unasserted: a
    one-column drift in the SELECT list would silently mis-assign them and no
    test would notice. Mint one property with a DISTINCT value per field and
    check every field individually."""

    def test_every_property_field_round_trips_to_its_own_column(
        self, store: Any
    ) -> None:
        h = _handler(store)
        h.put(id="fischer-etoac", rxn_smiles=_FISCHER)
        # Mint a proposed categorical: exercises name/dimension/allowed_values
        # together, which are the three that survived mutation.
        h.put(
            id="fischer-etoac",
            property="workup_method",
            value="aqueous",
            value_type="categorical",
            allowed_values=["aqueous", "chromatography"],
        )
        p = store.rxn_property_get("workup_method")
        assert p is not None
        assert p["prop_id"] == "workup_method"
        assert p["name"] == "Workup Method"
        assert p["canonical_unit"] is None
        assert p["dimension"] == "categorical"
        assert p["value_type"] == "categorical"
        assert sorted(p["allowed_values"]) == ["aqueous", "chromatography"]
        assert p["status"] == "proposed"

    def test_seeded_quantity_property_fields_are_distinct(self, store: Any) -> None:
        """A core quantity property — different shape from the categorical
        above, so a mapping that happened to work for one is still caught."""
        p = store.rxn_property_get("temperature")
        assert p is not None
        assert p["prop_id"] == "temperature"
        assert p["name"] == "Temperature"
        assert p["canonical_unit"] == "K"
        assert p["dimension"] == "temperature"
        assert p["value_type"] == "quantity"
        assert p["allowed_values"] is None
        assert p["status"] == "core"
