# ADR 0001 — Merge `acatome-extract` into `precis-mcp`

- **Status**: accepted (2026-05-21)
- **Deciders**: Reto + agent
- **Supersedes**: nothing

## Context

`acatome-extract` (PDF → `.acatome` bundle pipeline) and `precis-mcp`
(DB-backed knowledge platform) currently live as two pip packages with
two Docker images.

The split was made when `acatome-extract` was meant to be reusable
outside `precis-mcp`. In practice:

- `acatome-extract` depends on `precis-mcp` for ingest (today via the
  non-existent stub `acatome_store`; in flight to use `precis.store`).
- The two share an embedder model load (BGE-M3, ~2 GB on disk, ~2 GB
  in RAM). Two containers means two copies of both.
- Plan-first design work sits naturally in `precis-mcp`'s tree; ADRs
  about ingest belong with the ingest code.
- The `.acatome` bundle file format is becoming dead weight: a
  serialisation step between extraction and ingest that no other
  consumer reads.

## Decision

Fold `acatome-extract` into `precis-mcp` as the `precis.ingest`
sub-package. Drop `.acatome` bundles as a wire format. Keep:

- `.meta.json` sidecars for user-authored metadata overrides (read at
  ingest time).
- The PDF metadata stamping path (writes DOI / title back into the
  PDF `/Info` dict) — useful for round-trip and external tools.

Single Docker image, multiple compose services running the same image
with different commands:

```yaml
services:
  precis-watch:  # was acatome-watch
    image: precis-platform
    command: ["precis", "watch", "/inbox"]
  precis-mcp:    # serves the MCP API
    image: precis-platform
    command: ["precis", "serve", "--port", "8765"]
  precis-worker:
    image: precis-platform
    command: ["precis", "worker"]
```

## Consequences

### Positive

- One model cache (BGE-M3 download once, mounted volume shared).
- One pip dep tree, one `uv.lock`, one CI matrix.
- Cross-package ingest bugs go away (no API drift between packages).
- New `precis add` CLI becomes the single entry point for ingest, see
  ADR 0002 for the ID scheme it returns.
- Plan-first workflow (this ADR, the design doc in
  `docs/design/storage-v2.md`) covers the full ingest path.

### Negative

- `acatome-extract` no longer reusable outside `precis-mcp`. We accept
  this — it had no external users.
- `precis-mcp` grows ~5 MB of Marker / sentence-transformers code.
  Acceptable; both were already pulled in via `precis-mcp[paper]`
  optional extra.
- Existing `.acatome` files in `~/work/corpus/` become orphans. We
  re-ingest from PDFs rather than from bundles (see
  `docs/design/storage-v2.md` §migration).
- A few `acatome-extract` commits' worth of git history don't
  follow into `precis-mcp` (we vendor the code, don't rewrite history).

## Migration plan (high-level)

1. Land schema v2 in `precis-mcp` (separate ADR 0003 in
   `docs/design/storage-v2.md`).
2. Copy the salvageable `acatome_extract.*` modules into
   `precis/ingest/`, adjust imports.
3. Wire up `precis add`, `precis watch`, `precis worker` commands.
4. Update `infrastructure/compose.yaml` to drop the separate
   `acatome-watch` service in favour of three services off one image.
5. Wipe the live DB (already empty of paying users; the 2 200 refs
   from current backfill are throwaway), re-ingest from
   `~/work/new_papers/` and `~/work/corpus/`.
6. Archive `acatome-extract` repo with a README pointer to
   `precis-mcp`.

Detailed steps live in `docs/design/storage-v2.md`.
