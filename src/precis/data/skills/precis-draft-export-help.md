---
id: precis-draft-export-help
family: drafting
title: precis — exporting a draft to LaTeX, PDF, Word, or reMarkable
summary: draft export resolves cross-refs/citations/abbreviations automatically; PDF runs as a job, Word is synchronous, reMarkable needs a per-user device credential and footnotes citations/claims inline
answers:
  - how do I export a draft to LaTeX, PDF, or Word?
  - how do I send a draft to my reMarkable tablet?
  - how do I export just the papers a draft cites, without exporting the draft itself?
  - what does export do automatically with citations and abbreviations?
applies-to: get/put (kind='draft'), put (kind='job')
status: active
tags: [drafting]
kinds: [draft]
---

# precis-draft-export-help — export a draft

## What blocks an export, and what only warns

**Blocking** — the export refuses and reports:

- An uncleared figure (licensing), unless waived per the figure gate below.
- A **drifted cite**: the claim hub was reworded after the citing passage
  was written, so the prose may no longer say what the hub says. The
  failure names the chunk and quotes both statements. Clear it by
  re-checking the passage against the hub's current sentence and
  rewriting it — the rewrite re-pins the cite. See
  [[precis-claim-fidelity-help]] for how closely the prose must track it.

**Advisory** — reported, never blocking:

- Cites resting on hubs that are not yet signed/anchored/published,
  as a count. Most hubs are `candidate`; blocking on this would block
  every export in the corpus.
- Cites written before version pinning existed: status unknown, not
  drift.

Pull the same lists any time without exporting:
`get(kind='draft', id=<slug>, view='hygiene')`.


## Retraction checks

The export pane offers `retraction_check`: `stale` (default; check citations
older than 30 days or never checked), `all` (ignore the persisted TTL), or
`none` (read stored state only). PDF jobs check the whole draft, then block
retracted citations unless `ignore_retractions=True` explicitly waives the
gate. The pane button has a per-press cap; PDF jobs do not. Progress and the
pane show "N of M checked within 30 d". Word exports never contact Crossref.
Both formats place a Retraction coverage block after Unverified claims when
coverage is incomplete or checks were skipped; its first line is red for
`none` or any never-checked citation.

## Export — LaTeX, PDF, Word, reMarkable

```
precis draft export <slug> [--out DIR]   # → main.tex + refs.bib + preamble.tex
precis draft export <slug> --pdf          # …and run latexmk to produce main.pdf
precis draft export <slug> [--no-doi-links] [--no-library-links]
precis draft export <slug> --audience external [--pdf]
precis draft remarkable <slug> [--folder /Precis] [--dry-run]
```

```python
put(kind='job', job_type='draft_export', parent_id=<project-todo-id>, params={'draft': '<slug>', 'doi_links': True, 'library_links': True})
```

The job needs no LLM: an `executor=`-less put lands on `job_inproc` by
default (`executor='claude_inproc'` stays an explicit opt-in) and claims
only on hosts advertising `latexmk` — on a fleet with no TeX toolchain
it waits `STATUS:queued` (the sweeper's unschedulable alert pages)
rather than failing. Concurrent exports of the same draft serialize on
a per-slug advisory lock; a wait past the budget fails `infra` with the
`infra:lock-timeout` open tag (bounded retry — the parent todo is not
latched `child-failed`).

Exports are one-way and disposable (re-export, never hand-edit). Resolves
automatically: each block gets `\label{chunk:<handle>}`, `[dc<id>]`
cross-refs become `\cref{chunk:h}`; each `[pc<id>]`/`[fi<id>]` citation
resolves to its paper and becomes `\cite{}`, `refs.bib` carrying one
entry per cited paper (DOI/arXiv when known). In LaTeX the `[n]` itself
is one clickable link, no extra text: to `https://doi.org/<doi>` (the
arXiv abstract page when there's no DOI), else to the library discovery
search (labelled per install, default `UL` for the University of
Limerick); a paper with neither keeps the plain jump to its
bibliography entry. The bibliography entry prints the library link next
to its DOI. Two independent on-by-default switches,
`doi_links`/`--no-doi-links` and `library_links`/`--no-library-links`
(both off = the clean submission copy); the search URL and label are
per-install config on `PrecisConfig` (`PRECIS_LIBRARY_SEARCH_URL`,
`PRECIS_LIBRARY_LABEL`). The Word export is unchanged. Every defined abbreviation
becomes a `\newacronym`, first use full and later `\gls{…}`, with a
page-number list in the glossary. PDF and Word define the same
occurrence: headings print the bare short and never define it, URLs and
DOIs are left alone, `CNB-based` and `aerosol-CVD` count as uses (the
latter defines as `aerosol-CVD (chemical vapour deposition)`), and `et al.`/`e.g.` do
not capitalise the next word. Record handles follow the audience choice below.
The byline becomes an `authblk`
block under `\maketitle` (ROR hyperlinked). You never write `\cite{}`
(or the byline) yourself. Citations must resolve (`[fi<id>]` → a hub
with held originator papers; legacy `[pc<id>]` → a chunk of a held
paper) or the export marks a stub + warns.

Bibliography style: `bib_style` (job param) / `--bib-style` (CLI) picks the
biblatex style — `numeric-comp` (default), `chem-rsc` (alias `rsc`),
`chem-acs` (alias `acs`), `nature`; absent → the draft's
`meta.workspace.style`; an unsupported value keeps the default and warns.

- **PDF** — deterministic but slow, so it runs as a **job**
  (`put(kind='job', ...)` above), landing the path in
  `job_summary`/`meta.pdf`; no TeX toolchain → a friendly error instead.
- **Word/.docx** — toolchain-free and **synchronous**, with acronym
  first-use expansion + list; figure, table and equation numbers and
  cross-references are Word fields (update fields renumbers); styles below.

## Cross-references

Chunk cross-references read `Figure 1`, `Table 1`, `Section 5.4`
(`Eq. (1)`); ones joined by whitespace, `,` or `and` merge (`Sections 1 and 2`,
`Figures 1 to 3`; not `or`); a reference into the citing chunk's own section or an
ancestor prints `above` (target first) or `below` (`Above` at a sentence
start) and ends a run. A reference to a section adds a locator so the
reader need not hunt: PDF appends the page (`Section 2 (page 4)`; not in
posters), Word the heading name (`section 2 (Methods)`), as Word fields
have no page number. Equations get neither. The Word export abbreviates by default (`fig. 1`,
`table 1`, `section 5.4`, `Eq. (1)`); `?labels=full` on its download URL
keeps the spellings above.

## Repeated citations

Within one paragraph, list item, caption or table cell, a citation mark
naming the same references as the previous printed mark is dropped, in
Word and PDF: "text [5]. More [5]." prints "text [5]. More." The
reference still counts and stays in the list. A different reference in
between, an SI postnote or a trust mark ("(unverified)") keeps the mark.
So does a sentence that leans on it: after "(", a comma, a colon or a full
stop, after "see", "in", "ref." and the like, at the start of the block, or
before "'s". The kept mark is the one the next mark is compared with.
In EndNote mode the dropped mark's pinned passage joins the kept field's
research notes. To print every mark, pass `--repeat-cites` (CLI) or
`repeat_cites: true` (job param).

Two cited records with the same DOI share one number and one list entry
(the export warns once). Records with equal title, year and first author
but no shared DOI are not merged; the export only warns.

## Word citation machinery

The export dialog's dropdown (web `?citations=`) picks how the Word file
encodes citations; every choice prints the same marks and list. Offered
today: **EndNote fields** (default, `endnote`: `ADDIN EN.CITE` fields with
embedded records, `EN.REFLIST`), **Word citations** (`word`: Word's own
`CITATION` fields over the sources stored in the document, and a
`BIBLIOGRAPHY` field; a co-author needs no EndNote, numbering stays live
in Word's own style), **Zotero fields** (`zotero`: `ADDIN ZOTERO_ITEM`
fields carrying each item as embedded CSL JSON, an `ADDIN ZOTERO_BIBL`
list and the document preferences the Zotero plugin needs; a co-author
with the plugin keeps citing and refreshes) and **plain text** (`plain`:
no citation fields). Cross-references and the page number stay fields in
every choice. A group with an SI source stays one mark in every choice
(`[1 (SI), 2]`); in Word citations and Zotero fields the SI postnote is
bound to its own source. The draft's bibliography style picks the
EndNote and Zotero style.

The **research notes** box (web `?notes=0|1`, job param
`research_notes`) applies to EndNote fields only. On puts each cited
passage into its record's Research Notes; off leaves them out, also for
a dropped repeat. Default: on for **dev**, off for **external**.

## Word styles

The Word export puts its look in named styles, not in direct formatting:
restyle a whole class of text by editing one style in Word.

| Text | Style |
|---|---|
| Figure caption (left-aligned) | `Caption` |
| Table title | `Table Caption` (based on `Caption`) |
| "Figure N:" / "Table N:" label | `Caption Label` (character) |
| Picture paragraph | `Figure` |
| Reference entry: "[n]", a tab, the entry; hanging indent | `Bibliography`; `EndNote Bibliography` when citations are EndNote fields |
| Acronym entry "SHORT: long form" | `Definition`; the label in `Definition Term` (character) |
| Published claim artifact entry: sentence, source numbers, linked word "link", "(under embargo)" until public | `Published Claim` |
| "Abstract" label above the prose before the first heading (Word only; no number, not a heading) | `Abstract Title`; the paragraphs in `Abstract` until a "Keywords:" paragraph |
| Page footer "Page N" (live field, centred; no page count) | `Footer` |
| Header from page 2: short title left, first author's family name (or "A and B", "A et al.") right | `Header` |

## Audience and data supplement

The export dialog offers **dev / collaborators** (default) and **external**.
The same choice is `--audience dev|external` on the CLI and
`params={'draft': '<slug>', 'audience': 'external'}` on the existing
`draft_export` job. Omitting it keeps collaborator behavior.

Collaborator exports link record handles such as `[pw12]`, `[st34]`,
`[gr56]` and `[qu78]` to the web application using its configured
`PRECIS_WEB_BASE_URL` — one operator-set origin per installation
(`/secrets`, or `echo <origin> | precis secret set PRECIS_WEB_BASE_URL`);
there is no per-export `base_url` override. Unset, the export still runs but
every record-handle link is path-only — a PDF viewer cannot open it —
so the job warns in its events, the draft hygiene report flags
handle-bearing drafts, and the job summary says the origin is missing
(otherwise it prints the origin used). Paper citations and draft
cross-references keep their normal bibliography and section/figure
semantics.

External exports replace record handles with supplement numbers and
descriptive footnotes, without application links. S1 contains pathways,
S2 structures, S3 datasets/jobs; other kinds get subsequent groups in
first-mention order. Entries within a group follow first mention and
repeated references reuse their number. The document has a Data Supplement
table. One `supplement.zip` sits beside the PDF/Word document; the web
download bundles the two files together.

Pathway entries include a deterministic stored-field description,
manifest/stamps, configuration snapshot, results/kinetics JSON, extxyz
structures and a profile figure. Missing stored fields are marked
`absent`; they do not block export and no model invents them. Stored seed
geometry takes precedence over current linked structures; a fallback is
labelled. An external export refuses literal application/private URLs in
prose, naming the chunks to edit. Remove those URLs or select dev.

## reMarkable sends and snapshots

- **reMarkable** (`precis draft remarkable`, needs a device credential —
  per-user only, the signed-in user's own `/account` pairing (`--user
  <login>` on the CLI; required for an actual upload) — there is no
  deployment-wide fallback) uploads a reMarkable-mode PDF: RM2 page
  geometry, and every citation renders as a numbered `\footnote` instead
  of a bare `\cite`, so you
  read the source inline. A paper/patent cite footnotes the human cite +
  bibliography number + the referenced chunk excerpt; a **claim-hub**
  `[fi<id>]` cite footnotes the claim itself — the nanopub statement
  (frozen approved sentence once reviewed), the publish ladder with the
  current rung bolded, each supporting citation's grounding `pc<id>` +
  the source paper's title in bold + its bibliography number, and any
  recorded validation issues (trust label, citation misses, disputes,
  source integrity flags). Cites inside figure captions and headings
  stay plain `\cite` (LaTeX forbids a footnote there). Destination =
  `remarkable.target_folder` app_setting (default `/Precis`).
  `params={'placeholder_figures': True}` (job) waives the clearance gate:
  image-less figures print as visible placeholders and an uncleared
  third-party image is withheld (caption kept, image replaced by a box naming
  publisher and source), never embedded.
- **Cited sources → reMarkable**
  (`put(kind='job', job_type='remarkable_papers_send',
  params={'draft': '<slug>'})`) sends every cited source PDF (paper /
  patent / datasheet) held on the worker host into a per-draft subfolder
  (`/Precis/<slug>`); missing-on-host sources are reported, not fatal.
- **Reading editions → reMarkable**
  (`put(kind='job', job_type='remarkable_reading_send',
  params={'draft': '<slug>', 'source': '<optional slug>'})`) typesets
  each cited source as its own tablet-sized PDF: the source's body
  chunks in reading order, then a claims appendix (every Taproot claim
  hub grounded in that source), then the original PDF when this host
  holds a copy. A source missing from this host's corpus still gets a
  reading edition without the appended PDF; only a source with zero body
  chunks and no local PDF is skipped. `params.source` restricts the run
  to one cited source (slug).
- **Freeze/snapshot** (release + backup) copies the draft's current
  chunks into an immutable `paper`-like ref (versioned, searchable,
  citable), linked `snapshot-of` the draft; the draft keeps evolving.

## See also

- [[precis-draft-help]] — the draft kind, addressing, and the full verb/param quick reference.
- [[precis-draft-rich-content-help]] — figure clearance/origin, which export's placeholder-figures waiver interacts with.
- [[precis-taproot-help]] — claim-hub `[fi<id>]` cites, footnoted in full on reMarkable export.
