---
status: draft
title: nanopub grounding for DOI-less books (ISBN) — the Callister class
model: opus
---

# Book/ISBN grounding for nanopub passages

The mint `grounding` gate hard-requires a DOI per passage ("provenance
content is DOI + quote + snip; patent grounding is an open item").
Books bite the same way patents do: fi19981's best mid-span
corroboration is Callister, *Fundamentals of Materials Science and
Engineering* 3e (2008, Wiley, ISBN 978-0-470-12537-3) — "over one
billion transistors … doubles about every 18 months" — a textbook with
no DOI, so its passage can't ride the artifact even though the edge,
quote, snip and pdf sha all check out.

Design questions (same family as patent grounding — solve together):

- Identifier: ISBN-13 in the provenance graph where the DOI would go
  (`urn:isbn:` is a registered URN namespace, so the RDF stays a
  resolvable URI). Edition matters — the sha pins the copy, the ISBN
  names the edition.
- Gate: accept exactly one of doi | isbn | patent-no per passage;
  everything else (quote verbatim, snip uniqueness, pdf-sha pin,
  hearsay checks) is identifier-agnostic already.
- Payload/prefill: `_suggested_payload` emits an `isbn` field when the
  ref has one and no DOI; approve form passes it through.

Until then: DOI-less book passages stay OUT of minted payloads; keep
the corroborates edge (visible internally) and note the passage in the
hub for the human reader.
