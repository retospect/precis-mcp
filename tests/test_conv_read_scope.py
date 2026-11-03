"""gr555998: conv read-scope on a public surface (asa-slack).

``PRECIS_CONV_READ_SCOPE=<slug>`` — set by the bridge on the spawned
agent's serving env, or bound as ``TickContext.conv_read_scope`` on the
in-process OSS loop — confines a run's conv access to its own
conversation, enforced server-side in the handler (not just hidden in
the manifest): other convs, the bare list and cross-conv search refuse,
every conv write is denied, and an unscoped search auto-scopes. Runs
with no scope bound behave exactly as before.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import Denied
from precis.handlers.conversation import _READ_SCOPE_ENV, ConversationHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.types import ChunkInsert
from precis.utils import handle_registry, inproc_context
from tests.conftest import id_of

_OWN = "slack/T1/C1/111.000"
_OTHER = "slack/T1/C1/222.000"


@pytest.fixture
def conv(hub: Hub) -> ConversationHandler:
    return ConversationHandler(hub=hub)


@pytest.fixture
def scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_READ_SCOPE_ENV, _OWN)


def _seed_conv(store: Store, slug: str, turns: list[str]) -> int:
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="conv",
            slug=slug,
            title=f"conv {slug}",
            meta={},
            conn=conn,
        )
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=i, text=t) for i, t in enumerate(turns)],
            conn=conn,
        )
    return ref.id


def _seed_pair(store: Store) -> None:
    _seed_conv(store, _OWN, ["own needle turn", "own second turn"])
    _seed_conv(store, _OTHER, ["other needle turn"])


# ── reads ────────────────────────────────────────────────────────────


def test_scoped_get_own_conv_works(conv: ConversationHandler, scoped: None) -> None:
    _seed_pair(conv.store)
    out = conv.get(id=_OWN)
    assert "2 turns" in out.body
    transcript = conv.get(id=f"{_OWN}/transcript")
    assert "own needle turn" in transcript.body
    turn = conv.get(id=f"{_OWN}~1")
    assert "own second turn" in turn.body


def test_scoped_get_other_conv_refused(conv: ConversationHandler, scoped: None) -> None:
    _seed_pair(conv.store)
    with pytest.raises(Denied, match=_OWN):
        conv.get(id=_OTHER)
    with pytest.raises(Denied, match=_OWN):
        conv.get(id=f"{_OTHER}/transcript")


def test_scoped_bare_list_refused(conv: ConversationHandler, scoped: None) -> None:
    _seed_pair(conv.store)
    with pytest.raises(Denied, match=_OWN):
        conv.get()


def test_scoped_search_sees_only_own_conv(
    conv: ConversationHandler, scoped: None
) -> None:
    _seed_pair(conv.store)
    body = conv.search(q="needle").body
    assert "own needle turn" in body
    assert "other needle turn" not in body


def test_scoped_search_explicit_other_scope_refused(
    conv: ConversationHandler, scoped: None
) -> None:
    _seed_pair(conv.store)
    with pytest.raises(Denied, match=_OWN):
        conv.search(q="needle", scope=_OTHER)


def test_scoped_search_hits_sees_only_own_conv(
    conv: ConversationHandler, scoped: None
) -> None:
    _seed_pair(conv.store)
    hits = conv.search_hits(q="needle")
    assert hits
    assert all(h.slug == _OWN for h in hits)


# ── writes ───────────────────────────────────────────────────────────


def test_scoped_put_refused(conv: ConversationHandler, scoped: None) -> None:
    _seed_pair(conv.store)
    with pytest.raises(Denied, match="read-only"):
        conv.put(id=_OWN, text="hi", author="a", msg_id="m1")


def test_scoped_tag_and_link_refused(conv: ConversationHandler, scoped: None) -> None:
    _seed_pair(conv.store)
    with pytest.raises(Denied, match="read-only"):
        conv.tag(id=_OWN, add=["x"])
    with pytest.raises(Denied, match="read-only"):
        conv.link(id=_OWN, target="todo:1")


# ── in-process carrier ───────────────────────────────────────────────


def test_tick_context_binds_the_same_scope(
    conv: ConversationHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-process OSS loop can't read the env back-door (the worker's
    own env would answer for the wrong run) — the overlay's
    ``PRECIS_CONV_READ_SCOPE`` binds ``TickContext.conv_read_scope``
    instead, and the handler honours it identically."""
    monkeypatch.delenv(_READ_SCOPE_ENV, raising=False)
    _seed_pair(conv.store)
    ctx = inproc_context.TickContext(conv_read_scope=_OWN)
    with inproc_context.tick_context(ctx):
        assert "2 turns" in conv.get(id=_OWN).body
        with pytest.raises(Denied):
            conv.get(id=_OTHER)
        with pytest.raises(Denied):
            conv.put(id=_OWN, text="hi", author="a", msg_id="m1")
    # Outside the tick the scope lifts with the context.
    assert "1 turn" in conv.get(id=_OTHER).body


def test_overlay_binds_conv_read_scope() -> None:
    """The router's overlay→TickContext translation carries
    ``PRECIS_CONV_READ_SCOPE`` — else a containerless/in-process Slack
    turn would load conv unscoped."""
    from precis.utils.llm.router import _overlay_tick_context

    ctx = _overlay_tick_context({"PRECIS_CONV_READ_SCOPE": _OWN})
    assert ctx is not None
    assert ctx.conv_read_scope == _OWN


# ── unscoped session: unchanged ──────────────────────────────────────


def test_unscoped_conv_surface_unchanged(conv: ConversationHandler) -> None:
    _seed_pair(conv.store)
    listed = conv.get().body
    assert "2 conversations" in listed
    assert "1 turn" in conv.get(id=_OTHER).body
    body = conv.search(q="needle").body
    assert "own needle turn" in body
    assert "other needle turn" in body
    ack = conv.put(id=_OWN, text="written", author="a", msg_id="m9")
    assert "appended" in ack.body


# ── dispatch level: eye ladder, ring masking, link targets ──────────
#
# The review follow-up: the dispatcher routes a ``get`` carrying
# ``extent=`` to ``handler.eye`` — not ``get`` — so the ladder and the
# rings it renders need the same scope the handler enforces.


@pytest.fixture
def rt(runtime_with_store: PrecisRuntime) -> PrecisRuntime:
    return runtime_with_store


def _seed_memory(store: Store, text: str) -> int:
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="memory", slug=None, title=text[:60], meta={}, conn=conn
        )
        store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=text)], conn=conn)
    return ref.id


def _chunk_handle(store: Store, ref_id: int, ord_: int) -> str:
    block = store.chunks.get_chunk(ref_id, pos=ord_)
    assert block is not None
    return handle_registry.format_handle("conv", block.id, chunk=True)


def _conv_ref_id(store: Store, slug: str) -> int:
    ref = store.get_ref(kind="conv", id=slug)
    assert ref is not None
    return ref.id


def test_dispatch_eye_denies_other_conv_every_rung(
    rt: PrecisRuntime, store: Store, scoped: None
) -> None:
    """The ladder routes around ``ConversationHandler.get`` — every rung
    on another conversation must still refuse, focus and hops alike."""
    _seed_pair(store)
    for extent in ("kwd", "summary", "verbatim", "fisheye", "fisheye+1hop"):
        body = rt.dispatch("get", {"kind": "conv", "id": _OTHER, "extent": extent})
        assert "[error:Denied]" in body, (extent, body)
        assert "other needle turn" not in body


def test_dispatch_eye_denies_other_conv_by_handle(
    rt: PrecisRuntime, store: Store, scoped: None
) -> None:
    """``co<N>``/``cc<N>`` spellings of the other conv deny the same way —
    handle resolution is not a scope bypass."""
    _seed_pair(store)
    other_id = _conv_ref_id(store, _OTHER)
    co = handle_registry.format_handle("conv", other_id)
    cc = _chunk_handle(store, other_id, 0)
    for ident in (co, cc):
        body = rt.dispatch("get", {"kind": "conv", "id": ident, "extent": "fisheye"})
        assert "[error:Denied]" in body, (ident, body)


def test_dispatch_eye_on_own_conv_works(
    rt: PrecisRuntime, store: Store, scoped: None
) -> None:
    _seed_pair(store)
    body = rt.dispatch("get", {"kind": "conv", "id": _OWN, "extent": "fisheye"})
    assert "[error:" not in body
    assert "own needle turn" in body


def test_dispatch_cc_handle_resolves_to_owning_conv(
    rt: PrecisRuntime, store: Store, scoped: None
) -> None:
    """An agent citing its own turn by ``cc<id>`` is in-scope — the handle
    resolves to its conversation before the bound-slug compare."""
    _seed_pair(store)
    own_id = _conv_ref_id(store, _OWN)
    other_id = _conv_ref_id(store, _OTHER)
    own_turn = _chunk_handle(store, own_id, 1)
    other_turn = _chunk_handle(store, other_id, 0)

    body = rt.dispatch("get", {"id": own_turn})
    assert "own second turn" in body
    body = rt.dispatch("get", {"kind": "conv", "id": own_turn})
    assert "own second turn" in body

    for ident in (other_turn, handle_registry.format_handle("conv", other_id)):
        body = rt.dispatch("get", {"id": ident})
        assert "[error:Denied]" in body, (ident, body)


def test_dispatch_ring_masks_out_of_scope_conv_neighbour(
    rt: PrecisRuntime, store: Store, scoped: None
) -> None:
    """A memory linked to another conv: the ring prints the opaque
    placeholder and no title — at 1hop and at 2hop alike."""
    _seed_pair(store)
    own_id = _conv_ref_id(store, _OWN)
    other_id = _conv_ref_id(store, _OTHER)
    mem = _seed_memory(store, "a note that links two conversations")
    second = _seed_memory(store, "a note the other conv links to")
    store.add_link(src_ref_id=mem, dst_ref_id=other_id, relation="related-to")
    store.add_link(src_ref_id=mem, dst_ref_id=own_id, relation="related-to")
    store.add_link(src_ref_id=other_id, dst_ref_id=second, relation="related-to")

    for extent in ("fisheye+1hop", "fisheye+2hop"):
        body = rt.dispatch("get", {"kind": "memory", "id": mem, "extent": extent})
        assert "[error:" not in body, (extent, body)
        assert "conv (outside this session's scope)" in body
        # The masked row carries no title, handle or excerpt of the
        # other conv; the bound conv's own row renders unmasked.
        assert _OTHER not in body
        assert f"conv {_OTHER}" not in body
        assert _OWN in body


def test_dispatch_link_to_other_conv_refused(
    rt: PrecisRuntime, store: Store, scoped: None
) -> None:
    """``link()`` and ``put(link=)`` may target the bound conversation
    only — an edge to another conv would print it in this surface's
    rings."""
    _seed_pair(store)
    mem = _seed_memory(store, "a note wanting an edge")
    body = rt.dispatch(
        "link", {"kind": "memory", "id": mem, "target": f"conv:{_OTHER}"}
    )
    assert "[error:Denied]" in body, body
    body = rt.dispatch(
        "put", {"kind": "memory", "text": "another note", "link": f"conv:{_OTHER}"}
    )
    assert "[error:Denied]" in body, body
    # The bound conversation stays a legal target.
    body = rt.dispatch("link", {"kind": "memory", "id": mem, "target": f"conv:{_OWN}"})
    assert "[error:" not in body, body


def test_dispatch_unscoped_conv_eye_and_links_unchanged(
    rt: PrecisRuntime, store: Store
) -> None:
    _seed_pair(store)
    body = rt.dispatch("get", {"kind": "conv", "id": _OTHER, "extent": "verbatim"})
    assert "other needle turn" in body
    other_id = _conv_ref_id(store, _OTHER)
    mem = _seed_memory(store, "a note wanting an edge")
    store.add_link(src_ref_id=mem, dst_ref_id=other_id, relation="related-to")
    body = rt.dispatch("get", {"kind": "memory", "id": mem, "extent": "fisheye+1hop"})
    assert "outside this session's scope" not in body
    assert f"conv {_OTHER}" in body
    mem2 = _seed_memory(store, "one more note")
    body = rt.dispatch(
        "link", {"kind": "memory", "id": mem2, "target": f"conv:{_OTHER}"}
    )
    assert "[error:" not in body, body


# ── dispatch level: Links table, mention-sync, gated kinds ──────────
#
# Round 3: a memory's default ``get`` appends the Links table (as does
# ``view='links'``) — an out-of-scope conv endpoint must render the
# same opaque placeholder the rings print, never its handle/title/size.
# The mention autolinker must not materialise an edge to one either.
# And a neighbour of a gated kind (the surface's
# ``PRECIS_KINDS_DISABLED`` list — todo/gripe/job on Slack) gets the
# same placeholder treatment in both ring and table rows.

_KINDS_DISABLED_ENV = "PRECIS_KINDS_DISABLED"


@pytest.fixture
def slack_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two env bindings a Slack turn carries together: the conv
    read scope plus the allowlist complement (todo/gripe/job gated)."""
    monkeypatch.setenv(_READ_SCOPE_ENV, _OWN)
    monkeypatch.setenv(_KINDS_DISABLED_ENV, "todo,gripe,job")


def _seed_todo(store: Store, title: str) -> int:
    with store.tx() as conn:
        ref = store.insert_ref(kind="todo", slug=None, title=title, meta={}, conn=conn)
        store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=title)], conn=conn)
    return ref.id


def test_dispatch_links_table_masks_out_of_scope_conv(
    rt: PrecisRuntime, store: Store, scoped: None
) -> None:
    """The bare-get Links table and ``view='links`` alike: the masked
    row carries the relation marker and the placeholder — no handle,
    title, size or get-call for the other conversation."""
    _seed_pair(store)
    own_id = _conv_ref_id(store, _OWN)
    other_id = _conv_ref_id(store, _OTHER)
    mem = _seed_memory(store, "a note that links two conversations")
    store.add_link(src_ref_id=mem, dst_ref_id=other_id, relation="related-to")
    store.add_link(src_ref_id=mem, dst_ref_id=own_id, relation="related-to")

    body = rt.dispatch("get", {"kind": "memory", "id": mem})
    assert "[error:" not in body, body
    assert "conv (outside this session's scope)" in body
    # Nothing about the other conv survives the mask — its slug is
    # in the handle, the title teaser and the get-call alike.
    assert _OTHER not in body
    assert f"conv {_OTHER}" not in body
    # The bound conversation's own row still renders.
    assert _OWN in body

    body = rt.dispatch("get", {"kind": "memory", "id": mem, "view": "links"})
    assert "[error:" not in body, body
    assert "conv (outside this session's scope)" in body
    assert _OTHER not in body
    assert handle_registry.format_handle("conv", other_id) not in body
    assert handle_registry.format_handle("conv", own_id) in body


def test_dispatch_put_mention_creates_no_link_to_other_conv(
    rt: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_sync_mention_links`` bypasses ``parse_link_target`` — the
    conv scope filters resolved mention targets instead, so a body
    naming another conv materialises no edge to it (the bound conv's
    own mention still links)."""
    own = "slack/T1/C1/111111"
    other = "slack/T1/C1/222222"
    monkeypatch.setenv(_READ_SCOPE_ENV, own)
    _seed_conv(store, own, ["own turn"])
    _seed_conv(store, other, ["other turn"])

    body = rt.dispatch(
        "put",
        {"kind": "memory", "text": f"compare conv:{other} with conv:{own}"},
    )
    assert "[error:" not in body, body
    mem_id = id_of(body)
    dsts = {lk.dst_ref_id for lk in store.links_for(mem_id, direction="out")}
    assert _conv_ref_id(store, own) in dsts
    assert _conv_ref_id(store, other) not in dsts


def test_dispatch_put_mention_unscoped_links_conv(
    rt: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unscoped path is unchanged: the same mention materialises
    the edge — proving the scope check, not the grammar, dropped it."""
    monkeypatch.delenv(_READ_SCOPE_ENV, raising=False)
    other = "slack/T1/C1/222222"
    _seed_conv(store, other, ["other turn"])

    body = rt.dispatch("put", {"kind": "memory", "text": f"see conv:{other}"})
    assert "[error:" not in body, body
    mem_id = id_of(body)
    dsts = {lk.dst_ref_id for lk in store.links_for(mem_id, direction="out")}
    assert _conv_ref_id(store, other) in dsts


def test_dispatch_masks_gated_kind_neighbour(
    rt: PrecisRuntime, store: Store, slack_surface: None
) -> None:
    """A gated kind never leaks through a neighbour: the todo row is
    the placeholder in the Links table, ``view='links'`` and the
    ``fisheye+1hop`` ring — title, handle and size all hidden."""
    _seed_pair(store)
    mem = _seed_memory(store, "a note linked to a todo")
    todo_id = _seed_todo(store, "todo secret title")
    store.add_link(src_ref_id=mem, dst_ref_id=todo_id, relation="related-to")

    for args in (
        {"kind": "memory", "id": mem},
        {"kind": "memory", "id": mem, "view": "links"},
        {"kind": "memory", "id": mem, "extent": "fisheye+1hop"},
    ):
        body = rt.dispatch("get", args)
        assert "[error:" not in body, (args, body)
        assert "todo (not available on this surface)" in body
        assert "todo secret title" not in body


def test_dispatch_unscoped_links_table_unchanged(
    rt: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No scope and no disabled list: the Links table keeps its
    handle/title/get-call and ``view='links'`` its target line."""
    monkeypatch.delenv(_READ_SCOPE_ENV, raising=False)
    monkeypatch.delenv(_KINDS_DISABLED_ENV, raising=False)
    _seed_pair(store)
    other_id = _conv_ref_id(store, _OTHER)
    mem = _seed_memory(store, "a note that links a conversation")
    store.add_link(src_ref_id=mem, dst_ref_id=other_id, relation="related-to")

    body = rt.dispatch("get", {"kind": "memory", "id": mem})
    assert "outside this session's scope" not in body
    assert "not available on this surface" not in body
    assert f"conv {_OTHER}" in body
    body = rt.dispatch("get", {"kind": "memory", "id": mem, "view": "links"})
    assert "outside this session's scope" not in body
    assert handle_registry.format_handle("conv", other_id) in body
