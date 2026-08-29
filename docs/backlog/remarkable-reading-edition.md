---
status: draft
title: reMarkable reading edition — easy-read source PDF + claims appendix + original
prio: med
---

Asked by Reto 2026-08-29. Reading a draft's source papers on the reMarkable
fails because original PDFs have tiny fonts. But a cited, nanopub-covered
paper is *rebuildable*: we hold its body chunks and its claims, so we can
typeset a reading edition sized for the tablet.

For a draft (e.g. `/smartdraft/173020`) — all sources at once, or one paper
at a time — produce per-source a "reading edition" PDF with three parts:

1. **Easy-read body** — the paper's body chunks (`ord >= 0`) in order, light
   formatting, reMarkable geometry (the `remarkable=True` page setup in
   `precis.export.latex` already solves fonts/margins for drafts — reuse it).
2. **Claims appendix** — every claim rooting in this source (nanopub hubs /
   findings whose evidence grounds in this paper), printed after the body so
   the reader sees what precis extracted next to what the authors wrote.
3. **Original PDF appended** inside the same file (pdfpages in the LaTeX
   pass, or pypdf concat after compile), so the typeset text can be checked
   against the source without leaving the document.

Then deliver like `remarkable_papers_send` does: `send_pdf` per file into the
draft's tablet folder.

Existing seams: `collect_cited_sources` (`precis/export/sources.py`) for the
source set; chunk bodies from the store (body chunks are append-only — read
only); claims-per-source lookup exists for the paper `view='claims'` surface
(`precis/…` — see routes/papers.py claims view); `compile_pdf` +
`send_pdf` for delivery. First slice already shipped separately: the
"papers → reMarkable" button that sends the raw original PDFs
(`remarkable_papers_send` job).

Open design points: one combined volume per draft vs one PDF per source
(per-source favours tablet navigation); whether the claims appendix includes
claim state (published/draft) chips; figures — body chunks are text, so the
easy-read part is text-only with the original appended as the figure record.
