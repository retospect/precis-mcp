"""Closed-form pH terms for explicit environment reads, under CHE.

Only a released product receives the acid/base reservoir term. Mid-route
release (NH3+O -> O) changes that rung and all subsequent graph states,
independently of the staircase's terminal-only desorption label.
If the engine recorded ``results.speciation.species[product].dG``, remove
that baked-in term first: an explicit point replaces its reservoir reference,
never protonates the same product twice. No pH preserves that stored footing.
Adsorbed intermediates and measured barriers retain their stored values;
PCET at fixed RHE remains pH invariant. The supplied pKa constants are
fixed model inputs, while kT uses the requested environment temperature.
This is not a solvation, field, gas-pressure or barrier model.

HER uses the engine's ``her.dG_H_star``: ``G(H) - G(*)`` at 0 V_RHE
(``autocatpath.pipeline::_her_report``). The H2 reference is the standard
CHE equilibrium reservoir, ``G(H2) - 2 mu(H+ + e-) = 0`` at 0 V_RHE.
Thus Volmer is dG_H* + U and Heyrovsky is -dG_H* + U. No independent
H2 pressure/desorption correction or default H* energy is introduced.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.utils.coerce import finite_or

from .surface_pourbaix import CLOSED_SHELL, K_B_EV

if TYPE_CHECKING:
    from .environment import Environment

# Model constants supplied by gr559664 / td564087, the pH-terms brief
# (pathway-selectivity-u-ph-window, original In scope item 3).
PRODUCT_PKA = {
    "NH3": 9.25,  # Brief source: NH3/NH4+ aqueous conjugate-acid pKa 9.25.
    "NH2OH": 6.0,  # Brief source: NH2OH/NH3OH+ pKa ~6 (rounded model value).
}

HER_REFERENCE = "CHE H2 reference: G(H2) - 2 mu(H+ + e-) = 0 at 0 V_RHE"


def released_product(node: str) -> str | None:
    """The engine's landing tag, including a remaining spectator (O~nh3)."""
    _, separator, tag = node.partition("~")
    return tag.upper() if separator and tag else None


def released_products(source: str, target: str) -> tuple[str, ...]:
    """Whole-base fragment loss or new landing products, including spectators.

    Site suffixes do not change composition. Hydrogenation replaces fragments
    rather than releasing the consumed base; require a pure drop. Untagged
    parking of atomic fragments is not protonation of a product. Landing tags
    are cumulative multisets; only products added on this edge are released.
    """

    def fragments(state: str) -> Counter[str]:
        return Counter(
            part.split("@", 1)[0]
            for part in state.split("~", 1)[0].split("+")
            if part != "*"
        )

    before, after = fragments(source), fragments(target)
    gone = before - after
    if not gone or after - before:
        return ()
    tag = released_product(target)
    if tag is not None:
        source_tag = released_product(source)
        previous = Counter(source_tag.split("+")) if source_tag else Counter()
        landed = tuple((Counter(tag.split("+")) - previous).elements())
        if landed:
            return landed
    return tuple(
        product
        for product in gone.elements()
        if product in CLOSED_SHELL or target == "*"
    )


def protonation_terms(
    products: tuple[str, ...], environment: Environment, results: dict[str, Any]
) -> tuple[tuple[str, float], ...]:
    """Named additions relative to the stored reservoir, in eV."""
    terms = []
    for product in products:
        correction = product_protonation(product, environment)
        if correction is None:
            continue
        stored = stored_protonation(results, product)
        if stored:
            terms.append(("stored_product_protonation_removed", -stored))
        terms.append(("product_protonation", correction))
    return tuple(terms)


def stored_protonation(results: dict[str, Any], product: str) -> float:
    """The engine's exported shift, without guessing from its pH or version."""
    speciation = results.get("speciation") or {}
    species = speciation.get("species") if isinstance(speciation, dict) else None
    if species is None:
        return 0.0
    if not isinstance(species, dict):
        raise BadInput(
            "stored speciation.species is not a product table; cannot reprice pH",
            next="read view='analysis' without environment to inspect the stored footing",
        )
    if product not in species:
        return 0.0
    entry = species[product]
    value = entry.get("dG") if isinstance(entry, dict) else None
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
    ):
        raise BadInput(
            f"stored speciation for {product} has no finite dG; cannot reprice pH",
            next="get(kind='pathway', id='<pathway>', view='analysis') "
            "without an environment to read the stored footing",
        )
    return float(value)


def product_protonation(product: str, environment: Environment) -> float | None:
    """Acid/base reservoir gain in eV; None means no term is available."""
    ph = environment.electrochemical.get("pH")
    pka = PRODUCT_PKA.get(product.upper())
    if ph is None or pka is None:
        return None
    x = (pka - ph) * math.log(10.0)
    # Stable log(1 + exp(x)), including strongly protonated products.
    return -K_B_EV * environment.T * (max(x, 0.0) + math.log1p(math.exp(-abs(x))))


def protonate_graph(
    graph: dict[str, Any], environment: Environment, results: dict[str, Any]
) -> dict[str, Any]:
    """Copy and correct released-product ledgers and incident differences.

    Apply after ``reaction_graph.at_potential``. The same product function
    prices the affine staircase; neither view mutates stored graph/results.
    """
    if "pH" not in environment.electrochemical:
        return graph
    parents: dict[str, list[str]] = defaultdict(list)
    children: dict[str, list[str]] = defaultdict(list)
    for node in graph.get("nodes", []):
        parents[str(node["id"])]
    edge_terms: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    notes: list[str] = []
    for link in graph.get("links", []):
        a, b = str(link["source"]), str(link["target"])
        parents[a]
        parents[b].append(a)
        children[a].append(b)
        products = released_products(a, b)
        edge_terms[a, b] = protonation_terms(products, environment, results)
        for product in products:
            if product not in PRODUCT_PKA:
                notes.append(
                    f"product_protonation {product} ({a}->{b}): "
                    "no pKa in PRODUCT_PKA; no term applied"
                )
    # Reverse DFS finish order starts source components before their
    # descendants, even when a release-free cycle leaves no root node.
    visited: set[str] = set()
    finished: list[str] = []
    for start in parents:
        if start in visited:
            continue
        visited.add(start)
        stack = [(start, iter(children[start]))]
        while stack:
            name, descendants = stack[-1]
            child = next(descendants, None)
            if child is None:
                finished.append(name)
                stack.pop()
            elif child not in visited:
                visited.add(child)
                stack.append((child, iter(children[child])))
    shifts: dict[str, float] = {}
    # Revisits must agree; only a conflicting release ledger refuses.
    for start in reversed(finished):
        if start in shifts:
            continue
        pending = [(start, 0.0)]
        while pending:
            name, shift = pending.pop()
            if name in shifts:
                if not math.isclose(shifts[name], shift, abs_tol=1e-9):
                    raise BadInput(
                        f"pH repricing: inconsistent released-product histories at {name}",
                        next="read view='analysis' without environment to inspect the stored graph",
                    )
                continue
            shifts[name] = shift
            pending.extend(
                (child, shift + sum(value for _, value in edge_terms[name, child]))
                for child in children[name]
            )
    nodes = []
    for node in graph.get("nodes", []):
        name = str(node.get("id"))
        shift = shifts[name]
        corrected = dict(node)
        if shift:
            for key in ("energy", "rel_energy"):
                if corrected.get(key) is not None:
                    corrected[key] = float(corrected[key]) + shift
        nodes.append(corrected)
    links = []
    for link in graph.get("links", []):
        corrected = dict(link)
        if corrected.get("delta_e") is not None:
            corrected["delta_e"] = (
                float(corrected["delta_e"])
                + shifts.get(str(link.get("target")), 0.0)
                - shifts.get(str(link.get("source")), 0.0)
            )
        links.append(corrected)
    ledger = [
        (f"{a}->{b}", name, value)
        for (a, b), terms in edge_terms.items()
        for name, value in terms
    ]
    ec = environment.electrochemical
    if "U_vs_SHE" in ec:
        fixed_shift = (environment.u_rhe or 0.0) - ec["U_vs_SHE"]
        n_h = {str(node["id"]): finite_or(node.get("n_H")) for node in nodes}
        for a, b in edge_terms:
            n_e = n_h.get(b, 0.0) - n_h.get(a, 0.0)
            if n_e:
                ledger.append((f"{a}->{b}", "fixed_SHE", n_e * fixed_shift))
    return {
        **graph,
        "nodes": nodes,
        "links": links,
        "ph_terms": ledger,
        "ph_notes": notes,
    }
