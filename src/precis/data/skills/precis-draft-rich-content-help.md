---
id: precis-draft-rich-content-help
family: drafting
title: precis — figures, images, and data tables in a draft
summary: figure chunks (blob/graph/canvas media, clearance gate by origin), and table chunks (structured data, four edit forms, LaTeX-recovered grids)
answers:
  - how do I add a paragraph, figure, or table to a draft?
  - how do I attach an image to a draft chunk, and who owns clearance for a third-party figure?
  - how do I turn a data table into a rendered graph in a draft?
  - how do I edit one cell of a table chunk without rewriting the whole grid?
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

**Structure view** (`origin='original'`): a ball-and-stick picture of a
`structure` design, rendered server-side (no image bytes needed). Pass the
recipe in `meta={'view': {...}}`:

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
periodic sheet shows missing bonds at the edges). It pins the live slug; there is no `se:` source yet (render the
structure an `se` block is bound to).

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
grid), `cell='A1'`/`{'row':,'col':}` + `text=` (one cell, 1-based, row 1 =
header), `find=` + `text=` (find-replace across string cells, literal),
`sub='s/a/b/'` (regex across cells, commits immediately, no dry-run); plus
`caption=`/`regen=` for metadata only. `cell=` is type-inferred
Excel-style (int → finite float → bool → else string): `text='1.523'`
lands as a JSON number; a header cell (row 1) stays a string; a
non-finite `NaN`/`inf` stays a string. Excel-eager inference turns a
leading-zero code like `007` into int `7` — send the full `table=`
payload to force a type. An out-of-range/malformed `cell=` is refused,
naming the table's actual dimensions; a zero-match find-replace is
refused too (chunk untouched); only one of `table=`/`cell=`/`find=`/`sub=`
per edit.

A LaTeX-imported table flagged `needs-table-review` recovers its grid by
re-parsing the chunk's own raw LaTeX, so it's editable (and citable) like any
other — cells recovered this way stay **strings** (raw LaTeX carries no type
information). A chunk whose `tabular` genuinely isn't in its text refuses with
"no stored data"; don't hand-type a `table=` grid to defeat that, it risks
mangling live content.

For a LaTeX-sourced chunk the recovered grid **only addresses** the edit, it
never re-serialises the chunk: `cell=`/`find=`/`sub=` patch the matched span
*inside the raw LaTeX*, leaving the rest byte-for-byte intact — no grid is
persisted and the flag stays put, since nothing canonical was stored. The grid
is lossy (`{caption, header, rows}` and nothing else). Two consequences:

- A `cell=` address that can't be safely mapped — typically inside a
  `\multicolumn` span — **refuses**, chunk unchanged, rather than guessing.
- `caption=`/`regen=` can't ride along with a `cell=`/`find=`/`sub=` edit on
  such a chunk: caption is re-derived from `\caption{}` in the text, so patching
  metadata wouldn't be read back. Passing both is a `BadInput`.

`table=` is unchanged — a declared wholesale replacement, so it still re-derives
from the grid you hand it.

For a cell holding raw LaTeX (`$\sim$` and friends), prefer `cell=`/
`text=`/find-replace over the whole `table=` **dict**: a value nested in
a `table=` dict doubles its backslashes on the wire, while
`text=` round-trips one correctly. For a whole grid with backslashes,
pass `table=` as a JSON **string**, not a dict:
`table='{"header": [...], "rows": [["$\\sim$3 aJ"]]}'` — decoded once
server-side, the same reliable channel `caption=` uses.

## See also

- [[precis-draft-help]] — the draft kind, addressing, and the full verb/param quick reference.
- [[precis-figure-help]] — the standalone `figure` kind: an interactive SVG canvas, distinct from a draft's figure chunks.
