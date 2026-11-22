"""PaperHandler.acquire — the gated dream stub-mint tool (#9).

Mirrors the supersede test shape: handler-level invocation (MCP wiring
is deferred to the agent loop). S2 enrichment is patched out so every
test stays offline; the identifier parser is unit-tested standalone.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers import paper as paper_mod
from precis.handlers.paper import (
    PaperHandler,
    _lookup_acquire_metadata,
    _parse_acquire_identifier,
)
from precis.store import Store
from precis.store._stub_predicate import ACQUIRE_RESTAMP_MIN_HOURS
from tests.conftest import record_handle


def test_doi_alias_check_without_pdf_extras() -> None:
    """Exercise the DOI registry check in a fresh process without RapidFuzz."""
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from unittest.mock import patch

sys.modules["rapidfuzz"] = None

from precis.handlers import paper

doi = "10.1039/c7cp06180c"
registry = {"title": "Graphene heterostructures"}
s2 = {"title": "Unrelated mining research", "s2_id": "other-work"}
with patch.object(paper, "_lookup_doi_registry", return_value=registry):
    assert paper._poisoned_s2_cluster_registry(s2, doi) == registry
""",
        ],
        check=True,
    )


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: a placeholder S2 lookup that satisfies the verify=True
    validator without populating any stub fields, and a DOI-registry
    fallback that knows nothing. Individual tests override the S2 stub
    to return real metadata, or ``None`` to exercise the DOI-registry
    fallback and the hallucinated-identifier rejection path (gr486090).
    """
    monkeypatch.setattr(
        paper_mod, "_lookup_acquire_metadata", lambda *_: {"_resolved": True}
    )
    monkeypatch.setattr(paper_mod, "_lookup_doi_registry", lambda *_: None)


def _ref_id(body: str) -> int:
    return int(body.split("id=pa", 1)[1].split()[0])


def _identifiers(store: Store, ref_id: int) -> set[tuple[str, str]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id_kind, id_value FROM ref_identifiers WHERE ref_id = %s",
            (ref_id,),
        ).fetchall()
    return {(str(k), str(v)) for k, v in rows}


def _tags(store: Store, ref_id: int) -> set[str]:
    return {str(t) for t in store.tags_for(ref_id)}


# ── identifier parser (pure) ────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("doi:10.1/X", ("doi", "10.1/x")),
        ("arxiv:2401.00001", ("arxiv", "2401.00001")),
        ("s2:abc123", ("s2", "abc123")),
        ("pubmed:99", ("pubmed", "99")),
        # PMCID (gr663681): PMC-prefix optional, value canonicalises to
        # the lowercase ``pmc<digits>`` form ref_identifiers stores.
        ("pmcid:PMC11089570", ("pmcid", "pmc11089570")),
        ("pmcid:pmc11089570", ("pmcid", "pmc11089570")),
        ("pmcid:11089570", ("pmcid", "pmc11089570")),
        ("pmcid:", None),
        ("pmcid:PMC", None),
        ("pmcid:notanid", None),
        ("10.1038/nature10352", ("doi", "10.1038/nature10352")),
        # Canonicalisation (gr484234): DOI wrappers/case and arXiv
        # version suffixes collapse to the ref_identifiers form.
        ("DOI:10.1/X", ("doi", "10.1/x")),
        ("https://doi.org/10.1/X", ("doi", "10.1/x")),
        ("http://dx.doi.org/10.1/x", ("doi", "10.1/x")),
        ("doi.org/10.1/x", ("doi", "10.1/x")),
        ("doi:https://doi.org/10.1/X", ("doi", "10.1/x")),
        ("2401.00001", ("arxiv", "2401.00001")),
        ("2401.00001v2", ("arxiv", "2401.00001")),
        ("arxiv:2401.00001v3", ("arxiv", "2401.00001")),
        ("https://arxiv.org/abs/2401.00001", ("arxiv", "2401.00001")),
        ("cs.LG/0501001", ("arxiv", "cs.LG/0501001")),
        ("not an id", None),
        ("", None),
        ("doi:", None),
        ("doi:not.a.doi", ("doi", "not.a.doi")),
    ],
)
def test_parse_acquire_identifier(raw: str, expected: tuple[str, str] | None) -> None:
    assert _parse_acquire_identifier(raw) == expected


def test_detect_identifier_scheme_pmcid() -> None:
    """``PMC…`` / ``pmcid:…`` forms resolve through the generic paper
    identifier door so ``get(kind='paper', id='PMC11089570')`` finds the
    stub an acquire minted (gr663681). Bare digits stay ``pubmed``."""
    from precis.store._identifiers_ops import (
        _normalise_identifier,
        detect_identifier_scheme,
    )

    assert detect_identifier_scheme("PMC11089570") == "pmcid"
    assert detect_identifier_scheme("pmcid:PMC11089570") == "pmcid"
    assert detect_identifier_scheme("PMC:11089570") == "pmcid"
    assert detect_identifier_scheme("11089570") == "pubmed"
    assert _normalise_identifier("pmcid", "PMCID:PMC11089570") == "pmc11089570"
    assert _normalise_identifier("pmcid", "PMC11089570") == "pmc11089570"
    assert _normalise_identifier("pmcid", "PMC:11089570") == "pmc11089570"
    assert _normalise_identifier("pmcid", "pmcid:11089570") == "pmc11089570"


def test_lookup_acquire_metadata_routes_pubmed_and_pmcid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pubmed``/``pmcid`` identifiers resolve through S2's external-id
    endpoint (``PMID:``/``PMCID:``) — the gap that made pmcid acquires
    unverifiable (gr663681). Calls the real helper, not the autouse
    stub — the top-level import binds the original function object,
    unaffected by the fixture's module-attr patch."""
    calls: list[str] = []

    def _fake(paper_id: str, api_key: str = "", *, propagate: bool = False) -> dict:
        calls.append(paper_id)
        return {"title": "resolved"}

    monkeypatch.setattr("precis.ingest.semantic_scholar.get_paper_by_id", _fake)
    assert _lookup_acquire_metadata("pubmed", "12345678") == {"title": "resolved"}
    assert _lookup_acquire_metadata("pmcid", "pmc11089570") == {"title": "resolved"}
    assert calls == ["pmid:12345678", "pmcid:PMC11089570"]


# ── guards ──────────────────────────────────────────────────────────


def test_acquire_requires_identifier_or_title(handler: PaperHandler) -> None:
    with pytest.raises(BadInput, match="identifier= .* or title="):
        handler.acquire()


def test_acquire_rejects_unrecognised_identifier(handler: PaperHandler) -> None:
    with pytest.raises(BadInput, match="unrecognised identifier"):
        handler.acquire(identifier="gibberish")


def test_acquire_rejects_dead_context_ref(handler: PaperHandler) -> None:
    with pytest.raises(BadInput, match="not a live ref"):
        handler.acquire(identifier="doi:10.1/x", context_ref_id=999999)


def test_acquire_rejects_non_int_context_ref(handler: PaperHandler) -> None:
    with pytest.raises(BadInput, match="must be an int or a handle"):
        handler.acquire(identifier="doi:10.1/x", context_ref_id="abc")


def test_acquire_rejects_dead_handle_context_ref(handler: PaperHandler) -> None:
    # A well-formed handle naming no live ref fails the same way a dead
    # numeric id does — before any write.
    with pytest.raises(BadInput, match="not a live ref"):
        handler.acquire(identifier="doi:10.1/x", context_ref_id="pa999999999")


# ── minting + idempotency ───────────────────────────────────────────


def test_acquire_mints_tagged_stub(handler: PaperHandler, store: Store) -> None:
    r = handler.acquire(identifier="doi:10.1/x", reason="cited a lot")
    assert "minted stub" in r.body
    rid = _ref_id(r.body)
    assert ("doi", "10.1/x") in _identifiers(store, rid)
    assert "DREAM:acquire" in _tags(store, rid)
    # it's a stub: a live paper ref with no pdf
    assert store.get_ref(kind="paper", id=rid) is not None


def test_acquire_is_idempotent(handler: PaperHandler, store: Store) -> None:
    first = _ref_id(handler.acquire(identifier="doi:10.1/x").body)
    second = handler.acquire(identifier="doi:10.1/X")  # case-insensitive
    assert "already tracked" in second.body
    assert _ref_id(second.body) == first
    # A collapse hit returns the existing paper, not just "already tracked":
    # the slug handle + a get hint so the caller can read it directly.
    second_ref = store.get_ref(kind="paper", id=first)
    assert second_ref is not None and second_ref.slug is not None
    assert record_handle(store, second_ref.slug) in second.body
    assert "get(id=" in second.body


def test_acquire_reports_ref_id(handler: PaperHandler) -> None:
    """gr690594: in-process callers read ``Response.ref_id`` (mint and
    collapse hit alike) instead of scraping ``id=paN`` out of the ack."""
    first = handler.acquire(identifier="doi:10.1/x")
    assert first.ref_id == _ref_id(first.body)
    second = handler.acquire(identifier="doi:10.1/X")
    assert second.ref_id == first.ref_id


def test_acquire_wiley_twin_doi_collapses(handler: PaperHandler, store: Store) -> None:
    # The twin collapse must also hold when the anie twin is an
    # already-HELD paper (pdf present): the ange acquire collapses onto
    # it, registers the second DOI there, and must not pin it (nothing
    # left to fetch).
    held_ref = store.insert_ref(kind="paper", slug="angew1966", title="Held Angew")
    held = held_ref.id
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('doi', '10.1002/anie.5660280', %s, 'test')",
            (held,),
        )
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 1, 100, '/tmp/stub')",
            ("c" * 64, "c" * 64),
        )
        conn.execute(
            "UPDATE refs SET pdf_sha256 = %s, prio = NULL, "
            "meta = meta - 'prio_by' WHERE ref_id = %s",
            ("c" * 64, held),
        )
        conn.commit()
    r = handler.acquire(identifier="doi:10.1002/ange.5660280")
    assert _ref_id(r.body) == held
    assert "already tracked" in r.body
    assert f"twin DOI of pa{held}, nothing minted" in r.body
    assert "pinned" not in r.body
    assert (
        store.find_ref_by_identifier("doi", "10.1002/anie.5660280", kind="paper")
        == held
    )
    assert (
        store.find_ref_by_identifier("doi", "10.1002/ange.5660280", kind="paper")
        == held
    )
    prio, prio_by, _ = _fetch_pin(store, held)
    assert (prio, prio_by) == (None, None)


def test_acquire_pmcid_mints_stub(handler: PaperHandler, store: Store) -> None:
    """gr663681: ``pmcid:`` identifiers queue a stub — the gripe's
    PMC-only reference could not be put at all."""
    r = handler.acquire(identifier="pmcid:PMC11089570")
    assert "minted stub" in r.body
    rid = _ref_id(r.body)
    assert ("pmcid", "pmc11089570") in _identifiers(store, rid)
    assert "DREAM:acquire" in _tags(store, rid)


def test_acquire_pmcid_collapses_on_repeat(handler: PaperHandler, store: Store) -> None:
    """Both spellings of one PMCID land on the same stub."""
    first = _ref_id(handler.acquire(identifier="pmcid:PMC11089570").body)
    second = handler.acquire(identifier="pmcid:11089570")
    assert "already tracked" in second.body
    assert _ref_id(second.body) == first


def test_acquire_registers_pubmed_pmc_aliases_from_s2(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An S2-resolved acquire also registers the externalIds cluster's
    PubMed / PMC aliases, so a later ``pmcid:``/``PMC…``/``pubmed:``
    lookup collapses onto the same stub instead of minting a twin."""
    monkeypatch.setattr(
        paper_mod,
        "_lookup_acquire_metadata",
        lambda *_: {
            "title": "Resolved Paper",
            "year": 2024,
            "doi": "10.9/withpmc",
            "external_ids": {
                "DOI": "10.9/withpmc",
                "PubMed": "99887766",
                "PubMedCentral": "PMC11089570",
            },
        },
    )
    r = handler.acquire(identifier="doi:10.9/withpmc")
    rid = _ref_id(r.body)
    ids = _identifiers(store, rid)
    assert ("doi", "10.9/withpmc") in ids
    assert ("pubmed", "99887766") in ids
    assert ("pmcid", "pmc11089570") in ids
    # And the collapse works both directions: a pmcid re-acquire hits.
    again = handler.acquire(identifier="pmcid:PMC11089570")
    assert "already tracked" in again.body
    assert _ref_id(again.body) == rid


# ── S2-attributed DOI corroboration (gr710828) ───────────────────────
#
# S2's cluster for arXiv:1705.07874 (s2:442e10a3… — the real SHAP
# record) mis-merged the Korean journal DOI 10.9728/dcs.2024.25.7.1847
# into its externalIds. Re-asking S2 cannot detect it — DOI:<that doi>
# resolves back onto the same poisoned cluster — so an S2-ATTRIBUTED
# DOI (one the caller did not ask by) must be corroborated at the DOI
# registries before the stub carries it.

_SHAP_S2_META = {
    "title": "A Unified Approach to Interpreting Model Predictions",
    "year": 2017,
    "authors": [{"name": "Scott M. Lundberg"}, {"name": "Su-In Lee"}],
    "doi": "10.9728/dcs.2024.25.7.1847",
    "arxiv_id": "1705.07874",
    "s2_id": "442e10a3c6640ded9408622005e3c2a8906ce4c2",
    "external_ids": {
        "ArXiv": "1705.07874",
        "DOI": "10.9728/dcs.2024.25.7.1847",
        "DBLP": "conf/nips/LundbergL17",
    },
}

_KOREAN_REGISTRY_META = {
    "title": "Exploration of Digital Divide Factors Affecting Life "
    "Satisfaction in Older People: Using Tree-Based Ensemble "
    "Machine Learning and SHAP",
    "authors": [{"given": "Jeong-Seon", "family": "Lee"}],
    "year": 2024,
    "source": "crossref",
}


def test_acquire_arxiv_drops_registry_uncorroborated_s2_doi(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr710828: the S2 record carries a foreign DOI and the registry's
    title for it shares no distinctive token with the record's — the
    stub mints arxiv-only."""
    monkeypatch.setattr(
        paper_mod, "_lookup_acquire_metadata", lambda *_: dict(_SHAP_S2_META)
    )
    monkeypatch.setattr(
        paper_mod, "_lookup_doi_registry", lambda *_: dict(_KOREAN_REGISTRY_META)
    )
    r = handler.acquire(
        identifier="arxiv:1705.07874",
        title="A Unified Approach to Interpreting Model Predictions",
    )
    assert "minted stub" in r.body
    rid = _ref_id(r.body)
    ids = _identifiers(store, rid)
    assert ("arxiv", "1705.07874") in ids
    assert ("s2", "442e10a3c6640ded9408622005e3c2a8906ce4c2") in ids
    assert not any(kind == "doi" for kind, _ in ids)


def test_acquire_arxiv_keeps_registry_corroborated_s2_doi(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The good case the gripe names: an S2-attributed DOI whose registry
    record names the same work (shared title tokens, agreeing first
    author) still lands on the stub."""
    monkeypatch.setattr(
        paper_mod,
        "_lookup_acquire_metadata",
        lambda *_: {
            **_SHAP_S2_META,
            "doi": "10.5555/shap-journal",
            "external_ids": {
                "ArXiv": "1705.07874",
                "DOI": "10.5555/shap-journal",
                "DBLP": "conf/nips/LundbergL17",
            },
        },
    )
    monkeypatch.setattr(
        paper_mod,
        "_lookup_doi_registry",
        lambda *_: {
            "title": "A unified approach to interpreting model predictions",
            "authors": [{"given": "Scott", "family": "Lundberg"}],
            "year": 2017,
            "source": "crossref",
        },
    )
    r = handler.acquire(identifier="arxiv:1705.07874")
    rid = _ref_id(r.body)
    ids = _identifiers(store, rid)
    assert ("doi", "10.5555/shap-journal") in ids
    assert ("arxiv", "1705.07874") in ids


def test_acquire_arxiv_keeps_s2_doi_when_registry_cannot_answer(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry fault is not evidence of a mis-merge — abstain and
    keep the DOI (the gr483930 miss-vs-fault split)."""
    monkeypatch.setattr(
        paper_mod, "_lookup_acquire_metadata", lambda *_: dict(_SHAP_S2_META)
    )

    def _down(*_a: object) -> None:
        raise RuntimeError("crossref unreachable")

    monkeypatch.setattr(paper_mod, "_lookup_doi_registry", _down)
    r = handler.acquire(identifier="arxiv:1705.07874")
    rid = _ref_id(r.body)
    assert ("doi", "10.9728/dcs.2024.25.7.1847") in _identifiers(store, rid)


def test_acquire_doi_identifier_never_scrubbed(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The requested DOI is trusted by definition — the scrub and the
    poisoned-cluster check must not consult the registry to corroborate
    what the caller asked for. Asserted on the call count, not a raising
    mock: the mismatch check swallows resolver exceptions, so a raise
    inside the mock could not prove the registry went unconsulted. The S2
    response here carries no year, so the registry year cross-check
    (gr722127) is outside this test's scope; see
    ``test_acquire_doi_year_crosscheck_consults_registry_once``."""
    monkeypatch.setattr(
        paper_mod,
        "_lookup_acquire_metadata",
        lambda *_: {
            "title": "Whatever",
            "doi": "10.9728/dcs.2024.25.7.1847",
            "external_ids": {"DOI": "10.9728/dcs.2024.25.7.1847"},
        },
    )
    consulted: list[str] = []
    monkeypatch.setattr(paper_mod, "_lookup_doi_registry", consulted.append)
    r = handler.acquire(identifier="doi:10.9728/dcs.2024.25.7.1847")
    rid = _ref_id(r.body)
    assert ("doi", "10.9728/dcs.2024.25.7.1847") in _identifiers(store, rid)
    assert consulted == []


def test_acquire_doi_year_crosscheck_consults_registry_once(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Companion to ``test_acquire_doi_identifier_never_scrubbed``: when S2
    carries a year, the registry year cross-check (gr722127) is the one
    registry call a requested DOI makes. The DOI identifier is still kept."""
    monkeypatch.setattr(
        paper_mod,
        "_lookup_acquire_metadata",
        lambda *_: {
            "title": "Whatever",
            "year": 2024,
            "doi": "10.9728/dcs.2024.25.7.1847",
            "external_ids": {"DOI": "10.9728/dcs.2024.25.7.1847"},
        },
    )
    consulted: list[str] = []
    monkeypatch.setattr(paper_mod, "_lookup_doi_registry", consulted.append)
    r = handler.acquire(identifier="doi:10.9728/dcs.2024.25.7.1847")
    rid = _ref_id(r.body)
    assert ("doi", "10.9728/dcs.2024.25.7.1847") in _identifiers(store, rid)
    assert consulted == ["10.9728/dcs.2024.25.7.1847"]


def _fetch_pin(store: Store, ref_id: int) -> tuple[int | None, str | None, str | None]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT prio, meta->>'prio_by', meta #>> '{oa_requeued,at}' "
            "FROM refs WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
    assert row is not None
    return row[0], row[1], row[2]


def test_acquire_pins_stub_to_fetch_front(handler: PaperHandler, store: Store) -> None:
    # Explicit acquire = "fetch NOW": the fresh stub is pinned prio=1 /
    # prio_by='acquire' (stub_rank's never-clobber guard leaves it alone)
    # and stamped oa_requeued so it claims on fetch_oa's next pass instead
    # of waiting unranked behind the whole prio-ranked backlog.
    r = handler.acquire(identifier="doi:10.1/pin")
    assert "pinned to front of fetch queue" in r.body
    prio, prio_by, requeued_at = _fetch_pin(store, _ref_id(r.body))
    assert (prio, prio_by) == (1, "acquire")
    assert requeued_at is not None


def _age_pin(store: Store, rid: int, *, stamp_age: str) -> None:
    """Simulate stub_rank-era drift plus an ``oa_requeued`` stamp this old."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET prio = 7, meta = meta || jsonb_build_object("
            "'prio_by', 'stub_rank', "
            "'oa_requeued', jsonb_build_object('at', now() - %s::interval)) "
            "WHERE ref_id = %s",
            (stamp_age, rid),
        )
        conn.commit()


def _stamp_is_fresh(store: Store, rid: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT (meta #>> '{oa_requeued,at}')::timestamptz "
            "> now() - interval '1 minute' FROM refs WHERE ref_id = %s",
            (rid,),
        ).fetchone()
    assert row is not None
    return bool(row[0])


def _fetcher_event(store: Store, rid: int, *, age: str, event: str) -> None:
    """One ``fetcher:%`` leg event ``age`` ago (what a cascade pass writes)."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_events (ref_id, source, event, payload, ts) "
            "VALUES (%s, 'fetcher:unpaywall', %s, '{}', now() - %s::interval)",
            (rid, event, age),
        )
        conn.commit()


def test_reacquire_keeps_unconsumed_stamp(handler: PaperHandler, store: Store) -> None:
    # gr453859: a draft that re-acquires its cites on every save must not
    # re-arm the backoff bypass each time. A stamp the fetcher has not yet
    # consumed (no fetcher event after it) is still armed, however old, so
    # the re-acquire re-pins prio but leaves the stamp alone.
    rid = _ref_id(handler.acquire(identifier="doi:10.1/pin2").body)
    _age_pin(store, rid, stamp_age="9 days")
    second = handler.acquire(identifier="doi:10.1/pin2")
    assert _ref_id(second.body) == rid
    assert "pinned to front of fetch queue" in second.body
    prio, prio_by, requeued_at = _fetch_pin(store, rid)
    assert (prio, prio_by) == (1, "acquire")
    assert requeued_at is not None
    assert _stamp_is_fresh(store, rid) is False


def test_reacquire_restamps_for_explicit_ask_inside_window(
    handler: PaperHandler, store: Store
) -> None:
    # gr510430: an explicit re-request (put(kind='paper')/acquire) always
    # re-stamps oa_requeued — ACQUIRE_RESTAMP_MIN_HOURS guards
    # auto-discovery churn only. The fresh stamp is newer than the last
    # fetcher event, so claim_stubs_to_fetch's backoff bypass picks the
    # stub up on the very next pass.
    rid = _ref_id(handler.acquire(identifier="doi:10.1/pin2b").body)
    _age_pin(store, rid, stamp_age="3 days")
    _fetcher_event(store, rid, age="2 hours", event="no_oa_version")
    second = handler.acquire(identifier="doi:10.1/pin2b")
    assert _ref_id(second.body) == rid
    assert "pinned to front of fetch queue" in second.body
    prio, prio_by, _ = _fetch_pin(store, rid)
    assert (prio, prio_by) == (1, "acquire")
    assert _stamp_is_fresh(store, rid) is True
    from precis.workers.fetch_oa import claim_stubs_to_fetch

    with store.pool.connection() as conn:
        claimed = claim_stubs_to_fetch(conn, limit=10)
        conn.commit()
    assert [s.ref_id for s in claimed] == [rid]


def test_reacquire_by_arxiv_gives_an_s2_only_stub_its_arxiv_id(
    handler: PaperHandler,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # gr711568: a stub minted from a bare S2 id holds no arXiv id, so the
    # cascade skips its arXiv leg and only S2 answers (no_oa_version, for
    # months). Re-asking by arXiv id collapses onto the stub through the
    # S2 id; the arXiv id must land on the row, or the next pass still
    # never reaches arXiv.
    from precis.workers import fetch_oa

    s2_id = "0123456789abcdef0123456789abcdef01234567"
    rid, _ = store.upsert_stub_paper(
        identifiers=[("s2", s2_id)], title="t", set_by="chase"
    )
    _fetcher_event(store, rid, age="2 hours", event="no_oa_version")
    monkeypatch.setattr(
        paper_mod,
        "_lookup_acquire_metadata",
        lambda *_: {"title": "t", "arxiv_id": "2401.00077", "s2_id": s2_id},
    )
    r = handler.acquire(identifier="arxiv:2401.00077")
    assert "already tracked" in r.body
    assert _ref_id(r.body) == rid
    assert ("arxiv", "2401.00077") in _identifiers(store, rid)

    with store.pool.connection() as conn:
        claimed = fetch_oa.claim_stubs_to_fetch(conn, limit=10)
        conn.commit()
    assert [(s.ref_id, s.arxiv) for s in claimed] == [(rid, "2401.00077")]
    monkeypatch.setattr(fetch_oa, "_download_pdf", lambda *_a, **_k: 1)
    outcome = fetch_oa._try_arxiv(claimed[0], inbox_dir=tmp_path)
    assert outcome is not None
    assert outcome.event == "fetch_ok"
    assert outcome.payload["url"] == "https://arxiv.org/pdf/2401.00077.pdf"


def test_datacite_doi_acquire_converges_on_arxiv_stub(
    handler: PaperHandler, store: Store
) -> None:
    # gr711568 second symptom: put(kind='paper', doi='10.48550/arXiv.X')
    # forked a duplicate stub next to the arXiv-row stub of the same
    # preprint (pa711567 beside pa159949). The cross-scheme probe must
    # collapse the ask onto the held stub and leave the DOI spelling on
    # its row.
    rid, _ = store.upsert_stub_paper(
        identifiers=[("arxiv", "2401.00077")], title="t", set_by="chase"
    )
    r = handler.acquire(identifier="doi:10.48550/arXiv.2401.00077")
    assert "already tracked" in r.body
    assert _ref_id(r.body) == rid
    ids = _identifiers(store, rid)
    assert ("arxiv", "2401.00077") in ids
    assert ("doi", "10.48550/arxiv.2401.00077") in ids


def test_reacquire_guarded_path_keeps_restamp_guard(
    handler: PaperHandler, store: Store
) -> None:
    # The auto-discovery spelling (quest lit-fetch → acquire(explicit=False)
    # → the pin's default restamp_min_hours) keeps the 24 h guard: a stub
    # whose newest attempt is fresh keeps its consumed stamp instead of
    # re-arming the bypass on every periodic re-request (gr453859). The
    # SAME stub re-requested explicitly still re-stamps (gr510430).
    rid = _ref_id(handler.acquire(identifier="doi:10.1/pin2e").body)
    _age_pin(store, rid, stamp_age="3 days")
    _fetcher_event(store, rid, age="2 hours", event="no_oa_version")
    handler.acquire(identifier="doi:10.1/pin2e", explicit=False)
    prio, prio_by, _ = _fetch_pin(store, rid)
    assert (prio, prio_by) == (1, "acquire")
    assert _stamp_is_fresh(store, rid) is False
    handler.acquire(identifier="doi:10.1/pin2e")
    assert _stamp_is_fresh(store, rid) is True


def test_reacquire_restamps_a_consumed_stamp_after_the_window(
    handler: PaperHandler, store: Store
) -> None:
    # Consumed stamp, newest attempt older than the window: this is the
    # one case an explicit re-acquire still lifts a backed-off stub out
    # of its retry window for one more pass.
    rid = _ref_id(handler.acquire(identifier="doi:10.1/pin2c").body)
    _age_pin(store, rid, stamp_age="3 days")
    _fetcher_event(
        store, rid, age=f"{ACQUIRE_RESTAMP_MIN_HOURS + 1} hours", event="no_oa_version"
    )
    handler.acquire(identifier="doi:10.1/pin2c")
    prio, prio_by, _ = _fetch_pin(store, rid)
    assert (prio, prio_by) == (1, "acquire")
    assert _stamp_is_fresh(store, rid) is True


def test_pin_without_stamp_skips_when_just_tried(store: Store) -> None:
    # No stamp at all but a fetcher event minutes old (a chase-minted stub
    # the worker already tried): pin prio, write no stamp.
    rid, _ = store.upsert_stub_paper(
        identifiers=[("doi", "10.1/pin2d")], title="t", set_by="chase"
    )
    _fetcher_event(store, rid, age="10 minutes", event="no_oa_version")
    assert store.pin_stub_for_fetch(rid) is True
    prio, prio_by, requeued_at = _fetch_pin(store, rid)
    assert (prio, prio_by) == (1, "acquire")
    assert requeued_at is None


def test_pin_stub_for_fetch_without_conn(handler: PaperHandler, store: Store) -> None:
    # The conn-less spelling (operator/script use) opens its own connection;
    # the handler path always passes the tx conn, so cover this leg directly.
    rid = _ref_id(handler.acquire(identifier="doi:10.1/pin3").body)
    assert store.pin_stub_for_fetch(rid) is True
    assert store.pin_stub_for_fetch(999999999) is False  # no such ref → no-op
    prio, prio_by, requeued_at = _fetch_pin(store, rid)
    assert (prio, prio_by) == (1, "acquire")
    assert requeued_at is not None


def test_acquire_does_not_pin_held_paper(handler: PaperHandler, store: Store) -> None:
    # A collapse hit onto an already-held paper (pdf present) must not get
    # the synthetic prio=1 — there is nothing left to fetch.
    rid = _ref_id(handler.acquire(identifier="doi:10.1/held").body)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 1, 100, '/tmp/stub')",
            ("b" * 64, "b" * 64),
        )
        conn.execute(
            "UPDATE refs SET pdf_sha256 = %s, prio = NULL, "
            "meta = meta - 'prio_by' WHERE ref_id = %s",
            ("b" * 64, rid),
        )
        conn.commit()
    second = handler.acquire(identifier="doi:10.1/held")
    assert _ref_id(second.body) == rid
    assert "pinned" not in second.body
    prio, prio_by, _ = _fetch_pin(store, rid)
    assert (prio, prio_by) == (None, None)


def test_acquire_title_only_mints_backlog_stub(
    handler: PaperHandler, store: Store
) -> None:
    r = handler.acquire(title="Some Uncited Paper We Want")
    assert "minted stub" in r.body
    rid = _ref_id(r.body)
    assert "DREAM:acquire" in _tags(store, rid)
    # no external id → fetch_oa can't auto-grab; it's a pure backlog stub
    minted_ref = store.get_ref(kind="paper", id=rid)
    assert minted_ref is not None
    assert _identifiers(store, rid) == {("cite_key", minted_ref.slug)}


def test_stub_mint_keys_on_authors_then_significant_title_word(
    store: Store,
) -> None:
    """gr523730: the stub mint keyed on the title's first word —
    ``the<yy>`` keys that ``is_junk_cite_key`` flags as born-junk. A
    known byline keys on the surname instead; with no authors the
    first *significant* title word seeds the key."""
    rid, created = store.upsert_stub_paper(
        identifiers=[("doi", "10.1/bindome-byline")],
        title="The Human Bindome",
        year=2026,
        authors=[{"name": "Adam Wenckstern"}],
        set_by="dream",
    )
    assert created is True
    minted = store.get_ref(kind="paper", id=rid)
    assert minted is not None
    assert minted.slug == "wenckstern26"

    rid2, created2 = store.upsert_stub_paper(
        identifiers=[("doi", "10.1/bindome-anon")],
        title="The Human Bindome",
        year=2026,
        set_by="dream",
    )
    assert created2 is True
    minted2 = store.get_ref(kind="paper", id=rid2)
    assert minted2 is not None
    assert minted2.slug == "human26"


def test_put_book_stub_stores_supplied_authors(
    handler: PaperHandler, store: Store
) -> None:
    response = handler.put(
        title="A Book On Graph Algorithms",
        year=2024,
        tags=["is-book"],
        args={"authors": ["Doe, Jane", {"given": "John", "family": "Smith"}]},
    )
    rid = _ref_id(response.body)
    ref = store.get_ref(kind="paper", id=rid)
    assert ref is not None
    assert ref.slug == "doe24"
    assert ref.year == 2024
    assert "is-book" in _tags(store, rid)
    assert ref.authors == [
        {"given": "Jane", "family": "Doe"},
        {"given": "John", "family": "Smith"},
    ]
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT family, source FROM paper_authors "
            "WHERE ref_id = %s ORDER BY position",
            (rid,),
        ).fetchall()
    assert rows == [("Doe", "human"), ("Smith", "human")]
    assert "stored 2 supplied authors" in response.body


def test_put_s2_verified_stub_writes_byline(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr689211: S2 authors seed both the cite key and stored byline."""
    monkeypatch.setattr(
        paper_mod,
        "_lookup_acquire_metadata",
        lambda *_: {
            "title": "Resolved Paper",
            "year": 2018,
            "authors": [{"name": "Jane Clark"}, {"name": "John Smith"}],
            "journal": "Test Journal",
        },
    )
    response = handler.put(doi="10.1/s2-byline")
    rid = _ref_id(response.body)
    ref = store.get_ref(kind="paper", id=rid)
    assert ref is not None
    assert ref.slug == "clark18"
    assert ref.authors is not None
    assert [a["name"] for a in ref.authors] == ["Jane Clark", "John Smith"]
    assert _meta(store, rid)["journal"] == "Test Journal"
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT source FROM paper_authors WHERE ref_id = %s ORDER BY position",
            (rid,),
        ).fetchall()
    assert rows == [("s2",), ("s2",)]


def test_acquire_doi_year_crosscheck_prefers_registry(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr722127: S2's year for 10.1007/BF02650179 is 1999 (a reprint
    date); the Crossref record says 1982. The registration agency is
    authoritative for what the DOI names, so the stub year and the cite
    key must come from the registry, not S2."""
    monkeypatch.setattr(
        paper_mod,
        "_lookup_acquire_metadata",
        lambda *_: {
            "title": "Simulating physics with computers",
            "year": 1999,
            "authors": [{"given": "Richard", "family": "Feynman"}],
            "journal": "International Journal of Theoretical Physics",
        },
    )
    monkeypatch.setattr(
        paper_mod,
        "_lookup_doi_registry",
        lambda *_: {
            "title": "Simulating physics with computers",
            "year": 1982,
            "doi": "10.1007/BF02650179",
            "journal": "International Journal of Theoretical Physics",
            "source": "crossref",
        },
    )
    response = handler.put(doi="10.1007/BF02650179")
    rid = _ref_id(response.body)
    ref = store.get_ref(kind="paper", id=rid)
    assert ref is not None
    assert ref.year == 1982
    assert ref.slug == "feynman82"
    assert _meta(store, rid).get("acquire_year_registry") == "crossref"


def test_acquire_doi_year_crosscheck_title_mismatch_keeps_s2(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry's year only wins when the registry vouches for the
    same work — a title that doesn't match S2's keeps S2's year."""
    monkeypatch.setattr(
        paper_mod,
        "_lookup_acquire_metadata",
        lambda *_: {
            "title": "Simulating physics with computers",
            "year": 1999,
            "authors": [{"given": "Richard", "family": "Feynman"}],
        },
    )
    monkeypatch.setattr(
        paper_mod,
        "_lookup_doi_registry",
        lambda *_: {
            "title": "An entirely different proceedings volume",
            "year": 1982,
            "source": "crossref",
        },
    )
    response = handler.put(doi="10.1007/BF02650179")
    rid = _ref_id(response.body)
    ref = store.get_ref(kind="paper", id=rid)
    assert ref is not None
    assert ref.year == 1999
    assert ref.slug == "feynman99"
    assert "acquire_year_registry" not in _meta(store, rid)


def test_acquire_doi_year_crosscheck_registry_fault_keeps_s2(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry fault on the cross-check leg is not evidence against
    the DOI — S2 verified it and S2's year stands."""

    def _down(*_a: object) -> None:
        raise RuntimeError("crossref offline in test")

    monkeypatch.setattr(
        paper_mod,
        "_lookup_acquire_metadata",
        lambda *_: {
            "title": "Simulating physics with computers",
            "year": 1999,
            "authors": [{"given": "Richard", "family": "Feynman"}],
        },
    )
    monkeypatch.setattr(paper_mod, "_lookup_doi_registry", _down)
    response = handler.put(doi="10.1007/BF02650179")
    rid = _ref_id(response.body)
    ref = store.get_ref(kind="paper", id=rid)
    assert ref is not None
    assert ref.year == 1999
    assert "acquire:unverified" not in _tags(store, rid)
    assert "acquire_year_registry" not in _meta(store, rid)


def test_put_supplied_authors_override_registry_on_fresh_stub(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", lambda *_: None)
    monkeypatch.setattr(
        paper_mod,
        "_lookup_doi_registry",
        lambda *_: {
            "title": "A Registered Book",
            "authors": [{"given": "Other", "family": "Writer"}],
            "year": 2024,
            "source": "crossref",
        },
    )
    response = handler.put(doi="10.1/supplied-byline", authors=["Doe, Jane"])
    ref = store.get_ref(kind="paper", id=_ref_id(response.body))
    assert ref is not None
    assert ref.slug == "doe24"
    assert ref.authors == [{"given": "Jane", "family": "Doe"}]


def test_put_authors_preserves_existing_byline_on_identifier_hit(
    handler: PaperHandler, store: Store
) -> None:
    first = handler.put(doi="10.1/existing-byline", authors=["Doe, Jane"])
    rid = _ref_id(first.body)
    repeated = handler.put(doi="10.1/existing-byline", authors=["Smith, John"])
    assert _ref_id(repeated.body) == rid
    ref = store.get_ref(kind="paper", id=rid)
    assert ref is not None
    assert ref.authors == [{"given": "Jane", "family": "Doe"}]
    assert "supplied authors not applied" in repeated.body
    assert f"edit(kind='paper', id='pa{rid}', authors=" in repeated.body


def test_acquire_links_from_context(handler: PaperHandler, store: Store) -> None:
    ctx = store.insert_ref(kind="memory", slug=None, title="where it came up")
    r = handler.acquire(
        identifier="arxiv:2401.00001", context_ref_id=ctx.id, reason="rev"
    )
    rid = _ref_id(r.body)
    out = store.links_for(ctx.id, direction="out", relation="related-to")
    assert any(link.dst_ref_id == rid for link in out)
    assert f"ref {ctx.id}" in r.body


def test_acquire_links_from_handle_context(handler: PaperHandler, store: Store) -> None:
    # gr623795: the universal handle is the address every read emits —
    # passing 'pa<id>'/'me<id>' for context_ref_id must not have to
    # strip the prefix. Kind-agnostic: the provenance ref may be a
    # paper or a memory.
    ctx_paper = store.insert_ref(kind="paper", slug="ctxsrc", title="ctx src")
    ctx_mem = store.insert_ref(kind="memory", slug=None, title="ctx mem")
    r = handler.acquire(
        identifier="arxiv:2401.00002",
        context_ref_id=f"pa{ctx_paper.id}",
        reason="rev",
    )
    rid = _ref_id(r.body)
    r2 = handler.acquire(
        identifier="arxiv:2401.00003",
        context_ref_id=f"me{ctx_mem.id}",
        reason="rev",
    )
    rid2 = _ref_id(r2.body)
    out = store.links_for(ctx_paper.id, direction="out", relation="related-to")
    assert any(link.dst_ref_id == rid for link in out)
    out2 = store.links_for(ctx_mem.id, direction="out", relation="related-to")
    assert any(link.dst_ref_id == rid2 for link in out2)
    assert f"ref {ctx_paper.id}" in r.body
    assert f"ref {ctx_mem.id}" in r2.body


def test_acquire_does_not_retag_existing_paper(
    handler: PaperHandler, store: Store
) -> None:
    # A paper the corpus already holds, registered under a DOI.
    existing = store.insert_ref(kind="paper", slug="held2020", title="Held Paper")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('doi', '10.9/held', %s, 'test')",
            (existing.id,),
        )
        conn.commit()
    r = handler.acquire(identifier="doi:10.9/held")
    assert "already tracked" in r.body
    assert _ref_id(r.body) == existing.id
    # the response returns the existing paper: its handle + a get hint
    assert record_handle(store, "held2020") in r.body
    assert "get(id=" in r.body
    # never slap DREAM:acquire onto an already-held paper
    assert "DREAM:acquire" not in _tags(store, existing.id)


# ── verify=True (default): reject hallucinated identifiers ─────────


def test_acquire_rejects_unresolved_identifier_by_default(
    handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify=True (default) + S2 returns None + no title → BadInput."""
    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", lambda *_: None)
    with pytest.raises(BadInput, match="did not resolve"):
        handler.acquire(identifier="doi:10.fake/hallucination")


def test_acquire_verify_false_skips_validation(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify=False mints even when S2 returns nothing (real-but-unindexed)."""
    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", lambda *_: None)
    r = handler.acquire(identifier="doi:10.brandnew/preprint", verify=False)
    assert "minted stub" in r.body
    rid = _ref_id(r.body)
    assert ("doi", "10.brandnew/preprint") in _identifiers(store, rid)


def test_acquire_title_hint_with_unresolved_id_marks_unverified(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unresolved identifier + title=hint mints with acquire:unverified tag."""
    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", lambda *_: None)
    r = handler.acquire(identifier="doi:10.niche/venue", title="My niche preprint")
    assert "minted stub" in r.body
    rid = _ref_id(r.body)
    assert "acquire:unverified" in _tags(store, rid)


# ── resolver outage: mint unverified, don't reject (gr483930) ───────


def _meta(store: Store, ref_id: int) -> dict:
    import json

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])


def test_acquire_resolver_fault_mints_unverified(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr483930: an S2 outage is NOT 'did not resolve'. When the lookup
    itself faults, acquire mints the stub marked unverified (tag + meta)
    instead of rejecting a real identifier as hallucinated."""
    from precis.ingest.semantic_scholar import S2ResolverError

    def _down(*_a: object) -> None:
        raise S2ResolverError("Semantic Scholar lookup failed: 429")

    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", _down)
    r = handler.acquire(identifier="doi:10.1038/nature10352")
    assert "minted stub" in r.body
    assert "not verified yet" in r.body and "S2ResolverError" in r.body
    rid = _ref_id(r.body)
    assert "acquire:unverified" in _tags(store, rid)
    meta = _meta(store, rid)
    assert meta.get("acquire_unverified") is True
    assert "Semantic Scholar" in str(meta.get("acquire_unverified_reason", ""))
    assert ("doi", "10.1038/nature10352") in _identifiers(store, rid)


def test_acquire_resolver_fault_without_s2_lib_mints_unverified(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken/absent client lib (ModuleNotFoundError) is also a
    resolver fault — mint unverified rather than reject or crash."""

    def _nobib(*_a: object) -> None:
        raise ModuleNotFoundError("No module named 'semanticscholar'")

    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", _nobib)
    r = handler.acquire(identifier="arxiv:2401.00001")
    assert "minted stub" in r.body
    rid = _ref_id(r.body)
    assert "acquire:unverified" in _tags(store, rid)
    assert _meta(store, rid).get("acquire_unverified") is True


def test_acquire_not_found_still_rejects_under_verify(
    handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard is unchanged for a genuine not-found: resolver
    ANSWERED 'no such paper' → BadInput. (The fault case above mints
    unverified; the outage classification is the whole point.)"""
    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", lambda *_: None)
    with pytest.raises(BadInput, match="did not resolve"):
        handler.acquire(identifier="doi:10.fake/hallucination")


def test_acquire_s2_fault_doi_verified_by_registry(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr489237: a gateway with no S2 client (pa489234's reason was
    "No module named 'semanticscholar'") still verifies a DOI through
    Crossref, so the stub is not minted unverified."""

    def _nobib(*_a: object) -> None:
        raise ModuleNotFoundError("No module named 'semanticscholar'")

    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", _nobib)
    monkeypatch.setattr(
        paper_mod,
        "_lookup_doi_registry",
        lambda *_: {
            "title": "Chitosan Films",
            "year": 2025,
            "doi": "10.1016/j.carbpol.2025.124361",
            "source": "crossref",
        },
    )
    r = handler.acquire(identifier="doi:10.1016/j.carbpol.2025.124361")
    assert "via Crossref — S2 unavailable" in r.body
    assert "not verified" not in r.body
    rid = _ref_id(r.body)
    assert "acquire:unverified" not in _tags(store, rid)
    meta = _meta(store, rid)
    assert "acquire_unverified" not in meta
    assert meta.get("acquire_registry") == "crossref"


def test_acquire_registry_carries_record_fields(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr514384: a DataCite software DOI registers already typed — the
    stub's meta carries ``record_type`` / ``version`` / ``publisher`` /
    typed ``entry_type``, so it is citable whole with no second put."""

    def _nobib(*_a: object) -> None:
        raise ModuleNotFoundError("No module named 'semanticscholar'")

    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", _nobib)
    monkeypatch.setattr(
        paper_mod,
        "_lookup_doi_registry",
        lambda *_: {
            "title": "precis-mcp",
            "year": 2026,
            "doi": "10.5281/zenodo.22863181",
            "source": "datacite",
            "record_type": "software",
            "version": "1.2.3",
            "publisher": "Zenodo",
            "entry_type": "software",
        },
    )
    r = handler.acquire(identifier="doi:10.5281/zenodo.22863181")
    rid = _ref_id(r.body)
    meta = _meta(store, rid)
    assert meta.get("record_type") == "software"
    assert meta.get("version") == "1.2.3"
    assert meta.get("publisher") == "Zenodo"
    assert meta.get("entry_type") == "software"


def test_clear_acquire_unverified_flips_the_stub(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr489237: once the paper is confirmed the flag, its reason and the
    tag go, and the confirming leg is stamped; a second call is a no-op."""
    from precis.store._refs_ops import clear_acquire_unverified

    def _nobib(*_a: object) -> None:
        raise ModuleNotFoundError("No module named 'semanticscholar'")

    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", _nobib)
    rid = _ref_id(handler.acquire(identifier="arxiv:2401.00002").body)
    assert "acquire:unverified" in _tags(store, rid)
    with store.pool.connection() as conn:
        assert clear_acquire_unverified(conn, rid, by="fetch") is True
        assert clear_acquire_unverified(conn, rid, by="fetch") is False
    assert "acquire:unverified" not in _tags(store, rid)
    meta = _meta(store, rid)
    assert "acquire_unverified" not in meta
    assert "acquire_unverified_reason" not in meta
    assert meta.get("acquire_verified_by") == "fetch"
    assert meta.get("acquire_verified_at")


def test_stub_rank_enrich_verifies_an_unverified_stub(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr489237: the S2 enrich pass resolving an acquire stub minted
    unverified flips it to verified (pa489234's path)."""
    from precis.workers import stub_rank

    def _nobib(*_a: object) -> None:
        raise ModuleNotFoundError("No module named 'semanticscholar'")

    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", _nobib)
    rid = _ref_id(handler.acquire(identifier="arxiv:2401.00003").body)
    attempted, resolved = stub_rank._run_enrich(
        store,
        limit=50,
        api_key="",
        resolve_batch=lambda ids, _key: [
            {"fields": ["Chemistry"], "citation_count": 1} for _ in ids
        ],
    )
    assert attempted >= 1 and resolved >= 1
    assert "acquire:unverified" not in _tags(store, rid)
    assert _meta(store, rid).get("acquire_verified_by") == "s2_enrich"


# ── S2 miss → DOI registry fallback (gr486090) ─────────────────────


def test_acquire_s2_miss_consults_registries_before_rejecting(
    handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine S2 miss for a DOI asks Crossref/DataCite first; the
    'hallucinated or mistyped' refusal fires only when no registry
    knows the DOI either."""
    consulted: list[str] = []
    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", lambda *_: None)
    monkeypatch.setattr(
        paper_mod,
        "_lookup_doi_registry",
        consulted.append,
    )
    with pytest.raises(BadInput, match="hallucinated"):
        handler.acquire(identifier="doi:10.fake/nothing")
    assert consulted == ["10.fake/nothing"]


def test_acquire_s2_miss_crossref_hit_mints_with_metadata(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr486090: S2 miss + Crossref hit mints the stub with the
    registry's bibliographic metadata and names the registry."""
    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", lambda *_: None)
    monkeypatch.setattr(
        paper_mod,
        "_lookup_doi_registry",
        lambda *_: {
            "title": "A Science Advances Paper S2 Missed",
            "authors": [{"given": "Jane", "family": "Doe"}],
            "year": 2026,
            "doi": "10.1126/sciadv.adx3969",
            "journal": "Science Advances",
            "abstract": "We measured a thing.",
            "entry_type": "journal-article",
            "source": "crossref",
        },
    )
    r = handler.acquire(identifier="doi:10.1126/sciadv.adx3969")
    assert "minted stub" in r.body
    assert "via Crossref" in r.body
    rid = _ref_id(r.body)
    got = store.get_ref(kind="paper", id=rid)
    assert got is not None
    assert got.title == "A Science Advances Paper S2 Missed"
    assert got.year == 2026
    assert got.authors and "Doe" in str(got.authors)
    meta = _meta(store, rid)
    assert meta.get("acquire_registry") == "crossref"
    assert meta.get("journal") == "Science Advances"
    assert meta.get("abstract") == "We measured a thing."
    assert "acquire:unverified" not in _tags(store, rid)
    assert ("doi", "10.1126/sciadv.adx3969") in _identifiers(store, rid)


def test_acquire_s2_miss_datacite_hit_mints(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DataCite leg covers DOIs Crossref doesn't register
    (LIPIcs proceedings & co)."""
    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", lambda *_: None)
    monkeypatch.setattr(
        paper_mod,
        "_lookup_doi_registry",
        lambda *_: {
            "title": "A LIPIcs Proceedings Paper",
            "authors": [{"given": "Jane", "family": "Doe"}],
            "year": 2025,
            "doi": "10.4230/lipics.dna.32.9",
            "journal": "LIPIcs, Vol. 32",
            "source": "datacite",
        },
    )
    r = handler.acquire(identifier="doi:10.4230/LIPIcs.DNA.32.9")
    assert "minted stub" in r.body
    assert "via DataCite" in r.body
    rid = _ref_id(r.body)
    got = store.get_ref(kind="paper", id=rid)
    assert got is not None
    assert got.title == "A LIPIcs Proceedings Paper"
    assert got.year == 2025
    assert _meta(store, rid).get("acquire_registry") == "datacite"
    assert "acquire:unverified" not in _tags(store, rid)


def test_acquire_registry_fault_mints_unverified(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry leg that can't answer (outage/5xx) is not evidence the
    DOI is wrong — mint unverified, same rule as an S2 fault."""
    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", lambda *_: None)

    def _down(*_a: object) -> None:
        raise RuntimeError("crossref answered HTTP 503")

    monkeypatch.setattr(paper_mod, "_lookup_doi_registry", _down)
    r = handler.acquire(identifier="doi:10.real/maybe")
    assert "minted stub" in r.body
    assert "not verified yet" in r.body
    rid = _ref_id(r.body)
    assert "acquire:unverified" in _tags(store, rid)
    assert "crossref" in str(_meta(store, rid).get("acquire_unverified_reason", ""))


def test_acquire_registry_lookup_on_s2_hit_crosschecks_year(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr722127: an S2 hit for a DOI now still consults the registry —
    the year cross-check. A registry that knows nothing leaves S2's
    stub untouched."""
    consulted: list[str] = []
    monkeypatch.setattr(
        paper_mod,
        "_lookup_acquire_metadata",
        lambda *_: {"title": "Resolved Paper", "year": 2018},
    )

    def _registry(doi: str) -> None:
        consulted.append(doi)
        return None

    monkeypatch.setattr(
        paper_mod,
        "_lookup_doi_registry",
        _registry,
    )
    r = handler.acquire(identifier="doi:10.1/x")
    assert "minted stub" in r.body
    assert consulted == ["10.1/x"]
    ref = store.get_ref(kind="paper", id=_ref_id(r.body))
    assert ref is not None
    assert ref.year == 2018


def test_acquire_registry_lookup_skipped_for_non_doi(
    handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """arXiv/S2 ids have no registry to ask — unchanged reject path."""
    consulted: list[str] = []
    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", lambda *_: None)
    monkeypatch.setattr(
        paper_mod,
        "_lookup_doi_registry",
        consulted.append,
    )
    with pytest.raises(BadInput, match="did not resolve"):
        handler.acquire(identifier="arxiv:2401.99999")
    assert consulted == []


# ── enrichment ──────────────────────────────────────────────────────


def test_acquire_enriches_from_s2(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        paper_mod,
        "_lookup_acquire_metadata",
        lambda *_: {
            "title": "Resolved Title",
            "year": 2021,
            "doi": "10.1/X",
            "arxiv_id": "2401.99999",
            "s2_id": "S2DEADBEEF",
        },
    )
    r = handler.acquire(identifier="doi:10.1/x")
    rid = _ref_id(r.body)
    got = store.get_ref(kind="paper", id=rid)
    assert got is not None
    assert got.title == "Resolved Title"
    assert got.year == 2021
    ids = _identifiers(store, rid)
    assert ("doi", "10.1/x") in ids
    assert ("arxiv", "2401.99999") in ids
    assert ("s2", "S2DEADBEEF") in ids


def test_acquire_tags_applied_to_minted_stub(
    handler: PaperHandler, store: Store
) -> None:
    """gr492998: tags= on put/acquire lands on the stub (offline)."""
    r = handler.put(doi="10.1234/tagged-stub", tags=["reading-list"], verify=False)
    assert "minted stub" in r.body
    assert "reading-list" in _tags(store, _ref_id(r.body))


def test_acquire_arxiv_mint_doi_for_other_preprint_is_corroborated(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``10.48550/arxiv.<other id>`` DOI does not self-corroborate: its
    suffix names a different preprint — a conflict on its face, so the
    alias drops WITHOUT a registry call (the registry cannot vouch for
    it while two preprint ids claim one record)."""
    other = "10.48550/arxiv.2001.00001"
    monkeypatch.setattr(
        paper_mod,
        "_lookup_acquire_metadata",
        lambda *_: {
            **_SHAP_S2_META,
            "doi": other,
            "external_ids": {"ArXiv": "1705.07874", "DOI": other},
        },
    )
    consulted: list[str] = []
    monkeypatch.setattr(paper_mod, "_lookup_doi_registry", consulted.append)
    r = handler.acquire(identifier="arxiv:1705.07874")
    ids = _identifiers(store, _ref_id(r.body))
    assert not any(kind == "doi" for kind, _ in ids)
    assert consulted == []


def test_acquire_arxiv_other_preprint_mint_dropped_with_registry_down(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conflicting arXiv ids are evidence enough: dropped without the registry."""
    other = "10.48550/arxiv.2001.00001"
    monkeypatch.setattr(
        paper_mod,
        "_lookup_acquire_metadata",
        lambda *_: {
            **_SHAP_S2_META,
            "doi": other,
            "external_ids": {"ArXiv": "1705.07874", "DOI": other},
        },
    )
    calls: list[str] = []

    def _down(doi: str) -> None:
        calls.append(doi)
        raise RuntimeError("registry down")

    monkeypatch.setattr(paper_mod, "_lookup_doi_registry", _down)
    r = handler.acquire(identifier="arxiv:1705.07874")
    ids = _identifiers(store, _ref_id(r.body))
    assert not any(kind == "doi" for kind, _ in ids)
    assert calls == []


def test_acquire_poisoned_doi_does_not_collapse_onto_shap_ref(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reverse direction: putting the foreign DOI must not adopt the
    poisoned S2 cluster's arXiv/S2 aliases and dedupe onto the SHAP ref."""
    monkeypatch.setattr(
        paper_mod, "_lookup_acquire_metadata", lambda *_: dict(_SHAP_S2_META)
    )
    monkeypatch.setattr(
        paper_mod, "_lookup_doi_registry", lambda *_: dict(_KOREAN_REGISTRY_META)
    )
    shap = _ref_id(
        handler.acquire(
            identifier="arxiv:1705.07874",
            title="A Unified Approach to Interpreting Model Predictions",
        ).body
    )
    r = handler.acquire(identifier="doi:10.9728/dcs.2024.25.7.1847")
    other = _ref_id(r.body)
    assert other != shap
    ids = _identifiers(store, other)
    assert ("doi", "10.9728/dcs.2024.25.7.1847") in ids
    assert not any(kind in ("arxiv", "s2") for kind, _ in ids)


def test_acquire_poisoned_doi_reuses_registry_record(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry record that established the poisoned S2 cluster is
    the DOI's own verification: acquire reuses it instead of asking the
    registries a second time — a second ask can miss and reject an
    already-confirmed DOI (gr710828 review)."""
    foreign = "10.9728/dcs.2024.25.7.1847"
    calls: list[str] = []

    def _registry(doi: str) -> dict[str, object]:
        calls.append(doi)
        return dict(_KOREAN_REGISTRY_META)

    monkeypatch.setattr(
        paper_mod, "_lookup_acquire_metadata", lambda *_: dict(_SHAP_S2_META)
    )
    monkeypatch.setattr(paper_mod, "_lookup_doi_registry", _registry)
    r = handler.acquire(identifier=f"doi:{foreign}")
    assert "minted stub" in r.body
    assert calls == [foreign]
    rid = _ref_id(r.body)
    ids = _identifiers(store, rid)
    assert ("doi", foreign) in ids
    assert not any(kind in ("arxiv", "s2") for kind, _ in ids)
    # Reused record still lands its provenance — verified, not unverified.
    assert _meta(store, rid).get("acquire_registry") == "crossref"
    assert "acquire:unverified" not in _tags(store, rid)


def test_acquire_arxiv_own_mint_doi_skips_registry(
    handler: PaperHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record's own arXiv DataCite mint is kept without a registry call."""
    own = "10.48550/arXiv.1705.07874"
    monkeypatch.setattr(
        paper_mod,
        "_lookup_acquire_metadata",
        lambda *_: {
            **_SHAP_S2_META,
            "doi": own,
            "external_ids": {"ArXiv": "1705.07874", "DOI": own},
        },
    )

    consulted: list[str] = []
    monkeypatch.setattr(paper_mod, "_lookup_doi_registry", consulted.append)
    r = handler.acquire(identifier="arxiv:1705.07874")
    ids = _identifiers(store, _ref_id(r.body))
    assert any(kind == "doi" for kind, _ in ids)
    assert consulted == []


def test_scrub_keeps_doi_when_a_title_has_no_usable_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identical one-word titles (``DNA``) give zero overlap by
    tokenisation, not by disagreement — inconclusive, so the DOI stays."""
    meta = {
        "title": "DNA",
        "doi": "10.5555/dna",
        "authors": [{"name": "Jane Roe"}],
        "external_ids": {"DOI": "10.5555/dna"},
    }
    monkeypatch.setattr(
        paper_mod,
        "_lookup_doi_registry",
        lambda *_: {
            "title": "DNA",
            "authors": [{"given": "Jane", "family": "Roe"}],
        },
    )
    assert paper_mod._scrub_uncorroborated_s2_doi("arxiv", meta) is meta
