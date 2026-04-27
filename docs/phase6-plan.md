# Phase 6 Plan — File handlers (docx, tex, markdown, book, plaintext, rmk)

Status: **queued** — not yet started.

Phase 6 ports v1's file-backed kinds. Each handler reads a document
file from disk, materializes it into the v2 schema as a slug-addressed
ref with one block per logical paragraph, and provides verbs for read
+ search + (where supported) put.

## Why this is its own phase

Each file handler in v1 is significant code:

| Kind        | v1 LOC | Tricky parts                                           |
|-------------|--------|--------------------------------------------------------|
| `docx`      | ~4000  | Track changes (insertions/deletions), comments, footnotes, run preservation, bookmark slugs, Word's XML quirks |
| `tex`       | ~2500  | Macro expansion, `\input` resolution, comment stripping, pgfplots/tikz blocks, citation extraction |
| `markdown`  | ~800   | Front-matter, fenced code, footnotes, embedded images |
| `book`      | ~600   | Multi-file project shape (TeX + Markdown + JSON config), TOC composition |
| `plaintext` | ~300   | Heuristic paragraph detection, character encodings    |
| `rmk`       | ~500   | Custom RMK note format (Reto's personal markdown variant with embedded metadata) |

That's ~8700 LOC just for the handlers, plus shared infrastructure
(file watchers, change detection, slug/anchor maintenance, diff-
preserving edits). Realistic phase 6 estimate: **2–4 sessions**.

## Recommended slice for the first session

Start with **markdown** — smallest surface, most leverage:

1. `MarkdownHandler` reads `.md` from a configured docs root, hashes
   content, materializes paragraphs as blocks with stable slug-anchors.
2. Re-ingest is idempotent (matching slug → same ref id; new content →
   new block; deleted paragraph → soft-delete block).
3. `put` modes: `append` (new paragraph), `replace` (one slug),
   `delete` (soft-delete one slug). No track-changes; that's deferred
   to docx/tex.
4. View paths: `~SLUG` (one paragraph), `/toc` (heading hierarchy
   reused from the paper TOC module — already done in phase 3.5).

After markdown lands, **plaintext** is mostly a subset (no headings,
no slug anchors → use position-only addressing). Then **rmk** which
adds front-matter and personal extensions.

`docx` and `tex` need their own dedicated sessions. They share a
contract with markdown (slug-based addressing, paragraph blocks,
re-ingest idempotency) but the parsers are an order of magnitude
larger.

## Schema additions needed

The existing schema accommodates file handlers without changes:

- `kinds` table already has `markdown`, `book`, `plaintext`, etc.
  registered (or we add a small migration if any are missing — check
  `0001_initial.sql`).
- `refs.meta` carries the file path + content hash + last-mtime so
  re-ingest can detect "no change" cheaply.
- `blocks.slug` already supports per-block slugs (used by phase 5
  `conv` for turn anchors).

## Reuse opportunities

Phase 3.5 + phase 5 give us a lot of free wiring:

- Hierarchical TOC code (`handlers/_paper_toc.py`) generalizes — its
  heading patterns just need a `markdown` mode.
- `NumericRefHandler` doesn't apply (file kinds are slug-addressed)
  but its `_render_one` / `_list_view` shape transfers.
- `Next:` trailer formatter (`utils/next_block.py`) is already shared.
- `Store.ingest_bundle` has the right *shape* for "ingest a file" —
  per-block insert with vector reuse + slug stability — and could
  factor out a `ingest_blocks_idempotent` primitive that file kinds
  reuse.

## Deferred (post-phase-6)

- **Track changes** in DOCX — needs a Word-XML-aware diff/merge layer.
- **`book` kind** — multi-file project glue. Easier once the leaf
  handlers (md / tex) work.
- **File watchers** — phase 7 territory; for now re-ingest on
  demand via a CLI command (`precis jobs ingest-file <path>`).

## Out of scope for v2

- Real-time collaborative editing.
- WebDAV / cloud-sync integration.
- PDF read-only ingest (covered by the `paper` pipeline already).
