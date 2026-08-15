"""Namespaces + agent identities for the published graphs.

Vocabulary is the spec's corrected-against-observed-usage table: the
claim is an ``aida:`` sentence URI, evidence edges are ``cito:``, quote
grounding is ``cito:hasQuotedText``-family, provenance is PROV-O, and
``npx:`` carries sign/retract/supersede. Local ``precis:`` predicates
(decomposition, structured fields, hypothesis machinery) live at a
namespace we own for decades — never a rented platform URL. The
``precis:`` namespace is resolvable as a static page (courtesy, not a
verification dependency).

Agent identities (spec "Agent strings"): the bot signs as
``https://precis.retostamm.com/id/precis`` (that URI doubles as the
out-of-band fingerprint page); the human attesting identity is an ORCID
URI, pinned by the allowlist. Software provenance is structured triples
(name, version, deployed sha, LLM model ids), not a prose label.
"""

from __future__ import annotations

from rdflib import Namespace

# Community namespaces.
NP = Namespace("http://www.nanopub.org/nschema#")
NPX = Namespace("http://purl.org/nanopub/x/")
PROV = Namespace("http://www.w3.org/ns/prov#")
DCT = Namespace("http://purl.org/dc/terms/")
CITO = Namespace("http://purl.org/spar/cito/")
AIDA = Namespace("http://purl.org/aida/")

# Ours. Signing identities are names owned for decades — never a rented
# platform namespace (spec: Agent strings, decided 2026-08-14).
PRECIS = Namespace("https://precis.retostamm.com/vocab#")
AGENT = Namespace("https://precis.retostamm.com/id/")

#: The non-attesting bot identity. A bot signature alone authorizes
#: nothing — publication always requires the human attesting key.
BOT_AGENT = AGENT["precis"]

#: The license our triples carry. Scoped to the assertion graph —
#: verbatim ``sourceQuote`` text remains © its publisher (fair-use
#: quotation); never assert CC-BY over the quote bytes.
CC_BY = "https://creativecommons.org/licenses/by/4.0/"

#: Artifact types (rdf:type of the claim node in the assertion graph).
ATOMIC_CLAIM = PRECIS["AtomicClaim"]
COMPOUND_CLAIM = PRECIS["CompoundClaim"]
HYPOTHESIS = PRECIS["Hypothesis"]

#: Quantity bound semantics (review feedback 2026-08-15: a quantity claim
#: carries whether its figure is exact, an upper bound, a lower bound, or
#: an approximate range — "up to 400:1" and "400:1" are different claims).
QUANTITY_BOUNDS = ("exact", "upper", "lower", "approx-range")
