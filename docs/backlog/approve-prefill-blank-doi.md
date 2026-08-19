---
status: draft
title: nanopub approve-form prefill leaves doi (and sometimes sha) blank despite the paper having both
model: sonnet
---

# Approve prefill omits the DOI it already knows

Live pattern (2026-08-17): during the 13-hub batch review of the nanobud
draft's claim hubs, the hub page's prefilled approve payload had
`"doi": ""` on essentially every passage — for papers whose DOI is
present in `ref_identifiers` (`id_kind='doi'`) and whose
`refs.pdf_sha256` is set. Every human/agent reviewer has to re-look up
and paste in a value the system already has, and a reviewer who trusts
the prefill submits a payload that fails the `[grounding]` DOI gate.

Also seen in the same sweep (same prefill code path, lower frequency):

- prefill picks tangential quotes (figure captions, table header rows)
  over the load-bearing sentence in the same chunk, and can emit a quote
  with citation-marker/`<sup>` residue or an empty snip.

(Mid-word truncated approve titles — the old `[:200]` `refs.title` cap —
are gone: 9d0b9206 dropped the cap and syncs the full hub title on
approve, so this sub-item no longer applies.)

Fix: the prefill builder should resolve doi from `ref_identifiers` and
sha from `refs.pdf_sha256` per passage (they're both already loaded for
the gate preview), and prefer quote candidates that pass the
citation-marker gate and contain the claim's numeric literals when the
claim has any.

Update 2026-08-19 — **code site pinned, and the blast radius is the whole
corpus.** `nanopub/evidence.py::_source` builds every `EvidenceSource`
with `doi=(ref.meta or {}).get("doi")` — meta only, no `ref_identifiers`
fallback. `pdf_sha_rows` in the same module already does the right thing
(it unions `refs.pdf_sha256` with `ref_identifiers WHERE
id_kind='pdf_sha256'`), so the two anchors are read inconsistently.

Prod distribution (2026-08-19, 30655 papers): **3** have
`meta->>'doi'`; **27846** have a `ref_identifiers` row with
`id_kind='doi'`; **27843** have the identifier row and no meta value; **0**
have meta without the identifier row. So the meta lookup misses ~99.99% of
the DOI corpus — spot-checked NULL on pa1222 / pa2713 / pa2823 / pa2862 /
pa42501, all of which do hold a DOI. DOI is a `ref_identifiers`
phenomenon; `meta->>'doi'` is vestigial.

Consequence beyond the prefill: the assembled provenance graph carries no
DOI, so a correctly-grounded hub fails the `[grounding]` DOI mint gate.
Give `_source`'s `doi=` the same `ref_identifiers` fallback
`pdf_sha_rows` uses, and grep for other `meta->>'doi'` / `meta.get("doi")`
readers while in there.
