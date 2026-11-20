"""Per-atom energy units (gr716220): 'meV/atom' folds to meV + 'atom' basis."""

from __future__ import annotations

import pytest

from precis.taxonomy.measure_units import basis_normalization, pint_unit_text


@pytest.mark.parametrize(
    "raw",
    ["meV/atom", "meV / atom", "meV atom^-1", "meV atom⁻¹", "meV per atom", "meV_atom"],
)
def test_per_atom_spellings_fold_to_basis(raw: str) -> None:
    assert pint_unit_text(raw) == ("meV", "atom")


def test_per_atom_normalization() -> None:
    assert basis_normalization("atom") == "per atom"


def test_plain_ratio_unit_untouched() -> None:
    assert pint_unit_text("m/s") == ("m/s", None)
