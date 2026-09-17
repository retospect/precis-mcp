# Datasheet ops — inbox routing and the browser reader

When you need to know where a `datasheet` ref physically comes from
(inbox paths, watch-dir layout) or how the two-pane reader / citation
rendering behind `precis-datasheet-help` works.

## Where a datasheet comes from

**Drop a PDF into `<inbox>/datasheets/`** — the `precis ingest --watch`
inbox routes that subtree through the paper Marker→chunks pipeline stamped
as `datasheet` (exactly like `<inbox>/cfp/` for a `cfp`). On the cluster
the inbox is `$PRECIS_WATCH_INBOX` (`/opt/nas/botshome/papers/inbox`), so
the drop dir is `/opt/nas/botshome/papers/inbox/datasheets/`; locally it's
the `datasheets/` subdir of whatever you pass to
`precis ingest --watch <dir>`. The usual `tagging/`
sentinel works too (`datasheets/tagging/<topic>/foo.pdf` → `topic:<topic>`).

Every catalog `part` row also carries a `datasheet_url`
([[precis-part-select-help]]); link a datasheet to the part it documents with
`link(kind='datasheet', id='<slug>', rel='datasheet-of', to='part:<C-number>')`
(inverse `has-datasheet`, seeded by migration 0054). Lazy fetch-on-first-
reference from `datasheet_url` is still the planned automation.

## Read it in the browser

Datasheets have a dedicated two-pane reader at **`/datasheets/<slug>`**
(vendored pdf.js on the right, Navigate/Jump/in-doc search on the left — the
same reader as `/papers` and `/pres`). It's reached from Drive (`/drive` — the unified cross-kind seek+manage
surface; `/items` redirects there) or the part it documents — there is
no standalone `/datasheets` list page (like `/pres`).

The **Meta** tab is datasheet-shaped (not the paper's bibliographic form): it
edits three `meta` fields —

- **vendor** (manufacturer, e.g. `Espressif Systems`),
- **sub-type** — one of `datasheet` / `app-note` / `errata` / `reference-manual`
  (the one-kind-for-the-family selector), and
- **part** — the LCSC C-number of the documented part.

They **flow into citations**: a cited datasheet renders as a BibTeX
`@manual` with `organization={vendor}` + `howpublished={<sub-type label>}`
(LaTeX/PDF export) and as a "Vendor (year). Title. [Sub-type] Part C…" line
in the docx **References** (`export.latex.build_bib` / `export.docx`).
(The part is stored on `meta`, not yet a `datasheet-of` graph edge — `part`
is a catalog kind, not a `refs` row.)
