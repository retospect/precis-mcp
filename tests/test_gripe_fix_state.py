"""gr519361: the gripe-fix loop's state lives on the gripe.

``edit(kind='gripe', mode='fix')`` writes the batch tag, ``meta.fix``
(host/session/tree/landed) and the brief/handoff/review chunks; the
queue is derived (``_gripe_fix.FixState.queue``); ``/queue`` and
``/batch`` are the list views; the card carries a ``fix:`` line.
Spec: docs/backlog/gripe-fix-loop-on-the-gripe.md.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers import _gripe_fix as gf
from precis.handlers.gripe import GripeHandler
from precis.store.types import ChunkInsert
from tests._refs import insert_gripe


@pytest.fixture
def handler(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


def _gripe(handler: GripeHandler, title: str = "Area: something is wrong") -> int:
    return int(insert_gripe(handler.store, title).id)


def _tags(handler: GripeHandler, gid: int) -> set[str]:
    return {str(t) for t in handler.store.tags_for(gid)}


def _state(handler: GripeHandler, gid: int) -> gf.FixState:
    ref = handler.store.get_ref(kind="gripe", id=gid)
    assert ref is not None
    return gf.fix_state(handler.store, ref)


def _text(st: gf.FixState, kind: str) -> str:
    ev = st.newest_of(kind)
    assert ev is not None
    return ev.text


# ── the lifecycle, one edit per step ─────────────────────────────────


def test_brief_and_batch_enter_the_loop(handler: GripeHandler) -> None:
    gid = _gripe(handler)
    out = handler.edit(
        id=gid, mode="fix", batch="tables-s2", brief="Task: fix the table parser."
    ).body
    assert "batch=tables-s2" in out
    assert "brief #1 appended (round 1)" in out
    assert "STATUS:open → STATUS:ready_for_fix" in out
    assert "queue none → queued" in out
    tags = _tags(handler, gid)
    assert "batch:tables-s2" in tags
    assert "STATUS:ready_for_fix" in tags
    st = _state(handler, gid)
    assert st.queue == "queued"
    assert st.rounds == 1
    assert _text(st, gf.BRIEF_KIND) == "Task: fix the table parser."


def test_full_loop_derives_each_queue(handler: GripeHandler) -> None:
    gid = _gripe(handler)
    handler.edit(id=gid, mode="fix", batch="b1", brief="do it")
    assert _state(handler, gid).queue == "queued"

    out = handler.edit(
        id=gid, mode="fix", host="mac-a", session="devin:tables-s2", tree="tables-s2"
    ).body
    assert "STATUS:ready_for_fix → STATUS:in_review" in out
    st = _state(handler, gid)
    assert st.queue == "in-build"
    assert st.who == "devin:tables-s2@mac-a"
    assert st.tree == "tables-s2"

    handler.edit(id=gid, mode="fix", handoff="Head abc1234; next: tests")
    assert _state(handler, gid).queue == "in-review"

    out = handler.edit(id=gid, mode="fix", review="fix", findings="mypy red").body
    assert "review FIX #" in out
    st = _state(handler, gid)
    assert st.queue == "fix-round"
    assert st.verdict == "fix"
    assert _text(st, gf.REVIEW_KIND) == "FIX — mypy red"

    handler.edit(id=gid, mode="fix", brief="round 2: fix mypy")
    st = _state(handler, gid)
    assert st.queue == "in-build"  # session still set → building again
    assert st.rounds == 2

    handler.edit(id=gid, mode="fix", handoff="done")
    handler.edit(id=gid, mode="fix", review="LAND")
    st = _state(handler, gid)
    assert st.queue == "landing"
    assert st.verdict == "land"

    out = handler.edit(id=gid, mode="fix", landed="98aa301d6").body
    assert "landed=98aa301d6" in out
    assert _state(handler, gid).queue == "awaiting-deploy"

    # close → out of every queue, nothing to un-tag
    handler.put(
        id=gid, text="resolved: 98aa301d6 (r110) — deployed", tags=["STATUS:done"]
    )
    st = _state(handler, gid)
    assert st.queue is None
    assert st.terminal
    assert "batch:b1" in _tags(handler, gid)  # history kept


def test_batch_tag_is_replaced_and_clearable(handler: GripeHandler) -> None:
    gid = _gripe(handler)
    handler.edit(id=gid, mode="fix", batch="first")
    out = handler.edit(id=gid, mode="fix", batch="second").body
    assert "batch first → second" in out
    tags = _tags(handler, gid)
    assert "batch:second" in tags and "batch:first" not in tags
    out = handler.edit(id=gid, mode="fix", batch="").body
    assert "batch second removed" in out
    assert not any(t.startswith("batch:") for t in _tags(handler, gid))


def test_meta_field_clears_with_empty_string(handler: GripeHandler) -> None:
    gid = _gripe(handler)
    handler.edit(id=gid, mode="fix", session="codex:lane-1", host="h1")
    handler.edit(id=gid, mode="fix", session="")
    st = _state(handler, gid)
    assert st.session is None
    assert st.host == "h1"
    ref = handler.store.get_ref(kind="gripe", id=gid)
    assert ref is not None
    assert ref.meta["fix"] == {"host": "h1"}


def test_noop_edit_says_so(handler: GripeHandler) -> None:
    gid = _gripe(handler)
    handler.edit(id=gid, mode="fix", host="h1")
    assert "nothing to change" in handler.edit(id=gid, mode="fix", host="h1").body


def test_batch_ids_apply_one_patch_to_all(handler: GripeHandler) -> None:
    ids: list[str | int] = [_gripe(handler, f"probe {i}") for i in range(3)]
    out = handler.edit(id=ids, mode="fix", batch="wave-1", brief="shared brief").body
    assert "edited 3 gripe in one transaction" in out
    for gid in ids:
        st = _state(handler, int(gid))
        assert st.batch == "wave-1"
        assert _text(st, gf.BRIEF_KIND) == "shared brief"


def test_dry_run_writes_nothing(handler: GripeHandler) -> None:
    gid = _gripe(handler)
    out = handler.edit(id=gid, mode="fix", batch="b", brief="x", dry_run=True).body
    assert out.startswith("dry run:")
    assert "nothing written" in out
    assert not _state(handler, gid).in_loop
    assert "STATUS:open" in _tags(handler, gid)


# ── refusals ─────────────────────────────────────────────────────────


def test_refusals_name_the_fix(handler: GripeHandler) -> None:
    gid = _gripe(handler)
    with pytest.raises(BadInput, match="needs at least one key"):
        handler.edit(id=gid, mode="fix")
    with pytest.raises(BadInput, match="not one of"):
        handler.edit(id=gid, mode="fix", review="maybe")
    with pytest.raises(BadInput, match="hex sha"):
        handler.edit(id=gid, mode="fix", landed="not-a-sha")
    with pytest.raises(BadInput, match="rides with"):
        handler.edit(id=gid, mode="fix", findings="orphan")
    with pytest.raises(BadInput, match="not a slug"):
        handler.edit(id=gid, mode="fix", batch="Has Spaces")
    with pytest.raises(BadInput, match="space-free"):
        handler.edit(id=gid, mode="fix", session="devin tables")
    with pytest.raises(BadInput, match="takes no title"):
        handler.edit(id=gid, mode="fix", title="x", brief="y")
    with pytest.raises(BadInput, match="requires id="):
        handler.edit(mode="fix", brief="y")
    # fix keys without mode='fix' point at the mode
    with pytest.raises(BadInput, match="need mode='fix'"):
        handler.edit(id=gid, brief="y")
    assert not _state(handler, gid).in_loop


def test_terminal_gripe_refuses_loop_state(handler: GripeHandler) -> None:
    gid = _gripe(handler)
    handler.put(id=gid, text="resolved: wontfix — nope", tags=["STATUS:wontfix"])
    with pytest.raises(BadInput, match="closed gripe takes no fix-loop state"):
        handler.edit(id=gid, mode="fix", batch="b")


def test_status_verify_is_left_alone(handler: GripeHandler) -> None:
    gid = _gripe(handler)
    handler.tag(id=gid, add=["STATUS:verify"])
    handler.edit(id=gid, mode="fix", landed="abcdef1")
    assert "STATUS:verify" in _tags(handler, gid)


# ── reads: card line, view='fix', /queue, /batch ─────────────────────


def test_card_line_and_fix_view(handler: GripeHandler) -> None:
    gid = _gripe(handler, "Parser: table rows lost\n\nmore detail")
    handler.edit(
        id=gid,
        mode="fix",
        batch="tables-s2",
        brief="the brief text",
        host="mac-a",
        session="claude:abc123",
        tree="tables-s2",
    )
    card = handler.get(id=gid).body
    assert "fix: batch=tables-s2 · queue=in-build · claude:abc123@mac-a" in card
    assert "tree=tables-s2" in card
    assert "brief #1 (round 1)" in card
    assert "view='fix'" in card
    view = handler.get(id=gid, view="fix").body
    assert "## brief #1" in view
    assert "the brief text" in view
    # batch form maps the view
    both = handler.get(id=[gid], view="fix").body
    assert "the brief text" in both
    # a gripe outside the loop says so
    other = _gripe(handler)
    assert "fix:" not in handler.get(id=other).body
    assert "not in the gripe-fix loop" in handler.get(id=other, view="fix").body


def test_queue_view_counts_and_filters_by_host(handler: GripeHandler) -> None:
    a = _gripe(handler, "A: alpha")
    b = _gripe(handler, "B: beta")
    c = _gripe(handler, "C: gamma")
    handler.edit(id=[a, b], mode="fix", batch="x", brief="brief x")
    handler.edit(id=a, mode="fix", host="h1", session="devin:x")
    handler.edit(
        id=c, mode="fix", batch="y", brief="brief y", host="h2", session="codex:y"
    )
    handler.edit(id=c, mode="fix", handoff="stopped")
    body = handler.get(id="/queue").body
    assert "3 in loop" in body
    assert "queued 1" in body and "in-build 1" in body and "in-review 1" in body
    assert "## in-build (1)" in body and f"gr{a}" in body
    assert "devin:x@h1" in body
    only_h1 = handler.get(id="/queue", host="h1").body
    assert f"gr{a}" in only_h1 and f"gr{c}" not in only_h1
    only_review = handler.get(id="/queue", queue="in-review").body
    assert f"gr{c}" in only_review and f"gr{a}" not in only_review
    # a closed gripe leaves the queues
    handler.put(id=c, text="resolved: done", tags=["STATUS:done"])
    assert "2 in loop" in handler.get(id="/queue").body
    with pytest.raises(BadInput, match="unknown queue"):
        handler.get(id="/queue", queue="nope")
    with pytest.raises(BadInput, match="list views only"):
        handler.get(id=a, host="h1")


def test_batch_views_census_and_dossier(handler: GripeHandler) -> None:
    a = _gripe(handler, "A: alpha\n\nbody of alpha")
    b = _gripe(handler, "B: beta")
    handler.edit(id=[a, b], mode="fix", batch="wave", brief="one brief for both")
    handler.put(id=a, text="ruling: keep the old flag")
    census = handler.get(id="/batch").body
    assert "1 live · 2 gripes" in census
    assert "wave  2 gripes  queued 2" in census
    dossier = handler.get(id="/batch", batch="wave").body
    assert dossier.count("one brief for both") == 1  # identical text once
    assert "## brief (all)" in dossier
    assert "body of alpha" in dossier
    assert "ruling: keep the old flag" in dossier
    assert f"## gr{b} — B: beta" in dossier
    assert "no gripe carries batch:zzz" in handler.get(id="/batch", batch="zzz").body
    with pytest.raises(BadInput, match="takes no page"):
        handler.get(id="/queue", page=2)


# ── pure derivation ──────────────────────────────────────────────────


def test_verdict_parsing() -> None:
    assert gf.verdict_of("LAND — all good") == "land"
    assert gf.verdict_of("fix: mypy red") == "fix"
    assert gf.verdict_of("Verdict pending") is None
    assert gf.verdict_of("") is None


def test_taken_ord_is_retried_once(
    handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent append took the planned ord: the write replans and
    retries once (the append_comment contract)."""
    from psycopg.errors import UniqueViolation

    gid = _gripe(handler)
    cls = type(handler.store.chunks)
    real = cls.insert_chunks
    calls: list[list[int]] = []

    def flaky(self: Any, ref_id: int, chunks: list[Any], **kw: Any) -> Any:
        calls.append([c.ord for c in chunks])
        if len(calls) == 1:
            # The "other writer" commits on its own connection (no conn=,
            # so outside our transaction), then we collide on that ord.
            real(self, ref_id, [ChunkInsert(ord=chunks[0].ord, text="other")])
            raise UniqueViolation("duplicate key")
        return real(self, ref_id, chunks, **kw)

    monkeypatch.setattr(cls, "insert_chunks", flaky)
    out = handler.edit(id=gid, mode="fix", brief="after the collision").body
    assert "brief #" in out
    assert len(calls) == 2 and calls[1][0] == calls[0][0] + 1
    st = _state(handler, gid)
    assert _text(st, gf.BRIEF_KIND) == "after the collision"
    assert st.rounds == 1


def test_missing_chunk_kind_seed_is_upstream(
    handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    from psycopg.errors import ForeignKeyViolation

    from precis.errors import Upstream

    gid = _gripe(handler)
    cls = type(handler.store.chunks)

    def no_seed(self: Any, *a: Any, **kw: Any) -> Any:
        raise ForeignKeyViolation("chunks_chunk_kind_fkey")

    monkeypatch.setattr(cls, "insert_chunks", no_seed)
    with pytest.raises(Upstream, match="missing chunk_kind seed") as info:
        handler.edit(id=gid, mode="fix", brief="x")
    assert "0254" in str(info.value.next)
    assert not _state(handler, gid).in_loop  # the transaction rolled back


def test_plan_status_table() -> None:
    brief = gf.FixPatch(brief="x")
    build = gf.FixPatch(session="s")
    assert gf.plan_status("open", brief) == "ready_for_fix"
    assert gf.plan_status("triaged", brief) == "ready_for_fix"
    assert gf.plan_status("ready_for_fix", brief) is None
    assert gf.plan_status("open", build) == "in_review"
    assert gf.plan_status("ready_for_fix", build) == "in_review"
    assert gf.plan_status("in_review", build) is None
    assert gf.plan_status("verify", build) is None
    assert gf.plan_status("done", build) is None
