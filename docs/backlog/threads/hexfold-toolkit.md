# hexfold toolkit

**Status:** ends when hexfold composites join, catalogue and validate
themselves end to end with trusted measured rows, per the hexfold spec §28
roadmap, and the se + hexfold paper (td344088) reports it. Today the join
op and the environment catalogue work on paper; a prod dogfood found the
join silently corrupting composites and the catalogue's persistence inert.
Correctness first, then make the thread diagnosable, then make measured
rows trustworthy.
**Last reviewed:** 2026-09-30
**Worktree:** `hexfold-toolkit`

## Do next

1. **gr457995** — a second join addressing a part's free rim is accepted
   and steals the part, leaving the first composite unable to replay
   itself. Silent, no finding at any severity, reproduced on landed code
   in prod. Every other item in this thread produces or consumes
   composites, so a corrupting join poisons what the rest reads.
2. **backlog/se-composite-integrity.md** — the invariant gr457995
   violates is unchecked anywhere: `validate.py` has no notion of
   `parts`. Needed both to find designs already corrupted in prod and as
   the regression gate for 1, so it lands with 1 or immediately after.
3. **backlog/se-join-observability.md** — a join's findings survive only
   inside the minted structure's meta, and there is no `view='catalogue'`
   despite SPEC §25.3 specifying one. Diagnosing 1 and 2 today means SQL
   archaeology; everything below this line gets cheaper once a join can
   be asked what it reported.
4. **gr457996** — slice 2's persistence is inert: the catalogue seed is
   issued from the join's pure/in-memory half and never commits. Cheap
   (move it into the committing half with its own connection) and it
   unblocks every measured-row item below.
5. **backlog/se-op-handler-test-fixture.md** — 4 passed 80 component
   tests because they call the store directly and never cross the
   prepare/finish boundary. Fix 4 with a test written on this fixture, so
   the class closes rather than the instance.
6. **gr456641 + gr457997** — one root cause: `EnvKey` records no
   measurement extent, so both the seam radius and the armchair leak
   threshold are tube-length artefacts keyed as rim-type properties. Do
   them together. This is the precondition for `trust_measured`, which is
   the entire point of the catalogue.
7. **gr346966** — stick-rung seam-adjacent angles relax to 82–93° on
   every cap fuse. The oldest open hexfold defect, predates the toolkit
   and is independent of 1–6; it caps how much any seam measurement on
   the stick rung can be believed.

## Horizon

(none — owner to fill: spec §28.x slices, valve Q1–Q4, armchair lids, the
seam-motif catalogue once Do-next 6 lands)

## Parked

- **backlog/hexfold-seam-type-catalogue.md** — the seam-motif half of the
  catalogue. Unparks when 6 lands and a measured row can be trusted.
- **backlog/hexfold-sp3-seam.md**, **backlog/hexfold-sp3-isolation-band.md**
  — heterojunction work. Unparks when a second `JOINERS` entry is wanted;
  the registry is already keyed for it.
- **backlog/se-join-unknown-op-in-web-proposal.md** — `status: ready`, but
  it is viewer-surface work; unparks when someone is in `precis_web`.

## No action needed

- **gr456201** — ruled: regeneration is the remedy, no new write path.
- **gr456202** — not a bug; `rim_word` takes `abs(turn)` by design, spec
  erratum and skill wording already shipped.
- **gr456203**, **gr456212** — both auto-diagnosed as already fixed.
  Verify against main and close; no code work.
