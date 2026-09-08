---
status: draft
title: nm and se are the same block-tree twice — extract a shared core
prio: normal
model: opus
---

# `nm` ↔ `se`: one abstraction, two forks

> **Scope, settled (Reto, 2026-09-07): `nm` + `se` only.** `pcb` is out — it
> owns a whole circuit domain the others do not touch. `structure` and `cad`
> are out — `nm`/`se` *rent* them, they do not duplicate them.


Found 2026-09-07 answering "can these kinds be consolidated?". The answer
for most of the family is **no, the splits are principled**. This is the one
exception, and it is the only overlap in the family with no item tracking it
(the three star schemas already have `material-component-shared-core.md`).

## The measurement

`precis_nm/ops.py` (869 lines, 36 top-level symbols) and `precis_se/ops.py`
(1329 lines, 52 symbols) share **26 symbol names** — 72 % of nm's surface:

```
ConnectSpec · OpError · PortSpec · apply_ops · effective_envelope ·
effective_ports · _as_vec3 · _connects_endpoint_pair · _descendants ·
_expands_to · _find_instance_cycle · _no_block_msg · _op_add_block ·
_op_add_port · _op_connect · _op_disconnect · _op_instance_block ·
_op_remove_block · _op_remove_port · _op_set_pose · _opt_str ·
_require_name · _resolve_connect_port · _split_endpoint · _unit_vec ·
_validate_envelope
```

`persist.py` is the same retire-all/reinsert-all shape in both (345 vs 542).

**There is no code dependency between them** — `se` imports nothing from
`precis_nm`. The only references are docstrings, and they state the fork
outright: *"transferred whole"* (`se/ops.py:29`, `se/persist.py:3`),
*"scaffold verbatim"* (`se/__init__.py:5`), *"Mirrors `precis_nm.ops`'s
discipline"*. So this is a known, deliberate copy, not accidental drift —
which makes it a *decision to revisit*, not a mistake to shame.

## Why it is the same abstraction

Both are: a recursive block tree (`parent_block_id` / `template_block_id`)
with envelopes expressed in the **cad DSL**, typed ports, port-to-port
connects, instancing with cycle guards, and SDF-based validation — renting
`precis.cad`'s kernel as a unit-agnostic float64 geometry library, never the
`cad` *kind*.

The repo already says so. `precis_se/__init__.py` and `se-kind.md`:

> The symmetry that locates this kind: **se : cad :: nm : structure.**
> nm is intent-over-atoms renting the cad kernel as Å; se is
> intent-over-solids renting the same kernel as metres.

Documented as siblings, implemented as strangers.

## What actually differs

Small, and cleanly separable from the shared spine:

- **unit** — Å vs m (one constant; the Å↔m boundary is already declared once,
  `se-kind.md`: "1 Å = 1e-10 m exact").
- **L2 vocabulary** — nm has mechanical *threading*; se has *arrays*,
  *joints*, *measures*, and a `se_bom` placement rollup.
- **bindings** — nm's L5 binds to a `structure` design (`bound_design` /
  `bound_atom`); se's leaves bind to `component` via `realized-by`.

## Two options, and the evidence picks the second

**Measured divergence** of the 26 shared symbols (AST body comparison, not
name matching): **5 byte-identical**, 12 near (0.77–0.92), **9 genuinely
diverged** — `_op_instance_block` 0.11, `effective_envelope` 0.27,
`effective_ports` 0.32, `PortSpec` 0.29.

That number kills the naive reading. This is *not* 1500 lines of copy-paste:
the spine has drifted precisely where the semantics got interesting, so a
subset extraction would capture the five boring helpers and leave every
valuable divergence forked.

### Option A — shared core (extract the common subset)

Pull out what agrees; each kind keeps its own everything else.

- **Captures** ~5 identical helpers cleanly, ~12 more with parameterisation.
- **Leaves forked** the 9 diverged symbols — i.e. all the parts that matter.
- **Risk** low; no behaviour change.
- **Verdict** poor value. It de-duplicates `_as_vec3` and leaves
  `effective_ports` in two places.

### Option B — superset (RECOMMENDED)

Read the divergences and the direction is one-way: **`se` is a superset of
`nm` almost everywhere it differs.**

| symbol | what the divergence actually is |
|---|---|
| `effective_ports` | se = nm's two sources **plus a third** (catalog port templates from a bound `component`), merged per-name, own-wins |
| `_op_instance_block` | se is 9 L because it already factored `_instance_shared` + `_commit_instance` to support `array_block`; nm's 45 L are inline because it never needed arrays |
| `PortSpec` | shared: `name`/`roles`/`direction`. se adds `annotations`, described in its own docstring as *"the open dict over the future **superset** registry"*. nm adds `expected_element`/`expected_hybridization` — which are exactly what that dict is for |
| `effective_envelope` | se 20 L vs nm 10 L — se resolves array/instance cases nm lacks |

So `se` has *already been generalising toward the superset*, and its author
said so in a docstring. nm's genuine extras are two: chemistry-flavoured port
expectations (annotation-shaped), and L2 mechanical threading.

**The unifying observation: nm's "bind to a `structure` design + atom label"
and se's "bind to a `component` + inherit its catalog ports" are the same
concept.** A block leaf resolves to a realisation in some other kind, and
inherits ports from it. Make *binding* pluggable and the two kinds stop being
different shapes.

Proposed shape — a `blocktree` core owning: the recursive tree
(`parent`/`template`), instancing with cycle guards **and** arrays, ports
with `roles` + `direction` + open `annotations`, connects, envelope
validation over the cad SDF kernel; parameterised by

1. **unit** (Å vs m — one constant; the 1 Å = 1e-10 m boundary is already
   declared once in `se-kind.md`),
2. **binding provider** (structure+atom vs component+catalog),
3. **L2 op vocabulary** (nm threading; se joints/measures/BOM).

nm's `expected_element`/`expected_hybridization` migrate into `annotations`
under the descriptive tier, which is where se's design already puts
not-yet-enforced keys.

### What this is NOT

**Still not a kind merge.** `nm` and `se` stay two kinds with two tables and
two dark-gate settings. A nanomachine block and a building truss are the same
*IR* and different *domains*; collapsing them would break one-word-one-meaning
the same way merging `component` (procured) into nm's `block` (made) would —
a distinction `nm-kind.md` already refused to give up. The superset is of the
**implementation**, not the vocabulary.

## The fact that settles it: none of them have ever been used

Measured on prod, 2026-09-07:

```
nm_blocks 0 · nm_ports 0 · nm_connects 0
se_blocks 0 · se_ports 0 · se_connects 0 · se_bom 0
pcb_boards 0 · pcb_instances 0 · pcb_nets 0
cad_nodes 101 · struct_atoms 793,553
```

**Every block-tree kind is empty.** All real work lives in `structure` and
`cad`. So this is not a refactor of a running system — it is three
speculative implementations of an overlapping abstraction with zero users
between them, and (Reto, 2026-09-07) **no backward-compatibility constraint
and no data worth keeping.**

That removes every reason the "extract carefully, keep both green" plan
existed. There is nothing to migrate, no dual-write window, no user to break.

### Revised recommendation: consolidate to ONE, don't build the superset yet

The superset design in Option B is sound, but building a
unit-and-binding-parameterised core to serve **three kinds that have never
been used** is speculative generality — the same mistake three times over,
one abstraction layer up. Two real users are what justify a parameterised
core; right now there are zero.

So:

1. **Pick one implementation and delete the others.** `se` is the more
   evolved (arrays, annotations, catalog binding, the factored
   `_instance_shared`/`_commit_instance`), so it is the better base. Take
   its machinery whole.
2. **Keep the kinds that earn their vocabulary, drop the code duplication.**
   Whether `nm` survives as a thin domain layer over that machinery, or is
   deleted until a real nanomachine design is wanted, is a judgement call —
   but it should not be a second 869-line ops module either way.
3. **Generalise on the second real user, not before.** When something is
   actually built in one of these kinds and a second domain genuinely needs
   the same tree, the parameterisation points are already identified above
   (unit · binding provider · L2 vocabulary). Write it then, informed by a
   working example instead of two guesses.

**`pcb` stays its own thing — decided (Reto, 2026-09-07).** The reason is
domain content, not geometry. `pcb` carries an entire circuit vocabulary that
exists nowhere else in the family — nets, netconns, pins, copper, planes,
footprints, DRC findings — across 11+ dedicated tables. It also imports
nothing from `precis.cad` (verified), so it is not even a cad consumer: 2.5D
copper and connectivity, not 3D CSG over solids.

Only its *instancing* rhymes with the block tree, and an instancing pattern is
far too thin a thread to pull a netlist-shaped kind into a solid-shaped one.
Not in scope; do not re-litigate.

## What was actually built (2026-09-07) — and why it is not the deferred superset

The phase-1 agent flagged a real tension: this doc recommends *not* building a
parameterised core yet, and then a core got built. Resolving it explicitly,
because a design doc that contradicts the code is worse than either.

**Deferred, still deferred:** a *superset* — one implementation whose **unit**,
**binding provider** and **L2 vocabulary** are parameters chosen at
configuration time, designed to serve domains that do not exist yet. That is
speculative generality for zero users and remains the wrong move.

**Built instead:** a *spine* — `precis.blocktree`, extracted from duplication
that already existed, with exactly one extension mechanism (`make_block` /
`make_connect` factory hooks) so a domain's ops mint the domain's own
subclass. It is unit-agnostic because it simply never mentions a unit, not
because a unit is a parameter. Nothing was designed for a hypothetical third
domain; the whole justification is the two forks measured above.

The distinction that matters: **deduplication is justified by the duplication
you can measure; parameterisation is justified by users you can name.** The
first is here today, the second is not.

Landed as `28877919`: core 724 lines (`types.py` 111, `ops.py` 580);
`se/ops.py` 1329 -> 962; ruff/mypy/`se` tests/`--impacted` (826 passed) all
green; every user-facing error string copied verbatim.

One naming note for whoever reads the code: the core type is `BlockNode`, not
`Block`. `tests/test_vocab_lint.py` reserves the bare name `Block` for
`precis.utils.prompt.model` and says to coin a different name rather than
allowlist — so the lint chose this, not taste.

## Sequencing / risk

Near zero, given the counts above: no data, no users, no compatibility
constraint. This is a delete-and-rewrite, not a migration. The only real
cost is the sunk design thinking in the two `__init__.py` IR docstrings,
which should be preserved into whichever implementation survives — those
level-ladder invariants are the valuable part, not the code.

## Outcome (both phases done, 2026-09-07)

`28877919` (se) and `96690d37` (nm). Both green: ruff, mypy over 30 files,
`nm`/`se`/blocktree suites, `--impacted`.

    before   nm/ops 869 + se/ops 1329                    = 2198
    after    nm/ops 659 + se/ops  962 + core 724         = 2345

**Total line count went UP by ~150, and that is the honest result.** The win
was never fewer lines — it is that the spine now exists **once**. Instancing,
cycle guards, port resolution and the connect/disconnect cascade had two
independent implementations that could drift; they now have one. The extra
150 lines are the core's own module docs plus thin delegation at each domain
— the price of the seam, paid once.

Deleted from the domains: 577 lines of duplicated spine. `set_pose` and
`disconnect` turned out byte-identical in both and are now consumed straight
from `CORE_OPS` with no wrapper at all.

What deliberately did NOT change: nm keeps `expected_element` /
`expected_hybridization` / `bound_design` / `bound_atom` as real fields rather
than folding them into the core's `annotations` dict, and nm's `kind` slot
stays independent of se's `joint`. Both are semantic changes, and a refactor
that changes semantics cannot be verified by "the tests still pass".

### Known wart: `BlockNode.ports` is not generic over its port type

`Tree` is generic (`Tree[TBlock, TConnect]`); `BlockNode.ports` is plain
`dict[str, Port]`. `se` never noticed — its `PortSpec` is a bare alias of
`Port`. `nm`'s genuinely diverges, so `NmBlock.ports: dict[str, PortSpec]`
needs a scoped, documented `# type: ignore[assignment]` (dict is invariant, so
mypy is right to flag the narrowing).

**Left as-is deliberately.** Widening the core to `BlockNode[TPort: Port]` is
a generic-hierarchy change for exactly one caller — the same
speculative-generality trap this doc argues against elsewhere. One documented
ignore beats a parameterised hierarchy serving one user. Revisit only if a
third domain hits it, at which point the pattern is established rather than
guessed.

## Not in scope (checked, and these splits are fine)

- **`nm` into `structure`** — no. `nm` owns L0–L4 (envelopes, ports, DOF,
  threading) and *creates* `structure` designs; `structure` owns L5 fill and
  the relax ladder, which `nm` explicitly refuses to reimplement
  (`nm/job.py` rejects a proposed `relax` op). `structure` is a flat
  atom/bond pair with no parent or instance concept — merging means either
  giving every catalysis slab a block hierarchy it will never use, or
  flattening away the L0–L2 IR that is nm's whole reason to exist.
- **`pcb` into `cad`** — no; verified disjoint, `precis/pcb/` imports
  nothing from `precis.cad`. 3D CSG vs 2.5D copper/nets.
- **The four `view='bom'` paths** — acceptable. They are four *views* over
  one price source: `se_bom` and `cad` both read `component`'s spec values
  rather than storing their own (mig `0003_se_bom.sql`: "nothing of that
  rollup is reimplemented").

## Related gap worth its own look

**Containment is expressed two incompatible ways across this family.**
`cad` and `component` use inter-ref `links` rows (`contains`/`part-of`);
`nm`, `se` and `pcb` use an intra-ref FK column (`parent_block_id`,
`pcb_instances`). Consequence: there is **no single "what contains what"
query** across the family, and `nm`/`se` block trees are invisible to the
links graph entirely. That may well be the right trade — a links row per
block would be heavy — but it is undocumented, and `docs/codebase.md`
currently says nothing about this kind family at all.
