---
id: precis-preflight
title: precis — manuscript preflight (retraction + citation check)
status: shipped
tier: 1
floor: any
applies-to: jobs check-provenance, get (kind='provenance')
last-updated: 2026-05-30
---

# precis-preflight — manuscript citation audit before release

You have a manuscript citing N papers and a week before release. This
skill walks through finding any citation that's been retracted, is
under expression of concern, has a correction, or — worst case — is
the wrong paper entirely (the DOI is real but points elsewhere).

## The 30-second version

```bash
# 1. Pull every cited DOI from your bibtex
grep -oE '10\.[0-9]{4,9}/[^"} ,]+' references.bib | sort -u > preflight.txt

# 2. (optional) Keep RW reasons fresh
precis jobs sync-retraction-watch --mailto you@example.org

# 3. Run the audit
precis jobs check-provenance --refs preflight.txt \
    --view default --out preflight.md
```

Open `preflight.md`. Anything 🔴 has to be addressed before submission;
🟠 needs human judgement; 🟡 is usually housekeeping; 🟢 is fine.

## What the report tells you

```
# Provenance check — 250 DOIs · 2026-05-30

3/250 resolved · 🔴 1 · 🟠 3 · 🟡 12 · ⚪ unknown: 2

## 🔴 Blocker (1)

### #47 · `10.1234/foo` — Smith 2019
_The retracted paper title_
- 🔴 retraction · 2022-08-14 · notice DOI: `10.1234/foo-r1`
  - Reasons: +Falsification/Fabrication of Data; +Misconduct

## 🟠 Review (3)

### #82 · `10.5678/bar` — Doe 2023
_Paper under EoC_
- 🟠 expression_of_concern · 2024-03-10
  - Reasons: +Concerns/Issues With Data

### #119 · `10.9999/baz` — Lee 2022
_A clean-itself paper that cites retracted work_
- 🔴 cites retracted `10.x/contested-source` — _The cited paper_

## ⚪ Unknown DOI (Crossref 404) (2)

- #163 · `10.1234/typo` — Crossref returned 404
```

The `#N` is the 1-based position in your input — `#47` here is the
47th line of `preflight.txt`. Stable across all views and JSON output,
so you can grep your bib for line 47 and find what to fix.

## Severity buckets

- 🔴 **Blocker** — paper retracted. Drop the citation or replace the
  supporting argument.
- 🟠 **Review** — Expression of Concern, OR the paper cites a
  retracted source, OR metadata mismatch detected. Human judgement
  required.
- 🟡 **Correction** — corrigendum / erratum. Skim each; most are
  housekeeping (affiliation, typo), occasionally substantive.
- 🟢 **Info** — clean, no action.
- ⚪ **Unknown DOI** — Crossref returned 404. Likely typo or
  hallucinated DOI. Verify the source.
- ⚪ **Malformed DOI** — doesn't match `10.<digits>/<suffix>`. Verify.

## Deeper checks (opt-in)

**Transitive cite-walk.** Do my citations themselves cite retracted
work? Adds depth-1 cite checking; clean-itself papers that cite
retracted sources get promoted into the 🟠 Review bucket.

```bash
precis jobs check-provenance --refs preflight.txt --transitive depth=1
```

Or via the kind API:

```python
get(kind='provenance', q='10.x/a, 10.x/b, ...', transitive=True)
```

**Metadata verification.** Catches the "right DOI, wrong paper" case.
Requires structured input (DOI + title + authors + year):

```python
get(kind='provenance', view='verify', q='''
[{"doi": "10.1234/foo", "title": "Quantum widgets in surface codes",
  "authors": ["Smith"], "year": 2019},
 {"doi": "10.5678/bar", "title": "...", "authors": ["Doe"], "year": 2023}]
''')
```

Token-set Jaccard comparison with NFKD diacritic stripping + German-
phonetic alts (`Müller` ↔ `Mueller`) + reverse-phonetic fold for the
ASCII↔ASCII case. Flags any DOI whose Crossref title doesn't match
your supplied title, or whose first author doesn't match.

**Candidate hints for unknown DOIs.** When a DOI 404s *and* you've
supplied bibliographic metadata, opt-in to advisory candidate
suggestions:

```python
get(kind='provenance',
    view='verify',
    q='[{"doi": "10.1234/typo", "title": "..."}]',
    suggest_candidates=True)
```

The supplied DOI's status stays `unknown` — we never auto-substitute.
The report just lists possible matches from Crossref. You verify
and replace by hand.

## Pre-release runbook

Recommended sequence one week before release:

1. **Sync the RW dataset** — once, monthly. Surfaces *why* a paper was
   retracted, not just *that* it was.
   ```bash
   precis jobs sync-retraction-watch --mailto you@example.org
   ```

2. **Extract DOIs** from your bib.
   ```bash
   grep -oE '10\.[0-9]{4,9}/[^"} ,]+' references.bib | sort -u > preflight.txt
   ```

3. **Run the audit** with full checks.
   ```bash
   precis jobs check-provenance --refs preflight.txt \
       --transitive depth=1 \
       --view default --out preflight.md
   ```

4. **Fix 🔴 entries first.** These are non-negotiable. Drop the citation
   or replace the argument.

5. **Read 🟠 entries.** Decide whether your argument depends on the
   contested claim. For "cites retracted work" findings: check whether
   the retracted citation is load-bearing in your argument or
   peripheral.

6. **Skim 🟡 entries.** Most are housekeeping; a few may have
   substantive corrections you should know about.

7. **Resolve ⚪ entries** (unknown DOIs). Manual verification — these
   are typos or hallucinated DOIs that need fixing or removal.

8. **Re-run** after fixes to confirm the report is clean.

## How to interpret "cites retracted work"

A clean-itself paper that cites a retracted source isn't automatically
a problem. The citing paper might:

- Only cite the retracted work for unrelated background context
- Cite multiple alternative sources making the same point
- Explicitly note the retraction in their text

OR it might be load-bearing:

- The cited claim is foundational to their argument
- No alternative source backs the assertion
- The retraction was for fabricated data the citing paper builds on

The tool can't tell the difference. Read the citing paper's use of
the contested source — typically a single paragraph — and decide.

## See also

- `precis-provenance-help` — full kind documentation
- `precis-paper-help` — paper ingest (so notice DOIs become refs in
  your local store)
- `precis-doi-resolution` — DOI canonicalisation rules
