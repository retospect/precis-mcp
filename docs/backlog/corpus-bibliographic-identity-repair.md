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
one-off re-run of `enrich_paper` (prod-side, human-gated); (b) an all-caps-titles count in the doctor
report (live papers with title = upper(title), length > 25, and how many
lack a DOI); (c) flag substantive registry/local title mismatches for a human (currently only left alone).

Who: agent-code.

### 4. Multi-article PDFs (gr353777)

A PDF that bundles several articles is ingested whole under one title.
pa207748 holds nine helium/superfluid chunks from a different article.
Fix direction: detect multi-article PDFs when chunk-level title/topic
clusters split. (The supersede path now fills a blank survivor's
title/authors from the stub; DOIs already moved with the identifiers.)

RULED (Reto 2026-10-11): multi-article scans are tagged `multi-article`
rather than split or trimmed (body chunks are append-only). Tagged so far:
pa44468, pa42559, pa207748. Follow-up: the title/body sweep detector (item
5) applies the same tag.

Shipped (gr463414): `link` onto a claim hub from a `multi-article` paper
returns an advisory to confirm the passage's article. Nanopub assembly is
pure over frozen input with no paper refs, so no refusal there.

Who: agent-code.

### 5. No title/body mismatch sweep; downstream gates blind to it (gr264184)

Nothing compares a paper's title with its body, so mismatches surface only
when someone reads a chunk. Fix direction, in order:

- DONE: `scripts/title-body-sweep` (scoring in
  `precis.utils.title_body`) ranks live papers by lexical title-to-body
  agreement, read-only via `prod-psql --ro`. Placeholder titles
  (`is_placeholder_title`) are listed separately, not ranked. First full
  run (30.4k scored): pa693 ranks 274, pa44468 1688, pa42567 1825 (the
  last two overlap lexically: a multi-article PDF body, a topic-adjacent
  body; they need embeddings). Head false positives ~0 of 10 spot-checked.
  Open for the gate design: the cutoff (a low score alone misses the 0.5
  to 0.6 band; a missing title formula caps the rank at 0.35).
- A nanopub mint gate in `src/precis/nanopub/assemble.py` that refuses to
  assemble from a source flagged as a title/body mismatch.
- cite_key regeneration, or refusal, when an author edit changes the
  author list the cite_key was derived from.

Item 1 of the gripe (pa5837) is already repaired.

Who: agent-code; remaining: the two gates below, once the sweep's
false-positive rate on a chosen threshold is measured.

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
  article. Ruled: tag `multi-article`, no split or trim (done).
- **pa44468** (gr249575): scan of four unrelated Nature articles (title is
  correct for the first); pa42559 is a second ingest of the same scan.
  Ruled: tag both `multi-article`, no split or trim (done).
- **Dup and misfiled rows** (gr356743): pa204955 and pa210358 are
  duplicates (merge); pa1873 is a separate preprint of pa1604 (link, do not
  merge); pa3063 is not a paper (retire or retype); pa209931 needs its
  Crossref title; check the cohort for an embedding gap.
- **Placeholder-title papers** (362 found by `scripts/title-body-sweep`:
  `Microsoft Word - x.doc`, `*.indd`, `Untitled`, `(no title)`, one- and
  two-word titles) need retitling from their body/Crossref.
- **Ground-truth check after repair**: list findings grounded on each
  repaired paper and re-verify their edges.

Who: ops for the re-ingest and merges; agent-code for edits that need the
supersede path. needs-Reto: none unless a repair would delete chunks that
signed nanopubs cite.
