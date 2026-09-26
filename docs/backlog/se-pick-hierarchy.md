---
status: draft
title: se viewer — cross-scale pick → hierarchical reference → prompt token
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
   - datum: `<se:UID@axis>`, `<se:UID@face:top>` — the datum grammar,
     namespaced under the block it is declared against.
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
