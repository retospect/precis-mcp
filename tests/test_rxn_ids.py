"""Identity-key contract for the ``rxn`` kind.

The two-axis scheme is the primary key of the fact table, so its properties
are pinned here rather than left to inspection:

- spelling and reactant order must not change either key;
- a byproduct must change ``uid_strict`` and NOT change ``uid_transform``
  (this is the whole reason the transform axis exists — sources record
  byproducts inconsistently);
- agents must change neither.
"""

from __future__ import annotations

import pytest

from precis.handlers._rxn_ids import (
    RxnParseError,
    parse_reaction_smiles,
    rdkit_available,
)

pytestmark = pytest.mark.skipif(
    not rdkit_available(), reason="rdkit ([chem] extra) not installed"
)


def test_spelling_and_order_do_not_change_either_key() -> None:
    a = parse_reaction_smiles("CCBr.N#C[Na]>>CCC#N")
    b = parse_reaction_smiles("N#C[Na].BrCC>>N#CCC")
    assert a.uid_strict == b.uid_strict
    assert a.uid_transform == b.uid_transform
    assert a.canonical_smiles == b.canonical_smiles


def test_byproduct_splits_strict_but_not_transform() -> None:
    plain = parse_reaction_smiles("CCBr.N#C[Na]>>CCC#N")
    with_salt = parse_reaction_smiles("CCBr.N#C[Na]>>CCC#N.[Na]Br")
    # Different balanced equations -> different exact identity.
    assert plain.uid_strict != with_salt.uid_strict
    # Same chemistry -> precedent must still converge.
    assert plain.uid_transform == with_salt.uid_transform


def test_product_order_does_not_perturb_strict_key() -> None:
    one = parse_reaction_smiles("CCBr.N#C[Na]>>CCC#N.[Na]Br")
    two = parse_reaction_smiles("CCBr.N#C[Na]>>[Na]Br.CCC#N")
    assert one.uid_strict == two.uid_strict
    # ...but the DESIRED product is the first as written, so transform differs.
    assert one.desired_product != two.desired_product
    assert one.uid_transform != two.uid_transform


def test_agents_are_preserved_but_excluded_from_both_keys() -> None:
    bare = parse_reaction_smiles("CC(=O)O.OCC>>CC(=O)OCC")
    catalysed = parse_reaction_smiles("CC(=O)O.OCC>OS(=O)(=O)O>CC(=O)OCC")
    assert catalysed.agents  # recorded
    assert bare.uid_strict == catalysed.uid_strict
    assert bare.uid_transform == catalysed.uid_transform


def test_desired_product_is_the_first_written() -> None:
    parsed = parse_reaction_smiles("CC(=O)O.OCC>>CC(=O)OCC.O")
    assert parsed.desired_product == "CCOC(C)=O"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "CCBr",  # no '>' at all
        "A>B>C>D",  # four blocks
        ">>CCO",  # no reactants
        "CCO>>",  # no products
        "not-a-smiles>>CCO",
    ],
)
def test_malformed_input_raises(bad: str) -> None:
    with pytest.raises(RxnParseError):
        parse_reaction_smiles(bad)
