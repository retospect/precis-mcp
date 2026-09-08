---
status: draft
title: a searchable library of functional molecular blocks, typed by joining chemistry, assembled and DRC'd before atoms
prio: high
model: opus
---

# Functional block library + assembly states

Reto, 2026-09-07:

> at the block and molecule abstraction level I'd like … a library (so I can
> search for *opto deform bistable 1nm lengthen with copper click chemistry*)
> … box type abstractions of the underlying molecules so we can check
> interfaces and do component level assembly and DRC before expanding to
> atoms … the "loaded for click chem molecule to do the assembly" and the
> "virtual bond is now here" type of block to see what it looks like without
> synthesis.

Good news: **most of this composes from parts that already exist**, and it
lands on the kinds built earlier today. Three specific gaps, named at the end.

## The three-level realisation chain

The request implicitly asks for three views of one thing, and each already has
a home:

| level | what it is | kind | why that one |
|---|---|---|---|
| **box** | envelope + typed ports + states. What you assemble and DRC. | `nm` block | it is intent-over-atoms; envelopes and ports are its whole point |
| **molecule** | the actual purchasable reagent, with supplier, price, purity | `component` | it is **bought**, and `component` is the procurement store |
| **atoms** | the realised structure, relaxed | `structure` | L5 fill; `nm` mints and binds these |

Edges already exist for both hops: `realized-by` (block → the procurable thing
that realises it; `se` already uses this) and nm's `bound_design` (block →
`structure`). So "expand to atoms" is a traversal, not a new feature.

**This resolves an apparent conflict.** `nm-kind.md` insists a `component` is
*bought* while an nm block is *made*, and names the nm object "block" to keep
that distinction. A click-chemistry building block is **both**: you buy
azide–azobenzene–alkyne from a catalogue, and you use it as a made-from block
in your design. The chain above is how both stay true — the block is the
abstraction, the component is the bottle on the shelf.

## The library query

*"opto deform bistable 1nm lengthen with copper click chemistry"* decomposes
into four filters, and each maps to a field we either have or should add:

| fragment | filter |
|---|---|
| opto deform | the block's **actuation stimulus** = light |
| bistable | both states thermally stable (P-type) — a property of the switch |
| 1 nm lengthen | Δ(end-to-end distance) between states ≈ 10 Å |
| copper click chemistry | its ports' **joining chemistry** = CuAAC |

The first three are *sourced facts about a molecule* — λ_on/λ_off, thermal
half-life, Δdistance, quantum yield, fatigue cycles — which is exactly the
star-schema shape (`rxn`/`material`: typed registry + many sourced value rows,
spread across sources is the answer, each row cited). **Do not invent a new
property store for this.** A photoswitch's Δdistance in solution and in a rigid
scaffold are different numbers and both are true; the multi-row design already
handles that, and `photoswitch-states-and-spectral-dof.md` argues the same.

The fourth is a **port role**, which is where the interesting gap is.

### The query is a RANKED PARTIAL MATCH, not a filter

Reto, 2026-09-07: *"search attributes should be multiple and not all required.
And the goal is reuse."*

That is the difference between a database lookup and a parts-selection tool,
and it changes the design. Under a strict AND, a specific query
("bistable, 1 nm, CuAAC") returns **nothing** as soon as no exact part exists —
which is the normal case, and useless. What a designer needs is *the closest
available thing, with the gap stated*:

    query: opto-deform · bistable · Δ 1.0 nm · CuAAC

    azo-CuAAC-01     opto ✓  bistable ✗ (T-type, τ½ 2 d)  Δ 0.34 nm  CuAAC ✓   3/4
    dae-alkyne-04    opto ✓  bistable ✓                   Δ 0.42 nm  CuAAC ✓   3/4
    dae-long-11      opto ✓  bistable ✓                   Δ 0.9 nm   NHS ✗     3/4

None is a hit; all three are decisions. The second says "right physics, half
the throw — use two in series". The third says "right throw, wrong handle —
re-functionalise". **A strict filter would have returned an empty set and
taught nothing.**

So: every attribute is optional, results are **ranked by how many matched**,
and the response must report **per-attribute match/miss with the actual
value**, never a bare score. A rank without the misses is unusable, because
the whole judgement is *which compromise can I live with*.

**Precedent to reuse rather than reinvent:** `quest` already does exactly this
shape — `meta.rubric_objectives` (Pareto front over named measures) and
`meta.rubric_composite` (weighted combination). Its discipline transfers too:
weights are **human-set**, and an agent may not tune its own objective. A
library search is a Pareto query over part attributes; treat it as one.

### Reuse has two structural consequences

*"The goal is reuse"* is not just motivation — it constrains the mechanism:

1. **Placement must be by reference, not copy.** If using a library part
   copies its geometry into your design, then fixing the part later fixes
   nothing downstream, and the library rots into N private forks. `nm` already
   has the right primitive — `template`/`instance`, where "an instance
   resolves envelope/ports/dof from its template at read time" — so
   cross-design placement (alteration 1) must extend *instancing*, not
   introduce an import-and-flatten.
2. **Part identity must be stable.** A part that is reused is referenced by
   many designs, so its slug is a contract. This is the same reasoning that
   made stable `mk<id>` handles matter for make-trees: an identity that moves
   breaks every reference to it.

## Gap 1: port roles gate on identity, click chemistry is complementary

`precis_nm/ops.py`'s connect gate, verbatim from its own docs:

> **Capability gate** … a `kind='bond'` connect requires *both* ports' `roles`
> to include `'covalent'` — or, when `objectives={'role': …}` names a
> different role, both ports must afford *that* role instead. … This is a
> **declared-intent check, not a chemistry validation** … capability
> *labelling*, not proof.

The trust model is right and should not change. The **arity** is wrong for
chemistry: the gate asks *do both ports afford the same role*, but click
chemistry is **complementary** — azide binds alkyne, not azide. Under today's
gate you would have to label both ports `CuAAC` and lose the direction, which
also makes an azide–azide connect legal.

Note the same repo already has the right concept one layer over:
`nm-face-codes-and-scale.md` specifies face-code complementarity as
"elementwise (donor↔acceptor, bump↔hole, +↔−)". **Ports need what faces
already have.**

Fix shape: let a role be declared as a complementary pair
(`azide`/`alkyne` afford `CuAAC` in opposite senses), and have the gate check
*affordance of complementary halves* rather than set intersection. Keep the
labelling-not-proof trust model exactly as is.

## Gap 2: "loaded" vs "bonded" is the same states mechanism as a photoswitch

Reto's *"loaded for click chem"* and *"virtual bond is now here"* are two
**discrete states of a block**:

- `loaded` — the reactive handle is present, the port is free, the envelope
  includes the pendant azide/alkyne;
- `bonded` — the handles are consumed, the ports are mated, and **the geometry
  has changed**: CuAAC forms a triazole, which is itself a linker with real
  length and rigidity. The joined assembly is not simply the two envelopes
  touching.

This is *precisely* the states-and-stimulus model
`photoswitch-states-and-spectral-dof.md` proposes for light. Same mechanism,
different driver:

    photoswitch:  state A --[ λ = 365 nm ]--> state B
    assembly:     loaded  --[ rxn: CuAAC  ]--> bonded

**One mechanism serves both**, which is a strong argument for building it once
and well: a block has named discrete states, each with its own rigid envelope
and port poses, and transitions carry *what drives them* — a wavelength, a
reaction, a redox potential, a pH.

That is also exactly the "see what it looks like without synthesis" ask: pose
the design in the `bonded` state and probe it. No atoms, no synthesis, just
the envelope arithmetic — which is what `cad`'s `state=` posing and
`view='sweep'` already do for articulated joints.

## Gap 3: the joining reaction has a home now — use it

The `bonded` state's geometry is not free-floating: it is *the product of a
reaction*. CuAAC's triazole linker length, its yield, its conditions (Cu(I)
source, solvent, whether it tolerates the rest of your molecule) are
**reaction facts** — which is what the `rxn` kind shipped today is for.

So the transition edge should reference a `rxn`, and then:

- the **precedent read** already built answers "does this joining chemistry
  actually work on substrates like mine, and at what yield" —
  `search(kind='rxn', property='yield', reaction_class=…)`;
- the DRC gets a real question to ask: *is the declared joining reaction
  precedented for these two partners?* A block pair whose connect names a
  reaction with **no precedent** is the same "unprecedented step" flag the
  synthesis-cost design already defines.

This is the payoff of having built `rxn` first: the assembly graph and the
chemistry share one source of truth instead of the block layer inventing a
private notion of "these can join".

## What DRC becomes possible before atoms

With the above, at envelope level and with no structure fill:

- **interface check** — do the two ports afford complementary halves of a
  declared joining chemistry (gap 1);
- **precedent check** — is that reaction precedented for these partners
  (gap 3), with the yield spread quoted;
- **fit check** — in the `bonded` state, does anything clash; does the
  assembly still terminate at real, purchasable blocks (the `realized-by`
  chain, mirroring stock-termination);
- **envelope arithmetic** — does the switch have room to move. The boxel work
  already showed the cage geometry is the binding constraint, and a
  photoswitch embedded in a rigid scaffold is exactly the failure mode the
  photoswitch research flags.

That last one matters: a 1 nm lengthening switch inside a cage whose internal
clear span is 4.4 nm is fine; inside the 3 nm cage measured at **1.0 nm**
vertex-band clearance, it is not. **That check is arithmetic on numbers we
already produce.**

## What must be ALTERED (Reto: big changes fine, no customers, data re-ingestable)

Every `nm`/`se`/`pcb` table is empty and the constraint is lifted, so these are
rewrites, not migrations. Ordered by what blocks what.

### 1. Cross-design block reuse — THE blocker for a library

**Measured 2026-09-07: `nm` and `se` cannot reuse a block from another
design.** Their `instance_block` resolves `template` to a block *in the same
design*; there is no cross-design import (`nm` op set is add_block,
instance_block, add_port, remove_port, connect, disconnect, remove_block,
declare_dof, clear_dof, declare_threading, remove_threading — nothing that
reaches another design). `cad` already has it: `use <slug> as <name>`, which
also syncs a `contains` edge.

Without this there is **no library**, because a catalogued part cannot be
placed. A library part should be an ordinary single-block design, and using it
should be instancing it — same shape as cad sub-assemblies. This is the one
change everything else in this doc depends on.

### 2. Discrete states + stimulus-labelled transitions

The mechanism shared by photoswitches (`--[λ]-->`) and assembly
(`--[rxn]-->`). A block gains named states, each with its own rigid envelope
and port poses; transitions carry their driver. Schema change to the block
tables, new ops (`add_state`, `set_state`, `declare_transition`), and
state-aware rendering/probing (cad already has `state=` posing and
`view='sweep'` to copy).

Build it **once, in `precis.blocktree`**, not twice — that is what the spine
extracted today is for.

### 3. Complementary port roles

Gate arity, not trust model. Today: *do both ports afford the same role*.
Needed: *do they afford complementary halves* (azide↔alkyne). Keep
"declared intent, not proof" exactly as is. Small, and it is what makes the
library's joining-chemistry filter mean anything.

### 4. Unblock `structure` authoring (gripe 330034)

`put(kind='structure')` is uninvokable from the MCP client — a JSON-shaped
`text=` is coerced to a dict, then rejected for not being a string. The whole
"expand to atoms" leg is unreachable until this is fixed. It is infrastructure,
not design, but it gates the bottom of the three-level chain.

### 5. `cad` unit declaration

Drop the millimetres-only assumption. nm-scale envelope work currently needs a
"1 mm = 1 nm" fiction declared in prose, which makes every probe output
(`-0.3 mm`) wrong by nine orders of magnitude unless the reader knows. A
declared unit on the design makes probe output self-describing. With no
compatibility constraint this is cheap.

### 6. Declare intended overlap in `cad`

A welded cage emits one interference warning per overlapping pair — 36 for a
14-part boxel, all intended, drowning any real one. Needs a way to say "these
two are welded" so the channel carries signal. (Gripe 330182 covers the doc
half; this is the affordance half.)

## What NOT to alter — worth stating, because each is tempting

- **Do not add deformable envelopes.** The rigid-body limit looked like the
  blocker for light-driven deformation and is not: bistable switches are two
  rigid states plus a transition. Adding deformation would cost cad's
  analytic exactness — the property that makes every probe cheap and exact —
  to model something the discrete-state design already covers.
- **Do not create a "part library" kind.** Parts are single-block designs;
  procurement is `component`; properties are the star schema. A new kind would
  duplicate two stores at once.
- **Do not merge the `nm` and `se` kinds.** Settled earlier today: different
  units, L2 semantics and binding targets. The *implementation* is now shared
  via `precis.blocktree`; that was the right scope.
- **Do not make the capability gate validate chemistry.** It is declared
  intent by design, and `rxn` precedent is evidence, not proof. Neither should
  start claiming a reaction will work.

## Build order

1. **Discrete block states + stimulus-labelled transitions** in
   `precis.blocktree` — serves photoswitches and assembly with one mechanism.
   Nothing below works without it.
2. **Complementary port roles** (gap 1) — small, self-contained, and it is
   what makes the library's joining-chemistry filter meaningful.
3. **The library as sourced facts** — photoswitch/linker properties as
   `rxn`/`material`-shaped value rows, so the search query decomposes into
   ordinary range+class filters rather than a bespoke index.
4. **Wire the transition to a `rxn`** (gap 3) and add the precedent DRC.
5. `realized-by` from block to a purchasable `component`, so "what do I order"
   is a traversal.

## Deliberately not proposed

- **A new kind.** Everything above is states on the existing block model plus
  ordinary use of `rxn`/`component`/`structure`. A "molecular part library"
  kind would duplicate the procurement store and the fact store at once.
- **Automatic chemistry validation.** The capability gate is *declared intent*
  by design, and the `rxn` precedent read is *evidence*, not proof. Neither
  should start claiming a reaction will work; both should say what is declared
  and what is precedented, and let the human decide.
