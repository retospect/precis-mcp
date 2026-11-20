"""``release_fix_handoff`` on a real store: the gripe table's commit-time
STATUS trigger (``gripe_status_check``), the link, tag and meta rows and the
plan root's dedupe record are real rows here, not the fakes of
``test_run_slice``."""

from __future__ import annotations

from typing import Any

import pytest

from precis.plans import release as _release
from precis.store import Store
from precis.store.types import Tag
from precis.workers import release_fix_handoff as rfh
from precis.workers.job_types.run_slice import _append_comment

pytestmark = pytest.mark.db

_G1 = "the lander ran ruff on the squash and reported a stale cache hit"
_G2 = "land lint flagged tests/test_x.py as changed but it was untouched"
_UNFIXED = "tests/test_a.py::test_b — needs the db container the lane lacks"
_HANDOFF = f"I stopped.\ngripe: {_G1}\ngripe: {_G2}\nunfixed: {_UNFIXED}\n"


def _world(store: Store) -> tuple[int, int]:
    """A plan root and a release-fix todo under it; ``(root, fix)``."""
    root = store.insert_ref(kind="todo", slug=None, title="Release pass", meta={})
    fix = store.insert_ref(
        kind="todo",
        slug=None,
        title="Fix the failures",
        meta={_release.LAND_TARGET_KEY: "release/r7"},
    )
    store.add_tag(root.id, Tag.open("release-pass"), set_by="system")
    store.add_tag(fix.id, Tag.open(f"plan:{root.id}"), set_by="system")
    return int(root.id), int(fix.id)


def _file(store: Store, fix: int, job: int, handoff: str = _HANDOFF) -> list[int]:
    return rfh.file_markers(
        store,
        todo_id=fix,
        job_id=job,
        handoff=handoff,
        append_comment=_append_comment,
    )


def _comments(store: Store, ref_id: int) -> list[str]:
    return [
        c.text
        for c in store.chunks.list_chunks_for_ref(ref_id)
        if c.chunk_kind == "todo_comment"
    ]


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    ref = store.get_ref(kind="todo", id=ref_id)
    assert ref is not None
    return dict(ref.meta or {})


def test_files_the_handoff_lines_on_a_real_store(store: Store) -> None:
    root, fix = _world(store)
    filed = _file(store, fix, job=900)
    assert len(filed) == 2

    for gid, text in zip(filed, (_G1, _G2), strict=True):
        ref = store.get_ref(kind="gripe", id=gid)
        assert ref is not None and ref.title == f"[release-fix] {text}"
        status = [str(t.value) for t in store.tags_for(gid) if t.prefix == "STATUS"]
        assert status == ["open"]
        opens = {str(t.value) for t in store.tags_for(gid) if t.prefix != "STATUS"}
        assert opens == {"origin:release-fix-handoff", "cluster:dark-factory"}
        assert ref.meta[rfh.GRIPE_META_KEY] == {
            "digest": rfh._digest(text),
            "plan": root,
            "todo": fix,
            "job": 900,
        }
        (body,) = [
            c.text
            for c in store.chunks.list_chunks_for_ref(gid)
            if c.chunk_kind == "gripe_body"
        ]
        assert body == (
            f"[release-fix] {text}\n\nFiled by the dispatcher from the hand-off "
            f"of td{fix} (release r7, job jo900); the fixer session cannot "
            "write to the graph."
        )

    links = store.links_for(fix, direction="out", relation="related-to")
    assert sorted(int(lk.dst_ref_id) for lk in links) == sorted(filed)
    assert {int(lk.src_ref_id) for lk in links} == {fix}

    state = _meta(store, root)[rfh.STATE_KEY]
    assert state == {
        "gripes": {
            rfh._digest(_G1): f"gr{filed[0]}",
            rfh._digest(_G2): f"gr{filed[1]}",
        },
        "jobs": [900],
    }
    assert _comments(store, root) == [f"release-fix td{fix} left red:\n- {_UNFIXED}"]
    assert _comments(store, fix) == [
        f"filed from this hand-off's gripe: lines: gr{filed[0]}, gr{filed[1]}"
    ]


def test_a_repolled_job_and_a_repeated_line_file_nothing_more(store: Store) -> None:
    root, fix = _world(store)
    first = _file(store, fix, job=900)
    # The same job again: nothing new anywhere.
    assert _file(store, fix, job=900) == []
    assert len(_comments(store, root)) == 1 and len(_comments(store, fix)) == 1
    # A later attempt repeating one line (other case, spacing) and adding one.
    again = (
        f"GRIPE:  {_G1.upper()}\n"
        "gripe: the digest named a test that no longer exists in the tree\n"
    )
    second = _file(store, fix, job=901, handoff=again)
    assert len(second) == 1 and second[0] not in first
    state = _meta(store, root)[rfh.STATE_KEY]
    assert sorted(state["gripes"].values()) == sorted(
        f"gr{g}" for g in [*first, *second]
    )
    assert state["jobs"] == [900, 901]
    assert _comments(store, fix)[-1] == (
        f"filed from this hand-off's gripe: lines: gr{second[0]}"
    )
    assert len(_comments(store, root)) == 1  # no unfixed line the second time
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'gripe' AND retired_at IS NULL"
        ).fetchone()
    assert row is not None and row[0] == 3
