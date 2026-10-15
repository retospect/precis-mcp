"""Label each route step with a reaction class.

Runs :func:`precis.handlers._rxn_classify.classify` (rdkit templates, RXNO
ids) over every ``RouteStep`` after planning, beside the constraint screen.
Without rdkit the graph is returned unchanged: the steps stay unlabelled,
which the render shows as no ``class:`` line rather than a guess.
"""

from __future__ import annotations

from dataclasses import replace

from precis.handlers._rxn_classify import ClassifierUnavailable, classify
from precis_chem.ir import RouteGraph, RouteStep


def _classify_step(step: RouteStep) -> RouteStep:
    if not step.reactants or not step.product:
        return step
    try:
        result = classify(step.reaction_smiles())
    except ValueError:
        return step
    if result.best is None:
        return step
    return replace(
        step,
        reaction_class=result.best.id,
        class_name=result.best.name,
        class_confidence=result.best.confidence,
    )


def classify_route(graph: RouteGraph) -> RouteGraph:
    """Return ``graph`` with every classifiable step labelled."""
    try:
        steps = [_classify_step(s) for s in graph.steps]
    except ClassifierUnavailable:
        return graph
    return replace(graph, steps=steps)


__all__ = ["classify_route"]
