---
status: idea
pillar: memory-graph
title: Papers whose title, DOI, authors or body disagree — resolver gates and the prod repairs
---

# Bibliographic identity repair

Bundled in the 2026-10-11 gripe triage. The resolver and enrich passes accept
metadata that does not describe the body, so a paper's title, DOI or authors
name a different work than its chunks. Findings that ground on such papers
inherit the error. Two parts: **Code** (stop producing and start detecting
mismatches) and **Prod data repairs** (fix the known rows). Take them
separately; the repairs do not depend on the code except where noted.

## Code

### 1. ALL-CAPS titles (gr353794)

The enrich pass now repairs casing from the Crossref title for DOI papers
that enrich once (`registry_recase`; old string kept in `meta.title_raw`).
Still open: (a) backfill — already-visited rows carry `authors_resolved_at`
and are never re-selected, so the ~103 existing ALL-CAPS titles need a
one-off re-run of `enrich_paper` (prod-side, human-gated); (b) brace-protect
capitalised tokens ({DFT}, {CO$_2$}) in the BibTeX export
(`src/precis/export/latex.py` `_bib_text`) so the casing survives LaTeX
styles; (c) an all-caps-titles count in the doctor report (live papers with
title = upper(title), length > 25, and how many lack a DOI); (d) document
the behaviour in skill `precis-paper-help`; (e) flag substantive
registry/local title mismatches for a human (currently only left alone).

Who: agent-code.

### 2. Track 2 accepts a title match the body contradicts (gr262881)

Track 2 of `src/precis/ingest/metadata_resolve.py` resolves by title search
and accepts a hit on title similarity alone. pa693 shows the failure: stored
title WSe2, body MoS2. Fix direction: a body-corroboration gate — require
distinctive title terms (formula, material name) to appear in the paper's
own chunks before Track 2 applies the match; otherwise route to review.

Who: agent-code.

### 3. Track 1 ignores version and year (gr260193)

`_resolve_one` in `src/precis/ingest/metadata_resolve.py` takes a
DOI/identifier hit without checking that the body is the same edition.
pa5468 carries the title of v5.0.0 on a 2015 v4.0 body. Fix direction: a
version/year guard in Track 1 — compare the registry year (and a version
token in the title) against the body's own year; disagreement goes to review.

Who: agent-code.

### 4. Supersede drops the stub's identity; multi-article PDFs (gr353777)

Superseding a stub with a fetched PDF does not carry the stub's
title/doi/authors onto the surviving ref, and a PDF that bundles several
articles is ingested whole under one title. pa207748 holds nine
helium/superfluid chunks from a different article. Fix direction: the
supersede path inherits title/doi/authors from the stub; flag (or trim)
multi-article PDFs when chunk-level title/topic clusters split.

Who: agent-code for the inheritance; needs-Reto: flag versus trim for
multi-article PDFs (trimming deletes append-only body chunks).

### 5. No title/body mismatch sweep; downstream gates blind to it (gr264184)

Nothing compares a paper's title with its body, so mismatches surface only
when someone reads a chunk. Fix direction, in order:

- A read-only sweep that scores title-to-body agreement (title terms
  against chunk embeddings/text) and lists outliers for review.
- A nanopub mint gate in `src/precis/nanopub/assemble.py` that refuses to
  assemble from a source flagged as a title/body mismatch.
- cite_key regeneration, or refusal, when an author edit changes the
  author list the cite_key was derived from.

Item 1 of the gripe (pa5837) is already repaired.

Who: agent-code; the sweep first, gates after its false-positive rate is
measured.

### 6. hexfold spec source S31 is a TODO (gr450122)

`src/hexfold/spec.md` source [S31], Miller/Terrones, *Carbon* 2016
(doi:10.1016/j.carbon.2015.10.040), is marked TODO: not in the paper store.
Fix direction: import the DOI, or drop S31 before any spec claim cites it.
Small.

Who: agent-code.

## Prod data repairs

Writes to prod; each is read-first, then a targeted edit. Items depending on
the code part are marked.

- **pa693** (gr262881): title WSe2, body MoS2. Re-ingest the WSe2 PDF for
  DOI 10.1021/acsanm.4c06561 and retire or retitle the wrong body.
- **pa5468** (gr260193): title v5.0.0, body is the 2015 v4.0. Retitle to the
  body's edition, or re-fetch v5.0.0.
- **pa42567** (gr348665): title/body mismatch. Compare pa4815 (likely the
  true holder of one side) before deciding which row keeps which.
- **pa207748** (gr353777): nine helium/superfluid chunks belong to a foreign
  article. Body chunks are append-only (DELETE + INSERT only); split them
  out through the supersede path, not an UPDATE.
- **pa44468** (gr249575): title "thalidomide defects", chunks are HRTEM
  peapod text. Find the true title from the body, retitle, and re-check any
  grounding edges that relied on the old title.
- **Dup and misfiled rows** (gr356743): pa204955 and pa210358 are
  duplicates (merge); pa1873 is a separate preprint of pa1604 (link, do not
  merge); pa3063 is not a paper (retire or retype); pa209931 needs its
  Crossref title; check the cohort for an embedding gap.
- **Ground-truth check after repair**: list findings grounded on each
  repaired paper and re-verify their edges.

Who: ops for the re-ingest and merges; agent-code for edits that need the
supersede path. needs-Reto: none unless a repair would delete chunks that
signed nanopubs cite.
