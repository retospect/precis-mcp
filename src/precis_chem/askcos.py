"""ASKCOS v2 Tree-Builder REST plumbing (ADR 0056 slice 3).

Two pure, gate-testable pieces — the request builder + the response path
extractor — so the *shape* of the ASKCOS call is validated without a running
deployment (the actual HTTP POST is the ``SERVICE_CALLER`` hook in
``jobs.py``, mirroring how ``RUNNER``/``STAGER`` isolate the cluster boundary).

ASKCOS v2 is a multi-service platform whose automatic planner is the **Tree
Builder**, exposed as a synchronous REST endpoint
(:data:`TREE_SEARCH_PATH`). It returns a set of synthetic **paths** in the
``askcosv2`` format LinChemIn's ``facade('translate', …,
input_format='askcosv2')`` normalizes — so the precis side reuses
:func:`precis_chem.normalize.parse_syngraph` unchanged (same IR as AiZynth).

.. warning::
   The exact request/response JSON is **pinned against the ASKCOS docs, not a
   live instance** (the deployment isn't stood up yet). Both the request body
   (:func:`build_treebuilder_request`) and the path extraction
   (:func:`extract_paths`) are deliberately localized + defensive so a single
   correction against a real deployment's ``/docs`` (``:9100/docs``) suffices.
   Same discipline that pinned ``ReactionTree.to_dict`` for AiZynth 1b.
"""

from __future__ import annotations

from typing import Any

#: ASKCOS v2 synchronous MCTS tree-search endpoint (relative to the base URL).
#: The ``-without-token`` variant skips the async job/result token dance —
#: appropriate for our node-pinned, one-target compute job.
TREE_SEARCH_PATH = "/api/tree-search/mcts/call-sync-without-token"

#: Defaults for a single-target retrosynthesis search (overridable per call).
DEFAULT_EXPANSION_TIME_S = 60
DEFAULT_MAX_BRANCHING = 25


def build_treebuilder_request(
    smiles: str,
    *,
    max_steps: int,
    expansion_time_s: int = DEFAULT_EXPANSION_TIME_S,
    max_branching: int = DEFAULT_MAX_BRANCHING,
) -> dict[str, Any]:
    """The JSON body POSTed to :data:`TREE_SEARCH_PATH` for one target.

    ASKCOS's ``max_depth`` is the retrosynthetic depth (our ``max_steps``);
    ``expansion_time`` bounds the MCTS wall-clock. Kept flat + minimal — the
    fields ASKCOS v2's Tree Builder documents as required/common. Verify the
    key names against a live ``/docs`` before the first real run.
    """
    return {
        "smiles": smiles,
        "max_depth": int(max_steps),
        "max_branching": int(max_branching),
        "expansion_time": int(expansion_time_s),
        # Return every solved path, not just the first — the normalizer keeps
        # the top-ranked one but records the count as provenance.
        "return_first": False,
    }


def extract_paths(response: Any) -> list[dict[str, Any]]:
    """Pull the list of route/path dicts out of a Tree-Builder response.

    Defensive over the wrapper shapes ASKCOS v2 uses (``{result: {paths: […]}}``
    is the documented sync-endpoint envelope; also tolerate a bare list, a
    top-level ``paths``/``trees``/``output``). Each element is an ``askcosv2``
    route dict LinChemIn translates. Returns ``[]`` when none are present
    (an unsolved target) — the caller records that as an empty route.
    """
    if isinstance(response, list):
        return [p for p in response if isinstance(p, dict)]
    if not isinstance(response, dict):
        return []
    # Documented sync-endpoint envelope: {"result": {"paths": [...]}}.
    result = response.get("result")
    if isinstance(result, dict):
        for key in ("paths", "trees", "output"):
            val = result.get(key)
            if isinstance(val, list):
                return [p for p in val if isinstance(p, dict)]
    if isinstance(result, list):
        return [p for p in result if isinstance(p, dict)]
    # Fall back to a top-level list under a few likely keys.
    for key in ("paths", "trees", "output"):
        val = response.get(key)
        if isinstance(val, list):
            return [p for p in val if isinstance(p, dict)]
    return []
