"""``search(q=...)`` surfaces the claim layer end to end
(the claim-layer-in-cross-kind-search design (shipped 2026-09-19) option 1) and
the ``kind='source'`` fan-out alias
(the read-for-question loop (skill precis-read-for-question) slice 5).

Dispatch-level (real store + runtime), mirroring
``test_search_source_facets.py``'s style. Unit-level coverage for
``FindingHandler.search_hits`` itself lives in ``test_finding.py``
(``TestSearchHits``); for the RRF ranking lever and the summary-cell
prefix in ``test_search_merge.py``.
"""

from __future__ import annotations

import re
from typing import Any

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.handlers.todo import TodoHandler
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from precis.utils import handle_registry


def _proj(hub: Hub) -> int:
    t = TodoHandler(hub=hub).put(text="proj", meta={"rotation_root": True})
    return int(t.body.split("id=")[1].split()[0].rstrip(",.()"))


def _mk_paper(store: Store, *, slug: str, title: str, text: str) -> tuple[int, int]:
    """Insert a paper with one searchable body chunk. Returns ``(ref_id,
    chunk_id)`` — ``PaperHandler.search_hits`` is block-level, so a paper
    hit's ``id`` cell in the merged table is the chunk handle (``pc<id>``),
    not the ref handle (``pa<id>``)."""
    ref = store.insert_ref(kind="paper", slug=slug, title=title)
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=text)])
    chunk_id = store.chunks.list_chunks_for_ref(ref.id)[0].id
    return ref.id, chunk_id


def _draft_ref_id(store: Store, slug: str) -> int:
    ref = store.get_ref(kind="draft", id=slug)
    assert ref is not None
    return ref.id


def _fi(ref_id: int) -> str:
    return handle_registry.format_handle("finding", ref_id)


def _pc(chunk_id: int) -> str:
    return handle_registry.format_handle("paper", chunk_id, chunk=True)


def _search(rt: PrecisRuntime, **args: Any) -> tuple[str, bool]:
    return rt.dispatch_with_status("search", dict(args))


# ── wildcard: the claim layer is no longer invisible ──────────────────────


def test_wildcard_search_surfaces_a_hub_with_posture_prefix(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    hub_id = mint_hub(
        store,
        CanonicalClaim(sentence="Osmium promotes furfural upgrading scl", scope={}),
    )
    supporter, _supporter_chunk = _mk_paper(
        store,
        slug="scl-supporter",
        title="Supporter",
        text="Osmium promotes furfural upgrading scl supporting text.",
    )
    attach_evidence(
        store,
        hub_ref_id=hub_id,
        paper_ref_id=supporter,
        role="establishes",
        meta={"support": "yes", "verified_by": "test"},
        set_by="system",
    )

    body, is_error = _search(rt, q="Osmium promotes furfural upgrading scl")
    assert not is_error
    assert _fi(hub_id) in body
    assert "◆" in body


def test_wildcard_search_never_returns_a_plain_chase_finding(
    runtime_with_store: PrecisRuntime,
) -> None:
    """A ``finding`` handler put with ``cited_in=`` (chase-tree, no
    supporters=) is the negative case option 1 exists to keep out of the
    default fan-out — only live claim hubs opt in."""
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    _mk_paper(
        store,
        slug="scm-src",
        title="Source",
        text="Rhodium accelerates furfural decarbonylation scm.",
    )
    from precis.handlers.finding import FindingHandler

    resp = FindingHandler(hub=rt.hub).put(
        title="in-flight claim about furfural decarbonylation scm",
        body="rhodium claim body text scm",
        cited_in="scm-src",
    )
    match = re.search(r"id=(\d+)", resp.body)
    assert match is not None
    chase_id = int(match.group(1))
    chase_handle = _fi(chase_id)

    body, is_error = _search(rt, q="Rhodium accelerates furfural decarbonylation scm")
    assert not is_error
    assert chase_handle not in body


# ── kind='source': paper + live claim hubs only ────────────────────────────


def test_kind_source_returns_only_paper_and_hub_rows(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    _paper_id, paper_chunk_id = _mk_paper(
        store,
        slug="scn-paper",
        title="Paper",
        text="Vanadium oxide catalyzes propane dehydrogenation scn.",
    )
    hub_id = mint_hub(
        store,
        CanonicalClaim(
            sentence="Vanadium oxide catalyzes propane dehydrogenation scn hub",
            scope={},
        ),
    )
    mem_ref = store.insert_ref(kind="memory", slug=None, title="a memory")
    store.chunks.insert_chunks(
        mem_ref.id,
        [
            ChunkInsert(
                ord=0, text="Vanadium oxide catalyzes propane dehydrogenation scn note."
            )
        ],
    )

    body, is_error = _search(
        rt, kind="source", q="Vanadium oxide catalyzes propane dehydrogenation scn"
    )
    assert not is_error
    assert _pc(paper_chunk_id) in body
    assert _fi(hub_id) in body
    assert "memory" not in body
    # per-kind breakdown line: exactly the two kinds kind='source' resolves to
    breakdown = [ln for ln in body.splitlines() if ln.startswith("_(per kind:")]
    assert breakdown, f"expected a per-kind breakdown line, got:\n{body}"
    assert "paper:" in breakdown[0] and "finding:" in breakdown[0]


def test_kind_source_honours_uncited_excludes_hub_and_supporter(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    supporter_id, supporter_chunk_id = _mk_paper(
        store,
        slug="sco-supporter",
        title="Supporter",
        text="Tungsten carbide enables ammonia decomposition sco supporting.",
    )
    _uncited_paper_id, uncited_chunk_id = _mk_paper(
        store,
        slug="sco-plain",
        title="Plain",
        text="Tungsten carbide enables ammonia decomposition sco unrelated paper.",
    )
    hub_id = mint_hub(
        store,
        CanonicalClaim(
            sentence="Tungsten carbide enables ammonia decomposition sco", scope={}
        ),
    )
    attach_evidence(
        store, hub_ref_id=hub_id, paper_ref_id=supporter_id, role="establishes"
    )

    proj = _proj(rt.hub)
    draft = DraftHandler(hub=rt.hub)
    draft.put(id="sco-draft", title="Draft sco", project=proj)
    fi_handle = _fi(hub_id)
    draft.put(
        id="sco-draft",
        chunk_kind="paragraph",
        text=f"Already settled [{fi_handle}].",
        at={"last": True},
    )
    draft_ref_id = _draft_ref_id(store, "sco-draft")

    body, is_error = _search(
        rt,
        kind="source",
        q="Tungsten carbide enables ammonia decomposition sco",
        uncited=f"dr{draft_ref_id}",
    )
    assert not is_error
    assert fi_handle not in body
    assert _pc(supporter_chunk_id) not in body
    assert _pc(uncited_chunk_id) in body


def test_kind_source_rejects_hubbed(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    body, is_error = _search(rt, kind="source", q="anything", hubbed=True)
    assert is_error
    assert "hubbed=" in body
    assert "source" in body
