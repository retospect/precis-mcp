"""End-to-end workbench tests — put → fork → edit → commit, no handler."""

from __future__ import annotations

import pytest
from ase.build import bulk, fcc111

from precis_dft.structures import canonical_poscar
from precis_dft.workbench import (
    DraftStructure,
    FrozenStructure,
    apply_edit,
    commit_draft,
    fork_draft,
    register_atoms,
    register_structure,
    view_toc,
    views_for,
)

# ── register_structure ────────────────────────────────────────────


class TestRegisterStructure:
    def test_returns_frozen_structure(self) -> None:
        atoms = bulk("Pt", "fcc", a=3.92)
        frozen = register_atoms(atoms)
        assert isinstance(frozen, FrozenStructure)
        assert frozen.id.startswith("structure:")
        assert frozen.sha == frozen.id.removeprefix("structure:")
        assert frozen.n_atoms == 1
        assert frozen.formula == "Pt"
        assert frozen.composition == {"Pt": 1}
        assert frozen.dimensionality == "bulk"

    def test_same_atoms_same_id(self) -> None:
        a = bulk("Pt", "fcc", a=3.92)
        b = bulk("Pt", "fcc", a=3.92)
        assert register_atoms(a).id == register_atoms(b).id

    def test_different_atoms_different_id(self) -> None:
        pt = register_atoms(bulk("Pt", "fcc", a=3.92))
        ni = register_atoms(bulk("Ni", "fcc", a=3.52))
        assert pt.id != ni.id

    def test_register_from_poscar_text(self) -> None:
        poscar = canonical_poscar(bulk("Pt", "fcc", a=3.92))
        frozen = register_structure(poscar)
        assert frozen.formula == "Pt"


# ── fork_draft ─────────────────────────────────────────────────────


class TestForkDraft:
    def test_fork_inherits_parent_poscar(self) -> None:
        parent = register_atoms(fcc111("Pt", size=(2, 2, 3), vacuum=10.0))
        draft = fork_draft(parent)
        assert isinstance(draft, DraftStructure)
        assert draft.id.startswith("draft:")
        assert draft.parent_id == parent.id
        assert draft.poscar == parent.poscar
        assert draft.edit_log == []
        assert draft.view_version == 0
        assert draft.views_pending is True

    def test_two_forks_have_distinct_ids(self) -> None:
        parent = register_atoms(bulk("Pt", "fcc", a=3.92))
        d1 = fork_draft(parent)
        d2 = fork_draft(parent)
        assert d1.id != d2.id


# ── apply_edit ─────────────────────────────────────────────────────


class TestApplyEdit:
    def test_set_species_returns_mutated_draft(self) -> None:
        parent = register_atoms(bulk("Pt", "fcc", a=3.92).repeat((2, 2, 2)))
        draft = fork_draft(parent)
        result = apply_edit(
            draft,
            [{"set_species": {"site": 0, "element": "Ni"}}],
            msg="swap one Pt for Ni",
        )
        assert isinstance(result, DraftStructure)
        assert "Ni" in result.poscar
        assert result.view_version == 1
        assert len(result.edit_log) == 1
        assert result.edit_log[0]["msg"] == "swap one Pt for Ni"
        assert result.views_pending is True

    def test_combinatorial_returns_list_of_siblings(self) -> None:
        parent = register_atoms(bulk("Pt", "fcc", a=3.92).repeat((2, 2, 2)))
        draft = fork_draft(parent)
        result = apply_edit(
            draft,
            [
                {
                    "substitute": {
                        "equivalence_class": "Pt",
                        "fraction": 0.125,
                        "element": "Ni",
                        "enumerate": "all",
                        "max_siblings": 16,
                    }
                }
            ],
        )
        assert isinstance(result, list)
        assert len(result) >= 1
        for sibling in result:
            assert isinstance(sibling, DraftStructure)
            # All siblings share the same parent.
            assert sibling.parent_id == parent.id
            # And the edit_log carries the combinatorial op.
            assert sibling.view_version == 1

    def test_two_edits_chain(self) -> None:
        parent = register_atoms(bulk("Pt", "fcc", a=3.92).repeat((2, 2, 2)))
        draft = fork_draft(parent)
        result1 = apply_edit(
            draft, [{"set_species": {"site": 0, "element": "Ni"}}], msg="first"
        )
        assert isinstance(result1, DraftStructure)
        result2 = apply_edit(
            result1, [{"set_species": {"site": 1, "element": "Cu"}}], msg="second"
        )
        assert isinstance(result2, DraftStructure)
        assert result2.view_version == 2
        assert len(result2.edit_log) == 2
        assert result2.edit_log[0]["msg"] == "first"
        assert result2.edit_log[1]["msg"] == "second"
        # Both elements present in the final poscar.
        assert "Ni" in result2.poscar
        assert "Cu" in result2.poscar


# ── commit_draft ───────────────────────────────────────────────────


class TestCommitDraft:
    def test_commit_returns_frozen_and_ops_payload(self) -> None:
        parent = register_atoms(bulk("Pt", "fcc", a=3.92).repeat((2, 2, 2)))
        draft = fork_draft(parent)
        edited = apply_edit(
            draft, [{"set_species": {"site": 0, "element": "Ni"}}], msg="swap"
        )
        assert isinstance(edited, DraftStructure)
        commit = commit_draft(edited, msg="commit the swap")

        assert isinstance(commit.frozen, FrozenStructure)
        assert commit.frozen.id.startswith("structure:")
        assert commit.frozen.id != parent.id  # mutation changed the structure

        # The link payload connects child to parent via the op list.
        assert commit.ops_payload["ops"][0]["set_species"] == {
            "site": 0,
            "element": "Ni",
        }
        assert commit.ops_payload["messages"] == ["swap"]
        assert commit.ops_payload["msg"] == "commit the swap"
        assert commit.ops_payload["view_version"] == 1

    def test_commit_with_no_edits_returns_parent_id(self) -> None:
        """A draft that was forked but never edited commits back to
        the parent's content-addressed id — the structure didn't
        actually change."""
        parent = register_atoms(bulk("Pt", "fcc", a=3.92))
        draft = fork_draft(parent)
        commit = commit_draft(draft)
        assert commit.frozen.id == parent.id


# ── views_for / view_toc ───────────────────────────────────────────


class TestViews:
    def test_views_for_returns_full_annotation(self) -> None:
        poscar = canonical_poscar(fcc111("Pt", size=(2, 2, 3), vacuum=10.0))
        views = views_for(poscar)
        # Same shape as annotate().
        for key in ("header", "sites", "graph", "special_sites", "symmetry"):
            assert key in views

    def test_view_toc_drops_heavy_fields(self) -> None:
        poscar = canonical_poscar(fcc111("Pt", size=(2, 2, 3), vacuum=10.0))
        toc = view_toc(poscar)
        assert "header" in toc
        assert "ascii_top" in toc
        assert "n_sites" in toc
        # Heavy fields elided.
        assert "sites" not in toc
        assert "graph" not in toc


# ── Full workflow ──────────────────────────────────────────────────


class TestFullWorkflow:
    def test_put_fork_edit_commit_round_trip(self) -> None:
        """The full LLM workbench loop.

        1. put: register a Pt(111) slab → frozen:<sha1>
        2. fork: draft from frozen
        3. edit: replace one atom
        4. commit: produce frozen:<sha2> with derived_from link payload
        5. assertion: the derivation-tree link points from sha2 to sha1
           with the op list inline.
        """
        # 1
        slab = fcc111("Pt", size=(2, 2, 3), vacuum=10.0)
        parent = register_atoms(slab)
        assert parent.dimensionality == "slab"

        # 2
        draft = fork_draft(parent)
        assert draft.parent_id == parent.id

        # 3
        edited = apply_edit(
            draft,
            [{"set_species": {"site": 0, "element": "Ni"}}],
            msg="introduce one Ni at site 0",
        )
        assert isinstance(edited, DraftStructure)

        # 4
        commit = commit_draft(edited, msg="ship Pt-Ni surface variant")
        assert commit.frozen.id != parent.id

        # 5: the link's metadata is enough to walk the lineage.
        assert commit.ops_payload["ops"] == [
            {"set_species": {"site": 0, "element": "Ni"}}
        ]
        assert commit.ops_payload["messages"] == ["introduce one Ni at site 0"]

    def test_substitute_enumerate_workflow_produces_family(self) -> None:
        """Combinatorial substitution → family of frozen structures
        sharing a derivation parent. This is how the campaign's
        screen phase fans out alloy candidates."""
        parent = register_atoms(bulk("Pt", "fcc", a=3.92).repeat((2, 2, 2)))
        draft = fork_draft(parent)
        siblings_drafts = apply_edit(
            draft,
            [
                {
                    "substitute": {
                        "equivalence_class": "Pt",
                        "fraction": 0.25,  # 25 % → 2 Ni out of 8 Pt
                        "element": "Ni",
                        "enumerate": "all",
                        "max_siblings": 16,
                    }
                }
            ],
            msg="enumerate 25%% Ni substitution",
        )
        assert isinstance(siblings_drafts, list)
        assert len(siblings_drafts) > 1

        # Commit each sibling.
        commits = [commit_draft(s) for s in siblings_drafts]
        frozen_ids = {c.frozen.id for c in commits}
        # Without symmetry reduction the siblings may collapse onto
        # the same frozen sha if the substitution patterns are
        # equivalent — at minimum we get one, at maximum we get
        # one per draft sibling.
        assert 1 <= len(frozen_ids) <= len(siblings_drafts)

        # Every commit points back to the same parent.
        for c in commits:
            assert c.frozen.id != parent.id  # NOT the parent (we substituted)
            # The link from each frozen sibling back to parent
            # carries the substitute op.
            assert c.ops_payload["ops"][0]["substitute"]["element"] == "Ni"


# ── Determinism ────────────────────────────────────────────────────


class TestDeterminism:
    def test_same_workflow_same_frozen_id(self) -> None:
        """Running the same fork-edit-commit twice produces the same
        ``structure:<sha>``. The content-addressing guarantees
        deduplication in the store."""
        atoms = bulk("Pt", "fcc", a=3.92).repeat((2, 2, 2))

        def _do_workflow() -> str:
            parent = register_atoms(atoms)
            draft = fork_draft(parent)
            edited = apply_edit(draft, [{"set_species": {"site": 0, "element": "Ni"}}])
            assert isinstance(edited, DraftStructure)
            return commit_draft(edited).frozen.id

        first = _do_workflow()
        second = _do_workflow()
        assert first == second

    def test_different_drafts_distinct_uuids(self) -> None:
        """Two drafts from the same parent are distinguishable by
        their uuid even though their content is identical at fork
        time. Important — two LLM sessions forking the same
        material shouldn't step on each other's draft."""
        with_caplog = pytest.warns(UserWarning) if False else None
        parent = register_atoms(bulk("Pt", "fcc", a=3.92))
        ids = {fork_draft(parent).id for _ in range(10)}
        assert len(ids) == 10
