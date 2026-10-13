"""The two non-empirical artifact types, ``report`` and ``definition``
(Reto 2026-10-02, review-queue ``nanobuds-paper-13``; method accepted by the
orchestrator with two changes: the genre must also come from the SOURCE, and
the type is approve-only). DB-backed except the assemble tests; reuses the
seed helpers of ``test_nanopub_gates_mint`` / ``test_nanopub_non_paper_sources``.

Fixtures are the 5 measured hubs' sentences: fi191169 (Canatu supplies
films, reworded to a report), fi449531 (schwarzites), fi262300 (molecular
assembler)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from rdflib import RDF, Dataset, URIRef

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.finding import FindingHandler
from precis.nanopub import assemble, evidence, gates, mint
from precis.nanopub.vocab import DEFINITION, PROV, REPORT
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub
from precis.taproot.reword import select_reword_cohort
from tests.test_nanopub_gates_mint import (
    _QUOTE,
    _SNIP,
    _gate_slugs,
    _payload,
    _seed_hub,
    _seed_paper,
)
from tests.test_nanopub_non_paper_sources import _seed_non_paper_source

#: fi191169 as it stands (no attribution verb, no genre, no method) ...
_CANATU_AS_IS = (
    "Canatu Oyj supplies carbon NanoBud transparent conductive films to major OEMs."
)
#: ... and reworded so it can pass as a report.
_REPORT = (
    "A 2014 conference report by Canatu states that Canatu supplies carbon "
    "NanoBud transparent conductive films to major OEMs."
)
_REPORT_2 = (
    "A 2014 conference report by Canatu reports medium-volume production of "
    "carbon NanoBud films and touch sensors in Helsinki."
)
#: fi449531 / fi262300.
_DEFINITION = "Negatively curved all-carbon frameworks are called schwarzites."
_DEFINITION_2 = (
    "A molecular assembler refers to a device that positions reactive "
    "molecules with atomic precision."
)
#: An empirical result dressed as nothing: neither type's sentence.
_EMPIRICAL = "DFT shows MOFs can be anisotropic up to 400:1."

_ACCESSION = "0000320193-23-000106"


def _typed(chunk: int, artifact_type: str, **over: Any) -> dict[str, Any]:
    """An approve payload for a report/definition: one grounded passage, no
    structured fields (``_payload``'s quantity field belongs to the
    empirical fixtures)."""
    payload = _payload(chunk, **over)
    payload["fields"] = {}
    payload["artifact_type"] = artifact_type
    return payload


def _set_entry_type(store: Any, ref_id: int, entry_type: str | None) -> None:
    with store.pool.connection() as conn:
        if entry_type is None:
            conn.execute(
                "UPDATE refs SET meta = meta - 'entry_type' WHERE ref_id = %s",
                (ref_id,),
            )
        else:
            conn.execute(
                "UPDATE refs SET meta = meta || jsonb_build_object('entry_type', %s::text) "
                "WHERE ref_id = %s",
                (entry_type, ref_id),
            )


def _set_journal(store: Any, ref_id: int, journal: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object('journal', %s::text) "
            "WHERE ref_id = %s",
            (journal, ref_id),
        )


def _proceedings_hub(
    store: Any, sentence: str, *, entry_type: str | None = "proceedings-article"
) -> tuple[int, int]:
    paper, chunk, _sha = _seed_paper(store)
    _set_entry_type(store, paper, entry_type)
    return _seed_hub(store, sentence, paper, chunk), chunk


# ── resolve_artifact_type ────────────────────────────────────────────────


def _bundle(kind: str = "claim") -> evidence.HubBundle:
    return evidence.HubBundle(
        hub_ref_id=1,
        sentence="s",
        artifact_type=kind,
        sources=[],
        grounding_chunks=[],
    )


@pytest.mark.parametrize("kind", ["report", "definition"])
def test_resolve_returns_the_explicit_type(kind: str) -> None:
    assert gates.resolve_artifact_type(_bundle(), {"artifact_type": kind}) == kind


@pytest.mark.parametrize("bad", ["claim", "composite", "hypothesis", "Report", "", 3])
def test_resolve_refuses_any_other_artifact_type(bad: Any) -> None:
    with pytest.raises(BadInput, match="artifact_type"):
        gates.resolve_artifact_type(_bundle(), {"artifact_type": bad})


def test_resolve_refuses_a_combination_the_types_do_not_allow() -> None:
    with pytest.raises(BadInput, match="hypothesis"):
        gates.resolve_artifact_type(
            _bundle(), {"artifact_type": "report", "hypothesis": True}
        )
    with pytest.raises(BadInput, match="composite"):
        gates.resolve_artifact_type(
            _bundle("composite"), {"artifact_type": "definition"}
        )


def test_absent_type_still_derives_as_before() -> None:
    assert gates.resolve_artifact_type(_bundle(), {}) == "claim"
    assert gates.resolve_artifact_type(_bundle("composite"), {}) == "composite"
    assert gates.resolve_artifact_type(_bundle(), {"hypothesis": True}) == "hypothesis"


# ── sentence checks (no DB) ──────────────────────────────────────────────


def test_both_types_are_exempt_from_the_epistemic_pair_only() -> None:
    for kind in ("report", "definition"):
        assert gates._ARTIFACT_LINT_EXEMPTIONS[kind] == {
            "no-epistemic-mode",
            "no-evidence-verb",
        }
        # The fixtures fail the pair as claims, pass as typed ...
        assert gates.check_claim_sentence(_REPORT) != []
        assert gates.check_claim_sentence(_REPORT, artifact_type="report") == []
        assert gates.check_claim_sentence(_DEFINITION, artifact_type="definition") == []
    # ... but every other blocking code still bites a typed sentence.
    no_period = _DEFINITION.rstrip(".")
    assert [
        v.message.split(":", 1)[0]
        for v in gates.check_claim_sentence(no_period, artifact_type="definition")
    ] == ["no-terminal-period"]


@pytest.mark.parametrize(
    ("sentence", "codes"),
    [
        (_REPORT, set()),
        (_REPORT_2, set()),
        # "reports" the NOUN ("conference reports") is not the attribution verb.
        (
            "Canatu supplies films, per its conference reports to major OEMs.",
            {"report-attribution"},
        ),
        # verb, no genre token
        (
            "Canatu states that Canatu supplies carbon NanoBud films to major OEMs.",
            {"report-genre"},
        ),
        # genre, no verb
        (
            "A 2014 conference report by Canatu on carbon NanoBud films for OEMs.",
            {"report-attribution"},
        ),
        # the original fi191169 sentence: neither
        (_CANATU_AS_IS, {"report-attribution", "report-genre"}),
        (
            "According to a 2014 press release, Canatu supplies NanoBud films.",
            set(),
        ),
        ("A 2014 white paper describes carbon NanoBud films for OEMs.", set()),
    ],
)
def test_report_sentence_checks(store: Any, sentence: str, codes: set[str]) -> None:
    got = {v.gate for v in gates.check_nonempirical(store, sentence, "report", [])}
    assert got == codes


@pytest.mark.parametrize(
    ("sentence", "ok"),
    [
        (_DEFINITION, True),
        (_DEFINITION_2, True),
        ("A schwarzite is defined as a negatively curved carbon framework.", True),
        ("The term fullerene denotes a closed carbon cage.", True),
        ("Carbon cages are known as fullerenes.", True),
        (_EMPIRICAL, False),
        (_REPORT, False),
    ],
)
def test_definition_sentence_checks(store: Any, sentence: str, ok: bool) -> None:
    got = {v.gate for v in gates.check_nonempirical(store, sentence, "definition", [])}
    assert got == (set() if ok else {"definition-copula"})


def test_other_types_run_no_replacement_checks(store: Any) -> None:
    for kind in ("claim", "composite", "hypothesis"):
        assert gates.check_nonempirical(store, _EMPIRICAL, kind, []) == []


# ── the gates, end to end ────────────────────────────────────────────────


def test_report_from_a_proceedings_paper_passes(store: Any) -> None:
    hub, chunk = _proceedings_hub(store, _REPORT)
    assert _gate_slugs(store, hub, _typed(chunk, "report")) == set()


@pytest.mark.parametrize("entry_type", [None, "report", "posted-content", "other"])
def test_report_accepts_any_non_journal_paper_type(
    store: Any, entry_type: str | None
) -> None:
    hub, chunk = _proceedings_hub(store, _REPORT_2, entry_type=entry_type)
    assert _gate_slugs(store, hub, _typed(chunk, "report")) == set()


def test_report_from_a_non_paper_evidence_kind_passes(store: Any) -> None:
    ref_id, chunk = _seed_non_paper_source(store, "edgar", slug=_ACCESSION)
    hub = _seed_hub(
        store,
        "A 2023 SEC filing by Apple states that Apple reported record revenue.",
        ref_id,
        chunk,
    )
    payload = _typed(chunk, "report")
    payload["passages"][0]["doi"] = ""
    assert _gate_slugs(store, hub, payload) == set()


def test_report_grounded_only_on_a_journal_article_is_refused(store: Any) -> None:
    hub, chunk = _proceedings_hub(store, _REPORT, entry_type="journal-article")
    bundle = evidence.load_bundle(store, hub)
    violations = gates.run_mint_gates(store, bundle, _typed(chunk, "report"))
    assert [v.gate for v in violations] == ["report-source-genre"]
    assert "journal-article" in violations[0].message


def test_sid_digest_journal_article_is_accepted_by_the_venue_rule(
    store: Any,
) -> None:
    # Crossref files the SID Symposium Digest as journal-article; the venue
    # rule treats it as the proceedings it is (the Canatu hubs' source).
    hub, chunk = _proceedings_hub(store, _REPORT, entry_type="journal-article")
    ref_id = evidence.load_bundle(store, hub).sources[0].ref_id
    _set_journal(store, ref_id, "SID Symposium Digest of Technical Papers")
    assert _gate_slugs(store, hub, _typed(chunk, "report")) == set()


@pytest.mark.parametrize(
    "journal", ["Nature Communications", "Journal of Applied Physics", ""]
)
def test_non_proceedings_journal_article_is_still_refused(
    store: Any, journal: str
) -> None:
    hub, chunk = _proceedings_hub(store, _REPORT, entry_type="journal-article")
    ref_id = evidence.load_bundle(store, hub).sources[0].ref_id
    _set_journal(store, ref_id, journal)
    assert _gate_slugs(store, hub, _typed(chunk, "report")) == {"report-source-genre"}


@pytest.mark.parametrize(
    "journal",
    [
        "Proceedings of the National Academy of Sciences",
        "Proceedings of the IEEE",
        "Digest Journal of Nanomaterials and Biostructures",
    ],
)
def test_vetoed_journal_articles_are_refused_and_say_why(
    store: Any, journal: str
) -> None:
    hub, chunk = _proceedings_hub(store, _REPORT, entry_type="journal-article")
    ref_id = evidence.load_bundle(store, hub).sources[0].ref_id
    _set_journal(store, ref_id, journal)
    bundle = evidence.load_bundle(store, hub)
    violations = gates.run_mint_gates(store, bundle, _typed(chunk, "report"))
    assert [v.gate for v in violations] == ["report-source-genre"]
    assert "not-venue list" in violations[0].message


def test_the_accepting_arm_is_recorded_per_source(store: Any) -> None:
    # venue arm
    hub, chunk = _proceedings_hub(store, _REPORT, entry_type="journal-article")
    ref_id = evidence.load_bundle(store, hub).sources[0].ref_id
    _set_journal(store, ref_id, "Journal of Physics: Conference Series")
    passages = _typed(chunk, "report")["passages"]
    (v,) = gates.report_source_verdicts(store, passages)
    assert (v.ref_id, v.accepted, v.arm) == (ref_id, True, "venue:Conference")
    assert gates.check_report_sources(store, passages) == []
    # entry_type arm
    hub2, chunk2 = _proceedings_hub(store, _REPORT_2)
    (v2,) = gates.report_source_verdicts(store, _typed(chunk2, "report")["passages"])
    assert (v2.accepted, v2.arm) == (True, "entry_type:proceedings-article")
    # untyped paper
    hub3, chunk3 = _proceedings_hub(store, _CANATU_AS_IS, entry_type=None)
    (v3,) = gates.report_source_verdicts(store, _typed(chunk3, "report")["passages"])
    assert v3.arm == "entry_type:none"
    # non-paper kind
    ref_id4, chunk4 = _seed_non_paper_source(store, "edgar", slug=_ACCESSION)
    (v4,) = gates.report_source_verdicts(store, _typed(chunk4, "report")["passages"])
    assert (v4.accepted, v4.arm) == (True, "kind:edgar")
    del hub, hub2, hub3, ref_id4


def test_approve_freezes_the_accepting_arm_for_a_report(store: Any) -> None:
    hub, chunk = _proceedings_hub(store, _REPORT, entry_type="journal-article")
    ref_id = evidence.load_bundle(store, hub).sources[0].ref_id
    _set_journal(store, ref_id, "SID Symposium Digest of Technical Papers")
    row = mint.approve(store, hub, payload=_typed(chunk, "report"), interactive=True)
    assert row.grounding["report_source_arms"] == {str(ref_id): "venue:Symposium"}


def test_report_with_one_journal_article_among_two_passages_is_refused(
    store: Any,
) -> None:
    good, good_chunk, _ = _seed_paper(store)
    _set_entry_type(store, good, "proceedings-article")
    bad, bad_chunk, _ = _seed_paper(store)
    _set_entry_type(store, bad, "journal-article")
    payload = _typed(good_chunk, "report")
    payload["passages"].append({**payload["passages"][0], "chunk_id": bad_chunk})
    got = gates.check_report_sources(store, payload["passages"])
    assert [v.gate for v in got] == ["report-source-genre"]
    assert f"ref {bad}" in got[0].message and f"ref {good}" not in got[0].message


def test_report_requires_a_grounding_passage_and_has_no_hanging_form(
    store: Any,
) -> None:
    hub, _chunk = _proceedings_hub(store, _REPORT)
    for payload in (
        {"artifact_type": "report", "hanging": True},
        {"artifact_type": "definition", "hanging": True},
    ):
        assert "schema-lint" in _gate_slugs(store, hub, payload)


def test_report_hearsay_and_quote_gates_still_apply(store: Any) -> None:
    paper, chunk, _ = _seed_paper(store, section=["References"])
    _set_entry_type(store, paper, "proceedings-article")
    hub = _seed_hub(store, _REPORT, paper, chunk)
    slugs = _gate_slugs(store, hub, _typed(chunk, "report"))
    assert slugs == {"primary-source"}
    paraphrase = _typed(chunk, "report")
    paraphrase["passages"][0]["quote"] = "Canatu says it supplies OEMs"
    hub2, chunk2 = _proceedings_hub(store, _REPORT_2)
    paraphrase["passages"][0]["chunk_id"] = chunk2
    assert "quote-verbatim" in _gate_slugs(store, hub2, paraphrase)


def test_definition_passes_and_fails_by_name(store: Any) -> None:
    hub, chunk = _proceedings_hub(store, _DEFINITION)
    assert _gate_slugs(store, hub, _typed(chunk, "definition")) == set()
    # An empirical sentence typed as a definition: the exempted pair no
    # longer catches it, the copula check does.
    hub2, chunk2 = _proceedings_hub(store, _EMPIRICAL)
    assert _gate_slugs(store, hub2, _typed(chunk2, "definition")) == {
        "definition-copula"
    }


def test_the_same_sentence_untyped_still_fails_the_epistemic_pair(
    store: Any,
) -> None:
    hub, chunk = _proceedings_hub(store, _REPORT)
    payload = _typed(chunk, "report")
    del payload["artifact_type"]
    assert "claim-sentence" in _gate_slugs(store, hub, payload)


# ── approve-only ─────────────────────────────────────────────────────────


def test_artifact_type_is_refused_at_put(store: Any) -> None:
    with pytest.raises(BadInput, match="approve-only"):
        FindingHandler(hub=Hub(store=store)).put(title=_REPORT, artifact_type="report")


def test_artifact_type_is_refused_at_mint_hub(store: Any) -> None:
    for kind in ("report", "definition"):
        with pytest.raises(BadInput, match="approve-only"):
            mint_hub(
                store,
                CanonicalClaim(sentence=_REPORT, scope={}),
                extra_meta={"artifact_type": kind},
            )


def test_invalid_approve_type_is_refused_before_the_reword(store: Any) -> None:
    hub, chunk = _proceedings_hub(store, _REPORT)
    payload = _typed(chunk, "claim")
    with pytest.raises(BadInput, match="artifact_type"):
        mint.approve(
            store, hub, payload=payload, title="A reworded sentence.", interactive=True
        )
    ref = store.fetch_refs_by_ids([hub])[hub]
    assert ref.title == _REPORT  # untouched


def test_approve_persists_the_type_the_marker_and_the_timestamp(store: Any) -> None:
    hub, chunk = _proceedings_hub(store, _REPORT)
    # A candidate staged as a plain claim is re-typed by the approve.
    store.nanopub_create_publish_row(hub)
    row = mint.approve(store, hub, payload=_typed(chunk, "report"), interactive=True)
    assert row.state == "reviewed"
    assert row.artifact_type == "report"
    assert row.grounding["artifact_type"] == "report"
    ref = store.fetch_refs_by_ids([hub])[hub]
    assert (ref.meta or {}).get("artifact_type") == "report"
    # the weekly gauge sees it
    weekly = store.nanopub_nonempirical_approvals_by_week()
    assert [(kind, n) for _wk, kind, n in weekly] == [("report", 1)]


def test_approve_creates_a_definition_row_when_none_exists(store: Any) -> None:
    hub, chunk = _proceedings_hub(store, _DEFINITION)
    row = mint.approve(
        store, hub, payload=_typed(chunk, "definition"), interactive=True
    )
    assert row.artifact_type == "definition"


def test_reapprove_as_a_claim_overwrites_a_stale_marker(store: Any) -> None:
    hub, chunk = _proceedings_hub(store, _EMPIRICAL)
    # The marker an earlier report/definition approve would have left
    # behind (a reopen keeps refs.meta), then an approve as a plain claim.
    store.stamp_ref_meta(hub, {"artifact_type": "definition"})
    row = mint.approve(store, hub, payload=_payload(chunk), interactive=True)
    assert row.artifact_type == "claim"
    ref = store.fetch_refs_by_ids([hub])[hub]
    assert (ref.meta or {}).get("artifact_type") == "claim"


def test_a_hypothesis_hub_cannot_be_approved_as_a_report(store: Any) -> None:
    hub, chunk = _proceedings_hub(store, _REPORT)
    store.stamp_ref_meta(hub, {"artifact_type": "hypothesis"})
    with pytest.raises(BadInput, match="hypothesis hub"):
        mint.approve(store, hub, payload=_typed(chunk, "report"), interactive=True)


def test_signing_re_gates_under_the_frozen_type(store: Any, monkeypatch: Any) -> None:
    from precis.nanopub.keys import generate_keypair

    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)
    hub, chunk = _proceedings_hub(store, _REPORT)
    mint.approve(store, hub, payload=_typed(chunk, "report"), interactive=True)

    signed = mint.sign(store, hub)
    assert signed.state == "signed"
    artifact = store.nanopub_artifact(signed.artifact_id)
    assert artifact is not None and artifact.artifact_type == "report"
    trig = artifact.trig_bytes.decode("utf-8")
    ds = Dataset()
    ds.parse(data=trig, format="trig")
    assert list(ds.quads((None, RDF.type, REPORT, None)))
    assert list(ds.quads((None, PROV.wasAttributedTo, None, None)))


# ── reword cohort ────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["report", "definition"])
def test_reword_cohort_excludes_the_marker_without_a_publish_row(
    store: Any, kind: str
) -> None:
    sentence = "Graphene has a tensile strength of 130 GPa."
    hub = mint_hub(store, CanonicalClaim(sentence=sentence, scope={}))
    assert [c.hub_ref_id for c in select_reword_cohort(store)] == [hub]  # control
    store.stamp_ref_meta(hub, {"artifact_type": kind})
    assert select_reword_cohort(store) == []
    assert store.nanopub_publish_row(hub) is None


def test_reword_cohort_keeps_a_hub_re_marked_as_a_claim(store: Any) -> None:
    hub = mint_hub(
        store,
        CanonicalClaim(
            sentence="Graphene has a tensile strength of 130 GPa.", scope={}
        ),
    )
    store.stamp_ref_meta(hub, {"artifact_type": "claim"})
    assert [c.hub_ref_id for c in select_reword_cohort(store)] == [hub]


# ── migration 0201 ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kind", ["claim", "composite", "hypothesis", "report", "definition"]
)
def test_publish_check_accepts_every_type(store: Any, kind: str) -> None:
    hub = mint_hub(store, CanonicalClaim(sentence=f"Hub for {kind} rows.", scope={}))
    assert (
        store.nanopub_create_publish_row(hub, artifact_type=kind).artifact_type == kind
    )


@pytest.mark.parametrize("bad", ["compound", "attributed", ""])
def test_publish_check_still_refuses_unknown_types(store: Any, bad: str) -> None:
    hub = mint_hub(store, CanonicalClaim(sentence=f"Hub for {bad!r} row.", scope={}))
    with pytest.raises(psycopg.errors.CheckViolation):
        store.nanopub_create_publish_row(hub, artifact_type=bad)


# ── assemble ─────────────────────────────────────────────────────────────


def _input(kind: str, *, n_sources: int = 1) -> assemble.MintInput:
    grounding = [
        assemble.GroundingInput(
            doi=f"10.1002/j.2168-0159.2014.tb0005{i}.x",
            pdf_sha256="ab" * 32,
            quote=_QUOTE,
            snip=_SNIP,
            role="corroborates",
            source_title="Printed Touch Sensors Using Carbon NanoBud Material",
        )
        for i in range(n_sources)
    ]
    return assemble.MintInput(
        artifact_type=kind,
        sentence=_REPORT,
        aida_uri="http://purl.org/aida/x",
        hub_ref_id=191169,
        grounding=grounding,
    )


def _graphs(inp: assemble.MintInput) -> tuple[Any, Any, Any]:
    return assemble.build_graphs(inp, assemble.DRAFT_NS)


@pytest.mark.parametrize(
    ("kind", "rdf_type"), [("report", REPORT), ("definition", DEFINITION)]
)
def test_assemble_types_the_claim_node(kind: str, rdf_type: URIRef) -> None:
    assertion, _prov, _pub = _graphs(_input(kind))
    assert (assemble.DRAFT_NS["claim"], RDF.type, rdf_type) in assertion
    assert str(REPORT).endswith("#Report") and str(DEFINITION).endswith("#Definition")


def test_report_is_attributed_to_each_source_node() -> None:
    inp = _input("report", n_sources=2)
    _a, prov, _p = _graphs(inp)
    nodes = {URIRef(g.source_node) for g in inp.grounding if g.source_node}
    assert len(nodes) == 2
    attributed = {o for _s, _p_, o in prov.triples((None, PROV.wasAttributedTo, None))}
    assert attributed == nodes


@pytest.mark.parametrize("kind", ["definition", "claim"])
def test_only_a_report_carries_was_attributed_to(kind: str) -> None:
    _a, prov, _p = _graphs(_input(kind))
    assert not list(prov.triples((None, PROV.wasAttributedTo, None)))
    # still derived from, and quoting, its source
    assert list(prov.triples((None, PROV.wasDerivedFrom, None)))
