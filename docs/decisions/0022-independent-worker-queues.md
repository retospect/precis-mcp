# ADR 0022 — Independent worker queues by resource class

- **Status**: **proposed** (2026-06-06) — plan-first stub; no code
  landed yet. Tracks `docs/design/embedder-service-and-image-split.md`.
- **Deciders**: Reto + agent
- **Extends**: [ADR 0017 — derived-queue family](./0017-derived-queue-family.md),
  [ADR 0016 — advisory-lock claims](./0016-advisory-lock-claims.md).

## Context

`precis worker` co-schedules passes with wildly different resource
profiles in one loop: GPU/embedder-bound (`embed`, `chunk_keywords`,
`tag_embeddings`), network-bound (`fetch_oa`), and multi-minute LLM
subprocesses (`chase --with-llm`, `job_claude_inproc`). A slow LLM job
starves the embed queue. `--only <pass>` already lets passes run
separately, but it's a flag, not a committed deployment shape.

`job_claude_inproc` is the sharpest coupling: it spawns Claude Code
subprocesses from inside the data-plane worker — an autonomous code
agent running in the queue worker's trust/resource boundary.

## Decision

Promote `--only` to first-class deployment units, scheduled by resource
class:

- **light** (`embed`, `chunk_keywords`, `tag_embeddings`, `fetch_oa`) —
  `RemoteEmbedder` client + PG; no torch; tiny.
- **llm** (`chase --with-llm`, `job_claude_inproc`) — node/claude-code;
  multi-minute subprocesses; must not share a loop with the light queue.

`job_claude_inproc` gets its own isolation boundary (separate process /
resource limits / trust model) — details deferred to its own follow-up
but flagged here so it isn't co-scheduled with embedding work.

Claims continue to use the advisory-lock `FOR UPDATE ... SKIP LOCKED`
pattern (ADR 0016), so independent queue processes coordinate safely
over the same tables without new locking machinery.

## Alternatives considered

- **Single worker loop (status quo).** Rejected: head-of-line blocking
  between LLM jobs and embedding.
- **Priority within one loop.** Rejected: doesn't isolate the
  `job_claude_inproc` subprocess trust/resource boundary, and still
  shares one process's memory/CPU envelope.

## Consequences

- **Positive**: embedding throughput no longer starved by LLM jobs;
  queues scale independently; LLM subprocess isolation.
- **Negative**: more processes/units to deploy and monitor.
- **Neutral**: aligns with the image split (ADR 0021) — light queues run
  in the torch-free worker image.

## See also

- `docs/design/embedder-service-and-image-split.md`
- [ADR 0021 — image split](./0021-image-split-serve-worker-ingest.md)
