"""gr334695: a caller kwarg that lands in a handler verb method's bare
``**_kw`` catch-all — instead of an explicit parameter, or a parameter
forwarded to it via the cooperative-inheritance ``super().<verb>(...,
**_kw)`` chain — must raise, not silently vanish.

Three prod incidents motivated the dispatch-boundary strictness gate
(:func:`precis.runtime.dispatch._handler_accepted_kwargs`,
:meth:`precis.runtime.dispatch.DispatchMixin._invoke_handler`):
``TodoHandler.put`` swallowing ``executor=``/``job_type=``/``params=``
(gr333433), ``DraftHandler.edit`` swallowing ``mode=``/``where=`` (data
loss, gr334153), and the original gripe report. Both of those specific
handler signatures are already fixed (they now declare the kwargs
explicitly) — the regression coverage here pins the *general* boundary
gate: a representative still-unrecognized kwarg on a plain handler
raises the new ``BadInput``, the two fixed paths keep working through
the actual dispatch boundary, and a deliberately-tolerant handler
(``@tolerates_extra_kwargs``) is unaffected.
"""

from __future__ import annotations

import re

import pytest

from precis.runtime import PrecisRuntime


def test_mro_forwarding_accepted_kwargs_unions_the_forwarding_chain() -> None:
    """:func:`_handler_accepted_kwargs` must union in an ancestor's own
    explicit params only when the resolved override actually forwards
    its catch-all — not blindly for every class in the MRO. Pins both
    directions: a forwarding override (``FindingHandler.get`` →
    ``super().get()``) picks up ``NumericRefHandler.get``'s params, while
    a non-forwarding override (``CitationHandler.put``, which never
    calls ``super().put()``) does NOT pick up unrelated
    ``NumericRefHandler.put`` params like ``auto_refresh_days``."""
    from precis.handlers.citation import CitationHandler
    from precis.handlers.finding import FindingHandler
    from precis.handlers.todo import TodoHandler
    from precis.runtime.dispatch import _handler_accepted_kwargs

    citation_put = _handler_accepted_kwargs(CitationHandler, "put")
    assert "source_handle" in citation_put
    assert "auto_refresh_days" not in citation_put

    finding_get = _handler_accepted_kwargs(FindingHandler, "get")
    assert {"id", "view", "q"} <= finding_get

    todo_put = _handler_accepted_kwargs(TodoHandler, "put")
    assert {"executor", "job_type", "params", "text"} <= todo_put


def test_unrecognized_top_level_kwarg_raises_bad_input(
    runtime_with_store: PrecisRuntime,
) -> None:
    """A kwarg that's on no explicit parameter anywhere in
    ``CitationHandler``'s own (non-forwarding) ``put`` signature raises,
    naming the accepted kwargs — it must not vanish into ``**_kw``."""
    out = runtime_with_store.dispatch(
        "put",
        {
            "kind": "citation",
            "source_handle": "pa1~1",
            "not_a_real_citation_kwarg": "should raise",
        },
    )
    assert "[error:BadInput]" in out
    assert "not_a_real_citation_kwarg" in out
    assert "does not accept" in out
    # The accepted-kwargs list is surfaced so the caller can self-correct.
    assert "source_handle" in out


def test_todo_put_executor_job_type_params_still_reach_meta(
    runtime_with_store: PrecisRuntime,
    store,
) -> None:
    """gr333433 fixed path, pinned at the actual dispatch boundary (not
    just ``TodoHandler.put`` called directly): the strictness gate must
    recognize these as explicit ``TodoHandler.put`` params, not reject
    them as unknown."""
    out = runtime_with_store.dispatch(
        "put",
        {
            "kind": "todo",
            "text": "run the sandbox smoke check",
            "executor": "claude_inproc",
            "job_type": "plan_tick",
            "params": {"model": "sonnet"},
        },
    )
    assert "[error:" not in out, out
    m = re.search(r"id=(\d+)", out)
    assert m is not None, out
    ref = store.get_ref(kind="todo", id=int(m.group(1)))
    assert ref is not None
    assert ref.meta.get("executor") == "claude_inproc"
    assert ref.meta.get("job_type") == "plan_tick"
    assert ref.meta.get("params") == {"model": "sonnet"}


def test_draft_edit_mode_insert_where_still_reach_the_handler(
    runtime_with_store: PrecisRuntime,
    store,
) -> None:
    """gr334153 fixed path, pinned at the actual dispatch boundary: the
    strictness gate must recognize ``mode=``/``where=`` as
    ``DraftHandler.edit``'s own explicit params, not reject them (or —
    worse — let them fall through to a stale-behaviour catch-all)."""
    proj_out = runtime_with_store.dispatch("put", {"kind": "todo", "text": "proj"})
    proj_id = int(re.search(r"id=(\d+)", proj_out).group(1))  # type: ignore[union-attr]

    draft_out = runtime_with_store.dispatch(
        "put",
        {
            "kind": "draft",
            "id": "gr334695-strictness-draft",
            "title": "Draft Title",
            "project": proj_id,
        },
    )
    assert "[error:" not in draft_out, draft_out

    anchor = "The classification remains unresolved."
    chunk_out = runtime_with_store.dispatch(
        "put",
        {
            "kind": "draft",
            "id": "gr334695-strictness-draft",
            "chunk_kind": "paragraph",
            "text": anchor,
            "at": {"last": True},
        },
    )
    assert "[error:" not in chunk_out, chunk_out
    dc = re.search(r"\bdc\d+\b", chunk_out)
    assert dc is not None, chunk_out

    edit_out = runtime_with_store.dispatch(
        "edit",
        {
            "kind": "draft",
            "id": dc.group(0),
            "mode": "insert",
            "find": anchor,
            "text": " A new sentence.",
            "where": "after",
        },
    )
    assert "[error:" not in edit_out, edit_out

    chunk = store.drafts.get_draft_chunk(dc.group(0))
    assert chunk is not None
    assert anchor in chunk.text
    assert "A new sentence." in chunk.text


def test_tolerant_handler_ignores_unrecognized_extras(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``RandomHandler.get`` is explicitly opted out
    (``@tolerates_extra_kwargs``) — an unrecognized top-level kwarg must
    still be silently ignored, exactly as before the gate existed."""
    out = runtime_with_store.dispatch(
        "get",
        {
            "kind": "random",
            "view": "slug",
            "some_unrelated_kwarg_nobody_declared": True,
        },
    )
    assert "[error:" not in out, out


def test_patent_and_edgar_put_stubs_are_marked_tolerant() -> None:
    """``PatentHandler.put``/``EdgarHandler.put`` always reject the call
    regardless of kwargs (the kind is read-only) — both are opted out of
    the strictness gate via ``@tolerates_extra_kwargs`` so a bogus kwarg
    can never preempt their domain-specific "read-only" message with a
    generic "unrecognized kwarg" ``BadInput`` instead. Checked directly
    against the marker (rather than through a live ``dispatch()`` call)
    because both kinds gate on optional third-party credentials that
    aren't configured in the test environment — that gate fires in
    ``_resolve_handler``, before ``_invoke_handler`` ever runs, so it
    can't exercise this decision anyway."""
    from precis.handlers.patent import PatentHandler
    from precis.handlers.edgar import EdgarHandler
    from precis.protocol import TOLERATES_EXTRA_KWARGS_ATTR

    assert getattr(PatentHandler.put, TOLERATES_EXTRA_KWARGS_ATTR, False) is True
    assert getattr(EdgarHandler.put, TOLERATES_EXTRA_KWARGS_ATTR, False) is True
