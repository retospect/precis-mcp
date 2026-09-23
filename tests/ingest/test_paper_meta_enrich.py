"""Paper metadata enrichment — one Crossref (+conditional OpenAlex) fetch
per paper, re-resolving authors/entry_type/journal/idents/retraction.

Resolver clients are injected so nothing hits the network.
"""

from __future__ import annotations

from typing import Any

import pytest

import precis.ingest.paper_meta_enrich as paper_meta_enrich
from precis.identity import PLACEHOLDER_TITLE
from precis.ingest.paper_meta_enrich import (
    RESOLVED_AT_KEY,
    SOURCE_KEY,
    enrich_paper,
)
from precis.store import Store

_VALID_ORCID = "0000-0002-1825-0097"


def _paper(
    store: Store,
    *,
    slug: str,
    title: str = "X",
    doi: str | None = None,
    authors: list[Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> int:
    ref = store.insert_ref(
        kind="paper", slug=slug, title=title, meta=meta or {}, authors=authors
    )
    if doi:
        store.set_ref_identifier(ref.id, "doi", doi, source="manual")
    return ref.id


def _ref(store: Store, ref_id: int):
    r = store.fetch_refs_by_ids([ref_id])[ref_id]
    return r


def _crossref_msg(
    *,
    doi: str = "10.1234/x",
    family: str = "Goldsmith",
    given: str = "Bryan R.",
    orcid: str | None = None,
    entry_type: str = "journal-article",
    journal: str = "J. Phys. Chem.",
    issn: str = "1234-5678",
    abstract: str = "An abstract.",
    update_to: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    author: dict[str, Any] = {"given": given, "family": family}
    if orcid:
        author["ORCID"] = f"https://orcid.org/{orcid}"
    msg: dict[str, Any] = {
        "title": ["A Paper"],
        "author": [author],
        "published-print": {"date-parts": [[2020, 1, 1]]},
        "container-title": [journal],
        "ISSN": [issn],
        "abstract": abstract,
        "type": entry_type,
        "DOI": doi,
    }
    if update_to is not None:
        msg["update-to"] = update_to
    return msg


def _link_exists(store: Store, src: int, dst: int, relation: str) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM links WHERE src_ref_id=%s AND dst_ref_id=%s AND relation=%s",
            (src, dst, relation),
        ).fetchone()
    return row is not None


class TestCrossrefAuthorReplace:
    def test_flat_author_becomes_structured_via_crossref(self, store: Store) -> None:
        rid = _paper(
            store,
            slug="p1",
            doi="10.1234/x",
            authors=[{"name": "Goldsmith, Bryan R."}],
        )
        msg = _crossref_msg(orcid=_VALID_ORCID)
        outcome = enrich_paper(
            store,
            rid,
            doi="10.1234/x",
            crossref_fn=lambda doi, mailto: msg,
            openalex_fn=lambda doi, **k: None,
        )
        assert outcome is not None
        ref = _ref(store, rid)
        assert ref.authors == [
            {
                "given": "Bryan R.",
                "family": "Goldsmith",
                "orcid": _VALID_ORCID,
            }
        ]
        assert (ref.meta or {}).get(SOURCE_KEY) == "crossref"
        assert (ref.meta or {}).get("entry_type") == "journal-article"
        assert (ref.meta or {}).get("journal") == "J. Phys. Chem."
        assert (ref.meta or {}).get("issn") == "1234-5678"
        assert (ref.meta or {}).get("abstract") == "An abstract."
        assert RESOLVED_AT_KEY in (ref.meta or {})

    def test_orcid_mints_and_links_author_node(self, store: Store) -> None:
        rid = _paper(store, slug="p2", doi="10.1234/y")
        msg = _crossref_msg(doi="10.1234/y", orcid=_VALID_ORCID)
        outcome = enrich_paper(
            store,
            rid,
            doi="10.1234/y",
            crossref_fn=lambda doi, mailto: msg,
            openalex_fn=lambda doi, **k: None,
        )
        assert outcome is not None
        assert outcome.orcid_links == 1
        orcid_ref = store.get_ref(kind="orcid", id=f"orcid:{_VALID_ORCID}")
        assert orcid_ref is not None
        assert _link_exists(store, orcid_ref.id, rid, "authored")

    def test_extra_openalex_ids_land_in_ref_identifiers(self, store: Store) -> None:
        rid = _paper(store, slug="p3", doi="10.1234/z")
        msg = _crossref_msg(doi="10.1234/z")
        work = {
            "id": "https://openalex.org/W123",
            "ids": {
                "openalex": "https://openalex.org/W123",
                "pmid": "https://pubmed.ncbi.nlm.nih.gov/98765",
                "mag": "555",
            },
        }
        outcome = enrich_paper(
            store,
            rid,
            doi="10.1234/z",
            crossref_fn=lambda doi, mailto: msg,
            openalex_fn=lambda doi, **k: work,
        )
        assert outcome is not None
        assert outcome.extra_identifiers == 3
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id_kind, id_value FROM ref_identifiers WHERE ref_id=%s "
                "ORDER BY id_kind",
                (rid,),
            ).fetchall()
        kinds = {r[0]: r[1] for r in rows}
        assert kinds["openalex"] == "w123"
        assert kinds["pubmed"] == "98765"
        assert kinds["mag"] == "555"

    def test_openalex_not_fetched_when_crossref_misses(self, store: Store) -> None:
        rid = _paper(store, slug="p4", doi="10.1234/miss")
        called = {"openalex": False}

        def _boom(doi: str, **k: Any) -> None:
            called["openalex"] = True
            return None

        outcome = enrich_paper(
            store,
            rid,
            doi="10.1234/miss",
            crossref_fn=lambda doi, mailto: None,
            openalex_fn=_boom,
        )
        assert outcome is not None
        assert called["openalex"] is False


class TestOpenAlexAuthorMerge:
    """precis.utils.authors module docstring — the OpenAlex leg (fetched
    only when Crossref resolved) merges ``openalex_author_id`` (and a
    missing ``orcid``) onto the Crossref author entries, never reordering
    or dropping one."""

    def test_merge_by_position_when_counts_match(self, store: Store) -> None:
        rid = _paper(store, slug="oa1", doi="10.1234/oa1")
        msg = _crossref_msg(doi="10.1234/oa1")  # single author, no ORCID
        msg["author"] = [
            {"given": "Bryan R.", "family": "Goldsmith"},
            {"given": "Jane", "family": "Doe"},
        ]
        work = {
            "authorships": [
                {
                    "author": {
                        "id": "https://openalex.org/A1111111111",
                        "display_name": "Bryan R. Goldsmith",
                        "orcid": "https://orcid.org/0000-0002-1825-0097",
                    }
                },
                {
                    "author": {
                        "id": "https://openalex.org/A2222222222",
                        "display_name": "Jane Doe",
                    }
                },
            ]
        }
        outcome = enrich_paper(
            store,
            rid,
            doi="10.1234/oa1",
            crossref_fn=lambda doi, mailto: msg,
            openalex_fn=lambda doi, **k: work,
        )
        assert outcome is not None
        ref = _ref(store, rid)
        assert ref.authors == [
            {
                "given": "Bryan R.",
                "family": "Goldsmith",
                "orcid": "0000-0002-1825-0097",
                "openalex_author_id": "A1111111111",
            },
            {
                "given": "Jane",
                "family": "Doe",
                "openalex_author_id": "A2222222222",
            },
        ]

        rows = store.get_paper_authors(rid)
        assert [r["source"] for r in rows] == ["crossref", "crossref"]
        assert rows[0]["orcid"] == "0000-0002-1825-0097"
        assert rows[0]["openalex_author_id"] == "A1111111111"
        assert rows[1]["openalex_author_id"] == "A2222222222"
        # ORCID mint+link now sees the OpenAlex-backfilled orcid too.
        assert outcome.orcid_links == 1

    def test_merge_by_family_name_when_counts_differ(self, store: Store) -> None:
        rid = _paper(store, slug="oa2", doi="10.1234/oa2")
        msg = _crossref_msg(doi="10.1234/oa2")
        msg["author"] = [
            {"given": "Bryan R.", "family": "Goldsmith"},
            {"given": "Jane", "family": "Doe"},
        ]
        # OpenAlex lists a third (e.g. a corrected/duplicate) authorship —
        # counts differ, so the merge falls back to family-name match.
        work = {
            "authorships": [
                {
                    "author": {
                        "id": "https://openalex.org/A333",
                        "display_name": "J Doe",
                    }
                },
                {
                    "author": {
                        "id": "https://openalex.org/A444",
                        "display_name": "B. GOLDSMITH",
                    }
                },
                {
                    "author": {
                        "id": "https://openalex.org/A555",
                        "display_name": "Extra One",
                    }
                },
            ]
        }
        outcome = enrich_paper(
            store,
            rid,
            doi="10.1234/oa2",
            crossref_fn=lambda doi, mailto: msg,
            openalex_fn=lambda doi, **k: work,
        )
        assert outcome is not None
        ref = _ref(store, rid)
        by_family = {a["family"]: a for a in ref.authors}
        assert by_family["Goldsmith"]["openalex_author_id"] == "A444"
        assert by_family["Doe"]["openalex_author_id"] == "A333"
        # Crossref order/entries are preserved regardless of match order.
        assert [a["family"] for a in ref.authors] == ["Goldsmith", "Doe"]

    def test_shared_family_name_is_not_paired(self, store: Store) -> None:
        # Two co-authors named Wang: pairing by surname would stamp one
        # author's identity on the other, so neither gets an id; the
        # unique surname still pairs.
        rid = _paper(store, slug="oa4", doi="10.1234/oa4")
        msg = _crossref_msg(doi="10.1234/oa4")
        msg["author"] = [
            {"given": "Li", "family": "Wang"},
            {"given": "Wei", "family": "Wang"},
            {"given": "Jane", "family": "Doe"},
        ]
        work = {
            "authorships": [
                {
                    "author": {
                        "id": "https://openalex.org/A1",
                        "display_name": "Li Wang",
                    }
                },
                {
                    "author": {
                        "id": "https://openalex.org/A2",
                        "display_name": "Wei Wang",
                    }
                },
                {
                    "author": {
                        "id": "https://openalex.org/A3",
                        "display_name": "Jane Doe",
                    }
                },
                {
                    "author": {
                        "id": "https://openalex.org/A9",
                        "display_name": "Extra One",
                    }
                },
            ]
        }
        outcome = enrich_paper(
            store,
            rid,
            doi="10.1234/oa4",
            crossref_fn=lambda doi, mailto: msg,
            openalex_fn=lambda doi, **k: work,
        )
        assert outcome is not None
        rows = store.get_paper_authors(rid)
        assert [r["openalex_author_id"] for r in rows] == [None, None, "A3"]

    def test_existing_crossref_orcid_is_not_overwritten(self, store: Store) -> None:
        rid = _paper(store, slug="oa3", doi="10.1234/oa3")
        msg = _crossref_msg(doi="10.1234/oa3", orcid=_VALID_ORCID)
        other_orcid = "0000-0001-2345-6789"
        work = {
            "authorships": [
                {
                    "author": {
                        "id": "https://openalex.org/A666",
                        "display_name": "Bryan R. Goldsmith",
                        "orcid": f"https://orcid.org/{other_orcid}",
                    }
                }
            ]
        }
        outcome = enrich_paper(
            store,
            rid,
            doi="10.1234/oa3",
            crossref_fn=lambda doi, mailto: msg,
            openalex_fn=lambda doi, **k: work,
        )
        assert outcome is not None
        ref = _ref(store, rid)
        assert ref.authors[0]["orcid"] == _VALID_ORCID
        assert ref.authors[0]["openalex_author_id"] == "A666"


class TestHumanVerifiedGuard:
    def test_verified_paper_authors_untouched_other_meta_filled(
        self, store: Store
    ) -> None:
        rid = _paper(
            store, slug="p5", doi="10.1234/v", authors=[{"name": "Human, Fixed"}]
        )
        store.set_human_verified(rid, by="operator")
        msg = _crossref_msg(doi="10.1234/v", family="Different", given="Author")
        outcome = enrich_paper(
            store,
            rid,
            doi="10.1234/v",
            crossref_fn=lambda doi, mailto: msg,
            openalex_fn=lambda doi, **k: None,
        )
        assert outcome is not None
        assert outcome.skipped_authors is True
        ref = _ref(store, rid)
        # The byline is the paper_authors projection (S1): the unambiguous
        # "Family, Given" string is split at insert, and stays that way.
        assert ref.authors == [{"given": "Fixed", "family": "Human"}]
        # other meta fields still fill in.
        assert (ref.meta or {}).get("entry_type") == "journal-article"
        assert SOURCE_KEY not in (ref.meta or {})
        assert RESOLVED_AT_KEY in (ref.meta or {})


class TestRetraction:
    def test_retracted_paper_gets_retraction_status(self, store: Store) -> None:
        rid = _paper(store, slug="p6", doi="10.1234/r")
        msg = _crossref_msg(
            doi="10.1234/r",
            update_to=[
                {
                    "type": "retraction",
                    "DOI": "10.1234/r-notice",
                    "updated": {"date-parts": [[2021, 5, 1]]},
                }
            ],
        )
        outcome = enrich_paper(
            store,
            rid,
            doi="10.1234/r",
            crossref_fn=lambda doi, mailto: msg,
            openalex_fn=lambda doi, **k: None,
        )
        assert outcome is not None
        assert outcome.retraction_status == "retracted"
        ref = _ref(store, rid)
        assert ref.retraction_status == "retracted"
        assert ref.retraction_reason == "10.1234/r-notice"
        assert ref.retraction_url == "https://doi.org/10.1234/r-notice"

    def test_clean_paper_retraction_status_left_alone(self, store: Store) -> None:
        rid = _paper(store, slug="p7", doi="10.1234/clean")
        msg = _crossref_msg(doi="10.1234/clean")
        enrich_paper(
            store,
            rid,
            doi="10.1234/clean",
            crossref_fn=lambda doi, mailto: msg,
            openalex_fn=lambda doi, **k: None,
        )
        ref = _ref(store, rid)
        assert ref.retraction_status is None

    def test_clean_crossref_read_stamps_checked_at(self, store: Store) -> None:
        """A clean answer is still an answer, and has to be recorded.

        Stamping only flagged papers leaves "checked, clean" identical to
        "never looked", so the TTL gate in ``ingest/provenance.py`` can
        never short-circuit and every trigger re-fetches forever.
        """
        rid = _paper(store, slug="p7b", doi="10.1234/clean2")
        msg = _crossref_msg(doi="10.1234/clean2")
        enrich_paper(
            store,
            rid,
            doi="10.1234/clean2",
            crossref_fn=lambda doi, mailto: msg,
            openalex_fn=lambda doi, **k: None,
        )
        ref = _ref(store, rid)
        assert ref.retraction_status is None
        assert ref.retraction_checked_at is not None

    def test_crossref_miss_leaves_checked_at_unset(self, store: Store) -> None:
        """A fetch that came back empty is not a check.

        Stamping it would poison the TTL: we would skip this paper for 30
        days on the strength of a lookup that never happened.
        """
        rid = _paper(store, slug="p7c", doi="10.1234/miss")
        enrich_paper(
            store,
            rid,
            doi="10.1234/miss",
            crossref_fn=lambda doi, mailto: None,
            openalex_fn=lambda doi, **k: None,
        )
        ref = _ref(store, rid)
        assert ref.retraction_checked_at is None


class TestDoiLessHeuristic:
    def test_heuristic_split_and_junk_flush(self, store: Store) -> None:
        rid = _paper(
            store,
            slug="p8",
            authors=["Smith, John", "REFERENCES"],
        )
        outcome = enrich_paper(store, rid, doi=None)
        assert outcome is not None
        ref = _ref(store, rid)
        assert ref.authors == [{"given": "John", "family": "Smith"}]
        assert (ref.meta or {}).get(SOURCE_KEY) == "heuristic"

    def test_crossref_miss_falls_back_to_heuristic(self, store: Store) -> None:
        rid = _paper(
            store,
            slug="p9",
            doi="10.1234/miss2",
            authors=["Jones, Alice"],
        )
        outcome = enrich_paper(
            store,
            rid,
            doi="10.1234/miss2",
            crossref_fn=lambda doi, mailto: None,
            openalex_fn=lambda doi, **k: None,
        )
        assert outcome is not None
        assert outcome.authors_source == "heuristic"
        ref = _ref(store, rid)
        assert ref.authors == [{"given": "Alice", "family": "Jones"}]


class TestMissingBlanksOnly:
    def test_journal_not_overwritten_when_already_set(self, store: Store) -> None:
        rid = _paper(
            store,
            slug="p10",
            doi="10.1234/j",
            meta={"journal": "Existing Journal"},
        )
        msg = _crossref_msg(doi="10.1234/j", journal="Crossref Journal")
        enrich_paper(
            store,
            rid,
            doi="10.1234/j",
            crossref_fn=lambda doi, mailto: msg,
            openalex_fn=lambda doi, **k: None,
        )
        ref = _ref(store, rid)
        assert (ref.meta or {}).get("journal") == "Existing Journal"

    def test_entry_type_not_overwritten_when_already_set(self, store: Store) -> None:
        """A human edit via the web Meta tab's entry_type form (or any
        prior write) on a not-yet-enriched paper must survive this pass —
        entry_type fills blanks only, same as journal/issn/abstract."""
        rid = _paper(
            store,
            slug="p11",
            doi="10.1234/et",
            meta={"entry_type": "book-chapter"},
        )
        msg = _crossref_msg(doi="10.1234/et", entry_type="journal-article")
        outcome = enrich_paper(
            store,
            rid,
            doi="10.1234/et",
            crossref_fn=lambda doi, mailto: msg,
            openalex_fn=lambda doi, **k: None,
        )
        assert outcome is not None
        assert outcome.entry_type is None
        ref = _ref(store, rid)
        assert (ref.meta or {}).get("entry_type") == "book-chapter"


class TestMissingRef:
    def test_returns_none_for_unknown_ref(self, store: Store) -> None:
        assert enrich_paper(store, 999_999_999, doi=None) is None


class TestStampOrdering:
    """``authors_resolved_at`` is written last — a raise anywhere in the
    side-effect chain (author/meta write, ORCID mint+link, card rebuild)
    must leave the ref unstamped so the next pass retries it, even though
    already-committed side effects (like the author replace here) stay
    durable."""

    def test_card_rebuild_failure_leaves_ref_unstamped(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        rid = _paper(store, slug="p12", doi="10.1234/ord")
        msg = _crossref_msg(doi="10.1234/ord")

        def _boom(store_: Store, ref_id: int) -> None:
            raise RuntimeError("card rebuild blew up")

        monkeypatch.setattr(paper_meta_enrich, "_rebuild_cards", _boom)

        with pytest.raises(RuntimeError):
            enrich_paper(
                store,
                rid,
                doi="10.1234/ord",
                crossref_fn=lambda doi, mailto: msg,
                openalex_fn=lambda doi, **k: None,
            )

        ref = _ref(store, rid)
        # The author replace already landed (durable, from the earlier tx)...
        assert ref.authors == [{"given": "Bryan R.", "family": "Goldsmith"}]
        # ...but the idempotency stamp was never reached.
        assert RESOLVED_AT_KEY not in (ref.meta or {})

    def test_orcid_mint_is_get_or_create_safe_to_retry(self, store: Store) -> None:
        rid = _paper(store, slug="p13", doi="10.1234/retry")
        msg = _crossref_msg(doi="10.1234/retry", orcid=_VALID_ORCID)

        for _ in range(2):
            outcome = enrich_paper(
                store,
                rid,
                doi="10.1234/retry",
                crossref_fn=lambda doi, mailto: msg,
                openalex_fn=lambda doi, **k: None,
            )
            assert outcome is not None
            assert outcome.orcid_links == 1

        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT count(*) FROM links WHERE dst_ref_id=%s AND relation='authored'",
                (rid,),
            ).fetchone()
        assert rows is not None and rows[0] == 1
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT count(*) FROM refs WHERE kind='orcid'"
            ).fetchone()
        assert rows is not None and rows[0] == 1


class TestPlaceholderTitleFill:
    """The no-title sentinel is a hole to fill, a real title is not.

    A DOI-only acquire mints its stub with ``PLACEHOLDER_TITLE`` and no
    year; before this, the pass normalized Crossref's answer, kept the
    journal/ISSN/abstract/byline, and dropped the title — leaving a row
    that looked enriched and read as ``(no title)``.
    """

    def test_placeholder_title_is_replaced_by_crossref(self, store: Store) -> None:
        rid = _paper(store, slug="p20", title=PLACEHOLDER_TITLE, doi="10.1234/ph")
        msg = _crossref_msg(doi="10.1234/ph")
        outcome = enrich_paper(
            store,
            rid,
            doi="10.1234/ph",
            crossref_fn=lambda doi, mailto: msg,
            openalex_fn=lambda doi, **k: None,
        )
        assert outcome is not None
        assert outcome.title_filled == "A Paper"
        assert outcome.year_filled == 2020
        ref = _ref(store, rid)
        assert ref.title == "A Paper"
        assert ref.year == 2020

    def test_blank_title_is_replaced_too(self, store: Store) -> None:
        rid = _paper(store, slug="p21", title="   ", doi="10.1234/blank")
        msg = _crossref_msg(doi="10.1234/blank")
        enrich_paper(
            store,
            rid,
            doi="10.1234/blank",
            crossref_fn=lambda doi, mailto: msg,
            openalex_fn=lambda doi, **k: None,
        )
        assert _ref(store, rid).title == "A Paper"

    def test_real_title_is_never_clobbered(self, store: Store) -> None:
        rid = _paper(
            store, slug="p22", title="The Author's Own Title", doi="10.1234/keep"
        )
        msg = _crossref_msg(doi="10.1234/keep")
        outcome = enrich_paper(
            store,
            rid,
            doi="10.1234/keep",
            crossref_fn=lambda doi, mailto: msg,
            openalex_fn=lambda doi, **k: None,
        )
        assert outcome is not None
        assert outcome.title_filled is None
        assert _ref(store, rid).title == "The Author's Own Title"

    def test_year_fills_independently_of_the_title(self, store: Store) -> None:
        """A properly-titled ref can still be missing its year."""
        rid = _paper(store, slug="p23", title="Titled Already", doi="10.1234/yr")
        msg = _crossref_msg(doi="10.1234/yr")
        outcome = enrich_paper(
            store,
            rid,
            doi="10.1234/yr",
            crossref_fn=lambda doi, mailto: msg,
            openalex_fn=lambda doi, **k: None,
        )
        assert outcome is not None
        assert outcome.year_filled == 2020
        assert _ref(store, rid).year == 2020

    def test_existing_year_is_not_overwritten(self, store: Store) -> None:
        ref = store.insert_ref(
            kind="paper", slug="p24", title=PLACEHOLDER_TITLE, year=1999
        )
        store.set_ref_identifier(ref.id, "doi", "10.1234/oldyear", source="manual")
        msg = _crossref_msg(doi="10.1234/oldyear")
        outcome = enrich_paper(
            store,
            ref.id,
            doi="10.1234/oldyear",
            crossref_fn=lambda doi, mailto: msg,
            openalex_fn=lambda doi, **k: None,
        )
        assert outcome is not None
        assert outcome.year_filled is None
        after = _ref(store, ref.id)
        assert after.year == 1999
        assert after.title == "A Paper"

    def test_no_doi_leaves_the_placeholder_alone(self, store: Store) -> None:
        """Nothing to look up — the sentinel survives rather than being
        replaced by a guess."""
        rid = _paper(store, slug="p25", title=PLACEHOLDER_TITLE)
        outcome = enrich_paper(
            store,
            rid,
            doi=None,
            crossref_fn=lambda doi, mailto: None,
            openalex_fn=lambda doi, **k: None,
        )
        assert outcome is not None
        assert outcome.title_filled is None
        assert _ref(store, rid).title == PLACEHOLDER_TITLE
