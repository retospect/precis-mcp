"""Build the assertion / provenance / pubinfo graphs for one artifact.

Shapes follow the Reto-signed wargame artifacts
(``docs/reference/nanopub-example/``) with the spec's later corrections
applied:

* **Assertion = the world-claim, nothing else.** Attribution (author,
  venue, year) lives in provenance — the nanopub convention, and what
  AIDA convergence requires: "Han et al. demonstrated X" and "X" must
  not mint two identities for one fact. The claim node carries its
  canonical AIDA URI (``precis:aidaUri``) so the content address is
  inside the signed artifact.
* **Compound assertions name their atoms by AIDA URI**
  (``<atom-aida> precis:conjunctOf sub:claim`` — semantic,
  supersede-stable); provenance ``prov:wasDerivedFrom`` the atom
  nanopubs' **trusty** URIs, hash-chaining the merge to the atoms' exact
  content. A compound cites no paper directly.
* **Hypotheses carry motivation, never evidence**: ``precis:motivation``
  prose + ``precis:motivatedBy``/``prov:wasDerivedFrom`` trusty URIs,
  ``precis:testableBy`` the discriminating experiment; no quote, no
  ``evidenceRole`` (schema-linted in :mod:`.gates`).
* **License scoped to the assertion graph** (gate #10): CC-BY over our
  triples; verbatim quotes remain © their publishers (a
  ``precis:licenseNote`` triple says so in the artifact itself).
* **Universal anchors only**: provenance carries DOI + ``pdf_sha256`` +
  verbatim quote + normalized snip. Chunk ids and ref ids never appear;
  pubinfo's ``precis:mintedFromHub`` is opaque production metadata, not
  evidence citation.

The builder is pure over :class:`MintInput` — no store access — so the
draft view and the mint path assemble identically; only the input
source differs (live bundle vs frozen publish row).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rdflib import RDF, RDFS, XSD, Graph, Literal, Namespace, URIRef

from precis.nanopub.vocab import (
    ATOMIC_CLAIM,
    BOT_AGENT,
    CC_BY,
    CITO,
    COMPOUND_CLAIM,
    DCT,
    HYPOTHESIS,
    PRECIS,
    PROV,
)

#: Placeholder namespace for the unsigned draft rendering (slice 1).
#: Mint uses the nanopub library's dummy namespace instead, which the
#: signing step rewrites to the final w3id trusty URI.
DRAFT_NS = Namespace("https://w3id.org/np/DRAFT#")


@dataclass(frozen=True, slots=True)
class GroundingInput:
    """One frozen grounding passage (publish-boundary form: universal
    anchors only — the chunk id stays in the publish row)."""

    doi: str
    pdf_sha256: str
    quote: str
    snip: str
    #: 'establishes' | 'corroborates' — a live `contradicts` never mints.
    role: str = "corroborates"
    source_title: str | None = None


@dataclass(frozen=True, slots=True)
class ConjunctInput:
    """One atom of a compound: its AIDA URI (assertion edge) and its
    minted artifact's trusty URI (provenance hash-chain)."""

    aida_uri: str
    trusty_uri: str | None


@dataclass(frozen=True, slots=True)
class MintInput:
    """Everything one artifact says, decoupled from the DB."""

    artifact_type: str  # 'claim' | 'compound' | 'hypothesis'
    sentence: str  # canonical form (aida.canonical_sentence)
    aida_uri: str
    hub_ref_id: int
    grounding: list[GroundingInput] = field(default_factory=list)
    #: Structured fields; values must be contained in a grounding quote
    #: (gate #5). `quantity` requires `quantity_bound`.
    fields: dict[str, str] = field(default_factory=dict)
    conjuncts: list[ConjunctInput] = field(default_factory=list)
    #: Hypothesis-only.
    motivation: str | None = None
    testable_by: str | None = None
    motivated_by: list[str] = field(default_factory=list)  # trusty URIs
    #: Software provenance (structured, resolved live at mint):
    #: {"name": ..., "version": ..., "sha": ..., "llm_models": [...]}.
    software: dict[str, Any] = field(default_factory=dict)


def build_graphs(inp: MintInput, ns: Namespace) -> tuple[Graph, Graph, Graph]:
    """The (assertion, provenance, pubinfo) graphs under ``ns``."""
    return (
        _assertion(inp, ns),
        _provenance(inp, ns),
        _pubinfo(inp, ns),
    )


def _type_uri(artifact_type: str) -> URIRef:
    return {
        "claim": ATOMIC_CLAIM,
        "compound": COMPOUND_CLAIM,
        "hypothesis": HYPOTHESIS,
    }[artifact_type]


def _assertion(inp: MintInput, ns: Namespace) -> Graph:
    g = Graph()
    claim = ns["claim"]
    g.add((claim, RDF.type, _type_uri(inp.artifact_type)))
    g.add((claim, RDFS.label, Literal(inp.sentence, lang="en")))
    g.add((claim, PRECIS["aidaUri"], URIRef(inp.aida_uri)))
    for key in ("material", "method", "quantity"):
        if inp.fields.get(key):
            g.add((claim, PRECIS[key], Literal(inp.fields[key])))
    if inp.fields.get("quantity_bound"):
        g.add((claim, PRECIS["quantityBound"], Literal(inp.fields["quantity_bound"])))
    for conj in inp.conjuncts:
        g.add((URIRef(conj.aida_uri), PRECIS["conjunctOf"], claim))
    if inp.artifact_type == "hypothesis" and inp.testable_by:
        g.add((claim, PRECIS["testableBy"], Literal(inp.testable_by, lang="en")))
    return g


def _provenance(inp: MintInput, ns: Namespace) -> Graph:
    g = Graph()
    assertion = ns["assertion"]

    if inp.artifact_type == "hypothesis":
        for trusty in inp.motivated_by:
            g.add((assertion, PROV.wasDerivedFrom, URIRef(trusty)))
            g.add((assertion, PRECIS["motivatedBy"], URIRef(trusty)))
        if inp.motivation:
            g.add((assertion, PRECIS["motivation"], Literal(inp.motivation, lang="en")))
        return g

    if inp.artifact_type == "compound":
        # Hash-chain to the atoms' exact content; no paper is cited —
        # the compound's trust derives worst-of-atoms, never serialized.
        for conj in inp.conjuncts:
            if conj.trusty_uri:
                g.add((assertion, PROV.wasDerivedFrom, URIRef(conj.trusty_uri)))
        return g

    for i, ground in enumerate(inp.grounding, start=1):
        doi_uri = URIRef(f"https://doi.org/{ground.doi}")
        g.add((assertion, PROV.wasDerivedFrom, doi_uri))
        # One node per passage so multi-grounding never mixes quotes and
        # shas; with a single passage this collapses to the wargame's
        # flat shape on the assertion node itself.
        node = assertion if len(inp.grounding) == 1 else ns[f"grounding{i}"]
        if node is not assertion:
            g.add((node, RDF.type, PRECIS["Grounding"]))
            g.add((assertion, PRECIS["groundedBy"], node))
            g.add((node, PRECIS["fromSource"], doi_uri))
        g.add((node, PRECIS["evidenceRole"], PRECIS[ground.role]))
        # Draft renderings may not have re-grounded quotes yet; an empty
        # value is omitted rather than serialized as "" (the mint gates
        # make absence fatal at sign time, not here).
        if ground.quote:
            g.add((node, PRECIS["sourceQuote"], Literal(ground.quote, lang="en")))
            g.add((node, CITO["includesQuotationFrom"], doi_uri))
        if ground.snip:
            g.add((node, PRECIS["searchSnip"], Literal(ground.snip)))
        if ground.pdf_sha256:
            g.add((node, PRECIS["sourcePdfSha256"], Literal(ground.pdf_sha256)))
        if ground.source_title:
            g.add((doi_uri, DCT.title, Literal(ground.source_title)))
    return g


def _pubinfo(inp: MintInput, ns: Namespace) -> Graph:
    g = Graph()
    this = ns[""]
    assertion = ns["assertion"]
    # License scoped to the assertion graph, never over quote bytes.
    g.add((assertion, DCT.license, URIRef(CC_BY)))
    if inp.grounding:
        g.add(
            (
                assertion,
                PRECIS["licenseNote"],
                Literal(
                    "CC-BY covers this nanopublication's triples; verbatim "
                    "sourceQuote text remains copyright its publisher "
                    "(fair-use quotation)."
                ),
            )
        )
    g.add(
        (
            this,
            PRECIS["mintedFromHub"],
            URIRef(f"https://precis.retostamm.com/ref/fi{inp.hub_ref_id}"),
        )
    )
    g.add((BOT_AGENT, RDF.type, PROV.SoftwareAgent))
    g.add((BOT_AGENT, RDFS.label, Literal("precis (non-attesting bot identity)")))
    if inp.software:
        sw = ns["software"]
        g.add((this, PRECIS["composedBy"], sw))
        if inp.software.get("name"):
            g.add((sw, RDFS.label, Literal(str(inp.software["name"]))))
        if inp.software.get("version"):
            g.add((sw, PRECIS["version"], Literal(str(inp.software["version"]))))
        if inp.software.get("sha"):
            g.add((sw, PRECIS["deployedSha"], Literal(str(inp.software["sha"]))))
        for model_id in inp.software.get("llm_models", []):
            g.add((sw, PRECIS["llmModel"], Literal(str(model_id))))
    return g


def draft_trig(inp: MintInput) -> str:
    """Slice-1 rendering: the four graphs as unsigned TriG under the
    ``DRAFT#`` placeholder namespace. Draft comments are fine here —
    comments are lexical syntax outside the integrity envelope, and this
    output is never hashed; mint re-assembles from frozen inputs via the
    nanopub library instead of reusing these bytes."""
    from rdflib import Dataset

    from precis.nanopub.vocab import NP

    ns = DRAFT_NS
    assertion_g, prov_g, pub_g = build_graphs(inp, ns)

    ds = Dataset()
    head = ds.graph(URIRef(ns["Head"]))
    this = URIRef(str(ns)[:-1])  # strip the '#'
    head.add((this, RDF.type, NP.Nanopublication))
    head.add((this, NP.hasAssertion, URIRef(ns["assertion"])))
    head.add((this, NP.hasProvenance, URIRef(ns["provenance"])))
    head.add((this, NP.hasPublicationInfo, URIRef(ns["pubinfo"])))
    for name, graph in (
        ("assertion", assertion_g),
        ("provenance", prov_g),
        ("pubinfo", pub_g),
    ):
        target = ds.graph(URIRef(ns[name]))
        for triple in graph:
            target.add(triple)

    for prefix, namespace in (
        ("np", NP),
        ("prov", PROV),
        ("dct", DCT),
        ("cito", CITO),
        ("precis", PRECIS),
        ("sub", ns),
        ("xsd", XSD),
    ):
        ds.bind(prefix, namespace, override=True)
    body = ds.serialize(format="trig")
    header = (
        "# DRAFT — unsigned nanopub rendering (view='nanopub').\n"
        "# Placeholder URI; trusty URI, signature and dct:created are\n"
        "# minted by the sign step. Comments are stripped at mint.\n"
    )
    return header + body
