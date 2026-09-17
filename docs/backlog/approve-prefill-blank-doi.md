---
status: draft
title: nanopub approve-form prefill picks tangential quotes over the load-bearing sentence
model: sonnet
---

# Approve prefill quote selection needs work

**DOI part FIXED by 3f3e2130** (2026-08-25): `nanopub/evidence.py::_source`
and `_awaiting_sources` now resolve `doi` from `ref_identifiers`
(`id_kind='doi'`) first, falling back to the legacy `refs.meta['doi']` —
covered by
`tests/precis_web/test_nanopub_routes.py::test_prefill_doi_comes_from_ref_identifiers`.
The blank-DOI-blocks-the-mint-gate failure described below no longer
applies; what's left open is the quote-selection sub-issue.

**Empty-snip part FIXED by b97e492f** (2026-09-17):
`nanopub_render.py::_suggest_quote_snip` now tries ranked sentence
candidates in turn and returns the first with a unique 8-token snip
across the paper's live body chunks (overlapping chunk seams could put
the top-ranked sentence verbatim in two chunks, making every window
non-unique) — covered by `tests/precis_web/test_nanopub_render_units.py`.
Only when no candidate has a unique window does it still fall back to an
empty snip.

Still open, from the same 2026-08-17 batch review of the nanobud draft's
claim hubs (same prefill code path as the now-fixed DOI bug, lower
frequency): the ranking itself is unchanged, so prefill can still pick a
tangential quote (figure caption, table header row) over the load-bearing
sentence in the same chunk, and a quote with citation-marker/`<sup>`
residue is still possible when no sentence-level candidate qualifies and
it falls back to the whole chunk text.

(Mid-word truncated approve titles — the old `[:200]` `refs.title` cap —
are gone: 9d0b9206 dropped the cap and syncs the full hub title on
approve, so this sub-item no longer applies.)

Fix: prefer quote candidates that pass the citation-marker gate and
contain the claim's numeric literals when the claim has any.
