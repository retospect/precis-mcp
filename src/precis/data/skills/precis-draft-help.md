---
id: precis-draft-help
title: precis — the editable document kind
summary: author a living document as chunks — create, read (outline/verbatim), edit text, reorder/reparent, soft-delete; markdown-ish prose with [dc…] links (any handle) and bare [pc…] paper-chunk citations
applies-to: get/search/put/edit/delete (kind='draft')
status: active
---

# precis-draft-help — author a living document

A `draft` is an **editable, chunk-native document** — the living
source of a project's write-up. Postgres is canonical; it exports to
LaTeX/PDF/Word. Unlike a `paper` (frozen), a draft's chunks are mutable
in structure (reorder/reparent) and in text. **One draft per project**;
a snapshot/backup is a *freeze* (see below).

Everything goes through five verbs — **no new verbs**: `put` (create /
add a chunk), `edit` (change text **or** move), `get` (outline /
verbatim), `delete` (soft-retire), `search` (lexical / semantic over
prose). A draft is **not** taggable or linkable as a whole (`tag`/`link`
on `kind='draft'` raise `Unsupported`) — cross-references are markdown
refs embedded in prose, and the per-chunk autolinker materialises a
backlink for each — a `cites` edge to a citable source
(paper/patent/finding), `related-to` otherwise (see *References in
prose*). The edge is grounded on **both** ends: it records the `dc<id>`
chunk the reference sits in (source) as well as the target chunk, so a
reader can see *which paragraph* cites a finding/paper, not just that the
draft as a whole does.

## Reorder chunks — quick reference

```python
edit(kind="draft", id="dc16", move={"before": "dc15"})  # reorder among siblings
```

`move=` takes `before`/`after` (siblings), `parent`+`before`/`after`/`last`
(reparent), or `into`+`last` (append to a section). Full grammar +
examples: *Reorder / move*, below.

## Search a draft (lexical / semantic)

```python
search(kind="draft", q="direct air capture")  # across ALL drafts
search(kind="draft", q="direct air capture", scope="test01")  # one draft
search(kind="draft", q="amine sites", scope="dc8")  # subtree under a heading
search(kind="draft", q="capture", mode="lexical")  # verbatim / keyword
search(kind="draft", q="capture", mode="semantic")  # by meaning (default: hybrid)
search(kind="draft", q="methods", headings_only=True)  # jump to a section heading
```

`mode=` is the same axis as everywhere else: `lexical` (exact / keyword),
`semantic` (meaning), default `hybrid` (both, fused). `scope=` narrows to
one draft (slug) or one section (a `dc<id>` → that chunk's subtree); omit
it to search every draft. `search(id='dc<id>', q='…')` is accepted too —
the handle already names the kind and the chunk is the scope. Each hit
shows its `draft:<slug>` and `dc<id>`; read one with `get(id='dc<id>')`.

## Find & substitute by regex (vi `/pattern` and `:%s/a/b/`)

Semantic / lexical search is about *meaning*; when you need a **literal**
pattern — markup, punctuation, a malformed citation form — use the regex
grep and substitute. The pattern is **Python regex** (`\w`, `\d`, groups,
`|`), run verbatim over chunk text. This is the tool to audit and fix the
house-style rules: stray `**bold**` / `_italic_`, em-dashes `—`, double
spaces, a bare `paper:123` cite.

**Find — `search(mode='regex')`:**

```python
search(kind="draft", mode="regex", q=r"\*\*\w+\*\*", scope="nanotrans")  # find **bold**
search(kind="draft", mode="regex", q="—", scope="nanotrans")  # find em-dashes
search(kind="draft", mode="regex", q="TODO", scope="nanotrans", flags="i")  # case-fold
```

Each hit shows `draft:<slug>  dc<id>  [kind]` and, per match, `L<line>:<col>`
with the matched span wrapped in `»…«`. `flags='i'` case-folds, `flags='s'`
makes `.` cross newlines; `^`/`$` always anchor per line. Find reads
table/figure text too (read-only).

**Substitute — `edit(sub=…)`, dry-run by default:**

```python
# dry-run: reports counts + a before→after sample per chunk, writes nothing
edit(kind="draft", id="nanotrans", sub={"find": "—", "replace": ", "})

# commit it
edit(kind="draft", id="nanotrans", sub={"find": "—", "replace": ", "}, apply=True)

# backreferences work — strip bold to plain text
edit(
    kind="draft",
    id="nanotrans",
    sub={"find": r"\*\*(\w+)\*\*", "replace": r"\1"},
    apply=True,
)

# the s/// string form is accepted too (delimiter is the char after s)
edit(kind="draft", id="nanotrans", sub="s/  / /", apply=True)  # collapse double spaces
```

`replace` is a Python regex template, so `\1` / `\g<name>` resolve.
Substitution always replaces **every** occurrence in each chunk. Each
rewritten chunk goes through the **normal edit path** — re-embed, keywords,
gist, autolinks all re-derive, and the prior text is kept in chunk history
(so a bad `s///` is recoverable). When the scope is a **slug or a section**
(a `dc<id>` heading covering many chunks), **table and figure chunks are
skipped** (their text is derived / a caption — edit the data, not the text);
the report names any that matched. Point `find=`/`sub=` at a table chunk's
own `dc<id>` directly, though, and it edits that table's *cells* instead —
see "Data / table chunks".

**Scope (both find and substitute)** is the same axis as search, three
levels:

| `scope=` / `id=` | covers |
|---|---|
| a draft slug (`'nanotrans'`) | the **whole draft** |
| a `dc<id>` **heading** | that **section** (the heading's subtree) |
| a `dc<id>` **leaf** | just **that chunk** |
| *(find only)* omitted | **every draft** |

Substitute **requires** a scope (a slug or a `dc<id>`) — there is no
corpus-wide rewrite. Point it at a section (`id='dc8'`) to confine a
substitution to one part of the document.

## Find a project's draft

A draft carries **no `project:` tag** — that tag lives on the project
*todo*, and the draft is bound to it 1:1 by a `draft-of` link.
`get(kind='draft', project=…)` is the reverse lookup — resolves the
project todo and returns the bound draft's outline directly (a list, if
somehow more than one is bound):

```python
get(kind="draft")  # list ALL drafts (no project filter yet)
get(kind="draft", project="<project-todo-id>")  # → that project's draft outline
```

`project=` and `id=` are mutually exclusive — pass one or the other.
(The planner prompt also tells an editor agent which draft it is in, so
this is rarely needed mid-edit.)

## Addressing — universal handles, never numbers

Each chunk has a stable **handle**: the computed universal handle
`dc<chunk_id>` (e.g. `dc41`) — `dc` = draft chunk, then its id. In verbs
just pass it: `id='dc41'` (handles are globally unique — no draft name
needed); the draft *record* is addressed by its name/slug or its `dr<id>`
handle. You **never** type or compute a handle for a *new* chunk — `put`
returns it. Reading windows use the relative grammar (`dc41-2..3`,
`dc41+1`, `dc41^`). Positional `~N` ordinals are **not** offered for
drafts (they rot on insert); use handles.

## Start a new draft

A draft is **born with a title heading** (so it is never empty), bound
1:1 to its project todo by a `draft-of` link. The brief lives on the
project's `meta.workspace.brief`; the draft carries `path`/`format`.

```python
# 1 — create the draft (returns the draft + its title heading dc1)
put(
    kind="draft",
    id="nanotrans",
    project="<project-todo-id>",
    title="Nanoscale Transistors",
    meta={"workspace": {"path": "projects/nanotrans", "format": "tex"}},
)

# 2 — add a section heading after the title
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="heading",
    text="Introduction",
    at={"after": "dc1"},
)  # → returns dc12

# 3 — a paragraph under it
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="paragraph",
    text="Nanoscale transistors …",
    at={"into": "dc12", "last": True},
)
```

`at` places the new chunk (all parts optional): `{'first'|'last': True}`,
`{'into': 'dc<id>'}`, `{'before'|'after': 'dc<id>'}`.

## Fork a draft — deep-copy into a new project

```python
put(kind="draft", copy_of="nanotrans", project="<new-project-todo-id>")
# → forked draft 'nanotrans-copy' bound draft-of the new project; source untouched
put(
    kind="draft",
    copy_of="nanotrans",
    project="Nanotrans review pass",
    id="nanotrans-r2",
)
# → project= a bare string mints a fresh project todo titled that; id= names the new slug
```

Deep-copies the WHOLE source draft — every chunk (live and retired), its
hierarchy, and every link touching it — into a NEW draft; the source is
never touched. `project=` is required: an existing project todo (id /
`todo:N`) or a title string that mints a fresh one; refuses if that
project already owns a draft (one draft per project still holds, even for
a fork). `id=` seeds the new slug (deduped `-2`/`-3`… on collision);
default `<src>-copy`. The copy starts fully unreviewed (the `chunk_review`
ledger is not carried over) — use this to spin off a review pass, or any
draft you want to diverge from the original without touching it.

## Scaffold — lay down a genre's standard sections

`edit(kind='draft', scaffold=<class>)` appends a document class's standard
section skeleton after whatever is already there (styled headings, ready
to fill) — the same table the web `/drafts/new` genre picker uses:

```python
edit(kind="draft", id="nanotrans", scaffold="paper")
# → styled headings: Abstract, Introduction, Related Work, Methods,
#   Results, Discussion, Conclusion
```

Classes: `paper`, `patent`, `report`, `review` (survey), `manufacturing`,
`book` (multi-chapter monograph — Preface, Introduction, Background,
Chapter 1-3, Conclusion, Bibliography), `summary` (short digest, distinct
from the comprehensive `review` — Summary, Key Points, Details,
References). An unknown class raises `BadInput` listing the valid ones.
`id` may be the draft slug or any `dc<id>`/`¶handle` inside it (resolves
to the owning draft, like `authors=`). Scaffolding never overwrites or
reorders existing sections — it only appends; re-scaffolding a draft that
already has sections adds a second copy, so scaffold once, early.

## Rename the document — `title=`

```python
edit(kind="draft", id="nanotrans", title="Nanoparticle transport in packed beds")
```

A draft carries its name in two places: `refs.title` (what search hits,
lists, and link chips show) and the title `heading` chunk at the top of the
document (what the reader and every export show). `title=` writes **both**,
in one transaction, so they can't drift — including from an already-drifted
state, so this is also the repair for a draft whose heading you edited
directly. The heading is edited in place, so anchors into it stay live.

`id` may be the draft slug or any `dc<id>`/`¶handle` inside it (resolves to
the owning draft, like `authors=`). A blank title raises `BadInput`. A draft
with no root heading (an import) renames the ref alone and says so.

## Byline — authors & affiliations

A draft carries a **document-level byline** (authors are a property of the
whole document, never of a paragraph). Set it with `edit(authors=…)`;
`id` is the draft slug, not a chunk handle:

```python
edit(
    kind="draft",
    id="nanotrans",
    authors=[
        {
            "name": "Doe, Jane",
            "affiliation": "Massachusetts Institute of Technology",
            "ror": "https://ror.org/042nb2s44",
        },
        {"name": "Roe, John", "affiliation": "Caltech"},  # affiliation/ror optional
        "Solo Author",  # a bare string is fine too
    ],
)
```

Each entry is `{'name', 'affiliation'?, 'ror'?}` (or `{'family','given', …}`,
or a bare name string). `ror` is the institution's
[ROR](https://ror.org) id — the canonical, de-duplicated organisation
identifier; two authors sharing a ROR collapse to **one** numbered
affiliation in the byline. Setting `authors=` **replaces** the whole
byline (it is not additive). The byline renders in the web reader and in
**both** exports (PDF via `authblk`, .docx), with the org name hyperlinked
to its ROR. No affiliations → a plain name list.

The web reader has the same editor — an **authors ▾** dropdown on the
draft page, one author per line as `Name | Affiliation | ROR` (affiliation
+ ROR optional), posting to `/drafts/<slug>/authors`.

## Add prose — one paragraph per put

Write **one paragraph per `put`**. A longer `put` is split at block
boundaries (blank lines; lists/code/tables stay whole) and returns one
handle per chunk:

```python
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="paragraph",
    text="First para.\n\nSecond para.",
    at={"after": "dc12"},
)
# → returns [dc13, dc14]
```

**Every block carries prose — write the sentences, not just the
scaffold.** A paragraph that is only a citation, a formula, a list of
references, or a bare claim with no explaining text is incomplete: state
the point in running prose, *then* support it. If a block is genuinely
just a figure / equation / table, give it the matching `chunk_kind` and a
one-line caption — don't leave a "paragraph" that is structure without
saying anything. (Why it happens: it's easy to drop the evidence and
move on; the prose that ties it to the argument is the actual writing.)

**Style: plain prose, no emphasis markup.** Write in plain declarative
sentences, one idea each. **Do not use bold or italics for emphasis.**
`**bold**` and a single `*word*` italic both render (in the reader, PDF and
Word) but read as shouting in a research write-up; `_italic_` is NOT rendered
and leaves literal `_` markers in the text (it collides with `$x_1$` math
subscripts). Let sentence structure carry the weight.
**No em-dashes** (the `—` character): split the thought into separate
sentences, or use a colon, comma, or parentheses instead. (Headings
already stand out, so you do not need bold on top.)

**Units & temperatures: literal sign, no space.** Write a temperature as
`63°C` — the degree sign `°` (U+00B0) immediately after the digit, then
`C`, with no space. A range is `63–65°C`; a tolerance is `±1°C` (the `±`
sign U+00B1, not `+/-`). Do **not** use a superscript, the single
character `℃`, an `o`/`º` stand-in (`63oC`), LaTeX (`^\circ`, `\degree`,
`\textdegree`), a space (`63 °C`, `63° C`), or the spelt-out "degrees
Celsius". A malformed temperature lands but trips a `⚠ temperature/unit
formatting` hint on the write so you can fix it.

## Figures & images

A **figure** is a chunk whose caption is the face (`text`) and whose
image bytes live in the database (a `chunk_blobs` row, 1:1 with the
chunk — never in `text`). Add one with `chunk_kind='figure'`, the
caption as `text`, the image **base64** in `image=`, and an `origin=`:

```python
# our own diagram / schematic
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="figure",
    text="Fig 1. Device cross-section.",
    image="<base64>",
    origin="original",
    at={"after": "dc12"},
)

# a plot we generated from data (ships a data supplement — see graphs)
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="figure",
    text="Fig 2. I–V curves.",
    image="<base64>",
    origin="own_graph",
)

# reused from another paper — REQUIRES the publisher paper-trail
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="figure",
    text="Fig 3 (after Smith 2019).",
    image="<base64>",
    origin="third_party",
    permission={
        "publisher": "Springer Nature",
        "permission_id": "SNCSC-2026-0451",
        "status": "granted",  # requested|granted|denied
        "requested_at": "2026-06-10",
        "granted_at": "2026-06-18",
        "scope": "this manuscript, print + electronic",
        "required_credit": "Reprinted by permission …",
        "source_paper": "smith19",
    },
)  # cite-key of the source
```

`origin` ∈ `{original, own_graph, third_party}` records where the figure
came from and drives a **clearance gate**: a `third_party` figure is
cleared only with a **granted, unexpired** permission. The reader shows a
warning banner listing any uncleared figures (and an all-clear note when
every figure passes), and **export fails** on an uncleared figure — so an
unlicensed image can't ship. A `third_party` figure **must** carry a
`permission` paper-trail — that is the whole point: track *with whose
permission*, *which permission number*, *when requested/granted*, *when it
expires*. `mime=` is sniffed from the bytes when omitted.
Permission lives in `meta.figure.permission`; the reader shows an origin
chip + a ✓/✗ clearance badge, and serves the image at
`/drafts/blob/<handle>`. In the **web reader** a per-block **"＋ figure"**
control uploads an image file directly (multipart) — for a `third_party`
image it reveals the permission form inline — so a human can drop in a
figure without base64. The clearance badge under a rendered figure is
**editable**: hover for the paper-trail, click to edit it. Programmatic
edits use `edit(kind='draft', id='dc<id>', origin='third_party',
permission={…})` — caption and image bytes stay put.

A figure's **medium** — *how the pixels are produced* — is separate from its
`origin` (ADR 0058). A figure can be a static **blob** (the `image=` upload
above), a data-driven **graph** (`own_graph` + a render recipe), or **our own
editable SVG canvas** — a `kind='figure'` drawing linked to the figure chunk by
a `has-figure` edge. In the **web reader**, a figure with *no image yet* renders
a **"create drawing"** placeholder (not a broken image): clicking it mints a
`kind='figure'` canvas seeded from the caption, links it, and opens the
`/figure` draw-with-the-model editor; a canvas-backed figure renders its SVG
inline with an **"✎ open in /figure"** affordance. Clearance is medium-aware — a
figure with no blob and no drawn canvas counts as **uncleared** ("no image
yet"), so an empty placeholder no longer reports "cleared to ship".

**Export** materialises figures into the PDF and Word output (ADR 0058 slice
4): a raster blob embeds directly, and an SVG — a blob-SVG or a linked canvas —
rasterises to PNG (via the bundled `resvg`), so `\includegraphics` (LaTeX) and
`add_picture` (docx) both carry the drawing. An image-less, canvas-less figure
is caught by the clearance gate before export.

> Graph regeneration (the plot's data + code as `figure_code` /
> `figure_data` chunks linked `derived-from`) is a later phase.

## Data / table chunks

A `chunk_kind='table'` chunk holds **structured data, not prose**. Pass the
canonical data as `table={header, rows}` — *not* `text=`. The markdown you
read back is **derived** from that data (regenerated on every write, never
hand-edited), so the numbers stay the single source of truth and stay
searchable / numerics-indexable.

```python
put(
    kind="draft",
    id="nanotrans",
    chunk_kind="table",
    table={"header": ["element", "gap_eV"], "rows": [["Si", 1.12], ["Ge", 0.67]]},
    caption="Measured band gaps",  # the legend (optional)
    regen={
        "source": "dft",
        "cmd": "vasp relax",
    },  # how the data was made (optional, inert)
    at={"last": True},
)
```

* **`caption=`** is the table's legend — it rides in the derived text so the
  table is findable by it.
* **`regen=`** records provenance / how to rebuild the data (a sim, a command,
  an ingest pointer). It is **inert metadata** — recorded, never executed.
* **Editing:** change the data, not the rendered text — plain `text=` is
  still *rejected* on a table chunk. Four ways, whole-grid down to one field:

  ```python
  # whole grid
  edit(
      kind="draft", id="dc42", table={"header": [...], "rows": [...]}
  )  # re-derives markdown

  # one cell — A1 address (row 1 = header) or {'row':,'col':} (1-based), + text=
  edit(kind="draft", id="dc42", cell="B2", text="1.523")  # row 2, col B
  edit(
      kind="draft", id="dc42", cell={"row": 1, "col": 2}, text="gap_eV_2"
  )  # rename header B

  # find-replace across cells (string cells only; numbers/bools untouched)
  edit(kind="draft", id="dc42", find="aJ", text="zJ")  # literal
  edit(kind="draft", id="dc42", sub="s/aJ/zJ/")  # regex — commits immediately, no dry-run

  # metadata only, data untouched
  edit(kind="draft", id="dc42", caption="New legend")
  edit(kind="draft", id="dc42", regen={"source": "manual"})
  ```
  (`dc<chunk_id>` is the chunk's address — `put` returns it.)

  A `cell=` value is type-inferred Excel-style (int → finite float → bool →
  else string), so `text='1.523'` lands as a JSON number and stays
  numerics-indexable; a header cell (row 1) is always stored as a string,
  and a non-finite `NaN`/`inf` stays a string. Inference is Excel-eager, so
  a leading-zero code like `007` becomes int `7`: to keep a numeric-looking
  value AS a string (or force any explicit type), use the full `table=`
  payload instead. An out-of-range or malformed
  `cell=` is refused, naming the table's actual dimensions; find-replace with
  zero matches is refused too (chunk untouched, same guard as prose
  find-replace), and only one of `table=`/`cell=`/`find=`/`sub=` may be
  passed per edit.

  For a cell holding raw LaTeX (`$\sim$` and friends), prefer `cell=`/`text=`
  or find-replace over resending the whole `table=` **dict**: the new value
  arrives on the string `text=` channel, which round-trips a single
  backslash correctly, whereas a value nested inside a `table=` dict doubles
  its backslashes (gr178512 — a client-side arg-serialization bug). When you
  must set a whole grid that contains backslashes, pass `table=` as a JSON
  **string** (not a dict): `table='{"header": [...], "rows": [["$\\sim$3 aJ"]]}'`.
  A string `table=` is `json.loads`-decoded once server-side — the same
  reliable channel `caption=` uses — so single backslashes survive; the dict
  form does not.

## Graph figures (computed from data)

A **graph** is a `figure` (the umbrella `chunk_kind`) whose image is *computed
from data*, not uploaded — `origin='own_graph'`. Instead of `image=`, give it
**`render=`** (the Python that draws it) and **`plots=[dc<id>]`** (the data/table
chunks it reads). The caption is `text=`, like any figure.

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

- The render code runs **sandboxed, out-of-band** (never at `put` time): it
  receives `data = {'tables': [...]}` (your `plots` chunks, in order) and `out`
  (the PNG path); an unsaved matplotlib figure is auto-saved. The image is
  **deferred** — a placeholder until the render lands, then it refreshes
  automatically whenever the plotted data changes (the `plots` edge is the one
  reactive recompute, ADR 0035).
- An *image* figure (uploaded `image=`, `origin∈{original,third_party}`) and a
  *graph* (computed, `own_graph`) are the **same `figure` kind** — they differ
  only in where the pixels come from. Clearance, caption, blob serving, export
  all apply identically.

## Read the document

```python
get(kind="draft", id="nanotrans")  # outline: handle | §-path | gist
get(
    kind="draft", id="nanotrans", view="outline"
)  # same — 'outline' is accepted explicitly
get(id="dc12")  # one chunk, verbatim source
get(id="dc12-5..3")  # that chunk + 5 before, 3 after
get(id="dc12", view="fisheye")  # verbatim center + graduated neighborhood
```

Navigate the **outline** first (cheap — one line per chunk), then pull
**verbatim** only for the region you act on. `dc<id>-2..3` is a
reading window (B before, A after, in reading order). `view='fisheye'`
is the same idea done for you: verbatim center, fanning out to summary
then keyword lines under the ancestor heading — see
`precis-fisheye-help`.

The outline ends with a **`## Work in progress`** block when todos are
working on this draft and are stuck or in flight — walked
draft → project → todo subtree. A `⚠ blocked` row carries a
`child-failed:<job>` bubble (a child job failed and parked the parent
out of the rotation); `⚙ in flight` is a live/queued job. Inspect with
`get(kind='todo', id=<id>)`; unblock a stuck todo by retrying, splitting,
or dropping it (`tag` off the `child-failed:` bubble + `STATUS:done`).
This is how a failed enrichment job *registers on the draft* instead of
silently stalling.

## Focus a chunk with its neighborhood, not alone (fisheye)

```python
get(id="dc12", extent="kwd")  # ancestor path + one-line bookmark
get(id="dc12", extent="summary")  # the chunk's gloss, alone
get(id="dc12", extent="verbatim")  # the chunk's full text, alone
get(id="dc12", extent="fisheye")  # verbatim center + reading-order neighbors
get(id="dc12", extent="fisheye+1hop")  # fisheye + everything it points at
```

Each rung strictly contains the previous. `fisheye` adds a graduated,
forward-biased span of the chunks around it (±5 full text, ±10 gloss, ±15
bookmark) under its ancestor heading — so you read a section in context
without pulling the whole draft. `fisheye+1hop` adds the reference ring:
cited papers/patents/datasheets, cross-referenced `[dc…]`/`[¶…]` chunks,
and notes linked to this one — grouped Cited / Cross-refs / Notes, one edge
out. A cited `[fi<id>]` (or `[pub_id]`) that names a live Taproot claim
hub gets its own **Claims** group, exploded into its derived
originators + a corroborator/contradictor summary
(`precis-fisheye-help`). Only wired for
`dc<id>`/`¶<base58>` chunk addresses on `kind='draft'` today, not
`kind='plan'`.

## Change a chunk's text

```python
edit(id="dc12", text="Nanoscale transistors, defined as …")  # whole-chunk rewrite
edit(
    id="dc12", mode="find-replace", find="60°C", text="65°C"
)  # substitute within the chunk
edit(id="dc12", find="60°C", text="65°C")  # find= alone implies find-replace
edit(id="dc12", find="typo phrase", text="")  # delete a span (text='')
edit(id="dc12", text="… big rewrite …", dry_run=True)  # PREVIEW the diff, write nothing
edit(
    id="dc12", text="… big rewrite …", dry_run="full"
)  # preview the whole post-edit text
```

Plain `text=` **replaces the whole chunk**. To change only part of a chunk,
use `mode='find-replace'` (or just pass `find=`): `find=` is located
**literally** and every occurrence is swapped for `text=`. If `find=` isn't
present in the chunk the edit is **refused** and the chunk is left untouched —
so a mistargeted find-replace can't erase the surrounding text. (For a
regex substitution across a whole section or draft, use `edit(sub=…)` above.)

**Preview a scary rewrite first.** Pass `dry_run=True` on a `text=` rewrite
or a `find=`/`text=` substitution to get a unified diff of current-vs-proposed
text and write **nothing**; `dry_run='full'` shows the entire post-edit chunk.
Re-run without `dry_run` to commit. (Structural ops — `move`/`style`/`table`/
`authors` — have no diff and reject `dry_run`; the regex `sub=` op previews by
default and commits on `apply=True`.)

In-place: the handle (and every reference to it) survives; embeddings /
keywords / gist re-derive automatically.

## Mark a chunk reviewed

```python
edit(kind="draft", id="dc12", review="human")  # record your sign-off
edit(kind="draft", id="dc12", review="human", verdict="needs-rework")
```

`review=` names the checker (`'human'` is the single human reviewer identity;
an automated checker like `'cites'`/`'flow'` records the same way from a
worker). `verdict=` is free text, default `'approved'`. It's an upsert keyed
on `(chunk_id, checker)` at the chunk's *current* content_sha — metadata-only,
no re-embed, no text touched — and a later text edit makes the chunk "dirty"
for that checker again (`Store.review_status_for_chunk`/
`review_status_for_draft`). The web reader's ✓ gutter button drives this same
verb; there's no un-review verb yet (re-review just overwrites the prior
row — see `Store.record_review`).

## Auto-author toggle — let the reviewer fix, not just flag

```python
edit(kind="draft", id="nanotrans", authoring="on")
edit(kind="draft", id="nanotrans", authoring="off")  # default
```

Per-document flag (`draft.meta.authoring_enabled`, default off). When on,
the `cites`/`structure` review lenses edit the draft inline (mint a
grounded citation, then extend/add a chunk stamped
`authored_by='review:<lens>'`) instead of only filing a change-request
todo, whenever they can ground the fix; `flow`/`adversarial` never author
regardless of the toggle. The web reader's toolbar carries the same
switch.

## Reorder / move (structure, not a new verb)

```python
edit(id="dc16", move={"before": "dc15"})  # reorder among siblings
edit(id="dc17", move={"parent": "dc20", "after": "dc18"})  # move into another section
edit(id="dc19", move={"into": "dc20", "last": True})  # to a section's end
```

Send the *intent* with handles; the system computes the ordering and
records it. No text changes → nothing re-embeds. Moving a heading
carries its whole subtree.

## Soft-delete (retire) — `delete`, reversible

```python
delete(id="dc12")  # retire a chunk (un-delete restores)
delete(id="dc20", mode="promote")  # remove heading, keep contents (lift to parent)
delete(id="dc20", mode="cascade")  # delete heading AND its contents
```

A **heading with children requires a `mode`** — `promote` (keep
contents) or `cascade` (delete the section) — there is no default for
that destructive choice. Retired chunks drop out of the document but
their history (and any anchor to them) survives. You **cannot delete
the last live chunk** — a draft is never empty.

## References in prose — handles route by what they name

Prose is **markdown**. To reference anything, write its `[<handle>]` —
a handle is a ref to *something*, and the system resolves it. You
**always copy the handle from search/get output** — never guess it, no
slug anatomy. Use `[text](<handle>)` when you want display words. What
a handle *does* depends on what it names, and there are exactly two
routes:

| write | route | means | renders / exports |
|---|---|---|---|
| `[pc<id>]` paper chunk, `[pk<id>]` patent, `[fi<id>]` finding | **citation** | this passage supports the claim | `cites` edge + one bibliography entry per paper at export |
| `[dc<id>]` draft chunk, `[me<id>]` memory, any other kind | **link** | provenance / cross-ref | `related-to` backlink; **never** in the bibliography |
| `[the prior result](<handle>)` | (either) | reference with display text | hyperlinked text |
| `[DuckDuckGo](https://…)` | — | web link | hyperlink |

A **citation is to the literature**: the bare paper-chunk handle
written inline. Cite the **exact** chunk that holds the detail —
`[pc234]` — not the whole paper. Several chunks supporting one claim
sit side by side: `[pc232][pc234][pc593]`. The export engine resolves
each handle → its paper and renders `\cite{}` plus one bibliography
entry per paper at compile time. **You never type `\cite{}` or
`\citequote{}`** — those are retired, export-only output.

A **link is to our own notes**: a `[me<id>]` memory, a `[dc<id>]`
chunk in this or another draft. It records provenance — "this para
came from that thought" — as a `related-to` backlink, and is **never a
citation**: it does not produce a `cites` edge and never reaches the
bibliography. *Citations are to the literature, not to our notes.*

**Copy the handle, never guess it.** `[pc234]` is right only because
you pasted `pc234` from a `search(kind='paper', …)` / `get(…)` result.
A made-up handle resolves to nothing — it never autolinks, never
exports, and on a verbatim read is flagged unresolved.

**Citation rigor (be strict).** A citation must **directly and
substantively support the specific claim** — you must be able to read
the sentence(s) in the cited chunk that establish it (open it with
`get(id='pc<id>')` and check). If you can't find a passage that
supports the claim, the cite is **too weak** — either:

- **soften the claim** to match the evidence ("suggests", "is
  consistent with", "reports") rather than asserting it, or
- **find a better source** (prefer the primary source for an empirical
  claim).

Never cite topically-related-but-non-supporting work, and **never cite
a source for a stronger claim than it actually makes** (citation
inflation). Match assertion strength to evidence strength: a single
study → tentative; replicated findings / a review / a meta-analysis →
strong. The reader's cite popover shows the cited chunk verbatim, so a
mismatch between claim and passage is visible — make them agree.

**Chunk-level citation isn't just tidier — it's what a later pass sees.**
A bare paper mention (no chunk) only ever surfaces as its keyword labels
to anything that revisits this passage later — never the sentence
itself. A `[pc<id>]` citation surfaces the real text. Citing the paper
instead of the chunk isn't a smaller version of citing the chunk; it's a
different, much weaker thing.

If you don't already have the right `pc<id>` from a `search(kind='paper',
q=…)` hit, drill for it: `get(kind='paper', id='<slug>~lo..hi',
view='toc')` re-clusters just that chunk range into finer groups —
narrow the range and repeat until a row names the chunk that holds the
claim.

**Already wrote a bunch of raw `[pc<id>]` cites and want a living hub
cite instead?** Enqueue a backfill job — write the intent as a todo and
the dispatch worker mints the job: `put(kind='todo', text='taproot
backfill <slug>', meta={'executor': 'claude_inproc', 'job_type':
'taproot_backfill', 'params': {'scope': <slug-or-dc>}})`. It converts a
section's or the whole draft's `[pc<id>]`/`[pa<id>]` cites to `[fi<id>]`
claim-hub cites on the cluster worker (the LLM cascade never runs in the
MCP); poll `get(kind='job', id='jo<id>')`. See [[precis-taproot-help]].

## Find sources you missed — the gap-finder

`get(kind='draft', id=<scope>, view='backfill')` sweeps the corpus for
relevant-but-**uncited** papers and assembles an eyes workspace around
the candidates — semantic + citation-graph recall, deduped against
everything the draft already cites. `id=` is a `dc<id>` section (full
per-candidate detail) or a draft **slug** (a slimmer aggregate roll-up
merged across every top-level section). The "already cited" exclusion
also folds in the supporting papers behind every cited `[fi<id>]` claim
hub, so a hub's own evidence never resurfaces as a false gap. A topic
precision gate keeps candidates on-domain when the cited papers carry
`topic:` tags (a nanobuds review won't surface nanoribbon/graphene
neighbours) — a no-op when they don't. Full contract:
`the `precis.backfill` package docstring`.

## Cite a paper we don't have yet — request it, don't fake it

The right source for a claim is often **not in the corpus yet**. That is
not a dead end, and it is **not** a reason to silently soften the claim:
soften only when the *evidence* is genuinely weaker, never because the
library merely lacks the paper. Every move below exists to **end with a
real, ingested paper chunk you can quote** — discovery tools find the
source, the corpus is the only thing you cite. Work cheapest /
highest-precision first:

1. **Re-check the corpus.** A semantic + lexical `search(kind='paper',
   q=…)` — we may already hold it under another slug/cite_key. Cheapest
   possible win.
2. **Mine the bibliographies of papers we already hold.** The primary
   source is almost always in the reference list of a review or
   related-work paper that *is* in the corpus. Walk it with Semantic
   Scholar — this hands you a real DOI, no guessing:

   ```python
   get(kind="semanticscholar", id="refs:<held-paper-doi>")  # papers it cites
   get(kind="semanticscholar", id="cites:<held-paper-doi>")  # papers citing it
   ```
3. **Find the canonical source by topic — as a pointer-finder, never the
   citation.** When no held paper points the way:

   ```python
   get(kind="semanticscholar", id="<title or topic>")  # structured hits → DOIs
   get(kind="perplexity-research", q="<question>")  # fills the gap, names the work
   ```

   Use S2 search first (it returns a structured DOI you can act on);
   Perplexity/websearch are the fallback. **Convert the answer into a
   resolvable id, then ingest it** — never cite Perplexity or a web page
   as the source of a scientific claim.
4. **Request it + park the citing work behind the ingest.** Once you have
   a resolvable id:

   ```python
   # a — request the paper (stub-only put; idempotent, DOI/arXiv preferred)
   put(
       kind="paper", doi="10.1038/nature10352"
   )  # → fetch_oa grabs an OA PDF, watcher ingests, embedder indexes
   # (or arxiv='2401.00001' / identifier='s2:<id>'; title-only parks with no auto-fetch)

   # b — park a leaf that waits until that paper is ingested + embedded
   wait = put(
       kind="todo",
       text="[auto] wait for 10.1038/nature10352 ingested+indexed",
       meta={
           "auto_check": {
               "type": "paper_ingested",
               "doi": "10.1038/nature10352",
               "timeout_at": "<ISO-8601, e.g. +7d>",
           }
       },
   )

   # c — block the citing change-request on the wait so it leaves the
   #     doable rotation until the paper lands
   link(kind="todo", id="<your citing todo>", target=f"todo:{wait.id}", rel="blocked-by")
   ```

   The wait is a plain **todo leaf** (not a job): the `auto_check` worker
   polls it ~every minute and flips it to `STATUS:done` once the paper is
   ingested + embedded, re-entering your citing todo. `timeout_at`
   surfaces a stalled fetch for triage instead of waiting forever.
5. **No resolvable id, only a fuzzy claim?** Mint
   `put(kind='finding', text='<claim>', …)` and let `finding_chase`
   resolve it (Unpaywall / arXiv / S2 / EPO), then cite on a re-tick.
   This is the fallback — prefer a stub when you have an id, since it's
   deterministic.
6. **Only now consider softening.** If steps 2–3 turn up no source that
   actually supports the claim, *then* soften to match the evidence (or
   drop it).

In the meantime **do not** invent a paper-chunk handle, write
`paper:slug` for a paper that isn't held, or leave a bare `[citation
pending]` with nothing chasing it — a placeholder nobody is fetching
never becomes a citation. The stub/finding *is* the acquisition; the
`[citation pending]` (if you mark the spot) then has something behind
it. (Until the real `[pc<id>]` lands you can cite the in-flight
`[fi<id>]` finding — it is a citation form, resolved on a re-tick.)
See
`precis-stubs-help` (the acquisition backlog), `precis-auto-tasks-help`
(the wait-on-ingest pattern in full), and `precis-paper-help` (S2 nav +
held-paper citing).

**Abbreviations — write the short form; define it once via a term call.**
Use the abbreviation itself in prose (`TTA`, `PEI`, `FET`). **Do not spell
it out inline** as `Term To Abbrev (TTA)`: the reader shows the definition
on hover wherever the short form appears (including plural forms like
`FETs`), so the expansion in the sentence is redundant clutter. After any
`put`/`edit`, the response **hints any undefined acronyms you just wrote**,
with copy-ready calls. For each, either:

- **define it** — `put(kind='draft', id='<slug>', chunk_kind='term',
  text='Kil Solvent Joule Warbler', meta={'short': 'KSJW'})` (filed
  under an auto-created **Glossary** heading). This term call **is** what
  "define an abbreviation" means here — not an inline parenthetical; or
- **mark it not-an-abbreviation** (a chemical formula, a model name, …)
  — `edit(kind='draft', id='<slug>', not_abbrev=['CO2'])` — to silence
  the hint.

If the term's label should stay the long form (`short='stereolithography'`)
but its acronym should also hover-resolve on its own, add both: `meta={'short':
'stereolithography', 'abbrev': 'STL'}`. `abbrev` is a distinct resolvable
surface from `short`, not a replacement for it.

Once defined or silenced, a token stops being hinted. Reference a term with
`[PEI](<dc-term-handle>)`; explicit
terms win over auto-detected ones. **Notes** (memory / think / other
drafts) are referenceable but **not citeable** — they get a
`[<handle>]` `related-to` link only, never a bibliography entry. (A
`finding` is the one exception: `[fi<id>]` is a citation form that exports
directly to `\cite{}` — the real `cite_key` once established, else a stub
off its `pub_id`. Swapping it for a direct paper cite is an optional
editorial pass on a later tick, not something the system rewrites for you.)
Math is `$…$` / `$$…$$` (LaTeX, rendered by KaTeX on the web).

**Don't write `[finding #<name>]`.** A finding is addressed by its
`[fi<id>]` handle, **not** by a made-up `#slug`. A `[finding
#amine-uptake]` / `[citation pending — finding #…]` marker resolves to
**nothing** — it never autolinks, never exports, and on a verbatim read
is flagged as an **⚠ unresolved finding reference**. If you mean to cite
a finding, reference its real `[fi<id>]` handle; if it doesn't exist
yet, `put(kind='finding', …)` it first (`[fi<id>]` is a citation form
that exports directly to `\cite{}`; swapping it for a direct paper cite
later is an optional editorial choice, not an automatic rewrite). Don't
leave dangling `#name` placeholders in the prose.

**Formatting.** Prose is plain text with a small markup subset:
`` `code` `` renders inline code, `$…$` / `$$…$$` is math (KaTeX), and
`<sub>`/`<sup>` render for chemistry and units (`NH<sub>2</sub>`,
`g<sup>-1</sup>`). **Do not use emphasis markup.** `_italic_` and a single
`*word*` are not rendered at all and leave literal `_`/`*` in the text;
`**bold**` does render but reads as shouting, so skip it. Inline
citations and cross-refs render as a compact marker in the reader,
so handles do not clutter the sentence: write `[pc234]` and it shows
as a small superscript. A chunk cross-ref must use the target chunk's
**`dc<id>` handle** (e.g. `[dc41]`), shown in the outline — never a
numeric id like `[45650]`, which resolves to nothing.

**Every** reference you write materialises a graph edge, by route: a
`[pc<id>]`/`[pk<id>]`/`[fi<id>]` **citation** materialises a `cites`
edge to the paper (so the draft surfaces in that paper's bibliography
— see `precis-bibliography-help`), while a `[me<id>]` or cross-draft
`[dc<id>]` **link** materialises a `related-to` backlink so the draft
is discoverable from the note's side. Remove a reference and its edge
drops on the next edit. Intra-draft `[dc<id>]` cross-refs are
document-internal (TOC / `\ref`), not graph edges.

## Draft hygiene — undefined abbreviations, stray footers, whole-paper vs chunk citations

Two things the runtime flags for you before export: an abbreviation
used but never defined (see **Abbreviations**, above — define it via
a `term` chunk or silence it with `not_abbrev`) and a citation that
resolves to nothing (see **References in prose**, above — cite the
exact `[pc<id>]` chunk, never the whole paper). Neither needs a
hand-maintained bibliography **footer**; citation handles resolve to
one entry per paper at export. Skim the **outline**
(`get(kind='draft', id=…)`) first — it's the cheapest place to catch
both before they reach a compile; its hygiene footer truncates each
list to 8 entries. For the full, un-elided lists (clearing a long
backlog of undefined abbreviations one alphabetical batch at a time
otherwise costs several paginated outline round-trips), use
`get(kind='draft', id=…, view='hygiene')` — same two checks, no
outline body, no truncation.

## Writing well — structure + common mistakes

A research write-up is *flowing prose*, not a slide deck. When you write
or revise a block:

**Structure**

- **One paragraph, one idea — topic sentence first.** Lead with the
  claim; the rest of the paragraph develops it. Don't bury the point or
  fuse two ideas into one paragraph.
- **Claim → evidence → citation, in that order.** Each claim earns its
  evidence, then its `[pc<id>]` cite. Don't stack unsupported assertions.
- **Given → new flow.** Open a sentence with familiar information, end
  with the new. Open each section with a sentence that says what it
  covers (signpost).

**Diction**

- **Consistent terminology** — one term per concept. No elegant
  variation on key terms (a synonym reads as a *different* thing).
- **Quantify** — a number + unit beats "significant / several / many".
- **Concise, active** — cut "it is important to note that", "in order
  to" → "to", "due to the fact that" → "because"; prefer active voice.
- **Tense** — past for what was done/found, present for established
  facts.

**Avoid (LLM tells)**

- Slide-deck/listy prose and over-bolding instead of paragraphs.
- Filler openings ("In recent years, X has attracted significant
  attention…").
- Mismatched calibration — over-hedging in one place, over-claiming
  ("proves", "clearly", "novel", "first") in another.
- Restating the brief, or repeating a point across blocks.

## Steer the draft — brief + change requests (don't hand-edit prose)

You usually don't rewrite prose directly; you **steer**:

```python
edit(id='nanotrans', meta={'workspace': {'brief': '…updated brief…'}})
put(kind='todo', parent_id='<project>', text='tighten this paragraph',
    meta={'anchor': 'dc12'}, ...)        # a change request, anchored
link(src='dc12', rel='derived-from', dst='memory:7x2')  # provenance
```

A change-request `todo` anchored to a handle flows through the normal
todo tree → dispatch → jobs; the executor decides whether to do it in
one job or fan out per section.

**If you (the executor) can't complete a request, ask clearly.** When
you yield an `ask-user:`, write a real question a human can act on, and
**reference chunks by their `dc<id>`** — never a numeric "chunk 0"
(drafts have no numeric chunk addresses; the reader can't find it). Bad:
`ask-user:see-chunk-0`. Good: `ask-user: '"remove this para" is anchored
at dc5 (the intro); did you mean dc5 or the sibling dc12?'`.
The ask surfaces on the draft block as a 🔔, linking to your run.

## Export (LaTeX) — `precis draft export`

A draft renders to a **compilable LaTeX project** with one command:

```
precis draft export <slug> [--out DIR]   # → main.tex + refs.bib + preamble.tex
precis draft export <slug> --pdf          # …and run latexmk to produce main.pdf
latexmk -pdf main.tex                     # biber + makeglossaries run for you
```

In the web reader, the **PDF** link (header) compiles on demand and
serves the result, cached by the draft's version token — so it only
recompiles after an edit. Hosts without a TeX toolchain get a friendly
"latexmk not installed" message instead of a build.

The export is a one-way resolution pass; the output is **disposable**
(re-export from the draft, never hand-edit the `.tex`). Everything
resolves automatically: each block gets a `\label{chunk:<handle>}` and a
`[dc<id>]` cross-ref becomes `\cref{chunk:h}`; each `[pc<id>]` paper-chunk
citation is resolved to its paper and becomes `\cite{}`, with a
`refs.bib` carrying **one entry per cited paper** (DOI/arXiv included
when known); every defined abbreviation becomes a `\newacronym` and each
occurrence a `\gls{…}` (first use full, later uses short), with the
page-number "where it occurs" list in the glossary. `[me<id>]` /
cross-draft `[dc<id>]` **links** render to nothing (provenance only —
never in the bibliography). The **byline** you set with `edit(authors=…)`
becomes an `authblk` `\author`/`\affil` block under `\maketitle` (ROR
hyperlinked); no authors → the legacy single-name default. You never
write `\cite{}` (or the byline) yourself; it is the exporter's output. This is why **citing the exact chunk** and
**defining your abbreviations** pays off — the exporter does the rest.

## Export — PDF (job) and Word/.docx

A draft renders to a real document. Two paths:

- **PDF** — `export_draft` → LaTeX → `latexmk`. This is **deterministic
  but slow**, so it runs as a **job**. Start one and watch its logs on the
  project's task page:

  ```python
  put(kind='job', job_type='draft_export', parent_id=<project-todo-id>,
      params={'draft': '<slug>'})
  ```

  The job streams `job_event` progress and lands the PDF path in its
  `job_summary` / `meta.pdf`. (Web: the **export PDF** button on the draft
  reader does exactly this.)
- **Word/.docx** — toolchain-free (python-docx), so it's **synchronous** —
  the web reader's **export .docx** link downloads it immediately. Citations
  resolve through the same paper lookup the PDF uses (identical references),
  with render-time acronym first-use expansion + an auto acronyms list.

Both are **disposable** — re-export from the draft; never hand-edit the
output. Citations must resolve (`[pc<id>]` → a chunk of a paper in the
corpus) or the export marks a stub + warns.

## Send to reMarkable (send-to-tablet)

A draft can be pushed to a reMarkable 2 for offline reading + pen annotation:

```
precis draft remarkable <slug> [--folder /Precis] [--dry-run]
```

Web: the **→ reMarkable** button on the draft reader (shown only when a device
credential is configured). It runs the `remarkable_send` job — same shape as
`draft_export` — which renders a **reMarkable-mode** PDF and uploads it.

reMarkable mode (`export_draft(remarkable=True)`) differs from the normal PDF in
two ways, so the document is **self-contained on the tablet**: (1) the page
geometry matches the RM2 screen (157.6×209.6 mm, wide outer margin for the pen);
(2) every source citation (`[pc<id>]` / `[§slug~n]` / patent) renders as a
numbered **`\footnote`** — the human cite + its bibliography number + the
referenced chunk excerpt — instead of a bare `\cite`, so you read the source
inline with no round-trip to the reference list. A numbered bibliography still
renders at the end (the footnote's `[N]` matches it).

The device credential lives in the **secrets vault** (`REMARKABLE_RMAPI_CONFIG`,
ADR 0055), never in `app_settings`; the upload runs in the `precis-remarkable`
container (`docker/remarkable`) via the `ddvk/rmapi` CLI. The destination folder
is the `remarkable.target_folder` app_setting (default `/Precis`).

## Freeze / snapshot (release + backup)

A *freeze* copies the draft's current chunks into an immutable
`paper`-like ref (versioned, searchable, citable), linked `snapshot-of`
the draft. The draft keeps evolving. (Operational verb TBD.)

## See also

```python
get(kind="skill", id="precis-citation-help")  # citation kind + verifier workflow
get(kind="skill", id="precis-paper-help")  # read, cite, search held papers
get(
    kind="skill", id="precis-stubs-help"
)  # request a paper we don't have (acquisition backlog)
get(kind="skill", id="precis-finding-help")  # flag a claim / chase an un-ingested DOI
get(
    kind="skill", id="precis-fisheye-help"
)  # view='fisheye'/'fisheye+1hop' — a chunk + its neighborhood/reference ring
get(
    kind="skill", id="precis-auto-tasks-help"
)  # wait-on-ingest (paper_ingested) leaf pattern
get(
    kind="skill", id="precis-taproot-help"
)  # cite a claim hub (living [fi<id>]); mint one; backfill [pc<id>] cites to it
```
