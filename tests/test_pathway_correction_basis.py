"""Executable adoption blockers for the proposed correction-policy contract.

Strict xfails pin missing behavior before catpath 0.24 adoption; see
docs/backlog/pathway-correction-basis.md. No engine or simulation required.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from precis.quest.compute import _network_basis
from precis.quest.frontier import same_network_basis


def _pathway() -> dict[str, Any]:
    return {
        "config": {"network": "ammonia", "corrections": {"gas": True, "h_star": True}},
        "autocatpath_version": "0.24.0",
        "results": {
            "network_digest": "same-topology",
            "corrections": {
                "version": "2026-10-02",
                "gas": {
                    "enabled": True,
                    "anchors": ["H2", "N2", "H2O"],
                    "sets": {
                        "mace:medium": {
                            "key": ["mace", "medium"],
                            "values": {"NO": 0.15},
                        }
                    },
                    "unknown": [],
                },
                "h_star": {
                    "enabled": True,
                    "host_metal": "Pd",
                    "sets": {
                        "mace:medium": {
                            "key": ["mace", "medium", "Pd"],
                            "shift_eV_per_H": 0.25,
                        }
                    },
                    "unanchored": [],
                },
                "state_shifts": {"NH+H": 0.25},
            },
        },
    }


def _assert_equivalent(a: dict[str, Any], b: dict[str, Any], expected: bool) -> None:
    ba, bb = _network_basis(a), _network_basis(b)
    assert ba is not None and bb is not None
    assert same_network_basis(ba, bb) is expected
    assert same_network_basis(bb, ba) is expected


@pytest.mark.xfail(
    strict=True,
    reason="Correction policy is not yet part of the network basis; blocks 0.24 adoption",
)
@pytest.mark.parametrize(
    "difference",
    ["gas", "h_star", "table", "host_override", "model", "missing", "both_missing"],
)
def test_distinct_correction_policies_must_not_compare(difference: str) -> None:
    a = _pathway()
    b = deepcopy(a)
    record = b["results"]["corrections"]
    if difference in ("gas", "h_star"):
        b["config"]["corrections"][difference] = False
        record[difference]["enabled"] = False
        record[difference]["sets"] = {}
    elif difference == "table":
        record["version"] = "next-table"
    elif difference == "host_override":
        b["config"]["corrections"]["host_metal"] = "Pd"
    elif difference == "model":
        for part in ("gas", "h_star"):
            record[part]["sets"] = {}
        record["gas"]["unknown"] = [["mace", "other-model"]]
        record["h_star"]["unanchored"] = [["mace", "other-model", "Pd"]]
    else:
        del b["results"]["corrections"]
        if difference == "both_missing":
            del a["results"]["corrections"]
    _assert_equivalent(a, b, False)


def test_correction_policy_does_not_include_candidate_outcomes() -> None:
    a = _pathway()
    b = deepcopy(a)
    record = b["results"]["corrections"]
    record["state_shifts"] = {"NH+H": 0.0}
    record["h_star"].update(
        host_metal="Cu", sets={}, unanchored=[["mace", "medium", "Cu"]]
    )
    record["gas"]["sets"]["mace:medium"]["source"] = "A different citation description"
    _assert_equivalent(a, b, True)


def test_disabled_corrections_ignore_inactive_policy_fields() -> None:
    a = _pathway()
    for part in ("gas", "h_star"):
        a["config"]["corrections"][part] = False
        a["results"]["corrections"][part] = {"enabled": False, "sets": {}}
    b = deepcopy(a)
    b["results"]["corrections"]["version"] = "next-table"
    b["config"]["corrections"]["host_metal"] = "Cu"
    _assert_equivalent(a, b, True)


def test_policy_ignores_serialization_order_labels_and_duplicate_model_keys() -> None:
    a = _pathway()
    a["results"]["corrections"]["gas"]["unknown"] = [
        ["mace", "unfitted-a"],
        ["mace", "unfitted-b"],
    ]
    b = deepcopy(a)
    gas = b["results"]["corrections"]["gas"]
    gas["unknown"].reverse()
    gas["unknown"].append(gas["unknown"][0])
    gas["anchors"].reverse()
    gas["sets"]["renamed-tag"] = gas["sets"].pop("mace:medium")
    _assert_equivalent(a, b, True)


def test_disabled_h_star_ignores_host_override() -> None:
    a = _pathway()
    a["config"]["corrections"]["h_star"] = False
    a["results"]["corrections"]["h_star"] = {"enabled": False, "sets": {}}
    b = deepcopy(a)
    b["config"]["corrections"]["host_metal"] = "Cu"
    _assert_equivalent(a, b, True)
