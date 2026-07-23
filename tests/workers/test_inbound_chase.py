"""Scenario tests for ``precis.workers.inbound_chase``.

Mirrors ``test_chase.py``'s shape: seed a paper (+ optional citer),
mock the S2 call (:func:`precis.workers.inbound_chase.load_s2_citation_graph`),
run one pass, and assert the resulting (tag, link, event) state.
"""

from __future__ import annotations

from unittest.mock import patch

from precis.store.types import BlockInsert, Tag
from precis.workers.inbound_chase import (
    inbound_chase_enabled,
    mark_paper_active,
    run_inbound_chase_pass,
)

# ── plumbing ────────────────────────────────────────────────────────


def _seed_paper(
    store,
    *,
    cite_key: str,
    blocks: list[str] | None = None,
    identifiers: list[tuple[str, str]] | None = None,
    abstract: str | None = None,
) -> int:
    ref = store.insert_ref(
        kind="paper",
        slug=cite_key,
        title=f"Test paper {cite_key}",
        meta={"abstract": abstract} if abstract else {},
    )
    if blocks:
        store.insert_blocks(
            ref.id,
            [BlockInsert(pos=i, text=t, meta={}) for i, t in enumerate(blocks)],
        )
    if identifiers:
        with store.pool.connection() as conn:
            for id_kind, id_value in identifiers:
                conn.execute(
                    "INSERT INTO ref_identifiers "
                    "(id_kind, id_value, ref_id, source) "
                    "VALUES (%s, %s, %s, %s)",
                    (id_kind, id_value, ref.id, "test"),
                )
    return ref.id


def _ref_id_by_doi(store, doi: str) -> int | None:
    """Resolve a ref_id by DOI — robust against the auto-minted cite_key
    (derived from title/year, not the DOI) that ``upsert_stub_paper``
    picks for a citer stub."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM ref_identifiers WHERE id_kind = 'doi' AND id_value = %s",
            (doi,),
        ).fetchone()
    return int(row[0]) if row else None


def _inbound_tag(store, ref_id: int) -> str | None:
    for t in store.tags_for(ref_id):
        if getattr(t, "prefix", None) == "INBOUND":
            return t.value
    return None


def _cites_links(store, *, src: int, dst: int) -> list:
    return [
        lk
        for lk in store.links_for(src, direction="out", relation="cites")
        if lk.dst_ref_id == dst
    ]


# ── inbound_chase_enabled / mark_paper_active ────────────────────────


def test_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("PRECIS_INBOUND_CHASE_ENABLED", raising=False)
    assert inbound_chase_enabled() is False


def test_enabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_INBOUND_CHASE_ENABLED", "1")
    assert inbound_chase_enabled() is True


def test_mark_paper_active_noop_when_disabled(store, monkeypatch) -> None:
    monkeypatch.delenv("PRECIS_INBOUND_CHASE_ENABLED", raising=False)
    ref_id = _seed_paper(store, cite_key="y2020", identifiers=[("doi", "10.1/y")])
    ref = store.get_ref(kind="paper", id=ref_id)
    mark_paper_active(store, ref)
    assert _inbound_tag(store, ref_id) is None


def test_mark_paper_active_tags_pending_once(store, monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_INBOUND_CHASE_ENABLED", "1")
    ref_id = _seed_paper(store, cite_key="y2021", identifiers=[("doi", "10.1/y2")])
    ref = store.get_ref(kind="paper", id=ref_id)
    mark_paper_active(store, ref)
    assert _inbound_tag(store, ref_id) == "pending"

    # Re-triggering (a second "read") must not re-tag or reset anything —
    # the permanent, never-re-trigger contract.
    store.add_tag(
        ref_id, Tag.closed("INBOUND", "swept"), set_by="system", replace_prefix=True
    )
    mark_paper_active(store, ref)
    assert _inbound_tag(store, ref_id) == "swept"


def test_mark_paper_active_ignores_non_paper_kinds(store, monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_INBOUND_CHASE_ENABLED", "1")
    m = store.insert_ref(kind="memory", slug=None, title="not a paper")
    ref = store.get_ref(kind="memory", id=m.id)
    mark_paper_active(store, ref)
    assert _inbound_tag(store, m.id) is None


# ── run_inbound_chase_pass — sweep ────────────────────────────────────


def test_sweep_no_pending_papers_is_a_noop(store) -> None:
    assert run_inbound_chase_pass(store, limit=10) == {
        "claimed": 0,
        "ok": 0,
        "failed": 0,
    }


def test_sweep_marks_swept_when_no_identifiers(store) -> None:
    """A pending paper with no doi/arxiv/s2 id can never resolve via S2 —
    swept immediately (with a reason) instead of re-claimed forever."""
    y = _seed_paper(store, cite_key="noid2020")
    store.add_tag(y, Tag.closed("INBOUND", "pending"), set_by="system")

    result = run_inbound_chase_pass(store, limit=10, with_llm=False)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _inbound_tag(store, y) == "swept"


def test_sweep_stub_citer_links_paper_level_only(store) -> None:
    """A citer S2 knows about but the corpus doesn't hold yet: minted as
    a stub, linked immediately at the paper level; no chunk-level link
    (it has no chunks) and no crash."""
    y = _seed_paper(store, cite_key="y2020", identifiers=[("doi", "10.1/y2020")])
    store.add_tag(y, Tag.closed("INBOUND", "pending"), set_by="system")

    citers = [{"title": "A citing paper", "doi": "10.1/citer1", "year": 2022}]
    with patch(
        "precis.workers.inbound_chase.load_s2_citation_graph",
        return_value={"references": [], "cited_by": citers},
    ):
        result = run_inbound_chase_pass(store, limit=10, with_llm=False)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _inbound_tag(store, y) == "swept"

    citer_ref_id = _ref_id_by_doi(store, "10.1/citer1")
    assert citer_ref_id is not None
    links = _cites_links(store, src=citer_ref_id, dst=y)
    assert len(links) == 1
    assert links[0].src_pos is None  # ref-level, no chunks to resolve yet


def test_sweep_resolves_chunk_level_when_citer_already_has_chunks(store) -> None:
    """A citer already in corpus (real chunks) gets resolved to a
    chunk-scoped ``cites`` link in the same sweep pass — deterministic
    (with_llm=False) path: lexical-best chunk, no verdict attached. The
    ref-level "link immediately" edge from the sweep step stays too —
    both coexist (distinct rows, different chunk-id endpoints)."""
    y = _seed_paper(
        store,
        cite_key="y2020",
        identifiers=[("doi", "10.1/y2020")],
        abstract="a superconducting nanowire operating at low temperature",
    )
    store.add_tag(y, Tag.closed("INBOUND", "pending"), set_by="system")
    citer_ref_id = _seed_paper(
        store,
        cite_key="citer2020",
        identifiers=[("doi", "10.1/citer2020")],
        blocks=[
            "An unrelated paragraph about something else entirely.",
            "We build on the superconducting nanowire result at low "
            "temperature reported previously [1].",
        ],
    )

    citers = [{"title": "citer", "doi": "10.1/citer2020", "year": 2022}]
    with patch(
        "precis.workers.inbound_chase.load_s2_citation_graph",
        return_value={"references": [], "cited_by": citers},
    ):
        result = run_inbound_chase_pass(store, limit=10, with_llm=False)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}

    links = _cites_links(store, src=citer_ref_id, dst=y)
    assert len(links) == 2  # ref-level + the new chunk-scoped resolution
    chunk_link = next(lk for lk in links if lk.src_pos is not None)
    assert chunk_link.src_pos == 1  # the nanowire paragraph, not ord 0
    assert "supports" not in chunk_link.meta  # with_llm=False → no verdict


def test_sweep_is_idempotent_never_resweeps(store) -> None:
    y = _seed_paper(store, cite_key="y2020", identifiers=[("doi", "10.1/y2020")])
    store.add_tag(y, Tag.closed("INBOUND", "pending"), set_by="system")
    citers = [{"title": "citer", "doi": "10.1/citer1", "year": 2022}]
    with patch(
        "precis.workers.inbound_chase.load_s2_citation_graph",
        return_value={"references": [], "cited_by": citers},
    ) as mocked:
        run_inbound_chase_pass(store, limit=10, with_llm=False)
        # Second pass: paper is already `swept`, not re-claimed.
        run_inbound_chase_pass(store, limit=10, with_llm=False)
    assert mocked.call_count == 1


def test_sweep_self_citation_is_skipped(store) -> None:
    """S2 occasionally lists a paper as citing itself (dup/version
    noise) — must not raise the self-loop BadInput."""
    y = _seed_paper(store, cite_key="y2020", identifiers=[("doi", "10.1/y2020")])
    store.add_tag(y, Tag.closed("INBOUND", "pending"), set_by="system")
    citers = [{"title": "self", "doi": "10.1/y2020", "year": 2022}]
    with patch(
        "precis.workers.inbound_chase.load_s2_citation_graph",
        return_value={"references": [], "cited_by": citers},
    ):
        result = run_inbound_chase_pass(store, limit=10, with_llm=False)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _inbound_tag(store, y) == "swept"


# ── run_inbound_chase_pass — follow-up chunk resolution ───────────────


def test_followup_resolves_once_citer_stub_lands_chunks(store) -> None:
    """A stub citer with no chunks at sweep time gets its chunk-level
    link filled in on a *later* pass, once it lands real chunks — no
    re-fetch from S2, no re-sweep of Y."""
    y = _seed_paper(
        store,
        cite_key="y2020",
        identifiers=[("doi", "10.1/y2020")],
        abstract="graphene field-effect transistor mobility",
    )
    store.add_tag(y, Tag.closed("INBOUND", "pending"), set_by="system")
    citers = [{"title": "citer", "doi": "10.1/citer1", "year": 2022}]
    with patch(
        "precis.workers.inbound_chase.load_s2_citation_graph",
        return_value={"references": [], "cited_by": citers},
    ):
        run_inbound_chase_pass(store, limit=10, with_llm=False)

    citer_ref_id = _ref_id_by_doi(store, "10.1/citer1")
    assert citer_ref_id is not None
    assert _cites_links(store, src=citer_ref_id, dst=y)[0].src_pos is None

    # The PDF lands later (fetch_oa or similar) — chunks appear.
    store.insert_blocks(
        citer_ref_id,
        [
            BlockInsert(
                pos=0,
                text="We measured graphene field-effect transistor mobility.",
                meta={},
            )
        ],
    )

    result = run_inbound_chase_pass(store, limit=10, with_llm=False)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}

    links = _cites_links(store, src=citer_ref_id, dst=y)
    assert len(links) == 2  # ref-level (unchanged) + new chunk-scoped
    assert any(lk.src_pos == 0 for lk in links)


def test_followup_no_pairs_is_a_noop(store) -> None:
    assert run_inbound_chase_pass(store, limit=10) == {
        "claimed": 0,
        "ok": 0,
        "failed": 0,
    }


# ── LLM-verified path (with_llm=True, hooks mocked) ───────────────────


def test_sweep_records_llm_verdict_when_with_llm(store) -> None:
    y = _seed_paper(
        store,
        cite_key="y2020",
        identifiers=[("doi", "10.1/y2020")],
        abstract="a claim about battery cycling",
    )
    store.add_tag(y, Tag.closed("INBOUND", "pending"), set_by="system")
    citer_ref_id = _seed_paper(
        store,
        cite_key="citer2020",
        identifiers=[("doi", "10.1/citer2020")],
        blocks=["We confirm the battery cycling claim under similar conditions."],
    )
    citers = [{"title": "citer", "doi": "10.1/citer2020", "year": 2022}]

    with (
        patch(
            "precis.workers.inbound_chase.load_s2_citation_graph",
            return_value={"references": [], "cited_by": citers},
        ),
        patch(
            "precis.workers.inbound_chase._locate_chunk_in_target",
            side_effect=lambda **kw: kw["proposed"],
        ),
        patch(
            "precis.workers.inbound_chase._verify_support_with_caveats",
            return_value={
                "supports": "yes",
                "support_reason": "matches",
                "caveats": ["only tested at room temperature"],
                "cited_others": [],
                "terminal": True,
            },
        ),
    ):
        result = run_inbound_chase_pass(store, limit=10, with_llm=True)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}

    links = _cites_links(store, src=citer_ref_id, dst=y)
    assert len(links) == 2  # ref-level + the LLM-verified chunk-scoped edge
    chunk_link = next(lk for lk in links if lk.src_pos is not None)
    assert chunk_link.meta.get("supports") == "yes"
    assert chunk_link.meta.get("caveats") == ["only tested at room temperature"]


# ── dst_pos — second locate pass into the cited paper's own chunks ────


def test_dst_pos_populated_on_confident_second_locate(store) -> None:
    """with_llm=True: the second locate pass into Y's own chunks finds
    the matching paragraph and populates dst_pos on the same link that
    already carries src_pos (the citer's located chunk)."""
    y = _seed_paper(
        store,
        cite_key="y2020",
        identifiers=[("doi", "10.1/y2020")],
        abstract="a superconducting nanowire operating at low temperature",
        blocks=[
            "An unrelated result about something else.",
            "We report a superconducting nanowire operating at low temperature.",
        ],
    )
    store.add_tag(y, Tag.closed("INBOUND", "pending"), set_by="system")
    citer_ref_id = _seed_paper(
        store,
        cite_key="citer2020",
        identifiers=[("doi", "10.1/citer2020")],
        blocks=[
            "An unrelated paragraph about something else entirely.",
            "We build on the superconducting nanowire result at low "
            "temperature reported previously [1].",
        ],
    )
    citers = [{"title": "citer", "doi": "10.1/citer2020", "year": 2022}]

    with (
        patch(
            "precis.workers.inbound_chase.load_s2_citation_graph",
            return_value={"references": [], "cited_by": citers},
        ),
        patch(
            "precis.workers.inbound_chase._locate_chunk_in_target",
            side_effect=lambda **kw: kw["proposed"],
        ),
        patch(
            "precis.workers.inbound_chase._verify_support_with_caveats",
            return_value=None,
        ),
    ):
        result = run_inbound_chase_pass(store, limit=10, with_llm=True)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}

    links = _cites_links(store, src=citer_ref_id, dst=y)
    chunk_link = next(lk for lk in links if lk.src_pos is not None)
    assert chunk_link.src_pos == 1  # citer's nanowire paragraph
    assert chunk_link.dst_pos == 1  # Y's own nanowire paragraph


def test_dst_pos_unset_when_second_locate_has_no_confident_match(store) -> None:
    """The second locate pass returning ``None`` (no chunk of Y
    confidently matches) must NOT force a guess — dst_pos stays unset,
    the paper-level fact still stands via the ref-level link."""
    y = _seed_paper(
        store,
        cite_key="y2020",
        identifiers=[("doi", "10.1/y2020")],
        abstract="a superconducting nanowire operating at low temperature",
        blocks=["We report a superconducting nanowire operating at low temperature."],
    )
    store.add_tag(y, Tag.closed("INBOUND", "pending"), set_by="system")
    citer_ref_id = _seed_paper(
        store,
        cite_key="citer2020",
        identifiers=[("doi", "10.1/citer2020")],
        blocks=[
            "We build on the superconducting nanowire result at low temperature [1]."
        ],
    )
    citers = [{"title": "citer", "doi": "10.1/citer2020", "year": 2022}]

    calls: list[dict] = []

    def _locate_side_effect(**kw):
        calls.append(kw)
        if len(calls) == 1:
            return kw["proposed"]  # first pass: citer's own chunk, confirmed
        return None  # second pass: no confident match in Y

    with (
        patch(
            "precis.workers.inbound_chase.load_s2_citation_graph",
            return_value={"references": [], "cited_by": citers},
        ),
        patch(
            "precis.workers.inbound_chase._locate_chunk_in_target",
            side_effect=_locate_side_effect,
        ),
        patch(
            "precis.workers.inbound_chase._verify_support_with_caveats",
            return_value=None,
        ),
    ):
        result = run_inbound_chase_pass(store, limit=10, with_llm=True)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert len(calls) == 2  # both locate passes attempted

    links = _cites_links(store, src=citer_ref_id, dst=y)
    chunk_link = next(lk for lk in links if lk.src_pos is not None)
    assert chunk_link.src_pos == 0
    assert chunk_link.dst_pos is None


def test_dst_pos_never_attempted_without_llm(store) -> None:
    """with_llm=False: no second locate pass at all — dst_pos stays
    unset even though Y has plenty of body chunks to locate into."""
    y = _seed_paper(
        store,
        cite_key="y2020",
        identifiers=[("doi", "10.1/y2020")],
        abstract="a superconducting nanowire operating at low temperature",
        blocks=["We report a superconducting nanowire operating at low temperature."],
    )
    store.add_tag(y, Tag.closed("INBOUND", "pending"), set_by="system")
    citer_ref_id = _seed_paper(
        store,
        cite_key="citer2020",
        identifiers=[("doi", "10.1/citer2020")],
        blocks=[
            "We build on the superconducting nanowire result at low temperature [1]."
        ],
    )
    citers = [{"title": "citer", "doi": "10.1/citer2020", "year": 2022}]
    with patch(
        "precis.workers.inbound_chase.load_s2_citation_graph",
        return_value={"references": [], "cited_by": citers},
    ):
        result = run_inbound_chase_pass(store, limit=10, with_llm=False)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}

    links = _cites_links(store, src=citer_ref_id, dst=y)
    chunk_link = next(lk for lk in links if lk.src_pos is not None)
    assert chunk_link.dst_pos is None
