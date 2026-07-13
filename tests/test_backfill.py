"""source-backfill slice 1 — Tier-0 dedup, seed derivation, workspace assembly.

Uses ``plan`` docs (a DraftMixin tree kind, like ``draft``) built via the
``PlanHandler``, mirroring ``test_working_set_render`` / ``test_refeye``. The
recall search itself (``find_candidates``) is monkeypatched in the assembly
tests so they don't depend on embeddings — the search leg is exercised
separately by the store's own multi-search tests.
"""

from __future__ import annotations

import re
from types import SimpleNamespace as NS

import pytest

from precis.backfill import workspace as wsmod
from precis.backfill.candidates import Candidate, draft_cited_ref_ids
from precis.backfill.workspace import _render_candidate_list, assemble
from precis.dispatch import Hub
from precis.handlers.plan import PlanHandler
from precis.utils import handle_registry
from precis.workers.working_set import Provenance


@pytest.fixture
def plan(hub: Hub) -> PlanHandler:
    return PlanHandler(hub=hub)


def _pe(body: str) -> str:
    return re.search(r"pe\d+", body).group(0)


# ── pure logic ───────────────────────────────────────────────────────


def test_subtree_chunks_is_heading_plus_descendants() -> None:
    from precis.backfill.candidates import _subtree_chunks

    # 1 ▸ (2, 3 ▸ 4);  5 is a sibling of 1
    chunks = [
        NS(chunk_id=1, parent_chunk_id=None),
        NS(chunk_id=2, parent_chunk_id=1),
        NS(chunk_id=3, parent_chunk_id=1),
        NS(chunk_id=4, parent_chunk_id=3),
        NS(chunk_id=5, parent_chunk_id=None),
    ]
    got = [c.chunk_id for c in _subtree_chunks(chunks, chunks[0])]
    assert got == [1, 2, 3, 4]  # excludes the sibling
    assert [c.chunk_id for c in _subtree_chunks(chunks, chunks[2])] == [3, 4]


def test_candidate_list_render_marks_gaps() -> None:
    cand = Candidate(
        ref_id=5,
        ref=NS(kind="paper", title="A Cool Paper on SEI"),
        chunk_id=10,
        chunk_handle="pc10",
        score=1.0,
    )
    out = _render_candidate_list([cand])
    assert "○" in out
    assert "pc10" in out  # the chunk to open
    assert "pa5" in out  # the paper handle
    assert "A Cool Paper on SEI" in out
    assert "text" in out  # the lens tag


def test_candidate_list_render_empty() -> None:
    assert _render_candidate_list([]).startswith("— candidate sources · none")


def _cand(
    ref_id: int, chunk_id: int, score: float, support: tuple[str, ...]
) -> Candidate:
    return Candidate(
        ref_id=ref_id,
        ref=NS(kind="paper", title=f"Paper {ref_id}"),
        chunk_id=chunk_id,
        chunk_handle=f"pc{chunk_id}",
        score=score,
        support=support,
    )


def test_merge_recurrence_ranks_cross_cutting_first() -> None:
    from precis.backfill.candidates import merge_recurrence

    # §dc1 recalls pa5 (strong) + pa6;  §dc2 recalls pa5 (weaker chunk) + pa7.
    a = [_cand(5, 10, 0.9, ("dc1",)), _cand(6, 11, 0.5, ("dc1",))]
    b = [_cand(5, 12, 0.7, ("dc2",)), _cand(7, 13, 0.8, ("dc2",))]
    out = merge_recurrence([a, b], limit=8)
    # the recurring source (both sections) ranks first, ahead of a higher-scoring
    # single-section hit (pa7 @ 0.8) — a cross-cutting gap is the stronger miss.
    assert [c.ref_id for c in out] == [5, 7, 6]
    pa5 = out[0]
    assert pa5.support == ("dc1", "dc2")  # accrued both supporting sections
    assert pa5.chunk_handle == "pc10"  # kept the best-scoring chunk
    assert pa5.score == 0.9


def test_candidate_list_render_shows_recurrence_and_support() -> None:
    recurring = _cand(5, 10, 1.0, ("dc1", "dc2"))
    single = _cand(6, 11, 1.0, ("dc7",))
    out = _render_candidate_list([recurring, single])
    assert "○○ " in out  # the recurrence glyph
    assert "recurs across dc1 dc2" in out
    assert "supports dc7" in out


# ── real-store: Tier-0 dedup ─────────────────────────────────────────


def test_draft_cited_ref_ids_finds_only_cited_papers(
    hub: Hub, plan: PlanHandler
) -> None:
    store = hub.store
    cited = store.insert_ref(kind="paper", slug="cited", title="Cited Paper")
    other = store.insert_ref(kind="paper", slug="other", title="Uncited Paper")
    note = store.insert_ref(kind="memory", slug=None, title="a note")
    proj = store.insert_ref(kind="todo", slug=None, title="proj").id
    plan.put(id="p", title="Doc", project=proj)
    sec = _pe(plan.put(id="p", text="Section", at={"last": True}).body)
    # cite the paper AND link a memory — only the paper is a "cited source".
    plan.put(
        id="p", text=f"a claim paper:{cited.id} memory:{note.id}", at={"into": sec}
    )

    ref_id = store.get_draft_chunk(sec, kind="plan").ref_id
    got = draft_cited_ref_ids(store, ref_id, kind="plan")
    assert cited.id in got
    assert other.id not in got  # never mentioned
    assert note.id not in got  # a memory is not a citeable source


# ── real-store: workspace assembly (recall monkeypatched) ────────────


def _doc_with_citation(store, plan: PlanHandler) -> tuple[str, int, int]:
    """A plan doc whose section cites one paper. Returns (section handle,
    cited paper id, candidate paper id)."""
    cited = store.insert_ref(kind="paper", slug="wang", title="Wang 2020")
    cand = store.insert_ref(kind="paper", slug="kumar", title="Kumar 2021")
    proj = store.insert_ref(kind="todo", slug=None, title="proj").id
    plan.put(id="p", title="Doc", project=proj)
    sec = _pe(plan.put(id="p", text="Ionic transport", at={"last": True}).body)
    plan.put(id="p", text=f"conductivity is high paper:{cited.id}", at={"into": sec})
    return sec, cited.id, cand.id


def test_assemble_builds_target_cited_and_candidate_eyes(
    hub: Hub, plan: PlanHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = hub.store
    sec, cited_id, cand_id = _doc_with_citation(store, plan)
    cand = Candidate(
        ref_id=cand_id,
        ref=store.fetch_refs_by_ids([cand_id])[cand_id],
        chunk_id=999,
        chunk_handle="pc999",
        score=1.0,
    )
    # exercise assembly without the embedding-dependent search leg
    monkeypatch.setattr(wsmod, "find_candidates", lambda *a, **k: [cand])

    ws, cands, cited = assemble(store, hub.embedder, [sec], kind="plan")

    assert cited_id in cited  # Tier-0 dedup set includes the cited paper
    assert cands == [cand]
    # the target section is the cursor, focused at fisheye+1hop
    assert ws.cursor == sec
    assert ws.get(sec) is not None
    assert ws.get(sec).extent.label == "fisheye+1hop"
    # the cited paper is a summary (cluster-TOC) eye
    pa = handle_registry.format_handle("paper", cited_id)
    assert ws.get(pa) is not None
    assert ws.get(pa).extent.label == "summary"
    # the candidate chunk is a verbatim, inferred/transient eye
    assert ws.get("pc999") is not None
    assert ws.get("pc999").extent.label == "verbatim"
    assert ws.get("pc999").provenance is Provenance.INFERRED


def test_assemble_raises_on_no_live_target(hub: Hub) -> None:
    with pytest.raises(ValueError, match="no live"):
        assemble(hub.store, hub.embedder, ["dc999999999"], kind="draft")


def test_recall_embedder_gated_on_remote_url(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    # no remote URL configured → None (recall runs lexical + citation only,
    # never pulling torch into the agent worker)
    monkeypatch.delenv("PRECIS_EMBEDDER_URL", raising=False)
    assert wsmod.recall_embedder(hub.store) is None
    # a configured remote URL → a live (connection-free) remote embedder
    monkeypatch.setenv("PRECIS_EMBEDDER_URL", "http://embed.local:8900")
    emb = wsmod.recall_embedder(hub.store)
    assert emb is not None
    assert emb.embed_one.__self__ is emb  # it's a real embedder, not a stub


def test_render_backfill_shows_workspace_and_candidates(
    hub: Hub, plan: PlanHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = hub.store
    sec, _cited_id, cand_id = _doc_with_citation(store, plan)
    cand = Candidate(
        ref_id=cand_id,
        ref=store.fetch_refs_by_ids([cand_id])[cand_id],
        chunk_id=999,
        chunk_handle="pc999",
        score=1.0,
    )
    monkeypatch.setattr(wsmod, "find_candidates", lambda *a, **k: [cand])

    out = wsmod.render_backfill(store, hub.embedder, [sec], kind="plan")
    assert "conductivity is high" in out  # target section body, verbatim
    assert "candidate sources" in out  # the gap list
    assert "pc999" in out  # the candidate chunk
    assert "Kumar 2021" in out  # the candidate title
    # grounding block: the section cites one paper (slug 'wang', no metadata)
    assert "grounding" in out
    assert "✓ wang" in out  # short-cite falls back to slug
    assert "single-source" in out  # only one cited paper → a coverage warning
    # folded-in source roles: the cited paper eye is stamped ★ with a back-ref
    # to the citing section; the candidate eye is stamped ○.
    assert "★ cited" in out
    assert f"← {sec}" in out  # back-ref to the section that cites it
    assert "○ candidate" in out


def test_backfill_marks_stamp_cited_and_candidate(hub: Hub, plan: PlanHandler) -> None:
    store = hub.store
    sec, cited_id, cand_id = _doc_with_citation(store, plan)
    cand = Candidate(
        ref_id=cand_id,
        ref=store.fetch_refs_by_ids([cand_id])[cand_id],
        chunk_id=999,
        chunk_handle="pc999",
        score=1.0,
    )
    tc = store.get_draft_chunk(sec, kind="plan")
    marks = wsmod._backfill_marks(store, [tc], [cand], kind="plan")
    cited_handle = handle_registry.format_handle("paper", cited_id)
    assert marks[cited_handle].startswith("★ cited")
    assert f"← {sec}" in marks[cited_handle]
    assert marks["pc999"] == "○ candidate · text"
