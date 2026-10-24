---
area: memory-graph
source: onto-s1
kind: look-at
blocks: nothing — the spec's catch-all covers them; a ruling only re-slots later
answer-via: tmux
filed: 2026-10-23T23:28:33Z
---
**Question.** 18 live measurand leaves aren't in the ruled Q8 table — OK
for them to land in `measurand/other` on the slice-1 apply?

**Options.** Numbered; the recommended one first with the reason.

1. **`other` now, re-slot later (recommended).** The spec's own rule ("a
   leaf with no group lands in `measurand/other`"), no guessing, and
   re-slotting is one `specialises` relink per leaf once Reto reads them.
2. **Hold the 18 flat** (no group edge) pending a ruling — the apply
   still works, but `under=measurand depth=1` keeps them loose and the
   catch-all stays unexercised.
3. **Propose real groups now** — e.g. `reaction engineering` for the
   tn5086xx seed, `climate` for the climate-orch batch — but that is a
   grouping decision, which the brief forbids me from making.

**Context.** Prod dry run of `scripts/onto-groups` (onto-s1): 102 live
direct leaves under tn460084; 87 resolve 1:1 to the ruled table; 6
skipped (tn460115 + the five legacy terms — onto-repoint/-fix owns
them). The 18 unmapped:

- 9 se-species seed taxa (tn508677–508685): inlet concentration, wall
  rate constant, species diffusivity, inlet velocity, pressure drop,
  outlet conversion, outlet concentration, wall flux fraction, Damkohler.
- 9 climate-orch taxa being minted tonight (coordinator relay, not yet
  on prod): GHG removal rate, cost per tonne removed, energy per tonne
  removed, storage permanence, effective radiative forcing,
  droplet/aerosol number concentration, sea-salt emission rate, surface
  temperature response, TRL — each carries a 'proposed … by
  climate-orch … group placement pending ontology-v2 slice 1' meta
  note. The driver flags pending-placement leaves in its report and
  still lands them in `other` — it cannot fail or orphan them.
---

answered 2026-10-23 (coordinator relay): option 1 — the 18 unmapped land in measurand/other on apply, flagged in the report. Reto: climate-taxon group placement can wait; the spec catch-all covers the rest.
