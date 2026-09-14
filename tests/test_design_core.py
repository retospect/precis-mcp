"""The shared design core (``precis.design``, migration 0162).

Four subsystems the ``se`` kind rents — at macro scale and in atomic mode
alike — instead of building each twice: scenarios + service environments,
per-number provenance, design history, and discrete block states. What
these tests pin is what the renter will rely on and what a later migration
must not quietly break:

* the scenario presets are seeded and genuinely *differ* (weights and the
  lifetime master switch) — a preset set that collapsed to one row would
  pass a "row exists" check and be useless;
* the standard load-case library applies by DEFAULT and comes off only
  with a reason;
* provenance validators reject malformed sidecars loudly at write time
  (a malformed sidecar is worse than none — it reads as provenance and
  audits as nothing);
* ``driver_kind`` is closed at BOTH layers, Python and the DB CHECK;
* current state is per BLOCK — two blocks in different states at once is
  the photoswitch case and the thing a design-level pointer cannot do;
* a cache key over a state-carrying block carries the state (addendum A9).
"""

from __future__ import annotations

import psycopg
import pytest

from precis.design import history, provenance, scenarios, states, uids
from precis.design.provenance import ProvenanceError
from precis.design.states import BlockState, StateError, Transition
from precis.store import Store


def _design_ref(store: Store, title: str = "design under test") -> int:
    """A ``refs`` row to hang design-core rows off. The kind is irrelevant
    here — the design tables FK to ``refs``, not to a kind."""
    ref = store.insert_ref(kind="memory", slug=None, title=title)
    return int(ref.id)


# ── scenarios + service environments ───────────────────────────────────────


def test_presets_are_seeded_and_differ(store: Store) -> None:
    ids = {s.scenario_id for s in scenarios.list_scenarios(store)}
    assert set(scenarios.PRESET_SCENARIOS) <= ids

    proto = scenarios.get_scenario(store, "prototype")
    batch = scenarios.get_scenario(store, "small_batch")
    mass = scenarios.get_scenario(store, "mass_production")
    assert proto is not None and batch is not None and mass is not None

    # Different weights is the point of having presets at all.
    assert proto.objective_weights != batch.objective_weights
    assert batch.objective_weights != mass.objective_weights
    assert proto.quantity is not None and mass.quantity is not None
    assert proto.quantity < mass.quantity

    # The lifetime master switch: a week on a bench drops the corrosion/
    # creep/fatigue family entirely; mass production runs it.
    assert proto.lifetime_checks is False
    assert mass.lifetime_checks is True
    assert proto.service_environment is not None
    assert proto.service_environment.expected_lifetime_s is not None


def test_unknown_scenario_is_none(store: Store) -> None:
    assert scenarios.get_scenario(store, "no_such_scenario") is None


def test_design_scenario_round_trips_and_replaces(store: Store) -> None:
    ref_id = _design_ref(store)
    assert scenarios.design_scenario(store, ref_id) is None

    scenarios.set_design_scenario(store, ref_id, "prototype", set_by="test")
    chosen = scenarios.design_scenario(store, ref_id)
    assert chosen is not None and chosen.scenario_id == "prototype"

    # One scenario per design — a second choice replaces, never accumulates.
    scenarios.set_design_scenario(store, ref_id, "mass_production")
    chosen = scenarios.design_scenario(store, ref_id)
    assert chosen is not None and chosen.scenario_id == "mass_production"
    assert chosen.lifetime_checks is True


def test_standard_load_cases_apply_by_default(store: Store) -> None:
    ref_id = _design_ref(store)
    standard = {c.case_id for c in scenarios.standard_load_cases(store)}
    assert {"std_shock", "std_vibration", "std_off_axis"} <= standard

    # No scenario chosen yet, and the standard library still applies: that
    # asymmetry is what catches a design that only works statically.
    applied = {c.case_id for c in scenarios.load_cases_for(store, ref_id)}
    assert standard <= applied


def test_exemption_needs_a_reason_and_removes_the_case(store: Store) -> None:
    ref_id = _design_ref(store)
    with pytest.raises(ValueError, match="needs a reason"):
        scenarios.exempt_load_case(store, ref_id, "std_shock", "   ")

    scenarios.exempt_load_case(
        store, ref_id, "std_shock", "bench fixture, never handled", set_by="test"
    )
    applied = {c.case_id for c in scenarios.load_cases_for(store, ref_id)}
    assert "std_shock" not in applied
    assert "std_vibration" in applied
    assert scenarios.exemptions(store, ref_id) == {
        "std_shock": "bench fixture, never handled"
    }

    assert scenarios.unexempt_load_case(store, ref_id, "std_shock") is True
    assert scenarios.unexempt_load_case(store, ref_id, "std_shock") is False
    assert "std_shock" in {c.case_id for c in scenarios.load_cases_for(store, ref_id)}


# ── uid mint ───────────────────────────────────────────────────────────────


def test_uids_are_unique_and_monotonic(store: Store) -> None:
    first = uids.mint_uid(store)
    batch = uids.mint_uids(store, 5)
    assert len(batch) == 5
    assert len(set(batch)) == 5
    assert batch == sorted(batch)
    assert all(u > first for u in batch)
    assert uids.mint_uids(store, 0) == []
    with pytest.raises(ValueError):
        uids.mint_uids(store, -1)


# ── provenance ─────────────────────────────────────────────────────────────


def test_provenance_entry_normalizes(store: Store) -> None:
    entry = provenance.entry(
        source="solver",
        fidelity="full_solve",
        solver_id="  fea:beam-v3 ",
        assumptions=["linear elastic"],
        margin_origin="fatigue_knockdown",
    )
    assert entry["solver_id"] == "fea:beam-v3"
    assert entry["assumptions"] == ["linear elastic"]
    # Canonical key order, so a stored entry compares equal to a fresh one.
    assert list(entry) == [
        "source",
        "fidelity",
        "solver_id",
        "assumptions",
        "margin_origin",
    ]
    # assumptions is always present, even when nobody declared any.
    assert provenance.entry(source="user_stated")["assumptions"] == []


@pytest.mark.parametrize(
    "bad",
    [
        "not a mapping",
        {"source": "guessed"},  # unknown source
        {"source": "solver", "fidelity": "vibes"},  # unknown fidelity
        {"source": "solver"},  # computed number with no rung
        {"source": "library", "fidelity": "template"},  # no library_version
        {"source": "user_stated", "confidence": 0.9},  # unknown key
        {"source": "user_stated", "assumptions": "linear elastic"},  # not a list
        {"source": "user_stated", "assumptions": [""]},  # empty member
        {"source": "user_stated", "solver_id": ""},  # empty string field
    ],
)
def test_provenance_rejects_malformed_entries(bad: object) -> None:
    with pytest.raises(ProvenanceError):
        provenance.validate_entry(bad)


def test_provenance_allows_stated_numbers_without_a_rung() -> None:
    # A number somebody stated or measured has no solver rung to declare;
    # requiring one would push callers to make one up.
    assert provenance.validate_entry({"source": "measured"})["source"] == "measured"


def test_sidecar_validation_and_queries() -> None:
    sidecar = provenance.validate_sidecar(
        {
            "length": {"source": "user_stated"},
            "wall": {
                "source": "solver",
                "fidelity": "analytic",
                "margin_origin": "fatigue_knockdown",
            },
            "bearing_life": {
                "source": "library",
                "fidelity": "template",
                "library_version": "skf-2026.1",
            },
        }
    )
    assert set(sidecar) == {"length", "wall", "bearing_life"}
    # The margin audit is a query over the sidecar, not a feature.
    assert provenance.margin_audit(sidecar) == {"wall": "fatigue_knockdown"}
    # The fidelity ladder, likewise. Stated numbers aren't on the ladder.
    assert provenance.below_fidelity(sidecar, "full_solve") == [
        "bearing_life",
        "wall",
    ]
    assert provenance.missing_provenance(["length", "mass"], sidecar) == ["mass"]
    assert provenance.validate_sidecar(None) == {}
    with pytest.raises(ProvenanceError):
        provenance.validate_sidecar([("length", {"source": "user_stated"})])


def test_merged_replaces_whole_entries() -> None:
    base = {"wall": provenance.entry(source="llm_assumed", assumptions=["guessed"])}
    update = {"wall": provenance.entry(source="solver", fidelity="full_solve")}
    out = provenance.merged(base, update)
    # A re-solve replaces the number's whole entry: keeping the old
    # assumptions beside a new fidelity would be a lie.
    assert out["wall"]["source"] == "solver"
    assert out["wall"]["assumptions"] == []


def test_load_provenance_collapses_into_the_shared_enum() -> None:
    entry = provenance.from_load_provenance("derived_by_statics")
    assert entry["source"] == "derived"
    assert entry["fidelity"] == "analytic"
    assert entry["solver_id"] == "statics"
    assert provenance.from_load_provenance("user_stated")["source"] == "user_stated"
    with pytest.raises(ProvenanceError):
        provenance.from_load_provenance("derived_by_vibes")


# ── design history ─────────────────────────────────────────────────────────


def test_envelope_revision_mints_and_logs(store: Store) -> None:
    ref_id = _design_ref(store)
    # A fresh design has never tightened; 0 is an answer, not a gap.
    assert history.current_revision(store, ref_id) == 0

    assert history.mint_envelope_revision(store, ref_id, reason="bore too tight") == 1
    assert (
        history.mint_envelope_revision(store, ref_id, reason="again", block_uid=7) == 2
    )
    assert history.current_revision(store, ref_id) == 2

    log = history.revision_log(store, ref_id)
    assert [r["revision"] for r in log] == [1, 2]
    assert log[0]["reason"] == "bore too tight"
    assert log[1]["block_uid"] == 7

    # Revisions are per design, not global.
    other = _design_ref(store, "second design")
    assert history.mint_envelope_revision(store, other) == 1


def test_checkpoint_round_trips(store: Store) -> None:
    ref_id = _design_ref(store)
    history.mint_envelope_revision(store, ref_id, reason="tighten")
    payload = {"blocks": [{"uid": 11, "name": "fork", "envelope": "box(1,2,3)"}]}

    saved = history.save_checkpoint(
        store,
        ref_id,
        label="before-pin",
        payload=payload,
        headline={"mass": 2.5},
        reason="about to try a lighter fork",
    )
    assert saved.envelope_revision == 1

    loaded = history.load_checkpoint(store, ref_id, "before-pin")
    assert loaded is not None
    # Payload verbatim: core stores the plugin's own serialisation and
    # never interprets it.
    assert loaded.payload == payload
    assert loaded.headline == {"mass": 2.5}

    # The list is a menu, so it carries no payloads.
    listed = history.list_checkpoints(store, ref_id)
    assert [c.label for c in listed] == ["before-pin"]
    assert listed[0].payload == {}

    # Re-saving the same label replaces rather than accumulating.
    history.save_checkpoint(
        store,
        ref_id,
        label="before-pin",
        payload={"blocks": []},
        headline={"mass": 2.1},
    )
    assert len(history.list_checkpoints(store, ref_id)) == 1
    again = history.load_checkpoint(store, ref_id, "before-pin")
    assert again is not None and again.payload == {"blocks": []}

    assert history.delete_checkpoint(store, ref_id, "before-pin") is True
    assert history.load_checkpoint(store, ref_id, "before-pin") is None


def test_branch_records_parent_and_compares(store: Store) -> None:
    parent_id = _design_ref(store, "parent design")
    child_id = _design_ref(store, "branch design")
    history.mint_envelope_revision(store, parent_id, reason="tighten")

    with pytest.raises(ValueError, match="one-line reason"):
        history.record_branch(store, child_id, reason="  ", parent_ref_id=parent_id)

    branch = history.record_branch(
        store,
        child_id,
        reason="lighter fork, thinner wall",
        parent_ref_id=parent_id,
        headline={"mass": 2.6, "worst_utilisation": 0.92, "cost": 41.0},
    )
    assert branch.parent_ref_id == parent_id
    assert branch.envelope_revision == 1

    assert history.get_branch(store, child_id) == branch
    assert [b.id for b in history.list_branches(store, parent_ref_id=parent_id)] == [
        branch.id
    ]
    # A design that is not a branch has no record.
    assert history.get_branch(store, parent_id) is None

    result = history.branch_result(
        branch,
        parent_headline={"mass": 2.5, "worst_utilisation": 0.78, "part_count": 12},
    )
    assert result.branch_id == branch.id
    assert result.infeasible is None
    delta = result.delta_vs_parent
    assert delta["mass"]["delta"] == pytest.approx(0.1)
    assert delta["mass"]["pct"] == pytest.approx(4.0)
    # One-sided keys still surface — that a branch dropped a headline
    # number is itself the news.
    assert delta["cost"]["from"] is None and delta["cost"]["to"] == 41.0
    assert delta["part_count"]["to"] is None
    # Unchanged keys are omitted.
    assert "unchanged" not in delta


def test_branch_result_carries_what_broke(store: Store) -> None:
    parent_id = _design_ref(store, "parent")
    child_id = _design_ref(store, "child")
    branch = history.record_branch(
        store, child_id, reason="try press fit", parent_ref_id=parent_id
    )
    result = history.branch_result(
        branch,
        parent_headline={"mass": 1.0},
        infeasible={"what_broke": "hoop stress", "which_constraint": "yield"},
    )
    assert result.infeasible == {
        "what_broke": "hoop stress",
        "which_constraint": "yield",
    }


def test_headline_delta_handles_zero_and_non_numeric() -> None:
    delta = history.headline_delta(
        {"mass": 1.0, "process": "milled", "ok": True},
        {"mass": 0.0, "process": "printed", "ok": False},
    )
    # No honest percentage exists against a zero baseline.
    assert delta["mass"]["delta"] == pytest.approx(1.0)
    assert delta["mass"]["pct"] is None
    assert delta["process"] == {"from": "printed", "to": "milled"}
    assert "delta" not in delta["ok"]


# ── discrete states ────────────────────────────────────────────────────────


def _azobenzene(store: Store, ref_id: int) -> int:
    """One block with the E/Z pair and both directed transitions."""
    uid = uids.mint_uid(store)
    states.set_states(
        store,
        ref_id,
        uid,
        [
            BlockState(uid, "E", envelope="box(1.2e-9,0.5e-9,0.5e-9)"),
            BlockState(
                uid,
                "Z",
                envelope="box(0.9e-9,0.5e-9,0.5e-9)",
                port_pose_overrides={"far": {"xyz": [0.9e-9, 0.0, 0.0]}},
            ),
        ],
    )
    states.set_transitions(
        store,
        ref_id,
        uid,
        [
            Transition(uid, "E", "Z", "light", "365nm", {"quantum_yield": 0.2}),
            Transition(uid, "Z", "E", "thermal", None, {"barrier_kj_mol": 96.0}),
        ],
    )
    return uid


def test_states_and_transitions_round_trip(store: Store) -> None:
    ref_id = _design_ref(store)
    uid = _azobenzene(store, ref_id)

    declared = states.states_for(store, ref_id, uid)
    assert [s.name for s in declared] == ["E", "Z"]
    assert declared[1].port_pose_overrides == {"far": {"xyz": [0.9e-9, 0.0, 0.0]}}

    edges = states.transitions_for(store, ref_id, uid)
    assert [(t.from_state, t.to_state, t.driver_kind) for t in edges] == [
        ("E", "Z", "light"),
        ("Z", "E", "thermal"),
    ]
    # Directed edges: forward and reverse carry independent params, which
    # is exactly what a ratchet needs (addendum A9).
    assert edges[0].params["quantum_yield"] == pytest.approx(0.2)
    assert edges[1].params["barrier_kj_mol"] == pytest.approx(96.0)

    assert states.design_states(store, ref_id)[uid] == declared


def test_redeclaring_states_keeps_untouched_transitions(store: Store) -> None:
    ref_id = _design_ref(store)
    uid = _azobenzene(store, ref_id)
    # Re-declaring the same states (with an edited envelope) must not
    # cascade away edges that never changed.
    states.set_states(
        store,
        ref_id,
        uid,
        [
            BlockState(uid, "E", envelope="box(1.3e-9,0.5e-9,0.5e-9)"),
            BlockState(uid, "Z"),
        ],
    )
    assert len(states.transitions_for(store, ref_id, uid)) == 2
    assert (
        states.states_for(store, ref_id, uid)[0].envelope == "box(1.3e-9,0.5e-9,0.5e-9)"
    )


def test_pruning_the_posed_state_is_refused(store: Store) -> None:
    """design_block_state cascades on a pruned state row, which would
    silently unpose the block (current_state reads back None,
    indistinguishable from never-posed). set_states must refuse instead."""
    ref_id = _design_ref(store)
    uid = _azobenzene(store, ref_id)
    states.set_current_state(store, ref_id, uid, "Z")

    with pytest.raises(StateError, match="currently posed in state 'Z'"):
        states.set_states(store, ref_id, uid, [BlockState(uid, "E")])

    # The row survives the refused call — no silent unposing happened.
    assert states.current_state(store, ref_id, uid) == "Z"

    # Re-posing to a surviving state, or keeping the posed state in the
    # declaration, both clear the guard.
    states.set_current_state(store, ref_id, uid, "E")
    states.set_states(store, ref_id, uid, [BlockState(uid, "E")])
    assert states.current_state(store, ref_id, uid) == "E"

    states.set_states(
        store,
        ref_id,
        uid,
        [BlockState(uid, "E"), BlockState(uid, "Z")],
    )
    states.set_current_state(store, ref_id, uid, "Z")
    states.set_states(
        store,
        ref_id,
        uid,
        [BlockState(uid, "E"), BlockState(uid, "Z")],
    )
    assert states.current_state(store, ref_id, uid) == "Z"


def test_duplicate_state_names_are_refused(store: Store) -> None:
    ref_id = _design_ref(store)
    uid = uids.mint_uid(store)
    with pytest.raises(StateError, match="twice"):
        states.set_states(
            store, ref_id, uid, [BlockState(uid, "E"), BlockState(uid, "E")]
        )


def test_driver_kind_is_closed_in_python(store: Store) -> None:
    ref_id = _design_ref(store)
    uid = _azobenzene(store, ref_id)
    with pytest.raises(StateError, match="closed"):
        states.set_transitions(
            store, ref_id, uid, [Transition(uid, "E", "Z", "wavelength")]
        )
    # `mechanical` IS in the enum — the macro adopters' member (a compliant
    # bistable snapping through), expressible with zero schema change.
    states.set_transitions(
        store,
        ref_id,
        uid,
        [Transition(uid, "E", "Z", "mechanical", "push-rod", {"snap_force_n": 4.2})],
    )
    assert states.transitions_for(store, ref_id, uid)[0].driver_kind == "mechanical"


def test_driver_kind_is_closed_in_the_database(store: Store) -> None:
    ref_id = _design_ref(store)
    uid = _azobenzene(store, ref_id)
    with pytest.raises(psycopg.errors.CheckViolation):
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO design_transitions "
                "(ref_id, block_uid, from_state, to_state, driver_kind) "
                "VALUES (%s, %s, 'E', 'Z', 'magic')",
                (ref_id, uid),
            )


def test_transition_endpoints_must_be_declared_states(store: Store) -> None:
    ref_id = _design_ref(store)
    uid = _azobenzene(store, ref_id)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        states.set_transitions(
            store, ref_id, uid, [Transition(uid, "E", "Q", "thermal")]
        )


def test_current_state_is_per_block(store: Store) -> None:
    ref_id = _design_ref(store)
    first = _azobenzene(store, ref_id)
    second = _azobenzene(store, ref_id)

    states.set_current_state(store, ref_id, first, "Z")
    states.set_current_state(store, ref_id, second, "E")
    # Two independently switchable blocks in DIFFERENT states at once —
    # the photoswitch case a design-level pointer cannot represent.
    assert states.current_state(store, ref_id, first) == "Z"
    assert states.current_state(store, ref_id, second) == "E"
    assert states.current_states(store, ref_id) == {first: "Z", second: "E"}

    # Never posed = no row, and that is not an error.
    assert states.current_state(store, ref_id, uids.mint_uid(store)) is None


def test_undeclared_current_state_is_refused(store: Store) -> None:
    ref_id = _design_ref(store)
    uid = _azobenzene(store, ref_id)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        states.set_current_state(store, ref_id, uid, "Q")


def test_current_state_is_per_design_not_per_uid(store: Store) -> None:
    """The theft scenario: block uids are PRESERVED across a branch copy
    (same uid, different ref_id — design_branches), so posing a branch's
    copy must never steal or overwrite the parent's current-state row for
    the same uid."""
    parent_id = _design_ref(store, "parent design")
    branch_id = _design_ref(store, "branch design")
    uid = uids.mint_uid(store)

    # Both designs declare the same states for the same uid — exactly what
    # a naive full-copy branch produces (uid preserved, ref_id distinct).
    for ref in (parent_id, branch_id):
        states.set_states(
            store,
            ref,
            uid,
            [BlockState(uid, "E"), BlockState(uid, "Z")],
        )

    states.set_current_state(store, parent_id, uid, "Z")
    # Posing the branch's copy of the same uid must not touch the parent's
    # row — that would be the branch reaching back and stealing state.
    states.set_current_state(store, branch_id, uid, "E")

    assert states.current_state(store, parent_id, uid) == "Z"
    assert states.current_state(store, branch_id, uid) == "E"

    # Re-posing the branch again still leaves the parent's row untouched.
    states.set_current_state(store, branch_id, uid, "Z")
    assert states.current_state(store, parent_id, uid) == "Z"
    assert states.current_state(store, branch_id, uid) == "Z"


def test_cache_key_carries_the_block_state(store: Store) -> None:
    """The A9 hysteresis rule, mechanised: a key over a state-carrying
    block must change when the block switches."""
    ref_id = _design_ref(store)
    uid = _azobenzene(store, ref_id)

    states.set_current_state(store, ref_id, uid, "E")
    in_e = states.cache_key(store, ref_id, "clearance:fork")
    states.set_current_state(store, ref_id, uid, "Z")
    in_z = states.cache_key(store, ref_id, "clearance:fork")
    assert in_e != in_z
    assert str(uid) in in_z

    # A design with no state-carrying block gets the bare key — a
    # single-state block never changes, so splitting the cache on it would
    # buy nothing.
    plain = _design_ref(store, "no states here")
    assert states.cache_key(store, plain, "clearance:fork") == "clearance:fork"


def test_state_cache_key_is_order_independent() -> None:
    assert states.state_cache_key("base", {2: "Z", 1: "E"}) == states.state_cache_key(
        "base", {1: "E", 2: "Z"}
    )
    assert states.state_cache_key("base", {}) == "base"


def test_state_carrying_uids_ignores_single_state_blocks(store: Store) -> None:
    ref_id = _design_ref(store)
    two_state = _azobenzene(store, ref_id)
    one_state = uids.mint_uid(store)
    states.set_states(store, ref_id, one_state, [BlockState(one_state, "only")])
    assert states.state_carrying_uids(store, ref_id) == {two_state}


def test_situation_table_is_a_stub_with_ids(store: Store) -> None:
    """The situation stub exists so ids can be referenced; the rule table
    itself is build-order step 3 and is deliberately absent."""
    ref_id = _design_ref(store)
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO design_situations (ref_id, name, descr) "
            "VALUES (%s, 'assembly', 'slide the fork in') RETURNING id",
            (ref_id,),
        ).fetchone()
        assert row is not None and int(row[0]) > 0
        cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'design_situations'"
            ).fetchall()
        }
    assert cols == {"id", "ref_id", "name", "descr", "created_at"}
