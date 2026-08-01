---
status: draft
title: Taproot hub-refine — periodic, converging enrichment of existing claim hubs
model: opus
---

# Taproot hub-refine — fill existing hubs, converge, don't spin

Build ticket. The *why* is `docs/proposals/taproot.md`; this is the *how* for
one gap the shipped phases leave open.

## The gap

Evidence attaches to a claim hub only two ways today, and neither enriches an
**existing** hub on purpose:

- the **forward bridge** (`_taproot_bridge`, `PRECIS_TAPROOT_CHASE_ENABLED`) is
  *finding-driven* — it attaches a hub's evidence as a side-effect of chasing a
  `STATUS:tracing` regular finding that canonicalizes onto it. It never looks at
  an existing hub and asks "what else in the corpus supports this?";
- `precis taproot mint` attaches supporters a human already *hands* it.

So a hub minted from a single draft cite (the common case) sits at one
corroborator forever — even when the corpus already holds the primary source
(observed: hub `tbx2hd` had only a 2025 circuit paper while Son/Cohen/Louie 2006
sat un-attached in the same corpus, cited elsewhere in the *same* draft).

**Hub-refine** closes this: a low-cadence pass that, per hub, searches the corpus
for corroborating chunks, LLM-verifies support, and attaches the survivors —
**converging**, never re-spinning.

## Non-negotiable: it must converge

The worry is a pass that "updates everything all the time." The design is
**accretive + idempotent + event-triggered + cadence-throttled + saturating —
never a periodic full re-scan.** Concretely:

1. **Idempotent attach** — `hub.attach_evidence` / `seed_claim_hub` already skip
   an existing `(paper, hub, role)` edge. Re-running is a no-op *write*.
2. **Edge-exists precheck BEFORE verify** — do not spend an LLM call to re-judge a
   paper already attached. (Today's mint checks existence only at write time,
   after the human already verified; a pass must check *first*.)
3. **Rejection memo** — the real repeat cost. Idempotency dedups *accepted* edges;
   a candidate judged `supports=no` has no edge and would be re-verified every
   run. Record the negative verdict so it's judged **once**. (The forward bridge
   has the same blind spot — it skips on `supports=no` but records nothing.)
4. **Per-hub cadence stamp** — `meta.last_refined_at`; the pass only claims hubs
   not refined within `PRECIS_TAPROOT_REFINE_INTERVAL_H` (default weekly),
   oldest-first, `HUBS_PER_PASS`-bounded. This is *scheduling* state (spread +
   "not too often"), **not** a corpus-change watermark — we deliberately do NOT
   build an ingest cursor (premature; the memo already kills redundant spend).
5. **Natural saturation** — a hub with no new above-threshold candidate attaches
   nothing, stamps `last_refined_at`, and drops out until the interval elapses.
   (A stronger `TAPROOT:saturated` long-backoff after K empty passes is a v2
   note, not v1.)

Why no watermark: correctness is guaranteed by (1); cost is bounded by (2)+(3)
top-K; cadence by (4). An ingest-triggered watermark is a scale optimization for
"many hubs × high cadence" — out of scope until the per-run baseline is proven
visible.

## Shape

New pass `hub_refine` in `src/precis/workers/hub_refine.py`, wired into
`cli/worker.py` beside `inbound_chase`, gated `PRECIS_TAPROOT_REFINE_ENABLED`
(default-OFF, like every taproot flag). Per claimed hub:

1. **Claim** — `TAPROOT:claim` / `STATUS:canonical` findings with
   `meta.last_refined_at` older than the interval (or absent), `SKIP LOCKED`,
   `LIMIT HUBS_PER_PASS`, oldest-first.
2. **Discover** — `store.search_blocks(mode='semantic', query_vec=embed_query(...),
   kind='paper', limit=TOPK)` (`store/_blocks_ops.py::search_blocks` →
   `search_blocks_semantic`, returning `(Block, Ref, score)`) → top-K candidate
   paper chunks. *Not* `canon.block` — that ANN is over hub cards, for dedup;
   this needs paper-chunk neighbors.
3. **Filter** — drop candidates whose parent paper already has an edge on this hub
   (precheck) or sits in the rejection memo. Cheap SQL, no LLM.
4. **Verify** — `_chase_llm._verify_support_with_caveats(claim, chunk)` per
   surviving candidate → `supports ∈ {yes, partial, no}` + caveats.
5. **Write** — `supports ∈ {yes, partial}` → `attach_evidence(role='corroborates',
   meta={support, caveats, source_handle=pc<id>})`; `supports=no` → append to the
   rejection memo. `partial` carries its caveats onto the edge (the mint path
   already renders `caveats`).
6. **Stamp** — set `meta.last_refined_at`, always (even on an empty pass), so
   cadence holds.

Originators (★) are still derived, not asserted — hub-refine only grows the
*supporter* set; the `cites`-graph that promotes an originator is the inbound
chase's job (`docs/design/citation-chunk-grounding.md`), out of scope here.

## Rejection memo — where it lives

`finding.meta['taproot_rejected']`: a dict `{paper_ref_id (str): {"at": iso,
"supports": "no"}}`. Rationale:

- **Migration-free** — findings already carry `meta` (scope lives there); no new
  table for v1.
- **Bounded** — a handful of rejected candidates per hub (top-K, deduped).
- **Co-located** — read/written in the same hub row the pass already holds.

Re-judge invalidation is deferred: a materially re-ingested candidate paper won't
be re-considered until v2 adds a paper-version tag to the memo entry. Acceptable —
a false-negative that goes stale is recoverable (drop the memo key); the memo's
job is only to stop per-run LLM churn.

> If a queryable judgment ledger is wanted later (analytics: "what did we reject
> and why"), promote to a `taproot_evidence_judgment` table. v1 stays in `meta`.

## Config (all env, default-safe)

| var | default | meaning |
|---|---|---|
| `PRECIS_TAPROOT_REFINE_ENABLED` | `0` | master gate (pass is dark until set) |
| `PRECIS_TAPROOT_REFINE_INTERVAL_H` | `168` | per-hub min re-refine interval (weekly) |
| `PRECIS_TAPROOT_REFINE_HUBS_PER_PASS` | `8` | hubs claimed per pass (throughput cap) |
| `PRECIS_TAPROOT_REFINE_TOPK` | `8` | candidate chunks pulled per hub |
| `PRECIS_TAPROOT_REFINE_MIN_SIM` | `—` | optional distance floor to drop weak ANN hits pre-verify |

Cost ceiling per pass ≈ `HUBS_PER_PASS × TOPK` verify calls, minus precheck/memo
skips — in practice far less once memos fill. The pass needs `--with-llm`-class
verification available (same dependency as chase); if the verifier/embedder is
absent it logs and no-ops (mirrors the bridge's degrade).

## Acceptance criteria

- Pass OFF by default; enabling it on a corpus with hubs attaches ≥1 new verified
  corroborator to at least one under-covered hub, with `support` + `source_handle`
  populated (parity with the mint path, unlike a bare `link`).
- **Idempotence**: two consecutive passes over the same corpus → the second
  attaches 0 and issues 0 verify calls for already-attached or already-rejected
  candidates (assert via a counting fake verifier).
- **Cadence**: a hub refined this pass is not re-claimed next pass (interval > 0).
- **Rejection memo**: a `supports=no` candidate is verified once across two passes.
- Unit-testable with a fake store + mock embedder + counting fake verifier (the
  `canon.block` testing pattern) — no live model in the gate.

## Out of scope (v2+)

Ingest-triggered watermark; `TAPROOT:saturated` long-backoff; paper-version memo
invalidation; queryable judgment table; a `precis taproot refine --once` CLI (the
worker `--only hub_refine` covers ad-hoc runs). Deploy-flip of the flag is a
separate, deliberate ship (env in the precis-worker role), not part of this build.

## Addendum (2026-08-01) — the ingest-triggered watermark got built

The "out of scope" watermark above shipped as a separate pass,
`src/precis/workers/chase_trigger.py` (`PRECIS_TAPROOT_CHASE_TRIGGER_ENABLED`,
dark like every taproot flag; new `claim_embeddings` table, migration 0101).
It reverse-ANNs each freshly-embedded paper/patent chunk against a claim-hub
embedding index and marks a near hub `TAPROOT_DUE`.

`_claim_hubs_due_for_refine` (§ Shape, step 1) is no longer the interval-only
claim query described above: it's now a **due-set** — a hub claims iff
`TAPROOT_DUE`-tagged, never refined, edited since (`meta.last_refined_sha` vs
`taproot.canon.claim_sha(title)`, a reopen), or a long backstop has elapsed.
`PRECIS_TAPROOT_REFINE_INTERVAL_H` is gone; `PRECIS_TAPROOT_REFINE_BACKSTOP_H`
(default 2160h / 90d) replaces it as a stuck-row failsafe, not a schedule. A
sha-reopen also clears `meta.taproot_rejected` before discovery, since the
claim wording itself changed. Everything else in this proposal (discover /
filter / verify / write / stamp) is unchanged. See
`docs/architecture/state-map.md`'s hub-refine entry for present state.
