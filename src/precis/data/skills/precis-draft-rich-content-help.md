---
id: precis-draft-rich-content-help
family: drafting
title: precis — figures, images, and data tables in a draft
summary: figure chunks (blob/graph/canvas/structure-view media, clearance gate by origin), and table chunks (structured data, four edit forms, LaTeX-recovered grids)
answers:
  - how do I add a paragraph, figure, or table to a draft?
  - how do I attach an image to a draft chunk, and who owns clearance for a third-party figure?
  - how do I turn a data table into a rendered graph in a draft?
  - how do I render a structure or se block as a figure in a draft?
  - how do I edit one cell of a table chunk without rewriting the whole grid?
  - how do I add a calculated column, footer total, or column stats to a draft table?
applies-to: put/edit (kind='draft')
status: active
tags: [drafting]
kinds: [draft]
---

# precis-draft-rich-content-help — figures and tables

## Figures & images

A **figure** is a chunk whose caption is `text` and whose image bytes are
stored separately (never in `text`): `chunk_kind='figure'`, image
**base64** in `image=`, plus `origin=`:

```python
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="figure",
    text="Fig 1. Device cross-section.",
    image="<base64>",
    origin="original",
    at={"after": "dc12"},
)  # our own diagram/schematic
```

`origin` ∈ `{original, own_graph, third_party}` drives a **clearance
gate**: a `third_party` figure needs a **granted, unexpired**
`permission={'publisher', 'permission_id', 'status', 'granted_at',
'source_paper'}` (also accepts `requested_at`/`scope`/`required_credit`;
`status` ∈ `requested|granted|denied`) or **export fails**. `mime=` is
sniffed when omitted. Set clearance later with `edit(kind='draft',
id='dc<id>', origin='third_party', permission={…})` — caption/image
bytes stay put. The caption **is** the figure's `text`, so it edits like
any other prose: `edit(kind='draft', id='dc<id>', text='…')`.

A figure's **medium** is separate from `origin`: a static **blob**
(`image=` above), a data-driven **graph** (`own_graph` + a render recipe,
below), or an editable SVG canvas (`has-figure` edge). Clearance is
medium-aware — no blob and no canvas = **uncleared**. On export a raster
blob embeds directly; an SVG (blob or canvas) rasterises to PNG.

**Graph** (`origin='own_graph'`): give it **`render=`** (the Python that
draws it) instead of `image=`, and **`plots=[dc<id>]`** (the data/table
chunks it reads):

```python
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="figure",
    text="Fig 2. Band gap vs lattice constant.",
    plots=["dc42"],  # the data/table chunk(s) it renders
    render=(
        "import matplotlib.pyplot as plt\n"
        't = data["tables"][0]\n'  # plotted chunks arrive as data["tables"]
        'plt.scatter([r[0] for r in t["rows"]], [r[1] for r in t["rows"]])'
    ),
    at={"last": True},
)
```

The render code runs **sandboxed, lazily** (never at `put` time): the first
export (docx/LaTeX) or reader view of the figure renders it, saves the PNG,
and later exports reuse it. It receives `data={'tables': [...]}` and `out`
(the PNG path). A render error shows up as an export warning (the figure
falls back to caption-only) and is retried on the next export. The image is
**deferred** — a placeholder until then. Each export or view compares the
plotted tables' current content to the key stamped at the last render and
re-renders on mismatch, so editing a plotted table refreshes the graph (the
`plots` edge is the one reactive recompute). A graph is otherwise an ordinary `figure` chunk — clearance,
caption, export all apply identically.

## Picture a structure or se block — a view figure

A **structure view** figure (`origin='original'`) is a ball-and-stick
picture of a `structure` design, rendered server-side (no image bytes
needed). Pass the recipe in `meta={'view': {...}}`:

```python
put(kind="draft", id="nanotrans", chunk_kind="figure",
    text="Fig 3. (8,8) nanobud, bud22.",
    meta={"view": {"source": "structure:nanobud-review-figs-bud22",
                   "camera": {"azimuth_deg": 30, "elevation_deg": 15},
                   "refine": 2}},   # refine 0 wire | 1 shaded | 2 gradients
    at={"last": True})
```

`camera` takes `azimuth_deg`, `elevation_deg`, `twist_deg`, `projection`
(`ortho`|`persp`), `zoom`; `style` takes `stick_radius`, `ball_scale`,
`color` (`cpk`|`mono`). Rendering is lazy like a graph: first export/view
renders, and a later geometry edit to the structure re-renders on the next
export. Limits: values are validated at `put` (zoom 0.1-20, `stick_radius`
0.01-2 Å, `ball_scale` 0.05-3, `refine` an int); structures over 20000
atoms are refused, and large ones render at reduced `refine`. **Bonds that
cross a periodic cell wall are not drawn** (molecular structures only; a
periodic sheet shows missing bonds at the edges). It pins the live slug.

From an `se` design, name the block instead of the structure:
`meta={'view': {'source': 'se:nanobud-review-figs', 'block': 'bud22', ...}}`.
The block's bound structure is resolved at `put` and pinned (`block` may
be omitted when exactly one block is bound; the error lists the bound
blocks otherwise). A generated block's structure is `<design>-<block>`;
re-generating it (delete that structure, then `generate`) re-mints the
same slug, and the figure re-renders on the next export. Rebinding the
block to a structure with a different slug does not move the figure — put
a new one. The `put` reply names the provenance (`se:<design> block '<b>' ->
structure:<slug> v<N>, generator <g>`); copy what the reader needs into
the caption, nothing is added to it for you. One block per figure; a
composite of several posed blocks is not supported.

## Data / table chunks

A `chunk_kind='table'` chunk holds **structured data, not prose** — pass
it as `table={header, rows}`, not `text=`; the markdown you read back is
*derived* (regenerated on every write), so the numbers stay the source
of truth and numerics-indexable.

```python
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="table",
    table={"header": ["element", "gap_eV"], "rows": [["Si", 1.12], ["Ge", 0.67]]},
    caption="Measured band gaps",  # the legend (optional); rides in the derived text
    regen={"source": "dft", "cmd": "vasp relax"},  # inert provenance metadata
    at={"last": True},
)
```

Editing: change the data, not the rendered text (`text=` is rejected on a
table chunk); the four forms are `table={'header':…, 'rows':…}` (whole
grid), `cell='<row-id>.<col-id>'` + `text=` (one cell — every row and
column carries a stable 4-char id minted at creation; `get` prints them
on an `ids:` line under the grid, and `id='dc<id>~<rowid>.<colid>'`
reads the one cell; the column side may
also be the column's name), `find=` + `text=` (find-replace across
string cells, literal), `sub='s/a/b/'` (regex across cells, commits
immediately, no dry-run); plus `caption=`/`regen=` for metadata only.
`cell=` is type-inferred
Excel-style (int → finite float → bool → else string): `text='1.523'`
lands as a JSON number; a
non-finite `NaN`/`inf` stays a string. Excel-eager inference turns a
leading-zero code like `007` into int `7` — send the full `table=`
payload to force a type. An id that names no live row/column is refused
listing the table's ids (a deleted id never reappears); a zero-match
find-replace is refused too (chunk untouched); only one selector per
edit. Column renames go through the column schema (`rename_column=`),
not `cell=` — the header row carries no id.

For anything beyond single-cell edits use the **toon surface**
(precis-table-help): `get(id='dc<id>', view='toon')` renders the grid
with `#` row ids and `<id>:<name>` header cells plus a `hash:` line;
`patch=` edits a subset (`''`=unchanged, `null`=clear,
`mode='update'|'upsert'`), `toon=` replaces the whole grid, `base=<hash>`
guards against clobbering a concurrent write, and `add_column=`/
`drop_column=`/`rename_column=`/`move_column=`/`move_row=`/`rows=`+`at=`/
`drop_rows=`/`sort=` reshape by stable id — all via `args={…}`.

## LaTeX-imported tables

A LaTeX-imported table flagged `needs-table-review` recovers its grid by
re-parsing the chunk's own raw LaTeX, so it's editable (and citable) like any
other — cells recovered this way stay **strings** (raw LaTeX carries no type
information). A chunk whose `tabular` genuinely isn't in its text refuses with
"no stored data"; don't hand-type a `table=` grid to defeat that, it risks
mangling live content.

For a LaTeX-sourced chunk the recovered grid has **no ids** — `find=`/`sub=`
patch the matched span *inside the raw LaTeX*, leaving the rest
byte-for-byte intact (no grid persisted, flag stays put), while `cell=`
needs ids it doesn't have. So the first `cell=` edit **converts** the
chunk to the canonical id-addressed grid — the raw LaTeX layout is
dropped (`\multicolumn` spans flatten, rules and `\,` spacing go), the
`\caption{}` legend survives in the derived text's lead line and the
`\label{}` anchor in `meta.label` — and then refuses, naming
the just-minted ids you couldn't have known; the retry with a real
`'<row-id>.<col-id>'` lands. Until that conversion, prefer `find=`/`sub=`
if the raw LaTeX must keep its layout. `caption=`/`regen=` can't ride
along with a `cell=`/`find=`/`sub=` edit on such a chunk: caption is
re-derived from `\caption{}` in the text, so patching metadata wouldn't
be read back. Passing both is a `BadInput`.

`table=` is unchanged — a declared wholesale replacement, so it still re-derives
from the grid you hand it. (A pre-schema table with no ids mints them on
its first write touch — cell=, find=, even a caption-only edit — and a
refusing cell= names the just-minted ids so the retry lands.)

For a cell holding raw LaTeX (`$\sim$` and friends), prefer `cell=`/
`text=`/find-replace over the whole `table=` **dict**: a value nested in
a `table=` dict doubles its backslashes on the wire, while
`text=` round-trips one correctly. For a whole grid with backslashes,
pass `table=` as a JSON **string**, not a dict:
`table='{"header": [...], "rows": [["$\\sim$3 aJ"]]}'` — decoded once
server-side, the same reliable channel `caption=` uses.

## Formulas, footer aggregates & stats

A column can carry a **formula** — declare it in `columns=` (the split-arg
form of `table=`):

```python
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="table",
    columns=[
        {"name": "qty"},
        {"name": "unit_price"},
        {"name": "line_cost", "formula": "qty*unit_price"},
    ],
    rows=[[2, 0.5], [4, 1.25]],
    footer={"line_cost": "sum"},
)
```

Formulas reference **column names** (`qty*unit_price`), evaluate per row,
and are stored internally by column id — renames rewrite nothing.
Operators: `+ - * / // % **`, comparisons, `and/or/not`, and
`x if cond else y`. Functions:
`abs round min max sum avg sqrt log exp if_(cond, a, b)`. Names are the
only references — attribute access, subscripts, f-strings, comprehensions
and imports are refused at write time. So is a formula that names a
column the table doesn't have: a new/changed formula must resolve every
name, and refuses with the valid column names/ids — a typo can't land.
A column a *stored* formula uses being dropped LATER is the `#REF`
case: the edit lands, the reply warns, and the dependent cells store
`#REF`. Any other per-row fault stores an error
marker cell and the write still lands — `#DIV/0`, `#REF`, `#VALUE` (a
type error — `*`/`+` on a text cell, or `%` with text as the LEFT
operand), `#NUM` (over-cap number), `#CYCLE` (a formula-dependency
loop, whose columns are never evaluated), `#ERROR`. An error-marker
operand propagates to dependents — the first error operand wins, so
`#REF` stays `#REF`. The reply's warning names the faulting cells, and
for a cycle names the loop's columns with the way out.

On an **existing** table, `edit` has no `columns=` kwarg:
`add_column={'name': 'total', 'formula': 'a*2'}` adds a formula column;
`table={'columns': [...], 'rows': [...]}` (the whole schema) changes or
removes one. `toon=` can't write a formula either — a header
re-declaring the stored formula round-trips, any other refuses.

**Footer aggregates** pin a summary function to a column:
`footer={'line_cost': 'sum'}` — functions
`sum mean median std min max count` (`std` is the SAMPLE std, like
spreadsheet STDEV; `count` counts non-empty cells). Every write recomputes
them; the
reply notes how many rows a footer skipped as empty/non-numeric. `edit`
accepts `footer=` by itself to set or replace the spec (`footer={}`
clears it) — it still counts as the edit's one selector, so it can't ride
with `table=`/`cell=`/`find=`/`sub=`. Read the results on the `footer:`
line under the grid, next to `ids:`.

**`view='stats'`**: `get(kind='draft', id='dc<id>', view='stats')` reports
per-column `n` (numeric cells), `nonempty`, `empty`, mean, sample std and
range — computed on read, never stored.

## See also

- [[precis-draft-help]] — the draft kind, addressing, and the full verb/param quick reference.
- [[precis-figure-help]] — the standalone `figure` kind: an interactive SVG canvas, distinct from a draft's figure chunks.
