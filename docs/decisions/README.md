# ADR index

Architecture Decision Records — one file per decision, numbered in
the order they were taken. Files are never deleted; obsolete
decisions are marked **superseded** and kept for history.

Per AGENTS.md: "sorted by number; never delete, only supersede".

## By topic — current authoritative ADR

| Topic | Current ADR | Notes |
|---|---|---|
| Repo merge (acatome → precis) | [0001](./0001-merge-acatome-into-precis.md) | foundational |
| Tabular output format (TOON) | [0002](./0002-pub-id-and-toon.md) | TOON portion in force; identifier portion superseded |
| Shared tool registry | [0003](./0003-shared-tool-registry.md) | |
| Dockerfile layout | [0004](./0004-multi-stage-dockerfile.md) → [0009](./0009-dockerfile-relocation-container-first.md) | 0009 relocated the file; 0004 still describes the stage layering |
| Migration discipline (forward-only) | [0005](./0005-greenfield-migrations.md) | governs every `*.sql` edit |
| Identifier scheme | [0008](./0008-drop-slug-identifier-normalisation.md) | extends 0006 (tri-id), which itself superseded 0002 §identifier |
| Derived queue pattern | [0017](./0017-derived-queue-family.md) | extends 0007 (chunk-level → family registry) |
| Database backend | [0010](./0010-postgres-pgvector-system-of-record.md) | |
| Dev image (Claude Code + UID/GID) | [0011](./0011-claude-in-dev-image.md) | |
| Model weights in runtime image | [0012](./0012-bake-models-into-runtime-image.md) | cold-build mitigation in [0019](./0019-premodels-build-context.md) |
| MCP session context | [0013](./0013-mcp-session-context-env-vars.md) | env-var triple |
| PDF metadata write-back | [0014](./0014-pdf-metadata-writeback.md) | |
| Marker memory leak | [0015](./0015-marker-leak-mitigation.md) | |
| Work-claim locking | [0016](./0016-advisory-lock-claims.md) | postgres advisory locks; replaces file-based |
| Discovery layer | **superseded** — see [F20 note in CLAUDE.md](../../CLAUDE.md) | [0018](./0018-persistent-discovery-layer.md) kept for history; per-chunk KeyBERT now lives in `src/precis/workers/chunk_keywords.py` |
| Embedder as a service | [0020](./0020-embedder-as-service.md) | **accepted**; client+service+CLI landed (CUDA image + launchd pending) |
| Image split (serve/worker/ingest) | [0021](./0021-image-split-serve-worker-ingest.md) | **accepted**; serve/worker/ingest/embedder targets + build-all landed |
| Independent worker queues | [0022](./0022-independent-worker-queues.md) | **proposed**; extends 0016/0017 |
| `view='dreamable'` (ANN ring, no clustering dep) | [0023](./0023-dreamable-no-clustering-dep.md) | **accepted** |
| Dream loop runtime | [0024](./0024-dream-loop-litellm-inprocess.md) | **superseded / reversed** — in-process litellm abandoned; dream runs the `claude` binary (`utils/claude_agent.py`) |
| In-place cluster reconcile (not a third greenfield) | [0025](./0025-in-place-reconcile-not-third-greenfield.md) | **accepted**; does not supersede 0019 |
| precis-web as sibling package | [0026](./0026-precis-web-surface.md) | **accepted**; browser UI over the handler layer |
| Reparent todos via `parent` link relation | [0027](./0027-reparent-via-parent-link.md) | **accepted**; supersedes the `parent_id` column path for reparenting |
| Host heartbeat telemetry (Status tab) | [0028](./0028-host-heartbeat-telemetry.md) | **accepted**; extends 0026 |
| Multi-root corpus for PDF serving | [0029](./0029-multi-root-corpus-pdf.md) | **accepted**; `PRECIS_CORPUS_DIR` accepts a list of roots |
| `job` / `finding` / `cron` stay separate from `todo` | [0030](./0030-job-finding-cron-stay-separate.md) | **accepted**; rejects collapsing the four kinds |

## Supersession graph

```
0002 (identifier §)  ──→  0006  ──→  0008          # slug story
0002 (TOON §)         (in force)
0007  ──→  0017                                    # derived queue
0007  ──┐
        ├──→  0018  ──→  F20 (CLAUDE.md, not an ADR)
0017  ──┘                # discovery layer — superseded post-ADR
0004  ──→  0009          # Dockerfile move (extends, not replaces)
0012  ──→  0019          # premodels build context (extends)
0024 (reversed)          # dream loop: in-process litellm → back to claude binary
0026  ──→  0028          # precis-web surface → host-heartbeat Status tab (extends)
```

## Conventions

- New ADRs get the next number; never reuse a number.
- An ADR can be **superseded** (replaced wholesale), **partially
  superseded** (one section replaced; others in force), or
  **extended** (later ADR builds on it without invalidating).
- The header of the *older* ADR should name its successor; the
  successor's header should name what it supersedes.
- When a feature ships outside the ADR process (e.g. F20), update
  the affected ADR's status line to point at the live code path
  and leave the ADR body intact.
