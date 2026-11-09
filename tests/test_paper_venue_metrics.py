"""gr606422: ``venue_metrics`` — per-ISSN Scimago SJR + OpenAlex source
stats — surfaced on the paper overview and filterable via
``search(kind='paper', args={'min_quartile': 'Qn'})``.

DB-backed (``store``/``hub`` fixtures). The offline parse side (CSV
dialect, decimal-comma SJR, OpenAlex row shaping) lives in
``tests/ingest/test_venue_metrics.py``.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.store import Store, Tag
from precis.utils import handle_registry
from tests.workers._helpers import seed_chunk, seed_ref

#: Chemical Engineering Journal's ISSN — the acceptance venue (gr606422).
CEJ = "1385-8947"
CEJ_NORM = "13858947"
OTHER = "0001-0001"
OTHER_NORM = "00010001"


@pytest.fixture
def papers(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _sjr_rows() -> list[dict]:
    return [
        {
            "issn": CEJ_NORM,
            "year": 2020,
            "title": "Chemical Engineering Journal",
            "sjr": 1.5,
            "sjr_quartile": "Q2",
        },
        {
            "issn": CEJ_NORM,
            "year": 2024,
            "title": "Chemical Engineering Journal",
            "sjr": 2.71,
            "sjr_quartile": "Q1",
        },
        {
            "issn": OTHER_NORM,
            "year": 2024,
            "title": "A Regional Journal",
            "sjr": 0.4,
            "sjr_quartile": "Q3",
        },
        # OpenAlex snapshot row: same ISSN, fetch year, OA columns only —
        # the upsert's per-column COALESCE must not disturb the SJR rows.
        {
            "issn": CEJ_NORM,
            "year": 2026,
            "title": "Chemical Engineering Journal",
            "openalex_source_id": "S4210173458",
            "openalex_2yr_mean_citedness": 11.4,
            "openalex_h_index": 244,
        },
    ]


def _paper_with_issn(
    store: Store, *, title: str, issn: str, year: int = 2020, body: str = ""
) -> tuple[int, int | None]:
    """Seed a paper carrying ``meta.issn`` (+ one searchable body chunk);
    returns ``(ref_id, chunk_id)`` — the hit table renders ``pc<chunk>``
    handles, so callers assert on the chunk id."""
    rid = seed_ref(store, title=title)
    store.update_paper_fields(
        rid,
        meta_patch={"issn": issn, "journal": "J Test"},
        year=year,
        source="test",
    )
    cid = seed_chunk(store, ref_id=rid, text=body) if body else None
    return rid, cid


# ── store layer ─────────────────────────────────────────────────────


def test_upsert_and_lookup_nearest_vintage(store: Store) -> None:
    assert store.upsert_venue_metrics(_sjr_rows()) == 4

    # Latest vintage when no paper year is supplied.
    vm = store.venue_metrics_for_issn(CEJ)
    assert vm is not None
    assert vm.sjr_quartile == "Q1" and vm.sjr_year == 2024
    assert vm.sjr == pytest.approx(2.71)
    assert vm.title == "Chemical Engineering Journal"
    # The OA leg merges off the snapshot row regardless of vintage.
    assert vm.openalex_h_index == 244
    assert vm.openalex_2yr_mean_citedness == pytest.approx(11.4)
    assert vm.oa_year == 2026

    # At-or-before preference: the paper's own vintage wins.
    vm = store.venue_metrics_for_issn(CEJ, year=2021)
    assert vm is not None
    assert vm.sjr_quartile == "Q2" and vm.sjr_year == 2020

    # A paper predating every vintage gets the earliest available.
    vm = store.venue_metrics_for_issn(CEJ, year=1990)
    assert vm is not None
    assert vm.sjr_year == 2020


def test_upsert_oa_columns_never_null_sjr(store: Store) -> None:
    """An OA refresh on a shared (issn, year) row keeps the SJR half."""
    store.upsert_venue_metrics(
        [{"issn": CEJ_NORM, "year": 2024, "sjr": 2.71, "sjr_quartile": "Q1"}]
    )
    store.upsert_venue_metrics(
        [
            {
                "issn": CEJ_NORM,
                "year": 2024,
                "openalex_source_id": "S1",
                "openalex_h_index": 100,
            }
        ]
    )
    vm = store.venue_metrics_for_issn(CEJ)
    assert vm is not None
    assert vm.sjr == pytest.approx(2.71) and vm.sjr_quartile == "Q1"
    assert vm.openalex_h_index == 100


def test_upsert_skips_unparseable(store: Store) -> None:
    n = store.upsert_venue_metrics(
        [
            {"issn": "not-an-issn", "year": 2024, "sjr": 1.0},
            {"issn": CEJ_NORM, "sjr": 1.0},  # no year
            {"issn": CEJ_NORM, "year": 2024, "sjr": 1.0},
        ]
    )
    assert n == 1


def test_lookup_oa_only_venue(store: Store) -> None:
    """A venue Scimago doesn't index still shows its OpenAlex half."""
    store.upsert_venue_metrics(
        [
            {
                "issn": CEJ_NORM,
                "year": 2026,
                "title": "OA-only J",
                "openalex_source_id": "S9",
                "openalex_2yr_mean_citedness": 3.3,
            }
        ]
    )
    vm = store.venue_metrics_for_issn(CEJ)
    assert vm is not None
    assert vm.sjr is None and vm.sjr_quartile is None
    assert vm.openalex_2yr_mean_citedness == pytest.approx(3.3)


def test_lookup_unknown_issn(store: Store) -> None:
    assert store.venue_metrics_for_issn("9999-9999") is None
    assert store.venue_metrics_for_issn("garbage") is None


# ── overview ────────────────────────────────────────────────────────


def test_overview_renders_venue_metrics(papers: PaperHandler, store: Store) -> None:
    """The acceptance read: get(kind='paper') on a CEJ paper shows the
    venue's SJR + quartile + OA stats (gr606422)."""
    rid, _cid = _paper_with_issn(store, title="CEJ paper", issn=CEJ, year=2023)
    store.upsert_venue_metrics(_sjr_rows())

    body = papers.get(id=f"pa{rid}").body
    assert "venue metrics:" in body
    assert "Q1" in body and "SJR" in body
    assert "11.4" in body and "244" in body


def test_overview_quiet_without_rows_or_issn(
    papers: PaperHandler, store: Store
) -> None:
    """No venue feed coverage → no venue-metrics line (quiet, not
    a "not found" banner)."""
    no_issn = seed_ref(store, title="issn-less")
    uncovered, _c = _paper_with_issn(store, title="uncovered", issn="9999-9999")
    store.upsert_venue_metrics(_sjr_rows())

    assert "venue metrics:" not in papers.get(id=f"pa{no_issn}").body
    assert "venue metrics:" not in papers.get(id=f"pa{uncovered}").body


# ── search filter ───────────────────────────────────────────────────

QUERY = "electrocoagulation sludge dewatering"


def _corpus(
    store: Store,
) -> tuple[tuple[int, int | None], tuple[int, int | None], tuple[int, int | None]]:
    """Two venue-attested papers + one uncovered, all lexical hits for
    QUERY; each entry is ``(ref_id, chunk_id)``."""
    q1 = _paper_with_issn(
        store, title="top venue paper", issn=CEJ, body=f"{QUERY} methods"
    )
    q3 = _paper_with_issn(
        store, title="regional paper", issn=OTHER, body=f"{QUERY} review"
    )
    none = _paper_with_issn(
        store, title="no venue data", issn="9999-9999", body=f"{QUERY} notes"
    )
    store.upsert_venue_metrics(_sjr_rows())
    return q1, q3, none


def test_search_min_quartile_filters(papers: PaperHandler, store: Store) -> None:
    (q1, c1), (q3, c3), _none = _corpus(store)
    h_q1 = f"pc{c1}"  # the hit table renders chunk handles
    h_q3 = f"pc{c3}"

    body = papers.search(q=QUERY, min_quartile="Q1").body
    assert h_q1 in body
    assert h_q3 not in body

    # 'Q3' keeps Q1 and Q3 venues — the uncovered paper still drops.
    body = papers.search(q=QUERY, min_quartile="Q3").body
    assert h_q1 in body and h_q3 in body

    # 'Q4' is the loosest cohort — both attested venues pass; the
    # uncovered paper still drops.
    body = papers.search(q=QUERY, min_quartile="Q4").body
    assert h_q1 in body and h_q3 in body
    # And the cohort itself is exactly the Q1 paper.
    assert papers._resolve_quartile_scope("Q1") == {q1}


def test_min_quartile_no_feed_is_zero_hits(papers: PaperHandler, store: Store) -> None:
    """An empty venue_metrics table must not widen the search — the
    cohort is empty, so the page says no matches rather than listing
    the whole corpus."""
    rid, _c = _paper_with_issn(store, title="t", issn=CEJ, body=QUERY)
    body = papers.search(q=QUERY, min_quartile="Q1").body
    assert handle_registry.format_handle("paper", rid) not in body
    assert papers._resolve_quartile_scope("Q1") == set()


def test_min_quartile_validation(papers: PaperHandler, store: Store) -> None:
    (q1, c1), _q3, _none = _corpus(store)
    for bad in ("Q5", "q0", "SJR", "1", ""):
        with pytest.raises(BadInput, match="Q1"):
            papers.search(q=QUERY, min_quartile=bad)
    # Case-insensitive normalisation still filters.
    body = papers.search(q=QUERY, min_quartile="q1").body
    assert f"pc{c1}" in body


def test_min_quartile_alone_refuses(papers: PaperHandler, store: Store) -> None:
    """A cohort filter with nothing to narrow refuses — same posture as
    serves=/record_type= (never widens silently to the corpus)."""
    with pytest.raises(BadInput, match="min_quartile"):
        papers.search(min_quartile="Q1")


def test_min_quartile_count_view(papers: PaperHandler, store: Store) -> None:
    """view='count' applies the cohort (gr605583: never silently drop a
    filter the view can express)."""
    _corpus(store)
    body = papers.search(view="count", min_quartile="Q1").body
    assert "# 1 paper entry" in body


def test_min_quartile_composes_with_tags(papers: PaperHandler, store: Store) -> None:
    """tags-only listing + min_quartile: the quartile cohort rides the
    include channel into the recency listing too."""
    (q1, _c1), (q3, _c3), _none = _corpus(store)
    for rid in (q1, q3):
        store.add_tag(rid, Tag.open("studytype:review"), set_by="agent")
    body = papers.search(tags=["studytype:review"], min_quartile="Q1").body
    assert handle_registry.format_handle("paper", q1) in body
    assert handle_registry.format_handle("paper", q3) not in body


def test_min_quartile_null_latest_vintage_drops(
    papers: PaperHandler, store: Store
) -> None:
    """td609081: a venue whose NEWEST SJR vintage carries no quartile
    keeps no older ranking — the cohort picks the latest SJR vintage
    first, then checks its quartile; a NULL there attests "unranked
    now", not "no data"."""
    rid, cid = _paper_with_issn(store, title="lapsed venue", issn=CEJ, body=QUERY)
    store.upsert_venue_metrics(
        [
            {"issn": CEJ_NORM, "year": 2023, "sjr": 2.0, "sjr_quartile": "Q1"},
            {"issn": CEJ_NORM, "year": 2024, "sjr": 0.4, "sjr_quartile": None},
        ]
    )
    assert papers._resolve_quartile_scope("Q4") == set()
    body = papers.search(q=QUERY, min_quartile="Q4").body
    assert f"pc{cid}" not in body
    assert handle_registry.format_handle("paper", rid) not in body


def test_min_quartile_oa_snapshot_is_not_a_vintage(
    papers: PaperHandler, store: Store
) -> None:
    """An OpenAlex-only snapshot row newer than the SJR feed is not a
    new SJR vintage — it must not shadow the venue's latest quartile
    (the SJR-vintage pick requires SJR data on the row)."""
    rid, _c = _paper_with_issn(store, title="oa-covered venue", issn=CEJ, body=QUERY)
    store.upsert_venue_metrics(
        [
            {"issn": CEJ_NORM, "year": 2023, "sjr": 2.0, "sjr_quartile": "Q2"},
            {
                "issn": CEJ_NORM,
                "year": 2026,
                "openalex_source_id": "S4210173458",
                "openalex_h_index": 9,
            },
        ]
    )
    assert papers._resolve_quartile_scope("Q2") == {rid}
    assert papers._resolve_quartile_scope("Q1") == set()


def _byline_paper(
    store: Store, *, slug: str, title: str, authors: list[str], issn: str
) -> int:
    """A paper findable by ``title=`` / ``author=`` carrying
    ``meta.issn`` — the venue-cohort join key."""
    ref = store.insert_ref(
        kind="paper",
        slug=slug,
        title=title,
        authors=[{"name": a} for a in authors],
        meta={"issn": issn},
    )
    return ref.id


def test_min_quartile_applies_to_byline_lookup(
    papers: PaperHandler, store: Store
) -> None:
    """td609081: ``title=``/``author=`` record lookups honour the
    cohort — they used to return before the filter was resolved,
    silently dropping it."""
    q1 = _byline_paper(
        store,
        slug="q1-venues",
        title="Venue quartiles survey",
        authors=["Ada One"],
        issn=CEJ,
    )
    q3 = _byline_paper(
        store,
        slug="q3-venues",
        title="Venue quartiles survey",
        authors=["Bob Two"],
        issn=OTHER,
    )
    store.upsert_venue_metrics(_sjr_rows())

    body = papers.search(title="venue quartiles survey", min_quartile="Q1").body
    assert f"pa{q1}" in body
    assert f"pa{q3}" not in body

    # author= rides the same include channel.
    body = papers.search(author="Ada One", min_quartile="Q1").body
    assert f"pa{q1}" in body
    body = papers.search(author="Bob Two", min_quartile="Q1").body
    assert f"pa{q3}" not in body
    assert "min_quartile" in body  # the empty page names the filter


def test_min_quartile_good_refuses(papers: PaperHandler) -> None:
    """``good=True`` mints an async campaign with no venue cohort —
    the combination refuses rather than submitting an unfiltered
    deep search (td609081)."""
    with pytest.raises(BadInput, match="min_quartile"):
        papers.search(q=QUERY, good=True, min_quartile="Q1")


# ── cross-kind fan-out (search_hits) ────────────────────────────────


def test_search_hits_honours_min_quartile(papers: PaperHandler, store: Store) -> None:
    """The cross-kind fan-out door applies the same cohort — an arg
    passed through __extras__ must never silently drop."""
    (q1, _c1), (q3, _c3), _none = _corpus(store)
    hits = papers.search_hits(q=QUERY, min_quartile="Q1", page_size=10)
    ids = {h.ref_id for h in hits}
    assert q1 in ids and q3 not in ids
    with pytest.raises(BadInput):
        papers.search_hits(q=QUERY, min_quartile="Q7")
