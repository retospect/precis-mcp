"""Canonical identity keys for a reaction, and the reaction-SMILES parse.

Two keys, because one is not enough (design-of-record
``docs/backlog/reaction-kind-and-synthesis-cost.md``):

``uid_strict``
    Over the **whole balanced equation** — every reactant and every product.
    Exact identity; the import-collapse key.

``uid_transform``
    Over ``(reactants, desired product)`` only, **ignoring byproducts**. This
    is the key precedent queries want. The literature records byproducts
    inconsistently — ``A.B>>C`` and ``A.B>>C.NaBr`` are the same chemistry —
    so a strict-only key fragments precedent across sources. Measured: those
    two forms produce different strict keys and the same transform key.

Canonicalisation is rdkit's (``MolToSmiles`` is canonical by default), so
reactant order and SMILES spelling do not affect either key: ``CCBr`` and
``BrCC`` agree, as do ``CCC#N`` and ``N#CCC``.

**rdkit is imported lazily**, inside the functions. It lives in the ``[chem]``
extra, and this module is reachable from the always-on request path — a
``[chem]``-less install must still import the handler cleanly. Without rdkit
the keys are simply unavailable (``None``), which the handler surfaces rather
than faking.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

#: Digest prefix length. 16 hex chars = 64 bits — ample for a corpus that will
#: not exceed millions of reactions, and short enough to eyeball in meta.
_DIGEST_LEN = 16


class RxnParseError(ValueError):
    """A reaction SMILES that rdkit cannot make sense of."""


@dataclass(frozen=True, slots=True)
class RxnIdentity:
    """Canonical parse of a reaction SMILES."""

    #: Canonical ``reactants>agents>products``, rebuilt from canonical parts.
    canonical_smiles: str
    reactants: tuple[str, ...]
    agents: tuple[str, ...]
    products: tuple[str, ...]
    #: First product as written — the desired one, by the universal convention
    #: of reaction SMILES.
    desired_product: str
    uid_strict: str
    uid_transform: str


def _canonical(smiles: str) -> str:
    """rdkit-canonical SMILES for one component. Raises on an unparseable one."""
    from rdkit import Chem  # lazy: [chem] extra

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise RxnParseError(f"unparseable component SMILES: {smiles!r}")
    return str(Chem.MolToSmiles(mol))


def _split_components(block: str) -> list[str]:
    return [p for p in (x.strip() for x in block.split(".")) if p]


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_DIGEST_LEN]


def parse_reaction_smiles(rxn_smiles: str) -> RxnIdentity:
    """Parse and canonicalise ``reactants>agents>products``.

    Accepts the two-part ``reactants>>products`` form and the three-part
    ``reactants>agents>products`` form. Agents (catalysts, solvents) are
    canonicalised and preserved but participate in **neither** key: the same
    transformation run with a different catalyst is the same transformation,
    and the catalyst belongs in a value row's ``conditions``, where it can be
    compared across sources.

    Raises :class:`RxnParseError` on a malformed string, an unparseable
    component, or an empty reactant/product side.
    """
    raw = (rxn_smiles or "").strip()
    if not raw:
        raise RxnParseError("empty reaction SMILES")
    parts = raw.split(">")
    if len(parts) == 2:
        left, mid, right = parts[0], "", parts[1]
    elif len(parts) == 3:
        left, mid, right = parts
    else:
        raise RxnParseError(
            f"reaction SMILES must have 2 or 3 '>'-separated blocks, got {len(parts)}"
        )

    reactant_list = _split_components(left)
    product_list = _split_components(right)
    if not reactant_list:
        raise RxnParseError("reaction SMILES has no reactants")
    if not product_list:
        raise RxnParseError("reaction SMILES has no products")

    reactants = tuple(sorted(_canonical(s) for s in reactant_list))
    agents = tuple(sorted(_canonical(s) for s in _split_components(mid)))
    # Products keep WRITTEN order — the first is the desired one — while the
    # strict key sorts a copy, so product order never perturbs identity.
    products = tuple(_canonical(s) for s in product_list)
    desired = products[0]

    uid_strict = _digest(".".join(reactants) + ">>" + ".".join(sorted(products)))
    uid_transform = _digest(".".join(reactants) + ">>" + desired)

    canonical = (
        ".".join(reactants) + ">" + ".".join(agents) + ">" + ".".join(products)
        if agents
        else ".".join(reactants) + ">>" + ".".join(products)
    )
    return RxnIdentity(
        canonical_smiles=canonical,
        reactants=reactants,
        agents=agents,
        products=products,
        desired_product=desired,
        uid_strict=uid_strict,
        uid_transform=uid_transform,
    )


def rdkit_available() -> bool:
    """Whether the ``[chem]`` extra is installed. The handler degrades to
    storing the reaction SMILES verbatim without identity keys when it is not,
    rather than inventing a key from an uncanonicalised string."""
    try:
        import rdkit  # noqa: F401
    except ImportError:
        return False
    return True


__all__ = [
    "RxnIdentity",
    "RxnParseError",
    "parse_reaction_smiles",
    "rdkit_available",
]
