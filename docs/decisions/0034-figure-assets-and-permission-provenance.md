# 0034 — Figure assets, data supplements, and permission provenance

* Status: **Draft / proposed** (design plan; not yet implemented)
* Date: 2026-06-21
* Continues: [0033 — draft chunks as an editable document](0033-draft-chunks-editable-document.md)
* Related: [0029 — multi-root corpus PDF serving](0029-multi-root-corpus-pdf.md)

## Context

ADR 0033 made drafts chunk-native and **deferred image/graph payloads to a
next phase** (§9): "An image payload is an *asset* the agent attaches/swaps
but cannot author … a graph/plot is an image payload backed by a raw-data
*supplement*, so the figure is regenerable *from the data*." This is that
phase. The worktree is `image-support`.

Drafts include figures, and (per Reto) figures fall into three legal/origin
classes that the graph must distinguish and **prove cleared at ship time**:

1. **Original** — a schematic/diagram we drew. No data, no permission. Fine
   as-is, but record that it is ours.
2. **Own graph/plot** — generated *from data* (and usually a script). The
   generating **data and code travel with the figure** so it is regenerable.
   The data also ships as a supplement.
3. **Third-party image** — reused from another paper "with the permission of
   the publisher". There is a real paper-trail (publisher, permission /
   licence number, date requested, date granted, scope, expiry, the
   request↔grant correspondence) and we want the **hi-res source image**, not
   just a pasted screenshot.

Today precis has **no binary storage in drafts** (everything is text/chunks;
PDFs are the only binary and they live on the filesystem with a metadata row
in `pdfs`) and **no figure-level provenance/permission model**. The
`provenance` kind is unrelated (stateless DOI retraction/amendment checks).

### Decisions taken (Reto, 2026-06-21)

* **Binaries live in the database, attached to the chunk** — *not* a filesystem
  asset store. Export plumbing (writing to `pics/`/`data/`) is a later concern.
* **Permission paper-trail is metadata on the figure chunk** — *not* a separate
  first-class kind. (Reusability/lifecycle didn't justify a kind for now;
  promotable later if blanket grants spanning many figures become common.)
* **A graph carries its own regeneration recipe** (data + code), stored
  **in-DB as chunks** alongside the figure (not as workspace files) — §3.

### What already exists to build on

| Mechanism | Where | Reuse |
|---|---|---|
| Chunk-native draft, handle-addressed, lifecycle-logged | ADR 0033, `chunks.handle`/`pos`/`parent_chunk_id`, `chunk_events` | a figure is just another chunk in the stream |
| Caption matching + base64 image extraction at ingest | `ingest/figures.py` (`encode_image`, `match_figure_captions`) | importing a figure from a source paper |
| Verified source-pointer + audit-in-export pattern | `citation` kind + `\citequote{key}{verbatim}` macro; bare `\cite` fails lint | mirror for figures: an uncleared figure fails review |
| Reference/link sync at write time | `DraftHandler._sync_draft_links` | add `derived-from` (graph→data/code) links |
| Content-addressing precedent | `pdfs` table (sha256 PK, `size_bytes`) | hash figure blobs for dedup/identity |
| Figure/data export targets | tex template `\graphicspath{{pics/}}` + `data/` dir | where blobs land *at export*, later |

## Decision

Everything hangs off the **figure chunk**. No new kind, no filesystem store.

```
  draft (kind=draft)
    └─ figure chunk (chunk_kind='figure')         ← caption (face, embedded)
         ├─ binary image bytes  ──────────────►  chunk_blobs(chunk_id) [in DB]
         └─ meta.figure = { origin, permission?, alt_text, screenshot? }
                                  origin='third_party' → meta.permission (paper-trail)
                                  origin='own_graph'   → derived-from links ▼
    └─ figure_code chunk (chunk_kind='figure_code') ← plot script (text, embedded)
    └─ figure_data chunk (chunk_kind='figure_data') ← data (text if small; blob if large)
         figure ──rel='derived-from'──► figure_code, figure_data
```

### 1 — Binary storage: `chunk_blobs` side table (bytes in the DB)

Per Reto: bytes live in Postgres, attached to the chunk. They cannot go in
`chunks.text` — that column is `NOT NULL` and feeds a `GENERATED ALWAYS`
`tsvector`; megabytes of base64 would poison full-text search and the embed
cascade. So a **side table keyed by `chunk_id`**:

```sql
CREATE TABLE chunk_blobs (
    chunk_id   BIGINT PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    bytes      BYTEA NOT NULL,        -- TOASTed out-of-line; never read unless selected
    mime       TEXT  NOT NULL,        -- image/png, image/tiff, text/csv, …
    sha256     CHAR(64) NOT NULL,     -- identity / dedup hint
    size_bytes BIGINT NOT NULL,
    width      INT, height INT,       -- NULL for non-images
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

* **Still "a chunk"** — one blob ↔ one chunk row, addressed by the chunk's
  handle, lifecycle-tracked via `chunk_events`, `ON DELETE CASCADE` with the
  chunk. The side table just keeps the hot `chunks` reads
  (`SELECT text, meta …`) lean; Postgres TOAST already stores large `bytea`
  out-of-line, so the blob is fetched only when explicitly selected (render /
  export).
* The figure chunk's **`text` is the caption** (embedded + searchable as
  normal); the image is in `chunk_blobs`.
* `sha256` gives us identity now and *optional* dedup later (not enforced —
  drafts rarely reuse the same image; revisit if they do).
* **Export is deferred**: writing blobs out to `pics/`/`data/` happens in the
  export step, built later. For now the graph just *holds* the bytes.

### 2 — `figure` chunk kind + permission meta (completes ADR 0033 §9)

Register `figure` as a draft chunk_kind (face/payload split from ADR 0033):

* **Face** (`chunks.text`) = caption. Authorable, embedded, searchable. The
  agent edits the caption; it **attaches/swaps the blob**, it cannot author it.
* **Payload** (`chunks.meta.figure`):
  ```json
  {
    "origin": "original" | "own_graph" | "third_party",
    "alt_text": "...",
    "screenshot_chunk": "<handle>",        // optional low-res paste kept pre hi-res
    "permission": {                        // present iff origin == third_party
      "publisher":       "Springer Nature",
      "permission_id":   "SNCSC-2026-0451",
      "license":         "RightsLink one-time print+electronic",
      "status":          "requested|granted|denied|expired",
      "requested_at":    "2026-06-10",
      "granted_at":      "2026-06-18",
      "expires_at":      null,
      "scope":           "this manuscript, print+electronic, worldwide",
      "required_credit": "Reprinted by permission from Springer Nature: …",
      "source_paper":    "smith19",        // cite-key; also a 'cites' link
      "correspondence":  ["<blob chunk handle>", "…"]  // grant letter / RightsLink PDF
    }
  }
  ```
* `origin` drives the clearance gate (§4).
* **Permission is figure meta**, per Reto. The grant letter / RightsLink
  confirmation PDF / screenshot is itself stored as a `chunk_blobs` blob and
  referenced by handle in `permission.correspondence`, so the paper-trail
  binary is in the graph too.
* `third_party` figures also emit a `rel='cites'` link to the source paper (so
  "what figures did we borrow from Smith 2019?" is a backlink query).
* `graph`/`image`/`plot` collapse into one `figure` chunk_kind discriminated by
  `meta.figure.origin` — identical storage, less duplication.

### 3 — Graph regeneration (the "with them, or on file?" question)

A graph is `origin='own_graph'`: a rendered image **plus** the data and code
that produced it. Recommended model — **keep all three in the DB as chunks**,
consistent with "binaries as chunks":

* `figure` chunk — caption (face) + rendered PNG/SVG in `chunk_blobs`.
* `figure_code` chunk — the plot script (matplotlib / plotly / gnuplot …).
  This is **plain text**, so it embeds and is searchable, diffs cleanly via
  `chunk_events`, and is the regeneration recipe. *Storing code as a chunk is
  a feature, not a workaround.*
* `figure_data` chunk — the inputs. Small CSV/JSON → text chunk (searchable,
  numerics-indexed). Large/binary data → `chunk_blobs`.
* Linked `figure ──rel='derived-from'──► {figure_code, figure_data}`.

**Regeneration** = run `figure_code` against `figure_data` → produces the
image blob. That render step is itself **next-phase** (ADR 0033 called the
data→plot pipeline generative/next-phase); for now we *store* the recipe so
regeneration is possible later and the data-supplement requirement is
satisfiable immediately.

Why in-DB over on-file (**decided**, Reto 2026-06-21): it keeps the recipe
addressable, versioned, and travelling with the draft (no dangling
`data/foo.csv` path that export has to chase); the workspace `data/`+scripts
dir becomes an **export target**, the same deferral as image blobs. The
rejected alternative — code/data as workspace files referenced by path — is
closer to how a human runs the plot locally but reintroduces the filesystem
coupling ADR 0033 moved away from.

### 4 — Figure-clearance gate (the payoff)

A draft is **figure-clear** iff every `figure` chunk satisfies its `origin`:

| origin | requirement |
|---|---|
| `original` | none (recorded as ours) |
| `own_graph` | ≥1 `derived-from` link to a `figure_data` chunk (data supplement present) |
| `third_party` | `meta.figure.permission.status == 'granted'` and not past `expires_at` |

* A **lint** in the review pass and at **export** — an uncleared figure is a
  hard error, exactly as a bare `\cite` (no `\citequote`) fails today.
* A **Figures web panel** in the draft viewer — each figure with an origin
  chip and ✓/✗ clearance badge (permission id + expiry, or "data attached", or
  "missing permission"). This is where Reto pastes a screenshot, then swaps in
  the hi-res blob + fills the permission meta.
* *(Later)* an `alert` for a permission whose `expires_at` is near while a live
  draft still references it.

### 5 — Capture & export

**Capture:**

* New `precis_web` `multipart/form-data` upload endpoint (first write path in
  the drafts UI) → hash, store in `chunk_blobs`, attach to a figure chunk.
  Used for hi-res images, data files, and grant screenshots/PDFs.
* Agent path: `edit(kind='draft', …)` accepts a base64 payload (pasted
  screenshot) or a URL fetched through **`safe_fetch`** (SSRF guard —
  mandatory for any agent-supplied URL).
* Import-from-corpus reuses `ingest/figures.py` (extracted image + caption).

**Export (deferred, sketch only):** at export each figure writes its blob to
`pics/<handle>.<ext>` + `\includegraphics`; `figure_data` writes to `data/`;
`figure_code` to a scripts dir; `third_party` emits `required_credit` verbatim
into the caption or a generated Permissions appendix. Built after the storage
+ capture layers land.

## Consequences

* **One new table** (`chunk_blobs`) and **three new chunk_kinds** (`figure`,
  `figure_code`, `figure_data`). **No new kind**, **no `refs` column change**,
  **no filesystem dependency** for the storage layer.
* Binaries in Postgres: simple and self-contained; cost is DB size + backup
  weight. Acceptable for draft-scale figure counts; the `sha256` column leaves
  the door open to dedup or a filesystem spill later without an API change.
* Embedding cost: only caption faces, plot code, and small data chunks are
  embedded; image/large-binary bytes never are.
* Permission-as-meta means a blanket grant covering many figures is duplicated
  across their meta. Fine at current scale; the promote-to-kind path stays open.
* Clearance becomes provable at ship time: enumerate figures, show each is
  ours / data-backed / validly licensed, reproduce the paper-trail on demand.
* Distinct from the existing `provenance` *kind* (DOI health) — note in
  `precis-overview` to avoid name confusion.

## Open decisions

1. **`chunk_blobs` side table (recommended) vs. a nullable `chunks.blob BYTEA`
   column.** TOAST makes the column viable; the side table keeps `chunks` pure
   and dedup-ready. Minor — can be settled at implementation time.

## Phasing

1. **`chunk_blobs` + figure chunk kind** — ✅ landed 2026-06-21. Migration
   `0033_chunk_blobs.sql`; `Store.add_figure`/`get_chunk_blob`; `put(kind=
   'draft', chunk_kind='figure', text=caption, image=<b64>, origin=, mime=?,
   permission=?)`; `meta.figure` with origin + permission; draft web render
   (origin chip + clearance badge) and `GET /drafts/blob/{handle}`. The
   permission paper-trail (Phase 2's meta schema) landed here too, since it
   rides on the same figure put.
2. **Capture surfaces** — ✅ web upload landed 2026-06-22: per-block
   "＋ figure" control → `POST /drafts/{ident}/figure` (multipart) → `put`,
   with an inline permission form for `third_party`. Agent base64 also done.
   **Still to do**: agent `safe_fetch`-URL ingest, grant-letter
   correspondence blobs.
3. **Graph recipe** — `figure_code` + `figure_data` chunk_kinds, `derived-from`
   link sync, Figures panel showing the recipe.
4. **Clearance gate** — lint in review + (stub) export, Figures-panel clearance
   badges, optional expiring-permission `alert`.
5. **Export** — `pics/`/`data/`/scripts materialisation, `required_credit`
   injection, supplement manifest, (later) data→plot regeneration step.
