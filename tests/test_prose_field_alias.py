"""gr484230: ``text=``/``body=`` alias at the dispatch boundary.

The verbs advertise both spellings; each kind's handler declares one
(finding/checklist-style kinds take ``body=``, every numeric-ref and
file kind takes ``text=``). When a handler declares exactly one,
:func:`precis.runtime.dispatch._alias_prose_field` rewrites the other
spelling onto it — a caller that generalised the wrong spelling across
kinds gets the write, not a refusal. A kind whose verb declares both
(todo ``put``: ``text=`` the title, ``body=`` the details) is ambiguous
and untouched; both spellings at once on a single-spelling handler is
a ``BadInput``.
"""

from __future__ import annotations

from precis.dispatch import Hub
from precis.handlers.gripe import GripeHandler
from precis.handlers.memory import MemoryHandler
from precis.runtime import PrecisRuntime
from tests.conftest import id_of


def test_put_body_aliased_to_text_on_gripe_create(
    runtime_with_store: PrecisRuntime,
) -> None:
    body = runtime_with_store.dispatch(
        "put", {"kind": "gripe", "body": "the near-miss gripe body"}
    )
    assert "created gripe" in body and "[error" not in body


def test_put_body_aliased_to_text_on_gripe_comment(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    handler = GripeHandler(hub=hub)
    rid = id_of(handler.put(text="a gripe").body)
    body = runtime_with_store.dispatch(
        "put", {"kind": "gripe", "id": rid, "body": "a comment via body="}
    )
    assert "appended comment" in body and "[error" not in body
    assert "a comment via body=" in handler.get(id=rid).body


def test_put_text_and_body_together_is_an_error(
    runtime_with_store: PrecisRuntime,
) -> None:
    body = runtime_with_store.dispatch(
        "put",
        {"kind": "gripe", "text": "one", "body": "two"},
    )
    assert "[error:BadInput]" in body
    assert "pass one, not both" in body


def test_put_body_on_kind_declaring_both_is_not_aliased(
    runtime_with_store: PrecisRuntime,
) -> None:
    """todo put takes both spellings (text= title, body= details) — no
    rewrite, no error, both land."""
    body = runtime_with_store.dispatch(
        "put", {"kind": "todo", "text": "the title", "body": "the details"}
    )
    assert "created todo" in body and "[error" not in body


def test_edit_body_aliased_to_text_on_memory(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    handler = MemoryHandler(hub=hub)
    ref_id = id_of(handler.put(text="before text").body)
    body = runtime_with_store.dispatch(
        "edit",
        {"kind": "memory", "id": ref_id, "mode": "replace", "body": "after text"},
    )
    assert "[error" not in body
    assert "after text" in handler.get(id=ref_id).body
