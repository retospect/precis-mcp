---
status: draft
title: Taproot atomic claims — migrate existing compound hubs (quiet-window op)
model: opus
---

# Taproot atomic claims — migration of existing hubs

The decomposition machinery shipped (2026-08-13): `extract_claim` returns a
`ClaimExtraction` (atoms + optional compound + not_claims), `conjunct-of`
relation (migration 0126) through the `link_claims` write door,
`hub.apply_extraction` orchestrator, compound hubs hold no direct evidence,
compound trust = worst-of its atoms, workers exclude compounds from
refine/re-embed, backfill runs the cascade per atom + compound. Present-state
truth: `src/precis/taproot/__init__.py` docstring. **What remains is the
migration of existing hubs**, run as a quiet-window operation.

## Population (probed prod 2026-08-14)

**1,346 live claim hubs** — the earlier ~112 figure was the reground review
set, a different denominator; this doc's original cost model was 12× low.

- Evidence: 79% have ≥1 evidence edge, overwhelmingly exactly 1 (935 of
  1,065); 281 have none. Re-pointing is therefore mostly a one-edge decision
  per hub, not a fan-out.
- Inbound cites: 81% cited from prose, again mostly exactly 1 (max 6).
  **Cites do NOT need re-pointing** — prose cites the bundling sentence,
  which stays the compound hub.
- Compoundness proxy on titles: 30% contain " and ", 39% are >160 chars,
  18% both, 3% contain ";". Likely-compound band ≈ 250–530 hubs; the rest
  are probably already atomic and only need a pass-through check.
- Claim-links: zero `refines`, zero `conjunct-of` — clean slate.
- Minting rate ~175/month (July spike: 942) — the population grows while
  the migration runs; the process must be resumable and re-runnable, not a
  one-shot snapshot.

## Strategy

**Pre-reqs (ordered):**
1. Deploy the machinery (`/go`) — it is on `main` only. Post-deploy, new
   backfill-minted claims arrive already decomposed; only `chase.py`'s
   bridge (deliberately non-decomposing, see its docstring) and the legacy
   population still produce/hold compounds.
2. `fisheye-conjunct-of-surfacing.md` — the human review surface can't show
   atom↔compound structure yet; blocking for any human-in-the-loop step.

**Phase 0 — score and cohort (read-only, no window needed).** Rank all
hubs by compoundness score: title heuristics (conjunctions, length,
semicolons) + source-chunk section (intro/abstract/conclusion ranks high,
results low — chunk section structure is available). Emit three cohorts:
likely-compound (~250–530), uncertain, likely-atomic.

**Phase 1 — dry-run decomposition (read-only against prod data, LLM spend
only).** Run `extract_claim` over every hub's full claim sentence
(`finding_body` ord=0 chunk, falling back to title). SMALL-tier, ~1.3k
calls — cheap. Persist proposed splits as a report, not as writes. This
turns the proxy cohorts into actual decompositions and sizes phase 2
precisely (expected: already-atomic majority passes through untouched).

**Phase 2 — apply, atomically per hub (the quiet window).** For each hub
whose extraction produced atoms + compound, in one transaction per hub:
mint atom hubs (converge on existing via the normal `block → judge →
place` cascade — atoms may dedup onto existing atomic hubs), link
`conjunct-of`, **re-point evidence edges** compound→atom, stamp the hub
(`meta.taproot_decomposed_at`) so re-runs skip it. Idempotent by
construction (`apply_extraction` converges; the stamp makes progress
resumable). Already-atomic hubs just get the stamp.

Evidence re-point is the one judgment call per hub: which atom(s) does the
existing paper edge support? With 88% of evidenced hubs carrying exactly
one edge, the shape is "one paper, N atoms". Proposal: per-atom
verification via the (now cheaper, sharper) `hub_refine`-style check —
attach where verified, leave the compound's edge dropped and file
`needs_review` when nothing verifies. Never blanket-copy the edge to every
atom — that recreates the mis-grain lie this whole build exists to fix.

**Phase 3 — human review, triaged not exhaustive.** At 112 hubs a full
human pass was plausible; at 1,346 it is not. Review only: (a)
`needs_review` placements from phase 2, (b) low-confidence splits, (c) a
random QA sample (~5%) of auto-applied hubs. Fisheye (pre-req 2) is the
surface.

**Quiet window definition:** phase 2 only. Pause the derived-queue workers
that touch hubs (`hub_refine`, `chase_trigger`) for the window so nothing
refines/re-embeds mid-repoint; avoid 02:00–03:30 UTC (nightly backup +
caspar's daily reboot). Phases 0/1/3 need no window.

**Rollback posture:** per-hub atomic; minted atoms are ordinary refs
(tombstone/undelete exists), `conjunct-of` edges deletable, the stamp
records what was touched. A bad batch is reverted hub-by-hub, not by
restore.

## Open questions / decisions log

1. **Reto:** human-review depth for phase 3 — triaged-only as proposed, or
   full review of the likely-compound cohort? (The 12× population revision
   is why this needs re-deciding.)
2. **Reto:** evidence re-point method — LLM per-atom verify as proposed,
   or human decision per hub?
3. **Reto:** window scheduling for phase 2.
4. Whether `chase.py::_taproot_bridge` should decompose post-migration, or
   chase-minted hubs just queue for a standing version of this pass
   (phase 0 scoring re-run monthly would catch them).
