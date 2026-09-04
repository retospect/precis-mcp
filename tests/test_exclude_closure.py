"""Cite-closure resolver for exclude= — container forms (dr…/dc…) plus
[pa…]/[pc…]/[fi…] cite-token expansion.

docs/backlog/discovery-exclude-by-container.md. DB-backed (real
refs/chunks/links via the `store` fixture); a real draft is authored
through `DraftHandler.put` and a real claim hub through
`precis.taproot.hub.mint_hub`/`attach_evidence` — no mocking of the
resolver's own store calls, since the whole point is the SQL walk.
"""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers._exclude_closure import resolve_exclude_paper_ids
from precis.handlers.draft import DraftHandler
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from precis.utils import handle_registry


def _proj(hub: Hub, text: str = "proj") -> int:
    t = TodoHandler(hub=hub).put(text=text, meta={"rotation_root": True})
    return int(t.body.split("id=")[1].split()[0].rstrip(",.()"))


def _handle_of(put_body: str) -> str:
    m = re.search(r"dc\d+", put_body)
    assert m is not None, f"no dc handle in {put_body!r}"
    return m.group(0)


def _mk_paper(store: Store, *, slug: str, title: str = "A paper") -> int:
    ref = store.insert_ref(
        kind="paper",
        slug=slug,
        title=title,
        provider="manual",
        authors=[{"name": "A. Author"}],
        year=2020,
    )
    return ref.id


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _draft_ref_id(store: Store, slug: str) -> int:
    ref = store.get_ref(kind="draft", id=slug)
    assert ref is not None
    return ref.id


# ── no-op shapes ────────────────────────────────────────────────────────


def test_none_or_empty_exclude_is_a_noop(store: Store) -> None:
    assert resolve_exclude_paper_ids(None, store=store) == set()
    assert resolve_exclude_paper_ids([], store=store) == set()


def test_bare_paper_slug_still_silently_drops_when_stale(store: Store) -> None:
    """Non-container entries keep the legacy paper-search contract: a
    stale/unknown slug is dropped, not raised."""
    assert resolve_exclude_paper_ids(["does-not-exist"], store=store) == set()


# ── dr<id> — whole-draft cite closure ────────────────────────────────────


def test_dr_container_excludes_direct_pa_cite(
    store: Store, hub: Hub, draft: DraftHandler
) -> None:
    cited = _mk_paper(store, slug="cited-paper-direct")
    proj = _proj(hub)
    draft.put(id="d1", title="Draft one", project=proj)
    handle = handle_registry.format_handle("paper", cited)
    draft.put(
        id="d1",
        chunk_kind="paragraph",
        text=f"See [{handle}] for details.",
        at={"last": True},
    )
    ref_id = _draft_ref_id(store, "d1")
    got = resolve_exclude_paper_ids([f"dr{ref_id}"], store=store)
    assert got == {cited}


def test_dr_container_excludes_via_pc_owning_paper(
    store: Store, hub: Hub, draft: DraftHandler
) -> None:
    """A ``[pc…]`` paper-chunk cite resolves to its OWNING paper."""
    from precis.store.types import ChunkInsert

    cited = _mk_paper(store, slug="cited-paper-chunk")
    store.chunks.insert_chunks(cited, [ChunkInsert(ord=0, text="Intro.")])
    chunk_id = store.chunks.list_chunks_for_ref(cited)[0].id

    proj = _proj(hub)
    draft.put(id="d2", title="Draft two", project=proj)
    pc_handle = handle_registry.format_handle("paper", chunk_id, chunk=True)
    draft.put(
        id="d2", chunk_kind="paragraph", text=f"See [{pc_handle}].", at={"last": True}
    )
    ref_id = _draft_ref_id(store, "d2")
    got = resolve_exclude_paper_ids([f"dr{ref_id}"], store=store)
    assert got == {cited}


def test_dr_container_with_no_cites_excludes_nothing(
    store: Store, hub: Hub, draft: DraftHandler
) -> None:
    proj = _proj(hub)
    draft.put(id="d-empty", title="Draft empty", project=proj)
    draft.put(
        id="d-empty",
        chunk_kind="paragraph",
        text="No citations here at all.",
        at={"last": True},
    )
    ref_id = _draft_ref_id(store, "d-empty")
    assert resolve_exclude_paper_ids([f"dr{ref_id}"], store=store) == set()


# ── fi<id> — hub grounding/supporter-paper expansion ─────────────────────


def test_fi_expansion_excludes_hub_grounding_papers(
    store: Store, hub: Hub, draft: DraftHandler
) -> None:
    """A draft citing a hub (but not its grounding paper directly) still
    excludes that grounding paper — the acceptance-criteria fi-expansion
    case."""
    grounding = _mk_paper(store, slug="grounding-paper")
    claim = CanonicalClaim(
        sentence="Pd/C catalyzes Suzuki coupling at room temperature.",
        scope={"material": "Pd/C", "method": "Suzuki coupling"},
    )
    hub_ref_id = mint_hub(store, claim, set_by="agent")
    attach_evidence(
        store, hub_ref_id=hub_ref_id, paper_ref_id=grounding, role="establishes"
    )

    proj = _proj(hub)
    draft.put(id="d3", title="Draft three", project=proj)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)
    draft.put(
        id="d3",
        chunk_kind="paragraph",
        text=f"This is well established [{fi_handle}].",
        at={"last": True},
    )
    ref_id = _draft_ref_id(store, "d3")
    got = resolve_exclude_paper_ids([f"dr{ref_id}"], store=store)
    # The draft never named the grounding paper's own [pa…]/[pc…] handle —
    # the exclusion comes ONLY through the hub's evidence.
    assert got == {grounding}


def test_fi_of_non_hub_finding_contributes_nothing(
    store: Store, hub: Hub, draft: DraftHandler
) -> None:
    """A ``[fi…]`` cite of an ordinary (non-hub) finding has no evidence
    to walk — it excludes nothing, and doesn't error."""
    plain = store.insert_ref(kind="finding", slug=None, title="just a note")
    fid = plain.id

    proj = _proj(hub)
    draft.put(id="d4", title="Draft four", project=proj)
    fi_handle = handle_registry.format_handle("finding", fid)
    draft.put(
        id="d4", chunk_kind="paragraph", text=f"Noted [{fi_handle}].", at={"last": True}
    )
    ref_id = _draft_ref_id(store, "d4")
    assert resolve_exclude_paper_ids([f"dr{ref_id}"], store=store) == set()


# ── dc<id> — subtree scoping (strictly fewer than the whole draft) ───────


def test_dc_subtree_excludes_fewer_than_whole_draft(
    store: Store, hub: Hub, draft: DraftHandler
) -> None:
    a = _mk_paper(store, slug="paper-sub-a")
    b = _mk_paper(store, slug="paper-sub-b")

    proj = _proj(hub)
    draft.put(id="d5", title="Draft five", project=proj)
    sec = draft.put(id="d5", chunk_kind="heading", text="Section", at={"last": True})
    sec_h = _handle_of(sec.body)
    handle_a = handle_registry.format_handle("paper", a)
    handle_b = handle_registry.format_handle("paper", b)
    draft.put(
        id="d5",
        chunk_kind="paragraph",
        text=f"cites [{handle_a}]",
        at={"into": sec_h, "last": True},
    )
    draft.put(
        id="d5",
        chunk_kind="paragraph",
        text=f"cites [{handle_b}]",
        at={"last": True},  # outside the "Section" heading
    )

    ref_id = _draft_ref_id(store, "d5")
    whole = resolve_exclude_paper_ids([f"dr{ref_id}"], store=store)
    assert whole == {a, b}
    subtree = resolve_exclude_paper_ids([sec_h], store=store)
    assert subtree == {a}
    assert len(subtree) < len(whole)


# ── mixed containers + bare slugs ────────────────────────────────────────


def test_mixed_dr_and_paper_slug_entries(
    store: Store, hub: Hub, draft: DraftHandler
) -> None:
    cited = _mk_paper(store, slug="cited-mix")
    other = _mk_paper(store, slug="other-mix")

    proj = _proj(hub)
    draft.put(id="d6", title="Draft six", project=proj)
    handle = handle_registry.format_handle("paper", cited)
    draft.put(
        id="d6", chunk_kind="paragraph", text=f"cites [{handle}]", at={"last": True}
    )
    ref_id = _draft_ref_id(store, "d6")
    got = resolve_exclude_paper_ids([f"dr{ref_id}", "other-mix"], store=store)
    assert got == {cited, other}


# ── bogus container → BadInput naming the entry ──────────────────────────


def test_bogus_dr_raises_bad_input_naming_entry(store: Store) -> None:
    with pytest.raises(BadInput, match="dr999999"):
        resolve_exclude_paper_ids(["dr999999"], store=store)


def test_bogus_dc_raises_bad_input_naming_entry(store: Store) -> None:
    with pytest.raises(BadInput, match="dc999999"):
        resolve_exclude_paper_ids(["dc999999"], store=store)


# ── gr311339: multi-handle exclude batches into ONE round trip ──────────


def _spy(obj: object, name: str, calls: list) -> None:
    """Wrap ``obj.<name>`` (instance attribute shadowing the class method)
    so each call is recorded in ``calls`` before delegating to the real
    implementation."""
    orig = getattr(obj, name)

    def _wrapped(*args: object, **kw: object) -> object:
        calls.append((args, kw))
        return orig(*args, **kw)

    object.__setattr__(obj, name, _wrapped)


def test_multi_record_handle_exclude_batches_into_one_fetch(store: Store) -> None:
    """N ``pa<id>`` (record-form, universal-handle) exclude entries must
    resolve via ONE bulk ``fetch_refs_by_ids`` call — not N sequential
    ``resolve_handle`` round trips (gr311339: a 4-entry exclude hung
    >1800s where a 1-entry exclude was fast, because each extra handle
    entry was its own serial connection checkout + SELECT)."""
    ids = [_mk_paper(store, slug=f"batch-handle-{i}") for i in range(4)]
    handles = [handle_registry.format_handle("paper", rid) for rid in ids]

    resolve_calls: list = []
    fetch_calls: list = []
    _spy(store, "resolve_handle", resolve_calls)
    _spy(store, "fetch_refs_by_ids", fetch_calls)

    got = resolve_exclude_paper_ids(handles, store=store, kind="paper")

    assert got == set(ids)  # unchanged results
    assert resolve_calls == []  # zero per-item handle round trips
    assert len(fetch_calls) == 1  # exactly one batched fetch for all 4
    (args, kw) = fetch_calls[0]
    fetched_ids = args[0] if args else kw["ref_ids"]
    assert set(fetched_ids) == set(ids)


def test_multi_record_handle_exclude_falls_back_for_dead_handle(store: Store) -> None:
    """A dead/unresolvable handle mixed into the batch still gets its
    per-item ``resolve_handle`` fallback (correctness preserved for the
    rare case the bulk fetch can't answer), while the live handles in the
    same call still cost only the one batched fetch."""
    live = _mk_paper(store, slug="batch-live")
    live_handle = handle_registry.format_handle("paper", live)
    dead_handle = "pa999999999"  # well-formed handle shape, no such ref

    resolve_calls: list = []
    _spy(store, "resolve_handle", resolve_calls)

    got = resolve_exclude_paper_ids(
        [live_handle, dead_handle], store=store, kind="paper"
    )

    assert got == {live}  # dead handle silently drops, same as before
    assert len(resolve_calls) == 1  # only the dead one falls back
    assert resolve_calls[0][0] == (dead_handle,)
