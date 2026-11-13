"""plans.outcome — the hard-gate / sampled-quality split (gr595545).

Pure tests against fake stores/conns: no DB, no sessions.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from precis.plans import outcome
from precis.plans.lanes import ANY_CLASS, COST_FREE, COST_SUBSCRIPTION, Lane
from precis.store import Store
from precis.workers import run_quality

# ── fakes ──────────────────────────────────────────────────────────


@dataclass
class _Ref:
    id: int
    kind: str
    meta: dict[str, Any] = field(default_factory=dict)
    parent_id: int | None = None


@dataclass
class _Chunk:
    ord: int
    id: int
    text: str
    chunk_kind: str


class _Chunks:
    def __init__(self) -> None:
        self.rows: dict[int, list[_Chunk]] = {}

    def list_chunks_for_ref(self, ref_id: int) -> list[_Chunk]:
        return list(self.rows.get(ref_id, []))

    def insert_chunks(self, ref_id: int, blocks: list[Any], conn: Any = None) -> None:
        for b in blocks:
            self.rows.setdefault(ref_id, []).append(
                _Chunk(b.ord, len(self.rows[ref_id]) + 1, b.text, b.meta["chunk_kind"])
            )


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = list(rows)

    def fetchone(self) -> Any:
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> list[Any]:
        return self._rows


class _Conn:
    """Answers canned rows keyed on a substring of the query."""

    def __init__(self, answers: list[tuple[str, list[Any]]] | None = None) -> None:
        self.answers = answers or []
        self.queries: list[tuple[str, Any]] = []
        self.writes: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> _Rows:
        self.queries.append((sql, params))
        if sql.lstrip().upper().startswith("UPDATE"):
            self.writes.append((sql, params))
        for key, rows in self.answers:
            if key in sql:
                return _Rows(rows)
        return _Rows([])


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    @contextmanager
    def connection(self) -> Any:
        yield self._conn


class _Store:
    def __init__(self, conn: _Conn | None = None) -> None:
        self.refs: dict[int, _Ref] = {}
        self.tags: dict[int, list[Any]] = {}
        self.links: list[tuple[int, int, str]] = []
        self.chunks = _Chunks()
        self._conn = conn or _Conn()
        self.pool = _Pool(self._conn)
        self._next = 900

    def add(self, ref: _Ref) -> _Ref:
        self.refs[ref.id] = ref
        return ref

    def get_ref(self, *, kind: str, id: int) -> _Ref | None:
        ref = self.refs.get(int(id))
        return ref if ref is not None and ref.kind == kind else None

    def recent_refs(
        self,
        kinds: list[str],
        *,
        parent_id: int | None = None,
        oldest: bool = False,
        limit: int = 30,
        **_: Any,
    ) -> list[_Ref]:
        rows = [
            r
            for r in self.refs.values()
            if r.kind in kinds and r.parent_id == parent_id
        ]
        rows.sort(key=lambda r: r.id, reverse=not oldest)
        return rows[:limit]

    def add_tag(self, ref_id: int, tag: Any, **_: Any) -> None:
        self.tags.setdefault(ref_id, []).append(tag)

    def remove_tag(self, ref_id: int, tag: Any, **_: Any) -> int:
        return 0

    def add_link(
        self, *, src_ref_id: int, dst_ref_id: int, relation: str, **_: Any
    ) -> None:
        self.links.append((src_ref_id, dst_ref_id, relation))

    def insert_ref(
        self,
        *,
        kind: str,
        slug: Any,
        title: str,
        meta: dict[str, Any],
        parent_id: int | None,
        conn: Any = None,
        prio: Any = None,
    ) -> _Ref:
        self._next += 1
        return self.add(_Ref(self._next, kind, dict(meta or {}), parent_id))

    @contextmanager
    def tx(self) -> Any:
        yield self._conn


def _lane(
    name: str,
    *,
    models: tuple[str, ...] = (),
    default_model: str | None = None,
    cost_class: str = COST_SUBSCRIPTION,
    vendor: str = "v",
    cap: int = 2,
) -> Lane:
    return Lane(
        name=name,
        adapter="claude_p",
        credential_pool="subscription",
        cap=cap,
        task_classes=frozenset({ANY_CLASS}),
        enabled=True,
        vendor=vendor,
        models=models,
        default_model=default_model,
        cost_class=cost_class,
    )


# ── hard gates + classify ─────────────────────────────────────────


def test_hard_gates_records_the_four_named_checks() -> None:
    gates = outcome.hard_gates(
        compiled=True,
        is_error=False,
        vanished=False,
        num_turns=12,
        max_turns=40,
        tests_observed=True,
        tests_expected=True,
    )
    assert gates == {
        "compiled": True,
        "tests_ran": True,
        "no_exceptions": True,
        "within_step_cap": True,
    }


def test_hard_gates_compiled_is_evidence_not_exit_code() -> None:
    # Nothing probeable (no .py touched, tree gone) records None —
    # never a conviction; a probe that failed records False.
    gates = outcome.hard_gates(
        compiled=None,
        is_error=False,
        vanished=False,
        num_turns=12,
        max_turns=40,
        tests_observed=True,
        tests_expected=True,
    )
    assert gates["compiled"] is None
    assert outcome.gate_pass(gates, denied=None, budget_note=None, test_failure=None)
    gates = outcome.hard_gates(
        compiled=False,
        is_error=False,
        vanished=False,
        num_turns=12,
        max_turns=40,
        tests_observed=True,
        tests_expected=True,
    )
    assert gates["compiled"] is False
    assert not outcome.gate_pass(
        gates, denied=None, budget_note=None, test_failure=None
    )


def test_hard_gates_tests_ran_none_without_obligation() -> None:
    gates = outcome.hard_gates(
        compiled=True,
        is_error=False,
        vanished=False,
        num_turns=3,
        max_turns=40,
        tests_observed=False,
        tests_expected=False,
    )
    assert gates["tests_ran"] is None
    # A None gate does not fail gate_pass — a review owes no tests.
    assert outcome.gate_pass(gates, denied=None, budget_note=None, test_failure=None)


def test_hard_gates_cap_and_error_signals() -> None:
    gates = outcome.hard_gates(
        compiled=False,
        is_error=True,
        vanished=False,
        num_turns=40,
        max_turns=40,
        tests_observed=False,
        tests_expected=True,
    )
    assert gates == {
        "compiled": False,
        "tests_ran": False,
        "no_exceptions": False,
        "within_step_cap": False,
    }
    assert not outcome.gate_pass(
        gates, denied="touched deny paths", budget_note=None, test_failure=None
    )
    # Unknown turn count → within_step_cap None, never a False.
    gates = outcome.hard_gates(
        compiled=True,
        is_error=False,
        vanished=False,
        num_turns=None,
        max_turns=40,
        tests_observed=True,
        tests_expected=True,
    )
    assert gates["within_step_cap"] is None


def test_gate_pass_folds_mechanical_faults() -> None:
    gates: dict[str, bool | None] = {
        "compiled": True,
        "tests_ran": True,
        "no_exceptions": True,
        "within_step_cap": True,
    }
    assert outcome.gate_pass(gates, denied=None, budget_note=None, test_failure=None)
    for fault in (
        {"denied": "touched deny paths"},
        {"budget_note": "crossed its remaining budget"},
        {"test_failure": "tests failed"},
    ):
        assert not outcome.gate_pass(
            gates,
            denied=fault.get("denied"),
            budget_note=fault.get("budget_note"),
            test_failure=fault.get("test_failure"),
        )


def test_classify_vocabulary() -> None:
    base: dict[str, Any] = dict(
        exit_code=0, vanished=False, num_turns=5, max_turns=40, clean=True, nothing=None
    )
    assert outcome.classify(**base) == "completed"
    assert (
        outcome.classify(**{**base, "nothing": "left no commit"}) == "completed_empty"
    )
    assert outcome.classify(**{**base, "vanished": True}) == "abandoned"
    assert outcome.classify(**{**base, "num_turns": 40}) == "halted_step_cap"
    assert outcome.classify(**{**base, "exit_code": None, "clean": False}) == (
        "halted_wall_clock"
    )
    assert outcome.classify(**{**base, "clean": False}) == "error"
    # Order: a capped session still reporting a clean exit is the halt.
    assert outcome.classify(**{**base, "num_turns": 40, "clean": False}) == (
        "halted_step_cap"
    )


# ── prior picks / disagreement ────────────────────────────────────


def _plan(
    store: _Store,
) -> tuple[_Ref, _Ref, _Ref]:
    root = store.add(_Ref(1, "todo", {"auto_close": True}))
    build = store.add(_Ref(2, "todo", {"task_class": "code"}, parent_id=1))
    validate = store.add(_Ref(3, "todo", {"task_class": "validate"}, parent_id=1))
    return root, build, validate


def test_prior_picks_reads_sibling_sessions() -> None:
    store = _Store()
    _, build, validate = _plan(store)
    store.add(
        _Ref(
            10,
            "job",
            {
                "job_type": "run_slice",
                "lane": "claude-sub",
                "model": "claude-sonnet-5-5",
            },
            parent_id=build.id,
        )
    )
    store.add(
        _Ref(
            11,
            "job",
            {"job_type": "land_slice"},
            parent_id=build.id,
        )
    )
    picks = outcome.prior_picks(store, validate.id)
    assert [(p.job_id, p.task_class, p.model) for p in picks] == [
        (10, "code", "claude-sonnet-5-5")
    ]
    assert outcome.code_session_models(store, validate.id) == (
        frozenset({"claude-sonnet-5-5"}),
        None,
    )


def test_prior_picks_empty_outside_a_plan() -> None:
    store = _Store()
    lone = store.add(_Ref(9, "todo", {}))
    assert outcome.prior_picks(store, lone.id) == []
    assert outcome.code_session_models(store, lone.id) == (frozenset(), None)


def test_code_session_models_flags_unestablishable_identity() -> None:
    """review td655142: an empty avoid set must never ride past the
    different-model MUST — a failed read says ``"read"`` (park), an
    unrecorded code-session model says ``"unrecorded"`` (refuse)."""
    store = _Store()
    _, build, validate = _plan(store)
    # A launched code pick whose model was never recorded — the
    # account default: the run can never prove a different model.
    store.add(
        _Ref(
            10,
            "job",
            {"job_type": "run_slice", "lane": "devin"},
            parent_id=build.id,
        )
    )
    assert outcome.code_session_models(store, validate.id) == (
        frozenset(),
        "unrecorded",
    )
    # …but a code job that died before its submit stamped a lane ran
    # no session — it has no model to avoid.
    store.refs[10].meta = {"job_type": "run_slice"}
    assert outcome.code_session_models(store, validate.id) == (frozenset(), None)

    class _DeadStore(_Store):
        def recent_refs(self, kinds: Any, **kw: Any) -> Any:
            raise RuntimeError("store down")

    dead = _DeadStore()
    _, _, dead_validate = _plan(dead)
    assert outcome.code_session_models(dead, dead_validate.id) == (
        frozenset(),
        "read",
    )


def test_session_disagreement_only_code_vs_test() -> None:
    picks = [
        outcome.PriorPick(2, 10, "code", "devin", "swe-2-high", "completed", True),
        outcome.PriorPick(4, 11, "review", "claude-sub", "x", "error", False),
    ]
    # A code run that passed its gates: the reviewer's gate record is
    # not the test session's verdict — no disagreement.
    assert not outcome.session_disagreement(picks, task_class="code", gate_pass=True)
    picks.append(
        outcome.PriorPick(3, 12, "validate", "devin", "swe-2-max", "error", False)
    )
    # …but a validate sibling that failed where the code run passed is.
    assert outcome.session_disagreement(picks, task_class="code", gate_pass=True)
    assert not outcome.session_disagreement(picks, task_class="code", gate_pass=False)
    # Non-code/test classes never disagree.
    assert not outcome.session_disagreement(picks, task_class="review", gate_pass=True)


def test_session_disagreement_a_correction_is_a_disagreement() -> None:
    """review td655142: a validate run that patched the fix disagreed
    with it however both sessions' own gates read."""
    picks = [
        outcome.PriorPick(2, 10, "code", "devin", "swe-2-high", "completed", True),
    ]
    # Both sessions pass their gates — but the test session left
    # non-test changes over the code sibling's produce.
    assert outcome.session_disagreement(
        picks, task_class="validate", gate_pass=True, corrected=True
    )
    # No correction → no disagreement; and a correction with no code
    # sibling to correct is none either.
    assert not outcome.session_disagreement(
        picks, task_class="validate", gate_pass=True, corrected=False
    )
    assert not outcome.session_disagreement(
        [], task_class="validate", gate_pass=True, corrected=True
    )
    # Only the test session's correction counts — a code run amending
    # its own branch is not a disagreement with itself.
    assert not outcome.session_disagreement(
        picks, task_class="code", gate_pass=True, corrected=True
    )


# ── sampling policy ───────────────────────────────────────────────


def test_select_review_new_category_then_ten_percent() -> None:
    assert outcome.select_review(
        job_id=777, n_in_category=3, disagreement=False, suspicious=False
    ) == ["new-category"]
    # Past the warmup only the deterministic ~10% sample fires — a
    # stable hash of the job id, not id % 100 (review td655142:
    # sequential low digits are not a random sample of runs).
    assert outcome.select_review(
        job_id=700, n_in_category=80, disagreement=False, suspicious=False
    ) == ["sampled-10pct"]  # sha256('run-quality:700') % 100 < 10
    for miss in (705, 750, 777):
        assert (
            outcome.select_review(
                job_id=miss,
                n_in_category=80,
                disagreement=False,
                suspicious=False,
            )
            == []
        )


def test_select_review_disagreement_and_suspicious_any_age() -> None:
    assert outcome.select_review(
        job_id=750, n_in_category=500, disagreement=True, suspicious=False
    ) == ["test-disagreement"]
    assert outcome.select_review(
        job_id=750, n_in_category=500, disagreement=False, suspicious=True
    ) == ["suspicious-cheap"]


def test_category_stats_counts_and_positive_deciles() -> None:
    conn = _Conn(
        [
            ("SELECT count(*)", [(42,)]),
            ("cost_usd", [(1.5,)]),
            ("wall_s", [(90.0,)]),
        ]
    )
    stats = outcome.category_stats(conn, "gripe.fix", exclude_job_id=10)
    assert stats == {"n": 42, "cost_p10": 1.5, "wall_p10": 90.0}
    # The decile queries only aggregate positive figures — a free lane's
    # $0 record cannot floor the category's real cost.
    decile_qs = [q for q, _ in conn.queries if "percentile_cont" in q]
    assert decile_qs and all("> 0" in q for q in decile_qs)
    assert all("run_slice" in q and "task_kind" in q for q, _ in conn.queries)


# ── model diversity ───────────────────────────────────────────────


def test_diverse_pick_prefers_same_lane_alternate() -> None:
    lane = _lane(
        "claude-sub",
        models=("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"),
        default_model="claude-sonnet-5-5",
    )
    other = _lane("devin", models=("swe-2-high",), cost_class=COST_FREE)
    new_lane, model, note = outcome.diverse_model_pick(
        None,
        lane=lane,
        model="claude-sonnet-5-5",
        candidates=(lane, other),
        counts={},
        backoffs=None,
        paid_ok=True,
        avoid=frozenset({"claude-sonnet-5-5"}),
        model_pin=None,
        task_class="validate",
    )
    assert new_lane is lane
    assert model == "claude-haiku-5-5"
    assert note and "claude-haiku-5-5" in note


def test_diverse_pick_crosses_lanes_when_same_lane_has_no_alternate() -> None:
    lane = _lane("only", models=("m1",), default_model="m1")
    other = _lane("free", models=("m2",), default_model="m2", cost_class=COST_FREE)
    new_lane, model, note = outcome.diverse_model_pick(
        None,
        lane=lane,
        model="m1",
        candidates=(lane, other),
        counts={},
        backoffs=None,
        paid_ok=True,
        avoid=frozenset({"m1"}),
        model_pin=None,
        task_class="validate",
    )
    assert new_lane is other
    assert model == "m2"
    assert note and "re-picked lane" in note


def test_diverse_pick_refuses_when_no_lane_differs() -> None:
    """review td655142: one model on the fleet cannot satisfy
    "different" — the MUST refuses the launch, it does not ride the
    old pick anyway."""
    lane = _lane("only", models=("m1",), default_model="m1")
    new_lane, model, note = outcome.diverse_model_pick(
        None,
        lane=lane,
        model="m1",
        candidates=(lane,),
        counts={},
        backoffs=None,
        paid_ok=True,
        avoid=frozenset({"m1"}),
        model_pin="m1",
        task_class="validate",
    )
    assert (new_lane, model) == (None, None)
    assert note and "refusing the launch" in note


def test_diverse_pick_refuses_an_unknown_account_default() -> None:
    """A resolved ``None`` — the lane's unnamed account default — is
    UNKNOWN, not "different": it could be the code model under another
    name, so the pick either pins a declared outside model or fails."""
    lane = _lane("default-only")  # models=() — the account default
    new_lane, model, note = outcome.diverse_model_pick(
        None,
        lane=lane,
        model=None,
        candidates=(lane,),
        counts={},
        backoffs=None,
        paid_ok=True,
        avoid=frozenset({"m1"}),
        model_pin=None,
        task_class="validate",
    )
    assert (new_lane, model) == (None, None)
    assert note and "account default" in note

    # …but the same lane with a declared outside model pins it rather
    # than trusting the unknown default.
    lane = _lane("mixed", models=("m1", "m2"))
    new_lane, model, note = outcome.diverse_model_pick(
        None,
        lane=lane,
        model=None,
        candidates=(lane,),
        counts={},
        backoffs=None,
        paid_ok=True,
        avoid=frozenset({"m1"}),
        model_pin=None,
        task_class="validate",
    )
    assert (new_lane, model) == (lane, "m2")
    assert note and "account default" in note


def test_diverse_pick_noops_when_model_already_differs() -> None:
    lane = _lane("a", models=("m1", "m2"), default_model="m2")
    new_lane, model, note = outcome.diverse_model_pick(
        None,
        lane=lane,
        model="m2",
        candidates=(lane,),
        counts={},
        backoffs=None,
        paid_ok=True,
        avoid=frozenset({"m1"}),
        model_pin=None,
        task_class="validate",
    )
    assert (new_lane, model, note) == (lane, "m2", None)


def test_diverse_pick_respects_caps_backoffs_and_free_first() -> None:
    picked = _lane("picked", models=("m1",), default_model="m1")
    full = _lane("full", models=("m2",), default_model="m2", cap=1)
    backed = _lane("backed", models=("m3",), default_model="m3")
    paid = _lane("paid", models=("m4",), default_model="m4")
    free = _lane("free", models=("m5",), default_model="m5", cost_class=COST_FREE)
    new_lane, model, _ = outcome.diverse_model_pick(
        None,
        lane=picked,
        model="m1",
        candidates=(picked, full, backed, paid, free),
        counts={"full": 1},
        backoffs={"backed": datetime.now(UTC) + timedelta(hours=1)},
        paid_ok=False,
        avoid=frozenset({"m1"}),
        model_pin=None,
        task_class="validate",
    )
    # The full lane, the backed-off lane and the paid lanes (a fit free
    # lane exists) are all out of the diverse re-pick.
    assert (new_lane.name, model) == ("free", "m5")


# ── build_outcome end-to-end ──────────────────────────────────────


def test_build_outcome_records_outcome_gates_and_review() -> None:
    conn = _Conn([("SELECT count(*)", [(3,)])])
    store = _Store(conn)
    _, build, validate = _plan(store)
    store.add(
        _Ref(
            10,
            "job",
            {
                "job_type": "run_slice",
                "lane": "claude-sub",
                "model": "claude-sonnet-5-5",
                "outcome": {"outcome": "completed", "gate_pass": True},
            },
            parent_id=build.id,
        )
    )
    rec = outcome.build_outcome(
        store,
        job_id=99,
        job_meta={"task_kind": "gripe.fix", "usage": {"num_turns": 12}},
        todo_id=validate.id,
        handle={"started_at": "2026-11-20T00:00:00Z"},
        exit_code=0,
        is_error=False,
        denied=None,
        vanished=False,
        budget_note=None,
        test_failure=None,
        tests_observed=True,
        nothing=None,
        clean=True,
        max_turns=40,
        cost_usd=0.5,
    )
    assert rec["outcome"] == "completed"
    assert rec["gate_pass"] is True
    assert rec["gates"]["tests_ran"] is True  # validate is a test class
    assert rec["task_kind"] == "gripe.fix"
    assert rec["cost_usd"] == 0.5
    assert rec["num_turns"] == 12
    # A new task category reviews every run.
    assert rec["review"] == {
        "sampled": True,
        "reasons": ["new-category"],
        "minted": False,
    }
    # The sibling code run's gate verdict agrees — no disagreement reason.
    assert "test-disagreement" not in rec["review"]["reasons"]


def test_build_outcome_error_run_flags_disagreement() -> None:
    conn = _Conn([("SELECT count(*)", [(3,)])])
    store = _Store(conn)
    _, build, validate = _plan(store)
    store.add(
        _Ref(
            10,
            "job",
            {
                "job_type": "run_slice",
                "lane": "devin",
                "model": "swe-2-high",
                "outcome": {"outcome": "completed", "gate_pass": True},
            },
            parent_id=build.id,
        )
    )
    rec = outcome.build_outcome(
        store,
        job_id=99,
        job_meta={"task_kind": "gripe.fix"},
        todo_id=validate.id,
        handle={},
        exit_code=1,
        is_error=True,
        denied=None,
        vanished=False,
        budget_note=None,
        test_failure=None,
        tests_observed=False,
        nothing=None,
        clean=False,
        max_turns=40,
        cost_usd=None,
    )
    assert rec["outcome"] == "error"
    assert rec["gate_pass"] is False
    # The test session failed where the code session passed the gates —
    # exactly the disagreement the sampler always reviews.
    assert "test-disagreement" in rec["review"]["reasons"]


def test_build_outcome_a_test_correction_flags_disagreement() -> None:
    """review td655142: a validate run that left non-test changes over
    the code sibling's produce samples for review however its own
    gates read."""
    conn = _Conn([("SELECT count(*)", [(60,)])])
    store = _Store(conn)
    _, build, validate = _plan(store)
    store.add(
        _Ref(
            10,
            "job",
            {
                "job_type": "run_slice",
                "lane": "devin",
                "model": "swe-2-high",
                "outcome": {"outcome": "completed", "gate_pass": True},
            },
            parent_id=build.id,
        )
    )
    rec = outcome.build_outcome(
        store,
        job_id=99,
        job_meta={"task_kind": "gripe.fix"},
        todo_id=validate.id,
        handle={},
        exit_code=0,
        is_error=False,
        denied=None,
        vanished=False,
        budget_note=None,
        test_failure=None,
        tests_observed=True,
        nothing=None,
        clean=True,
        max_turns=40,
        cost_usd=None,
        compiled=True,
        corrected=True,
    )
    assert rec["gate_pass"] is True and rec["corrected"] is True
    # Past warmup and no other reason — the correction alone samples.
    assert "test-disagreement" in rec["review"]["reasons"]


def test_build_outcome_suspicious_cheap_flags() -> None:
    conn = _Conn(
        [
            ("SELECT count(*)", [(30,)]),
            ("cost_usd", [(2.0,)]),
            ("wall_s", [(120.0,)]),
        ]
    )
    store = _Store(conn)
    store.add(_Ref(1, "todo", {"auto_close": True}))
    task = store.add(_Ref(2, "todo", {"task_class": "code"}, parent_id=1))
    rec = outcome.build_outcome(
        store,
        job_id=99,
        job_meta={"task_kind": "gripe.fix"},
        todo_id=task.id,
        handle={"started_at": "2026-11-20T00:00:00Z"},
        exit_code=0,
        is_error=False,
        denied=None,
        vanished=False,
        budget_note=None,
        test_failure=None,
        tests_observed=True,
        nothing=None,
        clean=True,
        max_turns=40,
        cost_usd=0.0,  # reported $0 against a $2.00 decile floor
    )
    assert "suspicious-cheap" in rec["review"]["reasons"]


# ── verdict + rollups ─────────────────────────────────────────────


def test_record_verdict_writes_meta_verdict() -> None:
    conn = _Conn()
    store = _Store(conn)
    outcome.record_verdict(store, 55, "fail", by="run-quality td77", note="bad")
    assert conn.writes
    sql, params = conn.writes[-1]
    assert "jsonb_set" in sql and "{verdict}" in sql
    payload = params[0]
    assert '"verdict": "fail"' in payload and '"by": "run-quality td77"' in payload
    assert params[1] == 55


def test_record_verdict_refuses_unknown_verdict() -> None:
    import pytest

    with pytest.raises(ValueError, match="verdict"):
        outcome.record_verdict(_Store(), 1, "meh", by="x")


def test_rollups_group_by_task_kind() -> None:
    conn = _Conn(
        [
            ("AS gate_passed", [("gripe.fix", 10, 8, 7, 1, 1, 1)]),
            ("AS lied", [("gripe.fix", 4, 1)]),
        ]
    )
    table = outcome.pass_rate_table(conn)
    assert table == [
        {
            "task_kind": "gripe.fix",
            "n": 10,
            "gate_passed": 8,
            "completed": 7,
            "completed_empty": 1,
            "error": 1,
            "halted": 1,
        }
    ]
    lied = outcome.gate_lied_rate(conn)
    assert lied == [{"task_kind": "gripe.fix", "judged": 4, "lied": 1, "rate": 0.25}]


# ── the run_quality pass ──────────────────────────────────────────


def test_verdict_comments_reads_newest_verdict() -> None:
    store = _Store()
    task = store.add(_Ref(20, "todo", {"judges": {"job": 55, "todo": 2}}))
    store.chunks.rows[task.id] = [
        _Chunk(0, 1, "the brief says `verdict: pass — <why>`", "todo_body"),
        _Chunk(1, 2, "handoff: verdict: fail — the diff undid the fix", "todo_comment"),
    ]
    # The brief's grammar echo is a body chunk — never a verdict.
    assert run_quality._verdict_comments(cast(Store, store), task.id) == (
        "fail",
        "the diff undid the fix",
    )
    assert run_quality._verdict_comments(cast(Store, store), 404) is None


def test_collect_verdicts_lands_on_the_judged_job() -> None:
    conn = _Conn([("judges", [(20, 55)])])
    store = _Store(conn)
    store.add(_Ref(55, "job", {"job_type": "run_slice"}))
    store.add(_Ref(20, "todo", {"judges": {"job": 55, "todo": 2}}))
    store.chunks.rows[20] = [
        _Chunk(1, 2, "handoff: verdict: pass — honest work", "todo_comment"),
    ]
    assert run_quality.collect_verdicts(cast(Store, store)) == 1
    sql, params = conn.writes[-1]
    assert "{verdict}" in sql
    assert '"verdict": "pass"' in params[0] and params[1] == 55


def _judged_job(todo_id: int) -> dict[str, Any]:
    return {
        "id": 55,
        "meta": {
            "job_type": "run_slice",
            "params": {"todo_id": todo_id},
            "lane": "devin",
            "model": "swe-2-high",
            "outcome": {
                "outcome": "completed",
                "gate_pass": True,
                "review": {"sampled": True, "reasons": ["suspicious-cheap"]},
            },
        },
    }


def test_mint_review_plans_and_marks_the_job(
    monkeypatch: Any,
) -> None:
    conn = _Conn()
    store = _Store(conn)
    judged_todo = store.add(_Ref(2, "todo", {"task_class": "code"}))
    job = _judged_job(judged_todo.id)
    monkeypatch.setattr(
        "precis.workers.factory_rescue._resolve_lane_pin",
        lambda store, lane, cls: (
            {"lane": lane, "lane_capability": "mcp"},
            "",
        ),
    )
    monkeypatch.setattr(
        "precis.plans.lanes.load_lanes",
        lambda store: (
            _lane(
                "claude-sub",
                models=("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"),
                default_model="claude-sonnet-5-5",
            ),
        ),
    )
    monkeypatch.setattr(run_quality, "_judge_model", lambda: "claude-opus-5-5")
    root = run_quality.mint_review(cast(Store, store), job)
    judge_root = store.refs[root]
    task = next(
        r for r in store.refs.values() if r.kind == "todo" and r.parent_id == root
    )
    assert judge_root.meta["auto_close"] is True
    assert task.meta["task_class"] == "review"
    assert task.meta["judges"] == {"job": 55, "todo": judged_todo.id}
    assert (root, judged_todo.id, "related-to") in store.links
    # The mint marks the job reviewed, atomically with the plan.
    assert any("{outcome,review,minted}" in sql for sql, _ in conn.writes)
    # review td655142: the judge is a BIG model, pinned — never the
    # lane's mid-tier default.
    params = task.meta["op"]["params"]
    assert params["lane"] == "claude-sub"
    assert params["model"] == "claude-opus-5-5"


def test_judge_params_rejects_a_lane_that_cannot_run_the_model(
    monkeypatch: Any,
) -> None:
    """review td655142: the model pin is validated against
    ``accepts_model`` — a pinned lane that cannot run it falls back to
    an MCP lane that can; a fleet with none mints bound so submit's
    gate refuses rather than silently judging on a lane default."""
    store = _Store()
    small = _lane(
        "claude-small", models=("claude-haiku-5-5",), default_model="claude-haiku-5-5"
    )
    big = _lane(
        "claude-big", models=("claude-opus-5-5",), default_model="claude-opus-5-5"
    )
    monkeypatch.setattr(
        "precis.workers.factory_rescue._resolve_lane_pin",
        lambda store, lane, cls: (
            {"lane": "claude-small", "lane_capability": "mcp"},
            "",
        ),
    )
    monkeypatch.setattr("precis.plans.lanes.load_lanes", lambda store: (small, big))
    monkeypatch.setattr(run_quality, "_judge_model", lambda: "claude-opus-5-5")
    params, note = run_quality._judge_params(cast(Store, store))
    assert params == {
        "lane": "claude-big",
        "lane_capability": "mcp",
        "model": "claude-opus-5-5",
    }
    assert "claude-small" in note and "claude-big" in note

    # No lane anywhere accepts the judge model: bound on capability +
    # model alone — submit refuses, never a silent small-model judge.
    monkeypatch.setattr("precis.plans.lanes.load_lanes", lambda store: (small,))
    params, note = run_quality._judge_params(cast(Store, store))
    assert params == {"lane_capability": "mcp", "model": "claude-opus-5-5"}
    assert "claude-opus-5-5" in note


def test_candidates_skips_minted_and_judge_runs() -> None:
    conn = _Conn()
    run_quality.candidates(conn, 1)
    sql, params = conn.queries[-1]
    assert "sampled" in sql and "minted" in sql and "judges" in sql
    assert params == (1,)


def test_run_quality_pass_dark_when_unlit() -> None:
    store = _Store()
    assert run_quality.run_run_quality_pass(cast(Store, store)).claimed == 0
