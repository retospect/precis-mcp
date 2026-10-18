"""Slice 1 of docs/backlog/pcb-board-profile-prototype.md — the board
``profile`` key, its data file, the accessor and the summary line.

Acceptance covered here: criterion 2 (the key round-trips through
``put``/``get``, an unknown value is refused with ``options=``, absent
means ``prod``, the summary shows ``profile:`` only for prototype) and the
accessor contract (``prod`` is the all-off instance; every set rule
carries a reason; no consumer sees a bare string)."""

from __future__ import annotations

from typing import Any

import pytest

from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import profiles
from precis.pcb.capabilities import FIELDS

from .test_pcb_declared_pins import _mini_design

# ── accessor and data (no database) ────────────────────────────────────


def test_prod_is_the_all_off_instance() -> None:
    prod = profiles.profile_rules("prod")
    assert prod == profiles.ProfileRules(name="prod", label=prod.label)
    assert profiles.profile_rules(None) is prod
    assert profiles.profile_rules("") is prod


def test_prototype_defaults_match_the_spec_table() -> None:
    p = profiles.profile_rules("prototype")
    assert (p.trace_width_mm, p.trace_spacing_mm, p.drill_mm) == (0.20, 0.20, 0.30)
    assert p.annular_ring_mm == 0.25
    # Production under-part via cost stays 3.0 in maze; the profile doubles it.
    assert p.via_body_cost_mm == 6.0
    assert p.via_density_max_per_mm2 == 0.25
    assert (p.via_density_window_mm, p.via_density_step_mm) == (10.0, 2.5)
    assert (p.thermal_spokes, p.thermal_spoke_width_mm, p.thermal_gap_mm) == (
        4,
        0.30,
        0.25,
    )
    assert p.refdes_height_mm == 1.2
    assert p.strict_fields == {
        "trace_width_mm",
        "trace_spacing_mm",
        "drill_mm",
        "annular_ring_mm",
    }
    assert p.checks == {
        "starved_thermal",
        "via_density",
        "via_under_part",
        "fine_pitch_pad",
    }


def test_every_set_rule_has_a_reason() -> None:
    for name in profiles.profile_names():
        rules = profiles.profile_rules(name)
        set_rules = {
            k
            for k in vars(rules)
            if k not in {"name", "label", "reasons", "strict_fields", "checks"}
            and getattr(rules, k) is not None
        }
        set_rules |= {k for k in ("strict_fields", "checks") if getattr(rules, k)}
        assert set_rules == set(rules.reasons), name
        assert all(rules.reasons.values()), name


def test_strict_fields_are_capability_fields() -> None:
    for name in profiles.profile_names():
        assert profiles.profile_rules(name).strict_fields <= set(FIELDS)


def test_unknown_profile_lists_options() -> None:
    with pytest.raises(ValueError, match=r"options: \['prod', 'prototype'\]"):
        profiles.profile_rules("dev")


def test_board_profile_defaults_to_prod() -> None:
    assert profiles.board_profile(None) == "prod"
    assert profiles.board_profile({}) == "prod"
    assert profiles.board_profile({"profile": "prototype"}) == "prototype"


@pytest.mark.parametrize(
    "row, match",
    [
        ({"name": "x", "rules": {"drill_mm": {"value": 0.3}}}, "no reason"),
        (
            {"name": "x", "rules": {"drill_mm": {"value": -1, "why": "w"}}},
            "number > 0",
        ),
        (
            {"name": "x", "rules": {"thermal_spokes": {"value": 2.5, "why": "w"}}},
            "int >= 1",
        ),
        ({"name": "x", "rules": {"bogus": {"value": 1, "why": "w"}}}, "unknown rule"),
        (
            {"name": "x", "checks": {"rules": ["nope"], "why": "w"}},
            "unknown",
        ),
        (
            {"name": "x", "strict_fields": {"fields": ["nope"], "why": "w"}},
            "unknown",
        ),
    ],
)
def test_bad_rows_are_rejected_at_load(row: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        profiles._build(row)


# ── the key on a board (database) ──────────────────────────────────────


@pytest.fixture
def pcb(store: Any) -> PcbHandler:
    from precis.dispatch import Hub

    return PcbHandler(hub=Hub(store=store))


@pytest.mark.db
def test_absent_profile_is_prod_and_summary_is_silent(pcb: PcbHandler) -> None:
    pcb.put(id="prof-a", args=_mini_design())
    assert "profile:" not in pcb.get(id="prof-a").body


@pytest.mark.db
def test_op_profile_round_trips_and_summary_shows_prototype(
    pcb: PcbHandler, store: Any
) -> None:
    pcb.put(id="prof-b", args=_mini_design())
    ref_id = store.get_ref(kind="pcb", id="prof-b").id
    out = pcb.put(id="prof-b", args={"op": "profile", "profile": "prototype"})
    assert "profile = prototype" in out.body
    assert profiles.board_profile(store.pcb_graph(ref_id)["board"]["meta"]) == (
        "prototype"
    )
    assert "profile: prototype" in pcb.get(id="prof-b").body
    # Back to prod removes the key: the board meta is as before.
    pcb.put(id="prof-b", args={"op": "profile", "profile": "prod"})
    assert "profile" not in store.pcb_graph(ref_id)["board"]["meta"]
    assert "profile:" not in pcb.get(id="prof-b").body


@pytest.mark.db
def test_op_profile_refuses_unknown_with_options(pcb: PcbHandler) -> None:
    pcb.put(id="prof-c", args=_mini_design())
    with pytest.raises(BadInput) as exc:
        pcb.put(id="prof-c", args={"op": "profile", "profile": "dev"})
    assert exc.value.options == ["prod", "prototype"]
    with pytest.raises(BadInput) as missing:
        pcb.put(id="prof-c", args={"op": "profile"})
    assert missing.value.options == ["prod", "prototype"]


@pytest.mark.db
def test_profile_at_create(pcb: PcbHandler, store: Any) -> None:
    pcb.put(id="prof-d", args={**_mini_design(), "profile": "prototype"})
    ref_id = store.get_ref(kind="pcb", id="prof-d").id
    assert store.pcb_graph(ref_id)["board"]["meta"]["profile"] == "prototype"
    assert "profile: prototype" in pcb.get(id="prof-d").body


@pytest.mark.db
def test_bad_profile_at_create_stores_nothing(pcb: PcbHandler, store: Any) -> None:
    with pytest.raises(BadInput) as exc:
        pcb.put(id="prof-e", args={**_mini_design(), "profile": "dev"})
    assert exc.value.options == ["prod", "prototype"]
    assert store.get_ref(kind="pcb", id="prof-e") is None
