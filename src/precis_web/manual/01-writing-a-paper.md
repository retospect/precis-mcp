# Writing a paper

A draft in precis is *steered*, not typed. You describe what you want
written, the writer works on it between your visits, and you spend your
time reviewing, correcting, and pointing — not composing paragraphs.

## Start one

From **Drive** → the **New** form → pick kind **draft**. Three fields
matter:

- **Title** — renameable later, so don't agonise.
- **Description** — *required*, and it is not a summary. This text
  becomes the writer's **initial prompt**: it is what the model reads
  first and returns to on every pass. "A review of solid-state
  electrolyte interphase formation, focused on the last five years of
  operando work" gets you a document; "SEI review" gets you drift.
- **Genre** — sets the register and the section scaffold. Available:
  research paper, patent application, proposal (answers a call),
  technical report, review / survey, system / manufacturing spec,
  book / monograph, summary / brief, general article.

Optional: **seeds** (free text — things to go read first), **tags**
(become `topic:` axis tags on the project), and for the *proposal*
genre a **call for proposals** to attach, which makes the writer mirror
that call's required sections and word limits instead of a generic
template.

Creating a draft also mints the project behind it — a draft is 1:1 with
a project. The writer starts on your description at the next dispatch
pass; you do not have to kick anything off.

## Read and steer it

The reader is **`/smartdraft/<id>`** (every `/drafts/<id>` and
`/draft/<id>` link redirects here). Three panes: a fisheye table of
contents on the left, the focused block and its neighbourhood in the
middle, and the collaboration pane on the right.

Two view modes: **full document** (📄, the default) renders a window
around your focus and loads the rest as you scroll; **fisheye**
collapses the quiet stretches so a long document fits on one screen.

The steering affordance is the gutter's **📌 pin** (key `p`): it marks a
paragraph as context for the writer's next pass. Pinning also sets the edit
target — a change request anchors on the first paragraph you've pinned,
falling back to your current focus when nothing is pinned. The **ask box**
at the bottom of the right pane sends a free-text request grounded in
everything pinned; "unpin all" clears the set. This is the main way to get
work done: pin, describe, send.

You can also edit directly — text, tables, splitting a block, merging
it into the previous one, deleting it — when it's faster to just fix
the sentence than to explain the fix.

## Citations

Cites are live objects, not text. Search the corpus from inside the
draft to insert one, and run **validate refs** to catch a cite that no
longer resolves. `§` and `¶` anchors are clickable and hover to a
preview of the exact source passage — yours and everyone else's.

## Sign off

The **✓ checkbox** in the gutter records *you* as having reviewed that
block. It is a real ledger entry, not a highlight, and it is what turns
a machine-written document into one a human stands behind. Un-ticking
retracts it.

Separately, the per-heading **review ▾** menu files review *tasks* for
a section — that queues work, it does not sign anything off.

## Export

- **`export.docx`** and **export PDF** — the formatted document.
- **`papers.zip`** — the cited sources as PDFs, with a manifest. Only the
  ones this machine actually holds go in the zip; anything it can't find
  is named in the manifest instead, so an incomplete bundle tells you
  what is missing rather than quietly omitting it.

Both exports run a **retraction gate** first: if the draft cites a paper
that has been retracted, the export is blocked. The status shown is read
from stored state and costs nothing; the **re-check** button goes out and
asks the source live. You can override the block deliberately, which is
the right call for a paper that *discusses* a retraction — but it has to
be deliberate.

If the draft cites claims you have signed and published as nanopubs
(→ *Publishing claims*), the export grows a **Published claim
artifacts** appendix automatically — frozen sentence, trusty URI,
status. Cite an unminted claim and the export is byte-identical to
before; nothing is added speculatively.

## Housekeeping

Fork a draft to try a different shape, rename it (the title in search
results and the title heading converge in one step, so they cannot
drift), edit the author list, or delete it — deletion makes you type the
draft's name, and is recoverable.
