"""``skill_garden`` — the monthly skill-rules review (gr658419).

The deterministic half (``collect_records``) needs no DB: it buckets
every rule record ``ask_reto`` (no ruling / governed / Reto's say-so)
or ``check`` (incident-born ``gr*`` ruling). The judged half is
DB-backed (``store`` fixture): verdicts write ``reviews`` rows on the
skill's anchor ref, deduped on a key carrying the skill file's sha, so
a re-run over unchanged bytes proposes and re-asks nothing. The LLM is
never invoked — the ``propose_llm``/``_run_example`` seams take fakes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.store import Store
from precis.workers.job_types import skill_garden as sg
from tests._refs import insert_gripe


def _skill(
    *,
    governs: str | None = None,
    rulings: list[str] | None = None,
    body: str | None = None,
) -> str:
    """A minimal skill markdown: front-matter + a rule-bearing section
    (citing the first ruling, as a section the judge may see must)."""
    if body is None:
        cite = f" ({rulings[0]})" if rulings else ""
        body = f"## Rules\n- never do the risky thing{cite}\n"
    fm: list[str] = []
    if governs is not None:
        fm.append(f"governs: {governs}")
    if rulings:
        fm.append("rulings: [" + ", ".join(rulings) + "]")
    head = "---\n" + "\n".join(fm) + "\n---\n" if fm else ""
    return f"{head}# T\n{body}"


def _gripe(store: Store, title: str = "the incident", *, done: bool = False) -> int:
    """Mint a gripe ref; ``done=True`` closes it with a resolved comment."""
    ref = insert_gripe(store, title, status="done" if done else "open")
    if done:
        store.chunks.insert_chunks(
            ref.id,
            [
                sg.ChunkInsert(
                    ord=0,
                    text="resolved: abc1234 (main) — fixed and guarded",
                    meta={"chunk_kind": "gripe_comment"},
                )
            ],
        )
    return int(ref.id)


def _proposals(store: Store) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        return [
            json.loads(r[0])["garden"]
            for r in conn.execute(
                "SELECT note FROM reviews WHERE verdict = 'proposed' ORDER BY review_id"
            ).fetchall()
        ]


def _rejections(store: Store) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        return [
            json.loads(r[0])["garden"]
            for r in conn.execute(
                "SELECT note FROM reviews WHERE verdict = 'rejected' ORDER BY review_id"
            ).fetchall()
        ]


class _Ctx:
    """Minimal DispatchContext stand-in (the test_garden shape)."""

    def __init__(
        self, store: Store, params: dict[str, Any] | None = None, **meta: Any
    ) -> None:
        self.store = store
        self.ref_id = 1
        self.meta: dict[str, Any] = {"params": params or {}}
        self.meta.update(meta)
        self.events: list[tuple[str, str]] = []
        self.failure: str | None = None
        self.deferred: str | None = None

    def append_chunk(self, kind: str, text: str) -> None:
        self.events.append((kind, text))

    def set_meta(self, **fields: Any) -> None:
        self.meta.update(fields)

    def record_failure(self, reason: str, **_kw: Any) -> None:
        self.failure = reason

    def defer(self, _delay: Any, reason: str) -> bool:
        self.deferred = reason
        return True


def test_candidates_split_by_ruling_kind() -> None:
    """A governed skill and an unruled rule-bearing section land in
    ``ask_reto``; an incident-born ``gr*`` ruling lands in ``check``;
    a memory ruling is Reto's say-so and lands in ``ask_reto``."""
    corpus = {
        "governed-skill": _skill(governs="factory", rulings=["gr123"]),
        "unruled": _skill(body="## Steps\n- must hold the door\n"),
        "incident": _skill(rulings=["gr7"]),
        "say-so": _skill(rulings=["me9"]),
    }
    records = sg.collect_records(corpus)
    by_skill: dict[str, list[sg.RuleRecord]] = {}
    for r in records:
        by_skill.setdefault(r.skill, []).append(r)

    [governed] = by_skill["governed-skill"]
    assert governed.bucket == "ask_reto" and governed.governed
    [unruled] = by_skill["unruled"]
    assert unruled.bucket == "ask_reto" and unruled.ruling is None
    assert unruled.section == "Steps"
    [incident] = by_skill["incident"]
    assert incident.bucket == "check" and incident.ruling_kind == "incident"
    assert incident.ruling == "gr7"
    [sayso] = by_skill["say-so"]
    assert sayso.bucket == "ask_reto" and sayso.ruling_kind == "say-so"


def test_drop_needs_evidence_and_dedups(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drop without an evidence quote is demoted to keep (rejection
    row, no proposal); a re-run over the unchanged skill writes no
    second row and never re-asks the judge. An edit re-opens the key."""
    gid = _gripe(store)
    corpus = {"inc": _skill(rulings=[f"gr{gid}"])}
    calls: list[str] = []

    def _propose(store: Store, rec: sg.RuleRecord, **_kw: Any) -> sg.SkillVerdict:
        calls.append(rec.skill)
        return sg.SkillVerdict(
            verdict="drop", evidence="", rationale="it's gone", model="m"
        )

    monkeypatch.setattr(sg, "_corpus", lambda: corpus)
    monkeypatch.setattr(sg, "propose_llm", _propose)
    monkeypatch.setattr(sg, "_run_example", lambda call, **kw: "call output")

    ctx = _Ctx(store)
    sg._dispatch(ctx, sg.SPEC)
    g = ctx.meta["skill_garden"]
    assert _proposals(store) == [] and g["proposals"] == 0
    assert g["kept"] == 1
    (rej,) = _rejections(store)
    assert rej["proposer_reject"] and rej["reason"] == "no_evidence"

    # Re-run on the same bytes: the key is decided — no judge call,
    # no second ledger row.
    ctx2 = _Ctx(store)
    sg._dispatch(ctx2, sg.SPEC)
    assert calls == ["inc"]
    assert ctx2.meta["skill_garden"]["skipped_rejected"] == 1
    assert _proposals(store) == []


def test_proposed_dedups_on_unchanged_and_reopens_on_edit(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An evidenced drop becomes a ``proposed`` row; a second run over
    unchanged bytes emits nothing; editing the skill re-opens the key."""
    gid = _gripe(store, done=True)
    corpus = {"inc": _skill(rulings=[f"gr{gid}"])}
    calls: list[str] = []

    def _propose(store: Store, rec: sg.RuleRecord, **_kw: Any) -> sg.SkillVerdict:
        calls.append(rec.skill)
        return sg.SkillVerdict(
            verdict="drop",
            evidence="resolved: abc1234 on main",
            rationale="cause gone",
            model="m",
        )

    monkeypatch.setattr(sg, "_corpus", lambda: corpus)
    monkeypatch.setattr(sg, "propose_llm", _propose)
    monkeypatch.setattr(sg, "_run_example", lambda call, **kw: "call output")

    sg._dispatch(_Ctx(store), sg.SPEC)
    (prop,) = _proposals(store)
    assert prop["detector"] == "skill_garden" and prop["action"] == "prune"

    sg._dispatch(_Ctx(store), sg.SPEC)
    assert len(_proposals(store)) == 1 and calls == ["inc"]

    corpus["inc"] = corpus["inc"] + "\nextra line\n"
    sg._dispatch(_Ctx(store), sg.SPEC)
    assert len(_proposals(store)) == 2 and calls == ["inc", "inc"]


def test_report_lists_three_buckets(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The monthly report counts simplified / kept (with why) / needs
    Reto with handles, lands on meta.skill_garden.report and as one
    todo_comment on the dispatching todo."""
    gid = _gripe(store, done=True)
    corpus = {
        "inc": _skill(rulings=[f"gr{gid}"]),
        "keepme": _skill(rulings=["gr999"]),
        "unruled": _skill(body="## Caution\n- warning: hot\n"),
    }
    verdicts = iter(
        [
            sg.SkillVerdict(
                verdict="simplify",
                evidence="resolved: abc1234 on main",
                rationale="shrink it",
                model="m",
            ),
            sg.SkillVerdict(verdict="keep", rationale="cause still live", model="m"),
        ]
    )

    monkeypatch.setattr(sg, "_corpus", lambda: corpus)
    monkeypatch.setattr(sg, "propose_llm", lambda *a, **kw: next(verdicts))
    monkeypatch.setattr(sg, "_run_example", lambda call, **kw: "call output")

    todo = int(
        store.insert_ref(kind="todo", slug=None, title="monthly tick", meta={}).id
    )
    ctx = _Ctx(store, dispatched_from_todo=todo)
    sg._dispatch(ctx, sg.SPEC)

    report = ctx.meta["skill_garden"]["report"]
    assert "proposed this run" in report
    assert "kept:" in report
    assert "needs Reto:" in report
    assert "unruled" in report and "inc" in report

    comments = [
        c
        for c in store.chunks.list_chunks_for_ref(todo)
        if c.chunk_kind == "todo_comment"
    ]
    assert len(comments) == 1 and "needs Reto" in comments[0].text


def test_dry_run_collects_without_llm_or_writes(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``params.dry_run`` is the deterministic pass alone: buckets on
    meta, zero ledger rows, zero judge calls."""
    gid = _gripe(store)
    corpus = {"inc": _skill(rulings=[f"gr{gid}"])}
    monkeypatch.setattr(sg, "_corpus", lambda: corpus)
    monkeypatch.setattr(
        sg,
        "propose_llm",
        lambda *a, **kw: pytest.fail("dry run called the judge"),
    )
    ctx = _Ctx(store, {"dry_run": True})
    sg._dispatch(ctx, sg.SPEC)
    g = ctx.meta["skill_garden"]
    assert g["dry_run"] is True
    assert len(g["check"]) == 1 and g["proposals"] == 0
    assert _proposals(store) == [] and _rejections(store) == []


def test_registered_job_type() -> None:
    from precis.workers.job_types import get_job_type, known_job_types

    spec = get_job_type("skill_garden")
    assert spec is not None and spec is sg.SPEC
    assert "skill_garden" in known_job_types()
    assert spec.compatible_executors == frozenset({"job_inproc"})


def test_limit_counts_attempts_and_reaches_later_records(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decided records do not eat the limit: run 2 reaches the record
    run 1 deferred."""
    corpus = {f"s{i}": _skill(rulings=[f"gr{_gripe(store)}"]) for i in range(3)}
    calls: list[str] = []

    def _propose(store: Store, rec: sg.RuleRecord, **_kw: Any) -> sg.SkillVerdict:
        calls.append(rec.skill)
        return sg.SkillVerdict(verdict="keep", rationale="holds", model="m")

    monkeypatch.setattr(sg, "_corpus", lambda: corpus)
    monkeypatch.setattr(sg, "propose_llm", _propose)
    monkeypatch.setattr(sg, "_run_example", lambda call, **kw: "out")
    for _ in range(3):
        sg._dispatch(_Ctx(store, {"limit": 1}), sg.SPEC)
    assert calls == ["s0", "s1", "s2"]


def test_closed_gripe_reopens_a_kept_rule(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keep under an open gripe is re-asked once the gripe resolves."""
    gid = _gripe(store)
    corpus = {"inc": _skill(rulings=[f"gr{gid}"])}
    calls: list[int] = []

    def _propose(store: Store, rec: sg.RuleRecord, **_kw: Any) -> sg.SkillVerdict:
        calls.append(1)
        return sg.SkillVerdict(verdict="keep", rationale="open", model="m")

    monkeypatch.setattr(sg, "_corpus", lambda: corpus)
    monkeypatch.setattr(sg, "propose_llm", _propose)
    monkeypatch.setattr(sg, "_run_example", lambda call, **kw: "out")
    sg._dispatch(_Ctx(store), sg.SPEC)
    sg._dispatch(_Ctx(store), sg.SPEC)
    assert len(calls) == 1
    store.chunks.insert_chunks(
        gid,
        [
            sg.ChunkInsert(
                ord=5,
                text="resolved: abc1234 (main)",
                meta={"chunk_kind": "gripe_comment"},
            )
        ],
    )
    sg._dispatch(_Ctx(store), sg.SPEC)
    assert len(calls) == 2


def test_section_attribution_and_ambiguity() -> None:
    """A section citing exactly one ruling is pinned to it; one citing
    several, or none, is Reto's call — a sole incident never governs an
    uncited section, and a mixed incident + say-so section never reaches
    the incident judge."""
    body = (
        "## A\n- never touch gr5 things\n\n## B\n- must ask first\n\n"
        "## C\n- never mix gr5 and me2\n"
    )
    recs = sg.collect_records({"x": _skill(rulings=["gr5", "me2"], body=body)})
    by = {r.section: r for r in recs if r.section}
    assert (by["A"].ruling, by["A"].attribution, by["A"].bucket) == (
        "gr5",
        "cited",
        "check",
    )
    for sec in ("B", "C"):
        assert (by[sec].ruling, by[sec].attribution, by[sec].bucket) == (
            None,
            "ambiguous",
            "ask_reto",
        )
    one = sg.collect_records({"y": _skill(rulings=["gr5"], body=body)})
    assert {(r.section, r.bucket) for r in one if r.section} == {
        ("A", "check"),
        ("B", "ask_reto"),
        ("C", "check"),
    }


def test_judge_sees_only_blocks_citing_the_ruling() -> None:
    body = "## A\n- never touch gr5 things\n\n- never do the unrelated thing\n"
    [rec] = sg.collect_records({"x": _skill(rulings=["gr5"], body=body)})
    assert "gr5" in rec.rule_text and "unrelated" not in rec.rule_text


def test_grounding_needs_cause_gone() -> None:
    done = {"status": "done", "resolved": "abc1234", "title": "t", "excerpt": ""}
    assert sg._grounded("resolved: abc1234", done, "skipped: no store")
    # open / wontfix gripes and failed replays prove nothing
    open_ = {"status": "open", "resolved": None, "title": "t", "excerpt": ""}
    assert not sg._grounded("status: open", open_, "")
    wont = {"status": "done", "resolved": "wontfix", "title": "t", "excerpt": ""}
    assert not sg._grounded("resolved: wontfix", wont, "")
    observation = {**done, "resolved": "observation"}
    assert not sg._grounded("resolved: observation", observation, "")
    assert not sg._grounded("no example call in the rule", done, sg._NO_EXAMPLE)
    assert not sg._grounded(
        "no runtime installed", done, "skipped: no runtime installed"
    )
    assert not sg._grounded("error: boom happened", done, "error: boom happened")
    assert sg._grounded("the cache now answers", done, "the cache now answers fast")


def test_ungrounded_evidence_is_demoted(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evidence not traceable to the gripe or replay never proposes."""
    gid = _gripe(store, done=True)
    corpus = {"inc": _skill(rulings=[f"gr{gid}"])}
    monkeypatch.setattr(sg, "_corpus", lambda: corpus)
    monkeypatch.setattr(
        sg,
        "propose_llm",
        lambda *a, **kw: sg.SkillVerdict(
            verdict="drop", evidence="totally invented proof", rationale="x"
        ),
    )
    monkeypatch.setattr(sg, "_run_example", lambda call, **kw: "out")
    sg._dispatch(_Ctx(store), sg.SPEC)
    assert _proposals(store) == []
    assert _rejections(store)[0]["reason"] == "no_evidence"


def test_run_example_refuses_writes_and_unlisted_kinds() -> None:
    assert "not a read-only verb" in sg._run_example("put(kind='memory', text='x')")
    assert "replay list" in sg._run_example("get(kind='paper', id='10.1/x')")


def test_report_accounts_decided_and_empty(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Earlier keeps keep their reason in the report; an empty run still
    comments on the todo."""
    gid = _gripe(store)
    corpus = {"inc": _skill(rulings=[f"gr{gid}"])}
    monkeypatch.setattr(sg, "_corpus", lambda: corpus)
    monkeypatch.setattr(
        sg,
        "propose_llm",
        lambda *a, **kw: sg.SkillVerdict(verdict="keep", rationale="still open"),
    )
    monkeypatch.setattr(sg, "_run_example", lambda call, **kw: "out")
    sg._dispatch(_Ctx(store), sg.SPEC)
    ctx = _Ctx(store)
    sg._dispatch(ctx, sg.SPEC)
    report = ctx.meta["skill_garden"]["report"]
    assert "earlier: still open" in report and f"(gr{gid})" in report

    todo = int(store.insert_ref(kind="todo", slug=None, title="t", meta={}).id)
    monkeypatch.setattr(sg, "_corpus", lambda: {})
    ctx = _Ctx(store, dispatched_from_todo=todo)
    sg._dispatch(ctx, sg.SPEC)
    assert any(
        c.chunk_kind == "todo_comment" for c in store.chunks.list_chunks_for_ref(todo)
    )


def test_approved_proposal_is_not_reproposed_and_reports_its_state(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approval does not re-propose over unchanged bytes and reports
    as 'edit not yet applied' — not as a confirmed edit; once the skill
    changes and the approved rule text is gone it reports 'edit verified'
    (history survives the sha change)."""
    from precis.graph.garden.proposals import resolve_proposal

    gid = _gripe(store, done=True)
    corpus = {"inc": _skill(rulings=[f"gr{gid}"])}
    calls: list[int] = []

    def _propose(*_a: Any, **_kw: Any) -> sg.SkillVerdict:
        calls.append(1)
        return sg.SkillVerdict(
            verdict="drop", evidence="resolved: abc1234", rationale="gone", model="m"
        )

    monkeypatch.setattr(sg, "_corpus", lambda: corpus)
    monkeypatch.setattr(sg, "propose_llm", _propose)
    monkeypatch.setattr(sg, "_run_example", lambda call, **kw: "out")
    sg._dispatch(_Ctx(store), sg.SPEC)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT review_id FROM reviews WHERE verdict = 'proposed'"
        ).fetchone()
    assert row is not None
    assert resolve_proposal(store, int(row[0]), actor="reto", verdict="approved")

    ctx = _Ctx(store)
    sg._dispatch(ctx, sg.SPEC)
    assert len(calls) == 1 and len(_proposals(store)) == 1
    report = ctx.meta["skill_garden"]["report"]
    assert "approved, edit not yet applied (last 35d): 1" in report
    assert (
        "edit verified (approved rule text gone from the skill) (last 35d): 0" in report
    )
    assert "confirmed" not in report and "simplified" not in report

    corpus["inc"] = corpus["inc"].replace("## Rules", "## Renamed rules")
    ctx = _Ctx(store)
    sg._dispatch(ctx, sg.SPEC)
    report = ctx.meta["skill_garden"]["report"]
    assert "skill changed since approval, edit unverified (last 35d): 1" in report
    assert (
        "edit verified (approved rule text gone from the skill) (last 35d): 0" in report
    )

    corpus["inc"] = _skill(rulings=[f"gr{gid}"], body="## Rules\n- calm now\n")
    ctx = _Ctx(store)
    sg._dispatch(ctx, sg.SPEC)
    report = ctx.meta["skill_garden"]["report"]
    assert "approved, edit not yet applied (last 35d): 0" in report
    assert (
        "edit verified (approved rule text gone from the skill) (last 35d): 1" in report
    )


def test_run_example_replay_allowlist_is_flat() -> None:
    for call in (
        "search(kind='memory', q='x', batch=[{'key': 'a', 'q': 'b'}])",
        "search(kind='memory', q='x', args={'batch': []})",
        "get(kind='skill', id='paper:10.1/x')",
        "search(kind='*', q='x')",
    ):
        assert "skipped" in sg._run_example(call), call


def test_replay_runs_on_a_pruned_runtime(store: Store) -> None:
    """Worker replay builds its own runtime on the job's store: allowlisted
    kinds answer, every other handler is gone (so routing cannot leave the
    allowlist), and a call's own failure comes back ``error:``-prefixed."""
    gid = _gripe(store, "replayable incident")
    rt = sg._build_replay_runtime(store)
    assert set(rt.hub.handlers) <= sg._REPLAY_KINDS

    def read_records() -> tuple[Any, Any]:
        with store.pool.connection() as conn:
            recall = conn.execute(
                "SELECT last_recalled_at FROM refs WHERE ref_id = %s", (gid,)
            ).fetchone()
            reads = conn.execute(
                "SELECT * FROM ref_reads WHERE ref_id = %s", (gid,)
            ).fetchall()
        return recall, reads

    before = read_records()
    assert before == ((None,), [])
    out = sg._run_example(f"get(kind='gripe', id='{gid}')", store)
    assert sg._replay_ok(out) and "replayable incident" in out
    assert read_records() == before
    assert not sg._replay_ok(
        sg._run_example("get(kind='gripe', id='999999999')", store)
    )


def test_empty_skill_selection_scans_and_judges_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    gid = _gripe(store)
    corpus = {"inc": _skill(rulings=[f"gr{gid}"])}
    assert sg.collect_records(corpus, skills=[]) == []
    monkeypatch.setattr(sg, "_corpus", lambda: corpus)
    monkeypatch.setattr(
        sg, "propose_llm", lambda *a, **kw: pytest.fail("empty scope called judge")
    )
    ctx = _Ctx(store, {"skills": []})
    sg._dispatch(ctx, sg.SPEC)
    garden = ctx.meta["skill_garden"]
    assert garden["scanned"] == garden["records"] == 0
    assert garden["check"] == garden["ask_reto"] == []
    assert "inc (gr" not in garden["report"]
    assert _proposals(store) == [] and _rejections(store) == []


def test_report_goes_to_schedule_owning_parent(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spawned tick carries no ``meta.schedule``; the report lands on
    the recurring root above it, not on the tick."""
    monkeypatch.setattr(sg, "_corpus", lambda: {"u": _skill()})
    root = int(
        store.insert_ref(
            kind="todo", slug=None, title="monthly", meta={"schedule": {"every": "30d"}}
        ).id
    )
    tick = int(
        store.insert_ref(
            kind="todo", slug=None, title="tick", meta={}, parent_id=root
        ).id
    )
    ctx = _Ctx(store, dispatched_from_todo=tick)
    sg._dispatch(ctx, sg.SPEC)

    def _comments(rid: int) -> list[Any]:
        return [
            c
            for c in store.chunks.list_chunks_for_ref(rid)
            if c.chunk_kind == "todo_comment"
        ]

    assert len(_comments(root)) == 1 and not _comments(tick)
    assert ctx.meta["skill_garden"]["reported_on"] == root


def test_mid_sentence_caution_reaches_ask_reto() -> None:
    corpus = {
        "r": _skill(
            body="## Queue\nThe recurring root never appears in the doable queue.\n"
        )
    }
    recs = sg.collect_records(corpus)
    assert [r.bucket for r in recs] == ["ask_reto"]


def test_marker_only_edit_is_not_a_verified_removal(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``- never x`` → ``* never x`` keeps the rule: the approval stays
    ``changed_unmatched``, never ``edited``."""
    from precis.graph.garden.proposals import resolve_proposal

    gid = _gripe(store, done=True)
    corpus = {"inc": _skill(rulings=[f"gr{gid}"])}
    monkeypatch.setattr(sg, "_corpus", lambda: corpus)
    monkeypatch.setattr(
        sg,
        "propose_llm",
        lambda *a, **kw: sg.SkillVerdict(
            verdict="drop", evidence="resolved: abc1234", rationale="gone", model="m"
        ),
    )
    monkeypatch.setattr(sg, "_run_example", lambda call, **kw: "out")
    sg._dispatch(_Ctx(store), sg.SPEC)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT review_id FROM reviews WHERE verdict = 'proposed'"
        ).fetchone()
    assert row is not None
    assert resolve_proposal(store, int(row[0]), actor="reto", verdict="approved")

    corpus["inc"] = corpus["inc"].replace("- never", "* never")
    states = [a.state for a in sg.approval_history(store, corpus)]
    assert states == ["changed_unmatched"]
