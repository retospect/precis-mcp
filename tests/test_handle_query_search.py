"""``search(q=<universal handle>)`` pins the identity card first (gr517806).

A handle names one object, so its card ranks ahead of every lexical /
semantic hit: a record handle (``pa5``, ``gr123``) yields its ref card;
a chunk handle (``dc4417588``, ``pc10`` — every chunk code the handle
registry knows) yields the containing ref's card showing that chunk's
text. The status kinds (gripe/todo/quest) collapse a comment-chunk
handle to the parent ref's card. An unknown or retired handle falls
through to the ordinary fan-out untouched.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.gripe import GripeHandler
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store
from tests.conftest import id_of


def _seed(
    store: Store, kind: str, slug: str | None, blocks: list[str]
) -> tuple[int, list[int]]:
    title = f"{kind} {slug}" if slug is not None else f"{kind} probe"
    ref = store.insert_ref(kind=kind, slug=slug, title=title, meta={})
    rows = store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=i, text=t) for i, t in enumerate(blocks)]
    )
    return int(ref.id), [int(r.id) for r in rows]


def test_chunk_handle_pins_containing_draft(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    _, (cid,) = _seed(store, "draft", "hqdraft", ["Amine uptake probe body."])
    handle = f"dc{cid}"
    # Decoy: a paper chunk whose text literally quotes the handle — a
    # pure lexical hit that must rank BELOW the pinned identity card.
    _, (pcid,) = _seed(store, "paper", "hqdecoy", [f"see {handle} for it."])
    body = runtime_with_store.dispatch("search", {"q": handle})
    assert "[error" not in body
    # The pinned row: the chunk handle as id, the containing draft's
    # name + the chunk's own text as the summary cell.
    assert "draft hqdraft — Amine uptake probe body." in body
    # Pinned first: the handle's own TOON row precedes the decoy's.
    assert body.index(f"\n{handle}\t") < body.index(f"\npc{pcid}\t")


def test_record_handle_pins_ref_card(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref_id, _ = _seed(store, "paper", "hqrec", ["unrelated body text"])
    handle = f"pa{ref_id}"
    _, (pcid,) = _seed(store, "paper", "hqdecoy2", [f"see {handle} here."])
    body = runtime_with_store.dispatch("search", {"q": handle})
    assert "[error" not in body
    assert "paper hqrec" in body
    assert body.index(f"\n{handle}\t") < body.index(f"\npc{pcid}\t")


def test_status_kind_chunk_handle_collapses_to_parent(
    runtime_with_store: PrecisRuntime, store: Store, hub: Hub
) -> None:
    # Mint via the handler — a bare insert_ref violates the gripe
    # STATUS-tag constraint; the gc chunk is the comment row (put id=
    # appends a gripe_comment at the next free ord).
    handler = GripeHandler(hub=hub)
    gripe_id = id_of(handler.put(text="gripe body").body)
    handler.put(id=gripe_id, text="a comment worth finding")
    cid = int(max(store.chunks.list_chunks_for_ref(gripe_id), key=lambda c: c.ord).id)
    body = runtime_with_store.dispatch("search", {"q": f"gc{cid}"})
    assert "[error" not in body
    # The card is the parent gripe's handle, not the gc<N> chunk handle —
    # the same collapse the source search applies to status-kind chunks.
    # (The headline echoes q, so assert on the TOON-row form.)
    assert f"gr{gripe_id}" in body
    assert f"\ngc{cid}\t" not in body


def test_chunk_handle_pinned_in_source_search(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The sort=/since=/until= route (``_dispatch_source_search``) carries
    the same pinned card — the markdown render leads with it."""
    _, (cid,) = _seed(store, "draft", "hqsrc", ["Recency probe body text."])
    handle = f"dc{cid}"
    body = runtime_with_store.dispatch("search", {"q": handle, "sort": "recency"})
    assert "[error" not in body
    assert f"## 1. {handle}" in body
    assert "Recency probe body text." in body


def test_pin_suppressed_outside_requested_kinds(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """An explicit kind list scopes the pin: a todo handle in a
    paper+memory search must not surface the todo card."""
    todo_id = store.insert_ref(kind="todo", slug=None, title="todo probe", meta={}).id
    handle = f"td{todo_id}"
    body = runtime_with_store.dispatch("search", {"kind": "paper,memory", "q": handle})
    assert "[error" not in body
    assert f"\n{handle}\t" not in body
    # Control: the same handle still pins when its kind IS requested.
    body = runtime_with_store.dispatch("search", {"kind": "todo,memory", "q": handle})
    assert f"\n{handle}\t" in body


def test_pin_suppressed_in_kind_scoped_source_search(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``kind='paper'`` + ``sort=`` is a paper-scoped source search — a
    memory handle must not pin its card as row 1 there."""
    mem_id = store.insert_ref(
        kind="memory", slug=None, title="memory probe", meta={}
    ).id
    handle = f"me{mem_id}"
    body = runtime_with_store.dispatch(
        "search", {"kind": "paper", "q": handle, "sort": "recency"}
    )
    assert "[error" not in body
    assert "memory probe" not in body
    # Control: kind='memory' pins the same card on the same route.
    body = runtime_with_store.dispatch(
        "search", {"kind": "memory", "q": handle, "sort": "recency"}
    )
    assert f"## 1. {handle}" in body


def test_profile_pin_respects_owner_privacy(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``q='pf<N>'`` for another owner's profile pins nothing
    (gr490266): no existence oracle, no title leak."""
    from precis.users import hash_password

    store.create_web_user(login="alice", abbrev="al", password=hash_password("pw"))
    prof = store.insert_ref(
        kind="profile", slug=None, title="AlicePalette", owner_login="alice"
    )
    handle = f"pf{prof.id}"
    # Unidentified caller (agent scope): the pin still shows.
    body = runtime_with_store.dispatch("search", {"q": handle})
    assert "AlicePalette" in body
    # Identified as a different owner: nothing pins, nothing leaks.
    monkeypatch.setattr("precis.profiles.acting_login", lambda _s: "bob")
    body = runtime_with_store.dispatch("search", {"q": handle})
    assert "AlicePalette" not in body
    assert f"\n{handle}\t" not in body


def test_pinned_ref_not_doubled_in_source_search(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The pinned card covers its ref — a lexical hit on ANOTHER chunk
    of the same draft must not list it again or push past top_k."""
    ref_id, (c1,) = _seed(store, "draft", "hqdup", ["alpha body"])
    rows = store.chunks.insert_chunks(
        ref_id, [ChunkInsert(ord=1, text=f"see dc{c1} here")]
    )
    c2 = int(rows[0].id)
    body = runtime_with_store.dispatch("search", {"q": f"dc{c1}", "sort": "recency"})
    assert "[error" not in body
    assert f"## 1. dc{c1}" in body
    assert "## 2." not in body
    assert f"dc{c2}" not in body


def test_pin_does_not_mask_total_fanout_failure(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every kind erroring must stay loud even when a handle pins — a
    lone pin on an all-errored fan-out is not a normal result."""
    from precis.errors import Internal

    rid = store.insert_ref(kind="memory", slug=None, title="pinmask", meta={}).id
    monkeypatch.setattr(
        runtime_with_store,
        "_cross_kind_fan_out",
        lambda kinds, kw: {k: Internal("boom") for k in kinds},
    )
    body = runtime_with_store.dispatch("search", {"q": f"me{rid}"})
    assert "[error" in body
    assert "failed in every kind" in body
    assert "pinmask" not in body


def test_unknown_handle_falls_through(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    _seed(store, "draft", "hqother", ["some body"])
    body = runtime_with_store.dispatch("search", {"q": "dc99999999"})
    assert "[error" not in body
    assert "no matches" in body


def test_non_handle_query_unaffected(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    # ``me5``-shaped tokens that are not well-formed handles never reach
    # the resolver: a text query still behaves as a text query.
    _seed(store, "paper", "hqtext", ["plain english prose about carbon"])
    body = runtime_with_store.dispatch(
        "search", {"q": "carbon prose", "mode": "lexical"}
    )
    assert "[error" not in body
    assert "hqtext" in body
