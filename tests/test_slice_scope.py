"""Slice-grant write scope at the dispatch gate (td575705 G1).

A ``slice:td<t>j<j>:<role>`` principal — bound by
``tools.core._ctx_principal`` off the ASGI grant stamps, bound directly
here — may only write what its role's grant covers
(:mod:`precis.principal`). ``mcp.slice_scope`` arms the gate: ``observe``
allows the call and marks the ok ``tool_calls`` row
(``error_type='SliceScopeWouldRefuse'``), ``enforce`` renders a Denied
whose ``error_head`` carries the same k=v line.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from psycopg.types.json import Jsonb

from precis import principal, settings
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.utils.inproc_context import TickContext, tick_context
from tests.conftest import id_of


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    principal._mode_last_good = None
    with tick_context(TickContext()):
        yield
    principal._mode_last_good = None


@pytest.fixture
def enforce(store: Store) -> Iterator[None]:
    settings.set_setting("mcp.slice_scope", "enforce", store=store)
    yield
    settings.clear_setting("mcp.slice_scope", store=store)


@pytest.fixture(params=["observe", "enforce"])
def both_modes(request: pytest.FixtureRequest, store: Store) -> Iterator[str]:
    """An allowed write must pass — and leave its ledger row unmarked —
    under EITHER mode: allowed is a property of the grant, not the arm."""
    settings.set_setting("mcp.slice_scope", request.param, store=store)
    yield str(request.param)
    settings.clear_setting("mcp.slice_scope", store=store)


def _mk_todo(runtime: PrecisRuntime, text: str = "scope test", **kw: Any) -> int:
    body, error = runtime.dispatch_with_status(
        "put", {"kind": "todo", "text": text, **kw}
    )
    assert not error, body
    return id_of(body)


def _set_meta(store: Any, ref_id: int, meta: dict[str, Any]) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = %s WHERE ref_id = %s",
            (Jsonb(meta), ref_id),
        )


def _slice(todo_id: int, role: str = "builder") -> Any:
    return principal.bind(principal.for_slice(f"td{todo_id}j9", role))


def _comment_meta(store: Any, todo_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM chunks WHERE ref_id = %s "
            "AND chunk_kind = 'todo_comment' "
            "ORDER BY chunk_id DESC LIMIT 1",
            (todo_id,),
        ).fetchone()
    assert row is not None
    return dict(row[0])


def _newest_call(store: Any) -> tuple[str | None, str | None, str]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT error_type, error_head, outcome FROM tool_calls "
            "ORDER BY call_id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    return row[0], row[1], row[2]


def _assert_row_unmarked(store: Any) -> None:
    """The newest ledger row is an unmarked ok row — an allowed call
    must not carry the would-refuse marker in either mode."""
    error_type, _head, outcome = _newest_call(store)
    assert outcome == "ok"
    assert error_type != principal.WOULD_REFUSE


def _assert_untouched(runtime: PrecisRuntime, verb: str, args: dict[str, Any]) -> None:
    """The same call under a non-slice principal is never refused by the
    slice gate (it may still fail for unrelated reasons — only the
    slice-scope refusal and marker are off-limits)."""
    for who in ("install", "user:reto"):
        with principal.bind(who):
            body, _err = runtime.dispatch_with_status(verb, dict(args))
        assert "slice scope refused" not in body
        error_type, _head, _outcome = _newest_call(runtime.store)
        assert error_type != principal.WOULD_REFUSE


# ── fake FastMCP ctx (mirrors tests/test_asa_precis_client_stamp.py) ──


class _FakeRC:
    """``RequestContext`` stand-in — only the attributes ``tools/core``'s
    ctx readers touch: ``request`` is the transport request (non-None =
    HTTP), ``meta`` the parsed ``params._meta``."""

    def __init__(self, request: Any) -> None:
        self.request = request
        self.meta = None


class _FakeCtx:
    """FastMCP ``Context`` stand-in for the bind-chain tests."""

    def __init__(self, request: Any) -> None:
        self.request_context = _FakeRC(request)
        self.session = None


def _slice_ctx(
    slice_id: str, scope: str | None = None, refs: tuple[str, ...] = ()
) -> Any:
    """A ctx shaped like one the auth middleware stamped a slice grant on."""
    from precis import slice_tokens

    stamps: dict[str, Any] = {slice_tokens.SCOPE_SLICE_ID: slice_id}
    if scope is not None:
        stamps[slice_tokens.SCOPE_GRANT_SCOPE] = scope
        stamps[slice_tokens.SCOPE_GRANT_REFS] = refs
    return _FakeCtx(SimpleNamespace(scope=stamps))


def _is_error_result(res: Any) -> bool:
    return not isinstance(res, str) and bool(getattr(res, "isError", False))


def _result_text(res: Any) -> str:
    if isinstance(res, str):
        return res
    content = getattr(res, "content", None) or []
    return str(getattr(content[0], "text", "")) if content else ""


# ── the grant ──────────────────────────────────────────────────────


def test_comment_on_own_todo_allowed(
    runtime_with_store: PrecisRuntime, store: Store, both_modes: str
) -> None:
    own = _mk_todo(runtime_with_store)
    with _slice(own):
        body, error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "todo", "id": own, "text": "progress note"}
        )
    assert not error, body
    # server-set principal on the comment chunk; a caller cannot supply it
    assert _comment_meta(store, own)["principal"] == (f"slice:td{own}j9:builder")
    _assert_row_unmarked(store)


def test_new_gripe_allowed(
    runtime_with_store: PrecisRuntime, store: Store, both_modes: str
) -> None:
    own = _mk_todo(runtime_with_store)
    with _slice(own):
        body, error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "gripe", "text": "the lane ate my work"}
        )
    assert not error, body
    gripe_id = id_of(body)
    ref = store.get_ref(kind="gripe", id=gripe_id)
    assert ref is not None
    assert ref.meta["filed_by"]["principal"] == f"slice:td{own}j9:builder"
    _assert_row_unmarked(store)


def test_reads_always_pass(runtime_with_store: PrecisRuntime) -> None:
    own = _mk_todo(runtime_with_store)
    with _slice(own):
        body, error = runtime_with_store.dispatch_with_status(
            "get", {"kind": "todo", "id": own}
        )
        assert not error, body
        body, error = runtime_with_store.dispatch_with_status(
            "search", {"kind": "todo", "q": "scope test"}
        )
        assert not error, body


def test_comment_on_other_todo_refused(
    runtime_with_store: PrecisRuntime, enforce: None
) -> None:
    own = _mk_todo(runtime_with_store)
    other = _mk_todo(runtime_with_store, "not mine")
    with _slice(own):
        body, error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "todo", "id": other, "text": "forged verdict"}
        )
    assert error
    assert "[error:Denied] slice scope refused" in body
    assert "role=builder" in body
    _assert_untouched(
        runtime_with_store,
        "put",
        {"kind": "todo", "id": other, "text": "install note"},
    )


def test_review_land_on_other_todo(
    runtime_with_store: PrecisRuntime, enforce: None
) -> None:
    """A reviewer-role slice is comment-only, and the comment grant is
    its OWN todo — a ``landed:`` verdict on a foreign todo refuses."""
    own = _mk_todo(runtime_with_store)
    other = _mk_todo(runtime_with_store, "not mine")
    with _slice(own, role="reviewer"):
        body, error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "todo", "id": other, "text": "landed: deadbee"}
        )
    assert error
    assert "[error:Denied] slice scope refused" in body
    assert "rule=comment-shape" in body and "role=reviewer" in body
    _assert_untouched(
        runtime_with_store,
        "put",
        {"kind": "todo", "id": other, "text": "landed: deadbee"},
    )


def test_status_tag_refused(runtime_with_store: PrecisRuntime, enforce: None) -> None:
    own = _mk_todo(runtime_with_store)
    with _slice(own):
        body, error = runtime_with_store.dispatch_with_status(
            "tag", {"kind": "todo", "id": own, "add": ["STATUS:done"]}
        )
    assert error
    assert "rule=tag-allowlist" in body
    _assert_untouched(
        runtime_with_store,
        "tag",
        {"kind": "todo", "id": own, "add": ["STATUS:done"]},
    )


def test_job_put_refused(runtime_with_store: PrecisRuntime, enforce: None) -> None:
    own = _mk_todo(runtime_with_store)
    with _slice(own):
        body, error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "job", "job_type": "dream"}
        )
        assert error and "[error:Denied]" in body
        # the settings path is refused too — claim.* is CLI-gated
        body, error = runtime_with_store.dispatch_with_status(
            "put",
            {
                "kind": "job",
                "mode": "setting",
                "key": "claim.x",
                "__extras__": {"value": "1"},
            },
        )
    assert error and "rule=kind" in body
    _assert_untouched(runtime_with_store, "put", {"kind": "job", "job_type": "dream"})
    _assert_untouched(
        runtime_with_store,
        "put",
        {
            "kind": "job",
            "mode": "setting",
            "key": "claim.x",
            "__extras__": {"value": "1"},
        },
    )


def test_memory_write_refused(runtime_with_store: PrecisRuntime, enforce: None) -> None:
    own = _mk_todo(runtime_with_store)
    with _slice(own):
        body, error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "memory", "text": "planted fact"}
        )
    assert error
    assert "rule=kind" in body and "kind=memory" in body
    _assert_untouched(
        runtime_with_store, "put", {"kind": "memory", "text": "planted fact"}
    )


def test_edit_and_delete_refused(
    runtime_with_store: PrecisRuntime, enforce: None
) -> None:
    own = _mk_todo(runtime_with_store)
    with _slice(own):
        body, error = runtime_with_store.dispatch_with_status(
            "edit", {"kind": "todo", "id": own, "text": "rewrite"}
        )
        assert error and "rule=verb" in body
        body, error = runtime_with_store.dispatch_with_status(
            "delete", {"kind": "todo", "id": own}
        )
    assert error and "rule=verb" in body
    _assert_untouched(
        runtime_with_store, "edit", {"kind": "todo", "id": own, "text": "x"}
    )
    _assert_untouched(runtime_with_store, "delete", {"kind": "todo", "id": own})


def test_child_todo_under_own_todo_allowed(
    runtime_with_store: PrecisRuntime, store: Store, both_modes: str
) -> None:
    own = _mk_todo(runtime_with_store)
    with _slice(own, role="generator"):
        body, error = runtime_with_store.dispatch_with_status(
            "put",
            {
                "kind": "todo",
                "text": "child build",
                "parent_id": own,
                "meta": {
                    "llm_tier": "sonnet",
                    "op": {
                        "type": "run_slice",
                        "params": {"model": "sonnet", "effort": "low"},
                    },
                },
            },
        )
    assert not error, body
    child = store.get_ref(kind="todo", id=id_of(body))
    assert child is not None and child.parent_id == own
    _assert_row_unmarked(store)


def test_child_op_outside_allowlist_refused(
    runtime_with_store: PrecisRuntime, enforce: None
) -> None:
    own = _mk_todo(runtime_with_store)
    with _slice(own, role="generator"):
        # meta key outside {llm_tier, op}
        body, error = runtime_with_store.dispatch_with_status(
            "put",
            {
                "kind": "todo",
                "text": "child",
                "parent_id": own,
                "meta": {"op": {"type": "run_slice", "params": {}}, "claim": "x"},
            },
        )
        assert error and "rule=meta-keys" in body
        # op type outside run_slice
        body, error = runtime_with_store.dispatch_with_status(
            "put",
            {
                "kind": "todo",
                "text": "child",
                "parent_id": own,
                "meta": {"op": {"type": "shell", "params": {"model": "x"}}},
            },
        )
        assert error and "rule=meta-keys" in body
        # executor keys are refused outright — they land on meta
        body, error = runtime_with_store.dispatch_with_status(
            "put",
            {
                "kind": "todo",
                "text": "child",
                "parent_id": own,
                "job_type": "dream",
            },
        )
        assert error and "rule=create-shape" in body
        # and a builder mints no children at all
        with _slice(own):
            body, error = runtime_with_store.dispatch_with_status(
                "put", {"kind": "todo", "text": "child", "parent_id": own}
            )
    assert error and "rule=create-role" in body
    _assert_untouched(
        runtime_with_store,
        "put",
        {"kind": "todo", "text": "child", "parent_id": own},
    )
    _assert_untouched(
        runtime_with_store,
        "put",
        {
            "kind": "todo",
            "text": "child",
            "parent_id": own,
            "meta": {"op": {"type": "shell", "params": {"model": "x"}}},
        },
    )


def test_blocked_by_link_between_children_allowed(
    runtime_with_store: PrecisRuntime, store: Store, both_modes: str
) -> None:
    own = _mk_todo(runtime_with_store)
    a = _mk_todo(runtime_with_store, "child a", parent_id=own)
    b = _mk_todo(runtime_with_store, "child b", parent_id=own)
    with _slice(own, role="generator"):
        body, error = runtime_with_store.dispatch_with_status(
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
    _assert_row_unmarked(store)


def test_batch_ids_checked_each(
    runtime_with_store: PrecisRuntime, enforce: None
) -> None:
    own = _mk_todo(runtime_with_store)
    other = _mk_todo(runtime_with_store, "not mine")
    with _slice(own):
        body, error = runtime_with_store.dispatch_with_status(
            "tag", {"kind": "todo", "id": [own, other], "add": ["x"]}
        )
        assert error and "rule=tag-allowlist" in body
    # link: one foreign end refuses the whole batch
    child = _mk_todo(runtime_with_store, "child", parent_id=own)
    with _slice(own, role="generator"):
        body, error = runtime_with_store.dispatch_with_status(
            "link",
            {
                "kind": "todo",
                "id": [child],
                "target": f"td{other}",
                "mode": "add",
                "rel": "blocked-by",
            },
        )
    assert error and "rule=link-ends" in body
    _assert_untouched(
        runtime_with_store,
        "tag",
        {"kind": "todo", "id": [own, other], "add": ["x"]},
    )
    _assert_untouched(
        runtime_with_store,
        "link",
        {
            "kind": "todo",
            "id": [child],
            "target": f"td{other}",
            "mode": "add",
            "rel": "blocked-by",
        },
    )


def test_principal_not_caller_settable(runtime_with_store: PrecisRuntime) -> None:
    own = _mk_todo(runtime_with_store)
    # a writer claim naming principal is an unknown key — refused
    body, error = runtime_with_store.dispatch_with_status(
        "put",
        {
            "kind": "todo",
            "text": "x",
            "__extras__": {"writer": {"principal": "slice:td1j1:builder"}},
        },
    )
    assert error
    assert "[error:BadInput]" in body
    # and meta.principal on a comment never survives the server stamp
    with _slice(own):
        body, error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "todo", "id": own, "text": "note"}
        )
    assert not error
    assert _comment_meta(runtime_with_store.store, own)["principal"].startswith(
        "slice:"
    )


# ── modes ──────────────────────────────────────────────────────────


def test_observe_allows_and_logs(
    runtime_with_store: PrecisRuntime, caplog: pytest.LogCaptureFixture
) -> None:
    own = _mk_todo(runtime_with_store)
    with caplog.at_level(logging.INFO, logger="precis.slice_scope"):
        with _slice(own):
            body, error = runtime_with_store.dispatch_with_status(
                "put", {"kind": "memory", "text": "observed write"}
            )
    assert not error, body  # observe allows the write
    error_type, error_head, outcome = _newest_call(runtime_with_store.store)
    assert outcome == "ok"
    assert error_type == principal.WOULD_REFUSE
    assert error_head is not None
    assert error_head.startswith("slice-scope would-refuse")
    assert "role=builder" in error_head and "rule=kind" in error_head
    assert any("would-refuse" in r.getMessage() for r in caplog.records)


def test_enforce_marks_denied_row(
    runtime_with_store: PrecisRuntime, enforce: None
) -> None:
    own = _mk_todo(runtime_with_store)
    with _slice(own):
        body, error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "memory", "text": "x"}
        )
    assert error
    error_type, error_head, outcome = _newest_call(runtime_with_store.store)
    assert outcome == "error"
    assert error_type == "Denied"
    assert error_head is not None and "slice scope refused role=builder" in error_head


def test_off_skips_the_check(runtime_with_store: PrecisRuntime, store: Store) -> None:
    settings.set_setting("mcp.slice_scope", "off", store=store)
    try:
        own = _mk_todo(runtime_with_store)
        with _slice(own):
            body, error = runtime_with_store.dispatch_with_status(
                "put", {"kind": "memory", "text": "unchecked"}
            )
        assert not error, body
    finally:
        settings.clear_setting("mcp.slice_scope", store=store)


def test_install_bearer_unchanged(runtime_with_store: PrecisRuntime) -> None:
    with principal.bind("install"):
        body, error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "memory", "text": "operator write"}
        )
    assert not error, body


def test_unknown_setting_value_acts_as_observe(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """A typo'd mode never opens the gate: an unknown value observes —
    allows the write and still marks the ledger row."""
    settings.set_setting("mcp.slice_scope", "bogus", store=store)
    try:
        own = _mk_todo(runtime_with_store)
        with _slice(own):
            body, error = runtime_with_store.dispatch_with_status(
                "put", {"kind": "memory", "text": "typo'd mode"}
            )
        assert not error, body
        error_type, _head, outcome = _newest_call(store)
        assert outcome == "ok" and error_type == principal.WOULD_REFUSE
    finally:
        settings.clear_setting("mcp.slice_scope", store=store)


def test_slice_scope_mode_never_degrades_on_a_read_error(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed ``app_settings`` read keeps the last good mode — an armed
    ``enforce`` cannot decay to ``observe`` mid-outage — and a failure
    with nothing good read yet fails closed on ``enforce``."""

    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("app_settings unreachable")

    try:
        # never saw a good read + broken DB → fail closed, not the
        # registered observe default (``_db_read`` delegates to the
        # strict read, so one patch breaks both)
        monkeypatch.setattr(settings, "_db_read_strict", _boom)
        assert principal.slice_scope_mode(store) == "enforce"
        monkeypatch.undo()

        settings.set_setting("mcp.slice_scope", "observe", store=store)
        assert principal.slice_scope_mode(store) == "observe"
        settings.invalidate("mcp.slice_scope")
        monkeypatch.setattr(settings, "_db_read_strict", _boom)
        assert principal.slice_scope_mode(store) == "observe"  # last good
        monkeypatch.undo()

        settings.set_setting("mcp.slice_scope", "enforce", store=store)
        assert principal.slice_scope_mode(store) == "enforce"
        settings.invalidate("mcp.slice_scope")
        monkeypatch.setattr(settings, "_db_read_strict", _boom)
        # an armed enforce survives the outage — never downgraded
        assert principal.slice_scope_mode(store) == "enforce"
        monkeypatch.undo()

        # a healthy read recovers and re-arms the cache
        assert principal.slice_scope_mode(store) == "enforce"
        settings.clear_setting("mcp.slice_scope", store=store)
        assert principal.slice_scope_mode(store) == "observe"
    finally:
        monkeypatch.undo()
        settings.clear_setting("mcp.slice_scope", store=store)


# ── the badge itself: malformed slice principals and the bind chain ──


def test_malformed_slice_principal_refused(
    runtime_with_store: PrecisRuntime, enforce: None
) -> None:
    """A ``slice:``-shaped principal that fails the grammar is a forged
    or half-stamped badge — a write under it refuses, never allows."""
    for bad in (
        "slice:",
        "slice:bogus",
        "slice:td1j2",
        "slice:td1j2:",
        "slice:td1j2:builder:extra",
    ):
        with principal.bind(bad):
            body, error = runtime_with_store.dispatch_with_status(
                "put", {"kind": "memory", "text": "forged badge"}
            )
        assert error, bad
        assert "slice scope refused" in body, bad
        assert "rule=principal-shape" in body, bad
    _assert_untouched(runtime_with_store, "put", {"kind": "memory", "text": "x"})


def test_minted_grant_binds_slice_principal(
    runtime_with_store: PrecisRuntime, store: Store, tmp_path: Any
) -> None:
    """The full bind chain: a minted grant validates into the ASGI scope
    stamps, ``_ctx_principal`` binds ``slice:<grant>:<role>`` off them,
    and the dispatch gate applies it — the own-todo comment lands
    attributed, a foreign-todo write is refused under enforce."""
    from precis import slice_tokens
    from precis.tools import core as tools_core

    own = _mk_todo(runtime_with_store)
    other = _mk_todo(runtime_with_store, "not mine")
    token = slice_tokens.mint(tmp_path, f"td{own}j7")
    granted = slice_tokens.grant(tmp_path, f"Bearer {token}")
    assert granted is not None
    ctx = _slice_ctx(granted.slice_id)
    tools_core._runtime = runtime_with_store
    try:
        res = tools_core._dispatch(
            "put", {"kind": "todo", "id": own, "text": "progress"}, ctx
        )
        assert not _is_error_result(res), _result_text(res)
        assert _comment_meta(store, own)["principal"] == (f"slice:td{own}j7:builder")
        settings.set_setting("mcp.slice_scope", "enforce", store=store)
        res = tools_core._dispatch(
            "put",
            {"kind": "todo", "id": other, "text": "forged verdict"},
            ctx,
        )
        assert _is_error_result(res)
        assert "slice scope refused" in _result_text(res)
    finally:
        settings.clear_setting("mcp.slice_scope", store=store)
        tools_core._runtime = None


def test_scoped_grant_binds_scope_as_role(
    runtime_with_store: PrecisRuntime, store: Store, tmp_path: Any
) -> None:
    """A grant minted ``scope=``/``refs=`` stamps them on the ASGI scope;
    ``_ctx_principal`` binds ``slice:<grant>:<scope>`` — the boundary's
    role, not the todo's ``meta.generate.role``. A triage write inside
    the boundary's allowlist lands attributed, a write outside it is
    refused by the dispatch backstop, and a reader write reaching the
    role table would-refuses (defense in depth)."""
    from precis import slice_tokens
    from precis.tools import core as tools_core

    own = _mk_todo(runtime_with_store)
    token = slice_tokens.mint(
        tmp_path, f"td{own}j7", scope="triage", refs=(f"td{own}",)
    )
    granted = slice_tokens.grant(tmp_path, f"Bearer {token}")
    assert granted is not None
    assert granted.scope == "triage" and granted.refs == (f"td{own}",)
    ctx = _slice_ctx(granted.slice_id, scope=granted.scope, refs=granted.refs)
    tools_core._runtime = runtime_with_store
    try:
        assert tools_core._ctx_principal(ctx, store, "put") == (
            f"slice:td{own}j7:triage"
        )
        res = tools_core._dispatch(
            "put", {"kind": "todo", "id": own, "text": "claiming"}, ctx
        )
        assert not _is_error_result(res), _result_text(res)
        assert _comment_meta(store, own)["principal"] == f"slice:td{own}j7:triage"
        res = tools_core._dispatch(
            "put", {"kind": "memory", "text": "outside the scope"}, ctx
        )
        assert _is_error_result(res)
        assert "[error:Denied]" in _result_text(res)
        # A reader role at the role table is defense-in-depth: the
        # boundary refused the write already; the verdict is a
        # would-refuse under observe, a Denied under enforce.
        with principal.bind(f"slice:td{own}j7:reader"):
            verdict = principal.slice_scope_verdict(
                runtime_with_store, "put", {"kind": "todo", "id": own}
            )
        assert verdict is not None and verdict.rule == "scope-reader"
    finally:
        tools_core._runtime = None


def test_triage_verdict_rec_calls_the_boundary(
    runtime_with_store: PrecisRuntime,
) -> None:
    """Defense in depth (review F3): the triage branch does not return
    None — it re-judges the write through ``slice_reader_scope.refusal``
    on the same flattened args, so a foreign-kind write, an own-todo
    tag or a verdict-shaped own-todo comment all verdict-refuse while
    the in-scope write passes."""
    own = _mk_todo(runtime_with_store)
    with principal.bind(f"slice:td{own}j7:triage"):
        for args in (
            {"kind": "memory", "text": "x"},
            {"kind": "todo", "id": "td999999", "text": "foreign todo"},
            {"kind": "todo", "id": own, "add": ["STATUS:done"]},
            {"kind": "todo", "id": own, "text": "review: LAND"},
            {"kind": "todo", "id": own, "text": "landed: deadbee"},
        ):
            verdict = principal.slice_scope_verdict(
                runtime_with_store, "put" if "text" in args else "tag", args
            )
            assert verdict is not None and verdict.rule == "scope-triage", args
        # In-scope writes pass: an own-todo comment and the one claim.
        assert (
            principal.slice_scope_verdict(
                runtime_with_store,
                "put",
                {"kind": "todo", "id": own, "text": "progress note"},
            )
            is None
        )
        assert (
            principal.slice_scope_verdict(
                runtime_with_store,
                "link",
                {"kind": "todo", "id": own, "target": "gr1", "rel": "fixes"},
            )
            is None
        )


def test_command_form_checked(runtime_with_store: PrecisRuntime, store: Store) -> None:
    """The ``precis(command=...)`` profile reaches the same dispatch —
    and the same gate: observed under ``observe``, refused under
    ``enforce``."""
    from precis import server as precis_server
    from precis.tools import core as tools_core

    own = _mk_todo(runtime_with_store)
    other = _mk_todo(runtime_with_store, "not mine")
    ctx = _slice_ctx(f"td{own}j4")
    command = f"put(kind='todo', id={other}, text='forged verdict')"
    tools_core._runtime = runtime_with_store
    try:
        settings.set_setting("mcp.slice_scope", "observe", store=store)
        res = precis_server.precis(command=command, ctx=ctx)
        assert not _is_error_result(res), _result_text(res)
        error_type, error_head, outcome = _newest_call(store)
        assert outcome == "ok" and error_type == principal.WOULD_REFUSE
        assert error_head is not None and "rule=comment-shape" in error_head
        settings.set_setting("mcp.slice_scope", "enforce", store=store)
        res = precis_server.precis(command=command, ctx=ctx)
        assert _is_error_result(res)
        assert "slice scope refused" in _result_text(res)
        _assert_untouched(
            runtime_with_store,
            "put",
            {"kind": "todo", "id": other, "text": "install note"},
        )
    finally:
        settings.clear_setting("mcp.slice_scope", store=store)
        tools_core._runtime = None


def test_todo_put_with_meta_schedule(
    runtime_with_store: PrecisRuntime, enforce: None
) -> None:
    """A recurring-root mint (``meta.schedule``) is outside the
    generator's child-meta allowlist — refused even parented to the
    grant's own todo."""
    own = _mk_todo(runtime_with_store)
    with _slice(own, role="generator"):
        body, error = runtime_with_store.dispatch_with_status(
            "put",
            {
                "kind": "todo",
                "text": "recurring child",
                "parent_id": own,
                "meta": {"schedule": {"cron": "0 9 * * *"}},
            },
        )
    assert error
    assert "[error:Denied] slice scope refused" in body
    assert "rule=meta-keys" in body
    _assert_untouched(
        runtime_with_store,
        "put",
        {
            "kind": "todo",
            "text": "recurring child",
            "parent_id": own,
            "meta": {"schedule": {"cron": "0 9 * * *"}},
        },
    )


def test_web_user_abbrev_binding(
    runtime_with_store: PrecisRuntime, store: Store, enforce: None
) -> None:
    """A browser write inside ``human_write_context`` binds
    ``user:<abbrev>`` — recorded on the comment and untouched by the
    slice gate even under enforce."""
    from starlette.requests import Request

    from precis.users import WebUser
    from precis_web.deps import human_write_context

    other = _mk_todo(runtime_with_store, "foreign")
    user = WebUser(
        id=1,
        login="reto",
        abbrev="rt",
        full_name=None,
        email=None,
        disabled_at=None,
        last_login_at=None,
        created_at=None,
        updated_at=None,
    )
    request = cast("Request", SimpleNamespace(scope={"state": {"web_user": user}}))
    with human_write_context(request):
        assert principal.current() == "user:rt"
        body, error = runtime_with_store.dispatch_with_status(
            "put", {"kind": "todo", "id": other, "text": "web comment"}
        )
    assert not error, body
    assert _comment_meta(store, other)["principal"] == "user:rt"
    _assert_row_unmarked(store)


# ── role resolution (slice_role reads the grant's own todo meta) ────


def test_slice_role_mapping(runtime_with_store: PrecisRuntime) -> None:
    store = runtime_with_store.store
    todo = _mk_todo(runtime_with_store)
    assert principal.slice_role(store, f"td{todo}") == "builder"
    _set_meta(store, todo, {"generate": {"role": "slicer"}})
    assert principal.slice_role(store, f"td{todo}j5") == "generator"
    _set_meta(store, todo, {"generate": {"role": "reviewer"}})
    assert principal.slice_role(store, f"td{todo}") == "reviewer"
    _set_meta(store, todo, {"rescue_for": 123, "generate": {"role": "slicer"}})
    assert principal.slice_role(store, f"td{todo}") == "diagnose"
    _set_meta(store, todo, {"generate": {"role": "not-a-role"}})
    assert principal.slice_role(store, f"td{todo}") == "comment"
    # an unreadable / missing grant todo fails closed to comment-only
    assert principal.slice_role(store, "td999999999") == "comment"
