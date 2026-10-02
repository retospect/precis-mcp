"""Edgar / datasheet evidence on the nanopub path
(docs/backlog/claim-publication-nanopub-ots.md): the read door accepts every
kind the write door does, an edgar filing is cited by its SEC accession, and
a datasheet by ``urn:sha256`` of its PDF (URL as a second triple). DB-backed;
reuses the seed helpers of ``test_nanopub_gates_mint``."""

from __future__ import annotations

from typing import Any

from rdflib import Literal, URIRef

from precis.nanopub import assemble, evidence, gates, mint
from precis.nanopub.vocab import PRECIS
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import EVIDENCE_SRC_KINDS, attach_evidence, mint_hub
from tests.test_nanopub_gates_mint import (
    _QUOTE,
    _SNIP,
    _add_body_chunk,
    _gate_slugs,
    _payload,
    _seed_hub,
    _seed_paper,
    _two_passage_payload,
)

_ACCESSION = "0000320193-23-000106"
_ACCESSION_URL = "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/"
_SENTENCE = "DFT shows MOFs can be anisotropic up to 400:1."


def _seed_non_paper_source(
    store: Any,
    kind: str,
    *,
    slug: str | None,
    title: str = "Evidence source",
) -> tuple[int, int]:
    """An evidence ref of ``kind`` with an explicit slug (its ``cite_key`` identifier) and one
    body chunk — no DOI, no pdf_sha256 (an HTML filing / a datasheet).
    Returns ``(ref_id, chunk_id)``."""
    with store.pool.connection() as conn:
        ref_row = conn.execute(
            "INSERT INTO refs (kind, set_by, title) "
            "VALUES (%s, 'system', %s) RETURNING ref_id",
            (kind, title),
        ).fetchone()
        assert ref_row is not None
        ref_id = int(ref_row[0])
        # ``Ref.slug`` is the ``cite_key`` identifier row (no slug column).
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('cite_key', %s, %s, 'test')",
            (slug, ref_id),
        )
        chunk_row = conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, "
            "section_path) VALUES (%s, 'system', 0, 'paragraph', %s, %s) "
            "RETURNING chunk_id",
            (
                ref_id,
                f"Tensorial analysis. {_QUOTE}, in stark contrast.",
                ["Results"],
            ),
        ).fetchone()
        assert chunk_row is not None
    return ref_id, int(chunk_row[0])


def _anchorless_payload(chunk_id: int) -> dict[str, Any]:
    """The reviewer payload for a source with no DOI and no pdf sha."""
    payload = _payload(chunk_id)
    payload["passages"][0]["doi"] = ""
    return payload


def test_source_filter_accepts_exactly_the_evidence_src_kinds(store: Any) -> None:
    """load_bundle's read-side ``_source`` filter and the write door's
    EVIDENCE_SRC_KINDS must not drift: every attachable kind reaches the
    bundle."""
    hub = mint_hub(store, CanonicalClaim(sentence="Every kind is read.", scope={}))
    for kind in sorted(EVIDENCE_SRC_KINDS):
        slug = _ACCESSION if kind == "edgar" else f"{kind}-slug"
        ref_id, _chunk = _seed_non_paper_source(store, kind, slug=slug, title=kind)
        attach_evidence(
            store,
            hub_ref_id=hub,
            paper_ref_id=ref_id,
            role="corroborates",
            check_retraction=False,
        )
    bundle = evidence.load_bundle(store, hub)
    assert {s.kind for s in bundle.sources} == set(EVIDENCE_SRC_KINDS)


def test_edgar_supporter_reaches_bundle_with_archive_url(store: Any) -> None:
    ref_id, chunk = _seed_non_paper_source(store, "edgar", slug=_ACCESSION)
    hub = _seed_hub(store, _SENTENCE, ref_id, chunk)
    (src,) = evidence.load_bundle(store, hub).sources
    assert src.kind == "edgar"
    assert src.source_uri == _ACCESSION_URL
    assert src.accession == _ACCESSION
    assert src.doi is None


def test_edgar_grounded_hub_passes_the_gates_and_assembles_with_sec_anchor(
    store: Any,
) -> None:
    ref_id, chunk = _seed_non_paper_source(store, "edgar", slug=_ACCESSION)
    hub = _seed_hub(store, _SENTENCE, ref_id, chunk)
    payload = _anchorless_payload(chunk)
    assert _gate_slugs(store, hub, payload) == set()

    row = mint.approve(store, hub, payload=payload, interactive=True)
    (frozen,) = row.grounding["passages"]
    assert frozen["source_uri"] == _ACCESSION_URL
    assert frozen["accession"] == _ACCESSION

    inp, _deps = mint._mint_input(store, row, evidence.load_bundle(store, hub))
    _, prov, _ = assemble.build_graphs(inp, assemble.DRAFT_NS)
    src = URIRef(_ACCESSION_URL)
    assert (src, PRECIS["secAccession"], Literal(_ACCESSION)) in prov
    assert (None, None, src) in prov
    assert not list(prov.triples((None, PRECIS["sourcePdfSha256"], None)))
    assert "doi.org" not in prov.serialize(format="nt")


def test_edgar_passage_with_unverifiable_pinned_sha_is_refused(store: Any) -> None:
    ref_id, chunk = _seed_non_paper_source(store, "edgar", slug=_ACCESSION)
    hub = _seed_hub(store, _SENTENCE, ref_id, chunk)
    payload = _anchorless_payload(chunk)
    payload["passages"][0]["pdf_sha256"] = "ab" * 32
    assert _gate_slugs(store, hub, payload) == {"pdf-sha"}


def test_bad_edgar_slug_is_a_violation_not_a_crash(store: Any) -> None:
    ref_id, chunk = _seed_non_paper_source(store, "edgar", slug="not-an-accession")
    hub = _seed_hub(store, _SENTENCE, ref_id, chunk)
    bundle = evidence.load_bundle(store, hub)
    (src,) = bundle.sources
    assert src.source_uri is None and src.accession is None
    violations = gates.run_mint_gates(store, bundle, _anchorless_payload(chunk))
    (v,) = [x for x in violations if x.gate == "grounding"]
    assert "SEC accession" in v.message and "not-an-accession" in v.message


def _give_datasheet_sha_and_url(
    store: Any, ref_id: int, *, sha: str, url: str | None
) -> None:
    """A datasheet's ingested-PDF sha (``pdf_sha256`` identifier row) and,
    when given, the auto-pull ingest's ``meta.source_url``."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('pdf_sha256', %s, %s, 'test')",
            (sha, ref_id),
        )
        if url is not None:
            conn.execute(
                "UPDATE refs SET meta = COALESCE(meta, '{}'::jsonb) "
                "|| jsonb_build_object('source_url', %s::text) WHERE ref_id = %s",
                (url, ref_id),
            )


def test_datasheet_is_cited_by_content_sha_with_its_url_as_a_second_triple(
    store: Any,
) -> None:
    """Reto 2026-10-02 (claims-and-evidence-2): urn:sha256 of the PDF always;
    the fetch URL rides as rdfs:seeAlso when known."""
    from rdflib import RDFS

    sha = "cd" * 32
    url = "https://example.com/ds/esp32.pdf"
    ref_id, chunk = _seed_non_paper_source(store, "datasheet", slug="ds-1")
    _give_datasheet_sha_and_url(store, ref_id, sha=sha, url=url)
    hub = _seed_hub(store, _SENTENCE, ref_id, chunk)
    (src,) = evidence.load_bundle(store, hub).sources
    assert src.source_uri == f"urn:sha256:{sha}"
    assert src.source_url == url

    payload = _anchorless_payload(chunk)
    payload["passages"][0]["pdf_sha256"] = sha
    # A reviewer-supplied URL is never the authority — approve re-derives it.
    payload["passages"][0]["source_url"] = "https://attacker.example/x.pdf"
    assert _gate_slugs(store, hub, payload) == set()

    row = mint.approve(store, hub, payload=payload, interactive=True)
    (frozen,) = row.grounding["passages"]
    assert frozen["source_uri"] == f"urn:sha256:{sha}"
    assert frozen["source_url"] == url

    inp, _deps = mint._mint_input(store, row, evidence.load_bundle(store, hub))
    _, prov, _ = assemble.build_graphs(inp, assemble.DRAFT_NS)
    node = URIRef(f"urn:sha256:{sha}")
    assert (node, RDFS.seeAlso, URIRef(url)) in prov
    assert "attacker.example" not in prov.serialize(format="nt")


def test_datasheet_without_a_known_url_is_cited_by_sha_alone(store: Any) -> None:
    sha = "ef" * 32
    ref_id, chunk = _seed_non_paper_source(store, "datasheet", slug="ds-2")
    _give_datasheet_sha_and_url(store, ref_id, sha=sha, url=None)
    hub = _seed_hub(store, _SENTENCE, ref_id, chunk)
    (src,) = evidence.load_bundle(store, hub).sources
    assert src.source_uri == f"urn:sha256:{sha}" and src.source_url is None
    payload = _anchorless_payload(chunk)
    payload["passages"][0]["pdf_sha256"] = sha
    assert _gate_slugs(store, hub, payload) == set()


def test_datasheet_without_a_pdf_sha_is_refused_by_name(store: Any) -> None:
    ref_id, chunk = _seed_non_paper_source(store, "datasheet", slug="ds-3")
    hub = _seed_hub(store, _SENTENCE, ref_id, chunk)
    bundle = evidence.load_bundle(store, hub)
    (src,) = bundle.sources
    assert src.kind == "datasheet" and src.source_uri is None
    violations = gates.run_mint_gates(store, bundle, _anchorless_payload(chunk))
    (v,) = [x for x in violations if x.gate == "grounding"]
    assert "datasheet has no single pdf_sha256" in v.message


def test_paper_still_requires_a_doi(store: Any) -> None:
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, _SENTENCE, paper, chunk)
    bundle = evidence.load_bundle(store, hub)
    violations = gates.run_mint_gates(store, bundle, _anchorless_payload(chunk))
    assert any("no DOI" in v.message for v in violations)


def test_approve_freezes_a_paper_source_uri_and_groups_contiguity_by_it(
    store: Any,
) -> None:
    doi = "10.1103/physrevlett.109.195502"
    paper, chunk1, sha = _seed_paper(store, doi=doi)
    chunk2 = _add_body_chunk(
        store, paper, ord=1, text="The elastic modulus stays isotropic overall."
    )
    hub = _seed_hub(store, "DFT shows two adjacent supporting passages.", paper, chunk1)
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": f"pc{chunk2}"},
        check_retraction=False,
    )
    payload = _two_passage_payload(doi, sha, chunk1, _QUOTE, _SNIP, chunk2)
    row = mint.approve(store, hub, payload=payload, interactive=True)
    passages = row.grounding["passages"]
    assert {p["contiguous_group"] for p in passages} == {True}
    assert {p["source_uri"] for p in passages} == {f"https://doi.org/{doi}"}


def test_patent_passage_with_a_hand_typed_doi_is_refused(store: Any) -> None:
    """Round-1 review: the payload DOI is reviewer-editable, so a patent with
    no DOI on record must not publish under a typed-in DOI URL."""
    ref_id, chunk = _seed_non_paper_source(store, "patent", slug="US1234567B2")
    hub = _seed_hub(store, _SENTENCE, ref_id, chunk)
    bundle = evidence.load_bundle(store, hub)
    violations = gates.run_mint_gates(store, bundle, _payload(chunk))
    assert any("no DOI on record" in v.message for v in violations)


def test_approve_freezes_the_papers_doi_on_record_over_a_typed_in_one(
    store: Any,
) -> None:
    paper, chunk, sha = _seed_paper(store, doi="10.1000/on-record")
    hub = _seed_hub(store, _SENTENCE, paper, chunk)
    typed = _payload(chunk, sha=sha)
    typed["passages"][0]["doi"] = "10.1000/typed-in"
    row = mint.approve(store, hub, payload=typed, interactive=True)
    (frozen,) = row.grounding["passages"]
    assert frozen["doi"] == "10.1000/on-record"
    assert frozen["source_uri"] == "https://doi.org/10.1000/on-record"
