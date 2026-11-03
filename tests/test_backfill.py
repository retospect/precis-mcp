"""source-backfill slice 1 — Tier-0 dedup, seed derivation, workspace assembly.

Uses ``plan`` docs (a DraftStore tree kind, like ``draft``) built via the
``PlanHandler``, mirroring ``test_working_set_render`` / ``test_refeye``. The
recall search itself (``find_candidates``) is monkeypatched in the assembly
tests so they don't depend on embeddings — the search leg is exercised
separately by the store's own multi-search tests.
"""

from __future__ import annotations

import re
from types import SimpleNamespace as NS
from typing import Any, cast

import pytest

from precis.backfill import workspace as wsmod
from precis.backfill.candidates import RecallCandidate, draft_cited_ref_ids
from precis.backfill.workspace import _render_candidate_list, assemble
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.plan import PlanHandler
from precis.utils import handle_registry
from precis.workers.working_set import Provenance


@pytest.fixture
def plan(hub: Hub) -> PlanHandler:
    return PlanHandler(hub=hub)


def _pe(body: str) -> str:
    m = re.search(r"pe\d+", body)
    assert m is not None, f"pe-handle not found in {body!r}"
    return m.group(0)


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
    cand = RecallCandidate(
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
    out = _render_candidate_list([])
    assert "no citable candidates found" in out
    assert "search(kind='paper'" in out  # the plain next step (gr553420)


def _cand(
    ref_id: int, chunk_id: int, score: float, support: tuple[str, ...]
) -> RecallCandidate:
    return RecallCandidate(
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


def test_candidate_list_render_shows_provenance_tier() -> None:
    paper = RecallCandidate(
        ref_id=5,
        ref=NS(kind="paper", title="Peer paper"),
        chunk_id=10,
        chunk_handle="pc10",
        score=1.0,
    )
    sheet = RecallCandidate(
        ref_id=6,
        ref=NS(kind="patent", title="A patent"),
        chunk_id=11,
        chunk_handle="pk11",
        score=1.0,
    )
    out = _render_candidate_list([paper, sheet])
    assert "[peer-reviewed]" in out  # the paper's tier
    assert "[prior-art]" in out  # the patent's tier


def test_find_candidates_scopes_to_citeable_kinds_and_weights_by_tier(
    hub: Hub, plan: PlanHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.backfill.candidates import find_candidates
    from precis.backfill.provenance import PRIOR_ART, SOURCE_KINDS

    store = hub.live_store
    sec, _cited_id, _cand_id = _doc_with_citation(store, plan)
    tc = store.drafts.get_draft_chunk(sec, kind="plan")

    captured: dict[str, object] = {}

    def fake_search(**kw: object) -> list[tuple[Any, Any, float]]:
        captured.update(kw)
        block = NS(id=777)
        ref = NS(id=42, kind="patent", title="A patent")
        return [(block, ref, 1.0)]

    monkeypatch.setattr(store.chunks, "search_chunks_multi", fake_search)

    out = find_candidates(store, None, [tc], kind="plan", limit=5)
    # the sweep scopes to the citeable kinds, not a hard-coded 'paper'
    assert captured["kinds"] == list(SOURCE_KINDS) == ["paper", "patent"]
    assert len(out) == 1
    assert out[0].chunk_handle == "pk777"  # patent chunk handle
    # the prior-art tier down-weights the score vs a peer-reviewed 1.0 hit
    assert out[0].score == 1.0 * PRIOR_ART.weight


def test_candidate_ref_level_lead_shape() -> None:
    """The ref-level shape a handle-less kind would take (reachable only when
    a caller opts the kind into ``kinds=`` — nothing in the default scope
    produces one, gr553420): no chunk handle, addressed + rendered by its
    ``me<id>`` record handle, tagged ``[own-note]``."""
    lead = RecallCandidate(
        ref_id=7,
        ref=NS(kind="memory", title="my own synthesis of the transport data"),
        chunk_id=555,
        chunk_handle="",  # memory exposes no chunk handle
        score=0.4,
    )
    assert lead.is_ref_level
    assert lead.paper_handle == "me7"
    assert lead.eye_handle == "me7"  # opened + marked under the ref handle
    out = _render_candidate_list([lead])
    assert "me7" in out  # addressed at ref level
    assert "[own-note]" in out  # the lead tier
    assert "me7  ·" not in out  # no empty chunk-handle artifact / double space


def test_find_candidates_drops_internal_kind_hits(
    hub: Hub, plan: PlanHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr553420: internal kinds are never candidates — ``memory``/``gripe``
    hits the search layer returns anyway are dropped at the boundary
    (``kinds`` no longer scopes them, and the post-filter guards the
    contract whatever the search returns)."""
    from precis.backfill.candidates import find_candidates

    store = hub.live_store
    sec, _cited, _cand = _doc_with_citation(store, plan)
    tc = store.drafts.get_draft_chunk(sec, kind="plan")

    def fake_search(**kw: object) -> list[tuple[Any, Any, float]]:
        return [
            (NS(id=555), NS(id=7, kind="memory", title="ops runbook"), 1.0),
            (NS(id=556), NS(id=8, kind="gripe", title="a gripe"), 0.9),
        ]

    monkeypatch.setattr(store.chunks, "search_chunks_multi", fake_search)

    assert find_candidates(store, None, [tc], kind="plan", limit=5) == []


def test_assemble_focuses_memory_lead_as_flat_ref_eye(
    hub: Hub, plan: PlanHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ref-level hit (the opt-in handle-less shape — a fabricated memory
    candidate here) opens as a flat ``summary`` note eye addressed by ``me<id>``
    (not a ``verbatim`` chunk eye), and the whole workspace renders without the
    handle-less kind blowing up the composer."""
    store = hub.live_store
    sec, _cited_id, _cand_id = _doc_with_citation(store, plan)
    note = store.insert_ref(kind="memory", slug=None, title="my synthesis").id
    lead = RecallCandidate(
        ref_id=note,
        ref=store.fetch_refs_by_ids([note])[note],
        chunk_id=999,
        chunk_handle="",
        score=0.4,
    )
    monkeypatch.setattr(wsmod, "find_candidates", lambda *a, **k: [lead])

    ws, cands, _cited = assemble(store, hub.embedder, [sec], kind="plan")
    eye = ws.get(f"me{note}")
    assert eye is not None  # opened at ref level, addressed by me<id>
    assert eye.extent.label == "summary"  # flat note eye, not verbatim chunk

    # end-to-end render surfaces the lead + its tier and does not raise
    rendered = wsmod.render_backfill(store, hub.embedder, [sec], kind="plan")
    assert f"me{note}" in rendered
    assert "[own-note]" in rendered


# ── real-store: Tier-0 dedup ─────────────────────────────────────────


def test_draft_cited_ref_ids_finds_only_cited_papers(
    hub: Hub, plan: PlanHandler
) -> None:
    store = hub.live_store
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

    sec_chunk = store.drafts.get_draft_chunk(sec, kind="plan")
    assert sec_chunk is not None
    ref_id = sec_chunk.ref_id
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
    store = hub.live_store
    sec, cited_id, cand_id = _doc_with_citation(store, plan)
    cand = RecallCandidate(
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
    sec_eye = ws.get(sec)
    assert sec_eye is not None
    assert sec_eye.extent.label == "fisheye+1hop"
    # the cited paper is a summary (cluster-TOC) eye
    pa = handle_registry.format_handle("paper", cited_id)
    pa_eye = ws.get(pa)
    assert pa_eye is not None
    assert pa_eye.extent.label == "summary"
    # the candidate chunk is a verbatim, inferred/transient eye
    chunk_eye = ws.get("pc999")
    assert chunk_eye is not None
    assert chunk_eye.extent.label == "verbatim"
    assert chunk_eye.provenance is Provenance.INFERRED


def test_assemble_raises_on_no_live_target(hub: Hub) -> None:
    with pytest.raises(ValueError, match="no live"):
        assemble(hub.live_store, hub.embedder, ["dc999999999"], kind="draft")


def test_recall_embedder_gated_on_remote_url(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    # no remote URL configured → None (recall runs lexical + citation only,
    # never pulling torch into the agent worker)
    monkeypatch.delenv("PRECIS_EMBEDDER_URL", raising=False)
    assert wsmod.recall_embedder(hub.live_store) is None
    # a configured remote URL → a live (connection-free) remote embedder
    monkeypatch.setenv("PRECIS_EMBEDDER_URL", "http://embed.local:8900")
    emb = wsmod.recall_embedder(hub.live_store)
    assert emb is not None
    assert emb.embed_one.__self__ is emb  # it's a real embedder, not a stub


def test_render_backfill_shows_workspace_and_candidates(
    hub: Hub, plan: PlanHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = hub.live_store
    sec, _cited_id, cand_id = _doc_with_citation(store, plan)
    cand = RecallCandidate(
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
    store = hub.live_store
    sec, cited_id, cand_id = _doc_with_citation(store, plan)
    cand = RecallCandidate(
        ref_id=cand_id,
        ref=store.fetch_refs_by_ids([cand_id])[cand_id],
        chunk_id=999,
        chunk_handle="pc999",
        score=1.0,
    )
    tc = store.drafts.get_draft_chunk(sec, kind="plan")
    marks = wsmod._backfill_marks(store, [tc], [cand], kind="plan")
    cited_handle = handle_registry.format_handle("paper", cited_id)
    assert marks[cited_handle].startswith("★ cited")
    assert f"← {sec}" in marks[cited_handle]
    # a paper candidate carries its provenance tier (peer-reviewed) in the mark
    assert marks["pc999"] == "○ candidate · [peer-reviewed] · text"


# ── Build 2 §G1 — closure folds in [fi] hub supporters ────────────────


def test_draft_cited_ref_ids_includes_fi_hub_supporters(
    hub: Hub, plan: PlanHandler
) -> None:
    """A draft citing a ``[fi<hub>]`` claim hub folds the hub's evidence
    supporters (originators + corroborators) into the closure — the
    load-bearing correctness fix: once ``[pc]``/``[pa]`` cites backfill to
    ``[fi]``, those papers must not resurface as fresh gaps. A contradicting
    paper is excluded — it is not "already cited for this point."""
    from precis.taproot.canon import CanonicalClaim
    from precis.taproot.hub import attach_evidence, mint_hub

    store = hub.live_store
    supporter = store.insert_ref(kind="paper", slug="supporter", title="Supporter")
    contradictor = store.insert_ref(kind="paper", slug="contra", title="Contradictor")
    hub_ref_id = mint_hub(
        store, CanonicalClaim(sentence="Nanobuds form via X", scope={})
    )
    attach_evidence(
        store, hub_ref_id=hub_ref_id, paper_ref_id=supporter.id, role="corroborates"
    )
    attach_evidence(
        store, hub_ref_id=hub_ref_id, paper_ref_id=contradictor.id, role="contradicts"
    )

    proj = store.insert_ref(kind="todo", slug=None, title="proj").id
    plan.put(id="g1", title="Doc", project=proj)
    sec = _pe(plan.put(id="g1", text="Section", at={"last": True}).body)
    plan.put(id="g1", text=f"a settled claim [fi{hub_ref_id}]", at={"into": sec})

    sec_chunk = store.drafts.get_draft_chunk(sec, kind="plan")
    assert sec_chunk is not None
    ref_id = sec_chunk.ref_id
    got = draft_cited_ref_ids(store, ref_id, kind="plan")
    assert supporter.id in got
    assert contradictor.id not in got


def test_draft_cited_ref_ids_ignores_non_hub_finding_cite(
    hub: Hub, plan: PlanHandler
) -> None:
    """A ``[fi<id>]`` cite to a plain chase finding (never tagged
    ``TAPROOT:claim``) is silently skipped — ``derive_evidence`` only accepts
    live claim hubs, and this closure scan must not error on the rest."""
    store = hub.live_store
    plain_finding = store.insert_ref(kind="finding", slug=None, title="chase finding")

    proj = store.insert_ref(kind="todo", slug=None, title="proj").id
    plan.put(id="g1b", title="Doc", project=proj)
    sec = _pe(plan.put(id="g1b", text="Section", at={"last": True}).body)
    plan.put(id="g1b", text=f"see [fi{plain_finding.id}] for detail", at={"into": sec})

    sec_chunk = store.drafts.get_draft_chunk(sec, kind="plan")
    assert sec_chunk is not None
    ref_id = sec_chunk.ref_id
    got = draft_cited_ref_ids(store, ref_id, kind="plan")  # must not raise
    assert got == set()


# ── Build 2 §G3 — topic/categorizer precision gate ────────────────────


def test_draft_topic_slugs_picks_the_dominant_tag(hub: Hub) -> None:
    from precis.backfill.candidates import draft_topic_slugs
    from precis.store.types import Tag

    store = hub.live_store
    a = store.insert_ref(kind="paper", slug="ta", title="A").id
    b = store.insert_ref(kind="paper", slug="tb", title="B").id
    c = store.insert_ref(kind="paper", slug="tc", title="C").id
    store.add_tag(a, Tag.open("topic:nanobuds"), set_by="system")
    store.add_tag(b, Tag.open("topic:nanobuds"), set_by="system")
    store.add_tag(c, Tag.open("topic:llm"), set_by="system")

    assert draft_topic_slugs(store, {a, b, c}) == {"nanobuds"}
    assert draft_topic_slugs(store, set()) == set()  # no closure → no domain


def test_find_candidates_topic_gate_demotes_off_domain_hit(
    hub: Hub, plan: PlanHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G3: with the draft's cited paper tagged ``topic:nanobuds``, an
    off-domain (``topic:nanoribbons``) semantic hit is demoted below an
    on-domain one — even though its raw text-lens score is higher — so a
    nanobuds draft doesn't drift into adjacent-but-different corpus
    neighbours. The on-domain hit's ``lenses`` gains the ``topic`` badge."""
    from precis.backfill.candidates import LENS_TOPIC, find_candidates
    from precis.store.types import Tag

    store = hub.live_store
    sec, cited_id, _cand_id = _doc_with_citation(store, plan)
    store.add_tag(cited_id, Tag.open("topic:nanobuds"), set_by="system")

    on_domain = store.insert_ref(kind="paper", slug="ondom", title="On-domain paper")
    off_domain = store.insert_ref(kind="paper", slug="offdom", title="Off-domain paper")
    store.add_tag(on_domain.id, Tag.open("topic:nanobuds"), set_by="system")
    store.add_tag(off_domain.id, Tag.open("topic:nanoribbons"), set_by="system")

    def fake_search(**kw: object) -> list[tuple[Any, Any, float]]:
        return [
            (NS(id=201), off_domain, 1.0),  # higher raw score, off-domain
            (NS(id=202), on_domain, 0.5),  # lower raw score, on-domain
        ]

    monkeypatch.setattr(store.chunks, "search_chunks_multi", fake_search)
    tc = store.drafts.get_draft_chunk(sec, kind="plan")

    out = find_candidates(
        store, None, [tc], kind="plan", limit=5, citation_seed_ref_ids={cited_id}
    )
    # gated: on-domain outranks off-domain despite the reversed raw scores
    assert [c.ref_id for c in out] == [on_domain.id, off_domain.id]
    assert LENS_TOPIC in out[0].lenses
    assert LENS_TOPIC not in out[1].lenses
    assert out[1].score == pytest.approx(0.2)  # 1.0 * the off-domain penalty


def test_find_candidates_topic_gate_noop_without_draft_topics(
    hub: Hub, plan: PlanHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate degrades to a no-op when the draft's cited papers carry no
    ``topic:`` tag at all (``classify_topics`` is a dark/default-off pass —
    sparse-to-absent coverage is expected) — an off-domain-tagged hit is
    NOT demoted or filtered when the domain can't be derived."""
    from precis.backfill.candidates import LENS_TOPIC, find_candidates
    from precis.store.types import Tag

    store = hub.live_store
    sec, cited_id, _cand_id = _doc_with_citation(store, plan)
    # cited_id carries no topic: tag — the domain is undeterminable

    off_domain = store.insert_ref(kind="paper", slug="offdom2", title="Off-domain 2")
    store.add_tag(off_domain.id, Tag.open("topic:nanoribbons"), set_by="system")

    def fake_search(**kw: object) -> list[tuple[Any, Any, float]]:
        return [(NS(id=301), off_domain, 1.0)]

    monkeypatch.setattr(store.chunks, "search_chunks_multi", fake_search)
    tc = store.drafts.get_draft_chunk(sec, kind="plan")

    out = find_candidates(
        store, None, [tc], kind="plan", limit=5, citation_seed_ref_ids={cited_id}
    )
    assert len(out) == 1
    assert out[0].score == pytest.approx(1.0)  # not demoted
    assert LENS_TOPIC not in out[0].lenses


def test_find_candidates_topic_gate_covers_citation_lens_only_hit(
    hub: Hub, plan: PlanHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G3 fix: the gate must also cover a candidate the CITATION lens alone
    surfaces — before the fix, ``_merge_citation_recall`` appended
    citation-only hits to ``out`` *after* ``_apply_topic_gate`` had already
    run, so an off-domain citation-graph neighbour rode through undemoted
    (a violation of "nanobuds stays on nanobuds"). Here the text lens finds
    nothing at all — the off-domain hit is citation-lens-only — and it must
    still be demoted once the draft's on-domain topic is derivable."""
    from precis.backfill import citation_recall as cl
    from precis.backfill.candidates import LENS_CITATION, LENS_TOPIC, find_candidates
    from precis.store.types import Tag

    store = hub.live_store
    sec, cited_id, _cand_id = _doc_with_citation(store, plan)
    store.add_tag(cited_id, Tag.open("topic:nanobuds"), set_by="system")

    off_domain = store.insert_ref(kind="paper", slug="offdom3", title="Off-domain 3")
    store.add_tag(off_domain.id, Tag.open("topic:nanoribbons"), set_by="system")

    def fake_search(**kw: object) -> list[tuple[Any, Any, float]]:
        return []  # the text lens finds nothing — this hit is citation-only

    monkeypatch.setattr(store.chunks, "search_chunks_multi", fake_search)

    cite_cand = RecallCandidate(
        ref_id=off_domain.id,
        ref=off_domain,
        chunk_id=911,
        chunk_handle="pc911",
        score=1.0,
        lenses=(LENS_CITATION,),
    )
    monkeypatch.setattr(cl, "find_citation_candidates", lambda *a, **k: [cite_cand])

    tc = store.drafts.get_draft_chunk(sec, kind="plan")
    out = find_candidates(
        store, None, [tc], kind="plan", limit=5, citation_seed_ref_ids={cited_id}
    )

    assert len(out) == 1
    assert out[0].ref_id == off_domain.id
    assert out[0].score == pytest.approx(0.2)  # demoted despite citation-only origin
    assert LENS_TOPIC not in out[0].lenses
    assert out[0].lenses == (
        LENS_CITATION,
    )  # citation badge preserved through the gate


# ── Build 2 §G2 — whole-draft roll-up ──────────────────────────────────


def test_whole_draft_backfill_rolls_up_and_merges_recurring_candidate(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``get(kind='draft', id='<slug>', view='backfill')`` (no section
    handle) rolls up every top-level section's sweep and merges by source
    ref — a candidate both sections independently recall shows the
    recurrence glyph, and the section-scoped path (tested elsewhere in this
    file) stays untouched."""
    from precis.handlers.draft import DraftHandler

    store = hub.live_store
    draft = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id="wd", title="Whole Draft Doc", project=proj)
    ref = store.get_ref(kind="draft", id="wd")
    assert ref is not None
    title_h = store.drafts.reading_order(ref.id)[0].handle
    draft.put(id="wd", chunk_kind="heading", text="Intro", at={"after": "¶" + title_h})
    intro_h = next(
        c.handle for c in store.drafts.reading_order(ref.id) if c.text == "Intro"
    )
    draft.put(
        id="wd", chunk_kind="paragraph", text="intro body", at={"into": "¶" + intro_h}
    )
    draft.put(
        id="wd", chunk_kind="heading", text="Methods", at={"after": "¶" + intro_h}
    )
    methods_h = next(
        c.handle for c in store.drafts.reading_order(ref.id) if c.text == "Methods"
    )
    draft.put(
        id="wd",
        chunk_kind="paragraph",
        text="methods body",
        at={"into": "¶" + methods_h},
    )

    cand = store.insert_ref(
        kind="paper", slug="cand", title="Recurring RecallCandidate"
    )

    def fake_search(**kw: object) -> list[tuple[Any, Any, float]]:
        return [(NS(id=555), cand, 1.0)]

    monkeypatch.setattr(store.chunks, "search_chunks_multi", fake_search)

    n_sections = sum(1 for e in store.drafts.draft_toc(ref.id) if e.depth == 0)
    out = draft.get(id="wd", view="backfill").body
    assert f"{n_sections} section(s) scanned" in out
    assert "Recurring RecallCandidate" in out
    assert "○○" in out  # recurs across the sections it was independently recalled in


def test_assemble_draft_notes_truncation_not_a_silent_drop(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``max_candidates`` caps the merged whole-draft list, but the caller
    always learns it was capped (``truncated=True``) — never a silent drop."""
    from precis.backfill.workspace import assemble_draft
    from precis.handlers.draft import DraftHandler

    store = hub.live_store
    draft = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id="wd2", title="Doc", project=proj)
    ref = store.get_ref(kind="draft", id="wd2")
    assert ref is not None
    title_h = store.drafts.reading_order(ref.id)[0].handle
    draft.put(id="wd2", chunk_kind="heading", text="A", at={"after": "¶" + title_h})
    a_h = next(c.handle for c in store.drafts.reading_order(ref.id) if c.text == "A")
    draft.put(id="wd2", chunk_kind="paragraph", text="a body", at={"into": "¶" + a_h})

    papers = [
        store.insert_ref(kind="paper", slug=f"tp{i}", title=f"Paper {i}")
        for i in range(3)
    ]

    def fake_search(**kw: object) -> list[tuple[Any, Any, float]]:
        return [(NS(id=900 + i), p, 1.0 - i * 0.01) for i, p in enumerate(papers)]

    monkeypatch.setattr(store.chunks, "search_chunks_multi", fake_search)

    candidates, _cited, _sections, truncated = assemble_draft(
        store, hub.embedder, ref.id, max_candidates=2
    )
    assert truncated is True
    assert len(candidates) == 2


def test_assemble_draft_reraises_badinput_and_skips_dead_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr515473: BadInput is a ValueError — the per-section ``except
    ValueError: continue`` (a dead heading's skip) must not eat a
    BadInput and drop its next=."""
    from precis.backfill.workspace import assemble_draft

    monkeypatch.setattr(wsmod, "_top_level_section_handles", lambda *a, **k: ["dc1"])
    bomb = BadInput("section refused", next="the way out")

    def _boom(*a: Any, **k: Any) -> Any:
        raise bomb

    monkeypatch.setattr(wsmod, "assemble", _boom)
    with pytest.raises(BadInput) as exc:
        assemble_draft(cast(Any, None), None, 1)
    assert exc.value is bomb

    def _dead(*a: Any, **k: Any) -> Any:
        raise ValueError("no live chunk")

    monkeypatch.setattr(wsmod, "assemble", _dead)
    assert assemble_draft(cast(Any, None), None, 1) == ([], set(), ["dc1"], False)


# ── gr553420 — citable-only, semantically corroborated candidates ──────


def _seed_embedded_source(
    store: Any, e: Any, *, kind: str, slug: str | None, title: str, text: str
) -> int:
    """A ref with one body chunk embedded by the test embedder — reachable
    by the text lens's semantic leg."""
    from precis.store import ChunkInsert

    ref = store.insert_ref(kind=kind, slug=slug, title=title)
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=text, embedding=e.embed_one(text))]
    )
    return int(ref.id)


def _seed_section_keyword(store: Any, chunk_id: int, keywords: list[str]) -> None:
    """Write ``chunks.keywords`` the way the chunk_keywords worker would —
    ``seed_from_targets`` reads them through ``block_views`` as lexical legs."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET keywords = %s WHERE chunk_id = %s",
            (keywords, chunk_id),
        )
        conn.commit()


def test_find_candidates_returns_only_the_citable_paper(
    hub: Hub, plan: PlanHandler
) -> None:
    """gr553420 (test DB): a section whose nearest semantic neighbours are
    a repo-dev memory and a relevant paper returns only the paper — an ops
    note is not a citable source at all, however topically dense."""
    from precis.backfill.candidates import find_candidates

    store = hub.live_store
    e = hub.embedder
    text = "DFT reproducibility of ionic transport in solid electrolytes"
    paper_id = _seed_embedded_source(
        store, e, kind="paper", slug="iontran", title="Ionic transport", text=text
    )
    _seed_embedded_source(
        store, e, kind="memory", slug=None, title="runbook note", text=text
    )
    proj = store.insert_ref(kind="todo", slug=None, title="proj").id
    plan.put(id="p-sem", title="Doc", project=proj)
    sec = _pe(plan.put(id="p-sem", text=text, at={"last": True}).body)
    tc = store.drafts.get_draft_chunk(sec, kind="plan")
    assert tc is not None

    out = find_candidates(store, e, [tc], kind="plan", limit=5)
    assert [c.ref_id for c in out] == [paper_id]


def test_find_candidates_drops_keyword_only_name_collision(
    hub: Hub, plan: PlanHandler
) -> None:
    """gr553420 (test DB): a paper matching only the product-name keyword
    ('catpath') — the semantic leg ranks it past the relevance floor — is
    dropped; the semantically corroborated paper survives."""
    from precis.backfill.candidates import find_candidates

    store = hub.live_store
    e = hub.embedder
    seed_text = "validating the catpath migration workflow end to end"
    rel_id = _seed_embedded_source(
        store,
        e,
        kind="paper",
        slug="relpaper",
        title="Relevant paper",
        text=seed_text,
    )
    _seed_embedded_source(
        store,
        e,
        kind="paper",
        slug="catpathgpt",
        title="CatPath-GPT",
        text="catpath model predicts token paths for language models",
    )
    proj = store.insert_ref(kind="todo", slug=None, title="proj").id
    plan.put(id="p-cat", title="Doc", project=proj)
    sec = _pe(plan.put(id="p-cat", text=seed_text, at={"last": True}).body)
    tc = store.drafts.get_draft_chunk(sec, kind="plan")
    assert tc is not None
    _seed_section_keyword(store, tc.chunk_id, ["catpath"])

    out = find_candidates(store, e, [tc], kind="plan", limit=5)
    assert [c.ref_id for c in out] == [rel_id]


def test_find_candidates_empty_result_hint(hub: Hub, plan: PlanHandler) -> None:
    """gr553420 (test DB): when no citable candidate clears the floor the
    render says so plainly instead of padding the list."""
    from precis.backfill.candidates import find_candidates

    store = hub.live_store
    e = hub.embedder
    _seed_embedded_source(
        store,
        e,
        kind="paper",
        slug="onlycat",
        title="CatPath-GPT",
        text="catpath model predicts token paths for language models",
    )
    proj = store.insert_ref(kind="todo", slug=None, title="proj").id
    plan.put(id="p-none", title="Doc", project=proj)
    seed_text = "benchmarking DFT formation energies against OC20 and MACE-MP-0"
    sec = _pe(plan.put(id="p-none", text=seed_text, at={"last": True}).body)
    tc = store.drafts.get_draft_chunk(sec, kind="plan")
    assert tc is not None
    _seed_section_keyword(store, tc.chunk_id, ["catpath"])

    out = find_candidates(store, e, [tc], kind="plan", limit=5)
    assert out == []
    rendered = _render_candidate_list(out)
    assert "no citable candidates found" in rendered
    assert "search(kind='paper'" in rendered


def test_find_candidates_never_suggests_an_unciteable_datasheet(
    hub: Hub, plan: PlanHandler
) -> None:
    """gr553420 review: a datasheet the section's embedding sits right on top
    of is still not a candidate — the cite path accepts only paper and patent
    (``_ACCEPTED_SOURCE_KINDS``), so suggesting one would be unciteable noise."""
    from precis.backfill.candidates import find_candidates

    store = hub.live_store
    e = hub.embedder
    text = "DFT reproducibility of ionic transport in solid electrolytes"
    _seed_embedded_source(
        store, e, kind="datasheet", slug="ds1", title="A datasheet", text=text
    )
    proj = store.insert_ref(kind="todo", slug=None, title="proj").id
    plan.put(id="p-ds", title="Doc", project=proj)
    sec = _pe(plan.put(id="p-ds", text=text, at={"last": True}).body)
    tc = store.drafts.get_draft_chunk(sec, kind="plan")
    assert tc is not None

    assert find_candidates(store, e, [tc], kind="plan", limit=5) == []


def test_find_candidates_per_kind_floor_keeps_paper_past_a_close_patent(
    hub: Hub, plan: PlanHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr553420 review: the relevance floor's gap cut applies per kind — a
    patent at sem distance 0.20 sets its own envelope and must NOT cut a
    paper at 0.40 (one pooled list would floor at 0.20+0.15 and drop the
    paper)."""
    from precis.backfill.candidates import find_candidates

    store = hub.live_store
    sec, _cited, _cand = _doc_with_citation(store, plan)
    tc = store.drafts.get_draft_chunk(sec, kind="plan")
    patent = store.insert_ref(kind="patent", slug="pat1", title="A patent")
    paper = store.insert_ref(kind="paper", slug="pap1", title="A paper")

    def fake_search(**kw: object) -> list[tuple[Any, Any, float]]:
        cast(dict, kw["legs_out"]).update(
            {111: {"sem0": (0, 0.20)}, 222: {"sem0": (1, 0.40)}}
        )
        return [(NS(id=111), patent, 1.0), (NS(id=222), paper, 0.9)]

    monkeypatch.setattr(store.chunks, "search_chunks_multi", fake_search)

    out = find_candidates(store, hub.embedder, [tc], kind="plan", limit=5)
    assert {c.ref_id for c in out} == {patent.id, paper.id}
