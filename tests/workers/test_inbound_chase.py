"""Scenario tests for ``precis.workers.inbound_chase``.

Mirrors ``test_chase.py``'s shape: seed a paper (+ optional citer),
mock the S2 call (:func:`precis.workers.inbound_chase.load_s2_citation_graph`),
run one pass, and assert the resulting (tag, link, event) state.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import patch

from precis.store.types import ChunkInsert, Tag
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
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=i, text=t, meta={}) for i, t in enumerate(blocks)],
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
    picks for a citer stub. Normalises first (gr514420): stored DOIs are
    trigger-lowercased, so a mixed-case probe must run through
    ``normalize_doi`` too or it silently misses."""
    from precis.identity import normalize_doi

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM ref_identifiers WHERE id_kind = 'doi' AND id_value = %s",
            (normalize_doi(doi),),
        ).fetchone()
    return int(row[0]) if row else None


def _inbound_tag(store, ref_id: int) -> str | None:
    for t in store.tags_for(ref_id):
        if getattr(t, "prefix", None) == "INBOUND":
            return t.value
    return None


def test_ref_id_by_doi_normalizes_mixed_case_probe(store) -> None:
    """gr514420: stored DOIs are canonical lowercase (``normalize_doi``
    at the write chokepoints, the 0049 trigger at the storage layer),
    so the helper must normalise its probe too — a mixed-case DOI
    silently returned ``None`` before."""
    ref_id = _seed_paper(
        store, cite_key="mixcase20", identifiers=[("doi", "10.1/bigA")]
    )
    assert _ref_id_by_doi(store, "10.1/biga") == ref_id
    assert _ref_id_by_doi(store, "10.1/BigA") == ref_id
    assert _ref_id_by_doi(store, "10.1/nope") is None


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
    assert links[0].src_ord is None  # ref-level, no chunks to resolve yet


def test_sweep_minted_citer_has_no_s2_enriched_stamp_without_extra_s2_fields(
    store,
) -> None:
    """``ingest.citations._get_citations``'s ``cited_by`` entries are the
    narrow doi/s2_id/title/year shape (no abstract/fields/citation_count)
    — ``_resolve_or_ingest_citer`` must NOT stamp ``s2_enriched_at`` on a
    mint from that shape alone, or ``stub_rank``'s real enrich pass would
    be permanently skipped for an empty patch."""
    y = _seed_paper(store, cite_key="y2020b", identifiers=[("doi", "10.1/y2020b")])
    store.add_tag(y, Tag.closed("INBOUND", "pending"), set_by="system")

    citers = [{"title": "A bare citing paper", "doi": "10.1/citer-bare", "year": 2022}]
    with patch(
        "precis.workers.inbound_chase.load_s2_citation_graph",
        return_value={"references": [], "cited_by": citers},
    ):
        run_inbound_chase_pass(store, limit=10, with_llm=False)

    citer_ref_id = _ref_id_by_doi(store, "10.1/citer-bare")
    assert citer_ref_id is not None
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (citer_ref_id,)
        ).fetchone()
    assert row is not None
    meta = dict(row[0] or {})
    assert "s2_enriched_at" not in meta


def test_sweep_minted_citer_carries_s2_meta_when_source_dict_has_it(store) -> None:
    """When the ``cited_by`` entry DOES carry abstract/fields/citation_count
    (a richer S2 shape than ``ingest.citations`` produces today, or a
    future extension of it), the mint writes ``s2_enriched_at`` + the
    abstract so ``stub_rank`` skips the redundant round-trip."""
    y = _seed_paper(store, cite_key="y2020c", identifiers=[("doi", "10.1/y2020c")])
    store.add_tag(y, Tag.closed("INBOUND", "pending"), set_by="system")

    citers = [
        {
            "title": "A richly-described citing paper",
            "doi": "10.1/citer-rich",
            "year": 2022,
            "abstract": "the citer's abstract",
            "fields": ["Computer Science"],
            "citation_count": 5,
        }
    ]
    with patch(
        "precis.workers.inbound_chase.load_s2_citation_graph",
        return_value={"references": [], "cited_by": citers},
    ):
        run_inbound_chase_pass(store, limit=10, with_llm=False)

    citer_ref_id = _ref_id_by_doi(store, "10.1/citer-rich")
    assert citer_ref_id is not None
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (citer_ref_id,)
        ).fetchone()
    assert row is not None
    meta = dict(row[0] or {})
    assert meta.get("s2_enriched_at") is not None
    assert meta.get("abstract") == "the citer's abstract"
    assert meta.get("s2_fields") == ["Computer Science"]
    assert meta.get("s2_citation_count") == 5


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
    chunk_link = next(lk for lk in links if lk.src_ord is not None)
    assert chunk_link.src_ord == 1  # the nanowire paragraph, not ord 0
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


# ── per-paper candidate cap (todo 489406) ─────────────────────────────


def test_sweep_per_paper_cap_skips_over_cap_candidates(store, monkeypatch) -> None:
    """``PRECIS_INBOUND_CHASE_MAX_CANDIDATES_PER_PAPER`` bounds how many
    S2 citers one activated paper ever processes — candidates over the
    cap are counted and skipped (no stub mint, no link), and the paper
    still marks ``swept`` (the cap is the policy, not a partial pass).
    The most-cited candidates win the bounded slots."""
    monkeypatch.setenv("PRECIS_INBOUND_CHASE_MAX_CANDIDATES_PER_PAPER", "2")
    y = _seed_paper(store, cite_key="y2020", identifiers=[("doi", "10.1/ycap")])
    store.add_tag(y, Tag.closed("INBOUND", "pending"), set_by="system")

    citers = [
        {"title": "minor one", "doi": "10.1/minor1", "year": 2022, "citation_count": 1},
        {
            "title": "landmark A",
            "doi": "10.1/big-a",
            "year": 2021,
            "citation_count": 900,
        },
        {"title": "minor two", "doi": "10.1/minor2", "year": 2023, "citation_count": 4},
        {
            "title": "landmark B",
            "doi": "10.1/big-b",
            "year": 2020,
            "citation_count": 700,
        },
    ]
    with patch(
        "precis.workers.inbound_chase.load_s2_citation_graph",
        return_value={"references": [], "cited_by": citers},
    ):
        result = run_inbound_chase_pass(store, limit=10, with_llm=False)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _inbound_tag(store, y) == "swept"

    # Only the two most-cited candidates were queued.
    assert _ref_id_by_doi(store, "10.1/big-a") is not None
    assert _ref_id_by_doi(store, "10.1/big-b") is not None
    assert _ref_id_by_doi(store, "10.1/minor1") is None
    assert _ref_id_by_doi(store, "10.1/minor2") is None

    # The skipped tail is counted on the swept event payload.
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT payload FROM ref_events "
            "WHERE ref_id = %s AND source = 'inbound_chase' AND event = 'swept'",
            (y,),
        ).fetchone()
    assert row is not None
    payload = dict(row[0])
    assert payload["citers_found"] == 4
    assert payload["capped"] == 2


def test_sweep_per_paper_cap_zero_processes_nothing(store, monkeypatch) -> None:
    """Cap of 0 is the knob's floor: every candidate is skipped, the
    paper still stamps swept with all of them counted as capped."""
    monkeypatch.setenv("PRECIS_INBOUND_CHASE_MAX_CANDIDATES_PER_PAPER", "0")
    y = _seed_paper(store, cite_key="y2020", identifiers=[("doi", "10.1/ycap0")])
    store.add_tag(y, Tag.closed("INBOUND", "pending"), set_by="system")
    citers = [{"title": "citer", "doi": "10.1/citer1", "year": 2022}]
    with patch(
        "precis.workers.inbound_chase.load_s2_citation_graph",
        return_value={"references": [], "cited_by": citers},
    ):
        run_inbound_chase_pass(store, limit=10, with_llm=False)
    assert _inbound_tag(store, y) == "swept"
    assert _ref_id_by_doi(store, "10.1/citer1") is None


# ── daily ceiling across papers (todo 489406) ─────────────────────────

_DAY1 = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
_DAY2 = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def _daily_counter(store, day: datetime) -> int | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = %s",
            (f"inbound_chase:daily:{day.date().isoformat()}",),
        ).fetchone()
    return None if row is None else int(row[0])


def test_daily_ceiling_stops_sweep_and_resumes_next_utc_day(store, monkeypatch) -> None:
    """``PRECIS_INBOUND_CHASE_DAILY_LIMIT`` bounds candidates chased per
    UTC day across all papers. Mid-sweep exhaustion ends the pass
    cleanly and unstamped (the paper stays ``INBOUND:pending``); a
    same-day re-pass is a no-op; on the next UTC day the already-chased
    prefix is skipped for free and the sweep finishes."""
    monkeypatch.setenv("PRECIS_INBOUND_CHASE_DAILY_LIMIT", "2")
    y = _seed_paper(store, cite_key="y2020", identifiers=[("doi", "10.1/ydaily")])
    store.add_tag(y, Tag.closed("INBOUND", "pending"), set_by="system")
    citers = [
        {"title": f"citer {i}", "doi": f"10.1/dc{i}", "year": 2022} for i in range(4)
    ]
    graph = {"references": [], "cited_by": citers}

    with patch(
        "precis.workers.inbound_chase.load_s2_citation_graph",
        return_value=graph,
    ):
        result = run_inbound_chase_pass(store, limit=10, with_llm=False, now=_DAY1)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    # Two candidates chased, the pass stopped before the rest, and the
    # paper was NOT stamped swept — it re-claims tomorrow.
    assert _inbound_tag(store, y) == "pending"
    assert _daily_counter(store, _DAY1) == 2
    assert _ref_id_by_doi(store, "10.1/dc0") is not None
    assert _ref_id_by_doi(store, "10.1/dc1") is not None
    assert _ref_id_by_doi(store, "10.1/dc2") is None

    with patch(
        "precis.workers.inbound_chase.load_s2_citation_graph",
        return_value=graph,
    ):
        # Same UTC day: the ceiling is already spent — the pass ends
        # cleanly before claiming anything.
        assert run_inbound_chase_pass(store, limit=10, with_llm=False, now=_DAY1) == {
            "claimed": 0,
            "ok": 0,
            "failed": 0,
        }
        assert _inbound_tag(store, y) == "pending"

        # Next UTC day: the done prefix is skipped without spending
        # (else a paper with more citers than the ceiling would
        # livelock re-chasing its own prefix), the remaining two are
        # chased, and the paper finally stamps swept.
        result = run_inbound_chase_pass(store, limit=10, with_llm=False, now=_DAY2)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _inbound_tag(store, y) == "swept"
    assert _daily_counter(store, _DAY2) == 2
    assert _ref_id_by_doi(store, "10.1/dc2") is not None
    assert _ref_id_by_doi(store, "10.1/dc3") is not None


def test_daily_ceiling_counts_across_papers(store, monkeypatch) -> None:
    """The ceiling is cross-paper: two pending papers share the same
    UTC-day counter, so the second paper's sweep inherits the first's
    spend and stops cleanly."""
    monkeypatch.setenv("PRECIS_INBOUND_CHASE_DAILY_LIMIT", "1")
    y1 = _seed_paper(store, cite_key="y2020a", identifiers=[("doi", "10.1/ya")])
    y2 = _seed_paper(store, cite_key="y2020b", identifiers=[("doi", "10.1/yb")])
    for y in (y1, y2):
        store.add_tag(y, Tag.closed("INBOUND", "pending"), set_by="system")

    citers = [{"title": "citer", "doi": "10.1/shared", "year": 2022}]
    with patch(
        "precis.workers.inbound_chase.load_s2_citation_graph",
        return_value={"references": [], "cited_by": citers},
    ):
        result = run_inbound_chase_pass(store, limit=10, with_llm=False, now=_DAY1)
    assert result == {"claimed": 2, "ok": 1, "failed": 0}
    # The single daily unit went to the first paper; the second is
    # skipped once the budget reads empty (unstamped).
    assert _daily_counter(store, _DAY1) == 1
    assert _inbound_tag(store, y1) == "swept"
    assert _inbound_tag(store, y2) == "pending"

    with patch(
        "precis.workers.inbound_chase.load_s2_citation_graph",
        return_value={"references": [], "cited_by": citers},
    ):
        run_inbound_chase_pass(store, limit=10, with_llm=False, now=_DAY2)
    assert _inbound_tag(store, y2) == "swept"


def test_daily_ceiling_also_bounds_followup_resolution(store, monkeypatch) -> None:
    """The follow-up chunk-resolution arm spends the same counter: a
    spent ceiling leaves a landed stub's chunk link unresolved until
    the next UTC day."""
    monkeypatch.setenv("PRECIS_INBOUND_CHASE_DAILY_LIMIT", "1")
    y = _seed_paper(store, cite_key="y2020", identifiers=[("doi", "10.1/yfu")])
    store.add_tag(y, Tag.closed("INBOUND", "pending"), set_by="system")
    citers = [{"title": "citer", "doi": "10.1/fu1", "year": 2022}]
    with patch(
        "precis.workers.inbound_chase.load_s2_citation_graph",
        return_value={"references": [], "cited_by": citers},
    ):
        run_inbound_chase_pass(store, limit=10, with_llm=False, now=_DAY1)
    citer_ref_id = _ref_id_by_doi(store, "10.1/fu1")
    assert citer_ref_id is not None
    assert _inbound_tag(store, y) == "swept"

    # Chunks land; still the same UTC day — the follow-up claim exists
    # but the ceiling is spent, so the pair waits.
    store.chunks.insert_chunks(
        citer_ref_id,
        [ChunkInsert(ord=0, text="we cite the target paper's claim.", meta={})],
    )
    result = run_inbound_chase_pass(store, limit=10, with_llm=False, now=_DAY1)
    assert result == {"claimed": 0, "ok": 0, "failed": 0}
    assert all(
        lk.src_ord is None for lk in _cites_links(store, src=citer_ref_id, dst=y)
    )

    result = run_inbound_chase_pass(store, limit=10, with_llm=False, now=_DAY2)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert any(lk.src_ord == 0 for lk in _cites_links(store, src=citer_ref_id, dst=y))


# ── job-digest surface (todo 489406: "cap visible in the job digest") ──


def _digest_payloads(caplog) -> list[dict]:
    """The ``extra={"payload": ...}`` dicts on this pass's own INFO
    digest rows (logger-derived ``pass='inbound_chase'`` in
    worker_logs)."""
    return [
        dict(r.payload)
        for r in caplog.records
        if isinstance(getattr(r, "payload", None), dict)
        and r.payload.get("handler") == "inbound_chase"
        and "remaining_today" in r.payload
    ]


def test_pass_digest_line_reports_chase_metrics(store, monkeypatch, caplog) -> None:
    """One working pass emits one worker_logs digest row carrying the
    cap surface — chased, capped_per_paper, ceiling_hit,
    remaining_today — without duplicating the runner's claimed/ok/
    failed keys (they'd skew activity_by_handler's filters)."""
    monkeypatch.setenv("PRECIS_INBOUND_CHASE_MAX_CANDIDATES_PER_PAPER", "2")
    y = _seed_paper(store, cite_key="y2020", identifiers=[("doi", "10.1/ydigest")])
    store.add_tag(y, Tag.closed("INBOUND", "pending"), set_by="system")
    citers = [
        {"title": f"citer {i}", "doi": f"10.1/dg{i}", "year": 2022} for i in range(3)
    ]
    with (
        caplog.at_level(logging.INFO, logger="precis.workers.inbound_chase"),
        patch(
            "precis.workers.inbound_chase.load_s2_citation_graph",
            return_value={"references": [], "cited_by": citers},
        ),
    ):
        run_inbound_chase_pass(store, limit=10, with_llm=False, now=_DAY1)
    rows = _digest_payloads(caplog)
    assert len(rows) == 1
    row = rows[0]
    assert row["chased"] == 2
    assert row["capped_per_paper"] == 1
    assert row["ceiling_hit"] is False
    assert row["remaining_today"] == 200 - 2
    assert not ({"claimed", "ok", "failed"} & row.keys())


def test_pass_digest_reports_ceiling_hit(store, monkeypatch, caplog) -> None:
    """A pass stopped by the spent ceiling (and one that finds it
    already spent before claiming) says so on the digest line."""
    monkeypatch.setenv("PRECIS_INBOUND_CHASE_DAILY_LIMIT", "1")
    y = _seed_paper(store, cite_key="y2020", identifiers=[("doi", "10.1/yd2")])
    store.add_tag(y, Tag.closed("INBOUND", "pending"), set_by="system")
    citers = [
        {"title": f"citer {i}", "doi": f"10.1/dh{i}", "year": 2022} for i in range(2)
    ]
    with (
        caplog.at_level(logging.INFO, logger="precis.workers.inbound_chase"),
        patch(
            "precis.workers.inbound_chase.load_s2_citation_graph",
            return_value={"references": [], "cited_by": citers},
        ),
    ):
        run_inbound_chase_pass(store, limit=10, with_llm=False, now=_DAY1)
        # Same UTC day, counter spent — the early-return path digests too.
        run_inbound_chase_pass(store, limit=10, with_llm=False, now=_DAY1)
    rows = _digest_payloads(caplog)
    assert len(rows) == 2
    assert rows[0]["chased"] == 1 and rows[0]["ceiling_hit"] is True
    assert rows[0]["remaining_today"] == 0
    assert rows[1]["chased"] == 0 and rows[1]["ceiling_hit"] is True


def test_module_docstring_cites_the_shipped_breaker() -> None:
    """The module docstring used to call the global spend circuit
    breaker "implemented but unshipped" — it has shipped
    (``precis.budget.breaker.gate_tier``, invoked from
    ``utils/llm/router.route``). The docstring must cite where the
    breaker lives and must never reintroduce the stale claim."""
    import precis.workers.inbound_chase as mod

    doc = mod.__doc__ or ""
    assert "precis.budget.breaker" in doc
    assert "unshipped" not in doc


def test_flag_off_nothing_runs(store, monkeypatch, caplog) -> None:
    """``PRECIS_INBOUND_CHASE_ENABLED`` dark: ``mark_paper_active``
    writes no tag, so the pass claims nothing — no daily-counter row,
    no digest line."""
    monkeypatch.delenv("PRECIS_INBOUND_CHASE_ENABLED", raising=False)
    ref_id = _seed_paper(store, cite_key="y2020", identifiers=[("doi", "10.1/yoff")])
    ref = store.get_ref(kind="paper", id=ref_id)
    with caplog.at_level(logging.INFO, logger="precis.workers.inbound_chase"):
        mark_paper_active(store, ref)
        assert _inbound_tag(store, ref_id) is None
        assert run_inbound_chase_pass(store, limit=10, with_llm=False) == {
            "claimed": 0,
            "ok": 0,
            "failed": 0,
        }
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM app_state WHERE key LIKE 'inbound_chase:daily:%'"
        ).fetchone()
    assert row is None
    assert _digest_payloads(caplog) == []


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
    assert _cites_links(store, src=citer_ref_id, dst=y)[0].src_ord is None

    # The PDF lands later (fetch_oa or similar) — chunks appear.
    store.chunks.insert_chunks(
        citer_ref_id,
        [
            ChunkInsert(
                ord=0,
                text="We measured graphene field-effect transistor mobility.",
                meta={},
            )
        ],
    )

    result = run_inbound_chase_pass(store, limit=10, with_llm=False)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}

    links = _cites_links(store, src=citer_ref_id, dst=y)
    assert len(links) == 2  # ref-level (unchanged) + new chunk-scoped
    assert any(lk.src_ord == 0 for lk in links)


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
    chunk_link = next(lk for lk in links if lk.src_ord is not None)
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
    chunk_link = next(lk for lk in links if lk.src_ord is not None)
    assert chunk_link.src_ord == 1  # citer's nanowire paragraph
    assert chunk_link.dst_ord == 1  # Y's own nanowire paragraph


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
    chunk_link = next(lk for lk in links if lk.src_ord is not None)
    assert chunk_link.src_ord == 0
    assert chunk_link.dst_ord is None


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
    chunk_link = next(lk for lk in links if lk.src_ord is not None)
    assert chunk_link.dst_ord is None


# ── import-time dependency guard ────────────────────────────────────


def test_importable_without_semanticscholar() -> None:
    """``semanticscholar`` ships in the ``[paper]`` extra, but this module
    is on the ``get(kind='paper')`` read path via ``mark_paper_active`` —
    it must import in venvs without that extra (asa's /opt/asa). Guards
    against a module-level import creeping back into the
    ``inbound_chase`` → ``chase`` chain.
    """
    import subprocess
    import sys

    script = (
        "import builtins\n"
        "_real = builtins.__import__\n"
        "def _guard(name, *a, **k):\n"
        "    if name.split('.')[0] == 'semanticscholar':\n"
        "        raise ModuleNotFoundError(name)\n"
        "    return _real(name, *a, **k)\n"
        "builtins.__import__ = _guard\n"
        "import precis.workers.inbound_chase\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.returncode == 0, proc.stderr
