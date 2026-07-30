---
id: precis-fisheye-help
title: precis — the fisheye neighborhood render (focus + context)
summary: view='fisheye'/'fisheye+1hop' on a draft/plan/paper/patent/memory chunk — the extent ladder, the spatial neighborhood, the reference ring
applies-to: get(kind='draft'|'plan'|'paper'|'patent'|'web'|'datasheet'|'cfp'|'memory'|'finding'|..., view=)
status: active
---

# precis-fisheye-help — focus a node and get its neighborhood, not a bare chunk

A **fisheye** is a degree-of-interest render (ADR 0051 §6): focus one
node and get it **plus its surroundings**, scaled by distance — not a
bare chunk floating with no context, and not the whole document either.
It is pure assembly of data that already exists (reading order, chunk
summaries/keywords, link edges) — no new storage, no background job.

## Read a chunk with its surroundings
## What does view='fisheye' return?
## I want more than the verbatim text but not the whole document

```python
get(kind="draft", id="dc41", view="fisheye")  # verbatim center + spatial neighborhood
get(
    kind="draft", id="dc41", view="fisheye+1hop"
)  # + everything the section references, one edge out
```

`view=` is the door; `kwd` / `summary` / `verbatim` / `fisheye` /
`fisheye+1hop` are the accepted values (the ladder is `Extent` in
`src/precis/workers/working_set.py::Extent`). Anything else on a chunk
address is an error, not a silent fall-back to the lone chunk.

## The extent ladder — how much to render

Each rung **strictly contains** the previous one (`Extent` in
`src/precis/workers/working_set.py::Extent`):

| `view=` | Shows |
|---|---|
| `kwd` | one-line bookmark, under its ancestor path |
| `summary` | the node's gloss (summary → keywords → first line) — alone |
| `verbatim` | the node's full text — alone |
| `fisheye` | verbatim center **+ the spatial neighborhood** |
| `fisheye+1hop` | `fisheye` **+ the reference ring** (what it points at) |

The first three rungs render the node **alone** — no surroundings.
Surroundings appear only at `fisheye` and up: that's the whole point of
this skill.

## The spatial neighborhood (the `fisheye` rung)

For a tree kind (`draft`/`plan`), `fisheye` renders a **graduated,
forward-biased span over reading-order neighbours** — not just
siblings — centered on the focused node
(`src/precis/utils/fisheye.py::render_fisheye`,
`src/precis/utils/fisheye.py::_render_fidelity_span`):

- **±5** neighbours render **full** (verbatim)
- **±10** render as a **summary** line
- **±15** render as a **keyword** (`kwd`) bookmark
- backward reach is **half** the forward reach (forward-biased — you've
  passed what's behind, you're heading into what's ahead)

The whole span renders under the node's **ancestor branch**
(`section_path`) so the focus never floats free of its heading — you
always see which `§` you're inside, not just the paragraph.

## The reference ring (`fisheye+1hop`)

Where the spatial fisheye walks *reading order* ("what's physically
near this"), `fisheye+1hop` adds the **reference ring** — what the
section *points at*, one edge out
(`src/precis/utils/refeye.py::render_reference_ring`,
`src/precis/utils/refeye.py::SEMANTIC_RELATIONS`):

- **Cited** — papers / datasheets / patents the section cites
- **Cross-refs** — other draft/plan chunks it links (`[[dc41]]`)
- **Notes** — memories/findings/etc. **linked to** the section (inbound
  edges — `related-to`, `see-also`, `cites`, …)
- **Claims** (Taproot) — a `[fi<id>]` (or `[pub_id]`) claim-hub cite in
  the section explodes into its evidence: the claim, its derived
  `establishes` originator(s) (★-marked, with the grounding chunk
  pointer when the chase has populated one), and a one-line
  corroborator/contradictor summary — via
  `src/precis/taproot/seniority.py::derive_evidence`, recomputed on
  every render. A handle naming an ordinary (non-hub) finding isn't
  mined into the ring. An authorial pin (Taproot slice A2 —
  `[fi<id>>pa5]` / `[fi<id>+pa5]`, same grammar the draft export
  reads) marks the pinned paper 📌 and, when it diverges from the
  derived originator, adds a short `(pinned; derived: pa99)` note.
  Each cited hub also surfaces its advisory `refines` neighbours
  (`derive_refines`): `↰ refined by fi<id> — <sentence>` (a sharper
  version of this claim exists) and `↳ refines fi<id> — …` (the coarser
  claim this one sharpens). Link-only — no evidence flows across it;
  authored via `precis taproot refine`.
  Evidence population depends on the forward chase
  (`PRECIS_TAPROOT_CHASE_ENABLED`, default-off, not yet run at corpus
  scale) — most hubs today show the claim with little or no derived
  evidence, so a populated Claims group is rare.

It follows **edges only**, both directions, capped per group with a
visible `+N more — focus to expand` line — never a silent truncation.
A memory that's merely *about* the section but was never linked is a
`search` hit, not a hop.

## Per-kind scope — the neighborhood shape depends on the kind

`render_eye` (`src/precis/utils/eye_render.py::render_eye`) dispatches
by kind — the ladder generalizes, the neighborhood shape does not:

- **Tree kinds** (`draft`, `plan` —
  `src/precis/utils/eye_render.py::_TREE_KINDS`) — the reading-order
  span above.
- **Doc kinds** (`paper`, `patent`, `web`, `datasheet`, `cfp` —
  `src/precis/utils/eye_render.py::_DOC_KINDS`) — no heading tree, so
  the "neighborhood" is the per-chunk KeyBERT keyword-cluster TOC
  (F20/ADR-0018) around the focused chunk: a whole-doc handle (`pa5`)
  renders the **cluster map** (one row per cluster); a chunk handle
  (`pc13234`) renders the **fisheye split within its cluster** —
  before/after chunks as gloss lines, the eye chunk verbatim, every
  *other* cluster collapsed to a label.
- **Link kinds** (`memory`, `finding`, and anything else not above) —
  the ref renders as its note (title → gist → body); `fisheye+1hop`
  grows the **link neighborhood** — every ref linked to it, either
  direction, with its relation type. Links are symmetric: fisheye-ing
  a paper surfaces a note linked to it, and vice versa.
- **Skill eyes** (`sk:<slug>`) — file-backed, no corpus position, so
  there's no neighborhood to have: `kwd`/`none` collapse to a bookmark,
  anything richer is the verbatim skill body.

```python
get(kind="paper", id="pa5", view="fisheye")  # cluster map (whole-doc handle)
get(kind="paper", id="pc13234", view="fisheye")  # fisheye split (chunk handle)
get(kind="memory", id="me9", view="fisheye+1hop")  # note + its link neighborhood
```

## Read the same neighborhood in a browser

The smartdraft web reader (`src/precis_web/routes/smartdraft.py::reader`)
is the fisheye rendered as a three-pane page — left: TOC nav, middle:
the focus + its neighborhood, right: relevance overlay:

```
/smartdraft/<draft-slug>?focus=dc<id>
```

## Don't confuse `fisheye` with `view='toc'`

`view='toc'` (`precis-toc-help`) is a **separate, recursive drill-down**
render for long documents (paper/skill) — you pick a range, it
re-clusters, you drill again. `fisheye` is the opposite move: you've
already picked one node, and want its immediate surroundings rendered
around it. A doc-kind whole-doc `fisheye` (the cluster map, above) looks
similar to a top-level TOC but is centered on nothing in particular;
focus a chunk handle to get an actual fisheye.

## See also

```python
get(kind="skill", id="precis-draft-help")  # draft chunk addressing, editing
get(kind="skill", id="precis-toc-help")  # the recursive drill-down TOC render
get(kind="skill", id="precis-get-help")  # the get verb generally
get(
    kind="skill", id="precis-paper-help"
)  # paper chunk handles (pc<id>), citation export
get(
    kind="skill", id="precis-relations"
)  # link relation vocabulary (cites, see-also, …)
get(
    kind="skill", id="precis-taproot-help"
)  # the Claims group's claim hubs, evidence edges
```
