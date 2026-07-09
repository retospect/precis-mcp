# ADR 0052 — Structured `term` registry: parts, components, and policy-numbered callouts

- **Status**: proposed (2026-07-09) · design conversation captured, not
  yet sliced. This ADR records the *decisions*; the fuller storage/render
  discussion lives in
  [`docs/design/draft-section-styles.md`](../design/draft-section-styles.md)
  (the `part`/`term` expansion table).
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0033 — Draft-as-chunks](./0033-draft-chunks-editable-document.md)
    — the editable chunk-native document and its `chunk_kind='term'`
    glossary leaf (`meta.short` → definition), which this ADR extends
    rather than replaces.
  - [ADR 0034 — Figure assets & permission provenance](./0034-figure-assets-and-permission-provenance.md)
    — the `figure` chunk_kind the patent drawings registry already hosts
    alongside its parts.
  - [ADR 0035 — Computed chunks, recipes & the recompute boundary](./0035-computed-chunks-recipes-and-the-recompute-boundary.md)
    — the `chunk_kind='table'` "edit the data, not the derived text"
    discipline §6 applies to a whole registry section.
  - [ADR 0036 — Universal handles](./0036-universal-handles.md) — the
    `dc…` handle a part/component leaf is referenced by from prose.
  - [ADR 0037 — Heading styles & numbering lock](./0037-heading-styles-and-numbering-lock.md)
    — §0 (stay on `term`/`paragraph` until prose stops sufficing) and §5
    (managed series numbering as an expansion). **This ADR builds and
    subsumes that §5 expansion** for the `assign="render"` case below.
  - `patent-image-part` section-style skill + the `patent` `doc_type`
    scaffold in `precis_web/routes/drafts.py`
    (`_SECTION_STYLES` / `_SCAFFOLDS`).
  - `Store.defined_abbrevs` → `linkify._highlight_abbrevs` → the
    `.pa`/`.pa-pop` reader tooltip — the instant hover this generalizes.
  - `Store.ensure_glossary_heading` — the text-matched home-heading
    lookup this ADR replaces with a role-tagged singleton (§7).

## Context

Two document shapes want the *same* mechanism, and each currently has
only a prompt-level stub:

- A **manufacturing / system-description document** — a `draft`
  subtype. It carries a **components table**: each row is a part with a
  short name, description, **manufacturer part number (MPN)**, and a
  **web link** (datasheet / ordering page). Referencing a short name or
  part number anywhere in the prose should raise a hover card, exactly
  like an abbreviation definition or a `kind:ref` preview.
- The **patent drawings registry** — the `patent-image-part` section
  style (a `draft` `doc_type='patent'` section). Its skill already tells
  the author to "register every reference numeral as a named leaf," but
  nothing structured backs it: `numeral` appears **only** in the design
  doc, not in code. `part` is a v1-reuses-`term` placeholder (ADR 0037
  §0); managed numbering is the unbuilt §5 expansion.

There is a **third instance of the same shape** — the ordinary
**abbreviation glossary** (`term` leaves under a "Glossary" heading). So
the abstraction generalizes to *three hoverlings*: glossary, patent
drawings/parts, product components. All three are the **same** pattern —
`term` leaves rendered as a derived table/list and hovered from prose.

These are the **same abstraction** — a registry of named reference
leaves, each referenced from prose by `dc…` + noun phrase and
hover-previewed — separated by exactly two axes:

1. **Content richness.** A patent leaf is `(name, description)`, nothing
   more. A manufacturing leaf adds `manufacturer`, `MPN`, `url`,
   `ordering`.
2. **Numbering policy.** Patent reference numerals are assigned *at the
   end*, spaced for readability — start on a 100 boundary, step 5 (or
   10): `100, 105, 110 …`. Manufacturing **callout** numbers are taken
   *as they go*, consecutive and stable once assigned: `1, 2, 3 …`.

The failure mode this ADR avoids: modelling the two as separate
chunk_kinds (or, worse, a separate kind) and re-implementing the hover +
numbering twice, diverging the registries that are conceptually one.

## Decisions

### 1. No new kind. A `draft` section-style feature.

The manufacturing / system-description document is a **`draft`
subtype** (an open `subtype:` / `topic:` tag + a `doc_type`), not a new
top-level kind. It has no distinct `corpus_role`, reader namespace, or
citation semantics, so a kind is unwarranted (AGENTS.md: no new kind
without one). Everything below is `draft` machinery.

### 2. One structured `term` leaf — a shared core plus an optional bag.

Both registries store their entries as the **existing
`chunk_kind='term'`** leaf (ADR 0033 §9). No new chunk_kind, no
migration — all additions are `meta` JSONB keys:

- **Shared core** (both use): `name` (the noun phrase, chunk text),
  `surface_forms` (inflections/aliases), and `meta.callout` — the
  assigned reference number (patent numeral / part item index). This is
  the prose-reference target.
- **`meta.registry`** — the family discriminator, `∈ {glossary, parts,
  components}`. It does double duty: routes the leaf to its one home
  heading (§7) and selects which derived table it projects into (§6).
  This is the single tag that keeps three registries on one `term` kind.
- **Optional attribute bag** (manufacturing parts only):
  `meta.manufacturer`, `meta.mpn`, `meta.url`, `meta.ordering`. Absent ⇒
  the leaf renders exactly as spare as a patent part. The MPN is an
  **external identifier** (authored, never assigned by us) — distinct
  from `meta.callout` (the in-doc index).

A patent part is just a manufacturing part with the bag empty; a
manufacturing part is a patent part with the bag filled. One shape.

> **`surface_forms` is net-new work, not a freebie.** A `term`'s
> `{short, long, surface_forms}` is *stored by convention*
> (`_insert_draft_chunk`), but `defined_abbrevs` consumes only
> `meta.short` + text today — `surface_forms` is read nowhere. Keying
> the hover on it (§4) is new consumption to build.

### 3. One numbering-policy primitive; the difference is data, not code.

The two numbering behaviours collapse to a single per-section (per
series) policy object — **not** two code paths:

```
numbering = { start: int, step: int, assign: "insert" | "render" }
```

- **`patent-image-part`**: `{ start: 100, step: 5, assign: "render" }` —
  the callout is a **display label derived from reading-order position
  at render/export**, *not stored* (the ADR 0037 non-stored-numbering
  discipline). Inserting a part mid-draft renumbers the series for free,
  so the spacing stays clean and 100-aligned.
- **`components` / `bom`**: `{ start: 1, step: 1, assign: "insert" }` —
  the callout is **frozen into `meta.callout` at add time**, consecutive,
  and **stable under reorder** (a BOM item number should not move when
  the table is re-sorted).

So `assign` is the one knob that expresses "taken as they go, stable"
(`insert` → stored) vs "assigned nicely at the end" (`render` → derived,
recomputed). `start`/`step` express the 100-boundary/step-5 aesthetic vs
consecutive-from-1. This subsumes the unbuilt ADR 0037 §5 patent
numbering as the `assign="render"` case.

### 4. Two hover paths — string-matched surfaces vs handle-anchored numerals.

Hover is **not** one path. `_highlight_abbrevs` works by string-matching
occurrences in the prose text, which is right for a word but
**catastrophic for a numeral** (it would highlight every bare `105`).
So the two must be kept distinct:

- **String-matched surfaces** — short name, `mpn`, `surface_forms`.
  These are literal strings an author types ("the LM358", "the op-amp"),
  so they route through the generalized highlighter.
  `Store.defined_abbrevs(ref_id)` becomes `defined_terms(ref_id)`
  returning, per surface string, `{definition, mpn?, url?}`; the
  highlighter keeps its longest-first alternation + inflection guard.
- **Handle-anchored callouts** — `meta.callout` numerals are referenced
  by `dc…` + noun phrase and **substituted at render** (§3
  `assign="render"`). The hover attaches at the *rendered numeral*
  position (where the `dc…` reference resolved), **never** by
  string-matching a bare number.

The popover fragment (`preview/popover.html.j2`) gains **optional** rows:
manufacturer / MPN / a datasheet link. Patent hovers, whose leaves have
an empty bag, render the bare definition as today; part hovers render
the rich card. Same template, conditional rows.

### 5. Two section styles, one mechanism underneath.

`patent-image-part` keeps the patent framing (figures + reference
numerals, `assign="render"`). A sibling `components` / `bom` section
style provides the manufacturing framing (`assign="insert"`, the
attribute bag). Both are scaffold/prompt skins over the *same* structured
`term` leaf, `defined_terms` hover, and numbering primitive.

Concrete integration anchors (all in `precis_web/routes/drafts.py` unless
noted): a `manufacturing`/`system-spec` genre in `_DOC_TYPE_BRIEF`; a
`components`/`bom` entry in `_SECTION_STYLES` + `_SCAFFOLDS`; a new
section-style skill file (sibling of `data/skills/patent-image-part.md`);
and the optional popover rows in `preview/popover.html.j2` + the `.pa-pop`
style block. The `{start, step, assign}` numbering policy **binds to the
section style** (so the style, not each heading, carries it).

### 6. The table is a *derived projection*, never an authored `table` chunk.

The rendered components table / glossary / parts list is a **view over
the `term` leaves**, not a separately-authored `chunk_kind='table'`
chunk. A `table` chunk's cells are opaque markdown — a cell cannot be an
addressable (`dc…`), embeddable, hover-previewed entity, which is exactly
what a part must be. Authoring the same data twice (table rows *and*
leaves) would give two sources of truth that drift.

So: **parts are `term` leaves (source of truth); the table is rendered
from them** — the ADR 0035 discipline ("edit the data, not the derived
text") applied to a whole section. The projection selects by
`meta.registry` and orders by the §3 numbering policy.

**The projection is placement-independent** — it unions *every* matching
`term` leaf in the draft regardless of which heading it sits under. This
is already how the hover behaves (`defined_abbrevs` has no heading
filter), and it is what makes §7 safe: a mis-filed leaf still appears in
the right table, so a stray cluster is cosmetic-in-the-tree, never
data-loss.

### 7. One home per registry: a role-tagged singleton heading.

**Observed failure:** abbreviation definitions clustered under *two*
headings. Cause: `ensure_glossary_heading` matches `lower(text) =
'glossary'` exactly, so a renamed/imported heading (`Abbreviations`,
`Glossary of Terms`, a trailing space) fails to match and a **second**
"Glossary" is minted; and a `term` added with an explicit `at=` bypasses
the auto-home entirely.

**Fix** — generalize to `ensure_registry_heading(ref_id, role)`, one per
`role ∈ {glossary, parts, components}`:

- Look the home heading up **by `meta.registry == role`** (a stable tag),
  not by heading text — survives rename, cannot be duplicated by wording.
- If absent, **adopt a legacy text-matched heading** (stamp
  `meta.registry` on the existing "Abbreviations"/"Glossary…") *instead
  of* creating a new one; create + stamp only as a last resort.
- Route a `term` `put` by its `meta.registry` even when `at=` is given
  (or reconcile it), so explicit placement can't scatter a registry leaf.

**Invariant: at most one registry heading per role per draft.** A lazy
reconcile (on term-add and cheaply at view-time) picks the earliest-`pos`
role heading as canonical, reparents stray leaves + any duplicate's
children under it, and retires the emptied duplicate. §6's
placement-independent projection is the belt; this is the suspenders.

## Consequences

- **No migration, no new chunk_kind.** The whole feature is `meta` JSONB
  keys on the existing `term` leaf + a policy object on the section.
  Respects the append-only-body-chunks invariant and ADR 0037 §0
  (stay-on-`term`).
- **`defined_abbrevs` widens to `defined_terms`.** A per-chunk keying
  change (short → short/callout/mpn/surface_forms) and a richer return
  record; the abbreviation-highlight caching (`_ABBREV_CACHE`) and the
  matcher survive intact.
- **The patent parts registry stops being prompt-only.** Reference
  numerals become structured, hover-backed, and rendered via the
  policy — closing the ADR 0037 §5 gap.
- **One authored `mpn`/`url` per part** flows into the popover and, later,
  an exportable BOM table; the callout stays the in-doc reference.
- **`assign="render"` numerals are derived, never stored** — reorder-safe
  and consistent with ADR 0037; `assign="insert"` callouts are stored and
  deliberately reorder-stable.
- **The table is a projection, not a chunk** — no `table` chunk is
  authored for a registry; the section renders from its `term` leaves
  (§6), so the reader table and the hover never disagree.
- **`ensure_glossary_heading` → `ensure_registry_heading(role)`** — the
  text-match is replaced by a `meta.registry` lookup + legacy-adopt + a
  one-heading-per-role reconcile (§7); the two-cluster bug closes for all
  three hoverlings at once.

## Sequencing

1. **Structured `term` + `defined_terms` + the manufacturing BOM**
   (`assign="insert"`, step 1, the attribute bag + rich popover). Serves
   the system-description document immediately.
2. **Managed-series renumber** (`assign="render"`, spaced policy) —
   lights up the patent reference numerals as the *same* mechanism.

## Out of scope (v1)

- A dedicated `part` / `component` chunk_kind with type-specific managed
  render (the heavier ADR 0037 §0 expansion) — reconsidered only if
  `term`-plus-`meta` stops sufficing.
- Exporting a standalone, sortable BOM table artifact (the reader hover +
  in-prose callouts ship first; a `view='bom'` table export is a
  follow-on).
- Numeral/part-name antecedent-basis consistency checking (the ADR 0037
  §3a patent review pass).

## Rejected

- **A new top-level kind** for the manufacturing document — no distinct
  corpus role, namespace, or citation semantics justifies it (§1).
- **Separate `part` and `component` chunk_kinds** — proliferates kinds
  and re-diverges two registries that are one abstraction (§2).
- **Two numbering implementations** — the spaced-at-export vs
  consecutive-as-added behaviours are one primitive with a 3-field policy
  (§3); building them separately duplicates the renumber logic.
