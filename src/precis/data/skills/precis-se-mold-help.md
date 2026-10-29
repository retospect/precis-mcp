---
id: precis-se-mold-help
family: se
title: precis — molding a part (molding mode, pull layouts, draft/undercut DFM, the mold op's sla halves)
summary: make a block moldable and cut its mold — declare molding intent (the mold op stamps `molding`, keeping a declared material half), pick a pull layout the release check clears (`halves` k=2 default, `3` slides, or the ungated `axial` sleeve), read view='mold' per relief leaf and analytic face (release rule n̂·p ≥ sin α; the undercut fraction is the share of the map leaning out of its slide's release cone, with the smallest k that clears), fix a refusal with a bigger k, a phi= parting rotation, or a regenerated pull-aware map (the view='mold' clip report shows how much depth must go), express draft as tcone/frustum slant, and cut one sla piece block per pull — cavity = box − scale(part, 1+shrink), the parting record on its meta — exporting the halves through the cad kind (the sla family has no print implementer: precis_se/modes.py implemented=False)
answers:
  - how do I make a part moldable in se?
  - how do I cut mold halves from a part I've designed?
  - which mold layout do I pick — halves, 3 slides, or axial?
  - what does the undercut fraction on view='mold' actually mean?
  - the mold op refused with an undercut — what do I change?
  - how do I clear the mold.undercut finding?
  - can I express draft in the cad DSL, or is there no draft node?
  - where does the parting line go — can I rotate it away from my features?
  - how does the molded part relate to its sla-printed mold halves?
  - how do I export the mold halves to print them?
  - what do alpha=, shrink=, margin=, phi= on the mold op mean?
applies-to: get/edit (kind='se'); get(kind='cad', view='mold'|'stl'|'3mf'|'step'); read precis-se-help first for the op grammar; precis-se-print-help for the fdm path a fit-test prototype takes instead
status: active
tags: workflow, design
kinds: se
---

# precis-se-mold-help — moldability: mode → draft → undercut → halves

Molding an `se` part is two fabrications: the part itself, stamped
`molding`, and the mold the `mold` op cuts from the part's cad geometry —
one piece block per pull, prototyped as SLA-printed halves. Read
`precis-se-help` first for the op grammar. Everything below is
`edit(kind='se', id=…, ops=[…])` unless it says otherwise.

## 1 — Choose the mode and the pull layout

- `molding` is a mode family like `fdm`: declare it ahead with
  `set_mode(mode='molding/<material>')` (free-text material half), or let
  the `mold` op stamp it — it keeps a material half the part already
  declared. No capability row characterizes a molding draft/wall
  threshold yet, and in `precis_se/modes.py` the `molding` family is
  `implemented=False` — recorded intent, nothing checks it (planned).
- The part must be cad-bound first — the op refuses an unbound block,
  pointing at `realize` (or `set_binding(kind='cad', …)`).
- `layout` (op arg, default `halves`):

  | layout | pieces | pulls |
  |---|---|---|
  | `halves` | 2 | parting plane through the axis, pulls ±x rotated by `phi` |
  | `3` | 3 | 120° wedges, radial pulls at `phi + 120°·j` |
  | `axial` | 1 | one-piece sleeve, the part slides out along +z |

- Radial layouts are gated by the undercut check (§2–§4). `axial` is not
  gated today — its constant-in-z undercut model is a named follow-up
  (planned).
- Other op args: `phi=` (rad) rotates the parting plane(s); `alpha=` (rad)
  is the draft angle the release check demands; `shrink=` (fraction ≥ 0)
  dilates the cavity for material shrinkage — explicit today, a
  material-card default is planned; `margin=` (m) is the cavity wall,
  default max(1 mm, 20% of the part's largest span).

## 2 — Read the release check on the part's cad design

```python
get(kind="cad", id="<part design>", view="mold",
    args={"layout": "k2", "alpha": "2deg"})      # or "k3"; alpha/phi optional
get(kind="cad", id="<part design>", view="mold",
    args={"layout": "k3", "alpha": "2deg", "relief_fit": "clip"})
```

One row per relief leaf: undercut fraction and area, `k=3 clears` or `no
k clears`, and a verdict (`ok`, or `FAIL` with the fix named). A PNG is
written only for a failing leaf — or whenever `args.path` is set; `path`
is honoured with exactly one leaf, otherwise the file lands in the temp
dir as `<name>.mold.<leaf>.png` (map in grayscale, undercut pixels red,
pull centres green). Analytic faces are their own table — rows of
`(face, draft, verdict)`, `FAIL` when the face drafts less than `alpha`.
The `face <label>: <draft>° draft < required` wording is the mold op's
refusal, not this view. The stored-part twin of this report is the se DRC
rule `mold.undercut` (error), rendered in `get(kind='se')`'s findings
table — relief leaves only; run `view='mold'` for the face rows.

Args: `layout` `'k2'|'k3'` (default each leaf's recorded pull) · `alpha`
(`'2deg'` style) · `phi` parting angle · `relief_fit='clip'` · `path`.

A relief leaf is a `relief:` node — `relief:<tokens>map:<sha>`, tokens
`r` cylinder radius, `h` height, `d` depth, `s` sign, `n` repeat, `k`
pulls, `a` draft angle, `p` parting angle, `t` twist, the map sha last —
and a pull-aware generator (knurl/ribs/sketch/text) stamps `k`/`a`/`p` on
the leaf. Storing the height map itself is a store-level call
(`put_hmap`), not one of the seven verbs today (planned): reference an
already-stored `map:<sha>`.

## 3 — What the undercut fraction means

The release rule is one test per map point: `n̂·p ≥ sin α` — the surface
normal against that point's own slide pull `p` (pulls at `phi + 2πj/k`),
at draft angle `alpha`. The fraction is the share of the relief map whose
live edges fail it: the finding's `% of the checked surface`, with the
`~mm²` figure that share of the cylinder's `2πR·H`. Read it as how much
of the pattern fights the pulls — near 0 the layout opens, and `no k in
{2,3} clears it` means the map itself must change (§4), not the layout.
Not failures, by the parting-line rule: the bare cylinder at a join
(zero draft accepted — the halves split there) and the part's open ends
(faces (anti-)parallel to the pull axis are not slide surfaces).

## 4 — When the mold op refuses, change one of four things

The refusal names the layout, the fraction/area, the smallest k that
clears (`clears at k=3`) or `no k in {2,3} clears it`, and points at
`view='mold'`:

1. **Bigger slide count** — re-cut with `"layout": "3"` when the message
   says k=3 clears.
2. **Rotate the parting line** — `phi=<rad>` moves the join (and its
   zero-draft dead zone) off the offending features.
3. **Read the clip report** — `relief_fit='clip'` on `view='mold'` is a
   dry run: it clips the map in memory and reports the depth that must go
   (max and mean); nothing is written back. Fix the part by regenerating
   the map pull-aware (4) or lowering the relief depth by hand, then
   re-cut.
4. **Re-author the map pull-aware** — the generators shape flanks from
   the release rule and stamp `k`/`alpha`/`phi` on the leaf. A leaf with
   no recorded `k` is not given the argmin-over-k layout: it is checked
   at k=3 with phi 0 (its recorded phi, else 0), and `clears at k=` is
   searched across the other k values.

Refused for other reasons: the part's design has internal
`cut`/`intersect` nodes — a cavity is `box − part`, so the part must be a
union of positive bodies; `shrink < 0`; `shrink > 0` on a patterned part
(the pattern pitch would not scale — planned); or a `<part>.mold.<j>`
block from an earlier cut is still present — remove it first.

## 5 — Draft is frustum geometry in the cad DSL

There is no draft parameter on a solid node — draft is the slant of the
body itself. `tcone:rb<rb>rt<rt>h<h>` (circular frustum) and
`frustum:n<n>rb<rb>rt<rt>h<h>` (regular polygon frustum) carry a lateral
whose draft angle is `atan((rb−rt)/h)`; `cyl`/`box`/`hex`/`ngon` walls are
vertical (0° draft), `cone`/`pyramid` the full-taper extremes. Build a
wall that must release from a slide out of frustum bodies. The check then
verifies it — `view='mold'` and the `mold` op measure each analytic
face's draft against the pulls and fail a face under `alpha` with its
measured angle. A vertical wall is safe exactly where it is a parting
surface: a true witness face (`n·p ≈ 0` for every pull) reads as a
candidate parting surface — reported, not failed.

## 6 — The part and its sla mold halves

The `mold` op cuts, per pull, a piece block `<part>.mold.<j>` parented to
the part, bound to a fresh cad design `<se design>-<part>-mold-<j>`
(name collision takes a numeric suffix), moded `sla` — the mold pieces
are prototyped on the `sla` family (spec Q4: SLA-printed halves) while
the part itself carries `molding`. Each piece design is
`Inter(sector_j, Diff(box, scale(part, 1+shrink)))` — the cavity is the
part dilated by `shrink`, the box is the cavity plus `margin` — and its
meta records the parting surface (`layout`, `k`, `phi`, `pull`,
`parting_angles`), the record a milled half would carry too (milled
molds: later; cutter radius planned).

The `sla` family is `implemented=False` in `precis_se/modes.py`
(planned), so the halves don't export through `view='print'` — export a
piece design through the cad kind instead:
`get(kind='cad', id='<piece design>', view='3mf',
args={'path': '…'})` (also `stl`, `step`, `scad`). Print both halves in
resin and mold the part in them; sprue/gate/runner/vent/ejector/cooling
design is out of scope (planned).

## 7 — Worked example: mold a knob grip

```python
edit(kind="se", id="knob", ops=[
  {"op": "add_block", "name": "grip", "envelope": "cyl:r0.02h0.04"},
  {"op": "realize", "block": "grip", "mode": "molding"},
])
edit(kind="se", id="knob", ops=[
  {"op": "mold", "block": "grip", "layout": "halves",
   "alpha": 0.035, "shrink": 0.005},  # rad (2°); 0.5% cavity dilation
])
get(kind="se", id="knob")   # findings table — mold.undercut rows if any
get(kind="cad", id="knob-grip", view="mold",
    args={"layout": "k2", "alpha": "2deg"})   # undercut image per leaf
get(kind="cad", id="knob-grip-mold-0", view="3mf",
    args={"path": "/tmp/knob-grip-mold-0.3mf"})  # the half to print
```

The echo names what it cut: `mold('grip'): radial layout, 2 piece(s)
[grip.mold.0, grip.mold.1], shrink 0.5%, margin 8 mm; part mode →
'molding'`. A plain cylinder has nothing to undercut; a relief leaf or an
under-drafted frustum face is what turns the refusal on (§4).
