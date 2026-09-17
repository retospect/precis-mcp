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

**Open — the `bound` half:** the enum value, CHECK and render exist, but
nothing writes `pose_source='bound'`. Natural writer is `bind_structure`
(it already resolves each port to an atom; the atom's block-local
coordinates are the pose). Reto's call: does a bind overwrite a
`declared` target, or refuse and file a mismatch finding? Waits on
per-state `bind_structure`. Intervals on a target ("10–12 Å") are
Decision 3's requirement, not the port slot.

## Decision 2 — Δ-length facts stay in the star schema

Unchanged from §Slice 4: Δ-distance, PSS conversion, τ½, strut stiffness
are sourced rows with conditions + citation on what the block is
`realized-by`, never denormalised onto the block. A port target is a
*requirement*; the star-schema row is the *fact*; ranked search compares
the two.

## Decision 3 — a requirement is a box with ports and interval constraints

Declare a block with two ports and a state pair; on the transition put
ranges: Δ between ports ∈ [10, 12] Å, span ∈ [40, 50] nm, stimulus
`light`, bistable preferred, cycles ≥ N. Declared intent only.

## New item — composition proposer (se atomic `propose` mode)

Reads slice 4's rows and enumerates compositions that satisfy the
requirement: series of n switches + spacers (n·Δ_unit ∈ Δ-range,
n·L_switch + spacers ∈ span-range), and lever/hinge families once ports
carry rotation. Small integer enumeration. Output = slice 4's response
shape extended to compositions, ranked, never empty:

    3 × azobenzene in series + 2 × spacer: Δ 10.2 Å (8.2 Å at PSS 80 % cis)
    span 44 nm · opto ✓ · bistable ✗ (T-type, τ½ 2 d) · CuAAC ✓

A chosen row instantiates as an se tree of library instances joined by
complementary click ports (slice 3 makes azide↔alkyne legal and
azide–azide refused); unit states compose into chain states the sweep
view already enumerates; selection reuses quest's rubric machinery with
human-set weights. DRC (slices 5/6) then runs on the composed tree.

**Must surface, never hide:** PSS conversion < 100 % (expected Δ =
n·Δ·p_cis, shown per row); a 40–50 nm span exceeds rigid small-organic
struts, so the spacer library (DNA / peptide / OPE rods with stiffness
rows) decides whether a series stroke survives.

### Design — enumeration over slice 4's rows (2026-09-17, unshipped)

**Surface.** `search(kind='se', wants={...}, compose={...})` — a second
dict kwarg beside `wants=` (same rationale: the vocabulary is star-schema
data, not a flat signature). NOT `se_propose_atomic`: that job
(`src/precis_se/atomic/propose.py`) is the tool-less LLM fragment fill
for ONE block, and `se_propose` is reserved for the whole-design LLM
proposer; this proposer is a deterministic enumerator, so it lives in
`precis_se/library.py`'s read path, not a job. `compose` is the Decision-3
requirement box until transitions carry ranges: `{delta: [lo, hi]` (Å,
port-to-port stroke), `span: [lo, hi]` (nm, long-state length), `n_max?`
(default 6 switches), `m_max?` (default 4 spacers)`}`. When Decision 3
ships, `compose='<design>#<block>'` reads the same box off that block's
declared transition ranges.

**Per-unit facts** come through `resolve_block_attrs` with these property
keys (minted `proposed`-tier on first write per the material skill — no
migration): `delta_length` (Å, long→short state Δ end-to-end),
`unit_length` (nm, long-state port-to-port), `pss_short_fraction` (0–1,
conditions carry the wavelength), `thermal_half_life` (s),
`persistence_length` (nm). A block with a `delta_length` row is a switch;
a block with `unit_length` and no `delta_length` is a spacer. Blocks
with neither are skipped and counted in the header ("N blocks carry no
length facts — put(kind='material', property='unit_length', …)").

**Enumeration.** For each switch S, n ∈ 1..n_max, each spacer P (or
none), m ∈ 0..m_max: Δ_ideal = n·Δ_S; Δ_pss = Δ_ideal·p_short (only when a
`pss_short_fraction` row exists; else Δ_ideal with a "PSS unknown" mark);
span = n·L_S + m·L_P. Cap at 2 000 compositions, largest n first is
NOT the order — feasibility is. Each composition is scored exactly like a
slice 4 row: synthesize value rows `{value_num: Δ_pss}` / `{value_num:
span}` and run them through `_match_value_row` against the box's
intervals; `wants=` keys (stimulus, bistable, joining, any star key)
evaluate on the switch block and pass through unchanged. Rank with
`rank_rows`' order (score, Pareto frontier on the two miss distances,
Σ distance, id). Never empty: with no feasible composition the nearest
misses show with their distances.

**Must surface, per row:** `Δ 10.2 Å (8.2 Å at PSS 80 % cis)`;
`bistable ✗ (T-type, τ½ …)` from `thermal_half_life`; `floppy: span 44 nm
> Lp 15 nm (OPE)` whenever span exceeds the spacer's `persistence_length`
(or `stiffness unknown` when the spacer has no row); `joining` from the
switch↔spacer port roles (slice 3's complementarity). Next-line = the
`instance_block` × n + spacer ops script joining alternating units via
complementary ports.

**Tests** (tests/test_se_library_compose.py): enumeration arithmetic incl.
PSS scaling and the "PSS unknown" mark; never-empty nearest-miss; the
floppy flag and the stiffness-unknown mark; a block without length rows is
skipped and counted; `compose=` reaches the handler over the MCP door
(the verb signature IS the schema, memory `mcp_verb_kwarg_silent_drop`);
`wants=` keys still score on the switch block.

## Order

1. Port pose slot (decision 1) — before slice 4 or a consumer bakes in
   direction-only semantics (`nm-stick-placement.md`,
   `structural-solution-space.md` slice 5).
2. Slice 4 ranked search, with the star-schema shape (decision 2) —
   **SHIPPED** 2026-09-17, `search(kind='se', wants={...})`; see
   blocktree-library-build-plan.md §Slice 4's shipped note for the
   `wants=` shape, join order and the structure-bound-block gap it leaves
   open.
3. Composition proposer.

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
  10.1021/cr990257j) — minted 2026-09-17, fetching; cite their chunks
  once landed. The spacer stiffness rows in the example above stay
  illustrative until then.
