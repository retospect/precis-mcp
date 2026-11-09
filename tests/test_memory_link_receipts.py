"""Memory edit receipts expose mention-link changes without changing sync policy."""

from __future__ import annotations

import ast
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers._memory_link_receipt import MentionSync
from precis.handlers.memory import MemoryHandler
from precis.store import Store
from precis.store.revision_context import revision_context
from precis.utils.link_target import parse_link_target
from tests.conftest import id_of


@pytest.fixture
def handler(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def _memory(handler: MemoryHandler, text: str) -> int:
    return id_of(handler.put(text=text, title="Link receipt fixture").body)


def _body(handler: MemoryHandler, store: Store, ref_id: int) -> str:
    ref = store.get_ref(kind="memory", id=ref_id)
    assert ref is not None
    return handler._body_text(ref)


@pytest.mark.parametrize("dry_run", [True, "full"])
def test_preview_lists_removed_mention_without_writes(
    handler: MemoryHandler, store: Store, dry_run: bool | str
) -> None:
    target = _memory(handler, "Mention target")
    original = f"Evidence: memory:{target}\nKeep this explanation."
    source = _memory(handler, original)
    before_links = store.links_for(source, direction="out")
    before_chunks = store.chunks.list_chunks_for_ref(source)

    receipt = handler.edit(
        id=source,
        mode="find-replace",
        find=f"memory:{target}",
        text="a condensed account",
        dry_run=dry_run,
    ).body

    assert "Links:" in receipt
    assert (
        f"- would remove related-to me{source} -> me{target} (generated mention edge)"
    ) in receipt
    assert (
        f"link(kind='memory', id={source}, target='memory:{target}', "
        "rel='related-to', meta={'auto': 'mention'})"
    ) in receipt
    assert store.links_for(source, direction="out") == before_links
    assert store.chunks.list_chunks_for_ref(source) == before_chunks
    assert _body(handler, store, source) == original


@pytest.mark.parametrize("mode", ["find-replace", "replace"])
def test_real_edit_reports_removed_and_added_edges_and_restore_call(
    handler: MemoryHandler, store: Store, mode: str
) -> None:
    old = _memory(handler, "Old evidence")
    new = _memory(handler, "New evidence")
    source = _memory(handler, f"Evidence: memory:{old}")
    args: dict[str, Any] = {"id": source, "mode": mode}
    if mode == "find-replace":
        args.update(find=f"memory:{old}", text=f"memory:{new}")
    else:
        args["text"] = f"Replacement evidence: memory:{new}"

    receipt = handler.edit(**args).body

    assert (
        f"- removed related-to me{source} -> me{old} (generated mention edge)"
    ) in receipt
    assert (
        f"- added related-to me{source} -> me{new} (generated mention edge)"
    ) in receipt
    assert "would remove" not in receipt
    assert {link.dst_ref_id for link in store.links_for(source, direction="out")} == {
        new
    }
    restore = (
        f"link(kind='memory', id={source}, target='memory:{old}', "
        "rel='related-to', meta={'auto': 'mention'})"
    )
    assert restore in receipt
    # Parse the printed call as data and invoke that exact payload. No eval:
    # an apparently plausible receipt is insufficient if its call cannot restore.
    call = ast.parse(restore, mode="eval").body
    assert isinstance(call, ast.Call)
    payload: dict[str, Any] = {}
    for kw in call.keywords:
        assert kw.arg is not None
        payload[kw.arg] = ast.literal_eval(kw.value)
    assert payload.pop("kind") == "memory"
    handler.link(**payload)
    links = store.links_for(source, direction="out")
    assert {link.dst_ref_id for link in links} == {old, new}
    # gr568170: the restore re-stamps the target's write-time sha.
    assert next(link for link in links if link.dst_ref_id == old).meta == {
        "auto": "mention",
        "asserted_fingerprint": store.target_sha("ref", old),
    }


@pytest.mark.parametrize("dry_run", [False, True, "full"])
def test_batch_edit_reports_every_sources_link_removal(
    handler: MemoryHandler, store: Store, dry_run: bool | str
) -> None:
    """Caught nested collectors losing receipts in the batch's first-line summary."""
    target = _memory(handler, "Shared evidence")
    originals = [f"First account memory:{target}", f"Second account memory:{target}"]
    sources = [_memory(handler, body) for body in originals]
    before_links = [store.links_for(source, direction="out") for source in sources]
    before_chunks = [store.chunks.list_chunks_for_ref(source) for source in sources]

    batch_ids: list[str | int] = list(sources)
    receipt = handler.edit(
        id=batch_ids,
        mode="find-replace",
        find=f"memory:{target}",
        text="condensed",
        dry_run=dry_run,
    ).body

    action = "would remove" if dry_run else "removed"
    for index, source in enumerate(sources):
        removal = (
            f"- {action} related-to me{source} -> me{target} (generated mention edge)"
        )
        assert receipt.count(removal) == 1
        assert (
            f"link(kind='memory', id={source}, target='memory:{target}', "
            "rel='related-to', meta={'auto': 'mention'})"
        ) in receipt
        if dry_run:
            assert store.links_for(source, direction="out") == before_links[index]
            assert store.chunks.list_chunks_for_ref(source) == before_chunks[index]
            assert _body(handler, store, source) == originals[index]
        else:
            assert not store.links_for(source, direction="out")
            assert _body(handler, store, source) == originals[index].replace(
                f"memory:{target}", "condensed"
            )


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("keep_mention", [False, True])
def test_explicit_edge_is_never_reported_removed(
    handler: MemoryHandler, store: Store, dry_run: bool, keep_mention: bool
) -> None:
    target = _memory(handler, "Explicit evidence")
    source = _memory(handler, "Original note")
    handler.link(
        id=source,
        target=f"memory:{target}",
        rel="related-to",
        meta={"reason": "Retain this provenance independently of prose"},
    )
    handler.edit(id=source, mode="append", text=f"memory:{target}")
    before = store.links_for(source, direction="out")
    assert len(before) == 1
    assert "auto" not in before[0].meta

    receipt = handler.edit(
        id=source,
        mode="find-replace",
        find="Original note" if keep_mention else f"memory:{target}",
        text="Condensed account",
        dry_run=dry_run,
    ).body

    assert "Links:" not in receipt
    assert store.links_for(source, direction="out") == before


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("with_mention", [False, True])
def test_unchanged_links_have_no_receipt_section(
    handler: MemoryHandler, store: Store, dry_run: bool, with_mention: bool
) -> None:
    target = _memory(handler, "Stable evidence")
    original = "Original explanation"
    if with_mention:
        original += f" memory:{target}"
    source = _memory(handler, original)

    receipt = handler.edit(
        id=source,
        mode="find-replace",
        find="Original",
        text="Revised",
        dry_run=dry_run,
    ).body

    assert "Links:" not in receipt
    links = store.links_for(source, direction="out")
    assert {link.dst_ref_id for link in links} == ({target} if with_mention else set())
    assert _body(handler, store, source) == (
        original if dry_run else original.replace("Original", "Revised")
    )


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("mode", ["append", "insert"])
def test_append_and_insert_report_added_mentions(
    handler: MemoryHandler, store: Store, dry_run: bool, mode: str
) -> None:
    target = _memory(handler, "Additional evidence")
    source = _memory(handler, "Existing account")
    args: dict[str, Any] = {
        "id": source,
        "mode": mode,
        "text": f" memory:{target}",
        "dry_run": dry_run,
    }
    if mode == "insert":
        args.update(find="Existing account", where="after")

    receipt = handler.edit(**args).body

    action = "would add" if dry_run else "added"
    assert (
        f"- {action} related-to me{source} -> me{target} (generated mention edge)"
    ) in receipt
    links = store.links_for(source, direction="out")
    assert {link.dst_ref_id for link in links} == (set() if dry_run else {target})
    if dry_run:
        assert _body(handler, store, source) == "Existing account"


@pytest.mark.parametrize("dry_run", [False, True])
def test_ops_receipt_reports_only_final_link_effects(
    handler: MemoryHandler, store: Store, dry_run: bool
) -> None:
    old = _memory(handler, "Initial evidence")
    intermediate = _memory(handler, "Intermediate evidence")
    final = _memory(handler, "Final evidence")
    source = _memory(handler, f"Evidence memory:{old}")
    before = store.links_for(source, direction="out")

    receipt = handler.edit(
        id=source,
        ops=[
            {"find": f"memory:{old}", "text": f"memory:{intermediate}"},
            {"find": f"memory:{intermediate}", "text": f"memory:{final}"},
        ],
        dry_run=dry_run,
    ).body

    remove = "would remove" if dry_run else "removed"
    add = "would add" if dry_run else "added"
    assert (
        f"- {remove} related-to me{source} -> me{old} (generated mention edge)"
    ) in receipt
    assert (
        f"- {add} related-to me{source} -> me{final} (generated mention edge)"
    ) in receipt
    assert f"-> me{intermediate} " not in receipt
    if dry_run:
        assert store.links_for(source, direction="out") == before
        assert _body(handler, store, source) == f"Evidence memory:{old}"
    else:
        assert {
            link.dst_ref_id for link in store.links_for(source, direction="out")
        } == {final}


@pytest.mark.parametrize(
    ("meta", "origin"),
    [
        ({"auto": "mention"}, "generated mention edge"),
        ({"reason": "Source provenance"}, "explicit/provenance edge"),
    ],
)
@pytest.mark.parametrize("action", ["added", "removed"])
def test_renderer_distinguishes_generated_and_explicit_edges(
    handler: MemoryHandler,
    store: Store,
    meta: dict[str, str],
    origin: str,
    action: str,
) -> None:
    """Renderer labels retained metadata; this does not change sync policy."""
    source = _memory(handler, "Source account")
    target = _memory(handler, "Supporting account")
    sync = MentionSync(store, source, replace_links=True)
    link = store.add_link(
        src_ref_id=source,
        dst_ref_id=target,
        relation="related-to",
        meta=dict(meta),
    )
    if action == "added":
        sync.added(link)
    else:
        sync = MentionSync(store, source, replace_links=True)
        sync.removed(link)

    receipt = sync.render()

    assert f"- {action} related-to me{source} -> me{target} ({origin})" in receipt
    other_origin = (
        "explicit/provenance edge"
        if origin == "generated mention edge"
        else "generated mention edge"
    )
    assert other_origin not in receipt
    if action == "removed":
        assert (
            f"link(kind='memory', id={source}, target='memory:{target}', "
            f"rel='related-to', meta={meta!r})"
        ) in receipt


def test_codeless_target_uses_its_public_address(
    handler: MemoryHandler, store: Store
) -> None:
    target = store.insert_ref(kind="web", slug="receipt-source", title="Source")
    source = _memory(handler, "Evidence web:receipt-source")
    before = store.links_for(source, direction="out")
    assert {link.dst_ref_id for link in before} == {target.id}

    receipt = handler.edit(
        id=source,
        mode="find-replace",
        find="web:receipt-source",
        text="the source account",
    ).body

    assert (
        f"- removed related-to me{source} -> web:receipt-source "
        "(generated mention edge)"
    ) in receipt
    assert (
        f"link(kind='memory', id={source}, target='web:receipt-source', "
        "rel='related-to', meta={'auto': 'mention'})"
    ) in receipt
    assert not store.links_for(source, direction="out")


def test_chunk_target_removal_preserves_selector_in_restore_call(
    handler: MemoryHandler, store: Store
) -> None:
    target = _memory(handler, "A source paragraph")
    source = _memory(handler, f"Evidence memory:{target}~0")
    before = store.links_for(source, direction="out")
    assert len(before) == 1
    assert (before[0].dst_ref_id, before[0].dst_ord) == (target, 0)

    receipt = handler.edit(
        id=source,
        mode="find-replace",
        find=f"memory:{target}~0",
        text="a shortened description",
    ).body

    assert (
        f"- removed related-to me{source} -> me{target}~0 (generated mention edge)"
    ) in receipt
    restore = (
        f"link(kind='memory', id={source}, target='memory:{target}~0', "
        "rel='related-to', meta={'auto': 'mention'})"
    )
    assert restore in receipt
    assert not store.links_for(source, direction="out")
    call = ast.parse(restore, mode="eval").body
    assert isinstance(call, ast.Call)
    payload: dict[str, Any] = {}
    for kw in call.keywords:
        assert kw.arg is not None
        payload[kw.arg] = ast.literal_eval(kw.value)
    assert payload.pop("kind") == "memory"
    handler.link(**payload)
    restored = store.links_for(source, direction="out")
    assert len(restored) == 1
    assert (restored[0].dst_ref_id, restored[0].dst_ord) == (target, 0)
    assert restored[0].meta == {
        "auto": "mention",
        "asserted_fingerprint": store.target_sha("chunk", restored[0].dst_chunk_id),
    }


@pytest.mark.parametrize("dry_run", [False, True])
def test_card_chunk_restore_call_parses_and_restores(
    handler: MemoryHandler, store: Store, dry_run: bool
) -> None:
    """Caught card receipts emitting the rejected paper:slug~-1 target."""
    target = store.insert_ref(kind="paper", slug="example2026", title="Card fixture")
    card_id = store.chunks.upsert_card_combined(target.id, "Synthetic card fixture")
    handle = f"pc{card_id}"
    original = f"Evidence [{handle}]"
    source = _memory(handler, original)
    before = store.links_for(source, direction="out")
    assert len(before) == 1
    assert (before[0].dst_ref_id, before[0].dst_ord, before[0].dst_chunk_id) == (
        target.id,
        -1,
        card_id,
    )

    receipt = handler.edit(
        id=source,
        mode="find-replace",
        find=f"[{handle}]",
        text="condensed",
        dry_run=dry_run,
    ).body

    restore = next(
        line.removeprefix("  Restore: ")
        for line in receipt.splitlines()
        if line.startswith("  Restore: ")
    )
    call = ast.parse(restore, mode="eval").body
    assert isinstance(call, ast.Call)
    payload: dict[str, Any] = {}
    for kw in call.keywords:
        assert kw.arg is not None
        payload[kw.arg] = ast.literal_eval(kw.value)
    assert payload.pop("kind") == "memory"
    assert payload["target"] == handle
    resolved = parse_link_target(payload["target"], store=store)
    assert (resolved.ref_id, resolved.pos) == (target.id, -1)
    if dry_run:
        assert store.links_for(source, direction="out") == before
        assert _body(handler, store, source) == original
    else:
        assert not store.links_for(source, direction="out")
        assert _body(handler, store, source) == "Evidence condensed"
        handler.link(**payload)
        restored = store.links_for(source, direction="out")
        assert len(restored) == 1
        assert (
            restored[0].dst_ref_id,
            restored[0].dst_ord,
            restored[0].dst_chunk_id,
        ) == (
            target.id,
            -1,
            card_id,
        )
        assert restored[0].meta == {
            "auto": "mention",
            "asserted_fingerprint": store.target_sha("chunk", card_id),
        }


@pytest.mark.parametrize("dry_run", [False, True])
def test_invalid_chunk_stops_additions_after_reporting_prior_removal(
    handler: MemoryHandler, store: Store, dry_run: bool
) -> None:
    old = _memory(handler, "Previous evidence")
    invalid = _memory(handler, "This source has no requested chunk")
    valid = _memory(handler, "A later valid source")
    original = f"Evidence memory:{old}"
    replacement = f"memory:{invalid}~99999 memory:{valid}"
    source = _memory(handler, original)
    before = store.links_for(source, direction="out")

    receipt = handler.edit(
        id=source,
        mode="find-replace",
        find=f"memory:{old}",
        text=replacement,
        dry_run=dry_run,
    ).body

    action = "would remove" if dry_run else "removed"
    assert (
        f"- {action} related-to me{source} -> me{old} (generated mention edge)"
    ) in receipt
    assert "- would add " not in receipt
    assert "- added " not in receipt
    if dry_run:
        assert "Mention sync would stop:" in receipt
        assert store.links_for(source, direction="out") == before
        assert _body(handler, store, source) == original
    else:
        assert not store.links_for(source, direction="out")
        assert _body(handler, store, source) == f"Evidence {replacement}"


def test_preview_reports_unminted_part_without_write_calls(
    handler: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _memory(handler, "Existing account")
    before_chunks = store.chunks.list_chunks_for_ref(source)
    writes: list[str] = []

    def reject_write(*_args: Any, **_kwargs: Any) -> None:
        writes.append("unexpected write")
        raise AssertionError("Mention preview must not mint parts or write")

    monkeypatch.setattr(store, "part_ref_id", lambda _lcsc: None)
    monkeypatch.setattr(store, "ensure_part_ref", reject_write)
    monkeypatch.setattr(store, "add_link", reject_write)
    monkeypatch.setattr(store, "remove_link", reject_write)
    monkeypatch.setattr(store, "update_ref", reject_write)
    monkeypatch.setattr(store.chunks, "replace_body_chunk", reject_write)

    receipt = handler.edit(
        id=source, mode="append", text="Additional evidence part:C12345", dry_run=True
    ).body

    assert (
        "Mention preview: part:C12345 has no live ref; apply may mint "
        "it and add a generated related-to edge."
    ) in receipt
    assert "Links:" not in receipt
    assert not writes
    assert not store.links_for(source, direction="out")
    assert store.chunks.list_chunks_for_ref(source) == before_chunks
    assert _body(handler, store, source) == "Existing account"


@pytest.mark.parametrize("dry_run", [False, True])
def test_retired_target_restore_call_names_liveness_requirement(
    handler: MemoryHandler, store: Store, dry_run: bool
) -> None:
    target = _memory(handler, "Former evidence")
    original = f"Earlier account memory:{target}"
    source = _memory(handler, original)
    store.retire_ref(target)
    before = store.links_for(source, direction="out")
    assert len(before) == 1

    receipt = handler.edit(
        id=source,
        mode="find-replace",
        find="Earlier",
        text="Revised",
        dry_run=dry_run,
    ).body

    action = "would remove" if dry_run else "removed"
    assert (
        f"- {action} related-to me{source} -> me{target} (generated mention edge)"
    ) in receipt
    assert (
        "Restore after target is live (currently retired): "
        f"link(kind='memory', id={source}, target='memory:{target}', "
        "rel='related-to', meta={'auto': 'mention'})"
    ) in receipt
    assert "Restore: link(" not in receipt
    if dry_run:
        assert store.links_for(source, direction="out") == before
        assert _body(handler, store, source) == original
    else:
        assert not store.links_for(source, direction="out")
        assert _body(handler, store, source) == original.replace("Earlier", "Revised")


@pytest.mark.parametrize("dry_run", [False, True])
def test_machine_writer_preview_matches_mention_admission_refusal(
    handler: MemoryHandler, store: Store, dry_run: bool
) -> None:
    old = _memory(handler, "Original evidence")
    new = _memory(handler, "Proposed evidence")
    source = _memory(handler, f"Evidence memory:{old}")
    before = store.links_for(source, direction="out")

    with revision_context(
        "memory receipt test",
        model="test-model",
        trust_origin="generated",
        trust_actor_type="agent",
    ):
        receipt = handler.edit(
            id=source,
            mode="find-replace",
            find=f"memory:{old}",
            text=f"memory:{new}",
            dry_run=dry_run,
        ).body

    action = "would remove" if dry_run else "removed"
    assert f"- {action} related-to me{source} -> me{old}" in receipt
    assert "- would add " not in receipt
    assert "- added " not in receipt
    if dry_run:
        assert "Mention sync would stop: machine-edge-human-only" in receipt
        assert store.links_for(source, direction="out") == before
    else:
        assert not store.links_for(source, direction="out")


def test_tilde_slug_restore_call_names_the_handle(
    handler: MemoryHandler, store: Store
) -> None:
    """Caught paper:source~notes parsing as slug source, selector notes."""
    target = store.insert_ref(kind="paper", slug="source~notes", title="Tilde fixture")
    handle = f"pa{target.id}"
    source = _memory(handler, f"Evidence [{handle}]")

    receipt = handler.edit(
        id=source, mode="find-replace", find=f"[{handle}]", text="condensed"
    ).body

    assert f"target={handle!r}" in receipt
    assert "source~notes" not in receipt
