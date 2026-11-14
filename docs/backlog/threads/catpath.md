---
roster: active
pillar: 3d-design
effort: high
design-review: yes
---
# catpath

## Resume

- **Next:** td633735 step 1: check the B1 setup (step site, N2 and N+N end states built by named-site ops, a converged and saddle-verified NEB) before blaming the potentials. In parallel the one-pathway pilot of td559534 (NH2OH on Pd; Reto's go 2026-11-14) — GRIPE_FIX owns that run, this thread reads its result.
- **Blocked by:** hold 25 (memory `me568380`): no bulk re-runs on qu164903 until validation B1 and C pass. Validation re-runs and about one canary re-run a day are allowed. Case C needs the licensed Zacros run (td568713).
- **Unblocks:** the re-run of the other 26 poisoned pathways (td559534) and with it the ranking that [catalysis-selectivity](catalysis-selectivity.md) reads.
- **Acceptance:** validation B1 and C green on the released engine; hold 25 lifted by Reto; the poisoned set re-measured; the gripes under Do next closed.
- **Worktree:** `catpath` (engine code lands in the catpath repo; precis-side changes land here)
- **Builds:** unknown. B1's cause is not known, so no estimate is honest yet; the gripes in Do next 3–9 are one build each.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

**Status:** ends when the catpath engine passes its validation cases and
the 27 poisoned pathways of qu164903 are re-measured on it, so the
NO → NH3 answer of catalysis-selectivity rests on a validated engine.
Today (2026-11-14) B1 is red on both potentials (1.40 eV and −0.48 eV
against a 0.49 eV reference), B2 is recorded as an expected failure with
Reto's approval, C has never run end to end, and none of the open
`project:catpath` gripes has a builder. Order: validation first, because
it lifts the hold; then what makes the re-run trustworthy; then what makes
its results readable.
**Last reviewed:** 2026-11-14
**Worktree:** `catpath`
**Allocation decision:** active — Reto 2026-11-14 (gr635804): catpath gets
its own thread and coordinator session; catalysis-selectivity keeps the
science. Bringing up the session is Reto's.

## Do next

1. **td633735** — get B1 and C running end to end ("whatever it takes",
   Reto 2026-11-11). It is the only way to lift hold 25, so everything
   bulk waits on it. Its parts: td568713 (case C through Zacros),
   td568731 (gate case C's TOF).
2. **td559534** — the NH2OH-on-Pd pilot first (allowed now), the other 26
   pathways after item 1. The pilot shows whether the fragment_split fix
   places the molecule intact; its energies are pre-validation.
3. **gr548906** — NEB trust gate. B1's negative barrier on one potential
   looks like an end-state fault; a gate that makes NEB tolerance and seed
   count inspectable keeps such barriers out of the re-run.
4. **gr592788** — kinetics solves killed on their timeout; Reto confirmed
   raising it (2026-11-11). Cheap, and a re-run without it loses solves.
5. **gr566942** — the quest stores no kinetics environment, so a recompute
   cannot say under which conditions a rate was solved.
6. **gr572242** — the anchor:none construction fault needs a diagnostic
   before more candidates are built on it.
7. **gr568291**, then **gr568290** — a base solve that returns no TOF, and
   TOFs at noise level: both decide whether a re-measured pathway can be
   ranked at all.
8. **gr593009** — the validation cases need handles, so a verdict can
   point at the case it rests on.
9. **gr611123**, with **td635806** — a factory rescue failed on a catpath
   slice; decide how the dark factory builds in the catpath repo, or this
   list stays hand-built.

## Horizon

1. **gr568292** — engine follow-ups; after validation, so they are not
   redone.
2. **gr608715** — the uncertainty estimate is a threshold on a standard
   deviation; revisit once validation gives numbers to calibrate against.
3. **gr584540** — a composition inspector for dispatched structures.
4. **gr600851**, **gr600854**, **gr600881** — host element and slab size
   on frontier candidates; one change closes all three.
5. **gr553314** — volcano plot for validation case A.
6. **gr608721** — catpath's ROADMAP.md and BACKLOG.md are stale; rewrite
   them from this file once items 1 and 2 have moved.

## Parked

- **td559534, the 26 remaining pathways** — unparks when td633735 is done
  and Reto lifts hold 25.
- **gr592999** — about six DFT single points for electronic analysis, a
  capped exception under hold 25; unparks when someone needs the analysis.
