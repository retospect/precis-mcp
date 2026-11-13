"""gr658432: ``put(idem_key=...)`` dedupes a blind retry.

A stalled put that committed looks identical to a dropped one, so a
blind retry risks double-filing. A caller-supplied ``idem_key`` stamps
``meta.idem_key`` on the minted ref (or the appended comment chunk); a
same-key retry under the advisory lock returns the first write instead
of a duplicate. Needs postgres — the ``store`` fixtures skip without it.
"""

from __future__ import annotations

import threading
import time

import pytest

from precis.dispatch import Hub
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.utils.idem import idem_lock_key
from tests.conftest import id_of


def _store(rt: PrecisRuntime) -> Store:
    store = rt.hub.store
    assert store is not None
    return store


def test_todo_create_idem_key_dedupes(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    args = {"kind": "todo", "text": "idem probe todo", "idem_key": "s:1"}
    first = rt.dispatch("put", args)
    second = rt.dispatch("put", dict(args))
    new_id = id_of(first)
    assert "created todo" in first
    assert "existing todo" in second and str(new_id) in second
    # The stored row carries the key, and exactly one ref was minted.
    ref = _store(rt).get_ref(kind="todo", id=new_id)
    assert ref is not None and ref.meta.get("idem_key") == "s:1"


def test_different_idem_keys_mint_different_refs(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    a = id_of(rt.dispatch("put", {"kind": "todo", "text": "a", "idem_key": "k:a"}))
    b = id_of(rt.dispatch("put", {"kind": "todo", "text": "b", "idem_key": "k:b"}))
    assert a != b


def test_idem_key_dedupes_across_kinds_independently(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The key namespaces per kind — a gripe key can't hit a todo."""
    rt = runtime_with_store
    t = id_of(rt.dispatch("put", {"kind": "todo", "text": "t", "idem_key": "x"}))
    g = id_of(rt.dispatch("put", {"kind": "gripe", "text": "g", "idem_key": "x"}))
    assert t != g
    again = rt.dispatch("put", {"kind": "gripe", "text": "g", "idem_key": "x"})
    assert "existing gripe" in again and str(g) in again


def test_todo_comment_idem_key_dedupes(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    tid = id_of(rt.dispatch("put", {"kind": "todo", "text": "comment host"}))
    args = {"kind": "todo", "id": tid, "text": "a comment", "idem_key": "c:1"}
    first = rt.dispatch("put", args)
    second = rt.dispatch("put", dict(args))
    assert "appended comment 1" in first
    assert "existing comment 1" in second and "duplicate" in second
    chunks = _store(rt).chunks.list_chunks_for_ref(tid)
    keyed = [c for c in chunks if c.meta.get("idem_key") == "c:1"]
    assert len(keyed) == 1 and keyed[0].text == "a comment"


def test_gripe_comment_idem_key_dedupes(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    gid = id_of(rt.dispatch("put", {"kind": "gripe", "text": "comment host"}))
    args = {"kind": "gripe", "id": gid, "text": "a comment", "idem_key": "c:1"}
    rt.dispatch("put", args)
    second = rt.dispatch("put", dict(args))
    assert "existing comment" in second
    chunks = _store(rt).chunks.list_chunks_for_ref(gid)
    keyed = [c for c in chunks if c.meta.get("idem_key") == "c:1"]
    assert len(keyed) == 1


def test_memory_create_idem_key_dedupes(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    args = {
        "kind": "memory",
        "text": "a note on retry safety",
        "idem_key": "m:1",
        "tags": ["SPACE:scratch"],
    }
    first = rt.dispatch("put", args)
    second = rt.dispatch("put", dict(args))
    new_id = id_of(first)
    assert "existing memory" in second and str(new_id) in second
    ref = _store(rt).get_ref(kind="memory", id=new_id)
    assert ref is not None and ref.meta.get("idem_key") == "m:1"


@pytest.mark.parametrize("bad", [123, "", "   ", "k" * 201])
def test_idem_key_rejects_malformed(
    runtime_with_store: PrecisRuntime, bad: object
) -> None:
    rt = runtime_with_store
    out = rt.dispatch("put", {"kind": "todo", "text": "x", "idem_key": bad})
    assert "[error:" in out
    assert "created todo" not in out


def test_taxon_create_idem_key_dedupes(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``_mint_checks`` must not refuse the retry's own mint as a
    duplicate — the key lookup answers first (td659660 review)."""
    rt = runtime_with_store
    args = {
        "kind": "taxon",
        "text": "zzidem probe term — a probe node for retry tests",
        "idem_key": "t:1",
    }
    first = rt.dispatch("put", args)
    second = rt.dispatch("put", dict(args))
    new_id = id_of(first)
    assert "created taxon" in first
    assert "existing taxon" in second and str(new_id) in second


def test_plan_mode_idem_key_dedupes_on_root(
    runtime_with_store: PrecisRuntime,
) -> None:
    """A keyed ``mode='plan'`` retry returns the subtree root instead of
    minting a second subtree (td659660 review)."""
    rt = runtime_with_store
    args = {
        "kind": "todo",
        "mode": "plan",
        "template": "release-pass",
        "idem_key": "p:1",
    }
    first = rt.dispatch("put", args)
    second = rt.dispatch("put", dict(args))
    root_id = id_of(first)
    assert "minted plan" in first
    assert "existing todo" in second and str(root_id) in second
    ref = _store(rt).get_ref(kind="todo", id=root_id)
    assert ref is not None and ref.meta.get("idem_key") == "p:1"


def test_keyed_ask_retry_does_not_relog_plan_event(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reused put must not re-fire the ask's quest-logbook event
    (td659660 review)."""
    rt = runtime_with_store
    calls = {"n": 0}

    def spy(*_a: object, **_k: object) -> None:
        calls["n"] += 1

    monkeypatch.setattr("precis.handlers.todo.plan_log.log_plan_event", spy)
    args = {
        "kind": "todo",
        "text": "which colour for the shed?",
        "tags": ["ask-user"],
        "meta": {"ask": {"kind": "ruling", "blocks": "nothing"}},
        "idem_key": "a:1",
    }
    rt.dispatch("put", args)
    rt.dispatch("put", dict(args))
    assert calls["n"] == 1


def test_gripe_keeper_retry_returns_own_mint(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry of a filing that minted the theme keeper must not append
    a '+1' comment onto its own ref (td659660 review)."""
    rt = runtime_with_store
    monkeypatch.setattr(
        "precis.handlers.gripe.match_themes", lambda _t: ["zz-idem-probe"]
    )
    args = {"kind": "gripe", "text": "the probe filing", "idem_key": "k:1"}
    first = rt.dispatch("put", args)
    second = rt.dispatch("put", dict(args))
    gid = id_of(first)
    assert "created gripe" in first
    assert "existing gripe" in second and str(gid) in second
    chunks = _store(rt).chunks.list_chunks_for_ref(gid)
    assert not any(c.text.startswith("+1") for c in chunks)


def test_gripe_merge_retry_dedupes_the_comment(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry of a filing that merged into a pre-existing keeper
    dedupes the '+1' comment chunk."""
    rt = runtime_with_store
    monkeypatch.setattr(
        "precis.handlers.gripe.match_themes", lambda _t: ["zz-idem-probe"]
    )
    keeper = id_of(
        rt.dispatch(
            "put",
            {"kind": "gripe", "text": "first", "tags": ["theme:zz-idem-probe"]},
        )
    )
    args = {"kind": "gripe", "text": "same again", "idem_key": "m:1"}
    rt.dispatch("put", args)
    second = rt.dispatch("put", dict(args))
    assert "existing +1 comment" in second
    chunks = _store(rt).chunks.list_chunks_for_ref(keeper)
    keyed = [c for c in chunks if (c.meta or {}).get("idem_key") == "m:1"]
    assert len(keyed) == 1


def test_gripe_keeper_retry_with_prio_returns_own_mint(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keyed retry resolves before theme routing, so prio= on it does
    not trip the merge refusal (td659660 review)."""
    rt = runtime_with_store
    monkeypatch.setattr(
        "precis.handlers.gripe.match_themes", lambda _t: ["zz-idem-probe"]
    )
    args = {"kind": "gripe", "text": "the probe filing", "idem_key": "kp:1", "prio": 3}
    first = rt.dispatch("put", args)
    second = rt.dispatch("put", dict(args))
    assert "created gripe" in first
    assert "existing gripe" in second and str(id_of(first)) in second


def test_gripe_merge_retry_after_keeper_closed_does_not_refile(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merged filing's retry returns the earlier merge even when its
    keeper has since closed — no new keeper, no second comment."""
    rt = runtime_with_store
    monkeypatch.setattr(
        "precis.handlers.gripe.match_themes", lambda _t: ["zz-idem-probe"]
    )
    keeper = id_of(
        rt.dispatch(
            "put",
            {"kind": "gripe", "text": "first", "tags": ["theme:zz-idem-probe"]},
        )
    )
    args = {"kind": "gripe", "text": "same again", "idem_key": "mc:1"}
    rt.dispatch("put", args)
    rt.dispatch("tag", {"kind": "gripe", "id": keeper, "add": ["STATUS:wontfix"]})
    second = rt.dispatch("put", dict(args))
    assert "existing" in second
    store = _store(rt)
    keyed = [
        c
        for c in store.chunks.list_chunks_for_ref(keeper)
        if (c.meta or {}).get("idem_key") == "mc:1"
    ]
    assert len(keyed) == 1
    assert len(store.list_refs(kind="gripe", limit=50)) == 1


def _advisory_waiter(store: Store, idem: str) -> bool:
    """True while a session waits on ``pg_advisory_xact_lock`` for the
    ``f"{kind}:{key}"`` string — the same-key retry queued behind the
    lock the parked filing holds. Scoped to the exact key: parallel
    tests' lock waits share the same ``pg_locks``."""
    key64 = idem_lock_key(idem) & 0xFFFFFFFFFFFFFFFF
    with store.tx() as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
            "AND NOT granted AND objsubid = 1 AND classid = %s AND objid = %s "
            "LIMIT 1",
            (key64 >> 32, key64 & 0xFFFFFFFF),
        ).fetchone()
    return row is not None


def _wait_for_b(store: Store, checked: threading.Event, idem: str) -> None:
    """Park filing A until filing B is measurably in the race.

    Pre-fix, B's replay lookup runs unlocked and it signals ``checked``
    from the routing gate right after; under the fix B instead blocks on
    ``pg_advisory_xact_lock``, which shows as an ungranted advisory lock
    in ``pg_locks``. Either observation means B's lookup has landed (or
    is about to be answered by A's commit) inside A's routing window.
    """
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if checked.is_set() or _advisory_waiter(store, idem):
            return
        time.sleep(0.05)


def test_keyed_filings_serialise_across_keeper_mint(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent same-key filings racing a keeper mint: the loser
    blocks on the (kind, key) lock until the winner commits, then
    replays — it must not re-route into the merge path's prio= refusal
    (td659660 review).

    ``mint_gate`` parks filing A at its mint decision so filing B's
    replay lookup lands in the window the pre-fix code left unlocked.
    """
    rt = runtime_with_store
    store = _store(rt)
    a_ready = threading.Event()
    b_checked = threading.Event()
    a_committed = threading.Event()

    def match_gate(_text: str) -> list[str]:
        if threading.current_thread().name == "filing-b":
            # B's replay lookup answered nothing and it is about to
            # route — hold it in the pre-fix gap until A commits.
            b_checked.set()
            a_committed.wait(timeout=30)
        return ["zz-idem-probe"]

    def mint_gate(_text: str, _slug: str) -> bool:
        a_ready.set()
        _wait_for_b(store, b_checked, "gripe:cc:1")
        return True

    monkeypatch.setattr("precis.handlers.gripe.match_themes", match_gate)
    monkeypatch.setattr("precis.handlers.gripe.keeper_mint_match", mint_gate)

    args = {"kind": "gripe", "text": "the probe filing", "idem_key": "cc:1", "prio": 3}
    results: dict[str, str] = {}

    def run(name: str) -> None:
        results[name] = rt.dispatch("put", dict(args))

    a = threading.Thread(target=run, args=("a",), name="filing-a", daemon=True)
    a.start()
    assert a_ready.wait(timeout=30)
    b = threading.Thread(target=run, args=("b",), name="filing-b", daemon=True)
    b.start()
    a.join(timeout=60)
    a_committed.set()
    b.join(timeout=60)
    assert "created gripe" in results["a"]
    assert "existing gripe" in results["b"]
    assert str(id_of(results["a"])) in results["b"]
    assert len(store.list_refs(kind="gripe", limit=50)) == 1


def test_keyed_merge_serialises_before_keeper_close_reroutes(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-key retry blocked behind a keyed merge finds the merge
    comment once the lock releases — even when the keeper has since
    closed — instead of falling through to the create path's ref-only
    lookup and minting a duplicate (td659660 review).

    Same gate shape as the mint race: A parks at ``match_themes`` so B's
    replay lookup lands in the pre-fix gap.
    """
    rt = runtime_with_store
    store = _store(rt)
    a_ready = threading.Event()
    b_checked = threading.Event()
    gate_open = threading.Event()

    def match_gate(_text: str) -> list[str]:
        name = threading.current_thread().name
        if name == "filing-a":
            a_ready.set()
            _wait_for_b(store, b_checked, "gripe:cc:2")
        elif name == "filing-b":
            b_checked.set()
            gate_open.wait(timeout=30)
        return ["zz-idem-probe"]

    monkeypatch.setattr("precis.handlers.gripe.match_themes", match_gate)
    keeper = id_of(
        rt.dispatch(
            "put",
            {"kind": "gripe", "text": "first", "tags": ["theme:zz-idem-probe"]},
        )
    )
    args = {"kind": "gripe", "text": "same again", "idem_key": "cc:2"}
    results: dict[str, str] = {}

    def run(name: str) -> None:
        results[name] = rt.dispatch("put", dict(args))

    a = threading.Thread(target=run, args=("a",), name="filing-a", daemon=True)
    a.start()
    assert a_ready.wait(timeout=30)
    b = threading.Thread(target=run, args=("b",), name="filing-b", daemon=True)
    b.start()
    a.join(timeout=60)
    # The merge has committed; close the keeper (comment+close in one
    # put — the close gate needs a newest resolved: comment) so a
    # re-routed retry finds no live keeper and falls through to create.
    closed = rt.dispatch(
        "put",
        {
            "kind": "gripe",
            "id": keeper,
            "text": "resolved: swept",
            "tags": ["STATUS:wontfix"],
        },
    )
    assert "error" not in closed.lower()
    gate_open.set()
    b.join(timeout=60)
    assert "existing +1 comment" in results["b"]
    keyed = [
        c
        for c in store.chunks.list_chunks_for_ref(keeper)
        if (c.meta or {}).get("idem_key") == "cc:2"
    ]
    assert len(keyed) == 1
    assert len(store.list_refs(kind="gripe", limit=50)) == 1


def test_memory_thread_guard_runs_once_on_keyed_retry(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dream-thread dup/cap guard must run inside the keyed scope —
    on a retry the key answers first (td659660 review class)."""
    rt = runtime_with_store
    calls = {"n": 0}

    def spy(_self: object, _body: str) -> None:
        calls["n"] += 1

    monkeypatch.setattr(
        "precis.handlers.memory.MemoryHandler._guard_thread_create", spy
    )
    args = {
        "kind": "memory",
        "text": "a thread note",
        "tags": ["dream-thread"],
        "idem_key": "th:1",
    }
    rt.dispatch("put", args)
    rt.dispatch("put", dict(args))
    assert calls["n"] == 1


def test_memory_thread_guard_runs_once_on_unkeyed_create(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unkeyed thread create guards early (before the mutating scope);
    ``create()`` must not re-run the embed/dup check (td659660 review
    finding: the unguarded ``if guard_thread:`` called it twice)."""
    rt = runtime_with_store
    calls = {"n": 0}

    def spy(_self: object, _body: str) -> None:
        calls["n"] += 1

    monkeypatch.setattr(
        "precis.handlers.memory.MemoryHandler._guard_thread_create", spy
    )
    rt.dispatch(
        "put",
        {"kind": "memory", "text": "a thread note", "tags": ["dream-thread"]},
    )
    assert calls["n"] == 1


def test_memory_thread_guard_runs_once_on_dry_run(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry-run returns before ``create()``, so its single guard call is
    the early one."""
    rt = runtime_with_store
    calls = {"n": 0}

    def spy(_self: object, _body: str) -> None:
        calls["n"] += 1

    monkeypatch.setattr(
        "precis.handlers.memory.MemoryHandler._guard_thread_create", spy
    )
    rt.dispatch(
        "put",
        {
            "kind": "memory",
            "text": "a thread note",
            "tags": ["dream-thread"],
            "dry_run": True,
        },
    )
    assert calls["n"] == 1


def test_quest_log_append_idem_key_dedupes(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    qid = id_of(rt.dispatch("put", {"kind": "quest", "text": "quest host"}))
    args = {"kind": "quest", "id": qid, "text": "a logbook note", "idem_key": "q:1"}
    rt.dispatch("put", args)
    second = rt.dispatch("put", dict(args))
    assert "existing logbook entry" in second
    chunks = _store(rt).chunks.list_chunks_for_ref(qid)
    keyed = [c for c in chunks if (c.meta or {}).get("idem_key") == "q:1"]
    assert len(keyed) == 1


def test_keyed_filing_files_and_dedupes_under_a_no_update_role(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The keyed filing's post-hoc ``update_ref`` stamp needs UPDATE on
    ``refs`` — a role-constrained (``write:none``/``agent_ro``-class)
    keyed filing rolled back entirely and wrote nothing (gr658432,
    td659660 review). The stamp now rides into ``file_gripe_readonly``
    (migration 0319) inside the definer; ``_run_idempotent`` skips the
    UPDATE the role can't run.

    ``pg_monitor`` (no precis grants) stands in for ``agent_ro`` through
    the real tier-2 mechanism — ``PRECIS_MCP_DB_ROLE_ENFORCE`` +
    ``PRECIS_MCP_DB_ROLE`` make every pool checkout ``SET LOCAL ROLE``
    (``test_pool_db_role.py``'s precedent). The grants probe then routes
    the put down the read-scoped channel for real."""
    from precis.handlers.gripe import GripeHandler

    store = hub.store
    assert store is not None
    # pg_monitor reads nothing by default; the keyed path's dedupe
    # SELECTs (and the definer's session-role triggers) need the same
    # SELECT grant the prod agent_ro holds.
    with store.pool.connection() as conn:
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO pg_monitor")
        conn.commit()
    monkeypatch.setenv("PRECIS_MCP_DB_ROLE_ENFORCE", "1")
    monkeypatch.setenv("PRECIS_MCP_DB_ROLE", "pg_monitor")
    try:
        h = GripeHandler(hub=hub)
        text = "the put stalled; a blind retry must not double-file"
        first = h.put(text=text, idem_key="ro:1")
        second = h.put(text=text, idem_key="ro:1")
    finally:
        monkeypatch.delenv("PRECIS_MCP_DB_ROLE_ENFORCE", raising=False)
        monkeypatch.delenv("PRECIS_MCP_DB_ROLE", raising=False)
        with store.pool.connection() as conn:
            conn.execute("REVOKE SELECT ON ALL TABLES IN SCHEMA public FROM pg_monitor")
            conn.commit()
    gid = id_of(first.body)
    assert "created gripe" in first.body
    assert "existing gripe" in second.body and str(gid) in second.body
    ref = store.get_ref(kind="gripe", id=gid)
    assert ref is not None and ref.meta.get("idem_key") == "ro:1"
    assert "source:agent_ro" in {str(t) for t in store.tags_for(gid)}
    assert len(store.list_refs(kind="gripe", limit=50)) == 1
