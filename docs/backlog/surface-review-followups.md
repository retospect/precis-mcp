---
status: ready
title: surface-review follow-ups — resume pointer for pass #1's findings
pillar: platform
prio: high
---

# surface-review pass #1 — what's left, in order

Resume pointer for the work that came out of the first `/surface-review`
(2026-09-29). Everything below is filed; this file only carries the **order**
and the **cross-item constraints**, which live in no single item.

Pass #1's numbers and method: `docs/runbooks/surface-review.md` `## Log`.
The miner itself: `scripts/mine-sessions/README.md`.

## Done, landed

Landed: `scripts/mine-sessions/` (committed extractor + 12 detectors +
evidence cards + scoreboard), the `/surface-review` command + runbook +
14-day cadence script, three doc-staleness fixes, and the `gen-schema`
statement_timeout fix with `docs/reference/schema.md` regenerated 58→130
tables. Also landed: `policy-gates-must-fail-distinguishably`.

**Deployed and gated.** Both landings were inside the deploy session's
2026-09-30 full gate of the integrated main and are on the fleet, so the
schema regen and the two changed product skills (`precis-fisheye-help`,
`precis-status-help`) are live and validated.

## The order — all items shipped (2026-10-15)

1. **Shared render cap** — shipped and on prod. Mechanism note for the
   record: `_capped_section.render_capped_section` serves inline links;
   comment timelines moved to `_comment_timeline`'s richer two-tier policy
   (b9f44519f) — a single `limit` can't express newest-in-full +
   one-liners + hidden. Render-diet (c37b29150 et al.) tuned that policy;
   the stale "one shared renderer" docstrings were corrected in the same
   pass.
2. **`singleton-id-no-batch-form`** — shipped in full: the batch
   `get`/`tag` half 2026-10-09 (`_coerce_ids`, cap 50, one transaction for
   the tag batch), the batch `put(kind='draft', chunk_kind='term',
   terms=[...])` glossary case 2026-10-12 (one transaction, per-term
   outcome lines, same `BATCH_ID_CAP`).
3. **`draft-write-latency-whole-draft-rescan`** — shipped 2026-10-02/03
   (chunk-scoped `sync_draft_links`, concurrent dedup judges, a full sync
   before export's gates): prod text edits p50 24 s → 0.27 s (n=39).
4. **Ledger instrumentation** — shipped 2026-10-15, no migration needed
   after all (the columns already existed): `result_bytes` live since the
   0209 deploy; `result_count` now populated from `Response.result_count`;
   the correlation key is `session_id`/`seq` for MCP-bound calls and, for
   the in-process OSS agent loop, `env_overlay`'s context keys now bind the
   per-tick ContextVar (`_overlay_tick_context`) so `agentlog_id`/`source`
   land there too. Residual: spawned-claude MCP env pass-through is deploy
   plumbing (template carries it; fleet propagation unverified on prod).
5. **`gr456287`** (harness worktree guard, 308 refusals / 97 of 99 sessions)
   — the in-repo lever (accepted-shape cheat sheet) already lives in
   `docs/conventions/container-ops.md`; what remains is upstream-only in
   `worktree-path-guard-false-positives.md`.

## Next pass

Due **2026-10-13** (`scripts/surface-review` prints DUE). It should be far
cheaper than pass #1 — the extraction is code now and `scoreboard.json`
carries a `schema_version`, so pass N is comparable with pass N−1.

Two things the runbook already encodes but that are worth knowing before
reading it: size the random arm at **30–40** (pass #1 ran 12 and that arm
still produced the most valuable finding), and read the byte/retry columns
before the error table (the error rate is 0.8%, so it is not where the waste
is).

## Traps that cost this session time

- **Miner artefacts live outside the repo** (`~/.cache/precis-mine-sessions/
  <worktree>/`), because the secret gate scans the working tree rather than
  the index. Don't move them back in; `scripts/mine-sessions/outdir.py`
  explains it.
- **Only 2 fleet-wide gate slots exist.** `scripts/test` queues behind
  siblings; a quiescent run is waiting, not hung. Read the holder file, not
  `docker ps`.
- **`OSError: [Errno 23]` from a policy gate is not a finding** — policy
  scans fail distinguishably on OSError since 2026-10-09; see
  `memory/gate-errno23-fd-exhaustion.md`.
