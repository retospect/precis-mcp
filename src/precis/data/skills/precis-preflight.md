---
id: precis-preflight
title: precis — manuscript preflight (retraction + citation audit)
applies-to: get (kind='provenance'), jobs check-provenance
status: active
---

# precis-preflight — audit a manuscript's citations before release

Run before submission to find citations that are retracted, under
Expression of Concern, have a correction, or point at the wrong paper.

## Audit a manuscript's bibliography
## Check my references for retractions before submitting
## Pre-flight my bib file

```bash
# 1. Extract every cited DOI from the bib
grep -oE '10\.[0-9]{4,9}/[^"} ,]+' references.bib | sort -u > preflight.txt

# 2. Run the audit
precis jobs check-provenance --refs preflight.txt \
    --view default --out preflight.md
```

Open `preflight.md`. 🔴 must be fixed; 🟠 needs human judgement;
🟡 is usually housekeeping; 🟢 / 🟢 is fine; ⚪ is an unresolvable DOI.

## Read the report
## What the severity emojis mean
## How do I interpret preflight output?

```text
Provenance check — 250 DOIs · 2026-05-30

3/250 resolved · 🔴 1 · 🟠 3 · 🟡 12 · ⚪ unknown: 2

-- 🔴 Blocker (1) --

#47 · `10.1234/foo` — Smith 2019
  _The retracted paper title_
  - 🔴 retraction · 2022-08-14 · notice DOI: `10.1234/foo-r1`
    - Reasons: +Falsification/Fabrication of Data; +Misconduct

-- 🟠 Review (3) --

#119 · `10.9999/baz` — Lee 2022
  - 🔴 cites retracted `10.x/contested-source`
```

- 🔴 **Blocker** — retracted. Drop the citation or replace the argument.
- 🟠 **Review** — Expression of Concern, cites retracted work, or
  metadata mismatch. Human judgement.
- 🟡 **Correction** — corrigendum / erratum. Skim; mostly housekeeping.
- 🟢 **Info** — clean.
- ⚪ **Unknown DOI** — Crossref 404. Likely typo or hallucinated DOI.
- ⚪ **Malformed DOI** — doesn't match `10.<digits>/<suffix>`.

`#N` is the 1-based line in `preflight.txt` — grep your bib for the
matching DOI to locate the citation.

## Catch the "right DOI, wrong paper" case
## Verify that each DOI's metadata matches what I cite
## How do I confirm the DOIs resolve to the papers I think they do?

Requires structured input (DOI + title + authors + year):

```python
get(kind='provenance', view='verify', q='''
[{"doi": "10.1234/foo", "title": "Quantum widgets in surface codes",
  "authors": ["Smith"], "year": 2019},
 {"doi": "10.5678/bar", "title": "...", "authors": ["Doe"], "year": 2023}]
''')
```

Flags any DOI whose Crossref title or first author doesn't match
your supplied bibliographic data. Handles diacritics and German
phonetic alternates (`Müller` ↔ `Mueller`).

## Check whether my citations themselves cite retracted work
## Run a transitive cite-walk one level deep
## Audit depth-1 — do my sources cite bad papers?

```bash
precis jobs check-provenance --refs preflight.txt --transitive depth=1
```

```python
get(kind='provenance', q='10.x/a, 10.x/b, ...', transitive=True)
```

Clean-itself papers that cite retracted sources get promoted to 🟠.

## Get candidate suggestions for unknown DOIs

```python
get(kind='provenance',
    view='verify',
    q='[{"doi": "10.1234/typo", "title": "..."}]',
    suggest_candidates=True)
```

The 404'd DOI stays `unknown` — no auto-substitution. The report lists
possible Crossref matches; verify and replace by hand.

## Keep retraction reasons fresh

```bash
precis jobs sync-retraction-watch --mailto you@example.org
```

Run once monthly. Surfaces *why* a paper was retracted, not just *that*
it was.

## Decide whether "cites retracted work" matters

The citing paper might cite the retracted source for unrelated
background, alongside alternatives, or while explicitly noting the
retraction — none load-bearing. Or the retracted claim might be
foundational. Read the citing paper's use of the source (usually one
paragraph) and decide.

## Pre-release sequence

1. `sync-retraction-watch` (monthly).
2. Extract DOIs from the bib.
3. Run `check-provenance` with `--transitive depth=1`.
4. Fix 🔴 first — non-negotiable.
5. Read 🟠; decide per-citation.
6. Skim 🟡 for substantive corrections.
7. Resolve ⚪ — typos or hallucinated DOIs.
8. Re-run until clean.

## See also

```python
get(kind='skill', id='precis-provenance-help')   # full provenance kind docs
get(kind='skill', id='precis-paper-help')        # ingest notice DOIs as papers
get(kind='skill', id='precis-doi-resolution')    # DOI canonicalisation rules
get(kind='skill', id='precis-citation-help')     # verifier workflow for writing
```
