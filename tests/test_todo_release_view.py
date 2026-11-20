"""``get(kind='todo', id=<release pass>, view='release')`` (gr735230): the
pass on one screen, read from stored data only — header, CI, one line per
node with its newest job, the stored hold reason, the open asks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Unsupported
from precis.handlers.todo import TodoHandler
from precis.plans import release as R
from precis.plans import templates as T
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.todo import release_view

pytestmark = pytest.mark.db

SHA = "c0ffee" + "1" * 34
RNG = f"{'9' * 12}..{SHA[:12]}"
NOW = datetime(2026, 11, 19, 18, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _cut_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "cut_host", lambda store: "node-a")


def _status(store: Store, ref_id: int, value: str) -> None:
    store.add_tag(
        ref_id, Tag.closed("STATUS", value), set_by="system", replace_prefix=True
    )


def _comment(store: Store, todo_id: int, text: str) -> None:
    ord_ = max((c.ord for c in store.chunks.list_chunks_for_ref(todo_id)), default=0)
    store.chunks.insert_chunks(
        todo_id,
        [ChunkInsert(ord=ord_ + 1, text=text, meta={"chunk_kind": "todo_comment"})],
    )


def _job(store: Store, node: int, job_type: str, status: str, **meta: Any) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title=f"{job_type} td{node}",
        parent_id=node,
        meta={"job_type": job_type},
    )
    _status(store, int(ref.id), status)
    # after the status: a terminal STATUS stamps its own ``finished_at``
    store.stamp_ref_meta(int(ref.id), meta)
    return int(ref.id)


def _ask(store: Store, title: str, *tags: str, options: list[str]) -> int:
    ref = store.insert_ref(
        kind="todo",
        slug=None,
        title=title,
        meta={"ask": {"kind": "ruling", "options": options, "blocks": "nothing"}},
    )
    _status(store, int(ref.id), "open")
    for tag in ("ask-user", "showstopper", *tags):
        store.add_tag(int(ref.id), Tag.open(tag), set_by="system")
    return int(ref.id)


def _cut_pass(store: Store) -> T.PlanMint:
    mint = T.mint_release_pass(store)
    R.record_cut(store, mint.ids["cut"], "r7", SHA, RNG, repo="o/r", host="node-a")
    return mint


def _body(store: Store, ref_id: int) -> str:
    return release_view.render_release(store, ref_id, now=NOW).body


def _line(body: str, prefix: str) -> str:
    return next(ln for ln in body.splitlines() if ln.startswith(prefix))


def test_fresh_after_the_cut_polls_ci(store: Store) -> None:
    mint = _cut_pass(store)
    ids = mint.ids
    _status(store, ids["cut"], "done")
    _job(
        store,
        ids["cut"],
        "release_cut",
        "succeeded",
        finished_at=(NOW - timedelta(minutes=12)).isoformat(),
    )
    _job(
        store,
        ids["poll"],
        "ci_verdict_poll",
        "running",
        started_at=(NOW - timedelta(minutes=7)).isoformat(),
    )

    body = _body(store, mint.root_id)
    assert body.startswith(
        "release r7  branch release/r7  cut c0ffee111  head c0ffee111  "
        "cut host node-a\nstate: polling CI\n"
    )
    assert "CI: pending on c0ffee111" in body
    assert "head on main: unknown   on prod: no" in body
    assert "succeeded 12m ago" in _line(body, f"td{ids['cut']} ")
    assert "running 7m" in _line(body, f"td{ids['poll']} ")
    assert "held:" not in body
    # the approval ask exists but sits behind the CI gate
    assert f"td{ids['approval']}" in body.split("asks:")[1]
    assert "Reto" in body.split("asks:")[1]
    assert "answer: deploy r7 / hold" in body
    assert f"blocked by td{ids['gate']}" in body
    assert len(body.splitlines()) < 40


def test_held_deploy_names_the_stored_reason_and_the_review_ask(store: Store) -> None:
    mint = _cut_pass(store)
    ids = mint.ids
    for key in ("cut", "poll", "gate", "loop", "approval"):
        _status(store, ids[key], "done")
    R.record_ci_verdict(store, SHA, RNG, "green", "31", now=NOW)
    wait_ask = _ask(
        store,
        "engine review td900 on the td901 land will not resolve on its own",
        "release-review-wait:td901",
        options=["acknowledged", "false alarm"],
    )
    sha_ask = _ask(
        store,
        "engine land abcdef012345 has no reviewer",
        "release-review-wait:sha:abcdef012345",
        options=["acknowledged", "false alarm"],
    )
    reason = (
        "release_deploy not dispatched — "
        "release_deploy: r7 waits on its engine reviews — engine review td900 on "
        "the td901 land is open; the release waits — next: let td900 finish; "
        "engine land abcdef012345 has no reviewer; the release waits"
    )
    store.stamp_ref_meta(
        ids["deploy"],
        {"dispatch_hold": {"reason": reason, "since": NOW.isoformat()}},
    )

    body = _body(store, mint.root_id)
    assert "state: deploy HELD since" in body
    deploy_at = body.index(f"td{ids['deploy']} ")
    held = body[deploy_at:].split("\n")[1:3]
    assert held[0].startswith(
        "    held: release_deploy not dispatched — release_deploy: r7"
    )
    assert held[1] == (
        f"    review units waiting: td901 (ask td{wait_ask}), "
        f"sha:abcdef012345 (ask td{sha_ask})"
    )
    assert "CI: green on c0ffee111 (run 31)" in body
    asks = body.split("asks:")[1]
    for aid in (wait_ask, sha_ask):
        assert f"td{aid}" in asks
    assert asks.count("| Reto | answer: acknowledged / false alarm") == 2
    assert "approval" not in asks  # answered and closed: no longer an ask
    assert len(body.splitlines()) < 40


def test_a_held_node_with_nothing_stored_says_so(store: Store) -> None:
    mint = _cut_pass(store)
    _status(store, mint.ids["poll"], "blocked")
    body = _body(store, mint.root_id)
    assert f"td{mint.ids['poll']}" in body
    assert "held: no reason stored" in body


def test_a_blocked_nodes_comment_drops_the_plan_dispatch_prefix(
    store: Store,
) -> None:
    """Comments the dispatcher writes carry ``plan_dispatch:`` (and
    ``td<N>:``) prefixes; the held line renders the reason, not them."""
    mint = _cut_pass(store)
    ids = mint.ids
    _status(store, ids["poll"], "blocked")
    _comment(store, ids["poll"], "plan_dispatch: blocked — the gate failed")
    _status(store, ids["gate"], "blocked")
    _comment(
        store,
        ids["gate"],
        f"plan_dispatch: td{ids['gate']}: fix td55 is held waiting-for:brief",
    )

    body = _body(store, mint.root_id)
    assert "held: blocked — the gate failed" in body
    assert "held: fix td55 is held waiting-for:brief" in body
    assert not any("plan_dispatch" in ln for ln in body.splitlines())


def test_deploy_waits_on_an_answerable_approval_ask(store: Store) -> None:
    mint = _cut_pass(store)
    ids = mint.ids
    for key in ("cut", "poll", "gate", "loop"):
        _status(store, ids[key], "done")
    body = _body(store, mint.root_id)
    assert f"state: deploy HELD — waiting on ask td{ids['approval']} (Reto)" in body
    assert f"    held: waiting on ask td{ids['approval']} (Reto)" in body
    assert "not answerable yet" not in body


def test_a_refused_cut_shows_the_stored_refusal(store: Store) -> None:
    mint = T.mint_release_pass(store)
    note = "td2: release_cut held — release.cut_host is unset"
    store.stamp_ref_meta(mint.ids["cut"], {"cut_refusal_logged": note})
    body = _body(store, mint.root_id)
    assert f"    held: {note}" in body
    assert "state: cut HELD" in body
    assert body.splitlines()[0].startswith("release r?  branch -  cut -  head -")
    assert "CI: none on -" in body


def test_done_pass(store: Store) -> None:
    mint = _cut_pass(store)
    ids = mint.ids
    root_meta = store.get_ref(kind="todo", id=mint.root_id).meta  # type: ignore[union-attr]
    store.stamp_ref_meta(
        mint.root_id,
        {
            "release": {
                **root_meta["release"],
                "confirmed": {"sha": SHA, "at": "2026-11-19T17:50:00Z"},
                "merge_back": {"state": "merged"},
            }
        },
    )
    for node in ids.values():
        _status(store, node, "done")
    _job(
        store,
        ids["merge_back"],
        "release_confirm",
        "succeeded",
        finished_at=(NOW - timedelta(minutes=3)).isoformat(),
    )

    body = _body(store, mint.root_id)
    assert "state: done" in body
    assert "head on main: yes   on prod: yes" in body
    assert "asks: none" in body
    assert "3m ago" in _line(body, f"td{ids['merge_back']} ")


def test_a_fix_iteration_reads_in_pass_order(store: Store) -> None:
    mint = _cut_pass(store)
    ids = mint.ids
    for key in ("cut", "poll"):
        _status(store, ids[key], "done")
    body_nodes = T.mint_body(store, ids["loop"], "release-fix", iteration=2)
    digest = body_nodes.ids["digest"]
    body = _body(store, mint.root_id)
    assert "state: red CI: digest (iteration 2)" in body
    order = [
        body.index(f"td{ids['gate']} "),
        body.index(f"td{digest} "),
        body.index(f"td{ids['deploy']} "),
    ]
    assert order == sorted(order)
    assert "digest#2" in body


def test_the_head_says_when_fix_lands_are_past_it(store: Store) -> None:
    """The stored head moves only at the restamp: fix lands of an iteration
    whose restamp has not run are on the branch but not in the record."""
    mint = _cut_pass(store)
    ids = mint.ids
    for key in ("cut", "poll"):
        _status(store, ids[key], "done")
    loop = T.mint_body(store, ids["loop"], "release-fix", iteration=1).ids
    assert "recorded at the last restamp" not in _body(store, mint.root_id)
    _status(store, loop["digest"], "done")
    _status(store, loop["fix"], "done")
    head = _body(store, mint.root_id).splitlines()[0]
    assert "head c0ffee111 (recorded at the last restamp; 1 fix land since)" in head
    _status(store, loop["restamp"], "done")
    assert "recorded at the last restamp" not in _body(store, mint.root_id)


def test_a_node_inside_the_pass_resolves_to_the_root(store: Store) -> None:
    mint = _cut_pass(store)
    assert _body(store, mint.ids["deploy"]) == _body(store, mint.root_id)


def test_a_todo_outside_a_pass_is_refused(store: Store, hub: Hub) -> None:
    plain = int(store.insert_ref(kind="todo", slug=None, title="plain").id)
    with pytest.raises(BadInput) as exc:
        TodoHandler(hub=hub).get(id=plain, view="release")
    assert "not in a release pass" in str(exc.value)
    assert "view='tree'" in str(exc.value.next)


def test_the_handler_serves_the_view_and_lists_it_as_an_option(
    store: Store, hub: Hub
) -> None:
    mint = _cut_pass(store)
    out = TodoHandler(hub=hub).get(id=mint.root_id, view="release").body
    assert out.startswith("release r7  branch release/r7")
    with pytest.raises(Unsupported) as exc:
        TodoHandler(hub=hub).get(id=mint.root_id, view="nonsense")
    assert "release" in (exc.value.options or [])


# ── review fixes: what the screen must not get wrong ─────────────────


def test_a_deploy_whose_job_ended_is_not_read_as_undispatched(store: Store) -> None:
    mint = _cut_pass(store)
    ids = mint.ids
    for key in ("cut", "poll", "gate", "loop", "approval"):
        _status(store, ids[key], "done")
    R.mark_deploy_started(store, ids["deploy"])
    job = _job(store, ids["deploy"], "release_deploy", "failed")
    body = _body(store, mint.root_id)
    assert f"state: deploy job jo{job} failed; the deploy node is still open" in body
    assert "not dispatched yet" not in body
    assert "on prod: unknown" in body


def test_a_node_carrying_an_ask_tag_stays_a_node(store: Store) -> None:
    """An uncertain halt tags the node being worked; it must keep its
    line and carry the state, not move to ``asks:``."""
    mint = _cut_pass(store)
    ids = mint.ids
    _status(store, ids["cut"], "done")
    store.add_tag(
        ids["poll"], Tag.open("ask-user:uncertain-halt-outcome"), set_by="system"
    )
    body = _body(store, mint.root_id)
    assert _line(body, f"td{ids['poll']} ").split()[1] == "poll"
    assert "state: poll HELD — tagged ask-user:uncertain-halt-outcome" in body
    assert f"td{ids['poll']}" not in body.split("asks:")[1]


def test_the_ci_line_never_shows_an_earlier_heads_verdict(store: Store) -> None:
    mint = _cut_pass(store)
    ids = mint.ids
    _status(store, ids["cut"], "done")
    _status(store, ids["poll"], "done")
    _job(
        store,
        ids["poll"],
        "ci_verdict_poll",
        "succeeded",
        ci_verdict_poll={"state": "red", "sha": SHA, "run_id": "5"},
    )
    assert "CI: red on c0ffee111 (run 5)" in _body(store, mint.root_id)
    # A fix round moved the head: the cut poll's red is the earlier head's.
    head2 = "d00d" + "2" * 36
    R.record_head(store, ids["cut"], head2, now=NOW)
    T.mint_body(store, ids["loop"], "release-fix", iteration=1)
    body = _body(store, mint.root_id)
    assert "CI: pending on d00d22222 (earlier head: red on c0ffee111)" in body


def test_triage_routed_asks_still_list_with_who_answers(store: Store) -> None:
    mint = _cut_pass(store)
    ids = mint.ids
    for key in ("cut", "poll", "gate", "loop", "approval"):
        _status(store, ids[key], "done")
    routed = store.insert_ref(
        kind="todo",
        slug=None,
        title="which bounce scope?",
        parent_id=mint.root_id,
        meta={"ask": {"kind": "ruling", "options": ["full", "web"]}},
    )
    _status(store, int(routed.id), "open")
    for tag in ("waiting-for:coordinator", "asks-triage:routed"):
        store.add_tag(int(routed.id), Tag.open(tag), set_by="system")
    carry_title = "engine land td901 is cleared but its corrective commit is missing"
    wait_ask = _ask(
        store,
        carry_title,
        f"release-review-wait:td901:carry:r7:{'b' * 12}",
        "waiting-for:coordinator",
        "asks-triage:routed",
        options=["acknowledged", "false alarm"],
    )
    other_release = _ask(
        store,
        carry_title,
        f"release-review-wait:td901:carry:r8:{'b' * 12}",
        options=["acknowledged", "false alarm"],
    )
    reason = (
        "release_deploy not dispatched — "
        "engine land td901 is cleared on a corrective commit "
        f"({'b' * 12}) that is not on release/r7"
    )
    store.stamp_ref_meta(
        ids["deploy"],
        {"dispatch_hold": {"reason": reason, "since": NOW.isoformat()}},
    )

    body = _body(store, mint.root_id)
    asks = body.split("asks:")[1]
    assert (
        f"td{int(routed.id)} which bounce scope? | coordinator | answer: full / web"
        in asks
    )
    assert f"td{wait_ask} " in asks
    assert "| Reto (in the coordinator's queue; the answer is Reto's) |" in asks
    # another release's carry ask of the same unit is not this pass's
    assert f"td{other_release}" not in body
    assert f"review units waiting: td901 (ask td{wait_ask})" in body


def test_the_merge_back_resolver_is_not_called_a_red_ci_fix(store: Store) -> None:
    mint = _cut_pass(store)
    for node in mint.ids.values():
        if node != mint.root_id:  # the pass stays open while it resolves
            _status(store, node, "done")
    resolver = store.insert_ref(
        kind="todo",
        slug=None,
        title="Resolve the merge of release/r7 back into main",
        parent_id=mint.root_id,
        meta={"op": "run_slice", "merge_back_resolver": "r7"},
    )
    _status(store, int(resolver.id), "open")
    body = _body(store, mint.root_id)
    assert "state: resolving the merge-back conflict" in body
    assert _line(body, f"td{int(resolver.id)} ").split()[1] == "resolve"


def test_a_retired_pass_root_is_refused(store: Store) -> None:
    mint = _cut_pass(store)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET retired_at = now() WHERE ref_id = %s", (mint.root_id,)
        )
    with pytest.raises(BadInput) as exc:
        release_view.render_release(store, mint.ids["deploy"], now=NOW)
    assert "is retired" in str(exc.value)
