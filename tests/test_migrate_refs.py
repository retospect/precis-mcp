"""Pure-rewrite unit tests for ``precis migrate-refs``.

The DB-bound scan/apply is exercised by the handler/draft suites; here we
pin the span-dispatch grammar with injected resolvers (no DB), since the
rewrite core is the risky part of the migration.
"""

from __future__ import annotations

from precis.cli import migrate_refs
from precis.utils import handle_registry


def _record(kind, ident):
    """Resolve a small fixed universe; unknown idents stay legacy."""
    table = {"6184": "me6184", "kong24": "fi42", "7": "td7"}
    return table.get(ident)


def _chunk(base58):
    return {"abc123": "dc41"}.get(base58)


def _rw(text):
    return migrate_refs.rewrite(text, resolve_record=_record, resolve_chunk=_chunk)


def test_bare_kind_ref_becomes_handle() -> None:
    out, ch = _rw("as shown in memory:6184, the effect holds")
    assert out == "as shown in [me6184], the effect holds"
    assert ch == [("memory:6184", "[me6184]")]


def test_authoring_link_collapses_to_handle() -> None:
    out, _ = _rw("provenance [[memory:6184]] here")
    assert out == "provenance [me6184] here"


def test_display_link_target_rewrites_text_preserved() -> None:
    out, _ = _rw("see [the note](memory:6184) now")
    assert out == "see [the note](me6184) now"


def test_pilcrow_bare_bracket_becomes_dc() -> None:
    out, _ = _rw("compare [¶abc123] above")
    assert out == "compare [dc41] above"


def test_pilcrow_display_link_becomes_dc() -> None:
    out, _ = _rw("compare [the intro](¶abc123)")
    assert out == "compare [the intro](dc41)"


def test_paper_mention_left_as_citation() -> None:
    out, ch = _rw("per paper:kong24 and paper:smith2024~3")
    assert out == "per paper:kong24 and paper:smith2024~3"
    assert ch == []


def test_section_citation_left_untouched() -> None:
    out, ch = _rw("as in [§kong24~3] and [§smith2024]")
    assert out == "as in [§kong24~3] and [§smith2024]"
    assert ch == []


def test_unresolvable_mention_stays_legacy() -> None:
    # ``time:30`` over-fires the kind:ref regex but isn't an allowlist
    # kind; ``memory:9999`` is an allowlist kind that doesn't resolve.
    out, ch = _rw("at time:30 see memory:9999")
    assert out == "at time:30 see memory:9999"
    assert ch == []


def test_already_migrated_handle_is_idempotent() -> None:
    out, ch = _rw("see [me6184] and [dc41]")
    assert out == "see [me6184] and [dc41]"
    assert ch == []


def test_url_display_link_untouched() -> None:
    out, _ = _rw("[DDG](https://duckduckgo.com)")
    assert out == "[DDG](https://duckduckgo.com)"


def test_chunk_addressed_mention_left_alone() -> None:
    # a ~chunk suffix has no bare-handle form, so it stays legacy.
    out, ch = _rw("see memory:6184~2 there")
    assert out == "see memory:6184~2 there"
    assert ch == []


def test_dangling_pilcrow_stays_legacy() -> None:
    out, ch = _rw("compare [¶missing]")
    assert out == "compare [¶missing]"
    assert ch == []


def test_kind_resolves_to_actual_kind() -> None:
    # written ``memory:kong24`` but the row is really a finding → fi42.
    out, _ = _rw("see memory:kong24 here")
    assert out == "see [fi42] here"


def test_mixed_forms_in_one_body() -> None:
    out, ch = _rw(
        "see memory:6184, [[todo:7]], [¶abc123], paper:kong24 and [§kong24~1]"
    )
    assert out == "see [me6184], [td7], [dc41], paper:kong24 and [§kong24~1]"
    assert len(ch) == 3


# ── DB-backed scan + apply (real Postgres via the hub fixture) ────────


def test_scan_and_apply_memory(hub) -> None:
    store = hub.store
    target = store.insert_ref(kind="memory", slug=None, title="cited note").id
    me = handle_registry.format_handle("memory", target)
    body = store.insert_ref(
        kind="memory", slug=None, title=f"as shown in memory:{target}, it holds"
    ).id

    found = migrate_refs._scan_memories(store)
    plan = {c.ident: c.new for c in found}
    assert plan[body] == f"as shown in [{me}], it holds"

    from precis.handlers.memory import MemoryHandler

    handler = MemoryHandler(hub=hub)
    for c in found:
        handler.edit(id=c.ident, mode="replace", text=c.new)

    ref = store.get_ref(kind="memory", id=body)
    assert ref.title == f"as shown in [{me}], it holds"
    # the auto-mention link to the target survives the rewrite
    links = {
        link.dst_ref_id
        for link in store.links_for(body, direction="out", relation="related-to")
    }
    assert target in links
    # idempotent: a second scan finds nothing
    assert all(c.ident != body for c in migrate_refs._scan_memories(store))


def test_scan_and_apply_draft(hub) -> None:
    from precis.handlers.draft import DraftHandler

    store = hub.store
    draft = DraftHandler(hub=hub)
    target = store.insert_ref(kind="memory", slug=None, title="cited note").id
    me = handle_registry.format_handle("memory", target)
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id="nt", title="T", project=proj)
    ref = store.get_ref(kind="draft", id="nt")
    title_h = store.reading_order(ref.id)[0].handle  # legacy base-58
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"see memory:{target} and [¶{title_h}]",
        at={"after": f"¶{title_h}"},
    )

    found = migrate_refs._scan_drafts(store)
    para = store.reading_order(ref.id)[1]
    dc_title = handle_registry.format_handle(
        "draft", _chunk_id(store, title_h), chunk=True
    )
    plan = {c.ident: c.new for c in found}
    assert plan[para.handle] == f"see [{me}] and [{dc_title}]"

    for c in found:
        store.edit_text(c.ident, c.new, source={"tool": "migrate-refs"})

    reread = store.reading_order(ref.id)[1]
    assert reread.text == f"see [{me}] and [{dc_title}]"


def _chunk_id(store, base58):
    with store.pool.connection() as conn:
        return int(
            conn.execute(
                "SELECT chunk_id FROM chunks WHERE handle = %s", (base58,)
            ).fetchone()[0]
        )
