"""End-to-end annotator smoke tests on real structures.

Run the whole pipeline on a Pt(111) slab, a bulk Pt cell, and a
CO adsorbate on Pt — assert the output shapes and the obvious
high-confidence facts.
"""

from __future__ import annotations

from ase.build import add_adsorbate, bulk, fcc111, molecule

from precis_dft.annotator import VIEW_VERSION, annotate
from precis_dft.annotator.header import (
    detect_dimensionality,
    detect_slab_layers,
    header,
)
from precis_dft.annotator.sites import adjacency, site_table
from precis_dft.annotator.symmetry import analyse

# ── Dimensionality detection ──────────────────────────────────────


class TestDimensionality:
    def test_bulk_is_bulk(self) -> None:
        atoms = bulk("Pt", "fcc", a=3.92)
        assert detect_dimensionality(atoms) == "bulk"

    def test_slab_is_slab(self) -> None:
        slab = fcc111("Pt", size=(2, 2, 4), vacuum=10.0)
        assert detect_dimensionality(slab) == "slab"

    def test_cluster_is_cluster(self) -> None:
        co = molecule("CO")
        co.set_pbc(False)
        assert detect_dimensionality(co) == "cluster"


# ── Header ─────────────────────────────────────────────────────────


class TestHeader:
    def test_header_has_required_fields(self) -> None:
        slab = fcc111("Pt", size=(2, 2, 4), vacuum=10.0)
        h = header(slab)
        for key in (
            "n_atoms",
            "composition",
            "formula",
            "dimensionality",
            "cell_lengths_angstrom",
            "cell_angles_degrees",
            "pbc",
            "slab",
        ):
            assert key in h
        assert h["n_atoms"] == 16
        assert h["composition"] == {"Pt": 16}
        assert h["dimensionality"] == "slab"
        assert h["slab"] is not None
        assert h["slab"]["n_layers"] == 4

    def test_bulk_has_no_slab_block(self) -> None:
        atoms = bulk("Ni", "fcc", a=3.52)
        h = header(atoms)
        assert h["dimensionality"] == "bulk"
        assert h["slab"] is None


# ── Layer detection ────────────────────────────────────────────────


class TestLayers:
    def test_pt111_three_layers(self) -> None:
        slab = fcc111("Pt", size=(2, 2, 3), vacuum=10.0)
        layers = detect_slab_layers(slab, axis=2)
        assert len(layers) == 3
        # Each layer has 4 atoms (2×2 surface cell).
        for layer in layers:
            assert len(layer) == 4


# ── Site table ─────────────────────────────────────────────────────


class TestSiteTable:
    def test_every_atom_has_row(self) -> None:
        slab = fcc111("Pt", size=(2, 2, 4), vacuum=10.0)
        rows = site_table(slab)
        assert len(rows) == len(slab)
        for i, row in enumerate(rows):
            assert row["index"] == i
            assert row["element"] == "Pt"
            assert row["coord_num"] >= 0

    def test_surface_top_role_assigned(self) -> None:
        slab = fcc111("Pt", size=(2, 2, 4), vacuum=10.0)
        rows = site_table(slab)
        roles = {row["site_role"] for row in rows}
        assert "surface_top" in roles
        assert "bottom" in roles

    def test_adsorbate_role_assigned_for_co_on_pt(self) -> None:
        slab = fcc111("Pt", size=(2, 2, 4), vacuum=10.0)
        co = molecule("CO")
        add_adsorbate(slab, co, height=1.8, position="ontop")
        rows = site_table(slab)
        roles = {row["element"]: row["site_role"] for row in rows}
        # CO atoms should be adsorbate, Pt atoms should be host roles.
        assert roles["C"] == "adsorbate"
        assert roles["O"] == "adsorbate"


# ── Adjacency graph ────────────────────────────────────────────────


class TestAdjacency:
    def test_bulk_pt_has_12_neighbours_for_each_atom(self) -> None:
        atoms = bulk("Pt", "fcc", a=3.92).repeat((2, 2, 2))
        edges = adjacency(atoms)
        for row in edges:
            # FCC has coord. number 12; allow for the cutoff being
            # generous and counting a couple of next-nearest atoms.
            assert 12 <= len(row["neighbors"]) <= 14


# ── Symmetry ───────────────────────────────────────────────────────


class TestSymmetry:
    def test_bulk_pt_is_fm3m(self) -> None:
        atoms = bulk("Pt", "fcc", a=3.92)
        out = analyse(atoms)
        assert out["spacegroup_number"] == 225
        # Single equivalence class for monatomic Pt.
        assert sum(len(v) for v in out["equivalence_classes"].values()) == len(atoms)

    def test_pt3ni_l12_breaks_symmetry(self) -> None:
        """A 3:1 Pt:Ni substitution in the fcc cell should produce
        separate equivalence classes for Pt and Ni."""
        atoms = bulk("Pt", "fcc", a=3.92).repeat((2, 2, 2))
        # Replace one quarter of the atoms with Ni.
        symbols = atoms.get_chemical_symbols()
        for i in range(0, len(atoms), 4):
            symbols[i] = "Ni"
        atoms.set_chemical_symbols(symbols)
        out = analyse(atoms)
        # We expect at least one Pt and one Ni class.
        elements = {name.split("_eq_")[0] for name in out["equivalence_classes"]}
        assert "Pt" in elements
        assert "Ni" in elements


# ── Whole annotator ────────────────────────────────────────────────


class TestAnnotateEndToEnd:
    def test_annotate_returns_all_keys(self) -> None:
        slab = fcc111("Pt", size=(2, 2, 4), vacuum=10.0)
        out = annotate(slab)
        for key in (
            "header",
            "sites",
            "graph",
            "special_sites",
            "symmetry",
            "ascii_top",
            "ascii_side",
            "warnings",
            "view_version",
        ):
            assert key in out
        assert out["view_version"] == VIEW_VERSION

    def test_ascii_top_has_some_atoms(self) -> None:
        slab = fcc111("Pt", size=(3, 3, 4), vacuum=10.0)
        out = annotate(slab)
        # At least some non-empty cells in the grid.
        assert "P" in out["ascii_top"]

    def test_warning_fires_on_thin_slab(self) -> None:
        thin = fcc111("Pt", size=(2, 2, 2), vacuum=10.0)
        out = annotate(thin)
        # Should warn about insufficient layers (< 4).
        assert any("layers" in w for w in out["warnings"])

    def test_warning_fires_on_small_vacuum(self) -> None:
        small = fcc111("Pt", size=(2, 2, 4), vacuum=4.0)
        out = annotate(small)
        assert any("vacuum" in w for w in out["warnings"])
