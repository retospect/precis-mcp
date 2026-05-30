---
id: precis-provenance-help
title: precis — retraction & amendment checks
status: shipped
tier: 1
floor: any
applies-to: get (kind='provenance')
last-updated: 2026-05-30
---

# precis-provenance-help — has this paper been retracted?

`provenance` checks a paper's publication health via Crossref:
retractions, expressions of concern, corrections, and amendments.
Use it before citing or shipping anything that references a DOI.

```python
get(kind='provenance', id='10.1038/nature05095')
get(kind='provenance', id='doi:10.1038/s41586-021-03819-2')
get(kind='provenance', id='https://doi.org/10.1126/science.1141204')
```

`id=` and `q=` are equivalent. URL forms and `doi:` prefixes are
canonicalised (lowercased, prefix stripped) before the lookup.

## What you get back

A markdown report grouped by severity:

- 🔴 **Blocker** — paper is retracted / withdrawn / removed. Do not
  cite without addressing the retraction.
- 🟠 **Review** — Expression of Concern issued. Paper is under
  investigation but has not been retracted. Re-read; check whether
  your argument depends on the contested claim.
- 🟡 **Correction** — corrigendum / erratum / addendum. Often
  housekeeping (affiliation, typo) but occasionally substantive.
- 🟢 **Info** — clean, or an informational notice (clarification,
  new version). No action required.

If the DOI is **malformed** or **unknown to Crossref** the report
says so explicitly — likely a hallucinated or mistyped DOI. Don't
silently ship a citation that failed validation.

## What gets persisted

If the parent paper is already in your local store, calling
`provenance` writes through:

- Notice DOIs are auto-ingested as their own `paper` refs (for
  retractions and EoCs only — corrigenda are too common to ingest
  by default). Notice refs are tagged `STATUS:notice` so search
  surfaces can distinguish them.
- A `retracted-by` / `corrected-by` / `concern-raised-by` link
  attaches the parent paper to each notice.
- `refs.retraction_status` is set to the dominant status
  (`retracted` > `expression_of_concern` > `corrected`).
- A `STATUS:retracted` / `STATUS:concern` / `STATUS:corrected` tag
  is applied to the parent.

You can then `get(kind='paper', id=<notice-slug>)` to read the
notice, or navigate from the local paper via `link(...)`.

If the parent paper is **not** in the local store, the report is
informational only — no writes happen. Ingest the paper first
(`precis add <pdf>` or `precis_add(...)`) to capture the retraction
graph locally.

## DOI verification

The handler validates the DOI shape before making any HTTP call:

- `10.<registrant>/<suffix>` where registrant is 4-9 digits
- Strips `doi:` / `https://doi.org/` / `https://dx.doi.org/` prefixes
- Lowercases for canonical comparison

A malformed DOI returns `status='malformed'` without hitting Crossref.
A well-formed but non-resolving DOI returns `status='unknown'` —
useful for catching hallucinated citations (LLMs frequently produce
plausible-looking DOIs that don't exist).

## Limits in Phase 1

Single DOI per call. Batch (`q='doi1,doi2,...'`) lands in Phase 2.
Retraction Watch reason taxonomy (the "why" behind a retraction)
lands in Phase 3 — until then the report shows the Crossref
`update_type` ("retraction" / "expression_of_concern" / …) but no
human-readable reason. Transitive cite-walk ("does this paper cite
something retracted?") is Phase 4. Fuzzy resolution from
bibliographic hints (when the supplied DOI 404s) is Phase 5.

## Why use Crossref

Since December 2023 Crossref is the consolidated source of retraction
data — they aggregate publisher-reported notices via their `update-to`
relation and distribute the Retraction Watch Database under CC-BY
through Crossref Labs. One endpoint, one lookup; no separate
Retraction Watch API to hit.

## See also

- `precis-overview` — verbs and kinds
- `precis-paper-help` — paper ingest workflow
- `precis-doi-resolution` — DOI canonicalisation rules
