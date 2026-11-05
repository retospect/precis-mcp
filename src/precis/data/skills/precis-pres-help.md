---
id: precis-pres-help
family: drafting
title: precis — store and search slide decks + unpublished writeups
summary: internal artefacts — slide decks, unpublished writeups, course notes; subtype taxonomy
answers:
  - how do I ingest a slide deck one slide at a time?
  - how do I capture an unpublished writeup or course notes?
  - how do I find a specific slide or paragraph in a stored deck?
  - how do I delete or restore a presentation?
  - why are the slides empty and can I re-extract a stored deck?
applies-to: get/search/put/edit/delete/tag/link (kind='pres')
tags: addressing, drafting
kinds: pres
status: active
---

# precis-pres-help — slide decks, drafts, course notes

`pres` is the kind for internal work the user wants searchable but
kept separate from the academic paper library (`paper`). Use it for:

- **Slide decks** (`subtype:slides`) — PDFs of talks, lectures,
  conference presentations.
- **Unpublished writeups** (`subtype:writeup`) — drafts, internal
  reports, working documents.
- **Course / talk notes** (`subtype:notes`) — note-form artefacts
  associated with a talk or class.

One ref per artefact. One block per slide (for decks) or per
paragraph (for writeups). Slug-addressed: pick a stable slug
(date + topic works well, e.g. `2026-06-talk-precis-architecture`).

## Add a new deck or writeup

## Convert a deck already stored as a paper

```python
edit(kind="paper", id="pa123", args={"convert_to": "pres", "reason": "slide deck"})
```

Add `dry_run=True` to preview. Requires the paper's matching PDF in local
storage. One transaction creates a new `pr` record with one slide per PDF
page (including blank pages), transfers identifiers and compatible ref links,
and retires the paper. The receipt names both handles; `get(kind='paper', id='pa123')`
follows the existing redirect to the presentation. The PDF and original
cite key are kept; a stable cite key is created if absent. New embeddings
are filled by the worker.

Open/shared ref tags carry over; paper-only axes, their CASCADE markers and
`DREAM` are omitted, and `subtype:slides` is added. Any link or evidence pin
anchored to a paper chunk refuses, naming the link and chunk handles;
remove or move those links before converting. Chunk page estimates cannot
identify PDF slides, so anchors are never re-anchored. SI records/parents
and incompatible relations, including evidence edges `establishes` and
`corroborates`, also refuse: unlink the named edges first.
Original chunks and chunk tags remain on the retired paper; other stored
`pc` references do not redirect. Only the `slides` subtype is supported by
this conversion.

## Ingest a slide deck one slide at a time
## Capture an unpublished writeup

```python
put(
    kind="pres",
    id="2026-06-talk-precis-architecture",
    text="Slide 1 body: title, author, date.",
    pos=0,
    title="Precis architecture (talk @ Cluster Demo Day)",
    subtype="slides",
    ref_meta={
        "venue": "cluster demo day",
        "date": "2026-06-04",
        "audience": "internal",
        "source_pdf": "/path/to/talk.pdf",
    },
)

# Subsequent slides — title and subtype are ignored on update.
put(
    kind="pres",
    id="2026-06-talk-precis-architecture",
    text="Slide 2 body: what is precis?",
    pos=1,
)
put(
    kind="pres",
    id="2026-06-talk-precis-architecture",
    text="Slide 3 body: 22 kinds…",
    pos=2,
)
```

Omit `pos=` to append at the next free position. Re-putting at an
existing `pos` overwrites that block (useful for fixing OCR noise
on a single slide without rebuilding the whole deck).

For writeups, pass `chunk_kind='paragraph'` so the renderer
labels blocks as paragraphs instead of slides:

```python
put(
    kind="pres",
    id="2026-06-draft-cluster-postmortem",
    text="The cluster went down at 03:14 on 2026-06-02 because…",
    pos=0,
    title="Cluster postmortem — 2026-06-02 outage",
    subtype="writeup",
    chunk_kind="paragraph",
    ref_meta={"authors": [{"family": "Stamm"}], "date": "2026-06-08"},
)
```

## Read a deck

## Get the slide deck overview
## Show me all the slides

```python
get(kind="pres", id="2026-06-talk-precis-architecture")  # overview
get(kind="pres", id="2026-06-talk-precis-architecture/full")  # all blocks
get(
    kind="pres", id="2026-06-talk-precis-architecture~2"
)  # single block (output shows handle ps<id>; get(id='ps<id>') works too)
get(kind="pres", id="pr123~2..10")  # inclusive range of stored block positions
```

Positions are zero-based. A range ending past the last block returns the
available blocks; a range wholly outside the deck reports its valid bounds.
`/full` and range reads stop at a slide boundary when the byte budget binds.
Follow the footer's exact `next: get(kind='pres', id='pr123~N..M')` call
until no continuation remains: each slide appears once, including tail
references. A slide too large to fit whole refuses with a budget remedy.
LaTeXiT payloads are removed during PDF extraction and when reading old decks;
surrounding slide text is retained.

## Check or download the stored PDF

The overview includes `pdf: present` with the stored page count and byte
size, or `pdf: missing`, and a `/pres/<id>` web-reader continuation.

```python
get(kind="pres", id="pr5", view="pdf_link")
```

`pdf_link` returns a signed download URL, UTC expiry, SHA256, size, page
count and curl command. It uses the same short-lived link as papers and
datasheets; a deck without an attached PDF refuses with ingest guidance.

## Empty slides and re-extraction

PDF ingest uses fitz text extraction, with no OCR or model calls.
`meta.image_only_pages` counts pages with fewer than 10 non-whitespace
characters after cleaning. Blank pages, slide numbers and short headings
can also trigger this heuristic. The overview shows the count and next step;
affected decks receive the allowed open tag `ocr:needed`.

```python
edit(kind="pres", id="pr123", args={"reextract": True})
```

This queues a `pres_reextract` job on the deck, deduplicating active requests.
The receipt gives the job handle to watch. A deck without a stored source PDF
refuses with ingest/restore guidance. A file held elsewhere waits for a worker
with the bytes; exhausted placement retries fail with a restore instruction.

The worker re-reads the stored PDF with **fitz only**. A truly image-only deck
stays textless: this refreshes its count, it does not recover image text.
Inspect the PDF and arrange OCR separately to recover that text; this door
neither performs OCR nor attaches a replacement source. Keep metadata edits
in a separate call.

Successful extraction atomically refreshes the count and `ocr:needed` tag
(removing it when no pages are low-text). Unchanged slides retain their handles;
changed text replaces the slide body, removing old chunk links. Extraction
failure leaves the previous body intact. The successful outcome is recorded
in `meta.reextract`, and job failures name the next step.

## Browse what's been stored

```python
get(kind="pres")  # 20 most recent
get(kind="pres", id="/recent")  # explicit
```

## Find a slide or paragraph

```python
search(kind="pres", q="hnsw index on chunk_embeddings")
search(kind="pres", q="vector", scope="2026-06-talk-precis-architecture")
```

Lexical search across all `pres` blocks. Cross-kind search
(`search(kind='*', q='...')`) folds `pres` hits in alongside paper /
memory matches when a question spans both.

## Link a deck back to the papers it cites
## Cross-reference a writeup with an internal decision

```python
link(
    kind="pres",
    id="2026-06-talk-precis-architecture",
    target="pa5",
    rel="cites",
)

link(
    kind="pres",
    id="2026-06-draft-cluster-postmortem",
    target="gripe:3712",
    rel="derived-from",
)
```

Open-tag axes only; no closed workflow vocabulary on `pres`. To
mark a draft's status, use a paired `todo` or `gripe`.

## Tags

The overview shows stored tags; `get(kind='pres', id='pr5', view='tags')`
reads just the tag list. In the web reader's **Meta** panel, use the quick
tags, **+ tag** box, or **×** beside an open tag. Closed-vocabulary chips
are inert, as on the paper reader.

`tag` and metadata `edit` accept `id=[...]` (slugs, `pr` handles or numeric
ref IDs). They apply the same arguments to each deck in one transaction,
return per-deck results, and roll everything back if any deck fails.

```python
tag(
    kind="pres",
    id="2026-06-talk-precis-architecture",
    add=["topic:agent-architecture", "audience:internal"],
)
```

## Delete or restore a presentation

```python
delete(kind="pres", id="2026-06-talk-precis-architecture")
delete(kind="pres", id="pr5")  # a deck handle also works
```

Delete soft-deletes the whole deck and journals its retirement. It hides
from listings and search; slide chunks and the PDF are retained. Block
selectors (`~N`) and path views (`/full`) are refused for deletion.

In the reader's Meta panel, **Delete presentation** asks for confirmation.
Drive's row delete uses the same operation. To restore a deck, open
`/refs/pres/<numeric-id>` and click **Undelete** on its tombstone page.

## See also

- [[precis-overview]] — verbs and kinds
- [[precis-search-help]] — search mechanics
- [[precis-paper-help]] — academic papers (cite_key shape)
- [[precis-relations]] — cites, derived-from, supports
