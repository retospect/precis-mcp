# ADR 0029 — multi-root corpus for PDF serving

- **Status**: accepted (2026-06-15)
- **Deciders**: Reto + agent
- **Builds on**:
  - ADR 0026 — precis-web surface (the Papers tab + PDF viewer)

## Context

The Papers tab streams a held paper's PDF from disk, resolving
`<corpus_root>/<letter>/<cite_key>.pdf` where `corpus_root` came from a
single `PRECIS_CORPUS_DIR`. On the cluster the canonical store lives on
an NFS share that different hosts surface at different mount paths
(`/opt/nas/botshome/papers/corpus` on the macOS gateway,
`/opt/shared/corpus` historically). A single root is therefore wrong
for whichever host doesn't match its mount, and the held paper 404s
with "the file isn't where the server looked" even though the file
exists — just under a different path.

The watcher (`precis watch`) already writes into `papers_corpus_path`;
the web's root was a separately-hardcoded value that had drifted.

## Decision

`PRECIS_CORPUS_DIR` accepts an `os.pathsep`-separated **list** of
roots. `WebConfig` parses the first into `corpus_dir` (back-compat)
and the rest into `extra_corpus_dirs`, exposing a `corpus_dirs`
property (primary first). PDF resolution tries each
`<root>/<letter>/<cite_key>.pdf` in order and serves the first that
exists. The not-found diagnostics list **every** path tried; the
Status tab lists all roots. A single-path value behaves exactly as
before.

The cluster `precis_web` role points `PRECIS_CORPUS_DIR` at
`papers_corpus_path` (the same var the watcher uses) with the legacy
mount as a fallback, so web and watcher are single-sourced.

## Consequences

- A per-host NFS mount difference stops being a 404: list both paths
  and the web finds the file wherever it's mounted.
- No schema change. The PDF location stays derived from
  `(corpus_root, cite_key)` shard layout — we did not add an absolute
  path column to `refs` (see Alternatives).
- The web layer gains no new dependency; parsing is `str.split(os.pathsep)`.
- Resolution cost is at most one `is_file()` stat per configured root
  per request; roots are few (1–3).

## Alternatives considered

1. **Single correct path per host via host_vars / env.** Rejected as
   the primary fix: it works only until the next mount-path drift and
   needs per-host bookkeeping. The list subsumes it — a host can carry
   one root and behave identically.
2. **Store the absolute PDF path in `refs` at ingest.** Rejected: the
   path is host-relative (the same file has different absolute paths
   per mount), so a stored absolute path is wrong on every host but
   the writer. The shard layout + root list keeps the address
   host-portable.
3. **Symlink/standardise the mount path on every host.** Out of scope
   for the app and brittle (autofs/fstab differences across macOS and
   Linux nodes). The list is an app-level accommodation of an
   infra-level reality.
