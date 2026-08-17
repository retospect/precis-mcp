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
  with citation-marker/`<sup>` residue or an empty snip;
- hub titles truncated at 200 chars surface as mid-word approve titles
  (the `refs.title` cap) — prefill should flag when the stored title was
  truncated so the reviewer knows to complete it.

Fix: the prefill builder should resolve doi from `ref_identifiers` and
sha from `refs.pdf_sha256` per passage (they're both already loaded for
the gate preview), and prefer quote candidates that pass the
citation-marker gate and contain the claim's numeric literals when the
claim has any. Title-truncation flag is a one-line `len == 200` hint.
