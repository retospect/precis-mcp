---
status: draft
title: se viewer — cross-scale pick → hierarchical reference → prompt token
pillar: 3d-design
prio: medium
model: opus
blocked-by: hexfold-integration
---

# Pick anything, get every level it belongs to, cite one in the prompt

Ask (Reto, 2026-09-16): click an atom in the se viewer and get a list
with one entry per hierarchy level — the atom (unique id), the object it
is part of ("carbon nanocone #4 — the axle"), the group above that
("sorting wheel assembly"), … — pick the level to argue about, and the
correct id lands in the message box as a token, so a prompt reads:

> `<axle cone 234>` should sit inside `<bearing cone 123>` so it rotates
> freely; `<nanobud 923>` interferes with `<fold 322>` — move it away
> from `<axis of rotation 3>`.

Two things fall out: (1) a **reference grammar** the LLM and the ops can
both resolve; (2) **abstract helper objects** (axes, planes) that have no
atoms and no envelope but must be citable the same way.

## What already exists (don't rebuild)

- **Block hierarchy = viewer path.** `precis_web/blocktree_3d.py` emits
  three-cad-viewer `Shapes` whose slash path is the ancestor chain and
  whose leaf is `se_blocks.uid` (stable across saves; row ids are not).
  A block pick already returns that path; splitting it on `/` IS the
  level list for everything above the atom. Picking, vertex/edge/face
  filters, and the tree panel live in `static/blocktree-3d.js`.
- **Atom identity.** `hexfold` atoms carry lattice-path IDs
  (`post/(u,v,A)`, `glucose.1/C4`); the `hexfold` generator stores the
  path↔ordinal map and module regions in the block's `topology`. A bound
  `structure` design's atom ordinal is stable per design version.
- **Datums.** `se-datum-measure-eval.md` (ready) defines the selector
  grammar `frame · port:<n> · face:<inst>.<tag> · axis:<inst> ·
  face:largest · face:normal=… · face:perp=assembly`. `axis:<inst>` is
  already the "axis of rotation" the ask needs for rotational primitives.

## Gap

1. **Atom-level pick inside a bound structure.** The 3D viewer draws
   envelopes; atoms render in the separate 3dmol view. Need one surface
   where a pick can land on an atom and resolve to
   `(atom ordinal, hexfold path, region, block uid, ancestor uids)`.
2. **Reference grammar + resolver.** One token shape for all levels,
   resolvable by the LLM-facing ops and by the web UI:
   - block: `<se:UID>` — `uid` is the identity; the label the viewer
     shows ("axle cone") is display only, never the key.
   - atom: `<se:UID#ORD>` (ordinal in the bound structure) — the hexfold
     path is available via `topology` but is not the citation key: not
     every structure comes from hexfold.
   - region (hexfold module inside a block): `<se:UID/REGION>`.
   - helix offset / base pair: `<se:HELIXUID@3>` (built). `@` means a
     helix offset and nothing else — Reto, 2026-10-01: one glyph, one
     meaning. A datum token (the datum grammar, namespaced under its
     block, e.g. `axis`, `face:top`) takes a glyph of its own when the
     first datum pick is built; it is not `@`.
   - Rendering: token → label + level, so the message shows
     `<axle cone (se:7f3a…)>` to the human and the UID to the resolver.
   Resolver in `precis_se` (pure, over `SeTree` + `topology`), used by the
   se `get`/`edit` echo so the LLM's reply can cite tokens back.
3. **Pick popup → prompt insertion.** Click → list (one row per level,
   innermost first: atom, region, block, parents…) → click a row →
   token appended to the ask box (`precis_web/ask.py` surface).
   Modifier: shift-click appends without the popup at the block level.
4. **Abstract helper objects (the "planes and axes" ask).** Extend the
   datum vocabulary — in `se-datum-measure-eval.md`, not here — with
   constructed datums that are *declared*, not derived from an envelope
   face: `axis:through(<ref>,<ref>)`, `axis:tube(<se:UID>)` (best-fit
   axis of a bound tube structure), `plane:points(<ref>,<ref>,<ref>)`,
   `plane:tangent(<se:UID@face>, angle=…)`, `plane:top3(<se:UID>)`
   (three highest atoms along a direction). They persist as named datums
   on a block so they have UIDs and are citable; they are never free DOF.
   This is the same "no free DOF / legal-at-write, DRC-at-read" posture
   as the existing datums.

## Out of scope / open

- Multi-select regions ("these 40 atoms") — a hexfold `region` covers the
  common case; ad-hoc atom sets are a later `set:` datum.
- Whether the atom pick uses 3dmol picking merged into the block viewer
  or a hexfold-emitted atom mesh in three-cad-viewer — decide when the
  `hexfold` generator has landed and the first bound structure renders.

## Chain-design instance (added 2026-09-30, from a prod dogfood)

Reto, looking at `/structure/dogfood-fold-3-hp.h0.s0`: "I would like to see
what basepair these are part of ... and get some hierarchy". For a chain
design the levels under the segment block are helix → offset (the base
pair, id `<helix>@<offset>`) → strand.domain → residue → atom, and every
one of them is already derivable: `RegionAtoms.residues` carries
`(chain, resseq, strand, ord, offset, letter)` per residue but
`precis_se/atomic/generate.py` drops it from the structure's
`meta['chain_atoms']` (only names/resnames/resseq/chain_ids survive), so
persisting that list is the one data change; pairing is `derive_pairing`
over the tree. The se page (`/se/<slug>`) already draws the bound
structure's atoms inside the block tree (`routes/blocktree_view.py::
_atomic_block_payload`) with no residue index and no atom pick — gap 1
above. The action a pick would offer (unpair this offset) is the op
proposed in `se-chain-insertions-deletions.md`.

**Resolver built (2026-10-01)** — gap 2 for this instance. `precis_se/pick.py`
(store-free; its docstring is the grammar's home now) and
`get(kind='se', view='pick')`: `args={'block', 'atom'}` (ordinal or the
scene label a finding prints) returns atom → residue → base pair → strand
domain → strand → segment → helix → ancestors, one token per row;
`args={'token'}` reads a token back. Grammar as decided there: residue
`<se:SEGUID/A.8>` (chain.resseq, the region slot), domain
`<se:STRANDUID/d1>`, and the base pair is `<se:HELIXUID@3>` — not the
`<se:UID@h0:3>` first written here, which keyed on a helix label against
this item's own "uid is the identity" rule. A structure with no
`realize_chain` record resolves atom → blocks, so a hexfold structure
already gets the block levels; its region level is the open part.

Still open for this instance, all on the se-3d-viewer side (seam agreed
with that thread 2026-10-01: resolver here, render there): gap 1 atom pick
on the `/se/<slug>` atoms, gap 3 popup + ask-box insertion. Agreed shape:
a click-time `GET /se/<slug>/pick?block=…&atom=…` route returning the rows
as JSON, never folded into the scene payloads (they are refetched on every
level change). The route is three calls: `identity.resolve_block`,
`atomic.render.bound_pick_inputs`, `pick.atom_levels`.
