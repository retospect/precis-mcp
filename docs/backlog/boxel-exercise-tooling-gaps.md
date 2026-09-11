---
status: draft
title: what the boxel exercise taught about the cad/structure surface
prio: normal
---

# Tooling learnings from building boxels (2026-09-07)

First real use of `cad` for nm-scale envelope work (`cad:boxel-cage-5nm`,
`cad:boxel-cage-3nm`; result in `nm-face-codes-and-scale.md`). Recording what
the surface got right and where it cost time, while it is fresh — these are
usage findings, not review findings, and they only surface by building
something.

## What worked, and should not be traded away

- **`connectivity` at `put` time caught a real modelling error before any
  probe ran.** The first cage reported `⚠ floating (touches nothing)` for 8 of
  14 bodies. That was a genuine coordinate mistake on my part, and a render
  would not have shown it — the panels looked fine individually. Author-time
  structural feedback is the single most valuable thing in the kind.
- **The ray probe answered the design question directly.** `view='ray'` with
  interval + feature attribution gave the internal clear span (4.4 nm in a
  5 nm cage) as a number, not a picture — which is what made
  "a 5 nm cage cannot hold a 5 nm cassette" a fact rather than an impression.
- The design → probe loop is genuinely good. Nothing below argues against it.

## Gaps, worst first

### 1. `put(kind='structure')` is uninvokable from the MCP client

Blocked the whole atomistic leg. Filed separately as **gripe 330034** — a
JSON-shaped `text=` string is coerced to a dict before it reaches the tool,
which then rejects it for not being a string. `cad` is unaffected (its source
is a line language, not JSON), which is consistent with a JSON-sniffing
coercion.

### 2. The placement convention is one clause with no worked example

`precis-cad-help` line 420, in full:

> All are placed base-at-`z=0`, centred on the local axis; `@x,y,z` and
> `rot:` set the world pose.

This is *correct* and I still got it wrong, because the convention is
**mixed** — centred in x/y, but based in z — and the sentence states it once,
in passing, with nothing showing the resulting extents. I assumed min-corner
placement, put six wall panels outside the cage, and only the connectivity
check saved me.

**Fix: one worked example.** e.g. "`box:w5d5h0.3 @0,0,0` occupies
x,y ∈ [−2.5, 2.5], z ∈ [0, 0.3]" — an extents line is unambiguous where a
prose clause is not.

### 3. Node-name uniqueness scope is undocumented

`component <name>` reads like it opens a scope ("nodes belong to it until the
next `component` line"), so per-component node names look natural. They are
not: names are **global** to the design, and reusing `plate` across two
components fails with `duplicate node name`. The skill never says this —
grep for "unique"/"duplicate" in `precis-cad-help.md` returns nothing.

**Fix: one sentence** where `component` is introduced.

### 4. No way to declare intended overlap — the warning channel drowns

A welded cage *deliberately* overlaps its panels and vertex pieces at every
corner. `put` therefore emitted **36 interference warnings**, all of them
exactly `-0.3` (the wall thickness), all of them intended. The skill covers
the opposite case (`⚠ floating (touches nothing)` — "a part welded to
nothing") but says nothing about overlap that is *supposed* to be there.

Consequence: the one warning that would have mattered would have been
invisible in the noise. I had to document in the design's own `desc:` that
the interference is expected — prose, unenforced, unreadable by the tool.

**Fix (affordance, not docs):** a way to declare a weld/joint between
components so intended overlap is acknowledged rather than warned — even just
a `weld a b` line that suppresses that pair. Absent that, at minimum have
`put` summarise ("36 interferences, all −0.3") rather than enumerate.

### 5. `put` scaling is undocumented and bites early

A 14-component design took **>120 s** and had to be backgrounded — twice.
Pairwise interference is O(N²) and 14 parts is not a large assembly. Nothing
in the skill hints that component count is the cost driver, so the natural
modelling instinct (one component per real body, which the "Truisms" section
actively encourages for connectivity) walks straight into it.

**Fix:** say so, and say what to do — model fewer components, or expect to
poll.

### 6. `cad` is millimetres-only, so nm work needs a fiction

For a 5 nm cage I adopted "1 mm = 1 nm" and declared it in `desc:` — prose
again, invisible to every probe, and it makes every returned number
(`-0.3 mm`) wrong by nine orders of magnitude unless the reader knows.

This is *why* `nm`/`se` exist as separate kinds, and that is the right
long-run answer. But for quick envelope sketching, a declared unit on the
design would remove the fiction and make probe output self-describing.
Related: the extracted `precis.blocktree` spine is unit-agnostic
(`blocktree-library-build-plan.md` §Settled) — the same observation one
layer down.

## Suggested split, and sequencing vs the blocktree refactor

Items 2, 3 and 5 are one small docs pass on `precis-cad-help`. Item 4 is a
real affordance change. Item 1 is already tracked (gripe 330034).

**Do NOT fold any of this into the `nm`/`se` blocktree refactor
(`blocktree-library-build-plan.md` §Settled).** Decided 2026-09-07.
Reasons, in order:

1. **A refactor must change no behaviour.** That property is the only thing
   that makes "tests still pass" mean "nothing broke". Mixing a cad
   affordance change or a semantic question into a move-the-code change
   destroys it, and a regression afterwards is then unattributable.
2. **Different surfaces.** Items 1–5 are `cad` and MCP-client work. The
   refactor touches `precis_se` and `precis_nm`. They barely overlap; bundling
   them widens the blast radius of a refactor already spanning two live
   plugins for no shared benefit.
3. **They are independently cheap.** A docs pass and one affordance change do
   not need to wait for anything.

Two items *do* touch the blocktree work, and are already accounted for:

- **Item 6 (units)** — resolved by construction. The extracted core is
  specified unit-agnostic, with the domain supplying Å or m. Nothing extra to
  fold in; this is the same observation one layer down and the refactor
  already answers it.
- **The rigid-body limit** (section below) — a *design* question about whether
  envelopes may deform. It must not be answered inside the refactor for reason
  (1), but whoever later generalises the core should read it first, because
  the answer changes what `Block` means. Recorded here deliberately rather
  than in the refactor item, so it cannot be mistaken for refactor scope.

## Expressiveness limit: bodies are rigid, molecules are not

Asked 2026-09-07: can the mechanical kinds express stretch / shrink / twist?

**Relative motion between bodies: yes, and it is well built.** `cad` has
`joint` with `revolute` (deg), `prismatic` (mm) and `cylindrical` ([deg, mm],
two DOF), plus `limits:`, `state=` posing, and `view='sweep'` — which answers
"does anything collide anywhere in the travel", not just in one pose. `nm`
mirrors the same two axes: `_DOF_KINDS = ("rotational", "translational")`,
declared between two named ports. `se` carries a `MECHANISMS` registry.

So **twist** is a revolute/rotational DOF, and **length change by telescoping**
is prismatic/translational. Both are expressible today.

**Deformation of a body itself: no, by explicit design.** `precis-cad-help`:

> transforms are **rigid** (translate + rotate, no scale), so every probe is
> **exact**

That exactness is *why* the whole probe surface can be analytic rather than
meshed — it is a deliberate trade, not an oversight. But it means a block
cannot stretch, bend, or twist *as a body*. There is no compliant member, no
spring, no elastic envelope.

### Why this matters more for `nm` than for `se`

A steel beam is stiff enough that rigid-body is a fine approximation, and
`se-feasibility-and-cost.md` already handles the residue as tolerance.
Molecules are not. Bond stretching, angle bending, entropic elasticity of a
chain, allosteric flex — these are not corrections to a rigid model, they are
often the mechanism itself. `nm-kind.md` names **"length-changing
structures"** among its goals; a telescoping prismatic DOF covers the
rigid-parts reading of that, but not a member that is genuinely elastic.

The sharp version: **rigidity is an assumption of the envelope layer (L0–L4),
and `structure` (L5) does not share it.** Atoms relax; deformation is native
and physical down there. So a compliant block is representable only *after*
fill — the envelope will claim a shape the relaxed atoms do not have, and
nothing currently reconciles the two. That is the same "envelope↔fill drift"
failure `nm-kind.md` warns about, arriving through a door the doc does not
name.

### Not proposing a fix here

Adding deformation to the envelope layer would cost the analytic-exactness
property that makes every `cad` probe cheap and exact, which is a bad trade
made casually. Plausible directions, in increasing cost:

1. **Say it.** Document rigid-body as an explicit modelling assumption of
   `nm` envelopes, so a designer knows the envelope is a stiff-limit
   approximation rather than the truth.
2. **A compliance annotation** on a block — descriptive, unenforced (se's
   `annotations` tier already exists for exactly this) — so "this member is
   expected to flex" is at least *recorded* and can be read by a later check.
3. **Reconcile at bind time.** Once a block binds to a relaxed `structure`,
   compare the realised extent against the declared envelope and flag drift.
   This is checkable with what exists today and needs no deformation model at
   all — it turns the limitation into a detectable condition instead of a
   silent one.

(3) looks like the honest first move: it does not pretend the envelope can
deform, it just refuses to let the lie go unnoticed.
