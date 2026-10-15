---
status: draft
pillar: 3d-design
title: complementarity solver — open: screw-clamp must_contact wiring
prio: high
---

# Complementarity solver — open remainder: screw-clamp must_contact wiring

All three slices shipped: the active-set core e49fb80a
(`src/precis/structsolve/complementarity.py` — `solve_complementarity`,
`check_completeness`, `probe_bistability`,
`tests/test_structsolve_complementarity.py`) and the se side 2026-10-13
(`src/precis_se/stability.py` — `load_path_report` renders the taut/slack
`## load path` section of `view='stability'`, `completeness_findings` is
the warn-first `completeness` rule in `view='drc'`/`validate`,
`probe_member_bistability` behind `args={'bistable': ...}`). `git log`
is the record. Mechanics stay owned by
`multiscale-design-architecture.md` §Complementarity.

## Still open: screw-clamp must-contact wiring

A screwed/bolted clamp is the canonical must-contact producer (Reto,
2026-09-12): the fastener is a tension member carrying preload, the
clamped faces are its complementary compression-only/must-contact pair
carrying the balancing compression. `seated` while service tension <
preload; service load exceeding preload drives the face force to zero →
`unseated` = joint separation. The shipped closed-form rung-5
separation check is the two-member special case.

The se bridge does not wire this: se has no `must_contact` vocabulary
(no axial param selects the idiom), no `screw`/`press` connect is
decomposed into the member pair, and the `unseated` verdict is
unreachable from se today. That wiring is what makes the general solve
and the closed-form check agree by construction.

## What a re-spec needs (no design exists yet)

- **Param name** — the axial-member param that selects the must-contact
  idiom, named per the sign-idiom vocabulary in
  `multiscale-design-addendum-a.md` A6 (tension-only, compression-only,
  bidirectional, spring, hard stop).
- **Decomposition rule** — how a `screw`/`press` connect (`joints.py`
  preload, N tension-positive) decomposes into the fastener-tie +
  face-stop pair, and where the decomposition lives.
- **Acceptance test** — the closed-form rung-5 separation check and the
  general solve agree on a two-member screw clamp.

Bounds (unchanged): normal-gap complementarity only — no dynamics,
friction, sliding/rolling contact, cable sag, 3D gap geometry (cad owns
that), overturning/contact-patch-escape, or an nm bridge. No solver
tunes its own objective; an infeasible complementarity problem is a
refusal with cause, never a silent least-squares answer.
