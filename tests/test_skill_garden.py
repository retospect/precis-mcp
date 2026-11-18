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
    body: str = "## Rules\n- never do the risky thing\n",
) -> str:
    """A minimal skill markdown: front-matter + a rule-bearing section."""
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
    monkeypatch.setattr(sg, "_run_example", lambda call: "call output")

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
    monkeypatch.setattr(sg, "_run_example", lambda call: "call output")

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
                evidence="gr resolved on main",
                rationale="shrink it",
                model="m",
            ),
            sg.SkillVerdict(verdict="keep", rationale="cause still live", model="m"),
        ]
    )

    monkeypatch.setattr(sg, "_corpus", lambda: corpus)
    monkeypatch.setattr(sg, "propose_llm", lambda *a, **kw: next(verdicts))
    monkeypatch.setattr(sg, "_run_example", lambda call: "call output")

    todo = int(
        store.insert_ref(kind="todo", slug=None, title="monthly tick", meta={}).id
    )
    ctx = _Ctx(store, dispatched_from_todo=todo)
    sg._dispatch(ctx, sg.SPEC)

    report = ctx.meta["skill_garden"]["report"]
    assert "simplified:" in report
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
