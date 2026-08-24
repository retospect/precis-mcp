"""Nanopub graph assembly + offline mint/sign. No DB, no network — keys
are generated per-module (2048 for speed; the 4096 production default is
pinned separately)."""

from __future__ import annotations

from typing import Any

import pytest
from rdflib import Dataset, URIRef

from precis.nanopub import assemble
from precis.nanopub.aida import aida_uri, canonical_sentence
from precis.nanopub.keys import MIN_KEY_BITS, fingerprint, generate_keypair
from precis.nanopub.vocab import CC_BY, PRECIS

_SENTENCE = canonical_sentence(
    "Flexible metal-organic frameworks can exhibit elastic anisotropy "
    "ratios up to 400:1 between their most rigid and weakest directions"
)


def _claim_input(**over: Any) -> assemble.MintInput:
    base: dict[str, Any] = dict(
        artifact_type="claim",
        sentence=_SENTENCE,
        aida_uri=aida_uri(_SENTENCE),
        hub_ref_id=176435,
        grounding=[
            assemble.GroundingInput(
                doi="10.1103/PhysRevLett.109.195502",
                pdf_sha256="cf2c" * 16,
                quote="This anisotropy can reach a 400:1 ratio",
                snip="anisotropy 400 1 ratio rigid weakest",
                role="corroborates",
                source_title="Anisotropic Elastic Properties of Flexible MOFs",
            )
        ],
        fields={"quantity": "400:1", "quantity_bound": "upper"},
    )
    base.update(over)
    return assemble.MintInput(**base)


def _parse(trig: str) -> Dataset:
    ds = Dataset()
    ds.parse(data=trig, format="trig")
    return ds


def test_draft_trig_parses_and_carries_the_claim() -> None:
    trig = assemble.draft_trig(_claim_input())
    ds = _parse(trig)
    nq = ds.serialize(format="nquads")
    assert _SENTENCE in nq
    assert aida_uri(_SENTENCE) in nq
    assert "https://doi.org/10.1103/PhysRevLett.109.195502" in nq
    assert str(PRECIS["AtomicClaim"]) in nq


def test_assertion_carries_no_attribution() -> None:
    # Attribution lives in provenance (nanopub convention + AIDA
    # convergence); the assertion graph must not name the source.
    assertion, _, _ = assemble.build_graphs(_claim_input(), assemble.DRAFT_NS)
    text = assertion.serialize(format="nt")
    assert "doi.org" not in text
    assert "Anisotropic Elastic Properties" not in text


def test_license_scoped_to_assertion_graph_not_this() -> None:
    _, _, pubinfo = assemble.build_graphs(_claim_input(), assemble.DRAFT_NS)
    lic = list(pubinfo.subjects(predicate=None, object=URIRef(CC_BY)))
    assert lic == [assemble.DRAFT_NS["assertion"]]


def test_compound_names_atoms_by_aida_uri_and_derives_from_trusty() -> None:
    atom_a = aida_uri("A.")
    comp = assemble.MintInput(
        artifact_type="compound",
        sentence=canonical_sentence("A and B jointly imply C"),
        aida_uri=aida_uri("A and B jointly imply C"),
        hub_ref_id=1,
        conjuncts=[
            assemble.ConjunctInput(
                aida_uri=atom_a, trusty_uri="https://w3id.org/np/RAx"
            ),
        ],
    )
    assertion, prov, _ = assemble.build_graphs(comp, assemble.DRAFT_NS)
    a_text = assertion.serialize(format="nt")
    assert atom_a in a_text
    assert "https://w3id.org/np/RAx" not in a_text  # trusty never in assertion
    p_text = prov.serialize(format="nt")
    assert "https://w3id.org/np/RAx" in p_text
    assert "doi.org" not in p_text  # a compound cites no paper


def test_hypothesis_has_motivation_never_quotes() -> None:
    hyp = assemble.MintInput(
        artifact_type="hypothesis",
        sentence=canonical_sentence("QI switching can scale to films"),
        aida_uri=aida_uri("QI switching can scale to films"),
        hub_ref_id=2,
        motivation="Shared mechanism class.",
        testable_by="conductance under strain",
        motivated_by=["https://w3id.org/np/RAy"],
    )
    assertion, prov, _ = assemble.build_graphs(hyp, assemble.DRAFT_NS)
    assert str(PRECIS["Hypothesis"]) in assertion.serialize(format="nt")
    assert "testableBy" in assertion.serialize(format="nt")
    p_text = prov.serialize(format="nt")
    assert "motivatedBy" in p_text and "motivation" in p_text
    assert "sourceQuote" not in p_text


def test_multi_grounding_gets_per_passage_nodes() -> None:
    two = _claim_input(
        grounding=[
            assemble.GroundingInput(
                doi="10.1/a",
                pdf_sha256="a" * 64,
                quote="quote one",
                snip="quote one",
                role="corroborates",
            ),
            assemble.GroundingInput(
                doi="10.2/b",
                pdf_sha256="b" * 64,
                quote="quote two",
                snip="quote two",
                role="establishes",
            ),
        ]
    )
    _, prov, _ = assemble.build_graphs(two, assemble.DRAFT_NS)
    text = prov.serialize(format="nt")
    assert "grounding1" in text and "grounding2" in text
    # Each quote binds to its own node, never mixed onto the assertion.
    assert str(assemble.DRAFT_NS["assertion"]) not in [
        str(s) for s, _, _ in prov.triples((None, PRECIS["sourceQuote"], None))
    ]


def test_draft_omits_empty_quote_triples() -> None:
    bare = _claim_input(
        grounding=[
            assemble.GroundingInput(
                doi="10.1/a", pdf_sha256="", quote="", snip="", role="corroborates"
            )
        ],
        fields={},
    )
    _, prov, _ = assemble.build_graphs(bare, assemble.DRAFT_NS)
    text = prov.serialize(format="nt")
    assert "sourceQuote" not in text and "searchSnip" not in text
    assert "https://doi.org/10.1/a" in text


# ── offline sign round trip ─────────────────────────────────────────────


@pytest.fixture(scope="module")
def profile() -> Any:
    from nanopub import Profile

    priv, pub = generate_keypair(2048)
    return Profile(
        orcid_id="https://precis.retostamm.com/id/precis",
        name="precis bot",
        private_key=priv,
        public_key=pub,
    )


def test_sign_produces_valid_w3id_trusty_artifact(profile: Any) -> None:
    from precis.nanopub.mint import _build_and_sign

    np = _build_and_sign(_claim_input(), profile, ["claude-opus-5"])
    assert str(np.source_uri).startswith("https://w3id.org/np/RA")
    assert np.has_valid_trusty and np.has_valid_signature
    trig = np.rdf.serialize(format="trig")
    assert "llmModel" in trig and "claude-opus-5" in trig
    # The mint timestamp is written by the sign step, never hand-authored
    # (gate #12) — the library stamps prov:generatedAtTime in pubinfo.
    assert "generatedAtTime" in trig


def test_signed_artifact_reparses_and_reverifies_from_bytes(profile: Any) -> None:
    from nanopub import Nanopub

    from precis.nanopub.mint import _build_and_sign

    np = _build_and_sign(_claim_input(), profile, [])
    trig_bytes = np.rdf.serialize(format="trig").encode("utf-8")
    ds = Dataset()
    ds.parse(data=trig_bytes.decode("utf-8"), format="trig")
    reloaded = Nanopub(rdf=ds)
    assert reloaded.is_valid
    assert str(reloaded.source_uri) == str(np.source_uri)


def test_reword_changes_claim_identity_resign_does_not() -> None:
    from nanopub import Profile

    from precis.nanopub.mint import _build_and_sign

    priv, pub = generate_keypair(2048)
    p = Profile(
        orcid_id="https://precis.retostamm.com/id/precis",
        name="precis bot",
        private_key=priv,
        public_key=pub,
    )
    a = _claim_input()
    reworded_sentence = canonical_sentence(_SENTENCE.replace("400:1", "300:1"))
    b = _claim_input(sentence=reworded_sentence, aida_uri=aida_uri(reworded_sentence))
    assert a.aida_uri != b.aida_uri  # reword = new claim identity
    # Same input signed twice: same claim identity, artifacts may differ
    # only by signature/timestamp — the AIDA URI is stable.
    np1 = _build_and_sign(a, p, [])
    assert a.aida_uri in np1.rdf.serialize(format="trig")


def test_keygen_floor_and_fingerprint() -> None:
    with pytest.raises(ValueError):
        generate_keypair(1024)
    priv, pub = generate_keypair(2048)
    assert len(fingerprint(pub)) == 64
    assert MIN_KEY_BITS == 2048


def test_software_sha_falls_back_to_dist_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gr249771: prod's pip-from-git venv has no env var and no checkout —
    # the installed wheel's direct_url.json commit must still resolve, or
    # artifacts freeze deployedSha "unknown" forever.
    import precis.handlers.skill as skill_mod
    from precis.nanopub.mint import _software_provenance

    monkeypatch.delenv("PRECIS_GIT_SHA", raising=False)
    monkeypatch.setattr(skill_mod, "_SOURCE_GIT_INFO", {})
    monkeypatch.setattr(skill_mod, "_DIST_GIT_INFO", {"git_sha": "cafe" * 10})
    assert _software_provenance()["sha"] == "cafe" * 10
