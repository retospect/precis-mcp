---
id: precis-provenance-help
title: precis — retraction and amendment checks for DOIs
applies-to: get (kind='provenance')
status: active
---

# precis-provenance-help — has this paper been retracted?

`provenance` checks publication health for one or many DOIs:
retractions, expressions of concern, corrections, and what each
cited paper says. Run it before citing or shipping anything that
references a DOI.

## Check whether a DOI has been retracted
## Is this paper safe to cite?
## Preflight a single DOI before quoting it

```python
get(kind='provenance', id='10.1038/nature05095')
```

URL forms and `doi:` prefixes are canonicalised before lookup.
`id=` and `q=` are interchangeable for the single-DOI case.

## Check a batch of DOIs at once
## Preflight a manuscript's reference list
## Run provenance over many DOIs in one call

```python
get(kind='provenance', q='10.1038/nature05095, 10.5678/foo, 10.9999/bar')
```

Comma, whitespace, or newline-separated. Every result carries a
1-based `input_index` (`#3`, `#47`) that's stable across views, so
follow-up actions can reference the same entry regardless of
which severity bucket it landed in.

## Read the severity report

```text
🔴 Blocker    — retracted / withdrawn / removed; don't cite without addressing
🟠 Review     — Expression of Concern, cites a retracted work, or metadata mismatch
🟡 Correction — corrigendum / erratum / addendum; usually housekeeping
🟢 Info       — clean, or informational notice (clarification, new version)
```

Malformed and unknown DOIs surface in their own sections — likely
hallucinated or mistyped. The tool never silently substitutes.

## Pick a view for the output

```python
get(kind='provenance', q='...')                       # full triaged markdown
get(kind='provenance', q='...', view='blockers')      # only 🔴 + 🟠
get(kind='provenance', q='...', view='json')          # structured payload
get(kind='provenance', q='...', view='exists')        # compact ✓/✗ per DOI
get(kind='provenance', view='verify', q='<bib>')      # adds metadata-mismatch section
```

`view='blockers'` shows a count of hidden 🟡/🟢 entries at the
bottom. `view='json'` carries every field including raw scores and
`input_index`.

## Verify that a DOI matches its bib entry
## Catch DOIs pointing to the wrong paper
## My LLM-generated bibliography has DOIs — are they right?

```python
get(kind='provenance', view='verify', q='''
  [{"doi": "10.1038/nature05095", "title": "...",
    "authors": ["Hwang"], "year": 2005},
   {"doi": "10.1234/foo",         "title": "Quantum widgets",
    "authors": ["Smith"],  "year": 2019}]
''')
```

A real DOI pointing to a *different* paper than the bib claims is
a worse failure than a 404. `view='verify'` adds a **⚠️ Metadata
mismatch** section between Blockers and Review with per-field
diff: title (fuzzy match), first-author surname, year ±1.

## Surface candidate DOIs when one 404s

```python
get(kind='provenance', view='verify', suggest_candidates=True,
    q='[{"doi": "10.x/typo", "title": "...", "authors": [...], "year": 2019}]')
```

On a 404 DOI with bib hints, shows Crossref candidate matches as
advisory only — the supplied DOI's status stays `unknown`.

## Chase retractions one level deep

```python
get(kind='provenance', q='...', transitive=True)
```

For each parent, also checks its references. A clean paper that
cites a retracted source is promoted into the 🟠 Review bucket.

## What gets written to the store

When the parent paper is locally ingested, calling `provenance`
writes through:

- Retraction and EoC notices ingest as their own `paper` refs,
  tagged `STATUS:notice`. Corrigenda don't auto-ingest.
- A `retracted-by` / `corrected-by` / `concern-raised-by` link
  attaches the parent to each notice.
- `STATUS:retracted` / `STATUS:concern` / `STATUS:corrected` tag
  applied to the parent.

After this, `get(kind='paper', id='<notice-slug>')` reads the
notice text.

If the parent isn't in the store, the report is informational —
nothing is written. Ingest first to capture the retraction graph.

## Read the reason a paper was retracted

When the local Retraction Watch cache is populated, notice lines
gain a reason sub-bullet:

```text
- 🔴 Retraction (RW) · notice DOI: 10.1126/science.1124926
  - Reasons: Falsification/Fabrication of Data; Investigation by
    Company/Institution; Misconduct - Official Investigation(s)
    and/or Finding(s); Misconduct by Author
```

`(RW)` means the notice came from the Retraction Watch cache. If
the cache is empty, ask the user to run
`precis jobs sync-retraction-watch --mailto <addr>` (monthly cron
is the intended cadence). Crossref-only notices still appear,
without the reasons sub-bullet.

## When the two sources disagree

Crossref carries the *fact* of a retraction; RW carries the *why*
plus pre-CrossMark notices Crossref never received (the Hwang
stem-cell paper is the canonical case — retracted 2006, Crossref
still reports it clean). When both have data for the same notice
DOI, Crossref's `update_type`/date drives the line and RW's
reasons drive the sub-bullet. Dedup is by `notice_doi`.

If Crossref times out but RW cache has data, the report still
goes through with a `⚠️ Crossref unavailable` banner.

## Preflight a whole manuscript from the CLI

```bash
precis jobs check-provenance --refs preflight.txt --out preflight.md
precis jobs check-provenance --refs preflight.txt --view blockers
precis jobs check-provenance --refs preflight.txt --view json --out preflight.json
```

DOI-per-line input file. See `precis-preflight` for the full
manuscript-release recipe.

## See also

```python
get(kind='skill', id='precis-preflight')          # manuscript-release recipe
get(kind='skill', id='precis-paper-help')         # ingest a paper so writes-through take effect
get(kind='skill', id='precis-doi-resolution')     # DOI canonicalisation rules
get(kind='skill', id='precis-overview')           # verbs and kinds
```
