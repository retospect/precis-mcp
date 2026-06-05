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

`provenance` checks publication health for one or many DOIs via
Crossref + the Retraction Watch dataset: retractions, expressions
of concern, corrections, and what each cited paper says. Use it
before citing or shipping anything that references a DOI.

```python
# Single DOI
get(kind='provenance', id='10.1038/nature05095')

# Batch — comma- or whitespace- or newline-separated
get(kind='provenance', q='10.1038/nature05095, 10.5678/foo, 10.9999/bar')

# Bibliography-shaped input (enables metadata verification)
get(kind='provenance', view='verify', q='''
  [{"doi": "10.1038/nature05095", "title": "...", "authors": ["Hwang"], "year": 2005},
   {"doi": "10.5678/foo",         "title": "...", "authors": ["Smith"],  "year": 2019}]
''')
```

`id=` and `q=` are interchangeable. URL forms and `doi:` prefixes
are canonicalised before lookup.

## Severity tiers

A markdown report grouped by severity:

- 🔴 **Blocker** — paper retracted / withdrawn / removed. Don't cite
  without addressing the retraction.
- 🟠 **Review** — Expression of Concern issued, paper cites a
  retracted work, OR metadata mismatch detected. Re-read; check
  whether your claim depends on the contested bit.
- 🟡 **Correction** — corrigendum / erratum / addendum. Often
  housekeeping (affiliation, typo) but occasionally substantive.
- 🟢 **Info** — clean paper, or informational notice (clarification,
  new version). No action required.

Malformed DOIs and unknown DOIs surface in their own sections —
likely hallucinated or mistyped. The tool never silently substitutes.

## Views

| view       | what it gives you |
|------------|-------------------|
| (default)  | full triaged markdown report |
| `blockers` | only 🔴 + 🟠 — the must-act list; a count of hidden 🟡/🟢 entries appears |
| `json`     | structured payload (every field, raw scores, `input_index` for cross-reference) |
| `verify`   | default + **Metadata mismatch** section comparing supplied bib metadata to Crossref (requires structured input) |
| `exists`   | compact "does this DOI resolve?" check (✓/✗ per DOI; skips notice processing) |

## Optional flags

```python
get(kind='provenance', q='...', transitive=True)
# → For each parent, also check its references (depth 1). Surfaces
#   citations of retracted/EoC work. A clean paper that cites a
#   retracted source is promoted into the 🟠 Review bucket.

get(kind='provenance', q='[{"doi": "10.x/typo", "title": "..."}]',
    view='verify', suggest_candidates=True)
# → On a 404 DOI with bibliographic hints, surface Crossref candidate
#   matches as ADVISORY hints (the supplied DOI's status stays
#   `unknown` — we never auto-substitute).
```

## Numbered output

Every batch result carries a 1-based `input_index` matching its
position in your input list. The report shows `#3`, `#47`, etc.
alongside each entry, regardless of which severity bucket it ended
up in. So `#47` is the same paper in `view='default'`,
`view='blockers'`, and `view='json'` — useful when an LLM is
producing follow-up actions against a numbered preflight report.

## What gets persisted

If the parent paper is in your local store, calling `provenance`
writes through:

- Notice DOIs are auto-ingested as their own `paper` refs (for
  retractions and EoCs only — corrigenda are too common to ingest
  by default). Notice refs are tagged `STATUS:notice`.
- A `retracted-by` / `corrected-by` / `concern-raised-by` link
  attaches the parent paper to each notice.
- `refs.retraction_status` set to the dominant status
  (`retracted` > `expression_of_concern` > `corrected`).
- A `STATUS:retracted` / `STATUS:concern` / `STATUS:corrected` tag
  applied to the parent.

After this, `get(kind='paper', id=<notice-slug>)` reads the notice.

If the paper isn't in the store, the report is informational only —
no writes. Ingest first (`precis add <pdf>`) to capture the
retraction graph locally.

## Retraction Watch reasons

When the local `provenance_rw_cache` is populated (via
`precis jobs sync-retraction-watch`), every notice in the report
gains its reason codes:

```
- 🔴 Retraction (RW) · notice DOI: 10.1126/science.1124926
  - Reasons: Falsification/Fabrication of Data; Investigation by
    Company/Institution; Misconduct - Official Investigation(s) and/or
    Finding(s); Misconduct by Author
```

The `(RW)` label on a notice line means it came from the Retraction
Watch cache rather than Crossref's `update-to` field. Run the sync
monthly via cron.

## When Crossref disagrees with Retraction Watch

The two sources are consulted together. The RW cache contributes:

1. **Reasons** — Crossref carries the *fact* of a retraction; RW
   carries the *why* (the >100 reason codes maintained by the RW
   editors).
2. **Notices Crossref doesn't have** — pre-CrossMark retractions and
   cases where the publisher never deposited an `update-to` relation
   (the Hwang stem-cell paper is the canonical example: retracted by
   Science in 2006, AAAS never backfilled the relation, so Crossref
   alone reports the paper as clean). RW knows about it; we surface
   it here as a synthesised notice with the `(RW)` source label.
3. **Resilience when Crossref is down** — if the Crossref API times
   out or returns a transport error but the local RW cache has data
   for the DOI, the report still goes through. The renderer shows a
   `⚠️ Crossref unavailable` banner so you know the live source
   wasn't consulted, but you get the retraction info either way.

When Crossref has data AND RW has data for the same notice DOI, the
two are merged: Crossref's `update_type`/date drives the line, RW's
reasons drive the "Reasons:" sub-bullet. Dedup is by `notice_doi`.

## Metadata verification (`view='verify'`)

Catches a worse failure than a 404: a real DOI pointing to a
*different paper* than your bib claims. Common with
LLM-generated bibliographies.

```python
get(kind='provenance', view='verify', q='''
  [{"doi": "10.1234/foo", "title": "Quantum widgets",
    "authors": ["Smith"], "year": 2019}]
''')
```

Per-field diff (title via token-set Jaccard with NFKD + German-
phonetic + reverse-phonetic normalisation; first-author via
normalised surname match; year ±1 tolerance) surfaces in a new
**⚠️ Metadata mismatch** section between Blockers and Review.

## DOI verification

Validates shape before any HTTP call:
- `10.<registrant>/<suffix>` with registrant 4-9 digits
- Strips `doi:` / `https://doi.org/` / `https://dx.doi.org/` prefixes

A malformed DOI → `status='malformed'` without a Crossref call. A
well-formed but non-resolving DOI → `status='unknown'`.

## CLI

For the "preflight 250 references before manuscript release" workflow:

```bash
# Run the check against a DOI-per-line file, write a markdown report
precis jobs check-provenance --refs preflight.txt --out preflight.md

# Show only the must-act items
precis jobs check-provenance --refs preflight.txt --view blockers

# JSON for downstream tooling
precis jobs check-provenance --refs preflight.txt --view json --out preflight.json

# Monthly Retraction Watch sync (run via cron)
precis jobs sync-retraction-watch --mailto you@example.org
```

See `precis-preflight` for the full manuscript-release recipe.

## Why use Crossref

Since December 2023 Crossref is the consolidated source of retraction
data — they aggregate publisher-reported notices via their `update-to`
relation and distribute the Retraction Watch Database under CC-BY.
One endpoint, one lookup; no separate Retraction Watch API to hit.

## See also

- `precis-preflight` — manuscript-release recipe
- `precis-overview` — verbs and kinds
- `precis-paper-help` — paper ingest workflow
- `precis-doi-resolution` — DOI canonicalisation rules
