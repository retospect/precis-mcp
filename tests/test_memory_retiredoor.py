"""Retirement owns prose mentions even when no mention edge was materialised."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest
from psycopg import Connection

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.graph.garden.candidates import dangling_mention_candidates
from precis.handlers.md import MdHandler
from precis.handlers.memory import MemoryHandler
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store
from precis.utils.handle_registry import format_handle
from tests._refs import insert_gripe


def _memory(store: Store, body: str, *, title: str = "Retirement fixture") -> int:
    """Insert the prose directly: these references deliberately have no edges."""
    ref = store.insert_ref(kind="memory", slug=None, title=title)
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})]
    )
    return ref.id


def _snapshot(store: Store, ref_id: int) -> tuple[Any, ...]:
    with store.pool.connection() as conn:
        ref = conn.execute(
            "SELECT retired_at, meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
        chunks = conn.execute(
            "SELECT chunk_id, text, meta FROM chunks WHERE ref_id = %s "
            "ORDER BY chunk_id",
            (ref_id,),
        ).fetchall()
        links = conn.execute(
            "SELECT * FROM links WHERE src_ref_id = %s OR dst_ref_id = %s "
            "ORDER BY link_id",
            (ref_id, ref_id),
        ).fetchall()
        events = conn.execute(
            "SELECT * FROM ref_events WHERE ref_id = %s ORDER BY event_id", (ref_id,)
        ).fetchall()
    return ref, chunks, links, events


def _edit_calls(ack: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for line in ack.splitlines():
        start = line.find("edit(")
        if start < 0:
            continue
        expression = ast.parse(line[start:].strip().strip("`"), mode="eval").body
        assert isinstance(expression, ast.Call)
        values: dict[str, Any] = {}
        for keyword in expression.keywords:
            assert keyword.arg is not None
            value = keyword.value
            values[keyword.arg] = (
                value.id if isinstance(value, ast.Name) else ast.literal_eval(value)
            )
        calls.append(values)
    return calls


def test_unlinked_live_mention_refuses_with_both_ways_out(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old truth.")
    sentence = f"Use me{old} for the current build procedure."
    source = _memory(store, sentence)
    before = _snapshot(store, old)
    assert not store.links_for(source)

    with pytest.raises(BadInput) as exc:
        handler.delete(id=old)

    refusal = str(exc.value) + "\n" + str(exc.value.next)
    assert f"me{source}" in refusal
    assert sentence in refusal
    assert "superseded_by" in refusal and "force" in refusal
    assert _snapshot(store, old) == before


def test_force_lists_sentence_and_leaves_successorless_candidate(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old truth.")
    sentence = f"Use me{old} for the current build procedure."
    source = _memory(store, "Unrelated first sentence. " + sentence)

    ack = handler.delete(id=old, force=True, reason="The procedure is obsolete").body

    assert f"me{source}" in ack
    calls = _edit_calls(ack)
    assert len(calls) == 1
    call = calls[0]
    assert call["kind"] == "memory"
    assert str(call["id"]) in {str(source), f"me{source}"}
    assert call["find"] == sentence
    assert call["text"] == "<write the current truth in>"
    assert re.search(rf"me{old} retired \d{{4}}-\d{{2}}-\d{{2}}", call["reason"])
    assert "The procedure is obsolete" in call["reason"]
    ref = store.fetch_refs_by_ids([old])[old]
    assert ref.retired_at is not None
    assert not ref.meta.get("superseded_by")
    candidates = dangling_mention_candidates(store)
    assert [candidate.target_id for candidate in candidates] == [source]
    assert candidates[0].evidence["mentions"][0]["successor"] is None


def test_delete_requires_id_with_next_step(hub: Hub) -> None:
    with pytest.raises(BadInput, match="delete requires id=") as exc:
        MemoryHandler(hub=hub).delete()

    assert exc.value.next == "delete(kind='memory', id=N)"


@pytest.mark.parametrize("id_form", ["handle", "integer", "numeric_string"])
def test_inbound_successor_stamps_handle_and_redirects(
    hub: Hub, runtime_with_store: PrecisRuntime, id_form: str
) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Obsolete retirement content.")
    successor = _memory(store, "Replacement content is current.")
    source = _memory(store, f"Use me{old} for builds.")
    store.add_link(src_ref_id=successor, dst_ref_id=old, relation="supersedes")

    ack = handler.delete(id=old).body

    assert f"me{source}" in ack
    assert store.fetch_refs_by_ids([old])[old].meta["superseded_by"] == f"me{successor}"
    assert store.follow_supersede(old) == successor
    resolved = store.resolve_handle(f"me{old}")
    assert resolved is not None
    assert resolved.ref_id == successor and resolved.redirected_from == f"me{old}"
    id_value = {"handle": f"me{old}", "integer": old, "numeric_string": str(old)}[
        id_form
    ]
    body, failed = runtime_with_store.dispatch_with_status(
        "get", {"kind": "memory", "id": id_value}
    )
    assert not failed, body
    assert "Replacement content is current." in body
    retired, failed = runtime_with_store.dispatch_with_status(
        "get", {"kind": "memory", "id": id_value, "view": "retired"}
    )
    assert not failed, retired
    assert f"me{old} — retired" in retired
    assert "Obsolete retirement content." in retired


@pytest.mark.parametrize("kind", ["todo", "skill", "gripe"])
@pytest.mark.parametrize("id_form", ["handle", "integer", "numeric_string"])
@pytest.mark.parametrize("memory_options", [False, True])
def test_explicit_successor_can_be_another_kind(
    hub: Hub,
    runtime_with_store: PrecisRuntime,
    kind: str,
    id_form: str,
    memory_options: bool,
) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Absorbed old memory content.")
    source = _memory(store, f"The current record is me{old}.")
    if kind == "gripe":
        successor = insert_gripe(store, "Current successor record")
    else:
        successor = store.insert_ref(
            kind=kind,
            slug="precis-memory-help" if kind == "skill" else None,
            title="Current successor record",
        )
    handle = format_handle(kind, successor.id)

    ack = handler.delete(id=old, superseded_by=handle).body

    assert f"me{source}" in ack and handle in ack
    assert store.fetch_refs_by_ids([old])[old].meta["superseded_by"] == handle
    assert store.follow_supersede(old) == successor.id
    resolved = store.resolve_handle(f"me{old}")
    assert resolved is not None
    assert (resolved.kind, resolved.ref_id) == (kind, successor.id)
    id_value = {"handle": f"me{old}", "integer": old, "numeric_string": str(old)}[
        id_form
    ]
    options = (
        {"view": "fisheye+1hop+recall", "extent": "fisheye+1hop", "q": "memory:build"}
        if memory_options
        else {}
    )
    body, failed = runtime_with_store.dispatch_with_status(
        "get", {"kind": "memory", "id": id_value, **options}
    )
    assert not failed, body
    expected = "precis-memory-help" if kind == "skill" else "Current successor record"
    assert expected in body
    if memory_options:
        assert body.startswith(
            f"Dropped memory read options view, extent, q when redirecting to {kind}."
        )
    else:
        assert "Dropped memory read options" not in body


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("stored_successor", [False, True])
def test_multiple_inbound_successors_require_explicit_choice(
    hub: Hub, dry_run: bool, stored_successor: bool
) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old procedure.")
    first = _memory(store, "First successor.")
    second = _memory(store, "Second successor.")
    for successor in (first, second):
        store.add_link(src_ref_id=successor, dst_ref_id=old, relation="supersedes")
    if stored_successor:
        store.stamp_ref_meta(old, {"superseded_by": f"me{first}"})
    before = _snapshot(store, old)

    with pytest.raises(BadInput, match="multiple live successors") as exc:
        handler.delete(id=old, dry_run=dry_run)

    assert f"superseded_by='me{first}'" in str(exc.value.next)
    assert f"superseded_by='me{second}'" in str(exc.value.next)
    assert _snapshot(store, old) == before


def test_explicit_successor_preserves_supersedes_edges_on_tombstone(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old procedure.")
    first = _memory(store, "First successor.")
    second = _memory(store, "Second successor.")
    neighbour = _memory(store, "Related procedure.")
    ancestor = _memory(store, "Earlier procedure.")
    for successor in (first, second):
        store.add_link(src_ref_id=successor, dst_ref_id=old, relation="supersedes")
    store.add_link(src_ref_id=old, dst_ref_id=ancestor, relation="supersedes")
    store.add_link(src_ref_id=old, dst_ref_id=neighbour, relation="related-to")
    before = store.links_for(old, relation="supersedes")

    handler.delete(id=old, superseded_by=f"me{first}")

    assert store.fetch_refs_by_ids([old])[old].retired_at is not None
    assert store.links_for(old, relation="supersedes") == before
    for successor in (first, second):
        edges = store.links_for(successor, direction="out", relation="supersedes")
        assert {edge.dst_ref_id for edge in edges} == {old}
    related = store.links_for(first, direction="out", relation="related-to")
    assert {edge.dst_ref_id for edge in related} == {neighbour}
    assert not store.links_for(old, relation="related-to")


@pytest.mark.parametrize(
    "body",
    [
        "2026-10-01: We used {handle} during migration.",
        "## Earlier\nWe used {handle} during migration.",
        "Retired here:\nWe used {handle} during migration.",
    ],
)
def test_dated_history_is_reported_but_does_not_refuse(hub: Hub, body: str) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Historical record.")
    source = _memory(store, body.format(handle=f"me{old}"))

    ack = handler.delete(id=old).body

    assert f"me{source}" in ack and f"me{old}" in ack
    assert "history" in ack.lower()
    assert store.fetch_refs_by_ids([old])[old].retired_at is not None


def test_history_does_not_exempt_a_current_mention(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Historical record.")
    _memory(
        store,
        f"## Earlier\nWe used me{old}.\n\n## Current\nUse me{old} for builds.",
    )

    with pytest.raises(BadInput):
        handler.delete(id=old)
    assert store.fetch_refs_by_ids([old])[old].retired_at is None


def test_dry_run_is_read_only_and_real_ack_has_same_repairs(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old record.")
    successor = _memory(store, "Current record.")
    source = _memory(store, f"Use me{old} for builds.")
    store.add_link(src_ref_id=source, dst_ref_id=old, relation="related-to")
    before = _snapshot(store, old)

    preview = handler.delete(id=old, superseded_by=f"me{successor}", dry_run=True)

    assert _snapshot(store, old) == before
    assert "dry" in preview.body.lower()
    assert f"me{source}" in preview.body
    retired = handler.delete(id=old, superseded_by=f"me{successor}")
    assert _edit_calls(preview.body) == _edit_calls(retired.body)


def test_dry_run_without_successor_reports_blockers_without_refusing(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old record.")
    source = _memory(store, f"Use me{old} for builds.")
    before = _snapshot(store, old)

    preview = handler.delete(id=old, dry_run=True).body

    assert f"me{source}" in preview
    assert "superseded_by" in preview and "force" in preview
    assert _snapshot(store, old) == before


def test_failure_rolls_back_tombstone_metadata_links_and_events(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old record.")
    successor = _memory(store, "Current record.")
    source = _memory(store, f"Use me{old} for builds.")
    store.add_link(src_ref_id=source, dst_ref_id=old, relation="related-to")
    before = _snapshot(store, old)
    retire = store.retire_ref

    def fail_after_retire(ref_id: int, *, conn: Connection | None = None) -> None:
        assert conn is not None, "retirement must share the mention-check transaction"
        retire(ref_id, conn=conn)
        raise RuntimeError("retiredoor rollback fixture")

    monkeypatch.setattr(store, "retire_ref", fail_after_retire)
    with pytest.raises(RuntimeError, match="retiredoor rollback fixture"):
        handler.delete(id=old, superseded_by=f"me{successor}", reason="Absorbed")
    assert _snapshot(store, old) == before


def test_scan_ignores_retired_memories_other_kinds_and_card_chunks(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old record.")
    dead_source = _memory(store, f"Use me{old} for builds.")
    store.retire_ref(dead_source)
    card_source = _memory(store, f"Partial xme{old} and wrong-kind pa{old}.")
    store.chunks.insert_chunks(
        card_source,
        [ChunkInsert(ord=-1, text=f"me{old}", meta={"chunk_kind": "card_combined"})],
    )
    other = store.insert_ref(kind="todo", slug=None, title="Other kind")
    store.chunks.insert_chunks(
        other.id,
        [ChunkInsert(ord=0, text=f"me{old}", meta={"chunk_kind": "memory_body"})],
    )

    ack = handler.delete(id=old).body

    assert not _edit_calls(ack)
    assert store.fetch_refs_by_ids([old])[old].retired_at is not None


def test_report_cap_counts_omitted_nodes_without_hiding_blockers(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.handlers import _memory_retire

    monkeypatch.setattr(_memory_retire, "MENTION_REPORT_CAP", 2)
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old record.")
    for _ in range(3):
        _memory(store, f"2026-10-01: We used me{old} for builds.")
    _memory(store, f"Use me{old} for current builds.")

    preview = handler.delete(id=old, dry_run=True).body

    assert "+2 more" in preview
    assert len(_edit_calls(preview)) == 2
    with pytest.raises(BadInput):
        handler.delete(id=old)
    assert store.fetch_refs_by_ids([old])[old].retired_at is None


@pytest.mark.parametrize("successor", ["self", "missing", "retired", "wrong_kind"])
def test_invalid_successor_refuses_before_any_write(hub: Hub, successor: str) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old record.")
    dead = _memory(store, "Dead successor.")
    store.retire_ref(dead)
    handle = {
        "self": f"me{old}",
        "missing": "me9223372036854775807",
        "retired": f"me{dead}",
        "wrong_kind": f"td{old}",
    }[successor]
    before = _snapshot(store, old)

    with pytest.raises(BadInput):
        handler.delete(id=old, superseded_by=handle)
    assert _snapshot(store, old) == before


def test_prefilled_edit_preserves_sentence_quotes_and_backslashes(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old record.")
    sentence = f"""The builder's "current" path \\build follows me{old}."""
    _memory(store, sentence)

    preview = handler.delete(id=old, dry_run=True).body

    (call,) = _edit_calls(preview)
    assert call["find"] == sentence


@pytest.mark.parametrize("handle_id", [False, True])
@pytest.mark.parametrize(
    "options",
    [
        {},
        {"view": "fisheye+1hop+recall"},
        {"extent": "fisheye+1hop+recall", "q": "memory:build"},
        {"view": "fisheye+1hop+recall", "extent": "fisheye", "q": "memory:build"},
    ],
)
def test_md_section_successor_redirects_and_preserves_tombstone(
    runtime_with_store: PrecisRuntime,
    tmp_path: Path,
    handle_id: bool,
    options: dict[str, str],
) -> None:
    runtime = runtime_with_store
    store, handler = runtime.live_store, MemoryHandler(hub=runtime.hub)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "example.md").write_text(
        "# Build procedure\n\n## Current\nThe replacement build procedure.\n"
        "\n## Other\nAn unrelated procedure.\n",
        encoding="utf-8",
    )
    MdHandler(hub=runtime.hub, roots={"main": tmp_path})._register_with(runtime.hub)
    old = _memory(store, "Obsolete procedure kept for audit.")
    source = _memory(store, f"Use me{old} for builds.")
    address = "md:main/docs/example.md~current"

    ack = handler.delete(id=old, superseded_by=address).body

    assert address in ack and f"me{source}" in ack
    assert store.fetch_refs_by_ids([old])[old].meta["superseded_by"] == address
    assert store.follow_supersede_address(old) == address
    assert store.follow_supersede(old) is None
    (candidate,) = dangling_mention_candidates(store)
    assert candidate.target_id == source
    assert candidate.evidence["mentions"][0]["successor"] == address
    id_value: str | int = f"me{old}" if handle_id else old
    # Every read must report discarded options, including repeated requests.
    for _ in range(2):
        body, failed = runtime.dispatch_with_status(
            "get", {"kind": "memory", "id": id_value, **options}
        )
        assert not failed, body
        assert "The replacement build procedure." in body
        assert "An unrelated procedure." not in body
        assert "Obsolete procedure kept for audit." not in body
        if options:
            assert body.startswith(
                f"Dropped memory read options {', '.join(options)} when redirecting to md."
            )
        else:
            assert "Dropped memory read options" not in body
    retired, failed = runtime.dispatch_with_status(
        "get", {"kind": "memory", "id": id_value, "view": "retired"}
    )
    assert not failed, retired
    assert "Obsolete procedure kept for audit." in retired
    assert address in retired


def test_md_successor_requires_an_existing_section(
    runtime_with_store: PrecisRuntime, tmp_path: Path
) -> None:
    runtime = runtime_with_store
    store, handler = runtime.live_store, MemoryHandler(hub=runtime.hub)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "example.md").write_text("# Current\nLive procedure.\n", encoding="utf-8")
    MdHandler(hub=runtime.hub, roots={"main": tmp_path})._register_with(runtime.hub)
    old = _memory(store, "Old procedure.")
    before = _snapshot(store, old)

    with pytest.raises(NotFound):
        handler.delete(id=old, superseded_by="md:main/docs/example.md~missing")
    assert _snapshot(store, old) == before


def test_earlier_in_ordinary_prose_is_not_a_history_marker(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old procedure.")
    _memory(store, f"Earlier guidance is obsolete.\nUse me{old} for current builds.")

    with pytest.raises(BadInput):
        handler.delete(id=old)
    assert store.fetch_refs_by_ids([old])[old].retired_at is None


def test_prefilled_edit_preserves_a_multiline_sentence(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old procedure.")
    sentence = f"Use me{old} for the\ncurrent build procedure."
    _memory(store, "Unrelated first sentence. " + sentence + " Another thought.")

    preview = handler.delete(id=old, dry_run=True).body

    (call,) = _edit_calls(preview)
    assert call["find"] == sentence


@pytest.mark.parametrize("legacy_first", [False, True])
def test_successor_chain_accepts_full_handles_and_legacy_numeric_ids(
    store: Store, legacy_first: bool
) -> None:
    old = _memory(store, "Old procedure.")
    middle = _memory(store, "Intermediate procedure.")
    current = _memory(store, "Current procedure.")
    store.stamp_ref_meta(
        old, {"superseded_by": middle if legacy_first else f"me{middle}"}
    )
    store.stamp_ref_meta(
        middle, {"superseded_by": f"me{current}" if legacy_first else current}
    )
    store.retire_ref(old)
    store.retire_ref(middle)

    assert store.follow_supersede(old) == current
    assert store.follow_supersede_address(old) == f"me{current}"
    resolved = store.resolve_handle(f"me{old}")
    assert resolved is not None
    assert resolved.ref_id == current
    assert resolved.redirected_from == f"me{old}"
    assert store.resolve_handle(f"pa{old}") is None


def test_full_handle_successor_cycle_does_not_redirect(store: Store) -> None:
    first = _memory(store, "First obsolete procedure.")
    second = _memory(store, "Second obsolete procedure.")
    store.stamp_ref_meta(first, {"superseded_by": f"me{second}"})
    store.stamp_ref_meta(second, {"superseded_by": f"me{first}"})
    store.retire_ref(first)
    store.retire_ref(second)

    assert store.follow_supersede_address(first) is None
    assert store.follow_supersede(first) is None
    assert store.resolve_handle(f"me{first}") is None


def test_retirement_ignores_live_corpus_size(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old procedure.")
    _memory(store, "First other live memory, without a mention.")
    _memory(store, "Second other live memory, without a mention.")
    successor = _memory(store, "Third other live memory, without a mention.")
    source = _memory(store, f"Use me{old} for the current build procedure.")
    before = _snapshot(store, old)

    preview = handler.delete(id=old, dry_run=True).body

    assert f"me{source}" in preview
    assert _snapshot(store, old) == before
    handler.delete(id=old, superseded_by=f"me{successor}")
    assert store.fetch_refs_by_ids([old])[old].retired_at is not None


def test_mention_forms_found_through_word_index(hub: Hub) -> None:
    """Whole-token handle forms reach the receipt; glued compounds do not."""
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old procedure.")
    found = [
        f"Use me{old} for the current build procedure.",
        f"Bracketed [me{old}] still counts.",
        f"Parenthesised (me{old}) still counts.",
        f"Backticked `me{old}` still counts.",
        f'Quoted "me{old}" still counts.',
        f"The selector form me{old}~3 still counts.",
        f"The possessive me{old}'s still counts.",
        f"The hyphenated me{old}-based approach counts.",
        f"The addressed foo@me{old} still counts.",
        f"One leading zero me0{old} still counts.",
        f"Two leading zeros me00{old} still counts.",
        f"me{old} may start a line.",
        f"A line may end with me{old}",
    ]
    # Word-index gaps: each compound is one lexeme, so a glued handle is
    # never a candidate even though the sentence verifier would count it.
    gaps = [
        f"See docs/me{old} for the procedure.",
        f"See me{old}.md for the procedure.",
        f"See src/me{old}.py for the procedure.",
        f"See https://example.com/me{old} for the procedure.",
    ]
    negatives = [
        f"Glued digit me{old}9 names a different handle.",
        f"Glued prefix xme{old} names a different handle.",
        f"Glued plural me{old}s names a different handle.",
        # The index nominates x_me{old} (underscore splits the token);
        # the verifier rejects it, so it stays off the receipt.
        f"Underscore prefix x_me{old} names a different handle.",
    ]
    reported = [_memory(store, body) for body in found]
    silent = [_memory(store, body) for body in [*gaps, *negatives]]

    preview = handler.delete(id=old, dry_run=True).body

    assert f"Also update: {len(reported)} mentioning" in preview
    for ref_id in reported:
        assert f"me{ref_id}:" in preview
    for ref_id in silent:
        assert f"me{ref_id}:" not in preview


def test_repeated_sentence_anchor_selects_current_occurrence(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old procedure.")
    sentence = f"Use me{old} for builds."
    _memory(store, f"## Earlier\n{sentence}\n\n## Current\n{sentence}\n")

    preview = handler.delete(id=old, dry_run=True).body

    (call,) = _edit_calls(preview)
    assert call["find"] == sentence
    assert call["match"] == "nth"
    assert call["nth"] == 2


@pytest.mark.parametrize("kind", ["gripe", "paper"])
def test_chunk_successor_refuses_without_writes(hub: Hub, kind: str) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old procedure.")
    successor = (
        insert_gripe(store, "Current procedure")
        if kind == "gripe"
        else store.insert_ref(kind="paper", slug="current-procedure", title="Current")
    )
    chunk_kind = "gripe_body" if kind == "gripe" else "paragraph"
    store.chunks.insert_chunks(
        successor.id,
        [
            ChunkInsert(
                ord=0, text="Current procedure.", meta={"chunk_kind": chunk_kind}
            )
        ],
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND chunk_kind = %s",
            (successor.id, chunk_kind),
        ).fetchone()
    assert row is not None
    handle = format_handle(kind, int(row[0]), chunk=True)
    before = _snapshot(store, old)

    with pytest.raises(BadInput, match="cannot name a chunk") as exc:
        handler.delete(id=old, superseded_by=handle)

    assert format_handle(kind, successor.id) in str(exc.value.next)
    assert _snapshot(store, old) == before


def test_successor_converted_after_retirement_keeps_the_redirect(store: Store) -> None:
    """A stored paper handle may now name a datasheet with the same identity."""
    old = _memory(store, "Old procedure.")
    successor = store.insert_ref(
        kind="datasheet",
        slug="retiredoor-converted-successor",
        title="Converted successor",
        meta={"converted_from_kind": "paper"},
    )
    store.stamp_ref_meta(old, {"superseded_by": f"pa{successor.id}"})
    store.retire_ref(old)
    address = format_handle("datasheet", successor.id)

    assert store.follow_supersede_address(old) == address
    assert store.follow_supersede(old) == successor.id
    resolved = store.resolve_handle(f"me{old}")
    assert resolved is not None
    assert (resolved.kind, resolved.ref_id) == ("datasheet", successor.id)
    assert resolved.redirected_from == f"me{old}"


@pytest.mark.parametrize("dry_run", [False, True])
def test_codeless_explicit_successor_refuses_without_writes(
    hub: Hub, dry_run: bool
) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old procedure.")
    successor = store.insert_ref(kind="web", slug="codeless-successor", title="Web")
    before = _snapshot(store, old)

    with pytest.raises(BadInput, match="no record handle") as exc:
        handler.delete(id=old, superseded_by=f"web:{successor.id}", dry_run=dry_run)

    assert "live record handle" in str(exc.value.next)
    assert _snapshot(store, old) == before


@pytest.mark.parametrize("live_mention", [False, True])
def test_codeless_inbound_edge_is_not_a_successor(hub: Hub, live_mention: bool) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old procedure.")
    successor = store.insert_ref(kind="web", slug="codeless-successor", title="Web")
    store.add_link(src_ref_id=successor.id, dst_ref_id=old, relation="supersedes")
    if live_mention:
        _memory(store, f"Use me{old} for current builds.")
        before = _snapshot(store, old)
        with pytest.raises(BadInput, match="live mention"):
            handler.delete(id=old)
        assert _snapshot(store, old) == before
    else:
        handler.delete(id=old)
        ref = store.fetch_refs_by_ids([old])[old]
        assert ref.retired_at is not None
        assert "superseded_by" not in ref.meta


@pytest.mark.parametrize("coded_count", [1, 2])
def test_codeless_inbound_edges_do_not_hide_coded_successors(
    hub: Hub, coded_count: int
) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    old = _memory(store, "Old procedure.")
    for index in range(2):
        web = store.insert_ref(kind="web", slug=f"codeless-{index}", title="Web")
        store.add_link(src_ref_id=web.id, dst_ref_id=old, relation="supersedes")
    successors = [_memory(store, "Current procedure.") for _ in range(coded_count)]
    for successor in successors:
        store.add_link(src_ref_id=successor, dst_ref_id=old, relation="supersedes")

    if coded_count == 1:
        handler.delete(id=old)
        assert (
            store.fetch_refs_by_ids([old])[old].meta["superseded_by"]
            == f"me{successors[0]}"
        )
    else:
        before = _snapshot(store, old)
        with pytest.raises(BadInput, match="multiple live successors") as exc:
            handler.delete(id=old)
        for successor in successors:
            assert f"superseded_by='me{successor}'" in str(exc.value.next)
        assert _snapshot(store, old) == before
