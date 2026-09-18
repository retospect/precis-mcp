"""The typed op catalog is well-formed."""

from __future__ import annotations

from precis_dft.ops.catalog import OP_CATALOG, known_ops

_EXPECTED_OPS = {
    "set_species",
    "substitute",
    "add_adsorbate",
    "intercalate",
    "vacancy",
    "strain",
    "supercell",
    "constrain",
    "displace",
    "set_magmom",
}


def test_every_expected_op_present() -> None:
    assert set(known_ops()) == _EXPECTED_OPS


def test_every_op_has_required_metadata() -> None:
    for name, entry in OP_CATALOG.items():
        assert "schema" in entry, f"{name} missing 'schema'"
        assert "combinatorial" in entry, f"{name} missing 'combinatorial'"
        assert "purity" in entry, f"{name} missing 'purity'"
        assert entry["purity"] in ("mutation", "batch", "pure"), f"{name}: bad purity"


def test_substitute_is_combinatorial() -> None:
    assert OP_CATALOG["substitute"]["combinatorial"] is True


def test_set_species_is_not_combinatorial() -> None:
    assert OP_CATALOG["set_species"]["combinatorial"] is False
