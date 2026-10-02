---
status: draft
title: catalyst quests gate candidates on the bulk Pourbaix verdict at their operating U/pH window, recomputably
pillar: 3d-design
prio: high
blocked-by: pourbaix-bulk-verdict-job
---

# Catalyst quests gate candidates on the bulk Pourbaix verdict at their operating window

Part B of the bulk Pourbaix gate. The verdict engine and job are
`pourbaix-bulk-verdict-job.md` (Part A). Design-reviewed 2026-10-02
(design note §13, S1–S5).

## Motivation / why

See Part A. This item puts its verdict onto quest candidates, so a
candidate whose bulk dissolves across the quest's operating window
stops consuming selectivity budget. A verdict built on a provisional
operating point or a GGA-level energy must stay revisable.

## In scope

1. **Operating conditions on the quest.** `quest.meta.operating_conditions
   = {"U_RHE": V, "pH": x, "ion_conc_M": 1e-6, "window": {"U_RHE": [lo,
   hi], "pH": [lo, hi]}}`. Human-set like the other quest caps; the tick
   never writes it. The window is optional. This is the same key
   `pathway-selectivity-u-ph-window.md` reads for its default window, so
   there is one notion of operating point per quest.
2. **Dispatch.** The compute step dispatches `pourbaix_bulk` for every
   live candidate on a quest with `operating_conditions` set, when the
   candidate has no verdict or its stored verdict's inputs (Part A item 7)
   differ from the quest's current ones or the current MP version. It is
   idem-keyed on (candidate geometry hash, inputs), so it is one job per
   input set. A `config` failure (no key) is not re-minted until the key
   exists (one note per quest, not one per tick).
3. **Harvest and stamp.** It is read from the job meta and stamped on the
   candidate: `pourbaix_verdict` (point), `pourbaix_worst_in_window`,
   `pourbaix_dG_eV_atom`, `pourbaix_domain`, `pourbaix_basis` (the full
   inputs dict). `pourbaix_basis`, `pourbaix_domain` and
   `pourbaix_verdict` go in `frontier._META_NON_MEASURE`.
   `pourbaix_dG_eV_atom` stays out of the default objectives (it is a gate,
   not a ranking axis): it is in `_META_NON_MEASURE` too, shown in the
   leaderboard column only.
4. **Gate** (S2, S3, S4):
   - rule-out `ruled-out:pourbaix-dissolved` only when the quest has a
     window AND `dissolved_everywhere` is true AND the verdict did not
     come from the elemental fallback;
   - `leached`: flag only (Reto, 2026-10-02), and propose the surviving
     solid as a new candidate on the same quest (`ensure_candidate` with
     the named solid's MP structure; dedup by its composition);
   - point-only (no window), `oxidised`, `transformed`, `unmatched`,
     dopant-unassessed: flags, shown in the leaderboard and surfaced in the
     tick prompt with the "necessary, not sufficient" sentence.
5. **Lifting a rule-out** (S2). The same code path that sets
   `ruled-out:pourbaix-dissolved` removes it when a re-evaluation (new
   operating point, window, MP version, tolerance) no longer meets the
   rule. That path is exempt from `harvest_measures`'s `already_out` guard
   for this one tag, and every lift writes a logbook note.
6. **Prod gate on the operating point.** No verdict is stamped on a prod
   candidate until the quest's `operating_conditions` are set by Reto.
   Without them, dispatch does nothing.

## Explicitly NOT in scope

- The engine, the job, dependencies, secret (Part A).
- Using ΔG_pbx as a ranking objective.
- Waking dormant quests: qu202468 is `striving: dormant`, so its first
  verdicts need a manual `precis quest tick --compute` (or a wake) after
  its operating point is set.

## Acceptance criteria

- Test: a quest with a window where one candidate is dissolved across
  the window and one only at the point → only the first is ruled out;
  the second is flagged.
- Test: change the window so the first is no longer dissolved everywhere
  → the tag is removed with a logbook note.
- Test: an elemental-fallback `dissolved` never rules out.
- Test: no `operating_conditions` → no dispatch.
- Test: a `config` failure is not re-minted every tick.
- After Reto sets qu202468's operating point and a compute tick runs:
  every live candidate carries `pourbaix_verdict` + `pourbaix_basis`, and
  the leaderboard shows the column.

## Target + blast radius

`src/precis/quest/compute.py` (dispatch, harvest stamp, rule-out/lift),
`src/precis/quest/frontier.py` (`_META_NON_MEASURE`, leaderboard
column, flags), `src/precis/quest/tick.py` (prompt surface). Prod writes
start only once a quest has `operating_conditions`.

## Open questions / decisions log

- **Decided, Reto 2026-10-02 (review-queue `catalysis-selectivity-4`B):**
  `leached` is a flag, not a rule-out, and the surviving solid is proposed
  as a new candidate.
- **Decided, Reto 2026-10-02 (review-queue `catalysis-selectivity-2`):**
  qu202468's operating point is U_RHE −0.2 V, pH 7, dissolved-ion 1e-6 M;
  window U_RHE −0.4 to 0 V, pH 7 to 10. Rule out only if dissolved across
  the whole window. Written to the quest's `operating_conditions` when
  this item ships, not before (item 6's prod gate).
- **Pending, Reto (review-queue `catalysis-selectivity-4`A):** the
  Materials Project key goes into the vault as `PRECIS_MP_API_KEY`; not
  there yet. Part A builds and tests without it.
