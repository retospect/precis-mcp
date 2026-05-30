# ADR 0015 — Marker memory-leak mitigation: cap raise, subprocess batches, in-process cleanup

- **Status**: accepted (2026-05-30)
- **Deciders**: Reto + agent
- **Related**:
  - ADR 0014 — PDF metadata write-back (the lock-file recovery pattern
    used here was introduced there; see
    `src/precis/cli/watch.py:_recover_crashed`)
  - `infrastructure/compose.yaml` — `precis-watch:` deploy + command
    blocks

## Context

24 hours of small-PDF backfill on the new write-back-enabled watcher
produced 46 OOM/restart cycles even though smallest-first ordering
was in effect. Math doesn't support "giant PDFs OOMed the
container" as the cause: the watcher had ingested 137 of ~5900
candidates (2.3% through the size-sorted queue) and was working on
files in the 0.4–1 MB range. The median PDF is 2.4 MB; the p99 is
35 MB. Whatever was blowing past the 12 GiB cap was **cumulative
state**, not single-PDF input size.

The likely culprit is Marker / surya / transformers tensor cache
leakage across consecutive ingests in the long-running watcher
process. The `EmbedHandler` / `RakeLemmaHandler` worker is unaffected
(bounded memory profile, never crashed) — only the Marker side of
the watcher leaks.

## Decision

Three complementary fixes, designed to fail gracefully in sequence
so degraded operation still makes progress.

### 1. Raise the container memory cap from 12 GiB to 64 GiB

One-line edit in `infrastructure/compose.yaml`:

```yaml
precis-watch:
  deploy:
    resources:
      limits:
        memory: 64G   # was 12G
```

This is the cheapest mitigation: it doesn't fix the leak, it just
buys more headroom before the kernel reaps the container.
Empirically, 64 GiB should absorb several hundred consecutive PDFs
of leaked state on a typical academic corpus. Combined with the
crash-lock recovery from ADR 0014 (which moves the offending PDF
out of `/inbox` on the next start), OOMs that *do* still occur stop
being pathological — the loop ends after one cycle.

### 2. Subprocess-per-batch backfill (new `--subprocess-batch-size N`)

The structural fix. The watcher's startup backfill now spawns
`precis _watch_batch_ingest` subprocesses of N PDFs each, waits for
each to finish, and moves to the next batch. Marker leaks
accumulate inside the child; the OS reclaims them at child exit.
Tunable via `--subprocess-batch-size` on `precis watch`
(default 0 = legacy in-process behaviour; production default is set
to 50 in compose).

Tradeoffs:
- **Cost**: each subprocess re-loads Marker (~15 s). On a 5900-PDF
  backfill with `N=50`, that's 118 subprocess starts ≈ 30 min of
  cumulative model load. Acceptable amortised across days of
  ingest.
- **Benefit**: per-batch RSS is bounded at "fresh Marker + 50 PDFs
  of leak" ≈ 4–6 GiB, well below any reasonable cap.
- **Compatibility**: live (watchdog) events still use the in-process
  path. The leak only manifests during dense backfill; live ingest
  is rate-limited by arrival, so accumulated state has time to drop
  between calls.
- **Failure mode**: if a child OOMs, the parent keeps running. Per-
  PDF lock files (ADR 0014) survive to be reaped by the next
  watcher start. The OOM-causing PDF lands in
  `/inbox/errors/crashed/<ts>/` and the backfill continues with the
  next batch.

Hidden CLI: `precis _watch_batch_ingest <pdfs...>` (underscore
prefix marks it as not a user surface — the parent/child contract
can shift without a CHANGELOG entry).

### 3. In-process cleanup probe (`_release_marker_caches`)

After each PDF completes through `extract_blocks_marker`, an
explicit cleanup helper runs:

- `gc.collect()` to break ref cycles the surya layout pipeline tends
  to leave behind;
- `torch.cuda.empty_cache()` if CUDA is available;
- `torch.mps.empty_cache()` if MPS is available (Apple Silicon dev
  hosts only — production deployment is CPU-only Linux).

All branches are import-and-feature guarded so the call is safe
without torch installed. Cost: ~10 ms/PDF. Effect: hard to predict
without measurement — torch CPU has its own caching allocator that
`empty_cache` doesn't reach, and `gc.collect` only helps if the
leak is ref-cycle-based rather than C++ heap. Layered on top of
the subprocess isolation as a no-regret probe.

## Off-switches

- Cap raise: edit compose, recreate container. The change isn't
  reversible per-run.
- Subprocess batching: `--subprocess-batch-size 0` (or omit the
  flag) reverts to in-process backfill.
- In-process cleanup: not gated. It's a 10 ms/PDF best-effort that
  doesn't change correctness; gating it would just add an env-var
  knob nobody needs to flip.

## Operational signal

Watch `docker inspect precis-watch --format '{{.RestartCount}}'`.
Pre-fix baseline was ~2 restarts/hour during dense backfill. Post-
fix target is ≤ 0.1 restarts/hour (i.e., one OOM per ~10 hours,
caused by a genuinely too-big PDF rather than cumulative leak).

If restarts stay high after deploy, the leak is likely **per-PDF
rather than per-batch** — Fix B alone won't solve it. The next
escalation would be subprocess-per-PDF (high cost, ~15 s overhead
per file) or a Marker upgrade if the underlying library has shipped
a fix upstream.

## Alternatives considered

- **Subprocess per PDF instead of per batch**. Ironclad isolation,
  but pays the 15 s model-load cost per file. At 2.4 MB median PDF
  size that's a ~10× slowdown of the actual conversion. Rejected.
- **Periodic in-process restart**: kill and re-fork the watcher
  process every N PDFs via `os.execv` or supervisor scripting.
  Same effect as Fix B but harder to reason about. Rejected.
- **Profile and patch the leak in-tree**. Right long-term fix but
  research-project scope. Tracked as a follow-up.
- **Switch off Marker, use the fitz fallback for everything**.
  Significant extraction-quality regression. Rejected.
