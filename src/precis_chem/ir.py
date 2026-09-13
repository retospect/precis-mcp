"""The canonical retrosynthesis route-graph IR.

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
    #: Advisory platform-constraint flags (``precis_chem.constraints``) —
    #: empty on an unconstrained route. Additive: absent in old blobs.
    constraint_flags: list[str] = field(default_factory=list)

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
            constraint_flags=[str(f) for f in d.get("constraint_flags", [])],
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
    #: Route-level descriptors — the LinChemIn ``routes_descriptors`` row
    #: (``nr_steps``/``nr_branches``/``branchedness``/``longest_seq``/
    #: ``convergence``/``cdscore``/``simplified_atom_effectiveness``, …). Empty
    #: for a stub/legacy route (no normalizer ran). Surfaced via
    #: ``get(kind='route', view='metrics')`` — the substrate for our own scoring.
    metrics: dict[str, Any] = field(default_factory=dict)
    #: Free-form engine provenance (image digest, model version, stock set, …).
    provenance: dict[str, Any] = field(default_factory=dict)
    #: Declared platform constraints (``precis_chem.constraints`` names, e.g.
    #: ``ewod-oil``) — advisory screen, part of the plan's content address.
    constraints: list[str] = field(default_factory=list)

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
            "metrics": self.metrics,
            "provenance": self.provenance,
            "constraints": self.constraints,
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
            metrics=dict(d.get("metrics", {})),
            provenance=dict(d.get("provenance", {})),
            constraints=[str(c) for c in d.get("constraints", [])],
        )

    # ── renders ──────────────────────────────────────────────────────
    def render(self) -> str:
        """A markdown route tree the LLM reads. One line per step.

        SMILES go inside code spans. A stereocentre followed by a branch —
        ``C[C@H](N)C(=O)O``, alanine, and much of drug-like chemical space —
        matches markdown's inline-link grammar ``[text](target)`` and would
        otherwise render as a *link* (text ``C@H``, target ``N``), silently
        deleting the structure from what the reader sees. Backticks are safe
        unconditionally: SMILES have no backtick in their grammar.

        Display only — ``card_text`` deliberately stays bare, because that
        string feeds the search index, not a renderer.
        """
        state = "solved" if self.solved else "unsolved"
        head = (
            f"# route → `{self.target}`\n"
            f"engine: {self.engine} ({self.engine_version}) · {state} · "
            f"{len(self.steps)} step(s)"
        )
        if self.score is not None:
            head += f" · score {self.score:.3f}"
        if self.constraints:
            head += (
                "\nconstraints: " + ", ".join(self.constraints) + " (advisory screen)"
            )
        if not self.steps:
            return head + "\n\n(no route found)" + self._constraints_tail()
        lines = [head, ""]
        for s in self.steps:
            precursors = " + ".join(f"`{r}`" for r in s.reactants) or "—"
            leaf = "  ✔ in stock" if s.in_stock else ""
            conf = f"  [{s.confidence:.2f}]" if s.confidence is not None else ""
            # template_id is documented as "template / SMARTS the engine
            # matched"; guillemets are decoration, not an escape, so a SMARTS
            # value would still be parsed as markdown. Code-span the whole
            # thing.
            tmpl = f"  `«{s.template_id}»`" if s.template_id else ""
            lines.append(f"{s.id}. `{s.product}` ⇐ {precursors}{conf}{tmpl}{leaf}")
            if s.conditions:
                # Free text from the engine — reagents/solvent/catalyst. Ligand
                # notation like Pd[P(t-Bu)3](OAc)2 hits the same inline-link
                # grammar as a SMILES stereocentre and would drop the catalyst
                # from the rendered line.
                lines.append(f"   conditions: `{s.conditions}`")
            for flag in s.constraint_flags:
                # Three outcomes, three glyphs. "unscreened" (no data) and
                # "check" (a known incompatibility) are opposite in urgency;
                # collapsing both to ⚠ would let a reader skimming for
                # trouble mistake one for the other.
                if ": ok — " in flag:
                    mark = "✓"
                elif ": unscreened — " in flag:
                    mark = "·"
                else:
                    mark = "⚠"
                lines.append(f"   {mark} {flag}")
        if self.metrics:
            lines += ["", self._metrics_line()]
        return "\n".join(lines) + self._constraints_tail()

    def _constraints_tail(self) -> str:
        """The declared-constraint requirement ledger appended to render()."""
        if not self.constraints:
            return ""
        # Function-local by necessity, not by oversight: constraints.py
        # imports RouteGraph/RouteStep from here at module level and
        # *constructs* them, so it needs the real classes — hoisting this to
        # the top would close the cycle. Leave it here.
        from precis_chem.constraints import requirements_render

        return "\n\n" + requirements_render(self.constraints)

    #: Descriptor keys rendered first, in this order, when present (the rest
    #: follow alphabetically). Keeps the human-facing summary stable across
    #: LinChemIn versions that add/drop columns.
    _METRIC_ORDER = (
        "nr_steps",
        "longest_seq",
        "nr_branches",
        "branchedness",
        "convergence",
        "cdscore",
    )

    def _ordered_metrics(self) -> list[tuple[str, Any]]:
        keys = [k for k in self._METRIC_ORDER if k in self.metrics]
        keys += sorted(k for k in self.metrics if k not in self._METRIC_ORDER)
        return [(k, self.metrics[k]) for k in keys]

    def _fmt_metric(self, v: Any) -> str:
        if isinstance(v, float):
            return f"{v:g}"
        return str(v)

    def _metrics_line(self) -> str:
        return "metrics: " + " · ".join(
            f"{k}={self._fmt_metric(v)}" for k, v in self._ordered_metrics()
        )

    def metrics_render(self) -> str:
        """The ``view='metrics'`` render — route descriptors as a table, the
        substrate for our own scoring. Empty ⇒ no normalizer ran (stub/legacy)."""
        head = (
            f"# route metrics → `{self.target}`\n"
            f"engine: {self.engine} ({self.engine_version})"
        )
        if not self.metrics:
            return (
                head
                + "\n\n(no route-level descriptors — engine ran without the LinChemIn normalizer)"
            )
        rows = "\n".join(
            f"  {k:<30} {self._fmt_metric(v)}" for k, v in self._ordered_metrics()
        )
        return f"{head}\n\n{rows}"

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
    constraints: tuple[str, ...] | list[str] = (),
) -> str:
    """Content address for a route plan.

    Same ``(target, engine, engine_version, stock snapshot, depth,
    constraints)`` ⇒ same key ⇒ zero recompute. The engine *version* (an
    image digest in prod) invalidates the cache when the model changes;
    ``stock`` is the buyable-set snapshot id. Declared platform constraints
    fold in only when non-empty, so every pre-constraint key stays valid.
    Returned as ``retrosynth:<sha256[:16]>``.
    """
    body: dict[str, Any] = {
        "t": normalize_smiles(target),
        "e": engine,
        "v": engine_version,
        "s": stock,
        "n": int(max_steps),
    }
    if constraints:
        body["c"] = sorted(str(c) for c in constraints)
    payload = json.dumps(body, sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"retrosynth:{digest}"
