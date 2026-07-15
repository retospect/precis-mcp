"""The canonical retrosynthesis route-graph IR (ADR 0056 §2).

One ``RouteGraph`` normalizes the output of *any* planner (AiZynthFinder,
ASKCOS, …) into a single schema — "swap the engine, keep the IR". Pure
Python: **no chemistry dependencies** (rdkit / aizynth live behind the
``[chem]`` extra and are only touched by the compute-node engine
adapter), so this module imports cleanly on the always-on request path
and the plugin loads even when the extra isn't installed.

A route is a shallow DAG rendered as an ordered list of steps. Each
:class:`RouteStep` disconnects one product into its precursors; a
precursor that is ``in_stock`` is a buyable leaf (search terminates).
The IR is serialized to JSON on ``refs.meta.route`` and rendered to a
markdown tree the LLM reads via ``get(kind='route', id=…)``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

#: Version of the serialized envelope. Bump when the JSON shape changes so a
#: reader can migrate old ``meta.route`` blobs (there is no consumer of old
#: shapes yet — this is forward insurance).
IR_VERSION = 1


def normalize_smiles(smiles: str) -> str:
    """Canonicalize a SMILES string.

    Slice 1 is deliberately lexical — strip + collapse whitespace — so the IR
    has **zero** chemistry deps. Real canonicalization (rdkit
    ``MolToSmiles(MolFromSmiles(s), canonical=True)``) is an engine-side
    concern (the ``[chem]`` extra) and folds in when LinChemIn normalization
    lands (slice 2); doing it here would drag rdkit onto the request path.
    """
    return " ".join(str(smiles).split())


@dataclass(frozen=True, slots=True)
class RouteStep:
    """One retrosynthetic disconnection: ``product`` ⇐ ``reactants``."""

    #: 1-based position in the ordered plan (target = step 1).
    id: int
    #: Product SMILES (what this step makes).
    product: str
    #: Precursor SMILES that react to give the product.
    reactants: list[str]
    #: Reaction template / SMARTS the engine matched (engine-specific id).
    template_id: str | None = None
    reaction_smarts: str | None = None
    #: Free-text conditions (reagents/solvent/temp) when the engine reports them.
    conditions: str | None = None
    #: Per-step confidence in [0, 1] (engine policy/template score) when known.
    confidence: float | None = None
    #: True when *every* reactant is a buyable/stock leaf (this branch is solved).
    in_stock: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> RouteStep:
        return cls(
            id=int(d["id"]),
            product=str(d["product"]),
            reactants=[str(r) for r in d.get("reactants", [])],
            template_id=d.get("template_id"),
            reaction_smarts=d.get("reaction_smarts"),
            conditions=d.get("conditions"),
            confidence=d.get("confidence"),
            in_stock=bool(d.get("in_stock", False)),
        )


@dataclass(frozen=True, slots=True)
class RouteGraph:
    """A normalized synthetic route to ``target``."""

    target: str
    engine: str
    engine_version: str
    steps: list[RouteStep] = field(default_factory=list)
    #: True when the search reached buyable leaves on every branch.
    solved: bool = False
    #: Overall route score in [0, 1] (engine-defined) when known.
    score: float | None = None
    #: Free-form engine provenance (image digest, model version, stock set, …).
    provenance: dict[str, Any] = field(default_factory=dict)

    # ── serialization ────────────────────────────────────────────────
    def to_json(self) -> dict[str, Any]:
        return {
            "version": IR_VERSION,
            "target": self.target,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "solved": self.solved,
            "score": self.score,
            "steps": [s.to_json() for s in self.steps],
            "provenance": self.provenance,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> RouteGraph:
        return cls(
            target=str(d["target"]),
            engine=str(d.get("engine", "?")),
            engine_version=str(d.get("engine_version", "?")),
            steps=[RouteStep.from_json(s) for s in d.get("steps", [])],
            solved=bool(d.get("solved", False)),
            score=d.get("score"),
            provenance=dict(d.get("provenance", {})),
        )

    # ── renders ──────────────────────────────────────────────────────
    def render(self) -> str:
        """A markdown route tree the LLM reads. One line per step."""
        state = "solved" if self.solved else "unsolved"
        head = (
            f"# route → {self.target}\n"
            f"engine: {self.engine} ({self.engine_version}) · {state} · "
            f"{len(self.steps)} step(s)"
        )
        if self.score is not None:
            head += f" · score {self.score:.3f}"
        if not self.steps:
            return head + "\n\n(no route found)"
        lines = [head, ""]
        for s in self.steps:
            precursors = " + ".join(s.reactants) or "—"
            leaf = "  ✔ in stock" if s.in_stock else ""
            conf = f"  [{s.confidence:.2f}]" if s.confidence is not None else ""
            tmpl = f"  «{s.template_id}»" if s.template_id else ""
            lines.append(f"{s.id}. {s.product} ⇐ {precursors}{conf}{tmpl}{leaf}")
            if s.conditions:
                lines.append(f"   conditions: {s.conditions}")
        return "\n".join(lines)

    def card_text(self) -> str:
        """Plain text embedded into the ``card_combined`` search chunk — the
        molecules on the route, so a route surfaces on a SMILES/target query."""
        mols: list[str] = [self.target]
        for s in self.steps:
            mols.append(s.product)
            mols.extend(s.reactants)
        # De-dup preserving order.
        seen: set[str] = set()
        uniq: list[str] = []
        for m in mols:
            if m not in seen:
                seen.add(m)
                uniq.append(m)
        return f"retrosynthesis route to {self.target}\n" + " ".join(uniq)


def cache_key(
    *,
    target: str,
    engine: str,
    engine_version: str,
    stock: str = "",
    max_steps: int = 0,
) -> str:
    """Content address for a route plan (ADR 0056 §6 / ADR 0007).

    Same ``(target, engine, engine_version, stock snapshot, depth)`` ⇒ same
    key ⇒ zero recompute. The engine *version* (an image digest in prod)
    invalidates the cache when the model changes; ``stock`` is the buyable-set
    snapshot id. Returned as ``retrosynth:<sha256[:16]>``.
    """
    payload = json.dumps(
        {
            "t": normalize_smiles(target),
            "e": engine,
            "v": engine_version,
            "s": stock,
            "n": int(max_steps),
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"retrosynth:{digest}"
