---
status: draft
title: Whole-record cite for sources with no passage (software, datasets), with auto bib entry and graph edge
pillar: memory-graph
prio: normal
---

# Whole-record cite for software, datasets and other no-passage records (gr514384)

Reto's ruling (gr514384, folds gr514410): add a whole-record cite kind for
sources with no passage to quote. It creates the bibliography entry
automatically and keeps the graph edge (citing ref -> record).

## Motivation / why

Evidence (gr514384, gr514410, 2026-10-24): a draft author needs to cite
precis-mcp's own Zenodo release (software DOI `10.5281/zenodo.22863181`).
The record exists as paper `pa496283` with 0 chunks. The only documented
path is `precis-draft-cite-help`: "a citation is a finding hub `[fi<id>]`
grounded on a paper passage (`source_handle='pc<id>'`)". A hub cannot be
grounded on a chunkless record (`put(kind='finding', supporters=[{paper,
source_handle}])` needs a passage), so the skill-update agent invented a
fallback (fields typed into the venue bib file). That bypasses the graph.

What actually happens today for `[pa496283]` in prose (verified in the tree):

1. **Graph edge works.** `handlers/draft.py::sync_draft_links` turns any
   paper/patent/finding mention into a chunk-sourced `cites` edge. Only
   finding targets get the `cited_pub_id`/`cited_title` drift stamp.
2. **Lint shames it.** `draft/lint.py::whole_paper_cite_hint` warns "whole-paper
   citation, not a chunk" on write, and `_hygiene_*` lists it on every
   `get`. For a software record there is no better chunk to cite; the hint
   is wrong and trains authors to ignore hints.
3. **Bib type is wrong.** `ingest/doi_registry.py::_normalize_datacite`
   hard-codes `entry_type: "article"` and puts DataCite `publisher` in
   `journal`. `export/latex.py::build_bib` therefore emits `@article` with
   `journaltitle = {Zenodo}`. It drops `version` and `resourceTypeGeneral`
   and has no `@software`/`@dataset`. EndNote (`export/endnote.py::_REF_TYPES`)
   has no software type either.
4. **No documented recipe**, so agents either dodge the graph or fake a hub.

The gap is semantic (nothing says "this is a record cite, on purpose") plus
export fidelity, not missing edge machinery.

## In scope

### API shape: how an agent cites a record

Recommended: no new handle prefix. The author writes the existing whole-ref
handle `[pa<id>]`; what changes is a typed, checked declaration on the
record, so the cite is legitimate rather than merely tolerated.

- **Declare.** `put(kind='paper', id='pa496283', record_type='software')`
  (`software` | `dataset` | `record`). Also set automatically at DOI
  registration from DataCite `resourceTypeGeneral` (Software, Dataset,
  Collection, Model, Workflow, ...; Crossref `dataset` likewise). An agent
  corrects a wrong auto value with the same call.
- **Cite.** Prose `[pa496283]` as today. `sync_draft_links` writes the usual
  `cites` edge and stamps `meta.cite_mode='record'` (see Storage).
- **Discover.** `search(kind='paper', q=..., args={'record_type': 'software'})`
  lists records. `get(kind='paper', id='pa496283')` shows the record type and
  says "cite whole: [pa496283]; no passages".
- **Mint from a DOI.** `put(kind='paper', doi='10.5281/zenodo.22863181')`
  (idempotent request, exists) now also types the record. An un-held DOI is
  not citable until the record row exists; reuse the existing request/wait
  recipe.

Refusals (each raises `BadInput` with `next=`):

- `record_type=` on a paper that has body chunks: "this paper has N passages;
  cite a finding hub `[fi<id>]` grounded on one". This is the guard that keeps
  papers on the passage rules.
- Citing `[pa<id>]` of a typed record whose type was later cleared or whose
  record gains chunks: the cite stays valid but reverts to the ordinary
  whole-paper hint (the predicate is evaluated live, never cached on the edge).
- `record_type=` plus `supporters=` on a hub-mint: refused, a record with no
  passage cannot ground a claim. Claims about the software's behaviour still
  need a passage (README, paper).

### Bib entry generation

Generated at export by `export/latex.py::build_bib`, never stored. Mapping
from the record's `meta` (a pure function `record_bib_type(meta)` next to
`paper_bib_type`):

| record_type | biblatex | notes |
|---|---|---|
| software | `@software` | `version`, `publisher`, `doi`, `url` |
| dataset | `@dataset` | `publisher`, `doi`, `url`, `version` |
| record | `@misc` | `howpublished`/`note` from registry type |

Fields: `author` (DataCite `creators`, already normalised), `title`, `year`
(`publicationYear`), `version` (DataCite `version`, new), `publisher` (DataCite
`publisher`, new; stop overloading `journal`), `doi`, `url` (`https://doi.org/..`
fallback), `note = {Software}` style label where the style needs one. Never
emit `journaltitle` for these types. Classic BibTeX styles that lack
`@software` get a `@misc` downgrade via the existing style resolver
(`resolve_bib_style`), same as other unsupported types.

Registry extension (`ingest/doi_registry.py::_normalize_datacite`): also return
`record_type` (from `resourceTypeGeneral`), `version`, `publisher`; keep
`entry_type` populated for old readers (`article` only when truly a
text/journal article). Crossref normalisation maps `dataset` and `software`
types the same way. Docx reference line (`export/docx.py::_venue_runs`) and
EndNote (`_REF_TYPES`: Computer Program = 9, Dataset = 59) gain the same
types.

### Graph link: storage and rendering

- Edge: the existing `cites` row `draft chunk (src_pos) -> paper ref`,
  idempotent on `(src_ref, src_chunk, dst_ref, dst_chunk, relation)`.
  `dst_chunk` stays NULL; that is what "whole record" means in the graph.
- Edge `meta`: `{"cite_mode": "record", "record_type": "software"}`, written
  with `merge_meta=True` like `cited_pub_id`. Informational (lets the web UI and
  fisheye label the edge); the authoritative predicate is the live record
  state, not the stamp.
- Render: citation popover and the fisheye node show "software record,
  v1.2.3, DOI ..." instead of a verbatim chunk; no "keyword-only view"
  warning. `cites` inverse (`cited-by`) from the record is unchanged, so the
  record's page lists every draft citing it.

### How verification skips it without weakening papers

Checks that assume a passage, and what happens for a live record cite:

| check | today | after |
|---|---|---|
| `whole_paper_cite_hint` / hygiene whole-paper lines | warns on any `[pa]`/`[pk]` | skipped only when `is_record_cite(ref)` |
| `pc_cite_claim_hub_hint` hub nudge | for papers grounding a hub | n/a (record grounds none) |
| `find_drifted_cites` | finding targets only | unchanged (record edges never stamped) |
| evidence-edge verification / citation verifier | chunk-anchored | n/a (no chunk edge exists) |
| bib completeness (`draft-doi-completeness-check`) | DOI + metadata | applies: record must have DOI, title, creators, year |

`is_record_cite(ref)` is one predicate in one module:
`ref.kind == 'paper' and meta.record_type in {...} and live body chunk count
== 0`. Both exemptions (lint hint, hygiene) call it; nothing else changes.
A paper with chunks can never match, so a real paper cited whole still gets
the hint, and `record_type=` on it is refused. The exemption is by
construction narrower than the check, not a flag authors can set on papers.

### Skills (runtime docs)

Update `precis-draft-cite-help` (add a "software, datasets, no-passage
records" section; the "every citation is a finding hub" sentence gains this
one exception), `precis-cite-paper-help` router (new branch 4), and
`precis-bibliography-help` (record entry types). Per the doc rules these ship
in the same commit as the code.

## Storage

No migration. `record_type`, `version`, `publisher` live in `refs.meta` (jsonb)
for the paper ref; the edge stamp lives in `links.meta`. No new kind, relation
or table: `cites` already exists and covers paper targets. If review rules for
a first-class column or a new relation (see open questions), the migration is
forward-only (`docs/conventions/invariants.md`); body chunks are not touched.

## Explicitly NOT in scope

- Letting a claim rest on a chunkless record (hub grounding stays
  passage-only).
- Any weakening of the paper/patent passage hint or hub nudges.
- Fetching or ingesting the software/dataset content itself.
- A Zenodo/GitHub release-tracking feature or auto-versioned citation of
  "latest".
- Rewriting legacy `[pa]` cites in existing drafts (a backfill is a separate
  item; legacy cites keep working).
- Citation-style (CSL) work beyond choosing the biblatex entry type.

## Acceptance criteria

- `put(kind='paper', id='pa<N>', record_type='software')` succeeds on a
  chunkless record and is refused on a paper with body chunks, naming the
  `[fi<id>]` route in `next=`.
- A new DataCite software DOI registers with `meta.record_type='software'`,
  `version`, `publisher`; a DataCite journal article stays `article`.
- A draft chunk with `[pa<N>]` for a typed record writes a `cites` edge with
  `meta.cite_mode='record'`; `write` returns no whole-paper warning;
  `get(view='hygiene')` does not list it.
- The same text with `[pa<M>]` for an ordinary paper still returns the
  whole-paper warning (regression pin).
- `export` to LaTeX emits `@software{...}` with `version`, `publisher`, `doi`,
  `url` and no `journaltitle`; a dataset emits `@dataset`; docx and EndNote
  exports show the matching types. The citing sentence gets a numeric `\cite`.
- The record's `cited-by` list contains the citing draft chunk.
- `precis-draft-cite-help` documents the path; the skill lint and
  `scripts/test` are green. No migration file in the diff.

## Target + blast radius

`handlers/paper.py` (put param, refusal), `ingest/doi_registry.py`,
`handlers/draft.py::sync_draft_links`, `draft/lint.py`, `export/latex.py`,
`export/docx.py`, `export/endnote.py`, skills under `src/precis/data/skills/`.
Blast radius: every paper whose Crossref/DataCite `entry_type` changes
mapping (guard: only unknown-type-to-`article` fallbacks change, and only
for typed records); export byte-identity for ordinary papers must hold
(test). Post-deploy: export one draft containing pa496283.

## Tests

- `tests/test_doi_registry.py`: DataCite `Software`/`Dataset`/`Text` fixtures
  give the right `record_type`, `version`, `publisher`.
- `tests/test_export_latex.py`: `@software`/`@dataset`/`@misc` entries;
  ordinary paper entries byte-identical; no `journaltitle` on records.
- New `tests/test_record_cite.py`: predicate truth table (chunks vs none,
  typed vs untyped), put refusals, `sync_draft_links` stamp, lint hint
  skip, hygiene skip, paper regression pin, edge idempotence.
- Extend `tests/test_endnote_record_locators.py`-style mapping test for the
  new EndNote types; docx reference-line test.
- Skill lint / eval entry for the new section.

## Slices

1. **S** Registry + record typing: `_normalize_datacite`/Crossref fields,
   `put(kind='paper', record_type=)` with refusals, `is_record_cite`, tests.
2. **M** Bib export: `record_bib_type`, `@software`/`@dataset`/`@misc`
   fields in LaTeX, docx line, EndNote types, byte-identity pin.
3. **S** Draft side: edge stamp, lint/hygiene exemption, popover/fisheye
   label, regression pin.
4. **S** Skills + backfill of `record_type` for existing chunkless DOI
   records (`pa496283` first) as a one-shot script, reading the list before
   writing.

Total about M-L; slices 1 and 3 can land independently, 2 needs 1.

## Open questions / decisions log

- **Handle form (Reto).** Reuse `[pa<id>]` plus typed record (recommended; no
  new prefix, no migration, legacy cites become legitimate) or a distinct
  handle such as `[rc<id>]` (explicit in prose, but needs a handle-registry
  entry and a mention-grammar change)? The ruling says "new kind"; is a
  typed record under the existing handle enough?
- **Relation.** Keep the `cites` relation (recommended; every consumer works) or
  a new `cites-record` relation (cleaner queries, but needs a forward
  migration and touches every `cites` consumer listed by
  `grep "relation = 'cites'"`)?
- **Eligibility.** Strict "zero body chunks" (recommended) or also allow a
  chunked record (a software paper with a README ingested) when the author
  wants to cite the artifact, not a statement?
- **Auto vs explicit typing.** Auto-set from `resourceTypeGeneral`, or require
  an agent to declare? Auto risks mis-typing a preprint on Zenodo as software.
- **Existing records.** Backfill `record_type` on current chunkless DOI
  records, or only on first re-resolution?
- **Style gaps.** Which configured bib styles lack `@software`/`@dataset` and
  need the `@misc` downgrade (check `_BIB_STYLES` at build time)?
