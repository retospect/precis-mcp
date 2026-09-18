"""Volcano + pathway analyses: end-to-end on a Pt–Ni–Cu family."""

from __future__ import annotations

import pytest

from precis_dft._test_store import TestStore
from precis_dft.handlers.dft_calculation import DftCalculationHandler
from precis_dft.handlers.material import MaterialHandler
from precis_dft.handlers.reaction_network import ReactionNetworkHandler
from precis_dft.jobs.reaction_evaluate import compute_overpotential
from precis_dft.jobs.volcano import (
    PathwayResult,
    VolcanoResult,
    compute,
    pathway_diagram,
)


@pytest.fixture
def store() -> TestStore:
    return TestStore()


def _build_material_with_oer(
    store: TestStore,
    *,
    material_id: str,
    composition: dict[str, int],
    G: dict[str, float],
    network_id: str = "reaction:oer_4step_acid",
) -> None:
    """Set up one material + the three OER intermediate calcs + the
    written reaction_eval chunk. Returns nothing — inspects via the
    store fixture."""
    calc_handler = DftCalculationHandler(hub=None)
    calc_handler.store = store  # type: ignore[attr-defined]
    material_handler = MaterialHandler(hub=None)
    material_handler.store = store  # type: ignore[attr-defined]
    network_handler = ReactionNetworkHandler(hub=None)

    sid = material_id.removeprefix("material:")
    material_handler.put(
        id=material_id,
        composition=composition,
        phase="fcc",
        prototype="111-slab",
        canonical={"eads_ref_scheme": "norskov_water_h2"},
    )

    calc_ids: list[str] = []
    for sp, g in G.items():
        sp_safe = sp.lstrip("*").lower() or "h"
        cid = f"calc:{sid}_{sp_safe}"
        calc_handler.put(
            id=cid,
            structure=f"structure:{sid}_with_{sp_safe}",
            scalars={"E_tot": -100.0, "converged": True},
            species=sp,
            derived={"G_che": g},
        )
        calc_ids.append(cid)
    material_handler.edit(id=material_id, calculations=calc_ids)

    # Compute and persist the reaction_eval chunk.
    calc_metas = {}
    for sp, _ in G.items():
        sp_safe = sp.lstrip("*").lower() or "h"
        cid = f"calc:{sid}_{sp_safe}"
        ref = store.fetch_ref_by_slug("dft_calculation", cid)
        assert ref is not None
        calc_metas[sp] = ref.meta
    material_meta = dict(store.fetch_ref_by_slug("material", material_id).meta)  # type: ignore[union-attr]
    material_meta["id"] = material_id
    network = network_handler.library()[network_id]
    record = compute_overpotential(
        network=network,
        material_meta=material_meta,
        calculations=calc_metas,
        conditions={"T": 298, "pH": 0, "U_RHE": 0.0},
    )
    material_handler.edit(
        id=material_id,
        mode="write_reaction_eval",
        record=record,
    )


def _gather_for_volcano(
    store: TestStore, family_material_ids: list[str]
) -> tuple[
    list[tuple[str, dict, list]],
    dict[str, dict],
]:
    """Build the (materials, calc_lookup) tuple compute() expects.

    Pulls the materials' meta and reaction_eval chunks from the
    store, plus a calc_lookup keyed by calc_id.
    """
    materials = []
    calc_lookup: dict[str, dict] = {}
    for mid in family_material_ids:
        ref = store.fetch_ref_by_slug("material", mid)
        assert ref is not None
        chunks = store.list_blocks_for_ref(ref.ref_id)
        eval_chunks = [c for c in chunks if c.chunk_kind == "reaction_eval"]
        materials.append((mid, dict(ref.meta), eval_chunks))
        for cid in ref.meta.get("calculations", []):
            cref = store.fetch_ref_by_slug("dft_calculation", cid)
            if cref is not None:
                calc_lookup[cid] = dict(cref.meta)
    return materials, calc_lookup


# ── Volcano ───────────────────────────────────────────────────────


class TestVolcano:
    def test_three_materials_produce_three_points(self, store: TestStore) -> None:
        # Three Pt-based materials with slightly different OER intermediates.
        _build_material_with_oer(
            store,
            material_id="material:Pt111",
            composition={"Pt": 16},
            G={"*OH": 0.71, "*O": 1.74, "*OOH": 3.84},
        )
        _build_material_with_oer(
            store,
            material_id="material:Pt3Ni",
            composition={"Pt": 12, "Ni": 4},
            G={"*OH": 0.30, "*O": 1.20, "*OOH": 3.20},
        )
        _build_material_with_oer(
            store,
            material_id="material:Pt3Cu",
            composition={"Pt": 12, "Cu": 4},
            G={"*OH": 1.10, "*O": 2.40, "*OOH": 4.50},
        )

        materials, calc_lookup = _gather_for_volcano(
            store, ["material:Pt111", "material:Pt3Ni", "material:Pt3Cu"]
        )

        result = compute(
            materials=materials,
            calc_lookup=calc_lookup,
            network_id="reaction:oer_4step_acid",
            descriptor="G(*OH)",
            family="Pt-Ni-Cu_alloys",
        )

        assert isinstance(result, VolcanoResult)
        assert len(result.points) == 3
        # Sorted ascending by descriptor.
        x = [p.descriptor_value for p in result.points]
        assert x == sorted(x)
        # All overpotentials are positive numbers.
        for p in result.points:
            assert p.overpotential >= 0

    def test_summary_names_best_material(self, store: TestStore) -> None:
        _build_material_with_oer(
            store,
            material_id="material:Pt111",
            composition={"Pt": 16},
            G={"*OH": 0.71, "*O": 1.74, "*OOH": 3.84},
        )
        _build_material_with_oer(
            store,
            material_id="material:Pt3Ni",
            composition={"Pt": 12, "Ni": 4},
            G={"*OH": 0.60, "*O": 1.55, "*OOH": 3.78},  # closer to ideal 1.23
        )
        materials, calc_lookup = _gather_for_volcano(
            store, ["material:Pt111", "material:Pt3Ni"]
        )
        result = compute(
            materials=materials,
            calc_lookup=calc_lookup,
            network_id="reaction:oer_4step_acid",
            descriptor="G(*OH)",
            family="Pt-Ni",
        )
        best = min(result.points, key=lambda p: p.overpotential)
        assert best.material_id in result.summary

    def test_svg_is_self_contained(self, store: TestStore) -> None:
        _build_material_with_oer(
            store,
            material_id="material:Pt111",
            composition={"Pt": 16},
            G={"*OH": 0.71, "*O": 1.74, "*OOH": 3.84},
        )
        materials, calc_lookup = _gather_for_volcano(store, ["material:Pt111"])
        result = compute(
            materials=materials,
            calc_lookup=calc_lookup,
            network_id="reaction:oer_4step_acid",
            descriptor="G(*OH)",
        )
        assert result.svg.startswith("<svg")
        assert result.svg.endswith("</svg>")
        # No JavaScript, no external refs.
        assert "<script" not in result.svg
        assert "xlink:href" not in result.svg
        # Contains the material id.
        assert "Pt111" in result.svg

    def test_descriptor_parser_accepts_three_forms(self) -> None:
        from precis_dft.jobs.volcano import _species_from_descriptor

        assert _species_from_descriptor("G(*OH)") == "*OH"
        assert _species_from_descriptor("eads(*OH)") == "*OH"
        assert _species_from_descriptor("G:*OH") == "*OH"
        assert _species_from_descriptor("*OH") == "*OH"

    def test_descriptor_parser_rejects_bad_input(self) -> None:
        from precis_dft.jobs.volcano import _species_from_descriptor

        with pytest.raises(ValueError):
            _species_from_descriptor("formaldehyde")

    def test_empty_family_returns_empty_svg(self, store: TestStore) -> None:
        result = compute(
            materials=[],
            calc_lookup={},
            network_id="reaction:oer_4step_acid",
            descriptor="G(*OH)",
        )
        assert result.points == []
        assert "no volcano points" in result.svg


# ── Pathway diagram ──────────────────────────────────────────────


class TestPathway:
    def test_single_material_pathway(self, store: TestStore) -> None:
        _build_material_with_oer(
            store,
            material_id="material:Pt111",
            composition={"Pt": 16},
            G={"*OH": 0.71, "*O": 1.74, "*OOH": 3.84},
        )
        materials, _ = _gather_for_volcano(store, ["material:Pt111"])
        # Adapt to the pathway shape.
        mats = [(mid, eval_chunks) for mid, _, eval_chunks in materials]
        result = pathway_diagram(materials=mats, network_id="reaction:oer_4step_acid")

        assert isinstance(result, PathwayResult)
        assert "material:Pt111" in result.step_dGs_per_material
        # Pt(111) OER has 4 elementary steps.
        assert len(result.step_dGs_per_material["material:Pt111"]) == 4
        # Step 3 (*O → *OOH) is the largest jump.
        steps = result.step_dGs_per_material["material:Pt111"]
        assert steps[2] == max(steps)

    def test_multi_material_pathway_legend(self, store: TestStore) -> None:
        _build_material_with_oer(
            store,
            material_id="material:Pt111",
            composition={"Pt": 16},
            G={"*OH": 0.71, "*O": 1.74, "*OOH": 3.84},
        )
        _build_material_with_oer(
            store,
            material_id="material:Pt3Ni",
            composition={"Pt": 12, "Ni": 4},
            G={"*OH": 0.30, "*O": 1.20, "*OOH": 3.20},
        )
        materials, _ = _gather_for_volcano(store, ["material:Pt111", "material:Pt3Ni"])
        mats = [(mid, eval_chunks) for mid, _, eval_chunks in materials]
        result = pathway_diagram(materials=mats, network_id="reaction:oer_4step_acid")
        # Both materials present in the result.
        assert set(result.step_dGs_per_material) == {
            "material:Pt111",
            "material:Pt3Ni",
        }
        # SVG carries a legend entry for each.
        assert "Pt111" in result.svg
        assert "Pt3Ni" in result.svg

    def test_empty_pathway(self) -> None:
        result = pathway_diagram(materials=[], network_id="reaction:oer_4step_acid")
        assert result.step_dGs_per_material == {}
        assert "no pathway data" in result.svg


# ── conditions_hash matching ──────────────────────────────────────


class TestConditionsHashMatching:
    def test_volcano_filters_by_conditions_hash(self, store: TestStore) -> None:
        # Material with a reaction_eval at conditions A only.
        _build_material_with_oer(
            store,
            material_id="material:Pt111",
            composition={"Pt": 16},
            G={"*OH": 0.71, "*O": 1.74, "*OOH": 3.84},
        )
        materials, calc_lookup = _gather_for_volcano(store, ["material:Pt111"])
        # Read the actual conditions_hash off the chunk so we can
        # supply both a matching and a non-matching value.
        eval_chunks = materials[0][2]
        match_hash = eval_chunks[0].meta["conditions_hash"]
        no_match_hash = "definitely-not-a-real-hash"

        result_match = compute(
            materials=materials,
            calc_lookup=calc_lookup,
            network_id="reaction:oer_4step_acid",
            descriptor="G(*OH)",
            conditions_hash=match_hash,
        )
        assert len(result_match.points) == 1

        result_miss = compute(
            materials=materials,
            calc_lookup=calc_lookup,
            network_id="reaction:oer_4step_acid",
            descriptor="G(*OH)",
            conditions_hash=no_match_hash,
        )
        assert result_miss.points == []
