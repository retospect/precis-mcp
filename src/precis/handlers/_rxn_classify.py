"""Reaction classifier — reaction SMILES → RXNO class + confidence.

A class is assigned only when one of its templates, run forward over the
recorded reactants (and agents), **reproduces the recorded main product**.
A pattern that merely matches the reactants proves nothing, so it does not
count. This is the neuro-symbolic rule from Schwaller's
``reactionclassifier`` (a template must reproduce the product before it
labels), applied with a small hand-curated template set and no model:
rdkit only, torch-free, CPU.

Classes are stored as RXNO ids (CC BY 4.0; MOP ids for generic processes
RXNO has no name for). This keeps the store interoperable with
NameRxn-derived data (``reaction-kind-and-synthesis-cost.md`` §
reaction_class). Names come from the vendored ``data/rxno_classes.json``.

Confidence:

- ``0.9``: exactly one class (after dropping a class whose more specific
  child also fired) reproduces the product.
- ``0.5``: several unrelated classes reproduce it. The first in table
  order is reported and the rest are listed as alternatives.
- no class: nothing reproduces the product. This is reported as
  ``unclassified`` and never guessed. Coverage is the known gap: free
  classifiers reach 64–68 % on USPTO against NameRxn's ~89 %, and this
  table is narrower than either.

Products are compared without stereo, because templates do not carry it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from itertools import permutations
from typing import Any

#: Bumped whenever the template table changes, so stored classes say which
#: table produced them.
CLASSIFIER_VERSION = "precis-templates-v1"

_AMINE = "[N;H2,H1;!$(N[C,S]=O);!$(N=*);!$(N#*):{m}]"
_ALCOHOL = "[O;H1;$(O[CX4]),$(Oc):{m}]"
_ARX = "[c:{m}]-[Br,I,Cl,$(OS(=O)(=O)C(F)(F)F)]"


@dataclass(frozen=True, slots=True)
class _Class:
    id: str
    templates: tuple[str, ...]
    parent: str | None = None
    """A more general class this one refines. When both fire, the parent
    is dropped and the result is not counted as a tie."""


#: Table order is the tie-break order: the earlier entry is reported first.
_CLASSES: tuple[_Class, ...] = (
    # protection / deprotection (more specific than the acylations below)
    _Class(
        "RXNO:0000079", (f"{_AMINE.format(m=1)}>>[N:1]C(=O)OC(C)(C)C",), "RXNO:0000359"
    ),
    _Class("RXNO:0000326", ("[N:1]-C(=O)O[C](-[CH3])(-[CH3])-[CH3]>>[N:1]",)),
    _Class(
        "RXNO:0000328",
        (
            f"{_ALCOHOL.format(m=1)}.[Si:2]-[Cl,Br,I,$(OS(=O)(=O)C(F)(F)F)]>>[O:1]-[Si:2]",
        ),
    ),
    _Class("RXNO:0000692", ("[O;$(O[#6]):1]-[Si](-[#6])(-[#6])-[#6]>>[O:1]",)),
    # C–N / C–O / S–N bond formation
    _Class(
        "RXNO:0000357",
        (
            f"[C:1](=[O:2])-[OH1].{_AMINE.format(m=3)}>>[C:1](=[O:2])-[N:3]",
            f"[C:1](=[O:2])-[Cl,Br].{_AMINE.format(m=3)}>>[C:1](=[O:2])-[N:3]",
        ),
    ),
    _Class(
        "RXNO:0000359",
        (
            f"[O:4]-[C:1](=[O:2])-[Cl,$(OC(=O)O*)].{_AMINE.format(m=3)}>>[O:4]-[C:1](=[O:2])-[N:3]",
        ),
    ),
    _Class(
        "RXNO:0000358",
        (f"[N:1]=[C:2]=[O:3].{_AMINE.format(m=4)}>>[N:1]-[C:2](=[O:3])-[N:4]",),
    ),
    _Class(
        "MOP:0002524",
        (
            f"[S:1](=[O:2])(=[O:3])-[Cl,F].{_AMINE.format(m=4)}"
            ">>[S:1](=[O:2])(=[O:3])-[N:4]",
        ),
    ),
    _Class(
        "RXNO:0000360",
        (
            f"[C:1](=[O:2])-[OH1].{_ALCOHOL.format(m=3)}>>[C:1](=[O:2])-[O:3]",
            f"[C:1](=[O:2])-[Cl,Br].{_ALCOHOL.format(m=3)}>>[C:1](=[O:2])-[O:3]",
        ),
    ),
    _Class(
        "RXNO:0000335",
        (f"[C;!$(C(=O)[O,N,S,Cl]):1]=[O].{_AMINE.format(m=2)}>>[C:1]-[N:2]",),
    ),
    _Class(
        "RXNO:0000090",
        (
            f"{_ALCOHOL.format(m=1)}.[CX4;!$(C(F)(F)F):2]-[Br,I,Cl,$(OS(=O)(=O)*)]>>[O:1]-[C:2]",
        ),
    ),
    _Class(
        "MOP:0002369",
        (
            f"{_AMINE.format(m=1)}.[CX4;!$(C(F)(F)F):2]-[Br,I,Cl,$(OS(=O)(=O)*)]>>[N:1]-[C:2]",
        ),
    ),
    _Class("RXNO:0000192", (f"{_ARX.format(m=1)}.{_AMINE.format(m=2)}>>[c:1]-[N:2]",)),
    _Class("MOP:0000791", (f"[c:1]-[F].{_AMINE.format(m=2)}>>[c:1]-[N:2]",)),
    # C–C bond formation
    _Class("RXNO:0000140", (f"{_ARX.format(m=1)}.[#6:2]-[B](~[O])~[O]>>[c:1]-[#6:2]",)),
    _Class("RXNO:0000035", (f"{_ARX.format(m=1)}.[#6:2]-[Sn]>>[c:1]-[#6:2]",)),
    _Class("RXNO:0000088", (f"{_ARX.format(m=1)}.[#6:2]-[Zn]>>[c:1]-[#6:2]",)),
    _Class("RXNO:0000137", ("[c:1]-[Br,I].[C;H1:2]#[C:3]>>[c:1]-[C:2]#[C:3]",)),
    _Class("RXNO:0000024", ("[c:1]-[Br,I].[CH2:2]=[C:3]>>[c:1]-[C:2]=[C:3]",)),
    _Class(
        "RXNO:0000014",
        ("[C:1]=[O:2].[#6:3]-[Mg]>>[C:1](-[#6:3])-[O:2]",),
    ),
    _Class(
        "RXNO:0000015",
        (
            "[C:1]=[O].[C:2]=[P]>>[C:1]=[C:2]",
            "[C:1]=[O].[C-:2]-[P+]>>[C:1]=[C+0:2]",
        ),
    ),
    _Class("RXNO:0000045", ("[cH:1].[C:2](=[O:3])-[Cl]>>[c:1]-[C:2]=[O:3]",)),
    _Class(
        "RXNO:0000006",
        (
            "[C:1]=[C:2]-[C:3]=[C:4].[C:5]=[C:6]"
            ">>[C:1]1-[C:2]=[C:3]-[C:4]-[C:5]-[C:6]-1",
        ),
    ),
    # redox and cleavage
    _Class("RXNO:0000337", ("[c:2]-[N+:1](=O)[O-]>>[c:2]-[N+0:1]",)),
    _Class("RXNO:0000338", ("[C:1](=O)-[N:2]>>[C:1]-[N:2]",)),
    # Neighbours are mapped: an unmapped template atom is deleted.
    _Class("MOP:0000573", ("[CX4;H2:1](-[#6:3])-[OH1:2]>>[#6:3]-[C:1]=[O:2]",)),
    _Class(
        "MOP:0000574",
        ("[CX4;H1:1](-[#6:3])(-[#6:4])-[OH1:2]>>[#6:3]-[C:1](-[#6:4])=[O:2]",),
    ),
    _Class("RXNO:0000353", ("[CX4;H2:1](-[#6:3])-[OH1:2]>>[#6:3]-[C:1](=[O:2])O",)),
    _Class(
        "MOP:0000580",
        ("[CX3;H0:1](-[#6:3])(-[#6:4])=[O:2]>>[#6:3]-[C:1](-[#6:4])-[O:2]",),
    ),
    _Class("MOP:0000571", ("[CX3;H1:1](-[#6:3])=[O:2]>>[#6:3]-[C:1]-[O:2]",)),
    _Class("MOP:0000589", ("[C:1]=[C:2]>>[C:1]-[C:2]",)),
    _Class("MOP:0000619", ("[C:1](=[O:2])-O-[#6]>>[C:1](=[O:2])O",)),
)


@dataclass(frozen=True, slots=True)
class ClassHit:
    id: str
    name: str
    confidence: float


@dataclass(slots=True)
class Classification:
    """Result of :func:`classify`. ``best`` is None when unclassified."""

    best: ClassHit | None
    alternatives: list[ClassHit] = field(default_factory=list)
    note: str = ""
    version: str = CLASSIFIER_VERSION

    def to_meta(self) -> dict[str, Any]:
        """The keys :class:`RxnHandler` stores on the ref."""
        if self.best is None:
            return {}
        return {
            "reaction_class": self.best.id,
            "class_name": self.best.name,
            "class_confidence": self.best.confidence,
            "class_source": self.version,
            "class_alternatives": [a.id for a in self.alternatives],
        }

    def render(self) -> str:
        if self.best is None:
            return f"class: unclassified ({self.note or 'no template reproduced the product'})"
        line = (
            f"class: {self.best.id} {self.best.name} "
            f"(confidence {self.best.confidence:.1f}, {self.version})"
        )
        if self.alternatives:
            line += "; also fits: " + ", ".join(
                f"{a.id} {a.name}" for a in self.alternatives
            )
        return line


class ClassifierUnavailable(RuntimeError):
    """rdkit is not installed in this venv."""


@cache
def rxno_names() -> dict[str, str]:
    """RXNO/MOP id → name, from the vendored ontology table."""
    text = (
        resources.files("precis.data")
        .joinpath("rxno_classes.json")
        .read_text(encoding="utf-8")
    )
    return dict(json.loads(text)["classes"])


def class_name(class_id: str) -> str:
    return rxno_names().get((class_id or "").strip(), "")


def class_ids() -> list[str]:
    return [c.id for c in _CLASSES]


@cache
def _compiled() -> tuple[tuple[_Class, tuple[Any, ...]], ...]:
    from rdkit.Chem import rdChemReactions

    out = []
    for cls in _CLASSES:
        rxns = []
        for smarts in cls.templates:
            rxn = rdChemReactions.ReactionFromSmarts(smarts)
            rxn.Initialize()
            rxns.append(rxn)
        out.append((cls, tuple(rxns)))
    return tuple(out)


def _flat_key(mol: Any) -> str | None:
    from rdkit import Chem

    try:
        Chem.SanitizeMol(mol)
        return Chem.MolToSmiles(Chem.RemoveHs(mol), isomericSmiles=False)
    except Exception:
        return None


def _reproduces(rxn: Any, pool: list[Any], target: str) -> bool:
    n = rxn.GetNumReactantTemplates()
    if n > len(pool):
        return False
    for combo in permutations(pool, n):
        try:
            outcomes = rxn.RunReactants(combo, maxProducts=200)
        except Exception:
            continue
        for products in outcomes:
            for prod in products:
                if _flat_key(prod) == target:
                    return True
    return False


def _split(rxn_smiles: str) -> tuple[list[str], list[str], list[str]]:
    parts = rxn_smiles.strip().split(">")
    if len(parts) != 3:
        raise ValueError(
            "expected 'reactants>>products' or 'reactants>agents>products'"
        )
    r, a, p = ([s for s in part.split(".") if s] for part in parts)
    if not r or not p:
        raise ValueError("a reaction needs reactants and products")
    return r, a, p


def classify(rxn_smiles: str) -> Classification:
    """Classify one reaction SMILES. Raises :class:`ClassifierUnavailable`
    without rdkit and ``ValueError`` on a malformed reaction."""
    try:
        from rdkit import Chem, rdBase
    except ImportError as exc:
        raise ClassifierUnavailable(
            "reaction classification needs rdkit ([chem] extra)"
        ) from exc
    rdBase.DisableLog("rdApp.*")

    reactants, agents, products = _split(rxn_smiles)
    pool = []
    for smi in reactants + agents:
        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            raise ValueError(f"unparseable SMILES {smi!r}")
        pool.append(mol)
    prod_mols = [Chem.MolFromSmiles(s) for s in products]
    if any(m is None for m in prod_mols):
        raise ValueError("unparseable product SMILES")
    main = max(prod_mols, key=lambda m: m.GetNumHeavyAtoms())
    target = Chem.MolToSmiles(main, isomericSmiles=False)

    fired = [
        cls
        for cls, rxns in _compiled()
        if any(_reproduces(r, pool, target) for r in rxns)
    ]
    if not fired:
        return Classification(best=None, note="no template reproduced the product")
    fired_ids = {c.id for c in fired}
    kept = [c for c in fired if not any(o.parent == c.id for o in fired if o is not c)]
    names = rxno_names()
    conf = 0.9 if len(kept) == 1 else 0.5
    hits = [ClassHit(c.id, names.get(c.id, c.id), conf) for c in kept]
    note = ""
    if len(fired_ids) > len(kept):
        note = "general class dropped in favour of its specific child"
    return Classification(best=hits[0], alternatives=hits[1:], note=note)


__all__ = [
    "CLASSIFIER_VERSION",
    "ClassHit",
    "Classification",
    "ClassifierUnavailable",
    "class_ids",
    "class_name",
    "classify",
    "rxno_names",
]
