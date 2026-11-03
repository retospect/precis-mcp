"""gr553328 — the profile-vs-kinetics conditions labelling
(``precis.utils.pathway_conditions``).

A pathway whose kinetics solve ran at a stated environment must show BOTH
condition sets — the energy profile/span at its own T and gas table, the
kinetics at the environment's T and partial pressures — with the values
read off the stored record (never hard-coded), and a closing sentence that
follows the recorded tables: a 1-bar profile excludes the kinetics'
kT·ln(p/p°) terms; a profile priced at its own table already carries them,
so only the difference is excluded; equal table and T means the conditions
match. A pathway without a recorded kinetics environment keeps the
previous text everywhere.

Engine-free: the views exercise ``toon_views``/``handler.get`` on seeded
meta; ``PathwayHandler.__new__`` skips ``__init__``'s autocatpath probe
(same trick as ``test_pathway_kinetics_glue._recompute_handler``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from precis.utils.pathway_conditions import (
    conditions_note,
    kinetics_environment,
    methods_conditions,
    profile_conditions,
)

#: A ``kinetics_v2.environment`` record as ``Environment.resolved()``
#: writes it — stated p_total, resolved (bar) gas tables.
_ENV = {
    "T": 500.0,
    "p_total": 1.05,
    "p_total_stated": True,
    "gases": {
        "inputs": {"NO": 0.05, "H2": 0.9},
        "outputs": {"NH3": 1e-5},
        "inerts": {},
        "unlisted": "refuse",
    },
}

_BASIS = "ab" * 32

#: The profile's recorded gas table, priced per state (the
#: ``results.thermo`` record — ``runner._stored_node_pressures``'s read).
_THERMO = {"mode": "table", "temperature": 298.15, "pressures": {"NO": 0.5}}


def _meta(**results: Any) -> dict[str, Any]:
    return {"status": "ready", "results": dict(results)}


# ── kinetics_environment ─────────────────────────────────────────────


def test_env_v2_record_flattens_gas_tables() -> None:
    env = kinetics_environment(
        {
            "kinetics": {"environment_basis": _BASIS},  # v1-only key fills in
            "kinetics_v2": {"tof_mf": 1.0, "environment": _ENV},
        }
    )
    assert env is not None
    assert env["T"] == 500.0
    assert env["pressures"] == {"NO": 0.05, "H2": 0.9, "NH3": 1e-5}
    assert env["p_total"] == 1.05
    assert env["legacy"] is False
    assert env["basis"] == _BASIS[:12]


def test_env_v1_conditions_fallback() -> None:
    env = kinetics_environment(
        {"kinetics": {"conditions": {"temperature": 500.0, "pressures": {"NO": 0.05}}}}
    )
    assert env is not None
    assert env["T"] == 500.0
    assert env["pressures"] == {"NO": 0.05}


def test_env_legacy_record_marks_itself() -> None:
    env = kinetics_environment(
        {"kinetics_v2": {"environment": {"T": 400.0, "legacy": True, "gases": {}}}}
    )
    assert env is not None
    assert env["legacy"] is True
    assert env["pressures"] == {}


def test_env_absent_without_a_record() -> None:
    assert kinetics_environment({}) is None
    assert kinetics_environment({"kinetics": {"tof": 1.0}}) is None
    assert kinetics_environment({"kinetics_v2": {"tof_mf": 1.0}}) is None


# ── profile_conditions ───────────────────────────────────────────────


def test_profile_reads_the_thermo_record() -> None:
    """The profile T and gas table come off ``results.thermo`` — the same
    record the engine's energy-tier sentence and
    ``runner._stored_node_pressures`` read."""
    pc = profile_conditions({"results": {"thermo": _THERMO}})
    assert pc == {"T": 298.15, "pressures": {"NO": 0.5}}


def test_profile_t_falls_back_to_config_conditions() -> None:
    """No thermo temperature -> ``config.conditions.temperature`` — the
    second link in the engine's own chain. ``results.T`` is the
    electrochemistry lever's T and must NOT be read as the profile's."""
    pc = profile_conditions(
        {
            "results": {"T": 999.0},  # electro lever T — not the profile's
            "config": {"conditions": {"temperature": 250.0}},
        }
    )
    assert pc == {"T": 250.0, "pressures": {}}


def test_profile_t_unrecorded_is_none_never_defaulted() -> None:
    assert profile_conditions({}) == {"T": None, "pressures": {}}


def test_note_says_t_not_recorded() -> None:
    note = conditions_note(_meta(kinetics_v2={"environment": _ENV}))
    assert note is not None
    assert "T not recorded" in note
    assert "298.15" not in note


# ── the three _kt_sentence cases ──────────────────────────────────────


def test_case_a_profile_at_1bar_excludes_the_gas_terms() -> None:
    """Empty profile table -> the 1 bar standard state: the profile's ΔG
    excludes the kinetics' kT·ln(p/p°) terms outright."""
    meta = _meta(kinetics_v2={"environment": _ENV}, thermo={"mode": "table"})
    note = conditions_note(meta)
    assert note is not None
    assert "all gases at the 1 bar reference" in note
    assert "excludes the kT·ln(p/p°) gas terms the kinetics solve includes" in note


def test_case_b_own_priced_table_only_the_difference_excluded() -> None:
    """A profile priced at its own gas table already carries kT·ln(p/p°)
    terms — the note lists BOTH tables and says only the difference is
    excluded; it must NOT claim the terms are excluded outright."""
    meta = _meta(kinetics_v2={"environment": _ENV}, thermo=_THERMO)
    note = conditions_note(meta)
    assert note is not None
    assert "p(NO) = 0.5 bar" in note  # the profile's own table, listed
    assert "p(NH3) = 1e-05 bar" in note  # the kinetics' table, listed
    assert "only the difference between the two tables is excluded" in note
    assert "excludes the kT·ln(p/p°) gas terms the kinetics" not in note


def test_case_c_same_table_and_t_conditions_match() -> None:
    """Same gas table, same T: the note says the conditions match —
    comparable, no excluded-terms claim."""
    same_env = {
        "T": 298.15,
        "gases": {"inputs": {"NO": 0.5}, "unlisted": "refuse"},
    }
    meta = _meta(kinetics_v2={"environment": same_env}, thermo=_THERMO)
    note = conditions_note(meta)
    assert note is not None
    assert "directly comparable" in note
    assert "excludes" not in note


def test_case_a_prime_same_1bar_table_different_t() -> None:
    """Both sides at the 1 bar gas reference but different T: the
    difference is thermal, not a ln(p) term."""
    env = {"T": 500.0, "gases": {"unlisted": "refuse"}}
    meta = _meta(
        kinetics_v2={"environment": env},
        thermo={"mode": "table", "temperature": 298.15},
    )
    note = conditions_note(meta)
    assert note is not None
    assert "is thermal (the temperature)" in note
    assert "excludes the kT·ln(p/p°)" not in note


# ── methods_conditions ───────────────────────────────────────────────


def test_methods_block_states_both_and_the_basis() -> None:
    md = methods_conditions(
        _meta(
            kinetics={"environment_basis": _BASIS},
            kinetics_v2={"environment": _ENV},
            thermo=_THERMO,
        )
    )
    assert md is not None
    assert md.startswith("## Conditions")
    assert "Energy profile and span: T = 298.15 K, p(NO) = 0.5 bar" in md
    assert "Kinetics: T = 500 K" in md
    assert f"basis {_BASIS[:12]}" in md
    assert "only the difference between the two tables is excluded" in md


def test_methods_flags_a_disagreeing_hardcoded_body() -> None:
    """The stored Methods text hard-codes 298.15 K / 1 bar; when the record
    disagrees the appended block names which values are recorded."""
    body = "# Methods\n\nAll values are Gibbs free-energy differences at 298.15 K (all gases at the 1 bar reference)."
    md = methods_conditions(
        _meta(kinetics_v2={"environment": _ENV}, thermo=_THERMO),
        body=body,
    )
    assert md is not None
    assert "the values listed here are the recorded ones" in md
    # agreement -> no flag
    md2 = methods_conditions(
        _meta(kinetics_v2={"environment": _ENV}, thermo={"mode": "table"}),
        body=body,
    )
    assert md2 is not None
    assert "recorded ones" not in md2
    # no body passed -> no flag either way
    md3 = methods_conditions(_meta(kinetics_v2={"environment": _ENV}, thermo=_THERMO))
    assert md3 is not None
    assert "recorded ones" not in md3


def test_note_and_methods_none_without_kinetics_env() -> None:
    meta = _meta(target="NH3")
    assert conditions_note(meta) is None
    assert methods_conditions(meta) is None


# ── MCP views ────────────────────────────────────────────────────────

_GRAPH: dict[str, Any] = {
    "directed": True,
    "nodes": [{"id": "A"}, {"id": "B"}],
    "links": [
        {
            "source": "A",
            "target": "B",
            "barrier": 0.5,
            "barrier_std": 0.02,
            "delta_e": -0.1,
            "low_confidence": False,
            "kind": "reaction",
        }
    ],
}
_RESULTS = {
    "pathway": ["A", "B"],
    "target": "NH3",
    "thermo": _THERMO,
    "kinetics_v2": {"tof_mf": 1.0, "environment": _ENV},
}


def _handler(meta: dict[str, Any], body_text: str | None = None) -> Any:
    """A PathwayHandler over a one-ref fake store — engine-free
    (``__new__`` skips ``__init__``'s autocatpath probe)."""
    from precis_pathway.handler import PathwayHandler

    ref = SimpleNamespace(id=11, slug="pw-a", title="NO→NH3 on Pd", meta=meta)
    blocks = [SimpleNamespace(text=body_text)] if body_text else []
    store = SimpleNamespace(
        get_ref=lambda *, kind, id: ref,
        chunks=SimpleNamespace(list_chunks_for_ref=lambda _rid: blocks),
    )
    h = PathwayHandler.__new__(PathwayHandler)
    setattr(h, "hub", SimpleNamespace(live_store=store))
    return h


def _body(resp_body: str) -> str:
    """The view body minus the gr553326 ``pw<id> · /refs/pathway/<id>``
    header line every prose view now leads with."""
    first, _, rest = resp_body.partition("\n")
    assert "· /refs/pathway/" in first, f"no handle header line: {first!r}"
    return rest


def test_methods_view_appends_the_conditions_block() -> None:
    h = _handler(
        {**_meta(**_RESULTS), "config": {}},
        body_text="# Methods\n\nAll values are Gibbs free-energy differences.",
    )
    body = _body(h.get(id="pw-a", view="methods").body)
    assert body.startswith("# Methods")
    assert "## Conditions" in body
    assert "p(NO) = 0.05 bar" in body
    assert "only the difference between the two tables is excluded" in body


def test_methods_view_unchanged_without_kinetics_env() -> None:
    h = _handler(_meta(target="NH3"), body_text="# Methods\n\nplain.")
    assert _body(h.get(id="pw-a", view="methods").body) == "# Methods\n\nplain."


def test_profile_view_carries_the_note() -> None:
    meta = _meta(**_RESULTS)
    meta["results"]["nodes"] = {"A": {"mean": 0.0, "std": 0.0}}
    body = _handler(meta).get(id="pw-a", view="profile").body
    assert "conditions:" in body
    assert "kinetics at T = 500 K" in body


def test_analysis_and_kinetics_views_carry_the_note() -> None:
    meta = _meta(**_RESULTS)
    meta["graph"] = _GRAPH
    h = _handler(meta)
    analysis = h.get(id="pw-a", view="analysis").body
    assert "conditions:" in analysis
    assert "kinetics at T = 500 K" in analysis
    kinetics = h.get(id="pw-a", view="kinetics").body
    assert "conditions:" in kinetics
    assert "p(NH3) = 1e-05 bar" in kinetics


def test_views_unchanged_without_kinetics_env() -> None:
    meta = _meta(pathway=["A", "B"], target="NH3")
    meta["graph"] = _GRAPH
    meta["results"]["nodes"] = {"A": {"mean": 0.0, "std": 0.0}}
    h = _handler(meta)
    assert "conditions:" not in h.get(id="pw-a", view="analysis").body
    assert "conditions:" not in h.get(id="pw-a", view="kinetics").body
    assert "conditions:" not in h.get(id="pw-a", view="profile").body
