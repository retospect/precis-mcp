---
status: draft
title: port pose slot + requirement-driven composition search ("10–12 Å over 40–50 nm")
prio: high
model: opus
---

# Port pose slot + composition search

Reto, 2026-09-16, settling gr342026 and extending
`blocktree-library-build-plan.md` §Slice 4. Three decisions, one new
design item.

## Decision 1 — ports get a pose slot, on the port, as a *target* — **SHIPPED**

Ruling (gr342026): **put it on the port**, mirroring the block's own pose
— an origin and a rotation, so a state's `port_pose_overrides` is a
**rigid delta in the block frame** (translation + rotation), not
direction-only. Delta, not absolute, because it stays meaningful when the
port's own pose is unknown ("the far port moves 9 Å along x").

Shipped 2026-09-17 (declared half): `Port.pose`/`rot`/`pose_source`
(`precis.blocktree.types`, enum `PORT_POSE_SOURCES = declared | bound`,
`None` = unset); `add_port pose= rot=` and the new `set_port_pose` op
(rot without an origin refused, scalar refused); se columns
`se_ports.pose_xyz/pose_rot/pose_source` with CHECKs
(`0011_se_port_pose.sql`, metres/radians, block-local frame);
`port_pose_overrides = {port: {'direction'?, 'pose'?, 'rot'?}}` applied
at get time only when the port carries a pose (sweep resets it per
combo); a `pose` column on `view='block'|'ports'` only when some port
has one; `bond_length_sanity` uses the port-to-port world distance when
both ends carry a pose and says which source each came from, else the
envelope approximation and says so.

**Why rotation too:** a rotation at a hinge port × arm length is a
displacement at the far end. Without the angle the solver below can only
find series stacking; with it, levers/cranks/scissors are solutions.

**The `bound` half — SHIPPED 2026-09-18.** `bind_structure`
(`src/precis_se/atomic/bind.py::_measure_port_poses`) is the writer: each
mapped port takes the block-local position of the atom it resolves to as
its own `pose`, stamped `pose_source='bound'` (Å→m through
`validate.bound_port_origin`, the one enclave crossing).

Reto's call, settled: **a measurement fills an empty slot but never
overwrites a `declared` target.** Decision 2's split is the reason — the
declared pose is the *requirement* the realization gets checked against,
and a bind that silently restated it would destroy the only record of
what was asked for. The disagreement is reported instead, twice: an echo
line at bind time, and the standing `port_pose_mismatch` warn finding on
every later read (a bound structure can be edited under a live binding
long after the bind returned). Threshold `PORT_POSE_MISMATCH_FRACTION` =
a quarter of the block's own envelope diagonal, the same governing-length
convention `bond_length_sanity` uses.

Three edges, all pinned in `tests/test_se_atomic_bind.py`: a frame
mismatch (`envelope_fit`'s `FrameMismatch`) measures **nothing** — those
coordinates are in another frame; a re-target to a different design drops
the measurements it no longer speaks for; `unbind_structure` drops the
measured poses and keeps the declared ones. `rot` is never written — an
atom has a position, not an orientation, so a bound *rotation* still
waits on a real frame (lever/hinge families, below). Migration `0013`
restates the column comment 0011 shipped saying there was no writer.

Not in this: per-state `bind_structure` (a bound pose is measured against
the block's current realization, not per declared state), and intervals
on a target ("10–12 Å"), which are Decision 3's requirement box, not the
port slot.

## Decision 2 — Δ-length facts stay in the star schema

Unchanged from §Slice 4: Δ-distance, PSS conversion, τ½, strut stiffness
are sourced rows with conditions + citation on what the block is
`realized-by`, never denormalised onto the block. A port target is a
*requirement*; the star-schema row is the *fact*; ranked search compares
the two.

## Decision 3 — a requirement is a box with ports and interval constraints

Declare a block with two ports and a state pair; on the transition put
ranges: Δ between ports ∈ [10, 12] Å, span ∈ [40, 50] nm, stimulus
`light`, bistable preferred, cycles ≥ N. Declared intent only — nothing
in validate/drc/clearance reads it; the only consumer is `compose=`.

**Storage — a `requires` column, not a `params` key.** `params` is the
realization's per-driver numbers (quantum yield, barrier, the 436 nm PSS
on `se:azo-unit`); a requirement is the target the realization is checked
against. Same declared/bound split the port pose slot made: a reader must
be able to tell a measured figure from a wanted one without a naming
convention inside one JSON blob. Core migration `0167` adds
`design_transitions.requires jsonb NOT NULL DEFAULT '{}'` (forward-only;
0162 is sealed). `precis.design.states.Transition` gains `requires:
dict[str, Any]` (default `{}`), written by `set_transitions`, read by
`transitions_for` (`_TRANSITION_COLS`).

**Write path.** `declare_transitions` entries take an optional
`requires` object, vetted at op time by one shared vetter
(`precis_se.compose.parse_requires`, imported locally inside the op —
`ops.py → compose.py → library.py → ops.py` cycles at module level) so a box that fails here fails the
same way `compose=` would: `delta` `[lo, hi]` Å, `span` `[lo, hi]` nm,
`n_max`, `m_max` (the compose box keys, same `_range`/`_count` rules), plus
any *wants* key in the `parse_wants` value shapes (scalar, `[lo, hi]`, or
`{target, min, max, tol, weight}`) — `bistable: True`, `cycles: {'min':
1000}`. `stimulus` is refused with a pointer: the stimulus IS the
transition's `driver_kind`. An empty object and an absent key both mean
"no requirement". Rendered in `get(kind='se')`'s `## transitions` table as a
`requires` column (`—` when empty).

**Read path — `compose='<design>#<block>'`.** `<design>` is the design's
slug — `se` is a slug-only kind (`KindSpec.is_numeric=False`), resolved
exactly as `get(kind='se', id=)` does through `store.get_ref(kind='se',
id=slug)`; there is no numeric handle form to accept. The block resolves
through `Tree.resolve_key` (name or `#<uid>`) on `persist.load_tree`;
`<design>#<block>/<from>-><to>` names one transition. The transitions of that block carrying a non-empty
`requires`: exactly one → that box; none → `BadInput` pointing at
`declare_transitions … requires=`; several without the `/<from>-><to>`
segment → `BadInput` listing the addressable edges. Box keys become the
`ComposeBox`; the remaining keys become `wants` entries; `stimulus` is
added as a wants key from `driver_kind` unless the caller's `wants=`
already carries one (derived, so explicit wins). A caller `wants=` key
that collides with a declared `requires` key is the existing clash
`BadInput` ("the block's requirement owns that key") — an override is a
re-declare, not a search-time argument. The rendered header names the
source: `box from se:<slug>#<block> <from>-><to> (<driver_kind>)`. The
`search` verb's `compose` annotation widens to `dict | str` at
`tools/core.py` and `SeHandler.search`.

Tests: `tests/test_se_block_states.py` (requires round-trips through
edit→get; `stimulus` key and a malformed range rejected at op time with
the op name in the message; the whole edit rolls back) and
`tests/test_se_library_compose.py` (string form resolves and scores the
same as the equivalent dict; zero-box and multi-box refusals; the
`/<from>-><to>` selector; derived stimulus; the collision rule; MCP door
carries a string `compose`). Skill `precis-se-help`: the
`declare_transitions` bullet gains `requires?`, and the compose paragraph's
"not shipped yet" sentence becomes the string form's usage.

## Composition proposer — **SHIPPED** 2026-09-17 as `compose=`

`search(kind='se', compose={delta: [lo, hi] Å, span: [lo, hi] nm, n_max?,
m_max?}, wants=…)` — `src/precis_se/compose.py`, a deterministic
enumerator on slice 4's read path (NOT `se_propose_atomic`, the per-block
LLM fragment job; `se_propose` stays reserved for the whole-design LLM
proposer). n switches + m spacers, scored like a slice 4 row through the
same `_match_value_row`/`order_rows`, never empty; per-unit facts are the
proposed-tier properties `delta_length` (Å), `unit_length` (nm),
`pss_short_fraction`, `thermal_half_life` (s), `persistence_length` (nm)
resolved through the star schema. Every row surfaces the PSS-scaled
stroke (or `PSS unknown`), the T-type verdict with τ½, `floppy` /
`stiffness unknown` against the persistence length, and the
switch↔spacer port complementarity; the Next line is the
`instance_block` × n + `connect` ops script. Skill H2 "Composition
proposer"; tests `tests/test_se_library_compose.py`.

**Left open:**

- **Seed the prod facts** — azobenzene and dsDNA **DONE 2026-09-18**;
  the OPE rod is still open. `material:azobenzene` carries
  `delta_length` 3.5 Å (trans 9.0 → cis 5.5), `unit_length` 0.9 nm,
  `pss_short_fraction` 0.8 at 313 nm and `thermal_half_life` 172 800 s;
  `material:dsdna` carries `unit_length` 0.34 nm/bp and three sourced
  `persistence_length` rows (42.5 (30–55) nm review band, 49.89 low
  salt, 33.16 at 250 mM NaCl). Each hangs off a one-block design linked
  `made-of` it — `se:azo-unit` (the switch) and `se:dsdna-bp` (the
  spacer). **Two designs, not one mixed design:** a design-level
  `made-of` link is scoped to a block only through the link's
  `meta.block`, and no verb writes that (`link()` has no `meta=`), so a
  mixed design would resolve every block to whichever material the
  first link named. The OPE rod waits on stubs pa345576/pa345577, which
  still have zero body chunks.
- **A multi-row material property resolves by write order** — gr346735.
  `_material_hit` takes the first row and the store orders
  `created_at DESC`, so the honest spread the `material` kind invites
  (several conditions, several sources) collapses to the newest sample
  with nothing on the rendered row saying a choice was made. Seeding hit
  this: azobenzene's 436 nm PSS row (the reset channel, ~10 % cis)
  outranked the 313 nm actuating one and would have understated the
  stroke 8×. Worked around by keeping only the actuating row on the
  material — the 436 nm figure lives on `se:azo-unit`'s cis→trans
  transition params, where its wavelength can't be lost.
- `compose='<slug>#<block>[/<from>-><to>]'` reading the box off a
  block's declared transition `requires` — **BUILT 2026-09-18** per
  Decision 3 above (migration 0167, `declare_transitions … requires=`,
  `compose.resolve_compose`); a requires box must carry `delta` and/or
  `span`, wants-only keys are refused at declare time.
- Lever/hinge families once ports carry rotation (the pose slot's `rot`
  is declared but no composition uses it).
- Selection with human-set weights through quest's rubric machinery, and
  DRC (slices 5/6) on the composed tree, are the build plan's later
  slices, not this item.

## Order

1. Port pose slot (decision 1) — before slice 4 or a consumer bakes in
   direction-only semantics (`nm-stick-placement.md`,
   `structural-solution-space.md` slice 5).
2. Slice 4 ranked search, with the star-schema shape (decision 2) —
   **SHIPPED** 2026-09-17, `search(kind='se', wants={...})`; see
   blocktree-library-build-plan.md §Slice 4's shipped note for the
   `wants=` shape, join order and the structure-bound-block gap it leaves
   open.
3. Composition proposer — **SHIPPED** 2026-09-17 (`compose=`); prod
   facts seeded 2026-09-18 (see the item above). Note the seed is
   **unexercised**: the deployed prod build predates slice 4, so its
   `search` verb has neither `wants=` nor `compose=`. The first real
   `compose=` run over these rows happens after the next `/go` deploys.

Sources for the proposer (cite-sources rule) — resolved 2026-09-17, all
held or queued in prod:

- azobenzene Δ end-to-end: pa46340~pc1629105 (trans 9.0 Å → cis 5.5 Å,
  citing the primary work) and pa40485~pc1321680 (MCBJ plateau length
  trans > cis, measured); review pa3359 (Bandara & Burdette 2012).
- azobenzene PSS ratios: pa3359~pc389953 (313 nm → ~20 % trans, 436 nm →
  ~90 % trans); a red-shifted derivative's ratios in pa44934~pc1574485.
- dsDNA persistence length: pa2832~pc309592 (Table 1, 49.9 ± 0.8 nm;
  33 nm at 250 mM NaCl) and pa1564 (Smith/Cui/Bustamante 1996, primary).
- OPE / PPE persistence length: stubs pa345576 (Cotts, Swager, Zhao 1996,
  doi 10.1021/ma9602583) and pa345577 (Bunz 2000 review, doi
  10.1021/cr990257j) — minted 2026-09-17, **still zero body chunks as of
  2026-09-18**; cite their chunks once landed. The spacer stiffness rows
  in the example above stay illustrative until then.
- dsDNA rise + the persistence-length band, both explicitly labelled in
  one table: pa49952~pc1707101 (`L_bp` 0.34 nm B-DNA, `l_p` ≈ 30–55 nm).
  This is what the seeded rows cite for the quantities pa2832's
  extracted Table 1 lost its column header for.
- azobenzene cis thermal half-life: pa51091~pc1706125 (2 days,
  unmodified azobenzene, reported secondhand from that paper's ref 43).

## Open questions / decisions log

- 2026-09-18 readiness pass on Decision 3: one blocker (the read path
  claimed an `se<N>` numeric handle form; `se` is slug-only — fixed to
  slug-only above) and one advisory (`ops.py` → `compose.py` →
  `library.py` → `ops.py` cycles at module level; the op imports the
  vetter locally — folded in above). Migration 0167 free in every
  worktree; the derived-`stimulus` carve-out and the clash rule cover
  disjoint cases (`stimulus` is refused as a stored `requires` key).
