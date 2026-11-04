"""Shape advice accompanies writes and owns only its derived audit flags."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest

from precis.dispatch import Hub
from precis.handlers import _memory_shape
from precis.handlers.memory import MemoryHandler
from precis.store import ActorSlug, Store, Tag
from tests.conftest import id_of

_CLEAN_TITLE = "Memory writes retain their evidence"
_CLEAN_BODY = "The evidence stays on the source ref.\nSource: memory:1"
_BAG = (
    "- Locks serialize competing writes.\n"
    "- Caches expire after a request.\n"
    "- Citations preserve source identity.\n"
    "- Threads report remaining work."
)


@pytest.fixture
def handler(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def _tags(store: Store, ref_id: int) -> set[str]:
    return {str(tag) for tag in store.tags_for(ref_id)}


@pytest.mark.parametrize(
    ("code", "title", "body", "tags", "fix"),
    [
        ("grab-bag", _CLEAN_TITLE, _BAG, [], "split into one claim"),
        (
            "unanchored-plan",
            _CLEAN_TITLE,
            "Slice 15 needs a write audit.",
            [],
            "add the owning",
        ),
        (
            "old-sha",
            _CLEAN_TITLE,
            "The evidence points at abcdef1234.",
            [],
            "replace with a live anchor",
        ),
        (
            "history-density",
            _CLEAN_TITLE,
            "The change landed.\nThe patch shipped.\n"
            "Evidence stays on the source ref.\nThe source has a stable handle.",
            [],
            "move still-true claims",
        ),
        (
            "missing-route",
            _CLEAN_TITLE,
            "Memory writes always retain evidence.",
            ["section:reference"],
            "add a cites/derived-from",
        ),
        (
            "title-shape",
            "docs/memory.md",
            _CLEAN_BODY,
            [],
            "state one claim",
        ),
    ],
)
def test_shape_write_advises_then_clean_rewrite_clears(
    handler: MemoryHandler,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    title: str,
    body: str,
    tags: list[str],
    fix: str,
) -> None:
    monkeypatch.setenv("PRECIS_MEMORY_ATTRIBUTION_GATE", "reject")
    monkeypatch.setattr(
        _memory_shape, "_commit_times", lambda shas: tuple((sha, 0) for sha in shas)
    )
    ack = handler.put(title=title, text=body, tags=tags)
    ref_id = id_of(ack.body)
    assert f"AUDIT:{code}:" in ack.body
    assert fix in ack.body
    assert f"AUDIT:{code}" in _tags(store, ref_id)
    assert handler._body_text(handler._resolve_live_ref(ref_id)) == body

    cleaned = handler.edit(
        id=ref_id, mode="replace", title=_CLEAN_TITLE, text=_CLEAN_BODY
    )
    assert f"AUDIT:{code}:" not in cleaned.body
    assert not any(tag.startswith("AUDIT:") for tag in _tags(store, ref_id))


@pytest.mark.parametrize(
    "edit",
    [
        {"mode": "replace", "text": "Slice 15 needs a write audit."},
        {"find": "The evidence", "text": "Slice 15 evidence"},
        {
            "mode": "insert",
            "find": "The evidence",
            "text": "Slice 15: ",
            "where": "before",
        },
        {"mode": "append", "text": "Slice 15 needs a write audit."},
        {"ops": [{"find": "The evidence", "text": "Slice 15 evidence"}]},
        {"title": "Slice 15 needs a write audit"},
    ],
)
def test_each_edit_door_advises_and_tags(
    handler: MemoryHandler, store: Store, edit: dict[str, Any]
) -> None:
    ref_id = id_of(handler.put(title=_CLEAN_TITLE, text=_CLEAN_BODY).body)
    ack = handler.edit(id=ref_id, **edit)
    assert "AUDIT:unanchored-plan:" in ack.body
    assert "AUDIT:unanchored-plan" in _tags(store, ref_id)


def test_delete_span_checks_resulting_body(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = id_of(
        handler.put(title=_CLEAN_TITLE, text="Slice 15 me1 needs a write audit.").body
    )
    assert "AUDIT:unanchored-plan" not in _tags(store, ref_id)
    ack = handler.edit(id=ref_id, mode="delete", find="me1 ")
    assert "AUDIT:unanchored-plan:" in ack.body
    assert "AUDIT:unanchored-plan" in _tags(store, ref_id)


def test_shape_codes_coexist_with_attribution(
    handler: MemoryHandler, store: Store
) -> None:
    source = id_of(
        handler.put(title="Evidence has no figures", text="No figures.").body
    )
    body = f"- Slice 15 remains open.\n- The scale is 5 nm per memory:{source}."
    ack = handler.put(title="Two claims need evidence", text=body)
    tags = _tags(store, id_of(ack.body))
    assert {
        "AUDIT:grab-bag",
        "AUDIT:unanchored-plan",
        "AUDIT:ungrounded-number",
    } <= tags


def test_clean_rewrite_preserves_author_flags(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = id_of(handler.put(title=_CLEAN_TITLE, text=_CLEAN_BODY).body)
    handler.tag(id=ref_id, add=["AUDIT:grab-bag"])
    handler.edit(id=ref_id, mode="replace", text=_CLEAN_BODY)
    assert "AUDIT:grab-bag" in _tags(store, ref_id)


def test_matching_audit_preserves_author_ownership_before_clean_rewrite(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = id_of(handler.put(title=_CLEAN_TITLE, text=_CLEAN_BODY).body)
    handler.tag(id=ref_id, add=["AUDIT:grab-bag"])
    dirty = handler.edit(id=ref_id, mode="replace", text=_BAG)
    assert "AUDIT:grab-bag:" in dirty.body
    with store.tx() as conn:
        row = conn.execute(
            "SELECT rt.set_by FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = 'AUDIT' AND t.value = 'grab-bag'",
            (ref_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "agent"

    clean = handler.edit(id=ref_id, mode="replace", text=_CLEAN_BODY)
    assert "AUDIT:grab-bag:" not in clean.body
    assert "AUDIT:grab-bag" in _tags(store, ref_id)


@pytest.mark.parametrize("set_by", ["system", "dream"])
def test_non_agent_creation_skips_shape_audit(
    handler: MemoryHandler,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    set_by: ActorSlug,
) -> None:
    audit = Mock(side_effect=AssertionError("non-agent writes skip shape checks"))
    monkeypatch.setattr("precis.handlers.memory.audit_shape", audit)
    ack, ref_id = handler._create_with_id(
        text=_BAG,
        tags=[],
        link=None,
        put_meta={"title": "memory_snapshot"},
        set_by=set_by,
    )
    audit.assert_not_called()
    assert "AUDIT:" not in ack.body
    assert not any(tag.startswith("AUDIT:") for tag in _tags(store, ref_id))
    assert handler._body_text(handler._resolve_live_ref(ref_id)) == _BAG


def test_unavailable_check_preserves_flags(
    handler: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref_id = id_of(handler.put(title=_CLEAN_TITLE, text="Slice 15 is open.").body)

    def unavailable(**_kwargs: Any) -> None:
        raise RuntimeError("shape check unavailable")

    monkeypatch.setattr("precis.handlers.memory.audit_shape", unavailable)
    ack = handler.edit(id=ref_id, mode="replace", text=_CLEAN_BODY)
    assert "replaced body" in ack.body
    assert "AUDIT:unanchored-plan" in _tags(store, ref_id)


def test_git_unavailable_preserves_old_sha_flag(
    handler: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref_id = id_of(handler.put(title=_CLEAN_TITLE, text=_CLEAN_BODY).body)
    store.add_tag(ref_id, Tag.closed("AUDIT", "old-sha"), set_by="system")

    def unavailable(_shas: tuple[str, ...]) -> tuple[tuple[str, int], ...]:
        raise FileNotFoundError("git unavailable")

    monkeypatch.setattr(_memory_shape, "_commit_times", unavailable)
    handler.edit(id=ref_id, mode="replace", text="The evidence points at abcdef1234.")
    assert "AUDIT:old-sha" in _tags(store, ref_id)


def test_dry_runs_propose_titles_without_writing(
    handler: MemoryHandler, store: Store
) -> None:
    preview = handler.put(title=_CLEAN_TITLE, text=_BAG, dry_run=True)
    assert "AUDIT:grab-bag:" in preview.body
    assert "Proposed atomic titles:" in preview.body
    assert "Locks serialize competing writes" in preview.body
    ref_id = id_of(handler.put(title=_CLEAN_TITLE, text=_CLEAN_BODY).body)
    preview = handler.edit(id=ref_id, mode="append", text=_BAG, dry_run=True)
    assert "Proposed atomic titles:" in preview.body
    assert handler._body_text(handler._resolve_live_ref(ref_id)) == _CLEAN_BODY
    assert "AUDIT:grab-bag" not in _tags(store, ref_id)


def test_evidence_edge_clears_missing_route(
    handler: MemoryHandler, store: Store
) -> None:
    source = id_of(
        handler.put(title="Evidence has a stable handle", text="Evidence.").body
    )
    ref_id = id_of(
        handler.put(
            title=_CLEAN_TITLE,
            text="Memory writes always retain evidence.",
            tags=["section:reference"],
        ).body
    )
    assert "AUDIT:missing-route" in _tags(store, ref_id)
    store.add_link(src_ref_id=ref_id, dst_ref_id=source, relation="cites")
    handler.edit(
        id=ref_id, mode="replace", text="Memory writes always retain evidence."
    )
    assert "AUDIT:missing-route" not in _tags(store, ref_id)


def test_shape_tag_failure_rolls_back_only_the_savepoint(
    handler: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_tag = store.add_tag

    def broken_shape_tag(ref_id: int, tag: Tag, **kwargs: Any) -> None:
        if tag.prefix == "AUDIT" and tag.value == "unanchored-plan":
            kwargs["conn"].execute("SELECT missing_memory_shape_column")
        else:
            add_tag(ref_id, tag, **kwargs)

    monkeypatch.setattr(store, "add_tag", broken_shape_tag)
    ack = handler.put(title=_CLEAN_TITLE, text="Slice 15 is open.")
    ref_id = id_of(ack.body)
    assert handler._body_text(handler._resolve_live_ref(ref_id)) == "Slice 15 is open."
    assert "AUDIT:unanchored-plan" not in _tags(store, ref_id)


@pytest.mark.parametrize(
    ("concurrent_edit", "code"),
    [
        ({"title": "docs/memory.md"}, "title-shape"),
        ({"meta": {"type": "reference"}}, "missing-route"),
    ],
)
def test_body_write_audits_title_and_meta_committed_after_preflight(
    handler: MemoryHandler,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    concurrent_edit: dict[str, Any],
    code: str,
) -> None:
    """A stale clean snapshot must not clear the other writer's audit flag."""
    body = "Memory writes always retain evidence."
    ref_id = id_of(handler.put(title=_CLEAN_TITLE, text=body).body)
    write_body = handler._write_body

    def interleaved(*args: Any, **kwargs: Any) -> str | None:
        handler.edit(id=ref_id, **concurrent_edit)
        assert f"AUDIT:{code}" in _tags(store, ref_id)
        return write_body(*args, **kwargs)

    monkeypatch.setattr(handler, "_write_body", interleaved)
    ack = handler.edit(id=ref_id, mode="replace", text=body)
    assert f"AUDIT:{code}:" in ack.body
    assert f"AUDIT:{code}" in _tags(store, ref_id)


def test_title_write_audits_body_committed_after_preflight(
    handler: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref_id = id_of(handler.put(title=_CLEAN_TITLE, text=_CLEAN_BODY).body)
    write_retried = handler._write_retried

    def interleaved(ref: int, what: str, fn: Any) -> Any:
        if what == "meta edit":
            handler.edit(id=ref_id, mode="replace", text="Slice 15 remains open.")
            assert "AUDIT:unanchored-plan" in _tags(store, ref_id)
        return write_retried(ref, what, fn)

    monkeypatch.setattr(handler, "_write_retried", interleaved)
    ack = handler.edit(id=ref_id, title=_CLEAN_TITLE)
    assert "AUDIT:unanchored-plan:" in ack.body
    assert "AUDIT:unanchored-plan" in _tags(store, ref_id)


def test_locked_recheck_does_not_repeat_git_lookup(
    handler: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref_id = id_of(handler.put(title=_CLEAN_TITLE, text=_CLEAN_BODY).body)
    calls: list[tuple[str, ...]] = []

    def lookup(shas: tuple[str, ...]) -> tuple[tuple[str, int], ...]:
        calls.append(shas)
        return tuple((sha, 0) for sha in shas)

    monkeypatch.setattr(_memory_shape, "_commit_times", lookup)
    ack = handler.edit(
        id=ref_id, mode="replace", text="The evidence points at abcdef1234."
    )
    assert len(calls) == 1
    assert "AUDIT:old-sha:" in ack.body
    assert "AUDIT:old-sha" in _tags(store, ref_id)


def test_changed_git_context_preserves_concurrent_old_sha_flag(
    handler: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref_id = id_of(handler.put(title=_CLEAN_TITLE, text=_CLEAN_BODY).body)
    monkeypatch.setattr(
        _memory_shape, "_commit_times", lambda shas: tuple((sha, 0) for sha in shas)
    )
    write_body = handler._write_body

    def interleaved(*args: Any, **kwargs: Any) -> str | None:
        handler.edit(id=ref_id, title="The evidence uses abcdef1234")
        assert "AUDIT:old-sha" in _tags(store, ref_id)
        return write_body(*args, **kwargs)

    monkeypatch.setattr(handler, "_write_body", interleaved)
    handler.edit(id=ref_id, mode="replace", text=_CLEAN_BODY)
    assert "AUDIT:old-sha" in _tags(store, ref_id)
