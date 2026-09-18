"""Structure + StructureDraft handlers: full workflow against the mock store.

The handlers are thin shims over :mod:`precis_dft.workbench` and
the precis-mcp store. Tests use :class:`precis_dft._test_store.TestStore`
— an in-memory mock — so the workflow runs without a database.
"""

from __future__ import annotations

import pytest
from ase.build import bulk, fcc111

from precis_dft._test_store import TestStore
from precis_dft.handlers.structure import StructureHandler
from precis_dft.handlers.structure_draft import StructureDraftHandler
from precis_dft.structures import canonical_poscar


@pytest.fixture
def store() -> TestStore:
    return TestStore()


@pytest.fixture
def structure_handler(store: TestStore) -> StructureHandler:
    h = StructureHandler(hub=None)
    h.store = store  # type: ignore[attr-defined]
    return h


@pytest.fixture
def draft_handler(store: TestStore) -> StructureDraftHandler:
    h = StructureDraftHandler(hub=None)
    h.store = store  # type: ignore[attr-defined]
    return h


# ── Structure handler ────────────────────────────────────────────


class TestStructurePut:
    def test_put_registers_a_frozen_structure(
        self,
        structure_handler: StructureHandler,
        store: TestStore,
    ) -> None:
        poscar = canonical_poscar(bulk("Pt", "fcc", a=3.92))
        resp = structure_handler.put(poscar=poscar)
        assert "created structure" in resp.body
        # And the store now has it.
        assert any(ref.kind == "structure" for ref in store._refs.values())

    def test_put_dedupes_on_sha(
        self,
        structure_handler: StructureHandler,
        store: TestStore,
    ) -> None:
        poscar = canonical_poscar(bulk("Pt", "fcc", a=3.92))
        resp1 = structure_handler.put(poscar=poscar)
        resp2 = structure_handler.put(poscar=poscar)
        assert "created structure" in resp1.body
        assert "already exists" in resp2.body
        # Only one ref in the store.
        struct_refs = [r for r in store._refs.values() if r.kind == "structure"]
        assert len(struct_refs) == 1

    def test_put_rejects_missing_poscar(
        self, structure_handler: StructureHandler
    ) -> None:
        resp = structure_handler.put()
        assert "requires poscar" in resp.body

    def test_put_rejects_bad_poscar(self, structure_handler: StructureHandler) -> None:
        resp = structure_handler.put(poscar="this is not a poscar at all")
        assert "failed to parse" in resp.body


class TestStructureGet:
    def test_get_returns_summary(
        self,
        structure_handler: StructureHandler,
    ) -> None:
        poscar = canonical_poscar(bulk("Pt", "fcc", a=3.92))
        structure_handler.put(poscar=poscar)
        # Find the id from the put response.
        from precis_dft.workbench import register_structure

        sid = register_structure(poscar).id

        resp = structure_handler.get(id=sid)
        assert "Pt" in resp.body
        assert "n_atoms" in resp.body

    def test_get_view_toc(self, structure_handler: StructureHandler) -> None:
        poscar = canonical_poscar(fcc111("Pt", size=(2, 2, 3), vacuum=10.0))
        structure_handler.put(poscar=poscar)
        from precis_dft.workbench import register_structure

        sid = register_structure(poscar).id

        resp = structure_handler.get(id=sid, view="toc")
        # TOC includes the header dict — should mention Pt + slab.
        assert "slab" in resp.body

    def test_get_unknown_id(self, structure_handler: StructureHandler) -> None:
        resp = structure_handler.get(id="structure:nonexistent")
        assert "not found" in resp.body


# ── Draft handler ────────────────────────────────────────────────


class TestDraftFork:
    def test_fork_from_existing_structure(
        self,
        structure_handler: StructureHandler,
        draft_handler: StructureDraftHandler,
    ) -> None:
        # First register the parent.
        poscar = canonical_poscar(bulk("Pt", "fcc", a=3.92))
        structure_handler.put(poscar=poscar)
        from precis_dft.workbench import register_structure

        parent_id = register_structure(poscar).id

        # Now fork.
        resp = draft_handler.put(**{"from": parent_id})
        assert "forked draft" in resp.body
        assert "draft:" in resp.body

    def test_fork_from_missing_parent_fails(
        self, draft_handler: StructureDraftHandler
    ) -> None:
        resp = draft_handler.put(**{"from": "structure:nope"})
        assert "not found" in resp.body


class TestDraftEditAndCommit:
    def _fork(
        self,
        structure_handler: StructureHandler,
        draft_handler: StructureDraftHandler,
    ) -> tuple[str, str]:
        """Helper: register a Pt2x2x2 supercell + fork a draft.

        Returns (parent_id, draft_id).
        """
        from precis_dft.workbench import register_structure

        poscar = canonical_poscar(bulk("Pt", "fcc", a=3.92).repeat((2, 2, 2)))
        structure_handler.put(poscar=poscar)
        parent_id = register_structure(poscar).id
        resp = draft_handler.put(**{"from": parent_id})
        # Pull the draft id out of the response.
        draft_id = next(
            tok for tok in resp.body.split() if tok.startswith("id=draft:")
        ).removeprefix("id=")
        return parent_id, draft_id

    def test_edit_applies_one_op(
        self,
        structure_handler: StructureHandler,
        draft_handler: StructureDraftHandler,
        store: TestStore,
    ) -> None:
        _parent_id, draft_id = self._fork(structure_handler, draft_handler)
        resp = draft_handler.edit(
            id=draft_id,
            ops=[{"set_species": {"site": 0, "element": "Ni"}}],
            msg="swap one Pt for Ni",
        )
        assert "applied 1 op" in resp.body
        # And the meta on the draft ref carries the new POSCAR.
        draft_ref = store.fetch_ref_by_slug("structure_draft", draft_id)
        assert draft_ref is not None
        assert "Ni" in draft_ref.meta["poscar"]

    def test_commit_promotes_to_frozen_and_links(
        self,
        structure_handler: StructureHandler,
        draft_handler: StructureDraftHandler,
        store: TestStore,
    ) -> None:
        parent_id, draft_id = self._fork(structure_handler, draft_handler)
        draft_handler.edit(
            id=draft_id,
            ops=[{"set_species": {"site": 0, "element": "Ni"}}],
            msg="swap",
        )
        resp = draft_handler.edit(id=draft_id, mode="commit", msg="ship it")
        assert "committed draft" in resp.body
        assert "frozen structure:" in resp.body

        # Verify the derived_from link landed.
        frozen_refs = [
            r
            for r in store._refs.values()
            if r.kind == "structure" and r.slug != parent_id
        ]
        assert len(frozen_refs) == 1
        frozen = frozen_refs[0]
        links = store.links_for(frozen.ref_id, direction="out", relation="derived_from")
        assert len(links) == 1
        link = links[0]
        # The link's meta carries the ops.
        assert link.meta["ops"] == [{"set_species": {"site": 0, "element": "Ni"}}]
        assert link.meta["messages"] == ["swap"]
        assert link.meta["msg"] == "ship it"

        # And the draft was soft-deleted.
        assert store.fetch_ref_by_slug("structure_draft", draft_id) is None

    def test_combinatorial_edit_creates_sibling_drafts(
        self,
        structure_handler: StructureHandler,
        draft_handler: StructureDraftHandler,
        store: TestStore,
    ) -> None:
        _parent_id, draft_id = self._fork(structure_handler, draft_handler)
        resp = draft_handler.edit(
            id=draft_id,
            ops=[
                {
                    "substitute": {
                        "equivalence_class": "Pt",
                        "fraction": 0.125,
                        "element": "Ni",
                        "enumerate": "all",
                        "max_siblings": 8,
                    }
                }
            ],
        )
        assert "sibling drafts" in resp.body
        # The store now has the original draft + N siblings.
        draft_refs = [
            r
            for r in store._refs.values()
            if r.kind == "structure_draft" and not r.deleted
        ]
        assert len(draft_refs) > 1


class TestDraftGet:
    def test_get_summary(
        self,
        structure_handler: StructureHandler,
        draft_handler: StructureDraftHandler,
    ) -> None:
        from precis_dft.workbench import register_structure

        poscar = canonical_poscar(bulk("Pt", "fcc", a=3.92))
        structure_handler.put(poscar=poscar)
        parent_id = register_structure(poscar).id
        resp = draft_handler.put(**{"from": parent_id})
        draft_id = next(
            tok for tok in resp.body.split() if tok.startswith("id=draft:")
        ).removeprefix("id=")

        resp = draft_handler.get(id=draft_id)
        assert "parent" in resp.body
        assert "edits applied" in resp.body


# ── End-to-end ────────────────────────────────────────────────────


class TestFullLLMWorkflow:
    def test_put_fork_edit_commit_walks_tree(
        self,
        structure_handler: StructureHandler,
        draft_handler: StructureDraftHandler,
        store: TestStore,
    ) -> None:
        """The whole LLM workbench loop end-to-end.

        1. put: register a Pt(111) slab.
        2. fork: draft from it.
        3. edit: apply set_species.
        4. commit: promote to a new frozen structure with a
           ``derived_from`` link.
        5. assertion: the link payload lets us walk parent ↔ child
           and recover the op list that produced the child.
        """
        from precis_dft.workbench import register_structure

        slab_poscar = canonical_poscar(fcc111("Pt", size=(2, 2, 3), vacuum=10.0))
        structure_handler.put(poscar=slab_poscar)
        parent_id = register_structure(slab_poscar).id

        fork_resp = draft_handler.put(**{"from": parent_id})
        draft_id = next(
            tok for tok in fork_resp.body.split() if tok.startswith("id=draft:")
        ).removeprefix("id=")

        draft_handler.edit(
            id=draft_id,
            ops=[{"set_species": {"site": 0, "element": "Ni"}}],
            msg="introduce Ni",
        )

        commit_resp = draft_handler.edit(
            id=draft_id, mode="commit", msg="ship Pt-Ni slab"
        )
        assert "committed draft" in commit_resp.body

        # Walk the derivation tree from the child back to the parent.
        child_refs = [
            r
            for r in store._refs.values()
            if r.kind == "structure" and r.slug != parent_id
        ]
        assert len(child_refs) == 1
        child = child_refs[0]
        parent_ref = store.fetch_ref_by_slug("structure", parent_id)
        assert parent_ref is not None

        links = store.links_for(child.ref_id, direction="out", relation="derived_from")
        assert len(links) == 1
        assert links[0].dst_ref_id == parent_ref.ref_id
        # The op that produced the child is right there on the link.
        assert links[0].meta["ops"] == [{"set_species": {"site": 0, "element": "Ni"}}]
