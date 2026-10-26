---
status: idea
pillar: 3d-design
title: se modules and assemblies — parameterised definitions, stamp, tile, cap, fill, one framework from Å to 50 cm
prio: normal
model: fable
---

# se modules and assemblies

> **SUPERSEDED by [structure-workspaces](structure-workspaces.md) (Reto
> 2026-10-22).** Modularity lives in Python run in sandboxed structure
> workspaces; se keeps instances, provenance and the catalogue. What carries
> over: definitions are functions, `examples` are golden tests, `expose` is
> the manifest's `ports`, `stamp`/`cap` are calls, `decorate` is the
> `sheetnet` primitive, the Å-vs-macro table goes to the workspace skill.
> Dropped: `meta.role='definition'`, `$name`, the three refusals, `def.*`.
> Nothing below is to be built. The file stays until that spec's slice 6
> ships because `hexfold-multicell-prism.md` links it; delete it then.

Reto, 2026-10-22: "As we build higher complexity structures, we want to
define parameterized building blocks, and understand those. Ideally this
can happen in agent memory but it feels like inventing a new language. How
do we best do this? So we have these 12-fold structures, we want to tile,
cap, fill them, reuse the concept with different sizes, etc. We have anchors
and all that but do we need a more programmatic framework for modules and
assemblies? This is true atomic and macro." Standing principle: composable
tools and skills for the LLM, not a black-box builder per structure.

## Bottom line

No new language and no new kind. Three of the four layers already exist in
se; the missing one is small and declarative:

1. **Formulas live in Python generators** (`precis_se.atomic.generators.
   GENERATORS`, the typed-feature registry
   `precis_se.atomic.generators.typed_features._BUILDERS`, `precis.cad`
   catalog/`hmap` builders). A generator is `params → block`, theorem-loud
   on bad params, deterministic, tested on goldens. Anything with
   arithmetic, combinatorics or a closure law (side length from `rows`,
   `#+ − #− = 6`, cell merge groups, knurl pitch) is a generator. This is
   the only "language", and it is already Python with a deploy cycle.
2. **Composition is the existing se op table** — `add_block`,
   `instance_block` (by reference, `design#block` cross-design,
   `precis.blocktree.types.parse_template_ref`), `array_block`
   (`linear`/`polar`, `precis_se.ops._parse_array_spec`), `connect`,
   `join` (`precis_se.atomic.join`), `bind_structure`, `set_binding`,
   plus the specced `pattern-groups.md` generator (`repeat=` on the same
   op, orbit-deduped checks). Tile, cap and fill are three thin additions
   to this table, not a new op family (§Composition ops).
3. **Checks are the existing views** — `validate`, `drc`, `clearance`,
   `sites`, `catalogue`, `mold`, `stability`; generator findings;
   `hexfold.check`. Nothing here adds a checker; a definition only says
   which checks its examples must pass.
4. **New: the definition object** — an se design with `meta.role=
   'definition'` whose ops carry a `params` declaration and `$name`
   references, stamped into a consuming design by one new op (`stamp`).
   Data, in the DB, authorable live through the session MCP, replayed
   from its params ("regenerate, never patch", `src/hexfold/spec.md`
   §23.1). The pcb meta-blocks item already chose this shape for boards
   ("a block IS a pcb design with `meta.role='block'` + a ports
   declaration", `pcb-meta-blocks.md`); se gets the same shape, one
   level up, so both kinds say "definition" the same way.

Memory holds rulings, campaign evidence (me503663) and pointers to the
definitions that worked — never the definitions. A definition in prose is
not regenerable and not validated; two agents stamping it would diverge.

## Why this is not a language (the three refusals)

The LLM already authors a language: se ops JSON, typed-feature params,
`.hx` text. The definition layer adds two things to that JSON — a
`params` block and `$name` substitution — and refuses three more:

- **A value is a literal or `$param`. Never an expression.** `"rows":
  "$R"` yes; `"rows": "2*$R"` refused (`def.expression`, naming the
  generator that owns the formula, or "write one").
- **No conditionals.** Two shapes are two definitions. A definition that
  wants `if` has found a generator boundary.
- **No loops.** Repetition is `array_block`/`repeat=` (packed) or a
  generator's own list param (`cells`, fused). A loop that is neither is
  a generator.

When a definition hits any refusal it graduates to Python: one builder
module declaring `FEATURE_TYPE`, one line in `_BUILDERS`, one section in
`precis-hexfold-seams-help` (pinned by `tests/test_hexfold_typed_features.
py`), one golden. That is exactly how `hexfold-multicell-prism.md` lands
`cells-k3-sp3-z`. Declarative JSON + references is enough as long as the
derived numbers are someone else's theorem; a real language is unavoidable
only where a new closed form or combinatorial construction appears, and
there Python plus tests beats any DSL the repo would have to maintain.

House doctrine supports the split: "parameterisation by users you can
name" (`precis.blocktree` docstring, settled 2026-09-07) — the users here
are the prism family, the bundle family, the knurled-insert family, the
pcb meta-block; and "ONE placement mechanism, in the code AND on the tool
surface" (`pattern-groups.md`).

## Core concepts

| concept | Å (atomic mode) | macro (mm–50 cm) | shared object |
|---|---|---|---|
| definition | se design, `meta.role='definition'`, ops include `generate` | same, ops include `add_block`+`set_envelope` (cad DSL with `$W`) | one design shape, one `params` schema |
| instance | a `stamp` (expanded, own atoms, provenance `generated.definition`) or `instance_block` (by reference, same geometry) | same two: stamp when args differ, reference when identical | `template` column vs `generated.definition` record |
| parameters | integers mostly (`rows`, `periods`, `(n,m)`), parity and closure constraints enforced by the generator | lengths with units (`"20 mm"`), preferred-number wells (`precis.structsolve.preferred`), process minima (`precis_se.modes`) | `{name: {type, unit?, range?, even?, doc}}` |
| interface | rim port `(edge-word, N)` + `lattice` + hybridisation (`GeneratedPort.payload`, `_rim_payload`); match law = registry/closure (`registry.closure`, `port.mismatch`) | port + datum + mate class + tolerance (`precis_se.datums`, `measures`); match law = fit class + stack-up | `precis.blocktree.types.Port` (roles, direction, annotations) — already unified; the annotations carry the scale |
| periodic boundary | `periods` quantised in translation periods `T` (spec §12.2); a tile step is the template's period vector, never a free float | pitch is a float; symmetry is a group action (`pattern-groups.md`) | `array_block` — one op; `pitch: "period"` is the only atomic-mode special value |
| cap site | an open rim port; capping = `join` at that port; the cap must close the counting law (`Σ(6−n)Pₙ + B = 6χ`) or H-terminate (`terminate`, default) | a datum face; capping = `connect` + joint + tolerance | the port; `cap` is sugar over join/connect with definition lookup by interface signature |
| fill | decorate sites (valence-bounded, `view='sites'` selectors; the multicell slice-2 primitive) or a guest block + vdW `clearance` + `declare_threading` | an insert in a pocket (`precis_se.pockets`, `fit_classes`) or a filler solid | `decorate` op + `instance_block` into a parent; `clearance` is the check at both scales |
| scale-with-size | integer scaling, derived lengths from σ/a (generator) | continuous, snapped to preferred numbers, bounded by process rows | ranges in `params`; the rule itself is never in data |
| verified | a GFN2/xtb relax record on the structure (multicell spec §Stability claims) | structsolve/SIMP/`mold` result | `examples[].verified` → a finding/memory/relax record; no text claims |

**Where the scales must differ, stated once:** (a) at Å a *fused* tile
(shared walls, bonds across the boundary) changes the counting law and the
seam chemistry, so it is a generator parameter (`cells` list), while a
*packed* tile (vdW gap) is `array_block`; at macro the same split is
array (parts) vs print-in-place union (`precis_se.manufacture`), already
two ops. (b) at Å the interface match is a theorem (rim word, N, lattice);
at macro it is a tolerance. (c) atomic params are integers with parity
laws; macro params are lengths with preferred wells. Everything else —
definition, stamp, array, connect, clearance, provenance, states
(`precis.design`) — is one path.

Anchors: the repo has no "anchor" term. What Reto means is the union of
ports (interfaces), datums (what a measure is declared against, `precis_se.
datums`) and hexfold handles (surface-space points, spec §23.1). This item
adds none; a definition exposes ports, and a stamp's ports are addressable
as `<stamp>.<port>` like any nested block's.

## The definition object

An se design with `meta.role='definition'`; `put` validates it like any
design, plus the definition rules below. Its ops are the ordinary ops with
two additions.

```json
{"role": "definition",
 "params": {"R": {"type": "int", "even": true, "range": [2, 40],
                  "doc": "zigzag chains per wall"},
            "P": {"type": "int", "even": true, "range": [2, 60],
                  "doc": "periods along the axis"}},
 "ops": [ ... ops whose values may be "$R" / "$P" ... ],
 "expose": {"top": "core.rim_top", "bottom": "core.rim_bottom"},
 "examples": [{"args": {"R": 2, "P": 4}, "expect": {"C": 192, "H": 24}},
              {"args": {"R": 4, "P": 6}, "expect": {"C": 576, "H": 36},
               "verified": "me503663"}],
 "checks": ["validate", "drc"]}
```

- `params` — closed types: `int`, `number`, `length` (unit-tagged string,
  converted by the existing `precis_se.catalog.to_metres`), `enum`,
  `string` (seam token strings, `cccRcccRcccR`), `list` (a `cells`
  list). `range`, `even`, `choices` are the only constraints; everything
  else is the generator's refusal, surfaced verbatim.
- `$name` — allowed in any op value position and inside cad DSL strings
  (`box:w$Wd$Dh$H` substituted before `precis.cad.dsl` parses). A
  `$name` not in `params` is `def.param.unknown`; a param no op reads is
  `def.param.unused` (the swallowed-facet rule, `precis_se.ops.
  _reject_instance_dof`).
- `expose` — which inner ports become the stamped block's own ports.
  Optional; unlisted ports stay reachable as `<stamp>.<child>.<port>`.
- `examples` — each is stamped on `put` into a scratch tree and run
  through `checks`; an ERROR is `def.example.failed` on the definition's
  `validate` view, naming the example. `expect` holds census numbers
  (closed forms from the multicell spec: `C = 2·R·P·walls + P·k3`,
  `H = 2·P·(hydrogenated folds)`); `verified` points at the record that
  carries the stability or mold claim. Examples are the catalogue row
  and the test in one place.

`view='definition'` on such a design renders the params table, exposed
ports with their interface signature, examples with pass/fail, verified
records, and the one `stamp` call that reproduces each example — the
"understand the block" read.

## Composition ops (the thin additions)

- **`stamp`** — `{"op": "stamp", "definition": "<design-slug>", "name":
  "<block>", "args": {...}, "parent"?, "pose"?, "rot"?}`. Replays the
  definition's ops under a new parent block, child names prefixed
  `<name>/`, `$params` bound, exposed ports copied up. Store-aware (it
  may run `generate`), so it joins `precis_se.atomic.apply.
  HANDLER_LEVEL_OPS` and the web turn's proposal class. Records
  `generated.definition = {slug, version_sha, args}` on the stamped block;
  `regenerate` replays it. Same shape as pcb meta-blocks' stamp
  (`prefix`, `bind`); the two kinds share the vocabulary, not code.
  Identical args twice → the echo suggests `instance_block` (one
  geometry, by reference).
- **tile** = `array_block` + one value: in atomic mode `linear.pitch:
  "period"` reads the template's `topology.period_A` (hexfold's `T`)
  so the stepped copies are in register. No new op; `pattern-groups.md`'s
  `repeat=` generalises it later. Fused tiling is a generator list param
  (`cells`), per §Where the scales differ.
- **cap** — sugar: `{"op": "cap", "block": "p", "port": "top", "with":
  "<definition>"|"H"}`. `"H"` is today's `terminate` (already the default
  at mint). A definition is stamped with its args resolved from the rim
  signature (`rim.N`, edge word) and joined with `join` at that port; the
  existing `port.mismatch`/`join.lattice` refusals apply. At macro, cap
  resolves to `stamp` + `connect` with a joint. The definition is found
  by `search(kind='se', wants={"interface": "<signature>"})` — the
  library index (`precis_se.library`) gains definitions as candidate
  rows with their exposed port signatures.
- **`decorate`** — `{"op": "decorate", "block": "p", "sites": {...},
  "group": "H"|"F"|"OH"|...}`. The multicell slice-2 primitive
  (`hexfold.build` site decoration, `topology.hydrogenated`,
  `hydrogenated:interior` tag) exposed as an se op whose `sites` selector
  is the one `view='sites'`/`lining` already use (`ring:<class>`,
  ordinals, `face: in|out|axial`) plus `column:<seam|fold>:<i>` from
  `topology.junctions`. One selector vocabulary, three users. A guest
  molecule is not an op: `instance_block` into the parent + `clearance` +
  `declare_threading`, as today.

Not added: expressions, inheritance (`extends`), per-element overrides
(the `pattern-groups.md` `explode` rule holds), a `module` kind.

## Worked example — the 12-wall H-column prism

Definition `hcol-prism` (A2 of the multicell spec: three tri cells in one
merge group with H on the core's reflex folds ≡ `cccRcccRcccR`):

```json
put(kind="se", id="hcol-prism", text='''{
 "role": "definition",
 "params": {"R": {"type": "int", "even": true, "range": [2, 40]},
            "P": {"type": "int", "even": true, "range": [2, 60]}},
 "ops": [{"op": "generate", "generator": "hexfold_scene", "name": "core",
          "params": {"terminate": "ports-open",
            "features": [{"name": "core", "type": "cells-k3-sp3-z",
              "rows": "$R", "periods": "$P",
              "cells": [[0,0,"a"],[1,0,"a"],[1,-1,"a"]],
              "fold_h": {"a": "reflex"}}]}}],
 "expose": {"top": "core.rim_top", "bottom": "core.rim_bottom"},
 "examples": [{"args": {"R": 2, "P": 4}, "expect": {"C": 192, "H": 24}},
              {"args": {"R": 4, "P": 6}, "expect": {"C": 576, "H": 36},
               "verified": "me503663"}],
 "checks": ["validate"]}''')
```

The fused bundle is a second definition, `hcol-bundle3`, whose `cells`
literal adds the three outer cells at the notches (bundle3 of the spec:
C1188 H36 at R4 P6); the `cells.fold.enclosed` WARN travels with it until
the 4-chain GFN2 result lands. No arithmetic derives the outer cells from
`n_outer` — that would be the third definition's generator.

Consuming design — stamp, pack, cap, fill:

```json
put(kind="se", id="hcol-array-1", text='''{"ops": [
 {"op": "stamp", "definition": "hcol-prism", "name": "p",
  "args": {"R": 4, "P": 6}},
 {"op": "array_block", "name": "pack", "template": "p",
  "linear": {"count": 3, "pitch": "4.1 nm", "axis": [1, 0, 0]}},
 {"op": "cap", "block": "p", "port": "top", "with": "H"},
 {"op": "cap", "block": "p", "port": "bottom", "with": "H"},
 {"op": "decorate", "block": "p", "group": "OH",
  "sites": {"face": "in", "ring": "6-ring", "column": "fold:1"}},
 {"op": "instance_block", "name": "guest", "template": "guests#c60",
  "parent": "p", "pose": "[0 0 2] nm"},
 {"op": "declare_threading", "a": "guest", "b": "p"}]}''')
get(kind="se", id="hcol-array-1", view="clearance", args={"a": "p", "b": "guest"})
get(kind="se", id="hcol-array-1", view="clearance")   # pack pitch vs vdW
```

The pitch is a number the agent reads off the stamp echo's envelope plus
a vdW gap and the `clearance` view checks — composable tools, not a
`pitch: "envelope+gap"` rule in data. Same design at R6 P12 is one `args`
change; the generator refuses odd values and the closure law refuses a
bad `cells` set before any geometry runs.

A lid definition for the notched 12-gon rim does not exist (no catalogue
row closes that rim); `cap with: "<lid>"` would refuse `port.mismatch`
with the rim signature, and H termination is the honest v1. The hex prism
`CCCCCC` end rim vs `cap(6k,0)` is a `join` question the op answers, not a
claim this item makes.

## Worked example — macro, knurled insert array in a mold

```json
put(kind="se", id="grip-insert", text='''{
 "role": "definition",
 "params": {"R": {"type": "length", "range": ["5 mm", "60 mm"]},
            "H": {"type": "length", "range": ["5 mm", "200 mm"]},
            "map": {"type": "string", "doc": "relief map sha (hmap knurl)"}},
 "ops": [{"op": "add_block", "name": "insert",
          "envelope": "relief:$R..$H..map:$map"},
         {"op": "add_port", "block": "insert", "name": "base",
          "direction": [0, 0, -1], "pose": "[0 0 0] mm"}],
 "expose": {"base": "insert.base"},
 "examples": [{"args": {"R": "12 mm", "H": "30 mm", "map": "<sha>"},
               "checks": ["validate", "mold"]}]}''')
put(kind="se", id="grip-tool-1", text='''{"ops": [
 {"op": "add_block", "name": "core", "envelope": "cyl:r40mmh30mm"},
 {"op": "stamp", "definition": "grip-insert", "name": "ins",
  "args": {"R": "12 mm", "H": "30 mm", "map": "<sha>"}, "parent": "core"},
 {"op": "array_block", "name": "ring", "template": "ins",
  "polar": {"count": 6, "radius": "28 mm"}},
 {"op": "connect", "a": "ins.base", "b": "core.top", "joint": "fixed"}]}''')
get(kind="se", id="grip-tool-1", view="mold", args={"layout": "halves", "alpha_deg": 2})
```

The relief leaf is dimension-free (`cad/primitives.py::CylRelief`, shared
`hexfold.wrap` frame),
`view='mold'` runs the pull check per orbit once `pattern-groups.md`
lands, and the `mold` op (slice 3) refuses an undercut layout. The map
sha comes from the `hmap` knurl builder — a generator, where the pitch
arithmetic lives. The pcb sub-assembly case is the same three ops with
`set_binding(kind='pcb')` (`pcb-se-binding.md`, rough box built) as the
stamped definition's L3.

## How an agent learns and reuses blocks

- **Catalogue** = `search(kind='se', wants={...})` over definitions as
  well as blocks (`precis_se.library.iter_candidates` adds
  `role='definition'` rows; new keys `interface`, `params`). Every row
  shows its examples' pass state. `view='definition'` is the one-page
  read. Hexfold's "generate on demand, no precomputed catalogue" (spec
  §26) holds: examples are built on `put`, nothing is guessed.
- **Validation feedback** is the stamp echo (generator findings,
  `def.*`), then the ordinary views. A definition whose example turns red
  after a generator change shows `def.example.failed` on its own
  `validate`, so the library self-reports drift.
- **Skills**: one new `precis-se-modules-help` (definition shape, the
  three refusals, stamp/tile/cap/decorate, both worked examples, the
  Å-vs-macro table); pointer lines in `precis-se-help` (ops section),
  `precis-se-atomic-help` (generate), `precis-hexfold-seams-help`
  (`cells-k3-sp3-z` as a definition), `precis-se-search-help`
  (definitions in `wants=`).
- **Memory**: rulings and evidence only — "A2 holds at +0.160 eV/C
  (me503663)", "notch folds rupture at 2-chain walls" — linked `related`
  to the `se:` definition ref so a later agent finds the executable
  thing from the lesson. The `remember` skill gains one line: a
  reusable construction goes to a definition, the lesson about it to
  memory.

## Slices

| slice | delivers | extends | size |
|---|---|---|---|
| 1 | `meta.role='definition'`, `params`, `$name` substitution, `stamp` (atomic + macro), `generated.definition` provenance, `def.*` findings; both worked examples as tests (the prism one at the `prism-k3-sp3-z` level until `cells` lands) | `precis_se.ops` (new op + substitution pass before `apply_ops`), `precis_se.atomic.apply` (HANDLER_LEVEL_OPS), `precis_se.persist` (role meta), `precis_se.validate` | 2 builds |
| 2 | `examples`/`checks`/`expose`, `view='definition'`, definitions in `search(wants=)` | `precis_se.handler`, `precis_se.library` | 1 build |
| 3 | `decorate` op over the multicell slice-2 primitive with the shared `sites` selector; `column:` selector | `precis_se.atomic.sites`, `hexfold.sheetnet`, `precis_se.atomic.generators.hexfold_spec._block_from_net` | 1 build (after multicell slice 2) |
| 4 | `array_block linear.pitch: "period"`; `cap` sugar over `terminate`/`join`/`connect` with signature lookup | `precis_se.ops._parse_array_spec`, `precis_se.atomic.join`, `precis_se.library` | 1 build |
| 5 | `precis-se-modules-help` + pointer lines; glossary entries definition / stamp / fused vs packed tile | `src/precis/data/skills/`, `docs/glossary.md` | with each slice |

Slice 1 alone makes `hcol-prism(R, P)` reusable today on `prism-k3-sp3-z`
(`seams: "cccRcccRcccR"` as the literal, `R`/`P` free) — the smallest
useful thing. `pattern-groups.md` stays the owner of general repetition;
this item never adds a second transform path.

## Risks

- **Over-generalising**: `$name` grows into expressions. Guard: the three
  refusals are tests (`def.expression` on any `$` followed by an
  operator), and the skill says "write a generator".
- **Two ways to do one thing**: `stamp` vs `instance_block`; fused `cells`
  vs `array_block`; `cap` vs `join`. Each pair is stated as a rule above
  (args differ → stamp; bonds cross the boundary → generator; cap is
  sugar that always lowers to join/connect and the echo says so).
- **Validation cost**: examples run on every `put` of a definition. Cap
  at the generator's own bounds (`rows ≤ 40`, `periods ≤ 60`) and run
  examples above `SYNC_CELL_CAP`-class cost as a job, like SIMP.
- **Cross-design drift**: a stamp records `version_sha` of the definition;
  `regenerate` with a newer definition is an explicit op, never implicit.
- **Memory as a second store**: a definition pasted into memory bypasses
  validation. The `remember` skill line and `view='definition'` make the
  DB path cheaper than the paste.

## Open questions for Reto

1. Definition = se design with `meta.role='definition'` (this item) vs a
   separate `module` kind. Recommendation: role on se; the pcb precedent,
   no migration beyond a meta key, every se view works on it unchanged.
2. `stamp` as its own op vs `generate(generator="def:<slug>")`.
   Recommendation: own op — macro definitions never touch `generate`,
   and the proposal/auto-apply policy keys on op name.
3. Does `cap` earn sugar in v1, or is `join`/`terminate` enough until a
   second lid family exists? Recommendation: defer `cap` to slice 4 and
   ship `terminate` + `join` as the documented path first.
4. Examples run on `put` (synchronous, bounded) vs always as a job.
   Recommendation: synchronous under the generator bounds, job above.
5. Should memory `related` links to `se:` definitions be a lint rule
   (a construction lesson with no definition link is a finding)?
   Recommendation: yes, in memory-lint, once slice 2 exists.
