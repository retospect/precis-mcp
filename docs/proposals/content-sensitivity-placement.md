# Content sensitivity → placement constraint

- **Status**: proposed — **stub / requirements capture, not yet a design**
- **Owner**: carved out of [ADR 0066](../decisions/0066-capability-tiers-and-placement-chains.md) §6
- **Refs**: memory `backlog_proprietary_local_only`; ADR 0066 (Rollout gate —
  this **gates Phase C** of the tier migration)

## Why this exists

ADR 0066 makes the LLM *tier* a pure capability rung and moves placement
(local vs cloud) to operator-owned failover chains. That decoupling removes
the one non-capability thing `LLM:local` used to do: **force a job off cloud
APIs**. Proprietary/secret content must never reach a cloud provider
(`backlog_proprietary_local_only`). So ADR 0066 commits to the *boundary* —
**capability = tier, sensitivity = an orthogonal constraint that prunes cloud
rungs from any tier's chain** — and defers the constraint's actual shape to
here, because (Reto, 2026-07-25) *"that is a complex bag of worms; we need to
keep track of what is secret enough."*

It is **not** a binary flag. The hard part is not the rung-pruning at
dispatch (that's easy); it's knowing *which* content is sensitive, *how*
sensitive, and keeping that true as content is derived and re-derived.

## The requirement (what must hold)

1. Content marked sensitive above some level is **never** sent to a cloud LLM
   API — not as a prompt, not as retrieved context, not as a tool result.
2. The guarantee survives **derivation**: a chunk, summary, embedding-input,
   card, draft, or answer computed from sensitive source inherits the
   constraint. Lineage, not just the leaf.
3. A caller cannot accidentally launder sensitive content to cloud by picking
   a cloud-only tier — the constraint wins over the tier's chain.
4. It is **auditable**: one can prove after the fact that no
   above-threshold content hit a cloud call (`llm_call_log` + a sensitivity
   stamp).

## Open design questions (the bag of worms)

1. **Sensitivity taxonomy.** Binary (`local-only` yes/no) for v1, or graded
   levels (e.g. public / internal / proprietary / secret) mapping to allowed
   placements? "How secret is secret enough" is Reto's phrasing — implies a
   level, not a flag.
2. **Assignment — how does content acquire a level?** Source-derived (a
   `paper`/`email`/folder/project marked proprietary), a manual tag, an
   auto-classifier, or a mix? Who is authoritative? What's the fail-safe
   **default** for unclassified content (permissive = cloud-allowed, or
   restrictive)?
3. **Propagation to derived artifacts (the genuinely hard one).** How does
   the level flow through ingest (source → chunks → embeddings → summaries →
   cards) and synthesis (draft/answer built from mixed-sensitivity context)?
   A derived artifact's level should be the **max** of its inputs. Where does
   that get stamped, and how does retrieval avoid mixing a secret chunk into
   a cloud-bound prompt?
4. **Enforcement chokepoint.** The constraint rides an `LlmRequest`
   (sensitivity of the prompt + all injected context) and `dispatch` prunes
   cloud rungs. But context is assembled *before* dispatch — the pruning
   decision needs the max sensitivity of everything in the request. Compute
   where?
5. **Interaction with cloud-only FRONTIER.** `FRONTIER` (Opus) has no local
   rung (ADR 0066 §1). So **local-only content cannot run at FRONTIER** — it
   caps at whatever capability runs locally (`BIG`/`MEDIUM`/`SMALL` local
   rung). Is that acceptable (secrets don't get Opus), or do we need a
   trusted local frontier-class model eventually? A real product constraint.
6. **Audit + proof.** Stamp each `llm_call_log` row with the max sensitivity
   of its request; a query proves no above-threshold row ran on a cloud
   transport. Retention/PII implications of storing that.

## Not now

This is a requirements + open-questions capture so the constraint isn't lost
when ADR 0066's tiers land. The tier work (Phase A/B) can proceed; **Phase C
(collapsing `LLM:local` into `BIG`) is gated on this design existing and the
constraint shipping** — until then the `local →` alias keeps pinning local.
