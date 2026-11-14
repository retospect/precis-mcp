"""Run outcome — the hard-gate / sampled-quality split (gr595545).

``eval-run-spine.md``'s two columns, landed on the lane router's run
record — the ``run_slice`` job row, the task-aware-routing spec's
"run" (me568172):

* ``meta.outcome`` — the mechanical terminal state, cheap to compute
  and written on EVERY run by ``run_slice.poll``: the spine's outcome
  vocabulary (:data:`OUTCOMES`), the four hard gates
  (:func:`hard_gates`) — compiled, tests ran, no exceptions, finished
  within the step cap — cost/wall figures, and the sampled-review
  decision (:func:`select_review`). The gates drive the pass-rate
  table (:func:`pass_rate_table`) and are what escalation and the
  suitability ledger read; ``gate_pass`` only says "the run did not
  die mechanically", nothing about whether the work was any good.
* ``meta.verdict`` — the second column: ``None`` until the sampled
  big-model review (:mod:`precis.workers.run_quality`) or a hand
  writes it through :func:`record_verdict`. Keeping it off the
  outcome is the spine's whole point — a judge's error never
  rewrites a terminal state, and "how many runs died on the step cap
  last night" stays a cheap query.

The sampled-quality policy is :func:`select_review`: every run of a
new task category (``meta.task_kind``, the recipe row the mint stamps)
until :data:`CATEGORY_WARMUP`, then a deterministic ~10% plus every
run whose test session disagreed with its code session
(:func:`session_disagreement`) and anything that passed the gates
suspiciously fast or cheaply — the bottom decile of cost or wall time
for the category (:func:`category_stats`). "How often the gates lied"
— a gate pass the review failed — is :func:`gate_lied_rate`, keyed by
the same category.

The router constraint — a run's test session on a different model
than its code sessions — is :func:`code_session_models` plus
:func:`diverse_model_pick`. ``lanes.py`` is deny-listed, so
``run_slice.submit`` feeds the run's prior picks to the pick itself:
for :data:`TEST_TASK_CLASS` nodes it re-resolves the model over the
candidate table ``pick_lane`` already filtered, preferring another
model on the same lane, then a different lane — and REFUSING the
launch when nothing satisfies it, because a test session on its code
session's model is no test (the MUST is not a preference). The MUST
also binds when a prior code session's model cannot be established
— a failed pick read parks the launch; an unrecorded one resolves to
the vendor's account default where this host's config names it
(:func:`account_default_model`), and only a default that cannot be
named still refuses (:func:`code_session_models`, gr657521).
"""

from __future__ import annotations

import hashlib
import json
import logging
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from precis.plans.templates import VALIDATE_TASK_CLASS

log = logging.getLogger(__name__)

#: ``meta.task_class`` of a builder session — ``run_slice``'s default.
CODE_TASK_CLASS = "code"
#: The "test session": the chain node that re-runs the repro and rules
#: ``validated:`` on the fix — the class the model-diversity rule binds.
TEST_TASK_CLASS = VALIDATE_TASK_CLASS
#: Classes whose hard gates include "tests ran" — the builder's own
#: produce is tested, and the test session's deliverable IS the test.
TEST_EXPECTED_CLASSES = frozenset({CODE_TASK_CLASS, TEST_TASK_CLASS})

#: The mechanical terminal states (eval-run-spine.md, decided
#: 2026-09-29): knowable without judgment.
OUTCOMES: tuple[str, ...] = (
    "completed",
    "completed_empty",
    "halted_step_cap",
    "halted_wall_clock",
    "error",
    "abandoned",
)
#: ``meta.verdict.verdict`` values — the judge's (or a hand's) ruling
#: on the work, never the run's mechanics.
VERDICTS: tuple[str, ...] = ("pass", "fail")

#: Sampling policy knobs (the gripe's "~50", "10 %", "bottom decile").
CATEGORY_WARMUP = 50
SAMPLE_PERCENT = 10
#: Deciles need history; below this the suspicious-cheap reason cannot
#: fire (a "bottom decile" of three runs is noise).
SUSPICIOUS_MIN_RUNS = 10

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _utcnow() -> str:
    return datetime.now(UTC).strftime(_TS_FMT)


def hard_gates(
    *,
    compiled: bool | None,
    is_error: bool,
    vanished: bool,
    num_turns: int | None,
    max_turns: int | None,
    tests_observed: bool,
    tests_expected: bool,
) -> dict[str, bool | None]:
    """The four cheap checks the spec names, as observed facts.

    * ``compiled`` — actual compilation evidence, not the exit code:
      the caller compile-probes the slice's changed ``.py`` files on
      the worker (``run_slice._compile_gate``). ``None`` when nothing
      observable — no ``.py`` touched, the tree vanished — never a
      failure; ``False`` is the lie this gate exists for: a session
      that reported done while its tree does not compile.
    * ``tests_ran`` — acceptance tests ran on the worker or the
      session's own ``tests: ran`` marker. ``None`` when the run owed
      no tests: a class outside :data:`TEST_EXPECTED_CLASSES` (a
      review, a brief — recording False there would read as a gate
      failure it never owed), or a code run that left no commits —
      nothing to test. A code run that produced owes the receipt
      whether or not its ask named test paths (review td655142:
      the every-run gate; a later lander check is no substitute for
      THIS run's evidence).
    * ``no_exceptions`` — no adapter-reported error and the worktree
      did not vanish under the run.
    * ``within_step_cap`` — the session ended before ``max_turns``;
      ``None`` when the vendor reports no turn count.
    """
    no_exceptions = not is_error and not vanished
    within_cap: bool | None = None
    if num_turns is not None and max_turns:
        within_cap = num_turns < max_turns
    return {
        "compiled": compiled,
        "tests_ran": (bool(tests_observed) if tests_expected else None),
        "no_exceptions": no_exceptions,
        "within_step_cap": within_cap,
    }


def gate_pass(
    gates: dict[str, bool | None],
    *,
    denied: str | None,
    budget_note: str | None,
    test_failure: str | None,
) -> bool:
    """``True`` when every applicable gate passed and no mechanical
    fault fired — the run terminated clean; the gates say nothing about
    whether its work was any good (that is ``meta.verdict``'s job).

    A ``None`` gate is "not applicable / not observable" — a vendor
    with no turn counter is not convicted of a cap halt. Deny-path
    hits, budget kills and acceptance-test failures are mechanical
    faults of the run itself, folded in beside the four named gates."""
    return (
        all(v is not False for v in gates.values())
        and denied is None
        and budget_note is None
        and test_failure is None
    )


def classify(
    *,
    exit_code: int | None,
    vanished: bool,
    num_turns: int | None,
    max_turns: int | None,
    clean: bool,
    nothing: str | None,
) -> str:
    """The mechanical outcome (one of :data:`OUTCOMES`) for a terminal
    poll — ordered so the resumable halts name themselves before the
    generic ``error`` catch-all:
    ``vanished``/no-exit-status reads ``abandoned`` (the reaped tree) or
    ``halted_wall_clock`` (the kill deadline's dead process); a turn
    counter at the cap is ``halted_step_cap``; a clean run that left no
    deliverable is ``completed_empty``."""
    if vanished:
        return "abandoned"
    if num_turns is not None and max_turns and num_turns >= max_turns:
        return "halted_step_cap"
    if exit_code is None:
        return "halted_wall_clock"
    if not clean:
        return "error"
    if nothing is not None:
        return "completed_empty"
    return "completed"


class PriorPick(NamedTuple):
    """One earlier session of the same run: its todo, its run job, the
    lane/model the router chose, and the outcome it recorded if it
    already terminated. ``adapter`` is the lane's adapter stamp —
    :func:`account_default_model` resolves an unrecorded ``model``
    through it."""

    todo_id: int
    job_id: int
    task_class: str
    lane: str | None
    model: str | None
    outcome: str | None
    gate_pass: bool | None
    adapter: str | None = None


def prior_picks(
    store: Any, todo_id: int, *, limit: int = 200, strict: bool = False
) -> list[PriorPick]:
    """The run's earlier session picks — the sibling ``run_slice`` jobs
    under the same plan root (plan nodes all parent to the root), each
    job's ``meta.lane``/``meta.model`` the router stamped, oldest first.
    ``[]`` outside a plan (a hand-minted slice has no siblings).

    ``strict`` lets a store fault PROPAGATE instead of reading as an
    empty or partial pick list: the submit-side diversity rule must
    tell a failed read apart from "no prior picks" (gr595545 review
    td655142 — a dropped sibling is a model the test session may
    repeat). The default stays best-effort for ``build_outcome``'s
    disagreement read, where a missing sibling just means no signal."""
    try:
        todo = store.get_ref(kind="todo", id=int(todo_id))
        parent = getattr(todo, "parent_id", None)
        siblings = (
            store.recent_refs(["todo"], parent_id=int(parent), limit=limit, oldest=True)
            if parent is not None
            else []
        )
    except Exception:
        if strict:
            raise
        return []
    picks: list[PriorPick] = []
    for sib in siblings:
        if int(sib.id) == int(todo_id):
            continue
        cls = str((sib.meta or {}).get("task_class") or CODE_TASK_CLASS)
        try:
            jobs = store.recent_refs(
                ["job"], parent_id=int(sib.id), limit=25, oldest=True
            )
        except Exception:
            if strict:
                raise
            continue
        for job in jobs:
            meta = job.meta or {}
            if meta.get("job_type") != "run_slice":
                continue
            outcome = meta.get("outcome")
            rec = outcome if isinstance(outcome, dict) else {}
            gp = rec.get("gate_pass")
            picks.append(
                PriorPick(
                    todo_id=int(sib.id),
                    job_id=int(job.id),
                    task_class=cls,
                    lane=str(meta["lane"]) if meta.get("lane") else None,
                    model=str(meta["model"]) if meta.get("model") else None,
                    outcome=(str(rec["outcome"]) if rec.get("outcome") else None),
                    gate_pass=gp if isinstance(gp, bool) else None,
                    adapter=(str(meta["adapter"]) if meta.get("adapter") else None),
                )
            )
    return picks


def _strip_json_comments(text: str) -> str:
    """Drop ``//`` line comments outside string literals — devin's
    ``config.json`` tolerates them, :mod:`json` does not."""
    out: list[str] = []
    in_str = False
    esc = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
        elif ch == "/" and i + 1 < len(text) and text[i + 1] == "/":
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def account_default_model(adapter: str | None) -> str | None:
    """The concrete model id an UNPINNED session on ``adapter``'s
    account runs on this host — the vendor default the CLI's own
    config names (gr657521). ``None`` when the default cannot be
    named: the launch the different-model rule cannot prove.

    The reads mirror what the slice's vendor CLI itself loads —
    ``run_slice.slice_env`` carries ``HOME`` (and drops
    ``CODEX_HOME``), so the launched CLI resolves these same files:

    * ``claude_p`` — the router's ``Tier.FRONTIER`` pin, the same
      fallback ``run_slice.resolve_model`` takes (always concrete
      there; this arm exists for old records);
    * ``kimi_tree`` — ``kimi.MODELS[0]``, the ``KIMI_MODEL_NAME`` the
      launch env synthesizes for an unpinned run;
    * ``codex_exec`` — ``~/.codex/config.toml``'s ``model`` key (the
      slice argv carries no ``--ignore-user-config``);
    * ``devin_tree`` — ``~/.config/devin/config.json``'s
      ``agent.model``.
    """
    model: Any = None
    try:
        if adapter == "claude_p":
            from precis.utils.llm.router import Tier
            from precis.utils.llm.router import resolve_model as _resolve

            return _resolve(Tier.FRONTIER)
        if adapter == "kimi_tree":
            from precis.utils.llm import kimi

            return str(kimi.MODELS[0]) if kimi.MODELS else None
        if adapter == "codex_exec":
            cfg = Path.home() / ".codex" / "config.toml"
            if cfg.is_file():
                model = tomllib.loads(cfg.read_text(encoding="utf-8")).get("model")
        elif adapter == "devin_tree":
            cfg = Path.home() / ".config" / "devin" / "config.json"
            if cfg.is_file():
                data = json.loads(_strip_json_comments(cfg.read_text(encoding="utf-8")))
                agent = data.get("agent") if isinstance(data, dict) else None
                model = agent.get("model") if isinstance(agent, dict) else None
        else:
            return None
    except Exception:
        log.warning("outcome: %s account default unreadable", adapter, exc_info=True)
        return None
    if not isinstance(model, str) or not model.strip():
        return None
    return model.strip()


def code_session_models(store: Any, todo_id: int) -> tuple[frozenset[str], str | None]:
    """``(avoid, unknown)`` — the model ids the run's code sessions
    already ran (the picks the test session must route around,
    gr595545) and WHY that set cannot be trusted, when it cannot:

    * ``"read"`` — the prior-pick read failed: a transient store
      fault. The caller PARKS and retries; an empty avoid riding past
      the different-model MUST is how the old code let a same-model
      test launch (review td655142).
    * ``"unrecorded"`` — a prior code session's ``meta.model`` was
      never recorded AND its account default cannot be resolved either
      (:func:`account_default_model`, gr657521). Permanent — the run
      can never prove its test session ran a different model, so the
      caller REFUSES rather than guesses.

    ``unknown is None`` says every prior code pick's model is
    established (or there are none — a test session launched first
    owes no avoidance). A launched pick with no recorded model
    contributes its resolved account default to ``avoid``: the current
    default stands in for the launch-time default (gr657521 — a test
    pin to a different family then proves the difference).

    Only picks whose submit stamped ``meta.lane`` count as sessions:
    ``lane`` and ``model`` stamp together just before the launch, so a
    sibling job that died earlier — a lane wait, a refused launch —
    ran no session and has no model to avoid, while a launched pick
    with neither a recorded model nor a resolvable default is exactly
    the unprovable case."""
    try:
        picks = prior_picks(store, todo_id, strict=True)
    except Exception:
        log.warning("outcome: prior-pick read for td%d failed", todo_id, exc_info=True)
        return frozenset(), "read"
    sessions = [
        p for p in picks if p.task_class == CODE_TASK_CLASS and p.lane is not None
    ]
    avoid: set[str] = {p.model for p in sessions if p.model}
    unrecorded = False
    for p in sessions:
        if p.model is not None:
            continue
        default = account_default_model(p.adapter)
        if default is None:
            unrecorded = True
        else:
            avoid.add(default)
    return frozenset(avoid), "unrecorded" if unrecorded else None


def session_disagreement(
    picks: list[PriorPick],
    *,
    task_class: str,
    gate_pass: bool | None,
    corrected: bool = False,
) -> bool:
    """The test session disagreed with the code session, two ways:

    * a sibling session on the OTHER side of the code/test pair
      recorded an opposite ``gate_pass`` (only the code↔test pair is
      compared — a reviewer's gate record is not the test session's
      verdict);
    * ``corrected`` — the test session left non-test changes over a
      code sibling's produce: a validate run that had to patch the fix
      disagreed with it however both sessions' own gates read, since
      the work it handed back is not the work it was given.
    """
    other = (
        TEST_TASK_CLASS
        if task_class == CODE_TASK_CLASS
        else (CODE_TASK_CLASS if task_class == TEST_TASK_CLASS else None)
    )
    if other is None:
        return False
    siblings = [p for p in picks if p.task_class == other]
    if gate_pass is not None and any(
        p.gate_pass is not None and p.gate_pass != gate_pass for p in siblings
    ):
        return True
    return corrected and task_class == TEST_TASK_CLASS and bool(siblings)


def category_stats(
    conn: Any, task_kind: str, *, exclude_job_id: int | None = None
) -> dict[str, Any]:
    """``{n, cost_p10, wall_p10}`` over the run category (``meta.task_kind``)
    — the run count behind the new-category rule and the bottom deciles
    the suspicious-cheap rule compares against."""
    base = (
        "  FROM refs\n"
        " WHERE kind = 'job' AND retired_at IS NULL\n"
        "   AND meta->>'job_type' = 'run_slice'\n"
        "   AND meta->>'task_kind' = %s\n"
        "   AND meta ? 'outcome'\n"
        "   AND ref_id <> %s\n"
    )
    n = int(
        conn.execute(
            "SELECT count(*)\n" + base, (task_kind, int(exclude_job_id or 0))
        ).fetchone()[0]
    )

    # Deciles over positive figures only: a free lane's $0/0 s record
    # must not floor the category's real cost, but a run reporting $0 in
    # a category whose real floor is $5 IS the suspicious case — the
    # comparison itself takes any non-None value.
    def _decile(field: str) -> float | None:
        row = conn.execute(
            "SELECT percentile_cont(0.1) WITHIN GROUP (\n"
            f"        ORDER BY (meta->'outcome'->>'{field}')::float)\n"
            + base
            + f"   AND (meta->'outcome'->>'{field}')::float > 0\n",
            (task_kind, int(exclude_job_id or 0)),
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    return {"n": n, "cost_p10": _decile("cost_usd"), "wall_p10": _decile("wall_s")}


def select_review(
    *,
    job_id: int,
    n_in_category: int,
    disagreement: bool,
    suspicious: bool,
) -> list[str]:
    """The sampled-review reasons for one run — empty means unreviewed.
    New categories review every run until :data:`CATEGORY_WARMUP`; after
    that a deterministic ~``SAMPLE_PERCENT`` percent draw
    (:func:`_sample_hit`), plus disagreement and suspicious-cheap runs
    at any age."""
    reasons: list[str] = []
    if n_in_category < CATEGORY_WARMUP:
        reasons.append("new-category")
    if disagreement:
        reasons.append("test-disagreement")
    if suspicious:
        reasons.append("suspicious-cheap")
    if not reasons and _sample_hit(job_id):
        reasons.append("sampled-10pct")
    return reasons


def _sample_hit(job_id: int) -> bool:
    """The deterministic ~10% draw: a stable hash of the job id — NOT
    ``job_id % 100``, whose sequential low digits are not a random
    sample of runs (review td655142). Stable across workers and polls
    so the decision lands once."""
    digest = hashlib.sha256(f"run-quality:{int(job_id)}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100 < SAMPLE_PERCENT


def _wall_s(started_at: Any) -> float | None:
    """Whole-run wall seconds from the handle's ``started_at`` stamp."""
    try:
        started = datetime.strptime(str(started_at or ""), _TS_FMT).replace(tzinfo=UTC)
    except ValueError:
        return None
    return max(0.0, (datetime.now(UTC) - started).total_seconds())


def build_outcome(
    store: Any,
    *,
    job_id: int,
    job_meta: dict[str, Any],
    todo_id: int,
    handle: dict[str, Any],
    exit_code: int | None,
    is_error: bool,
    denied: str | None,
    vanished: bool,
    budget_note: str | None,
    test_failure: str | None,
    tests_observed: bool,
    nothing: str | None,
    clean: bool,
    max_turns: int | None,
    cost_usd: float | None,
    compiled: bool | None = None,
    corrected: bool = False,
    tests_expected: bool | None = None,
) -> dict[str, Any]:
    """The ``meta.outcome`` record for one terminal run: the spine's
    outcome, the four gates, the spend/wall figures the suspicious
    deciles and suitability ledger read, and the sampled-review
    decision. ``meta.verdict`` is deliberately untouched — it is the
    other column, filled by the review or by hand.

    ``compiled`` is the caller's probe evidence (``None`` = nothing to
    probe); ``corrected`` says a test session left non-test changes
    over the code sibling's produce; ``tests_expected`` overrides the
    class-level obligation (a code run owes tests once it left
    commits)."""
    task_class = CODE_TASK_CLASS
    try:
        todo = store.get_ref(kind="todo", id=int(todo_id))
        if todo is not None:
            task_class = str((todo.meta or {}).get("task_class") or CODE_TASK_CLASS)
    except Exception:
        pass
    usage = job_meta.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    nt = usage.get("num_turns")
    num_turns = (
        int(nt) if isinstance(nt, (int, float)) and not isinstance(nt, bool) else None
    )
    if tests_expected is None:
        tests_expected = task_class in TEST_EXPECTED_CLASSES
    gates = hard_gates(
        compiled=compiled,
        is_error=is_error,
        vanished=vanished,
        num_turns=num_turns,
        max_turns=max_turns,
        tests_observed=tests_observed,
        tests_expected=tests_expected,
    )
    passed = gate_pass(
        gates,
        denied=denied,
        budget_note=budget_note,
        test_failure=test_failure,
    )
    outcome = classify(
        exit_code=exit_code,
        vanished=vanished,
        num_turns=num_turns,
        max_turns=max_turns,
        clean=clean,
        nothing=nothing,
    )
    wall_s = _wall_s(handle.get("started_at"))
    task_kind = str(job_meta.get("task_kind") or f"unknown.{task_class}")
    picks = prior_picks(store, todo_id)
    disagree = session_disagreement(
        picks, task_class=task_class, gate_pass=passed, corrected=corrected
    )
    stats: dict[str, Any] = {"n": 0, "cost_p10": None, "wall_p10": None}
    try:
        with store.pool.connection() as conn:
            stats = category_stats(conn, task_kind, exclude_job_id=job_id)
    except Exception:
        log.warning("outcome: category stats failed", exc_info=True)
    cheap = (
        passed
        and int(stats["n"]) >= SUSPICIOUS_MIN_RUNS
        and (
            (
                cost_usd is not None
                and stats["cost_p10"] is not None
                and cost_usd <= float(stats["cost_p10"])
            )
            or (
                wall_s is not None
                and stats["wall_p10"] is not None
                and wall_s <= float(stats["wall_p10"])
            )
        )
    )
    reasons = select_review(
        job_id=job_id,
        n_in_category=int(stats["n"]),
        disagreement=disagree,
        suspicious=cheap,
    )
    return {
        "outcome": outcome,
        "gates": gates,
        "gate_pass": passed,
        "task_kind": task_kind,
        "cost_usd": cost_usd,
        "wall_s": wall_s,
        "num_turns": num_turns,
        "tests_observed": bool(tests_observed),
        "tests_expected": tests_expected,
        "corrected": corrected,
        "at": _utcnow(),
        "review": {
            "sampled": bool(reasons),
            "reasons": reasons,
            "minted": False,
        },
    }


#: Pinnable ids for a lane that accepts a whole ``model_prefix`` family
#: but declares no ``models`` outside the avoid set (gr660610).
PREFIX_LANE_FALLBACK_MODELS: tuple[str, ...] = (
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
)

#: Adapters whose ``argv`` refuses a pinned model outside the lane's
#: ``models`` — ``devin_tree``'s allowlist raises ``ValueError``
#: (``_slice_adapters.Adapter.argv``; restated like ``lanes``' adapter
#: facts — this package stays below the workers layer, and a drift test
#: derives the set from argv itself). On one, a named account default is
#: not a launchable pin: offering it died as ``launch failed`` on every
#: retry (review td661037).
ALLOWLIST_MODEL_ADAPTERS: frozenset[str] = frozenset({"devin_tree"})

#: Note prefix :func:`diverse_model_pick` returns when a lane that could
#: run a different model exists but is at cap or backed off right now: a
#: wait, not a refusal. The caller parks on the lane-wait machinery
#: instead of refusing — a full claude-sub lane read as "no fit lane"
#: filed one coordinator ask per validate node (gr674214).
DIVERSE_BUSY_PREFIX = "run_slice: the different-model lane is busy"


def diverse_model_pick(
    store: Any,
    *,
    lane: Any,
    model: str | None,
    candidates: tuple[Any, ...],
    counts: dict[str, int] | None,
    backoffs: dict[str, datetime] | None,
    paid_ok: bool,
    avoid: frozenset[str],
    model_pin: str | None,
    task_class: str,
) -> tuple[Any, str | None, str | None]:
    """``(lane, model, note)`` — the test-session pick routed around the
    run's prior code models (gr595545), or ``(None, None, note)`` when
    no pick satisfies the different-model MUST: the caller REFUSES the
    launch (review td655142 — a test session on its code session's
    model is no test, not a fallback the old pick may ride).

    Same-lane alternates win first (the pick's lane still stands, only
    the model moves), then another candidate lane offering a model
    outside ``avoid`` under ``pick_lane``'s own ordering (cost class,
    then live count). A ``model_pin`` that repeats a code model is
    overridden — the constraint is a MUST — but a pinned LANE is a
    contract: it only re-picks its model, never hops.

    A resolved ``model=None`` — the lane's unnamed account default — is
    UNKNOWN unless this host's vendor config names it
    (:func:`account_default_model`, gr660610): a named default outside
    ``avoid`` is pinned where the lane's adapter can launch the pin, so
    a codex lane whose ``config.toml`` names its model stays fit; a
    default that cannot be named, or one only an allowlist adapter
    (:data:`ALLOWLIST_MODEL_ADAPTERS`) would run, still needs a declared
    model outside ``avoid`` or the pick fails (review td661037).
    """
    from precis.plans import lanes as _lanes

    if not avoid or lane is None:
        return lane, model, None

    def _pinnable(ln: Any, candidate: str) -> bool:
        """``True`` when the lane's adapter can launch ``candidate`` as
        a pin — an allowlist adapter (:data:`ALLOWLIST_MODEL_ADAPTERS`)
        takes only ids in ``models`` and raises ``ValueError`` on
        anything else; the rest carry any id."""
        return candidate in ln.models or ln.adapter not in ALLOWLIST_MODEL_ADAPTERS

    def _alt(ln: Any) -> str | None:
        declared = next((m for m in ln.models if m not in avoid), None)
        if declared is not None:
            return declared
        # An unpinned lane runs the account default; where the vendor
        # config names it, that named id may be the different model —
        # but only where the adapter can launch the pin.
        if not ln.models:
            named = account_default_model(ln.adapter)
            if named is not None and named not in avoid and _pinnable(ln, named):
                return named
        # A prefix lane (``claude -p``) runs every ``model_prefix`` id
        # whether or not ``models`` lists it (gr660610): pin a known
        # one outside ``avoid`` rather than call the lane unfit.
        return next(
            (
                m
                for m in PREFIX_LANE_FALLBACK_MODELS
                if m not in avoid
                and ln.model_prefix
                and ln.accepts_model(m)
                and _pinnable(ln, m)
            ),
            None,
        )

    # A resolved model that already differs needs nothing; a resolved
    # None (the account default) is unknown — it could BE a code model.
    if model is not None and model not in avoid:
        return lane, model, None

    alt = _alt(lane)
    if alt is not None:
        return (
            lane,
            alt,
            f"run_slice: test session must differ from the run's code "
            f"model(s) {sorted(avoid)}; lane {lane.name!r} carries "
            f"{alt!r} instead of {model or model_pin or 'the account default'}",
        )

    def _refuse() -> tuple[Any, None, str]:
        why = (
            f"its resolved model {model!r} repeats them"
            if model is not None
            else "its unnamed account default could repeat them"
        )
        return (
            None,
            None,
            f"run_slice: the test session must run a different model "
            f"than the run's code session(s) {sorted(avoid)}, but {why} "
            f"on lane {lane.name!r} and no fit lane offers one outside "
            f"them; refusing the launch — a same-model test is no test",
        )

    if len(candidates) <= 1:
        return _refuse()
    live = dict(counts) if counts is not None else _lanes.live_lane_counts(store)
    now = datetime.now(UTC)
    backed = {n for n, until in (backoffs or {}).items() if until and until > now}
    fit = [
        ln
        for ln in candidates
        if ln is not lane
        and ln.enabled
        and not ln.parked
        and ln.fits(task_class)
        and ln.name not in backed
        and live.get(ln.name, 0) < ln.cap
        and _alt(ln) is not None
    ]
    if not paid_ok and any(ln.cost_class == _lanes.COST_FREE for ln in fit):
        fit = [ln for ln in fit if ln.cost_class == _lanes.COST_FREE]
    if not fit:
        busy = sorted(
            ln.name
            for ln in candidates
            if ln is not lane
            and ln.enabled
            and not ln.parked
            and ln.fits(task_class)
            and (ln.name in backed or live.get(ln.name, 0) >= ln.cap)
            and _alt(ln) is not None
        )
        if busy:
            return (
                None,
                None,
                f"{DIVERSE_BUSY_PREFIX}: {', '.join(busy)} would run a model "
                f"outside {sorted(avoid)} but is full or backed off; parked "
                f"until a slot frees",
            )
        return _refuse()
    best = min(
        fit,
        key=lambda ln: (
            _lanes.COST_ORDER.index(ln.cost_class),
            live.get(ln.name, 0),
        ),
    )
    return (
        best,
        _alt(best),
        f"run_slice: test session must differ from the run's code "
        f"model(s) {sorted(avoid)}; re-picked lane {best.name!r} "
        f"on {_alt(best)!r} (was {lane.name!r} "
        f"{model or model_pin or 'the account default'})",
    )


def record_verdict(
    store: Any,
    job_id: int,
    verdict: str,
    *,
    by: str,
    note: str | None = None,
) -> None:
    """Write the second column: ``meta.verdict = {verdict, by, at,
    note?}`` on the run's job row. ``verdict`` is one of
    :data:`VERDICTS`; ``by`` names the writer (``run-quality td<n>`` or
    a hand)."""
    if verdict not in VERDICTS:
        raise ValueError(
            f"outcome.record_verdict: verdict must be one of {VERDICTS}, "
            f"got {verdict!r}"
        )
    payload: dict[str, Any] = {
        "verdict": verdict,
        "by": by,
        "at": _utcnow(),
    }
    if note:
        payload["note"] = note[:300]
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET meta = jsonb_set(meta, '{verdict}', %s::jsonb) "
            "WHERE ref_id = %s",
            (json.dumps(payload), int(job_id)),
        )


def pass_rate_table(conn: Any) -> list[dict[str, Any]]:
    """The pass-rate table the hard gates drive: per task category, the
    run count, how many passed every gate, and the outcome histogram."""
    rows = conn.execute(
        """
        SELECT meta->>'task_kind' AS kind,
               count(*) AS n,
               count(*) FILTER (
                   WHERE (meta->'outcome'->>'gate_pass')::boolean) AS gate_passed,
               count(*) FILTER (
                   WHERE meta->'outcome'->>'outcome' = 'completed') AS completed,
               count(*) FILTER (
                   WHERE meta->'outcome'->>'outcome' = 'completed_empty') AS empty,
               count(*) FILTER (
                   WHERE meta->'outcome'->>'outcome' = 'error') AS errors,
               count(*) FILTER (
                   WHERE meta->'outcome'->>'outcome' LIKE 'halted%%') AS halted
          FROM refs
         WHERE kind = 'job' AND retired_at IS NULL
           AND meta->>'job_type' = 'run_slice'
           AND meta ? 'outcome'
         GROUP BY 1 ORDER BY 2 DESC
        """
    ).fetchall()
    return [
        {
            "task_kind": str(r[0]),
            "n": int(r[1]),
            "gate_passed": int(r[2]),
            "completed": int(r[3]),
            "completed_empty": int(r[4]),
            "error": int(r[5]),
            "halted": int(r[6]),
        }
        for r in rows
    ]


def gate_lied_rate(conn: Any) -> list[dict[str, Any]]:
    """How often the gates lied, per category: judged runs, gate-pass
    runs the review failed, and the lie rate — the calibration the
    sampling policy is for."""
    rows = conn.execute(
        """
        SELECT meta->>'task_kind' AS kind,
               count(*) FILTER (WHERE meta ? 'verdict') AS judged,
               count(*) FILTER (
                   WHERE (meta->'outcome'->>'gate_pass')::boolean
                     AND meta->'verdict'->>'verdict' = 'fail') AS lied
          FROM refs
         WHERE kind = 'job' AND retired_at IS NULL
           AND meta->>'job_type' = 'run_slice'
           AND meta ? 'outcome'
           AND meta ? 'verdict'
         GROUP BY 1 ORDER BY 2 DESC
        """
    ).fetchall()
    return [
        {
            "task_kind": str(r[0]),
            "judged": int(r[1]),
            "lied": int(r[2]),
            "rate": (int(r[2]) / int(r[1]) if int(r[1]) else 0.0),
        }
        for r in rows
    ]


__all__ = [
    "CATEGORY_WARMUP",
    "CODE_TASK_CLASS",
    "OUTCOMES",
    "SAMPLE_PERCENT",
    "SUSPICIOUS_MIN_RUNS",
    "TEST_EXPECTED_CLASSES",
    "TEST_TASK_CLASS",
    "VERDICTS",
    "PriorPick",
    "account_default_model",
    "build_outcome",
    "category_stats",
    "classify",
    "code_session_models",
    "diverse_model_pick",
    "gate_lied_rate",
    "gate_pass",
    "hard_gates",
    "pass_rate_table",
    "prior_picks",
    "record_verdict",
    "select_review",
    "session_disagreement",
]
