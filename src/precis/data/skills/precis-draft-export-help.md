---
id: precis-draft-export-help
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

## Export — LaTeX, PDF, Word, reMarkable

```
precis draft export <slug> [--out DIR]   # → main.tex + refs.bib + preamble.tex
precis draft export <slug> --pdf          # …and run latexmk to produce main.pdf
precis draft remarkable <slug> [--folder /Precis] [--dry-run]
```

```python
put(kind='job', job_type='draft_export', parent_id=<project-todo-id>, params={'draft': '<slug>'})
```

Exports are one-way and disposable (re-export, never hand-edit). Resolves
automatically: each block gets `\label{chunk:<handle>}`, `[dc<id>]`
cross-refs become `\cref{chunk:h}`; each `[pc<id>]`/`[fi<id>]` citation
resolves to its paper and becomes `\cite{}`, `refs.bib` carrying one
entry per cited paper (DOI/arXiv when known); every defined abbreviation
becomes a `\newacronym`, first use full and later `\gls{…}`, with a
page-number list in the glossary; `[me<id>]`/cross-draft `[dc<id>]` links
render to nothing (provenance only). The byline becomes an `authblk`
block under `\maketitle` (ROR hyperlinked). You never write `\cite{}`
(or the byline) yourself. Citations must resolve (`[fi<id>]` → a hub
with held originator papers; legacy `[pc<id>]` → a chunk of a held
paper) or the export marks a stub + warns.

- **PDF** — deterministic but slow, so it runs as a **job**
  (`put(kind='job', ...)` above), landing the path in
  `job_summary`/`meta.pdf`; no TeX toolchain → a friendly error instead.
- **Word/.docx** — toolchain-free and **synchronous**, with render-time
  acronym first-use expansion + an auto acronyms list.
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
  `params={'placeholder_figures': True}` (job) waives the clearance gate
  for **image-less** figures only — they print as visible placeholders; a
  licensing block on a real image still fails the send.
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
