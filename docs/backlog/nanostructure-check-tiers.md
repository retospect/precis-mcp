---
status: draft
title: run the chemistry checks — no generator route calls check(), and naming the classes topology / valence / geometry / physics
prio: high
model: opus
---

# The chemistry checks nobody runs

Reto, 2026-09-26: "do we need erc/drc equivalent from pcb (really, shape
check, electric check, and all the rules check (relaxed), and then DFT check
later on" — then "maybe we need our own ... names for these tiers".

**Revised 2026-09-26 after a Fable review that corrected the diagnosis.**
The first draft blamed the dual route for bypassing the checker and proposed
five new tier names. Both were wrong in ways worth recording, because the
wrong version is the intuitive one.

## The defect: the checker has no call site

`src/hexfold/check.py` is a good rule checker — typed findings, namespaced
codes, severities, a tolerance policy object. It is also almost never
called.

- `src/precis_se/atomic/generators/hexfold_spec.py:150` is the **only**
  `check()` call on any generator path, and it sits behind
  `fidelity == "check"`, which is a dry-run lint: it returns a
  `GeneratedBlock` with `dry_run=True`, `coords=np.zeros((0, 3))`,
  `bonds=[]`, `elements=[]`. It produces a report and *no atoms*.
- The path that actually produces atoms (`fidelity` anything else) calls
  `build(spec, strict=True)` then `stick(net)` and never calls `check()`.
  So neither the valence findings nor the geometry findings run in the
  production default.
- `src/precis_se/atomic/generate.py` goes `block = builder(params)` →
  dry-run branch → slug preflight → `add_block`. There is **no
  post-builder hook at all**, for any family.
- `GeneratedBlock` (`generators/_types.py:86`) carries envelope, ports,
  topology, provenance, elements, coords, bonds, dry_run — and **no
  findings channel**. Even a rule that fired would have nowhere to go.

So the bypass is universal, not dual-specific. cnt, fullerene, cone,
cyclodextrin, tpms and the hexfold stick path all mint unchecked. The
first draft of this file said the dual route was the exception because it
never builds a `Net`; that story was tidy and false, and it would have
produced a fix (a `DualNet`-shaped protocol) that left every other family
exactly as unchecked as before.

**The right invariant: every `GeneratedBlock` passes a block-level checker
in `generate.py` before `add_block`.** One call site, every family, present
and future. And the narrow interface that needs is what `GeneratedBlock`
already carries — `coords`, `bonds`, `elements`, `hybridizations`.

Second correction, same shape: gripe 451269 says `geom.bond.short` "would
have fired on every bond". It would have been *produced*, capped at the ten
worst (`check.py:224`), at WARN under `Profile.DEFAULT`. Only
`Profile.STRICT` promotes it to ERROR. Without a caller that inspects
`report.ok`, a fired finding and a silent pass are the same event.

## Naming: use the words the repo already has

Four classes, decomposed by **input kind**, which is also the cost class:

| class | input | asks | codes |
|---|---|---|---|
| **topology** | combinatorial map (graph + orientation) | is the cell complex self-consistent? | `euler.*`, `internal.*`, `ring.*` |
| **valence** | atom graph | can each atom exist, and what is conjugated to what? | `valence.*`, bond order, `conj.*` (new) |
| **geometry** | coordinates | are the atoms where carbon can be? | `geom.*` |
| **physics** | a relaxation | is it a minimum, and how deep? | `strain.over` |

Why these words and not the five I first picked:

- **`geometry` and `physics` are already the spec's words** (§13 names the
  "geometry tier" and the "physics tier"). Renaming them `shape`/`energy`
  is churn that the codes themselves would contradict, since they stay
  `geom.*`.
- **`bond` was a bad name.** It collides with hexfold's attachment verb
  `bond` (§30: "`bond`/`fuse`/`seam` as the attachment verbs") and with
  se's `kind='bond'` connects. "The bond check warns about a bond" is a
  trap for any agent reading a skill. `valence` is the existing namespace.
- **`topology` beats `count`** — it names what is checked rather than the
  mechanism, and `GeneratedBlock` already has a `topology` field, so the
  vocabulary is shared rather than parallel.
- **Do not call these "tiers".** `docs/glossary.md:51` reads "**fidelity
  ladder** (legacy: tier ladder)" — this repo already retired "tier" as
  the word for a cost ladder once. Reviving it mints a second vocabulary
  for one idea. They are check *classes*; the fidelity ladder is the
  separate thing `physics` escalates along.
- **DRC is not rejected — it is taken.** `src/precis_se/drc.py` is 644
  lines of "Graph-tier design DRC" backing `view='drc'`, the se mechanical
  layer's pre-geometry checks. The honest statement is "DRC stays the
  mechanical-layer name; these are the chemistry checks", not "we decline
  the EDA term". The first draft's argument that DRC is an invented
  abbreviation was wrong on both counts.

### `conj.*` is a rule namespace, not a class of its own

It has the same input (atoms, bonds, hyb) and the same cost (one linear
graph walk) as `valence`, so it does not earn a separate class. It also
needs a stated limit: **reachability over sp²-labelled atoms is a
necessary condition, not a sufficient one.** A three-bonded but heavily
pyramidalised carbon is labelled sp² and is not conjugated in practice, so
`conj.*` can prove a patch is *not* isolated and cannot prove it *is*.

### "No coordinates" was wrong; "no metric" is right

The first draft claimed `topology` and `valence` need no coordinates. They
need no *metric*, but ring enumeration needs an **orientation**: hexfold
traces faces through a rotation system with turn angles from dart
directions (`defects.py:355-380`), and the dual route's rings come from the
mesh's oriented vertex stars (`dual.py:86-91`). A bare `(atoms, bonds)`
graph has no rings.

This has a concrete consequence the first draft missed: **`topology`
cannot run on any generator's output today.** `unroll` returns
`(coords, bonds)` and discards rings (`dual.py:268`), and
`GeneratedBlock.topology["rings"]` is a *histogram*, not membership
(`tpms.py:340`). `geom.angle.dev` is blocked the same way — it iterates
`net.rings` (`check.py:242`). So ring membership is a missing channel, not
just a missing call.

`count`'s codes are also more hexfold-specific than the first draft
implied: `euler.residual` needs `b_expected` from ports/term_rims/seam_rims
(`check.py:108`), and per-sheet χ needs `net.sheet_atoms`. A periodic net
has no rims and no B term — its counting law is Σ(6−n)Pₙ = 6χ against
`welded_euler`, which tpms already computes separately (`tpms.py:280`).

## Plan

Ordered so the live defect dies first and each step changes something a
caller can see.

1. **tpms refuses or derives on implied bond length** (gripe 451269 a).
   A few lines in `_validate_params` (`tpms.py:130`). Stops 0.248 Å carbon
   being generatable *today* rather than after an architecture change.
2. **Per-family `_bond_lengths` assert for tpms.** Five lines, using the
   helper the same test file already applies to cnt, fullerene and cone
   (`tests/test_se_atomic_generators.py:145,255,324`). The first draft
   dismissed this as "pinning the symptom" in favour of a structural fix;
   that was a false choice — it would have caught 451269 on the day it
   shipped, and it costs nothing to keep.
3. **`check_block(block, profile)` called in `generate.py` before
   `add_block`**, over `(coords, bonds, elements, hybridizations)`,
   emitting `valence.*` + `geom.bond.*`, with a **findings field added to
   `GeneratedBlock`** and surfaced in the handler echo. This is the actual
   fix, and it covers every family at one site.
4. **Ring-membership channel** so `topology` and `geom.angle.dev` can run
   at all: `unroll` and `GeneratedBlock.topology` must carry membership,
   not just a histogram.
5. **Edge-length term in `remesh`** (451269 c). The real work: at the
   corrected `cell_A` the mean is 1.420 Å but the spread is 0.726–2.373 Å,
   because the tangential smoothing equalises valence only.
6. **Per-fidelity tolerance profiles.** `bond_tol_A=0.10` will never be met
   by an unrelaxed scaffold — `tpms.py:48` says bond lengths carry real
   spread by design. Without a scaffold-vs-stick tolerance split, `STRICT`
   is permanently unusable for this family. Not optional.
7. **Parameter ergonomics** (451270) — family aliases, all missing required
   params reported at once.
8. **`conj.*` plus the sp³ isolation band, together.** Not band-then-check
   and not check-then-band: no net today has sp³ sites except via `bond`
   attachments, so every net is one conjugated component and a `conj.*`
   checker would have no positive fixture. The band supplies the fixture.

Cut from the first draft: profiling se `drc` slowness (that is gr450524, a
different subsystem — `precis_se/drc.py`, not `hexfold.check`); the seam
catalogue (it contributes `port.mismatch`/`fit.unsolvable`, existing codes,
and is related reading rather than a step); and the `physics` rungs
(blocked on §29 Q5 — `strain_max` is unset).

DFT stays an escalation, not a rule: hours per region, run on a small
extracted patch, so it needs step 8 to define and validate the cut. That
part of the first draft survives.

## Open questions

1. **Two checkers, non-overlapping remits, no shared vocabulary.**
   `hexfold.check` owns chemistry rules; `precis_se/atomic/validate.py` is
   a different checker (envelope fit, port frames, rotation mismatch,
   cycles — the design↔atomistic seam) and has its own
   `bond_length_sanity` at metre scale (`:609`). That split is the
   condition that let 451269 through. Merge, or make one delegate?
2. Does `topology` keep §6.3's per-sheet χ framing once the seam catalogue
   lands multi-atom seam motifs? §6.3 assumes one seam atom per period.
3. **Missing classes with no home:** periodicity consistency (shifts,
   welded χ vs unrolled χ, rim-port count = dropped bonds — checked by
   hand in `test_tpms_p_bond_count_is_3_over_2_atoms_minus_rim_deficits`);
   declared-vs-derived bond order (tpms asserts 4/3 aromatic on every bond
   by fiat, `tpms.py:305`, unchecked against hyb); non-carbon elements
   (`valence.over` is `4 if sp3 else 3` with H skipped, `check.py:50`, and
   the band item adds F); and **synthesisability**, absent entirely.

Resolved, not open: whether `geometry` should gate on mean or spread.
`_geometry_findings` is already per-bond and `geom.summary` carries
`bond_max` (`check.py:282`) — gate on max under `STRICT`.
