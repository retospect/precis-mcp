"""ADR 0039 — ORCID author node, LLM-gated enqueue, and S2 author nav.

Split into:

* pure-unit coverage (iD normalisation, ORCID JSON normalisation, the
  S2 ``_format_author`` senior-flag/ORCID surfacing, the card text, and
  the missing-DOI diff against a fake store) — no DB, no network;
* a DB-backed handler resolve test guarded by the ``store`` fixture
  (skips when no test Postgres is reachable), with the ORCID API
  monkeypatched so it stays offline.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.errors import BadInput
from precis.handlers import orcid as orcid_handler
from precis.handlers.orcid import enqueue_authored_works
from precis.handlers.semanticscholar import _format_author
from precis.ingest import orcid as orcid_api

# A real, checksum-valid ORCID iD (Stephen Hawking's public iD).
_VALID_ID = "0000-0002-1825-0097"


# ── iD normalisation ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        _VALID_ID,
        "0000000218250097",
        f"orcid:{_VALID_ID}",
        f"https://orcid.org/{_VALID_ID}",
        f"  {_VALID_ID}  ",
    ],
)
def test_normalize_orcid_id_accepts_all_forms(raw: str) -> None:
    assert orcid_api.normalize_orcid_id(raw) == _VALID_ID


@pytest.mark.parametrize(
    "raw",
    [
        "https://orcid.org/0000-0002-0685-6171",
        "0000-0002-0685-6171",
        "orcid.org/0000-0002-0685-6171",
        "orcid/0000-0002-0685-6171",
        "http://orcid.org/0000-0002-0685-6171",
    ],
)
def test_normalize_orcid_id_url_and_host_forms(raw: str) -> None:
    # Every common copy-paste wrapper (scheme, bare host, ``orcid/``,
    # ``orcid.org/``) resolves to the canonical dashed iD.
    assert orcid_api.normalize_orcid_id(raw) == "0000-0002-0685-6171"


def test_normalize_orcid_id_rejects_garbage() -> None:
    with pytest.raises(BadInput):
        orcid_api.normalize_orcid_id("not-an-id")


def test_normalize_orcid_id_rejects_bad_checksum() -> None:
    # Flip the final check digit (…0097 → …0098).
    with pytest.raises(BadInput):
        orcid_api.normalize_orcid_id("0000-0002-1825-0098")


def test_slug_for() -> None:
    assert orcid_api.slug_for(_VALID_ID) == f"orcid:{_VALID_ID}"


# ── ORCID JSON normalisation ───────────────────────────────────────────


def test_normalize_work_extracts_external_ids() -> None:
    summary = {
        "title": {"title": {"value": "A Brief History"}},
        "publication-date": {"year": {"value": "1988"}},
        "external-ids": {
            "external-id": [
                {"external-id-type": "doi", "external-id-value": "10.1/ABC"},
                {"external-id-type": "arxiv", "external-id-value": "2401.00001"},
            ]
        },
        "url": {"value": "https://example.com/x"},
    }
    work = orcid_api._normalize_work(summary)
    assert work is not None
    assert work["title"] == "A Brief History"
    assert work["year"] == 1988
    assert work["doi"] == "10.1/abc"  # lower-cased
    assert work["arxiv"] == "2401.00001"
    assert work["url"] == "https://example.com/x"


def test_normalize_works_merges_group_summaries() -> None:
    works = {
        "group": [
            {
                "work-summary": [
                    {  # canonical title, no DOI
                        "title": {"title": {"value": "Paper One"}},
                        "external-ids": {"external-id": []},
                    },
                    {  # second source carries the DOI
                        "title": {"title": {"value": "Paper One (dup)"}},
                        "external-ids": {
                            "external-id": [
                                {
                                    "external-id-type": "doi",
                                    "external-id-value": "10.2/Z",
                                }
                            ]
                        },
                    },
                ]
            }
        ]
    }
    out = orcid_api._normalize_works(works)
    assert len(out) == 1
    assert out[0]["title"] == "Paper One"
    assert out[0]["doi"] == "10.2/z"


def test_normalize_person_pulls_name_bio_keywords() -> None:
    person = {
        "name": {
            "given-names": {"value": "Jane"},
            "family-name": {"value": "Doe"},
        },
        "biography": {"content": "Studies magnets."},
        "keywords": {"keyword": [{"content": "spintronics"}, {"content": "magnetism"}]},
        "addresses": {"address": [{"country": {"value": "GB"}}]},
    }
    rec = orcid_api._normalize_person(person)
    assert rec["name"] == "Jane Doe"
    assert rec["biography"] == "Studies magnets."
    assert rec["keywords"] == ["spintronics", "magnetism"]
    assert rec["country"] == "GB"


# ── S2 author formatting (§5) ──────────────────────────────────────────


def test_format_author_flags_senior_and_surfaces_orcid() -> None:
    author = {
        "authorId": "1741101",
        "name": "Senior PI",
        "externalIds": {"ORCID": _VALID_ID},
        "hIndex": 42,
        "affiliations": ["Cambridge"],
    }
    text = _format_author(author, position=4, n_authors=5)
    assert "senior (last) author" in text
    assert _VALID_ID in text
    assert "kind='orcid'" in text
    assert "h-index" in text


def test_format_author_non_senior_has_no_flag() -> None:
    author = {"authorId": "1", "name": "First Author", "externalIds": {}}
    text = _format_author(author, position=0, n_authors=5)
    assert "senior" not in text


def test_s2_parse_author_nav_keys() -> None:
    from precis.handlers.semanticscholar import SemanticScholarHandler as S2

    assert S2._parse_nav_key("authors:10.1/x") == ("authors", "10.1/x")
    assert S2._parse_nav_key("author:1741101") == ("author", "1741101")
    # Citation-graph prefixes still parse; a plain query does not.
    assert S2._parse_nav_key("refs:10.1/x") == ("refs", "10.1/x")
    assert S2._parse_nav_key("carbon nanotubes") is None


# ── card text ──────────────────────────────────────────────────────────


def test_card_text_combines_fields() -> None:
    record = {
        "orcid_id": _VALID_ID,
        "name": "Jane Doe",
        "biography": "Magnets.",
        "keywords": ["spintronics"],
        "employments": [{"organization": "Cambridge"}, {"organization": "Cambridge"}],
    }
    card = orcid_handler._card_text(record)
    assert "Jane Doe" in card
    assert "spintronics" in card
    assert card.count("Cambridge") == 1  # deduped


# ── missing-DOI diff against a fake store ──────────────────────────────


class _FakeStore:
    """Minimal store double exercising the enqueue diff branches."""

    def __init__(self, held: dict[str, int] | None = None) -> None:
        self.held = dict(held or {})
        self.next_id = 1000
        self.stubs: list[tuple[list[tuple[str, str]], str]] = []
        self.links: list[tuple[int, int, str, dict]] = []

    def find_paper_ref_by_identifier(self, value: str) -> int | None:
        return self.held.get(value)

    def upsert_stub_paper(self, *, identifiers, title, year, set_by):  # type: ignore[no-untyped-def]
        self.next_id += 1
        rid = self.next_id
        for _k, v in identifiers:
            self.held[v] = rid
        self.stubs.append((list(identifiers), set_by))
        return rid, True

    def add_link(self, *, src_ref_id, dst_ref_id, relation, set_by, meta):  # type: ignore[no-untyped-def]
        self.links.append((src_ref_id, dst_ref_id, relation, meta or {}))


def test_enqueue_links_held_unconditionally_counts_missing() -> None:
    # limit=0 (the resolve-time default): held papers are linked, missing
    # ones are only *counted* — no stubs minted (LLM-gated, ADR 0039 §4).
    store = _FakeStore(held={"10.1/held": 7})
    works = [
        {"doi": "10.1/held", "title": "Held", "year": 2020},
        {"doi": "10.2/missing", "title": "Missing", "year": 2021},
        {"arxiv": "2401.00002", "title": "Preprint", "year": 2024},
        {"title": "No identifier", "year": 2019},
    ]
    summary = enqueue_authored_works(store, 1, works)  # type: ignore[arg-type]
    assert summary["linked"] == 1
    assert summary["stubbed"] == 0  # gated off by default
    assert summary["missing_with_id"] == 2
    assert summary["missing_no_id"] == 1
    assert summary["remaining"] == 2
    assert store.stubs == []  # nothing minted
    # Only the one held paper got an authored edge.
    assert [(src, rel) for src, _d, rel, _m in store.links] == [(1, "authored")]


def test_enqueue_mints_up_to_limit() -> None:
    store = _FakeStore()
    works = [{"doi": f"10.9/p{i}", "title": f"P{i}"} for i in range(10)]
    summary = enqueue_authored_works(store, 1, works, limit=3)  # type: ignore[arg-type]
    assert summary["stubbed"] == 3
    assert summary["missing_with_id"] == 10
    assert summary["remaining"] == 7
    assert {m.get("set_by") for _s, _d, _r, m in store.links} == {"orcid"}


def test_enqueue_all_mints_every_missing() -> None:
    store = _FakeStore(held={"10.1/held": 7})
    works = [{"doi": "10.1/held"}] + [{"doi": f"10.9/p{i}"} for i in range(4)]
    summary = enqueue_authored_works(store, 1, works, limit=len(works))  # type: ignore[arg-type]
    assert summary["linked"] == 1
    assert summary["stubbed"] == 4
    assert summary["remaining"] == 0


# ── DB-backed handler resolve (skips without test Postgres) ────────────


def test_orcid_handler_resolve_then_gated_enqueue(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORCID_CLIENT_ID", "cid")
    monkeypatch.setenv("ORCID_CLIENT_SECRET", "secret")

    from precis.dispatch import Hub
    from precis.embedder import MockEmbedder
    from precis.handlers.orcid import OrcidHandler

    record = {
        "orcid_id": _VALID_ID,
        "name": "Jane Doe",
        "given": "Jane",
        "family": "Doe",
        "credit_name": "",
        "biography": "Studies magnets.",
        "keywords": ["spintronics"],
        "researcher_urls": [],
        "country": "GB",
        "employments": [
            {"organization": "Cambridge", "ror": "https://ror.org/013meh722"}
        ],
        "works": [
            {
                "doi": "10.1021/orcidtest.1",
                "title": "New Paper",
                "year": 2024,
                "url": "",
            }
        ],
        "work_count": 1,
    }
    monkeypatch.setattr(orcid_handler.orcid_api, "fetch_record", lambda _id: record)
    monkeypatch.setattr(
        orcid_handler.orcid_api, "fetch_works_only", lambda _id: record["works"]
    )

    embedder = MockEmbedder(dim=store.embedding_dim())
    handler = OrcidHandler(hub=Hub(store=store, embedder=embedder))

    # Plain resolve: stores the node, reports the missing count + affordance,
    # but mints NO stub (LLM-gated).
    resp = handler.get(id=_VALID_ID)
    assert "Jane Doe" in resp.body
    assert _VALID_ID in resp.body
    assert "1 with a DOI" in resp.body  # missing-with-id count surfaced
    assert "'enqueue'" in resp.body  # the affordance
    assert store.find_paper_ref_by_identifier("10.1021/orcidtest.1") is None

    ref = store.get_ref(kind="orcid", id=f"orcid:{_VALID_ID}")
    assert ref is not None

    # Now the agent opts in: enqueue all missing → the stub is minted + linked.
    resp2 = handler.get(id=_VALID_ID, enqueue="all")
    assert "Enqueued 1 fetch stub" in resp2.body
    paper_ref_id = store.find_paper_ref_by_identifier("10.1021/orcidtest.1")
    assert paper_ref_id is not None
    links = store.links_for(ref.id, relation="authored", direction="out")
    assert any(l.dst_ref_id == paper_ref_id for l in links)
