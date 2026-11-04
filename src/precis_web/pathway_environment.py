"""Shared, read-only environment state for pathway and Pourbaix links.

Only the canonical ``env`` parameter opts into strict record validation.
Historical ``u``/``ph`` links retain each page's existing defaults and
clamps. The same record travels between pages without starting a solve.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from precis_pathway.environment import Environment, lookup_environment


def linked_environment(params: Mapping[str, str]) -> Environment | None:
    """Decode a full environment link; leave legacy links to their reader."""
    if "env" not in params:
        return None
    return Environment.from_url(params)


def environment_context(
    meta: dict[str, Any], environment: Environment | None
) -> dict[str, Any] | None:
    """The selected point and its stored-result availability, without compute."""
    if environment is None:
        return None
    return {
        "record": environment.to_dict(),
        "query": environment.to_url(),
        "lookup": lookup_environment(meta, environment),
    }


def matching_kinetics_results(
    meta: dict[str, Any], environment: Environment | None
) -> dict[str, Any]:
    """Keep each kinetics generation only when its own solve matches the point.

    A matching v1 solve does not license a v2 rate (or vice versa). Both the
    panel and route flux reader consume this same filtered results mapping.
    A matching previous v2 solve is promoted to the reader's current slot.
    """
    results = dict(meta.get("results") or {})
    if environment is None:
        return results
    for name in ("kinetics", "kinetics_v2"):
        record = results.get(name)
        match = lookup_environment({**meta, "results": {name: record}}, environment)
        if match["status"] != "exact":
            results.pop(name, None)
    if "kinetics_v2" not in results:
        previous = results.get("kinetics_v2_previous")
        match = lookup_environment(
            {**meta, "results": {"kinetics_v2_previous": previous}}, environment
        )
        if match["status"] == "exact":
            results["kinetics_v2"] = match["record"]
    return results
