"""factory_rescue — the rescue tag and one gripe per failure signature
(dark-factory td574946, slice S1): every factory failure ends in done or
rescue; a node in rescue carries ``rescue:<class>`` and joins the ONE
live gripe tagged ``rescue-sig:<hash>``."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from precis import settings as psettings
from precis.errors import BadInput
from precis.store import Store
from precis.store.types import Tag
from precis.workers import factory_rescue as fr
from precis.workers import gripe_dispatch as gd
from tests._refs import insert_gripe


def _todo(store: Store, title: str = "a failing slice") -> int:
    return int(store.insert_ref(kind="todo", slug=None, title=title).id)


def _ev(**kw: Any) -> fr.RescueEvidence:
    base: dict[str, Any] = {
        "step": "run_slice",
        "error_line": "boom",
        "log_tail": "boom",
    }
    base.update(kw)
    return fr.RescueEvidence(**base)


def _open_tags(store: Store, ref_id: int) -> list[str]:
    return [str(t.value) for t in store.tags_for(ref_id) if t.namespace == "open"]


def _comments(store: Store, ref_id: int, kind: str) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = %s "
            "ORDER BY ord",
            (ref_id, kind),
        ).fetchall()
    return [str(r[0]) for r in rows]


def _sig_gripes(store: Store) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id FROM refs r "
            "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE r.kind = 'gripe' AND r.retired_at IS NULL "
            "AND t.namespace = 'OPEN' AND t.value LIKE 'rescue-sig:%' "
            "ORDER BY r.ref_id",
        ).fetchall()
    return [int(r[0]) for r in rows]


def _links(store: Store, src: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT dst_ref_id FROM links WHERE src_ref_id = %s "
            "AND relation = 'related-to' ORDER BY dst_ref_id",
            (src,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def test_forty_nodes_one_signature_make_one_gripe_and_forty_links(
    store: Store,
) -> None:
    nodes = [_todo(store, f"slice {i}") for i in range(40)]
    results = [fr.enter_rescue(store, n, "builder", _ev()) for n in nodes]

    [gripe] = _sig_gripes(store)
    assert all(r.gripe_id == gripe for r in results)
    assert sum(r.gripe_created for r in results) == 1
    assert all(r.tagged and not r.skipped for r in results)
    for n in nodes:
        assert _open_tags(store, n).count("rescue:builder") == 1
        assert _links(store, n) == [gripe]
    ref = store.get_ref(kind="gripe", id=gripe)
    assert ref is not None and ref.retired_at is None
    # A rescue gripe never reaches a build lane by the dispatch door:
    # no fix:code tag and no Claude-Code / human filed_by (worker-filed).
    assert "fix:code" not in _open_tags(store, gripe)
    filed_by = (ref.meta or {}).get("filed_by") or {}
    assert not str(filed_by.get("client") or "").startswith("claude-code/")
    assert not str(filed_by.get("actor") or "").startswith("user:")
    # the second node onward leaves one gripe_comment naming it
    assert len(_comments(store, gripe, "gripe_comment")) == 39
    # every node carries its own evidence comment
    assert all(len(_comments(store, n, "todo_comment")) == 1 for n in nodes)


def test_second_call_on_a_tagged_node_writes_nothing(store: Store) -> None:
    n = _todo(store)
    first = fr.enter_rescue(store, n, "builder", _ev())
    second = fr.enter_rescue(store, n, "builder", _ev())

    assert first.tagged and not first.skipped
    assert not second.tagged and second.skipped
    assert _open_tags(store, n).count("rescue:builder") == 1
    assert len(_sig_gripes(store)) == 1
    assert len(_comments(store, n, "todo_comment")) == 1
    # the first call already created the gripe, so no follower note either
    assert _comments(store, first.gripe_id or 0, "gripe_comment") == []


def test_kill_switch_writes_nothing(store: Store) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config (host, service, prio) "
            "VALUES ('*', 'plan_dispatch', 0)"
        )
    n = _todo(store)
    res = fr.enter_rescue(store, n, "builder", _ev())

    assert res.skipped and not res.tagged and res.gripe_id is None
    assert "rescue:builder" not in _open_tags(store, n)
    assert _sig_gripes(store) == []
    assert _comments(store, n, "todo_comment") == []


def test_engine_class_gripe_carries_cluster_dark_factory(store: Store) -> None:
    res = fr.enter_rescue(store, _todo(store, "stalled slice"), "stalled", _ev())
    assert res.gripe_id is not None
    assert "cluster:dark-factory" in _open_tags(store, res.gripe_id)


def test_non_engine_class_does_not(store: Store) -> None:
    res = fr.enter_rescue(store, _todo(store), "builder", _ev())
    assert res.gripe_id is not None
    assert "cluster:dark-factory" not in _open_tags(store, res.gripe_id)


def test_plan_root_is_tagged_but_not_linked(store: Store) -> None:
    root = _todo(store, "plan root")
    store.add_tag(root, Tag.open(f"plan:{root}"), set_by="system")

    res = fr.enter_rescue(store, root, "builder", _ev())

    assert res.tagged and res.gripe_id is not None
    assert "rescue:builder" in _open_tags(store, root)
    # a plan root's lowest related-to gripe is the land's resolved: target
    # (land_slice._plan_gripe) — a rescue link would misfile it
    assert _links(store, root) == []
    assert _comments(store, root, "todo_comment")
    # the unlinked signature gripe still never dispatches under the
    # ``filer`` trust rule: backdated and fix:code-tagged, the missing
    # filed_by keeps it out of candidates (``all``, the default since
    # 2026-11-10, skips that check on purpose)
    psettings.set_setting(gd.TRUST_KEY, "filer", store=store)
    store.add_tag(res.gripe_id, Tag.open("fix:code"), set_by="agent")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '2 hours' WHERE ref_id = %s",
            (res.gripe_id,),
        )
        conn.execute(
            "UPDATE ref_tags rt SET created_at = now() - interval '2 hours' "
            "FROM tags t WHERE rt.tag_id = t.tag_id AND rt.ref_id = %s "
            "AND t.namespace = 'OPEN' AND t.value = 'fix:code'",
            (res.gripe_id,),
        )
    try:
        assert res.gripe_id not in {c.gripe_id for c in gd.candidates(store, 50)}
    finally:
        psettings.invalidate(gd.TRUST_KEY)


def test_evidence_comment_has_branch_head_sha_and_40_line_cap(
    store: Store,
) -> None:
    tail = "\n".join(f"line {i}" for i in range(1, 61))
    n = _todo(store)
    res = fr.enter_rescue(
        store,
        n,
        "builder",
        _ev(
            job="jo123456",
            branch="slice-td999999",
            head_sha="a84e0a0d0",
            diff_stat=" src/x.py | 2 +-",
            log_tail=tail,
        ),
    )

    assert res.tagged
    [comment] = _comments(store, n, "todo_comment")
    assert comment.startswith("rescue: builder")
    assert "jo123456" in comment
    assert "slice-td999999" in comment
    assert "a84e0a0d0" in comment
    assert "src/x.py" in comment
    assert "line 60" in comment
    assert "line 20" not in comment


def test_closed_signature_gripe_is_not_reused(store: Store) -> None:
    first = fr.enter_rescue(store, _todo(store, "a"), "builder", _ev())
    assert first.gripe_id is not None
    store.add_tag(
        first.gripe_id,
        Tag.closed("STATUS", "done"),
        set_by="system",
        replace_prefix=True,
    )

    second = fr.enter_rescue(store, _todo(store, "b"), "builder", _ev())

    assert second.gripe_created
    assert second.gripe_id is not None and second.gripe_id != first.gripe_id
    assert len(_sig_gripes(store)) == 2


def test_unknown_class_refuses_with_options(store: Store) -> None:
    n = _todo(store)
    with pytest.raises(BadInput) as exc:
        fr.enter_rescue(store, n, "bogus-class", _ev())
    assert exc.value.options
    assert "builder" in exc.value.options
    assert "rescue:bogus-class" not in _open_tags(store, n)


def test_rescued_gripe_is_its_own_signature_gripe(store: Store) -> None:
    g = int(insert_gripe(store, "a gripe that cannot mint").id)
    res = fr.enter_rescue(store, g, "intake", _ev())

    assert res.gripe_id == g and not res.gripe_created
    assert "rescue:intake" in _open_tags(store, g)
    # no self-link, one gripe_comment evidence note on the gripe itself
    assert _links(store, g) == []
    [comment] = _comments(store, g, "gripe_comment")
    assert comment.startswith("rescue: intake")
    assert _sig_gripes(store) == []


def test_a_second_rescue_class_on_one_node_is_a_noop(store: Store) -> None:
    n = _todo(store)
    fr.enter_rescue(store, n, "builder", _ev())
    res = fr.enter_rescue(store, n, "review", _ev())
    assert res.skipped and not res.tagged
    assert "rescue:review" not in _open_tags(store, n)


def test_concurrent_callers_on_one_signature_file_one_gripe(
    store: Store,
) -> None:
    nodes = [_todo(store, f"raced {i}") for i in range(8)]
    barrier = threading.Barrier(len(nodes))
    results: list[fr.RescueResult] = []

    def work(n: int) -> None:
        barrier.wait()
        results.append(fr.enter_rescue(store, n, "builder", _ev()))

    threads = [threading.Thread(target=work, args=(n,)) for n in nodes]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    assert len(_sig_gripes(store)) == 1
    assert sum(r.gripe_created for r in results) == 1


def test_concurrent_calls_on_one_node_rescue_once(store: Store) -> None:
    n = _todo(store, "raced node")
    barrier = threading.Barrier(2)
    results: list[fr.RescueResult] = []

    def work(cls: str) -> None:
        barrier.wait()
        results.append(fr.enter_rescue(store, n, cls, _ev()))

    # Two different classes race the same node: whoever loses the
    # advisory lock sees the winner's rescue: tag and writes nothing.
    threads = [
        threading.Thread(target=work, args=(cls,)) for cls in ("builder", "review")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    assert sum(r.tagged for r in results) == 1
    assert sum(bool(r.skipped) for r in results) == 1
    rescue_tags = [t for t in _open_tags(store, n) if t.startswith("rescue:")]
    assert len(rescue_tags) == 1
    assert len(_sig_gripes(store)) == 1
    assert len(_links(store, n)) == 1
    assert len(_comments(store, n, "todo_comment")) == 1
