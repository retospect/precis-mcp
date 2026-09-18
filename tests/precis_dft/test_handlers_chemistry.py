"""Material + DftCalculation + ReactionNetwork handlers, end-to-end.

Same pattern as :mod:`tests.test_handlers_workflow` — mock store +
the handler shims + the workbench / reactions modules running for
real.
"""

from __future__ import annotations

import json

import pytest

from precis_dft._test_store import TestStore
from precis_dft.handlers.dft_calculation import DftCalculationHandler
from precis_dft.handlers.material import MaterialHandler
from precis_dft.handlers.reaction_network import ReactionNetworkHandler
from precis_dft.jobs.reaction_evaluate import compute_overpotential


@pytest.fixture
def store() -> TestStore:
    return TestStore()


@pytest.fixture
def calc_handler(store: TestStore) -> DftCalculationHandler:
    h = DftCalculationHandler(hub=None)
    h.store = store  # type: ignore[attr-defined]
    return h


@pytest.fixture
def material_handler(store: TestStore) -> MaterialHandler:
    h = MaterialHandler(hub=None)
    h.store = store  # type: ignore[attr-defined]
    return h


@pytest.fixture
def network_handler() -> ReactionNetworkHandler:
    return ReactionNetworkHandler(hub=None)


# ── dft_calculation handler ──────────────────────────────────────


class TestDftCalculationHandler:
    def test_put_and_get(self, calc_handler: DftCalculationHandler) -> None:
        resp = calc_handler.put(
            id="calc:job_201",
            structure="structure:abc123",
            scalars={"E_tot": -123.45, "converged": True, "max_force": 0.012},
            settings={"functional": "RPBE"},
            species="*OH",
        )
        assert "created calc" in resp.body
        get_resp = calc_handler.get(id="calc:job_201")
        assert "E_tot" in get_resp.body
        assert "*OH" in get_resp.body

    def test_put_rejects_missing_required(
        self, calc_handler: DftCalculationHandler
    ) -> None:
        # Missing id.
        assert (
            "requires id"
            in calc_handler.put(structure="structure:abc", scalars={}).body
        )
        # Missing structure.
        assert "requires structure" in calc_handler.put(id="calc:foo", scalars={}).body
        # Missing scalars.
        assert (
            "requires scalars"
            in calc_handler.put(id="calc:foo", structure="structure:abc").body
        )

    def test_dedup_on_id(self, calc_handler: DftCalculationHandler) -> None:
        params = dict(
            id="calc:dup",
            structure="structure:abc",
            scalars={"E_tot": -1.0},
        )
        calc_handler.put(**params)
        resp = calc_handler.put(**params)
        assert "already exists" in resp.body


# ── material handler ─────────────────────────────────────────────


class TestMaterialHandler:
    def test_put_and_get(self, material_handler: MaterialHandler) -> None:
        resp = material_handler.put(
            id="material:Pt111",
            composition={"Pt": 16},
            phase="fcc",
            prototype="111-slab",
            canonical={"eads_ref_scheme": "norskov_water_h2"},
        )
        assert "created material" in resp.body
        get_resp = material_handler.get(id="material:Pt111")
        assert "fcc" in get_resp.body
        assert "Pt16" in get_resp.body

    def test_edit_merges_fields(self, material_handler: MaterialHandler) -> None:
        material_handler.put(
            id="material:Pt111",
            composition={"Pt": 16},
            phase="fcc",
        )
        resp = material_handler.edit(
            id="material:Pt111",
            calculations=["calc:job_201", "calc:job_202"],
        )
        assert "updated material" in resp.body
        view = material_handler.get(id="material:Pt111")
        assert "n_calcs:        2" in view.body

    def test_write_reaction_eval_lands_in_chunks(
        self,
        store: TestStore,
        material_handler: MaterialHandler,
    ) -> None:
        material_handler.put(
            id="material:Pt111",
            composition={"Pt": 16},
            phase="fcc",
        )
        record = {
            "network_id": "reaction:oer_4step_acid",
            "conditions_hash": "abc",
            "overpotential": 0.66,
            "rate_determining_step": 2,
            "step_dG": [0.71, 1.03, 2.10, 1.08],
        }
        resp = material_handler.edit(
            id="material:Pt111",
            mode="write_reaction_eval",
            record=record,
        )
        assert "η=0.660" in resp.body

        # The chunk is present.
        ref = store.fetch_ref_by_slug("material", "material:Pt111")
        assert ref is not None
        chunks = store.list_blocks_for_ref(ref.ref_id)
        eval_chunks = [c for c in chunks if c.chunk_kind == "reaction_eval"]
        assert len(eval_chunks) == 1
        # And the body round-trips.
        parsed = json.loads(eval_chunks[0].text)
        assert parsed["overpotential"] == 0.66

        # And the derived.reactions mirror is set.
        ref = store.fetch_ref_by_slug("material", "material:Pt111")
        assert ref is not None
        reactions_summary = ref.meta["derived"]["reactions"]
        assert "reaction:oer_4step_acid" in reactions_summary
        assert reactions_summary["reaction:oer_4step_acid"]["overpotential"] == 0.66


# ── reaction_network handler ─────────────────────────────────────


class TestReactionNetworkHandler:
    def test_get_lists_library_without_id(
        self, network_handler: ReactionNetworkHandler
    ) -> None:
        resp = network_handler.get()
        assert "reaction:oer_4step_acid" in resp.body
        assert "reaction:her_volmer_heyrovsky" in resp.body

    def test_get_summary(self, network_handler: ReactionNetworkHandler) -> None:
        resp = network_handler.get(id="reaction:oer_4step_acid")
        assert "OER" in resp.body
        assert "4 elementary step" in resp.body

    def test_get_unknown_id(self, network_handler: ReactionNetworkHandler) -> None:
        resp = network_handler.get(id="reaction:not-a-real-network")
        assert "not found" in resp.body

    def test_search_by_keyword(self, network_handler: ReactionNetworkHandler) -> None:
        resp = network_handler.search(q="OER")
        # All four OER networks should match.
        assert "reaction:oer_4step_acid" in resp.body
        assert "reaction:oer_lom" in resp.body


# ── reaction_evaluate end-to-end ─────────────────────────────────


class TestReactionEvaluateEndToEnd:
    def _build_pt111_oer(
        self,
        store: TestStore,
        calc_handler: DftCalculationHandler,
        material_handler: MaterialHandler,
    ) -> None:
        """Set up a Pt(111) material with OER intermediates as calcs.

        Numbers are illustrative — close to literature Pt(111) Nørskov
        values. The test verifies the wiring and the overpotential
        ordering, not the chemistry accuracy.
        """
        material_handler.put(
            id="material:Pt111",
            composition={"Pt": 16},
            phase="fcc",
            prototype="111-slab",
            canonical={"eads_ref_scheme": "norskov_water_h2"},
        )

        species_to_g = {"*OH": 0.71, "*O": 1.74, "*OOH": 3.84}
        for sp, g in species_to_g.items():
            sp_safe = sp.lstrip("*").lower() or "h"
            calc_id = f"calc:Pt111_{sp_safe}"
            calc_handler.put(
                id=calc_id,
                structure=f"structure:Pt111_with_{sp_safe}",
                scalars={"E_tot": -100.0, "converged": True},
                species=sp,
                derived={"G_che": g},
            )

    def test_full_chain_produces_overpotential(
        self,
        store: TestStore,
        calc_handler: DftCalculationHandler,
        material_handler: MaterialHandler,
        network_handler: ReactionNetworkHandler,
    ) -> None:
        self._build_pt111_oer(store, calc_handler, material_handler)

        # Pull calc records.
        calc_metas: dict[str, dict] = {}
        for species, calc_slug in (
            ("*OH", "calc:Pt111_oh"),
            ("*O", "calc:Pt111_o"),
            ("*OOH", "calc:Pt111_ooh"),
        ):
            ref = store.fetch_ref_by_slug("dft_calculation", calc_slug)
            assert ref is not None
            calc_metas[species] = ref.meta

        material_ref = store.fetch_ref_by_slug("material", "material:Pt111")
        assert material_ref is not None
        material_meta = dict(material_ref.meta)
        material_meta["id"] = "material:Pt111"

        network = network_handler.library()["reaction:oer_4step_acid"]
        record = compute_overpotential(
            network=network,
            material_meta=material_meta,
            calculations=calc_metas,
            conditions={"T": 298, "pH": 0, "U_RHE": 0.0},
        )

        # Step 3 (*O → *OOH) is limiting on Pt(111).
        assert record["rate_determining_step"] == 2
        # Overpotential is positive and reasonable for these numbers.
        assert 0.7 < record["overpotential"] < 1.0
        assert record["missing_intermediates"] == []

        # And we can persist it on the material via the handler.
        write_resp = material_handler.edit(
            id="material:Pt111",
            mode="write_reaction_eval",
            record=record,
        )
        assert "wrote reaction_eval" in write_resp.body

        # Verify the material's reaction summary now carries the η.
        material_ref = store.fetch_ref_by_slug("material", "material:Pt111")
        assert material_ref is not None
        summary = material_ref.meta["derived"]["reactions"]
        assert (
            summary["reaction:oer_4step_acid"]["overpotential"]
            == record["overpotential"]
        )

    def test_missing_intermediates_are_reported(
        self,
        store: TestStore,
        calc_handler: DftCalculationHandler,
        material_handler: MaterialHandler,
        network_handler: ReactionNetworkHandler,
    ) -> None:
        """Skip *OOH so the evaluator reports it as missing."""
        material_handler.put(
            id="material:Pt111",
            composition={"Pt": 16},
            phase="fcc",
            canonical={"eads_ref_scheme": "norskov_water_h2"},
        )
        # Only *OH and *O.
        for sp, g in {"*OH": 0.71, "*O": 1.74}.items():
            sp_safe = sp.lstrip("*").lower()
            calc_handler.put(
                id=f"calc:Pt111_{sp_safe}",
                structure="structure:Pt111",
                scalars={"E_tot": -100.0, "converged": True},
                species=sp,
                derived={"G_che": g},
            )

        calc_metas = {
            "*OH": store.fetch_ref_by_slug("dft_calculation", "calc:Pt111_oh").meta,  # type: ignore[union-attr]
            "*O": store.fetch_ref_by_slug("dft_calculation", "calc:Pt111_o").meta,  # type: ignore[union-attr]
        }
        material_meta = dict(store.fetch_ref_by_slug("material", "material:Pt111").meta)  # type: ignore[union-attr]
        material_meta["id"] = "material:Pt111"
        network = network_handler.library()["reaction:oer_4step_acid"]
        record = compute_overpotential(
            network=network,
            material_meta=material_meta,
            calculations=calc_metas,
        )

        assert "*OOH" in record["missing_intermediates"]
