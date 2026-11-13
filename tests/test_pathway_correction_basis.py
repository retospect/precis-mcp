"""The correction policy joins the network basis (pathway-correction-basis).

``precis.quest.correction_policy`` is the engine-free mirror of catpath's
policy function; ``same_network_basis`` refuses stamps whose policies differ
or are unknown. No engine or simulation required.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

from precis.quest.compute import _network_basis
from precis.quest.correction_policy import correction_policy, same_correction_policy
from precis.quest.frontier import same_network_basis


def valid_corrections() -> dict[str, Any]:
    """A valid ``results.corrections`` record (both switches on)."""
    return deepcopy(_pathway()["results"]["corrections"])


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


def test_present_non_object_config_corrections_is_unknown() -> None:
    a = _pathway()
    a["config"]["corrections"] = None
    policy, issues = correction_policy(a)
    assert policy is None
    assert any("config.corrections is not an object" in i for i in issues)
    b = _pathway()
    del b["config"]["corrections"]
    policy_b, issues_b = correction_policy(b)
    assert policy_b is not None and policy_b["host_selection"] == "auto"
    assert issues_b == []


def _stamps(a: dict[str, Any], b: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    ba, bb = _network_basis(a), _network_basis(b)
    assert ba is not None and bb is not None
    return ba, bb


def _off(pw: dict[str, Any]) -> dict[str, Any]:
    for part in ("gas", "h_star"):
        pw["config"]["corrections"][part] = False
        pw["results"]["corrections"][part] = {"enabled": False, "sets": {}}
    return pw


def test_pre_024_pathways_carry_no_policy_and_compare() -> None:
    a, b = _pathway(), _pathway()
    for pw in (a, b):
        pw["autocatpath_version"] = "0.23.9"
        del pw["results"]["corrections"]
    ba, bb = _stamps(a, b)
    assert "correction_policy" not in ba and "correction_policy" not in bb
    assert same_network_basis(ba, bb)


def test_explicit_off_against_missing_record_is_unknown_with_r2_issue() -> None:
    off, missing = _off(_pathway()), _pathway()
    del missing["results"]["corrections"]
    _assert_equivalent(off, missing, False)
    policy, issues = correction_policy(missing)
    assert policy is None
    assert any("results.corrections missing on engine 0.24.0" in i for i in issues)


def test_present_non_object_record_reads_as_missing() -> None:
    broken = _pathway()
    broken["results"]["corrections"] = []
    policy, issues = correction_policy(broken)
    assert policy is None
    assert any("results.corrections missing on engine 0.24.0" in i for i in issues)
    _assert_equivalent(broken, broken, False)


def test_absent_part_equals_disabled_part() -> None:
    a, b = _pathway(), _pathway()
    a["results"]["corrections"]["h_star"] = {"enabled": False, "sets": {}}
    del b["results"]["corrections"]["h_star"]
    _assert_equivalent(a, b, True)
    c = _pathway()
    c["results"]["corrections"]["gas"] = {"sets": {}}
    d = _pathway()
    d["results"]["corrections"]["gas"] = {"enabled": False}
    _assert_equivalent(c, d, True)


def test_adsorbed_molecules_in_the_policy() -> None:
    on, off, absent = _pathway(), _pathway(), _pathway()
    on["results"]["corrections"]["gas"]["adsorbed_molecules"] = True
    off["results"]["corrections"]["gas"]["adsorbed_molecules"] = False
    _assert_equivalent(on, off, False)
    _assert_equivalent(absent, off, True)
    for pw in (on, off):
        pw["results"]["corrections"]["gas"] = {"enabled": False, "sets": {}}
        pw["results"]["corrections"]["gas"]["adsorbed_molecules"] = pw is on
    _assert_equivalent(on, off, True)


def test_config_contradicting_the_result_is_a_known_policy_with_an_issue() -> None:
    agree, clash = _pathway(), _pathway()
    clash["config"]["corrections"]["gas"] = False
    _assert_equivalent(agree, clash, True)
    policy, issues = correction_policy(clash)
    assert policy == correction_policy(agree)[0]
    assert issues == [
        "config.corrections.gas=False contradicts "
        "results.corrections.gas.enabled=True; the result is used"
    ]
    assert correction_policy(agree)[1] == []


def test_config_adsorbed_molecules_true_against_absent_result_key_is_an_issue() -> None:
    a = _pathway()
    a["config"]["corrections"]["adsorbed_molecules"] = True
    policy, issues = correction_policy(a)
    assert policy is not None and policy["adsorbed_molecules"] is False
    assert issues == [
        "config.corrections.adsorbed_molecules=True contradicts "
        "results.corrections.gas.adsorbed_molecules=False; the result is used"
    ]
    b = _pathway()
    b["config"]["corrections"]["adsorbed_molecules"] = False
    assert correction_policy(b)[1] == []


@pytest.mark.parametrize("version", [None, "garbage"])
def test_null_or_garbage_version_is_unknown_and_never_compares(
    version: str | None,
) -> None:
    a = _pathway()
    a["autocatpath_version"] = version
    ba = _network_basis(a)
    assert ba is not None and ba["correction_policy"] is None
    assert not same_network_basis(ba, ba)
    assert not same_correction_policy(ba, ba)
    other = _network_basis(_pathway())
    assert other is not None
    assert not same_correction_policy(ba, other)


def test_model_fields_ignored_with_both_switches_off() -> None:
    a, b = _off(_pathway()), _off(_pathway())
    b["results"]["corrections"]["gas"]["unknown"] = [["mace", "x"]]
    b["results"]["corrections"]["version"] = "other"
    _assert_equivalent(a, b, True)


def test_keyless_stamp_is_compatible_present_null_is_not() -> None:
    keyed, keyless = _stamps(_pathway(), _pathway())
    del keyless["correction_policy"]
    assert same_correction_policy(keyed, keyless)
    assert same_correction_policy(keyless, keyed)
    assert same_network_basis(keyed, keyless) and same_network_basis(keyless, keyed)
    null = {**keyed, "correction_policy": None}
    assert not same_correction_policy(null, keyless)
    assert not same_correction_policy(keyless, null)


def test_policy_survives_a_json_round_trip() -> None:
    ba, bb = _stamps(_pathway(), _pathway())
    stored = json.loads(json.dumps(ba))
    assert same_network_basis(stored, bb)
    assert stored["correction_policy"]["model_keys"] == [["mace", "medium"]]


def test_non_bool_enabled_and_malformed_set_key_are_unknown() -> None:
    a = _pathway()
    a["results"]["corrections"]["gas"]["enabled"] = "yes"
    assert correction_policy(a)[0] is None
    b = _pathway()
    b["results"]["corrections"]["h_star"]["sets"]["mace:medium"]["key"] = "mace"
    policy, issues = correction_policy(b)
    assert policy is None and issues
    c = _pathway()
    c["results"]["corrections"]["gas"]["sets"]["mace:medium"]["key"] = ["mace", 3]
    assert correction_policy(c)[0] is None
    d = _pathway()
    d["results"]["corrections"]["gas"]["sets"]["mace:medium"]["key"] = (
        "mace",
        "medium",
    )
    assert correction_policy(d)[0] is None


def test_template_and_policy_differences_are_independent() -> None:
    base = _pathway()
    base["results"]["network_digest"] = None
    tmpl = deepcopy(base)
    tmpl["config"]["template"] = "parked"
    ba, bt = _stamps(base, tmpl)
    assert same_correction_policy(ba, bt)
    assert not same_network_basis(ba, bt)
    pol = _pathway()
    pol["results"]["corrections"]["version"] = "next-table"
    ba, bp = _stamps(base, pol)
    assert not same_correction_policy(ba, bp)
    assert not same_network_basis(ba, bp)
