"""Write principal: ContextVar mechanics and where the stamp lands (td575705 G1).

:mod:`precis.principal` is the server-set write identity — never
caller-supplied. The unit legs cover the ContextVar (``worker:<source>``
fallback, ``bind`` nesting/reset, ``worker_comment_meta`` explicitness),
the ``for_slice``/``_parse_slice`` round-trip and the ``mcp.slice_scope``
mode resolver. The dispatch legs write under a bound ``slice:…``
principal (observe mode lets every write land) and assert it reaches
every artifact: comment ``meta.principal``, ``ref_tags.principal``,
``meta.filed_by.principal`` and ``revisions.writer.principal`` —
create/edit/delete on a ref, and the link-removal revision (link INSERTs
log no revision row; the principal shows on the edge's delete/update).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest

from precis import agentlog, principal, settings
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.utils.inproc_context import TickContext, tick_context
from tests.conftest import id_of


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    settings._warned.clear()
    with tick_context(TickContext()):
        yield
    settings._warned.clear()


def _mk_todo(runtime: PrecisRuntime, text: str, **kw: Any) -> int:
    body, error = runtime.dispatch_with_status(
        "put", {"kind": "todo", "text": text, **kw}
    )
    assert not error, body
    return id_of(body)


def _comment_meta(store: Store, todo_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM chunks WHERE ref_id = %s "
            "AND chunk_kind = 'todo_comment' "
            "ORDER BY chunk_id DESC LIMIT 1",
            (todo_id,),
        ).fetchone()
    assert row is not None
    return dict(row[0])


def _newest_writer(store: Store, target_kind: str, target_id: int) -> dict[str, Any]:
    revs = store.revisions_for(target_kind, target_id)
    assert revs
    writer = revs[-1].writer
    assert writer is not None, f"no writer on {target_kind} {target_id}"
    return dict(writer)


# ── the ContextVar ──────────────────────────────────────────────────


def test_current_falls_back_to_worker_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(agentlog.SOURCE_ENV_VAR, raising=False)
    assert principal.current() == "worker:cli"
    monkeypatch.setenv(agentlog.SOURCE_ENV_VAR, "run_slice")
    assert principal.current() == "worker:run_slice"


def test_bind_sets_nests_and_restores(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(agentlog.SOURCE_ENV_VAR, raising=False)
    with principal.bind("install"):
        assert principal.current() == "install"
        with principal.bind("slice:td1j2:builder"):
            assert principal.current() == "slice:td1j2:builder"
        assert principal.current() == "install"
        # bind(None) clears: the fallback shows through
        with principal.bind(None):
            assert principal.current() == "worker:cli"
        assert principal.current() == "install"
    assert principal.current() == "worker:cli"


def test_worker_comment_meta_is_explicit_not_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bound slice/install stamp must not leak onto a worker record —
    ``worker_comment_meta`` names the worker source outright."""
    monkeypatch.delenv(agentlog.SOURCE_ENV_VAR, raising=False)
    with principal.bind("slice:td1j2:builder"):
        assert principal.worker_comment_meta() == {"principal": "worker:cli"}
    monkeypatch.setenv(agentlog.SOURCE_ENV_VAR, "gripe_dispatch")
    with principal.bind("install"):
        assert principal.worker_comment_meta() == {"principal": "worker:gripe_dispatch"}


def test_for_slice_parse_round_trip() -> None:
    text = principal.for_slice("td42j7", "generator")
    assert text == "slice:td42j7:generator"
    assert principal._parse_slice(text) == ("td42j7", 42, "generator")
    assert principal._parse_slice("slice:td42:builder") == ("td42", 42, "builder")
    for bad in ("install", "user:rs", "worker:cli", None, "slice:x:y", "slice:td1:"):
        assert principal._parse_slice(bad) is None


# ── mcp.slice_scope mode ─────────────────────────────────────────────


def test_slice_scope_mode_default_set_and_bogus(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    try:
        assert principal.slice_scope_mode(store) == "observe"
        settings.set_setting("mcp.slice_scope", "enforce", store=store)
        assert principal.slice_scope_mode(store) == "enforce"
        settings.set_setting("mcp.slice_scope", "off", store=store)
        assert principal.slice_scope_mode(store) == "off"
        # an unknown value acts as observe and warns once — a security
        # control never silently opens on a typo'd mode
        settings.set_setting("mcp.slice_scope", "bogus", store=store)
        with caplog.at_level(logging.WARNING):
            assert principal.slice_scope_mode(store) == "observe"
            assert principal.slice_scope_mode(store) == "observe"
        warns = [r for r in caplog.records if "mcp.slice_scope" in r.message]
        assert len(warns) == 1
    finally:
        settings.clear_setting("mcp.slice_scope", store=store)


# ── where the stamp lands (observe mode: every write applies) ────────


def test_principal_recorded_on_comment_tag_put_edit_delete_link(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    rt = runtime_with_store
    own = _mk_todo(rt, "the grant's todo")
    a = _mk_todo(rt, "child a", parent_id=own)
    b = _mk_todo(rt, "child b", parent_id=own)
    bound = principal.for_slice(f"td{own}j9", "builder")
    with principal.bind(bound):
        # comment → chunks.meta.principal
        body, error = rt.dispatch_with_status(
            "put", {"kind": "todo", "id": own, "text": "progress note"}
        )
        assert not error, body
        assert _comment_meta(store, own)["principal"] == bound
        # tag → ref_tags.principal
        body, error = rt.dispatch_with_status(
            "tag", {"kind": "todo", "id": own, "add": ["slice-mark"]}
        )
        assert not error, body
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT principal FROM ref_tags WHERE ref_id = %s", (own,)
            ).fetchall()
        assert bound in {r[0] for r in rows}
        # put create → meta.filed_by.principal + revisions.writer.principal
        # (memory is a covered kind — todo revisions only carry trust acts)
        body, error = rt.dispatch_with_status(
            "put", {"kind": "memory", "text": "made under the grant"}
        )
        assert not error, body
        made = id_of(body)
        ref = store.get_ref(kind="memory", id=made)
        assert ref is not None
        assert ref.meta["filed_by"]["principal"] == bound
        assert _newest_writer(store, "ref", made)["principal"] == bound
        # edit → the new revision's writer.principal
        body, error = rt.dispatch_with_status(
            "edit",
            {"kind": "memory", "id": made, "mode": "append", "text": "more"},
        )
        assert not error, body
        assert _newest_writer(store, "ref", made)["principal"] == bound
        # link removal → the link row's 'deleted' revision writer.principal
        body, error = rt.dispatch_with_status(
            "link",
            {
                "kind": "todo",
                "id": a,
                "target": f"td{b}",
                "mode": "add",
                "rel": "blocked-by",
            },
        )
        assert not error, body
        with store.pool.connection() as conn:
            link_row = conn.execute(
                "SELECT link_id FROM links WHERE src_ref_id = %s "
                "AND dst_ref_id = %s AND relation = 'blocked-by'",
                (a, b),
            ).fetchone()
        assert link_row is not None
        link_id = int(link_row[0])
        body, error = rt.dispatch_with_status(
            "link",
            {
                "kind": "todo",
                "id": a,
                "target": f"td{b}",
                "mode": "remove",
                "rel": "blocked-by",
            },
        )
        assert not error, body
        revs = store.revisions_for("link", link_id)
        assert revs and revs[-1].event == "deleted"
        assert _newest_writer(store, "link", link_id)["principal"] == bound
        # delete → the ref's 'retired'/'deleted' revision writer.principal
        body, error = rt.dispatch_with_status("delete", {"kind": "memory", "id": made})
        assert not error, body
        revs = store.revisions_for("ref", made)
        assert revs[-1].event in ("retired", "deleted")
        assert _newest_writer(store, "ref", made)["principal"] == bound


def test_caller_meta_filed_by_is_refused(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``meta.filed_by`` is server-owned filing provenance — a put naming
    it is refused outright, same as a ``writer.principal`` claim."""
    rt = runtime_with_store
    body, error = rt.dispatch_with_status(
        "put",
        {
            "kind": "todo",
            "text": "forged origin",
            "meta": {"filed_by": {"principal": "install", "actor": "reto"}},
        },
    )
    assert error
    assert "filed_by" in body


def test_unbound_write_is_a_worker(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    body, error = runtime_with_store.dispatch_with_status(
        "put", {"kind": "todo", "text": "no bound principal"}
    )
    assert not error, body
    ref = store.get_ref(kind="todo", id=id_of(body))
    assert ref is not None
    assert ref.meta["filed_by"]["principal"].startswith("worker:")
