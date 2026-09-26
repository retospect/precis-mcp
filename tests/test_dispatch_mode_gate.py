"""gr343755: the dispatch-boundary ``mode=`` gate
(:meth:`precis.runtime.dispatch.DispatchMixin._validate_mode`) reads
``KindSpec.modes``/``edit_modes`` and rejects a caller-supplied ``mode=``
*before* the handler ever runs, for the three sentinel states:

- non-empty tuple (``job``: ``modes=('retry',)``) — membership check.
- ``()`` (``message``: ``modes=()``) — every value rejected.
- ``None`` (``todo``: ``modes=None``, the default) — untouched; the
  handler's own (pre-existing) behaviour is unaffected.

Pinned at the real ``PrecisRuntime.dispatch`` boundary (not by calling
the handler method directly) so a future refactor of ``_invoke_handler``
can't silently drop the gate.
"""

from __future__ import annotations

from precis.runtime import PrecisRuntime


def test_job_put_rejects_a_mode_outside_its_one_element_vocabulary(
    runtime_with_store: PrecisRuntime,
) -> None:
    out = runtime_with_store.dispatch(
        "put", {"kind": "job", "mode": "bogus", "job_type": "fix_gripe"}
    )
    assert "[error:BadInput]" in out
    assert "only supports mode=" in out
    assert "'retry'" in out


def test_job_put_mode_retry_passes_the_gate_and_reaches_the_handler(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``mode='retry'`` is in job's declared vocabulary, so the gate lets
    it through — the resulting error (if any) must come from the
    handler's own retry logic (``requires id=``), never from the gate's
    "only supports mode=" message."""
    out = runtime_with_store.dispatch("put", {"kind": "job", "mode": "retry"})
    assert "only supports mode=" not in out
    assert "requires id=" in out


def test_message_put_rejects_any_supplied_mode(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``message``'s ``modes=()``: recognised but every value is
    rejected — distinct from ``job``'s membership-checked vocabulary."""
    out = runtime_with_store.dispatch(
        "put",
        {
            "kind": "message",
            "mode": "anything",
            "text": "hi",
            "target": "discord/1/2/3",
        },
    )
    assert "[error:BadInput]" in out
    assert "mode= is not accepted" in out


def test_message_put_without_mode_is_unaffected(
    runtime_with_store: PrecisRuntime,
) -> None:
    """Omitting ``mode=`` entirely never trips the gate — only a
    caller-supplied value does."""
    out = runtime_with_store.dispatch(
        "put",
        {"kind": "message", "text": "hi", "target": "discord/1/2/3"},
    )
    assert "mode= is not accepted" not in out
    assert "[error:" not in out, out


def test_todo_put_mode_none_defers_to_existing_handler_behaviour(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``todo``'s ``modes`` is the ``None`` default (no ``mode=``
    concept declared) — the gate is a no-op, so a supplied ``mode=``
    surfaces whatever the handler already did before gr343755 (the
    base ``NumericRefHandler.put`` hand-rolled rejection), not the
    gate's own message."""
    out = runtime_with_store.dispatch(
        "put", {"kind": "todo", "mode": "bogus", "text": "hi"}
    )
    assert "[error:BadInput]" in out
    assert "only supports mode=" not in out


def test_todo_edit_mode_replace_still_works_through_the_wrapper_default(
    runtime_with_store: PrecisRuntime,
) -> None:
    """Regression guard for the wrapper-default trap
    (``_EDIT_MODE_WRAPPER_DEFAULT``): the gate must not fire on
    ``mode='find-replace'`` for a kind (todo) whose declared
    ``edit_modes`` doesn't include it — that value is
    ``tools/core.py::edit``'s own non-``None`` default, indistinguishable
    from "caller never mentioned mode=" at the dispatch layer, and must
    defer to the handler's own ``require_mode`` (and its meta-only
    bypass, gr439934) rather than reject blind."""
    create_out = runtime_with_store.dispatch(
        "put", {"kind": "todo", "text": "gr343755 gate regression todo"}
    )
    assert "[error:" not in create_out, create_out
    import re

    m = re.search(r"id=(\d+)", create_out)
    assert m is not None, create_out
    todo_id = int(m.group(1))

    # meta-only park path: no text=/body=, so mode= is irrelevant to this
    # call — must succeed even though the wrapper always fills mode= in
    # with 'find-replace', which is outside todo's own ('replace',)
    # vocabulary.
    park_out = runtime_with_store.dispatch(
        "edit",
        {
            "kind": "todo",
            "id": todo_id,
            "mode": "find-replace",
            "meta": {"llm_tier": None},
        },
    )
    assert "[error:" not in park_out, park_out
