"""Autoskill (``docs/backlog/autoskill.md``): selection math, fallback,
hold-out, prompt placement, the used-snapshot, the off switch, fault
isolation and the bimodal-skip at the planner / quest-tick seams.

Pure tests cover the selection functions; DB tests (``store`` fixture) seed
``autoskill_sessions`` with the mock embedder's vectors — the mock is
hash-based, so a past session "similar" to a request is one embedded from the
SAME text (cosine 1.0), and weighting/threshold cases are built from row
counts and ages rather than from semantic closeness.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from precis import route_log
from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.handlers.memory import MemoryHandler
from precis.skill_index import injection
from precis.skill_index.kind_skills import kind_skill_slugs
from precis.store import Tag
from precis.utils.llm import autoskill, router
from precis.utils.llm.router import LlmRequest, LlmResult, Tier
from tests.conftest import id_of

_TEXT = "autoskill probe: tighten the mounting boss on the enclosure lid"
_CORE = "precis-get-help"  # a real, mid-sized skill (well under 24k chars)
_TAIL_A = "precis-toon"
_TAIL_B = "precis-i2c-help"
_RARE = "precis-spi-help"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("PRECIS_AUTOSKILL", raising=False)
    monkeypatch.setenv("PRECIS_AUTOSKILL_HOLDOUT", "0")
    autoskill._reset_for_tests()
    route_log.bind_store(None)
    yield
    route_log.bind_store(None)
    autoskill._reset_for_tests()


@pytest.fixture
def bound(store: Any) -> Iterator[Any]:
    route_log.bind_store(store)
    yield store
    route_log.bind_store(None)


def _req(**kw: Any) -> LlmRequest:
    base: dict[str, Any] = {
        "tier": Tier.MEDIUM,
        "prompt": _TEXT,
        "tools_needed": True,
        "system_prompt": "SYSTEM PREFIX",
        "source": "autoskill-test",
    }
    base.update(kw)
    return LlmRequest(**base)


def _ok() -> LlmResult:
    return LlmResult(
        text="done", cost_usd=None, turns_used=1, model="m", tier=Tier.MEDIUM
    )


def _seed(
    store: Any,
    used: Sequence[Mapping[str, Any] | None],
    *,
    text: str = _TEXT,
    age_days: float = 0.0,
    errored: bool | None = False,
    injected: dict[str, Any] | None = None,
) -> None:
    vec = str(MockEmbedder().embed_one(text))
    inj = json.dumps(injected or {})
    with store.pool.connection() as conn:
        for u in used:
            conn.execute(
                "INSERT INTO autoskill_sessions "
                "(ts, source, embedding, holdout, injected, used, errored) "
                "VALUES (now() - (%s || ' days')::interval, 'seed', %s::vector, "
                "false, %s::jsonb, %s::jsonb, %s)",
                (
                    str(age_days),
                    vec,
                    inj,
                    None if u is None else json.dumps(u),
                    errored,
                ),
            )
        conn.commit()


def _rows(store: Any, where: str = "true", args: tuple[Any, ...] = ()) -> list[Any]:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT holdout, injected, used, errored, agentlog_id, source "
            f"FROM autoskill_sessions WHERE {where} ORDER BY id",
            args,
        ).fetchall()


# ── pure selection math ──────────────────────────────────────────────────


def test_neighbour_weight_halves_every_fourteen_days() -> None:
    assert autoskill.neighbour_weight(0.8, 0) == pytest.approx(0.8)
    assert autoskill.neighbour_weight(0.8, 14) == pytest.approx(0.4)
    assert autoskill.neighbour_weight(1.0, 28) == pytest.approx(0.25)
    assert autoskill.neighbour_weight(1.0, -3) == pytest.approx(1.0)  # clock skew


def test_shares_are_weighted_by_user_weight_over_total() -> None:
    nb = [
        autoskill.Neighbour(1.0, frozenset({"a", "b"}), frozenset({1})),
        autoskill.Neighbour(0.5, frozenset({"a"}), frozenset()),
        autoskill.Neighbour(0.5, frozenset(), frozenset({1})),
    ]
    skills, mems = autoskill.shares(nb)
    assert skills == {"a": pytest.approx(0.75), "b": pytest.approx(0.5)}
    assert mems == {1: pytest.approx(0.75)}
    assert autoskill.shares([]) == ({}, {})


def test_select_core_is_share_ge_80_in_share_order() -> None:
    share = {"a": 0.80, "b": 0.95, "c": 0.79, "d": 0.10, "e": 0.0999}
    sizes = dict.fromkeys(share, 1000)
    core, tail = autoskill.select_skills(share, sizes)
    assert core == ["b", "a"]
    assert tail == ["c", "d"]  # 0.79 and 0.10 qualify; 0.0999 does not


def test_select_char_budget_demotes_the_overflow_to_tail() -> None:
    share = {"big": 0.99, "next": 0.9, "small": 0.85}
    sizes = {"big": 20_000, "next": 10_000, "small": 500}
    core, tail = autoskill.select_skills(share, sizes)
    # 20k fits; 10k would reach 30k > 24k, so it and everything after it in
    # share order is demoted (the budget is share-ordered priority).
    assert core == ["big"]
    assert tail == ["next", "small"]
    # Exactly at the budget still fits.
    assert autoskill.select_skills({"x": 0.9}, {"x": 24_000}) == (["x"], [])
    assert autoskill.select_skills({"x": 0.9}, {"x": 24_001}) == ([], ["x"])


def test_select_tail_is_capped_at_eight_and_drops_unknown_skills() -> None:
    share = {f"s{i}": 0.5 - i * 0.01 for i in range(12)}
    share["gone"] = 0.99  # not in the corpus any more
    sizes = {k: 10 for k in share if k != "gone"}
    core, tail = autoskill.select_skills(share, sizes)
    assert core == []
    assert tail == [f"s{i}" for i in range(8)]
    assert "gone" not in core + tail


def test_neighbour_cap_is_fifty() -> None:
    assert autoskill._NEIGHBOURS == 50


def test_memory_sessions_counts_distinct_neighbours() -> None:
    nb = [
        autoskill.Neighbour(1.0, memories=frozenset({1, 2})),
        autoskill.Neighbour(0.1, memories=frozenset({1})),
        autoskill.Neighbour(0.1, memories=frozenset({1, 3})),
    ]
    assert autoskill.memory_sessions(nb) == {1: 3, 2: 1, 3: 1}


def test_select_memories_needs_three_distinct_sessions_cap_and_liveness() -> None:
    # 12 memories, ids 1..12, all fetched by 3 sessions; share falls with id.
    share = {i: 0.06 - i * 0.003 for i in range(1, 13)}
    sessions = dict.fromkeys(share, 3)
    sessions[99], share[99] = 2, 0.9  # huge share, but only 2 sessions
    live = set(share) - {2}  # id 2 is deleted / stale
    picked = autoskill.select_memories(share, sessions, live)
    assert picked == [1, 3, 4, 5, 6]  # cap 5, id 2 skipped, 99 below 3 sessions
    assert autoskill.select_memories({7: 0.001}, {7: 3}, {7}) == [7]  # no share floor
    assert autoskill.select_memories({7: 0.5}, {7: 2}, {7}) == []
    assert autoskill.select_memories({7: 0.5}, {}, {7}) == []


def test_select_memories_ranks_by_weighted_share_not_session_count() -> None:
    share = {1: 0.02, 2: 0.05, 3: 0.03}
    sessions = {1: 9, 2: 3, 3: 4}
    assert autoskill.select_memories(share, sessions, {1, 2, 3}) == [2, 3, 1]


def test_render_block_always_ends_with_the_two_line_gripe_stanza() -> None:
    block = autoskill.render_block([], [], [])
    stanza = block.rsplit("\n\n", 1)[-1].splitlines()
    assert len(stanza) == 2
    assert "put(kind='gripe', text=" in stanza[0]
    assert "get(kind='skill', id='precis-gripe-help')" in stanza[1]
    full = autoskill.render_block([_CORE], [_TAIL_A], [(5, "A memory title")])
    assert f"### Skill: {_CORE}" in full
    assert f"- {_TAIL_A} — " in full
    assert "- memory 5 — A memory title" in full
    assert full.endswith(stanza[1])


def test_enabled_and_holdout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert autoskill.enabled() is True
    monkeypatch.setenv("PRECIS_AUTOSKILL", " OFF ")
    assert autoskill.enabled() is False
    monkeypatch.delenv("PRECIS_AUTOSKILL_HOLDOUT")
    assert autoskill.holdout_fraction() == 0.05
    monkeypatch.setenv("PRECIS_AUTOSKILL_HOLDOUT", "0.5")
    assert autoskill.holdout_fraction() == 0.5
    monkeypatch.setenv("PRECIS_AUTOSKILL_HOLDOUT", "7")
    assert autoskill.holdout_fraction() == 1.0
    monkeypatch.setenv("PRECIS_AUTOSKILL_HOLDOUT", "junk")
    assert autoskill.holdout_fraction() == 0.05


def test_active_needs_a_bound_store(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert autoskill.active() is False  # enabled, but nothing bound
    route_log.bind_store(store)
    assert autoskill.active() is True
    monkeypatch.setenv("PRECIS_AUTOSKILL", "off")
    assert autoskill.active() is False


# ── prepare: selection against the DB ────────────────────────────────────


def _memory(hub: Hub, title: str) -> int:
    return id_of(MemoryHandler(hub=hub).put(text=f"{title}\n\nbody of {title}").body)


def test_prepare_selects_core_tail_and_live_memory_pointers(
    bound: Any, hub: Hub
) -> None:
    keep = _memory(hub, "Keep me fresh")
    rare = _memory(hub, "Rarely read")
    stale = _memory(hub, "Gone stale")
    deleted = _memory(hub, "Deleted one")
    bound.add_tag(stale, Tag.parse_strict("STALE:retracted-premise", kind="memory"))
    bound.retire_ref(deleted)

    used: list[dict[str, Any] | None] = []
    for i in range(30):
        skills = [_CORE]
        if i < 20:
            skills.append(_TAIL_A)  # 0.667 -> tail
        if i < 4:
            skills.append(_TAIL_B)  # 0.133 -> tail
        if i < 2:
            skills.append(_RARE)  # 0.067 -> dropped
        mems = [stale, deleted]  # share 1.0, but not live
        if i < 10:
            mems.append(keep)  # 0.333 -> pointer
        if i < 2:
            mems.append(rare)  # only 2 distinct sessions -> no pointer
        used.append({"skills": skills, "memories": mems})
    _seed(bound, used)

    plan = autoskill.prepare(_req())
    assert plan is not None and not plan.holdout
    assert plan.injected == {
        "core": [_CORE],
        "tail": [_TAIL_A, _TAIL_B],
        "memories": [keep],
        "fallback": None,
    }
    assert f"### Skill: {_CORE}" in plan.block
    assert f"- {_TAIL_A} — " in plan.block
    assert f"- memory {keep} — Keep me fresh" in plan.block
    assert "Gone stale" not in plan.block and "Deleted one" not in plan.block
    assert "put(kind='gripe', text=" in plan.block  # stanza always present


def test_prepare_time_decay_prefers_recent_sessions(bound: Any) -> None:
    # 14 sessions from 28 days ago (weight 0.25 each) use A; 16 fresh ones
    # use B. Undecayed shares would be 0.47 / 0.53 (both tail); decayed B is
    # 16 / 19.5 = 0.82 -> core, A is 0.18 -> tail.
    _seed(bound, [{"skills": [_TAIL_A], "memories": []}] * 14, age_days=28)
    _seed(bound, [{"skills": [_CORE], "memories": []}] * 16)
    plan = autoskill.prepare(_req())
    assert plan is not None
    assert plan.injected["core"] == [_CORE]
    assert plan.injected["tail"] == [_TAIL_A]


def _used_skills(store: Any) -> list[Any]:
    return [r[2]["skills"] for r in _rows(store)]


def test_injected_core_credit_keeps_a_never_fetched_skill_in_the_core(
    bound: Any,
) -> None:
    # Generation 1 (older) fetched _CORE itself.
    _seed(bound, [{"skills": [_CORE], "memories": []}] * 20, age_days=28)
    plan1 = autoskill.prepare(_req())
    assert plan1 is not None and plan1.injected["core"] == [_CORE]
    # Generation 2 was handed _CORE in full, so never fetched it: used is
    # empty, injected.core carries it. Without credit its share would be
    # 5 / 25 = 0.2 (tail); with credit the whole chain stays at 1.0.
    _seed(
        bound,
        [{"skills": [], "memories": []}] * 20,
        injected=plan1.injected,
    )
    plan2 = autoskill.prepare(_req())
    assert plan2 is not None
    assert plan2.injected["core"] == [_CORE]
    assert plan2.injected["tail"] == []
    # The stored evidence is raw: the credited rows still say "used nothing".
    assert _used_skills(bound).count([]) == 20
    assert _used_skills(bound).count([_CORE]) == 20


def test_without_credit_the_never_fetched_skill_would_drop_out(bound: Any) -> None:
    # Same shape, but gen 2 carries no injected core: _CORE falls to the tail.
    _seed(bound, [{"skills": [_CORE], "memories": []}] * 20, age_days=28)
    _seed(bound, [{"skills": [], "memories": []}] * 20)
    plan = autoskill.prepare(_req())
    assert plan is not None
    assert plan.injected["core"] == []
    assert plan.injected["tail"] == [_CORE]


def test_errored_neighbours_earn_no_injected_credit(
    bound: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(injection, "match_skill", lambda text: None)
    inj = {"core": [_CORE], "tail": [], "memories": [], "fallback": None}
    _seed(bound, [{"skills": [], "memories": []}] * 25, errored=True, injected=inj)
    _seed(bound, [{"skills": [_TAIL_A], "memories": []}] * 25, injected={})
    plan = autoskill.prepare(_req())
    assert plan is not None
    assert plan.injected["core"] == [_TAIL_A]
    assert _CORE not in plan.injected["tail"]


def test_fallback_injection_gets_the_same_credit(bound: Any) -> None:
    inj = {"core": [], "tail": [], "memories": [], "fallback": _TAIL_A}
    _seed(bound, [{"skills": [], "memories": []}] * 25, injected=inj)
    plan = autoskill.prepare(_req())
    assert plan is not None and plan.injected["core"] == [_TAIL_A]


def test_injected_credit_ignores_malformed_injected() -> None:
    assert autoskill._injected_credit(None) == set()
    assert autoskill._injected_credit({}) == set()
    assert autoskill._injected_credit({"core": ["a"], "fallback": "b"}) == {"a", "b"}


def test_prepare_ignores_errored_unattributed_and_expired_sessions(
    bound: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    used = [{"skills": [_CORE], "memories": []}]
    _seed(bound, used * 25, errored=True)  # errored
    _seed(bound, [None] * 25)  # no attribution
    _seed(bound, used * 25, age_days=61)  # outside the 60-day window
    monkeypatch.setattr(
        injection,
        "match_skill",
        lambda text: injection.SkillMatch(slug=_TAIL_A, title="t", score=0.99),
    )
    plan = autoskill.prepare(_req())
    assert plan is not None
    # 0 neighbours (< 20) -> bimodal fallback, not the seeded _CORE.
    assert plan.injected == {
        "core": [_TAIL_A],
        "tail": [],
        "memories": [],
        "fallback": _TAIL_A,
    }


def test_prepare_falls_back_to_bimodal_below_twenty_neighbours(
    bound: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(bound, [{"skills": [_CORE], "memories": []}] * 19)
    seen: list[str] = []

    def fake_match(text: str) -> injection.SkillMatch:
        seen.append(text)
        return injection.SkillMatch(slug=_TAIL_A, title="t", score=0.99)

    monkeypatch.setattr(injection, "match_skill", fake_match)
    plan = autoskill.prepare(_req())
    assert plan is not None
    assert seen == [_TEXT]
    assert plan.injected["fallback"] == _TAIL_A
    assert plan.injected["core"] == [_TAIL_A]
    assert plan.injected["tail"] == [] and plan.injected["memories"] == []
    assert f"### Skill: {_TAIL_A}" in plan.block

    # 20 neighbours is enough history: no fallback.
    _seed(bound, [{"skills": [_CORE], "memories": []}])
    seen.clear()
    plan = autoskill.prepare(_req())
    assert plan is not None and plan.injected["fallback"] is None
    assert seen == []


def test_fallback_with_no_bimodal_match_is_just_the_gripe_stanza(
    bound: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(injection, "match_skill", lambda text: None)
    plan = autoskill.prepare(_req())
    assert plan is not None
    assert plan.injected == {"core": [], "tail": [], "memories": [], "fallback": None}
    assert "get(kind='skill', id='precis-gripe-help')" in plan.block


def test_query_text_prefers_prompt_then_user_turns() -> None:
    assert autoskill.query_text(_req(prompt="  hello  ")) == "hello"
    msgs = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "mid"},
        {"role": "user", "content": [{"type": "text", "text": "second"}]},
    ]
    assert autoskill.query_text(_req(prompt="", messages=msgs)) == "first\n\nsecond"
    assert autoskill.query_text(_req(prompt="", messages=None)) == ""


# ── apply: placement ─────────────────────────────────────────────────────


def _plan(block: str = "BLOCK", *, holdout: bool = False) -> autoskill.Plan:
    return autoskill.Plan(
        [0.0] * 1024,
        block,
        {"core": [], "tail": [], "memories": [], "fallback": None},
        holdout,
    )


def test_apply_appends_to_prompt_end_never_system_prompt() -> None:
    req = _req(prompt="TASK", system_prompt="SYSTEM PREFIX")
    out = autoskill.apply(_plan(), req)
    assert out.prompt == "TASK\n\nBLOCK"
    assert out.system_prompt == "SYSTEM PREFIX"  # cached prefix byte-identical
    assert req.prompt == "TASK"  # the caller's request is not mutated


def test_apply_appends_to_the_last_user_message() -> None:
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "ack"},
        {"role": "user", "content": [{"type": "text", "text": "two"}]},
    ]
    req = _req(prompt="", messages=msgs, system_prompt=None)
    out = autoskill.apply(_plan(), req)
    assert out.prompt == ""
    assert out.messages is not None
    assert out.messages[0]["content"] == "SYS"
    assert out.messages[1]["content"] == "one"
    assert out.messages[3]["content"] == [
        {"type": "text", "text": "two"},
        {"type": "text", "text": "BLOCK"},
    ]
    assert msgs[3]["content"] == [{"type": "text", "text": "two"}]  # untouched

    both = autoskill.apply(_plan(), _req(prompt="TASK", messages=msgs[:2]))
    assert both.prompt == "TASK\n\nBLOCK"
    assert both.messages is not None and both.messages[1]["content"] == "one\n\nBLOCK"


def test_apply_holdout_or_none_plan_is_a_noop() -> None:
    req = _req()
    assert autoskill.apply(None, req) is req
    assert autoskill.apply(_plan("", holdout=True), req) is req


# ── record + the router wrapper ──────────────────────────────────────────


def _fake_inner(
    monkeypatch: pytest.MonkeyPatch,
    seen: list[LlmRequest],
    *,
    before_return: Any = None,
    result: LlmResult | None = None,
) -> None:
    def inner(req: LlmRequest) -> LlmResult:
        seen.append(req)
        if before_return is not None:
            before_return()
        return result or _ok()

    monkeypatch.setattr(router, "_route_inner", inner)


def test_route_injects_block_and_records_row_with_used_snapshot(
    bound: Any, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    mem = _memory(hub, "Read during the run")
    _seed(bound, [{"skills": [_CORE], "memories": []}] * 25)
    since = datetime.now(UTC)
    agentlog = 424242

    def tool(
        verb: str,
        kind: str,
        sample: dict[str, Any] | None,
        *,
        outcome: str = "ok",
        ts: datetime | None = None,
        log_id: int = agentlog,
    ) -> None:
        with bound.pool.connection() as conn:
            conn.execute(
                "INSERT INTO tool_calls (ts, agentlog_id, verb, kind, outcome, "
                "arg_sample) VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
                (
                    ts or datetime.now(UTC),
                    log_id,
                    verb,
                    kind,
                    outcome,
                    None if sample is None else json.dumps(sample),
                ),
            )
            conn.commit()

    paper_hint = kind_skill_slugs("paper")
    memory_hint = kind_skill_slugs("memory")  # every memory get is a kind use too
    assert paper_hint and memory_hint  # the kind->skill mapping leaned on exists

    def during_call() -> None:
        tool("get", "skill", {"id": _TAIL_A})  # explicit skill read
        tool("get", "skill", {"id": f"{_TAIL_B}~Intro"})  # ~section stripped
        tool("get", "skill", {"id": "no-such-skill-zz"})  # unknown slug dropped
        tool("get", "skill", {"id": _RARE}, outcome="error")  # failed read
        tool("search", "paper", {"q": "x"})  # kind hint
        tool("get", "memory", {"id": mem})  # memory fetch (int id)
        tool("get", "memory", {"id": f"me{mem + 1}"})  # handle-shaped id
        tool("get", "memory", None)  # no id: ignored
        tool("get", "skill", {"id": "precis-toc"}, ts=since - timedelta(hours=1))
        tool("get", "skill", {"id": _CORE}, log_id=agentlog + 1)  # other run

    seen: list[LlmRequest] = []
    _fake_inner(monkeypatch, seen, before_return=during_call)
    req = _req(env_overlay={"PRECIS_CURRENT_AGENTLOG": str(agentlog)})
    out = router.route(req)

    assert out.text == "done"
    assert len(seen) == 1
    assert seen[0].prompt.startswith(_TEXT + "\n\n")
    assert f"### Skill: {_CORE}" in seen[0].prompt
    assert seen[0].system_prompt == "SYSTEM PREFIX"

    rows = _rows(bound, "source = 'autoskill-test'")
    assert len(rows) == 1
    holdout, injected, used, errored, log_id, source = rows[0]
    assert holdout is False and errored is False and log_id == agentlog
    assert injected["core"] == [_CORE]
    assert used["skills"] == sorted({_TAIL_A, _TAIL_B, *paper_hint, *memory_hint})
    assert used["memories"] == sorted([mem, mem + 1])


def test_route_records_errored_calls_and_null_used_without_agentlog(
    bound: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[LlmRequest] = []
    failed = LlmResult(
        text="", cost_usd=None, turns_used=None, model="m", tier=Tier.MEDIUM,
        error="boom",
    )  # fmt: skip
    _fake_inner(monkeypatch, seen, result=failed)
    router.route(_req())  # no env_overlay -> no agentlog id
    (row,) = _rows(bound, "source = 'autoskill-test'")
    assert row[3] is True  # errored
    assert row[2] is None  # used: no attribution
    assert row[4] is None


def test_holdout_is_recorded_with_no_block_and_no_fallback(
    bound: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(autoskill, "_is_holdout", lambda: True)
    called: list[str] = []

    def _match(text: str) -> None:
        called.append(text)

    monkeypatch.setattr(injection, "match_skill", _match)
    seen: list[LlmRequest] = []
    _fake_inner(monkeypatch, seen)
    req = _req()
    router.route(req)
    assert seen[0].prompt == req.prompt  # unchanged: no block at all
    assert called == []  # no bimodal fallback for the control group
    (row,) = _rows(bound, "source = 'autoskill-test'")
    assert row[0] is True
    assert row[1] == {"core": [], "tail": [], "memories": [], "fallback": None}


def test_off_switch_is_a_noop(bound: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_AUTOSKILL", "off")
    boom = pytest.fail
    monkeypatch.setattr(autoskill, "prepare", lambda r: boom("prepare ran"))
    seen: list[LlmRequest] = []
    _fake_inner(monkeypatch, seen)
    req = _req()
    router.route(req)
    assert seen == [req]
    assert _rows(bound) == []


def test_tool_less_calls_skip_autoskill(
    bound: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(autoskill, "prepare", lambda r: pytest.fail("prepare ran"))
    seen: list[LlmRequest] = []
    _fake_inner(monkeypatch, seen)
    req = _req(tools_needed=False)
    router.route(req)
    assert seen == [req]


def test_no_bound_store_routes_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[LlmRequest] = []
    _fake_inner(monkeypatch, seen)
    req = _req()
    router.route(req)
    assert seen == [req]


# ── faults never break a call ────────────────────────────────────────────


class _BrokenStore:
    class pool:
        @staticmethod
        def connection() -> Any:
            raise RuntimeError("db down")


def test_raising_embedder_leaves_the_request_unchanged(
    bound: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Bad:
        dim = 1024

        def embed_one(self, text: str) -> list[float]:
            raise RuntimeError("embedder down")

    monkeypatch.setattr(autoskill, "_get_embedder", lambda: Bad())
    seen: list[LlmRequest] = []
    _fake_inner(monkeypatch, seen)
    req = _req()
    out = router.route(req)
    assert out.text == "done"
    assert seen == [req]
    assert _rows(bound) == []


def test_no_embedder_leaves_the_request_unchanged(
    bound: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(injection, "_default_embedder", lambda: None)
    seen: list[LlmRequest] = []
    _fake_inner(monkeypatch, seen)
    req = _req()
    router.route(req)
    assert seen == [req]
    assert autoskill._embedder_failed is True  # cached, not retried per call


def test_raising_db_leaves_the_request_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_log.bind_store(cast(Any, _BrokenStore()))
    seen: list[LlmRequest] = []
    _fake_inner(monkeypatch, seen)
    req = _req()
    out = router.route(req)
    assert out.text == "done"
    assert seen == [req]
    # record() with a plan against a dead DB swallows the fault too.
    autoskill.record(_plan(), req, _ok(), datetime.now(UTC))


def test_wrong_dimension_embedder_is_refused(
    bound: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(injection, "_default_embedder", lambda: MockEmbedder(dim=8))
    assert autoskill.prepare(_req()) is None
    assert autoskill._embedder_failed is True


# ── bimodal skip at the planner / quest-tick seams ───────────────────────


def _bimodal_hit(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake_match(text: str) -> injection.SkillMatch:
        calls.append(text)
        return injection.SkillMatch(slug=_CORE, title="t", score=0.99)

    monkeypatch.setattr(injection, "match_skill", fake_match)
    return calls


def test_planner_and_quest_bimodal_skip_when_autoskill_active(
    bound: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.quest import tick
    from precis.workers import planner_prompt

    calls = _bimodal_hit(monkeypatch)
    monkeypatch.setattr(planner_prompt, "_ref_body", lambda ctx: "task text")
    assert planner_prompt._m_skill_injection(cast(Any, None)) == ""
    assert tick._skill_injection_section("task text") == ""
    assert calls == []  # the bimodal match never even ran


def test_planner_and_quest_bimodal_unchanged_when_autoskill_off(
    bound: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.quest import tick
    from precis.workers import planner_prompt

    monkeypatch.setenv("PRECIS_AUTOSKILL", "off")
    calls = _bimodal_hit(monkeypatch)
    monkeypatch.setattr(planner_prompt, "_ref_body", lambda ctx: "task text")
    assert "Auto-matched skill" in planner_prompt._m_skill_injection(cast(Any, None))
    assert "Auto-matched skill" in tick._skill_injection_section("task text")
    assert calls == ["task text", "task text"]


def test_bimodal_still_runs_when_no_store_is_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Autoskill can't run without a store, so the bimodal call must not be
    # skipped either — a session never gets neither.
    from precis.quest import tick

    calls = _bimodal_hit(monkeypatch)
    assert "Auto-matched skill" in tick._skill_injection_section("task text")
    assert calls == ["task text"]


# ── retention ────────────────────────────────────────────────────────────


def test_gc_prunes_autoskill_rows_older_than_sixty_days(bound: Any) -> None:
    used: list[dict[str, Any] | None] = [{"skills": [], "memories": []}]
    _seed(bound, used * 3, age_days=61)
    _seed(bound, used * 2, age_days=59)
    route_log.gc(bound)
    assert len(_rows(bound)) == 2
