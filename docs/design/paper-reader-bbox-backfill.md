# Paper reader — per-chunk bbox backfill (pixel-perfect highlight) + optional re-embed

**Status:** deferred / backlog (not scheduled). Tracked as precis todo
`42379` (`PRIO:low`). Noted in `CHANGELOG.md` as the follow-up to the
two-pane paper reader shipped 2026-06-24/25.

## Goal

Give the paper reader (`/papers`) **precise per-chunk highlight overlays**
and **true chunk mouseover** on the embedded pdf.js viewer.

Today's highlight is **text-layer best-effort**: when a sidebar
(semantic / keyword / TOC / jump) hit is selected, the page jumps to the
chunk's first page and pdf.js's *find* highlights a distinctive phrase
pulled from the chunk text. That works, but:

- it misses on math / figure-heavy spans (the rendered page has `dk`, the
  chunk text has `$d_k$`), and
- it can't do per-region hover (no notion of "this rectangle is chunk N").

Pixel-accurate overlays + hover need **per-block bounding boxes**, which we
do not store.

## Why this is a big project (no cheap path)

There is **no cached source of bbox** to re-parse:

- `marker.py` drops coordinates at ingest (hardcodes `bbox=None`) and feeds
  on marker's *markdown render*, which it regex-splits into chunks.
- The `.acatome` bundles (legacy ingest output) also carry `bbox: null`.

The only way to obtain bbox is to **re-run marker (GPU) in its STRUCTURED
output mode** — marker's block objects carry `bbox`/`polygon`/`page`, but
the current pipeline never requests them. So this is an ingest-pipeline
change **plus a full corpus re-extract**, not a cheap re-parse.

## The reference-safety constraint (the important part)

**Do not re-chunk or DELETE+INSERT chunks.** Every durable reference to a
chunk is anchored to one of:

- **`chunk_id`** (primary key): universal handles `pc<chunk_id>`; the FK
  tables `chunk_embeddings` / `chunk_summaries` / `chunk_tags`; and
  `links` (stored as `src_chunk_id` / `dst_chunk_id`).
- **`ord` / position**: citations (`source_handle = "slug~pos"`), the web
  `?chunk=N` deep link, and TOC `slug~pos` handles.

Keep **`chunk_id` *and* `ord` fixed** and only **add** bbox → nothing
breaks. (Confirmed: re-ingesting an already-ingested paper already never
deletes its chunks; `write_paper` only runs on a fresh ingest.)

If chunk_id changes → embeddings/summaries/tags/links orphan and handles
404. If ord boundaries change → citations / `?chunk` / TOC point at the
wrong text. The backfill below avoids both.

## Approach — additive bbox backfill (a worker pass, not a re-ingest)

1. Per paper, re-run marker in **structured mode** to get per-block
   `bbox` / `polygon` / `page`.
2. **Match each marker block to an existing chunk by text.** The two text
   streams differ — chunks come from regex-splitting marker's *markdown*;
   bbox blocks come from marker's *structured* output (whitespace, table
   flattening, dehyphenation all differ) — so **strict `text ==` will miss
   a lot**. Use **normalized containment / overlap**, scoped by page, not
   strict equality. A miss simply leaves bbox null → the highlight falls
   back to today's text-layer search. Graceful; never breaks a reference.
3. On a match, write boxes into a **new side table**
   `chunk_bbox(chunk_id, page, x0, y0, x1, y1)` — a chunk can span multiple
   blocks / pages, so one row per box. The side table is **purely
   additive**: no touch to `chunks` (the append-only body rule holds), no
   embedding / summary cascade disturbed, no migration risk to existing
   refs.
4. **Viewer:** pdf.js draws overlay rectangles from `chunk_bbox` (exact
   highlight + per-chunk hover) instead of text-layer find; keep the
   text-layer path as the fallback when a chunk has no box.

## Re-embedding (optional, independent, also reference-safe)

Re-embedding is **in place**: the embed worker writes
`ON CONFLICT (chunk_id, embedder) DO UPDATE SET vector = …`, keyed by
`chunk_id`, so id / ord never move. Trigger by bumping the embedder
name/version or clearing rows to re-claim. Orthogonal to bbox — only needed
on a model upgrade.

## Components to build

- **ingest:** a structured-marker extraction path (new call mode) returning
  block `bbox` / `polygon` / `page`.
- **migration:** `chunk_bbox` side table (+ index on `chunk_id`). Forward-
  only, additive.
- **worker pass:** claim shape (papers lacking bbox), structured
  re-extract, block→chunk matcher (normalized containment, page-scoped),
  write boxes; log/skip mismatches (no-harm).
- **web:** pdf.js overlay rendering from `chunk_bbox` + hover; keep the
  text-layer highlight as the fallback.

## Cost / gotchas

- Real cost = the **GPU re-run of marker** across the whole corpus; the
  match + write is cheap.
- **Matcher coverage is the quality lever**; mismatches degrade gracefully
  to text-layer highlight, so partial coverage is fine and safe.
- Do it in due time — not urgent. The current text-layer highlight is
  acceptable meanwhile.

## See also

- `docs/decisions/0036-universal-handles.md` — why handles encode the
  primary key (`pc<chunk_id>`), the reason chunk_id stability matters.
- `docs/decisions/0007-derived-queue-no-block-jobs.md` — the in-place
  `ON CONFLICT DO UPDATE` embedding pattern.
- `src/precis/ingest/marker.py` — where bbox is currently dropped.
- precis todo `42379` — the tracking item.
