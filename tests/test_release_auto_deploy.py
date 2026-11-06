"""``release.auto_deploy`` (td573809, slice S9 of td570415): the standing
approval — the plan tick answers the release-pass approval ask
``deploy r<N>`` itself once CI is recorded green on the cut's exact sha
and the shared ``release.round`` record names an open round on that head
sha (me573231: no range inspection; an absent, unreadable or other-sha's
round refuses). A second red ``release-fix`` iteration files a ruling ask
and closes the loop — a third fix never starts by itself."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from precis import settings as psettings
from precis.plans import release as R
from precis.plans import templates as T
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.workers.job_types import plan_dispatch as pd
from tests._script import load_script

pytestmark = pytest.mark.db

REPO_ROOT = Path(__file__).resolve().parents[1]

SHA = "c0ffee" + "1" * 34
SHA2 = "beef" + "2" * 36
PREV = "9" * 12
RNG = f"{PREV}..{SHA[:12]}"
RNG2 = f"{PREV}..{SHA2[:12]}"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    psettings.invalidate()


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    ref = store.get_ref(kind="todo", id=ref_id)
    assert ref is not None
    return dict(ref.meta or {})


def _status(store: Store, ref_id: int) -> str | None:
    for t in store.tags_for(ref_id):
        if t.prefix == "STATUS":
            return str(t.value)
    return None


def _comment(store: Store, todo_id: int, text: str) -> None:
    existing = list(store.chunks.list_chunks_for_ref(todo_id))
    ord_ = max((int(c.ord) for c in existing), default=0) + 1
    store.chunks.insert_chunks(
        todo_id,
        [ChunkInsert(ord=ord_, text=text, meta={"chunk_kind": "todo_comment"})],
    )


def _comments(store: Store, todo_id: int) -> list[str]:
    return [
        str(c.text)
        for c in store.chunks.list_chunks_for_ref(todo_id)
        if c.chunk_kind == "todo_comment"
    ]


def _close(store: Store, todo_id: int) -> None:
    store.add_tag(
        todo_id, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )


def _jobs_under(store: Store, todo_id: int) -> list[tuple[int, dict[str, Any]]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = 'job' AND parent_id = %s "
            "AND retired_at IS NULL ORDER BY ref_id",
            (todo_id,),
        ).fetchall()
    return [(int(r[0]), dict(r[1])) for r in rows]


def _green_pass(store: Store) -> T.PlanMint:
    """A release-pass whose cut is recorded green; cut and poll closed."""
    mint = T.mint_release_pass(store)
    R.record_cut(store, mint.ids["cut"], "r7", SHA, RNG, repo="o/r")
    _close(store, mint.ids["cut"])
    _close(store, mint.ids["poll"])
    R.record_ci_verdict(store, SHA, RNG, "green", "31")
    return mint


def _round_row(
    store: Store, *, n: int = 7, base: str = SHA, state: str = "green"
) -> None:
    psettings.set_setting(
        "release.round",
        json.dumps(
            {
                "n": n,
                "branch": f"release/r{n}",
                "base": base,
                "state": state,
                "owner": {"host": "node-a", "checkout": "/srv/repo"},
            }
        ),
        store=store,
    )


def _open_round(store: Store) -> None:
    _round_row(store)


def _ask_ids(store: Store) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT rt.ref_id FROM ref_tags rt JOIN tags t USING (tag_id)
              JOIN refs r ON r.ref_id = rt.ref_id
             WHERE t.namespace = 'OPEN' AND t.value = 'ask-user'
               AND r.kind = 'todo' AND r.retired_at IS NULL
            """
        ).fetchall()
    return [int(r[0]) for r in rows]


# ── the setting ────────────────────────────────────────────────────


def test_setting_registered_default_off_db_only() -> None:
    spec = psettings.REGISTRY["release.auto_deploy"]
    assert spec.type == "bool"
    assert spec.default is False
    assert spec.env_var is None
    assert spec.doc.strip()


# ── the tick answers the green ask ─────────────────────────────────


def test_off_by_default_the_ask_stays_open(store: Store) -> None:
    mint = _green_pass(store)
    _open_round(store)
    for _ in range(3):
        pd.run_pass(store)
    approval = mint.ids["approval"]
    assert _status(store, approval) == "open"
    assert _comments(store, approval) == []
    assert "ask-user" in {str(t.value) for t in store.tags_for(approval)}
    assert _jobs_under(store, mint.ids["deploy"]) == []


def test_on_and_green_answers_then_deploys(store: Store) -> None:
    mint = _green_pass(store)
    _open_round(store)
    psettings.set_setting("release.auto_deploy", "true", store=store)
    approval = mint.ids["approval"]

    for _ in range(3):
        pd.run_pass(store)
        if _status(store, approval) == "done":
            break

    comments = _comments(store, approval)
    assert len(comments) == 1
    text = comments[0]
    assert text.startswith("answer: deploy r7")
    for piece in ("release.auto_deploy", SHA[:12], RNG, "31"):
        assert piece in text, piece
    assert "hold" not in text.split()
    assert _status(store, approval) == "done"
    assert "ask-user" not in {str(t.value) for t in store.tags_for(approval)}
    rn, why = R.approval(store, mint.ids["deploy"])
    assert rn == "r7", why

    res = pd.run_pass(store)
    jobs = _jobs_under(store, mint.ids["deploy"])
    assert len(jobs) == 1, res.notes
    assert jobs[0][1]["job_type"] == "release_deploy"


def test_dry_run_writes_nothing(store: Store) -> None:
    mint = _green_pass(store)
    _open_round(store)
    psettings.set_setting("release.auto_deploy", "true", store=store)
    res = pd.run_pass(store, dry_run=True)
    assert res.dry_run
    assert _comments(store, mint.ids["approval"]) == []
    assert _status(store, mint.ids["approval"]) == "open"


def test_factory_off_writes_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PRECIS_FACTORY=off`` parks the whole pass read-only: the armed
    auto-answer posts no comment either (the job's dry-run path)."""
    mint = _green_pass(store)
    _open_round(store)
    psettings.set_setting("release.auto_deploy", "true", store=store)
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    reason = pd.read_only_reason(store)
    assert reason == "PRECIS_FACTORY=off"
    res = pd.run_pass(store, dry_run=reason is not None, reason=reason)
    assert res.dry_run
    assert _comments(store, mint.ids["approval"]) == []
    assert _status(store, mint.ids["approval"]) == "open"


def test_a_second_tick_posts_no_second_answer(store: Store) -> None:
    mint = _green_pass(store)
    _open_round(store)
    psettings.set_setting("release.auto_deploy", "true", store=store)
    approval = mint.ids["approval"]
    pd.run_pass(store)
    assert len(_comments(store, approval)) == 1
    pd.run_pass(store)
    assert len(_comments(store, approval)) == 1


# ── fail closed ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "case",
    [
        "no-verdict",
        "green-other-sha",
        "red-newest",
        "prod-head-fallback",
        "round-unparsable",
        "round-json-list",
        "round-other-n",
        "round-deploying",
        "round-confirmed",
        "round-absent",
        "round-other-sha",
        "round-no-base",
        "round-raw-raises",
        "setting-maybe",
        "setting-off",
    ],
)
def test_auto_answer_fails_closed(
    store: Store, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    mint = T.mint_release_pass(store)
    R.record_cut(store, mint.ids["cut"], "r7", SHA, RNG, repo="o/r")
    approval = mint.ids["approval"]

    if case == "green-other-sha":
        R.record_ci_verdict(store, SHA2, RNG2, "green", "32")
    elif case == "red-newest":
        R.record_ci_verdict(store, SHA, RNG, "green", "31")
        R.record_ci_verdict(store, SHA, RNG, "red", "33")
    elif case == "prod-head-fallback":
        # The tests-green gate's prod-head fallback must not count: only
        # a recorded verdict row for the cut's exact sha answers.
        R.record_prod_head(store, SHA, [SHA[:12]])
    elif case != "no-verdict":
        R.record_ci_verdict(store, SHA, RNG, "green", "31")

    if case == "round-unparsable":
        psettings.set_setting("release.round", "not json", store=store)
    elif case == "round-json-list":
        psettings.set_setting("release.round", "[1,2]", store=store)
    elif case == "round-other-n":
        _round_row(store, n=8)
    elif case in ("round-deploying", "round-confirmed"):
        _round_row(store, state=case.removeprefix("round-"))
    elif case == "round-other-sha":
        _round_row(store, base=SHA2)
    elif case == "round-no-base":
        psettings.set_setting("release.round", '{"n":7,"state":"green"}', store=store)
    elif case != "round-absent":
        _open_round(store)
    if case == "round-raw-raises":

        def boom(store_: Store) -> str | None:
            raise OSError("db gone")

        monkeypatch.setattr(R, "_round_raw", boom)

    if case == "setting-maybe":
        psettings.set_setting("release.auto_deploy", "maybe", store=store)
    elif case != "setting-off":
        psettings.set_setting("release.auto_deploy", "true", store=store)

    ok, _why = R.auto_answer_approval(store, approval)
    assert not ok
    assert _comments(store, approval) == []


def test_a_human_answer_is_never_overwritten(store: Store) -> None:
    mint = _green_pass(store)
    _open_round(store)
    psettings.set_setting("release.auto_deploy", "true", store=store)
    _comment(store, mint.ids["approval"], "answer: hold")
    ok, why = R.auto_answer_approval(store, mint.ids["approval"])
    assert not ok and why == ""
    assert _comments(store, mint.ids["approval"]) == ["answer: hold"]


def test_setting_turned_off_after_on_refuses(store: Store) -> None:
    """The armed check reads the DB fresh — a flip back off between ticks
    disarms the next one (the cache must not answer a stale ``true``)."""
    mint = _green_pass(store)
    _open_round(store)
    psettings.set_setting("release.auto_deploy", "true", store=store)
    psettings.set_setting("release.auto_deploy", "false", store=store)
    ok, why = R.auto_answer_approval(store, mint.ids["approval"])
    assert (ok, why) == (False, "")
    assert _comments(store, mint.ids["approval"]) == []


def test_a_short_cut_sha_refuses(store: Store) -> None:
    """A cut sha under ``LANDED_SHA_LEN`` chars cannot name an exact CI
    verdict — refuse even with a green row recorded on it."""
    mint = T.mint_release_pass(store)
    R.record_cut(store, mint.ids["cut"], "r7", "abc1234", RNG, repo="o/r")
    _open_round(store)
    R.record_ci_verdict(store, "abc1234", RNG, "green", "31")
    psettings.set_setting("release.auto_deploy", "true", store=store)
    ok, why = R.auto_answer_approval(store, mint.ids["approval"])
    assert not ok and "sha" in why
    assert _comments(store, mint.ids["approval"]) == []


def test_ask_options_mismatch_refuses(store: Store) -> None:
    """Only the ask ``record_cut`` rewrote to ``deploy r<N> | hold`` is a
    release approval — any other options are a human's ask, untouched."""
    mint = _green_pass(store)
    _open_round(store)
    psettings.set_setting("release.auto_deploy", "true", store=store)
    approval = mint.ids["approval"]
    ask = dict(_meta(store, approval)["ask"])
    ask["options"] = ["deploy r7", "abort"]
    store.stamp_ref_meta(approval, {"ask": ask})
    ok, why = R.auto_answer_approval(store, approval)
    assert (ok, why) == (False, "")
    assert _comments(store, approval) == []


def test_ask_blocks_mismatch_refuses(store: Store) -> None:
    """The release approval must block a live ``release_deploy`` todo —
    an ask that blocks anything else never gets the deploy answer."""
    mint = _green_pass(store)
    _open_round(store)
    psettings.set_setting("release.auto_deploy", "true", store=store)
    approval = mint.ids["approval"]
    ask = dict(_meta(store, approval)["ask"])
    ask["blocks"] = "nothing"
    store.stamp_ref_meta(approval, {"ask": ask})
    ok, why = R.auto_answer_approval(store, approval)
    assert (ok, why) == (False, "")
    assert _comments(store, approval) == []


@pytest.mark.parametrize("state", ["cut", "gating", "fixing", "green"])
def test_an_open_round_naming_the_head_sha_is_answered(
    store: Store, state: str
) -> None:
    mint = _green_pass(store)
    psettings.set_setting("release.auto_deploy", "true", store=store)
    _round_row(store, state=state)
    ok, why = R.auto_answer_approval(store, mint.ids["approval"])
    assert ok, why
    comments = _comments(store, mint.ids["approval"])
    assert len(comments) == 1 and comments[0].startswith("answer: deploy r7")


def test_the_cut_written_row_arms_the_auto_answer(store: Store) -> None:
    """gr575362: the row ``release_cut.poll`` writes is exactly what
    ``_open_round_record`` needs — open state, same ``n``, ``base`` the
    cut's head sha — so the tick can answer without the script's
    publish ever having reached prod."""
    mint = _green_pass(store)
    R.update_round(
        store,
        {
            "n": 7,
            "branch": "release/r7",
            "base": SHA,
            "state": "cut",
            "owner": {"host": "node-a", "checkout": "/srv/repo"},
        },
        create=True,
    )
    psettings.set_setting("release.auto_deploy", "true", store=store)
    ok, why = R.auto_answer_approval(store, mint.ids["approval"])
    assert ok, why
    comments = _comments(store, mint.ids["approval"])
    assert len(comments) == 1
    assert comments[0].startswith("answer: deploy r7")


def test_non_release_approval_is_untouched(store: Store) -> None:
    psettings.set_setting("release.auto_deploy", "true", store=store)
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title="Land the PR?",
            meta={
                "ask": {
                    "kind": "approval",
                    "options": ["land", "hold"],
                    "blocks": "nothing",
                    "context": "a pull request approval, not a release",
                }
            },
            parent_id=None,
            conn=conn,
        )
    ok, why = R.auto_answer_approval(store, int(ref.id))
    assert (ok, why) == (False, "")
    assert _comments(store, int(ref.id)) == []


def test_round_open_states_match_the_script() -> None:
    """``release._ROUND_OPEN_STATES`` is the same literal
    ``scripts/lib/round_state.py`` and ``scripts/round`` validate — the
    states a round may be in while its deploy is still auto-answerable."""
    if sys.platform == "win32":
        pytest.skip("scripts/round is POSIX release tooling")
    reader = load_script(
        "round_state_auto_deploy_pin",
        REPO_ROOT / "scripts" / "lib" / "round_state.py",
    )
    script = load_script(
        "round_script_auto_deploy_pin", REPO_ROOT / "scripts" / "round"
    )
    assert R._ROUND_OPEN_STATES == reader.OPEN_STATES
    assert R._ROUND_OPEN_STATES == script._OPEN_STATES


# ── a second red fix loop asks; a third never starts by itself ──────


def _red_pass(
    store: Store, *, count: int, exhausted_ask: int | None = None
) -> T.PlanMint:
    """A release-pass cut recorded red, ``meta.loop.count`` stamped."""
    mint = T.mint_release_pass(store)
    R.record_cut(store, mint.ids["cut"], "r7", SHA, RNG, repo="o/r")
    _close(store, mint.ids["cut"])
    _close(store, mint.ids["poll"])
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    loop = dict(_meta(store, mint.ids["loop"])["loop"])
    loop["count"] = count
    if exhausted_ask is not None:
        loop["exhausted_ask"] = exhausted_ask
    store.stamp_ref_meta(mint.ids["loop"], {"loop": loop})
    return mint


def _loop_children(store: Store, loop_id: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'todo' AND parent_id = %s "
            "AND retired_at IS NULL",
            (loop_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


@pytest.mark.parametrize("auto", [False, True], ids=["auto-off", "auto-on"])
def test_second_red_fix_loop_files_an_ask_and_mints_no_third(
    store: Store, auto: bool
) -> None:
    """me573231: two red fix iterations run unattended; the second red
    files the ruling ask and stops — a third fix loop is a human call."""
    mint = _red_pass(store, count=2)
    if auto:
        psettings.set_setting("release.auto_deploy", "true", store=store)
    res = pd.run_pass(store)
    assert res.asks == 1, res.notes
    assert res.loops_minted == 0, res.notes
    assert _status(store, mint.ids["loop"]) == "done"
    assert _loop_children(store, mint.ids["loop"]) == []

    asks = [i for i in _ask_ids(store) if i != mint.ids["approval"]]
    assert len(asks) == 1, asks
    tags = {str(t.value) for t in store.tags_for(asks[0])}
    assert {"ask-user", "showstopper", f"plan:{mint.root_id}"} <= tags
    ask = _meta(store, asks[0])["ask"]
    assert ask["kind"] == "ruling"
    assert ask["blocks"] == "nothing"
    context = ask["context"]
    assert "r7" in context and "CI red" in context and "31" in context

    # A second pass files no second ask; the loop stays closed.
    res = pd.run_pass(store)
    assert res.asks == 0 and res.loops_minted == 0
    assert [i for i in _ask_ids(store) if i != mint.ids["approval"]] == asks


def test_first_red_fix_loop_mints_the_second_iteration(store: Store) -> None:
    mint = _red_pass(store, count=1)
    res = pd.run_pass(store)
    assert res.asks == 0 and res.loops_minted == 1, res.notes
    assert _meta(store, mint.ids["loop"])["loop"]["count"] == 2
    assert _loop_children(store, mint.ids["loop"]) != []


def test_an_exhausted_stamped_loop_closes_without_a_second_ask(
    store: Store,
) -> None:
    mint = _red_pass(store, count=2, exhausted_ask=4242)
    res = pd.run_pass(store)
    assert res.asks == 0, res.notes
    assert res.loops_minted == 0
    assert _status(store, mint.ids["loop"]) == "done"
    assert _loop_children(store, mint.ids["loop"]) == []


def _iteration_body(store: Store, loop_todo_id: int, **_ctx: Any) -> T.PlanMint:
    """A minimal loop body for the non-release cap check."""
    _loop, root = T._loop_context(store, loop_todo_id)

    def build(m: T._Minter) -> None:
        m.node("iter", "Iterate", {"op": "run_slice"}, body="Do it.")

    return T._run_body(store, loop_todo_id, root, build)


def test_a_non_release_loop_keeps_its_stamped_cap(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a ``release-fix`` loop stops at two automatic iterations
    (me573231); any other body honours its stamped ``meta.loop.max`` —
    a third iteration mints, no ask is filed."""
    monkeypatch.setitem(T.BODIES, "test-body", _iteration_body)

    def build(m: T._Minter) -> None:
        m.node("root", "Plan", {"auto_close": True})
        m.node(
            "loop",
            "Loop",
            {
                "loop": {
                    "max": 3,
                    "until": {"kind": "answer"},
                    "body": "test-body",
                    "count": 2,
                }
            },
        )

    mint = T._run(store, None, build)
    res = pd.run_pass(store)
    assert res.loops_minted == 1, res.notes
    assert res.asks == 0
    assert _meta(store, mint.ids["loop"])["loop"]["count"] == 3
    assert _status(store, mint.ids["loop"]) == "open"
