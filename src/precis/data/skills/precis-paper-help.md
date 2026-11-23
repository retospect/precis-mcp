---
id: precis-paper-help
family: paper
title: precis — find, read, cite papers
summary: scientific paper corpus — find, read, address by handle (pa/pc), views and chunk selectors
answers:
  - how do I open a paper by handle or DOI?
  - how do I find a paper by topic when I don't know any slugs?
  - how do I look a paper up by its exact title or author instead of a keyword search?
  - how do I find where a paper mentions a specific exact term?
  - how do I filter papers by publication year?
  - how many papers match, and is ingestion landing full text?
  - which of these papers are full text, card-only or stubs?
  - how do I search inside one paper for a passage?
  - how do I get a paper whose DOI isn't indexed yet?
  - how do I resolve a journal/volume/page/year citation to a paper?
applies-to: get/search/tag/link (kind='paper')
status: active
tags: [addressing, workflow]
kinds: [paper]
---

# precis-paper-help — find, read, cite papers

Papers are research articles in the store. The canonical address is
the **record handle** `pa<id>` (e.g. `pa40`); individual blocks are
chunk handles `pc<id>` (e.g. `pc512`).

## Look up a paper when I have a handle
## Open a paper by handle (or DOI)
## I have a DOI — how do I read the paper?

```python
get(kind="paper", id="pa40")  # full overview, by handle
get(kind="paper", id="pa40", view="toc")  # TOC — the whole-paper map
get(kind="paper", id="pa40", q="NOx")  # passages inside the paper (find-in-ref)
get(kind="paper", id="10.1038/nature10352")  # bare DOI resolves via metadata
get(kind="paper", id="10.1038/nature10352", view="abstract")  # DOI + view = kwarg only
get(kind="paper", id="10.1038/nature10352", view="toc")
```

The handle is the thing to keep. The DOI is a *lookup key* for papers
already held — once the paper is held, `pa<id>` is its canonical address.
DOI suffixes can contain `/`, so DOI + view needs the `view=` kwarg — the
`id/view` path form is ambiguous.

## Check whether the library holds a DOI without queuing ingest

`get(kind="paper", id="<DOI or handle>", view="lookup")` checks stored
holdings without queuing ingest or marking worker processing. It reports a
held handle (possibly a stub) or not held. Pass `id=[...]` to check a batch.

## The DOI isn't indexed yet — how do I get the paper?

A `get` is a read: on a well-formed unheld DOI it answers not held and
names the acquisition call (`put(kind="paper", doi=…)`); it queues nothing
by default. To acquire, use that `put` — it mints the stub and pins it to
the open-access fetch pass; no fetch runs inside the call. Check first
whether a published or preprint version of the same work is already held
(`search(kind="paper", q="<title>")`) so you do not ingest a duplicate.

To have the `get` itself queue a `paper_ingest` job, opt in with
`args={"queue": "bounded"}`: the answer is `not ingested yet; queued
ingest job jo<N> for <doi>; retry get in a few minutes`. Repeating the
same opted-in `get` reuses the job (any DOI spelling), so polling is safe;
a job that already failed inside the 24h window reports its failure
instead of requeueing. The worker verifies the DOI against Semantic
Scholar / the DOI registries before minting.

An explicit `view=` never queues, even with the opt-in (`meta`, `text`,
`body`, `full` included): an unheld DOI answers not found.

The opt-in queue is bounded — an ingest job can reach the paid OpenAlex
fetch leg: at most 5 mints per batch `get`, a fleet-wide 50 per rolling
24h (`PRECIS_GET_DOI_QUEUE_DAILY`), and no queue for a DOI whose paper was
deleted or on a read-only serve. Past the daily bound the `get` answers
`not ingested; daily get-queue cap reached` — resubmit with
`args={"queue": "explicit"}` (the unbounded-explicit lane, like `put`) or
use `put(kind="paper", doi=…)`. The flag lifts the daily cap only. A
batch over the per-call mint bound asks for
`args={"queue": "bounded", "confirm_bulk": True}` up front (echo the
`queue` value you used). With `q=`, the call can queue up to five jobs
before reporting capped IDs; retry those in batches of at most five.

```python
get(kind="paper", id="10.1126/sciadv.aeh2779", args={"queue": "bounded"})  # → queued ingest job jo<N>
get(kind="job", id=N)  # the ingest job's status
get(kind="paper", id="10.1126/sciadv.aeh2779")  # works once it is held
```

`put(kind="paper", doi=…)` is the same stub + fetch queue from the write
side — use it when you want the paper ingested, not just looked up.

## I only have a citation string (journal, volume, page, year)

```python
put(kind="paper", citation="Phys. Rev. B 47, 558 (1993)")
```

The string goes to Crossref's bibliographic query. Exactly one record
matching every part of it (year, volume, first page, journal) is stubbed
by its DOI. Anything weaker returns candidates with a `conf=` score and
mints nothing; pick one and `put(kind="paper", doi="<its DOI>")`.
Starting from something thinner — a conference title, byline or abstract
fragment — the identification ladder lives in
[[precis-identify-publication-help]].

## How do I get a paper's handle?

You don't construct it — you read it off `search`/`get` output (every
row leads with `pa<id>` for the paper and `pc<id>` for a block) and
paste it back into the next call. Don't reconstruct a slug from an
author + year + title; search for the paper and copy the handle:

```python
search(kind="paper", q="<author or topic>")  # → rows carry pa<id>/pc<id>
get(kind="paper", id="<DOI>")  # DOI of a held paper → its overview; read the handle
```

Holding several ids, pass them at once:
`get(kind='paper', id=['pa1', 'pa2', 'slug-or-doi'])` returns one compact
block per id (handle, title, byline/venue, block count; ≤50, a missing id
is a `not found` line, not a failed call). `view=` maps the single-id view
over the batch (`view='abstract'` for each), `view='status'` gives the
`search(view='status', ids=[…])` table, and `pc<id>` elements render their
chunks — a list of chunk handles reads several blocks in one call.

## Find a paper by topic
## Discover papers about a subject I'm researching
## I don't know any slugs — find papers by keyword

```python
search(kind="paper", q="photocatalytic NOx reduction")
search(
    kind="paper", q="photocatalytic NOx reduction", page_size=20
)  # default 25 papers, max 100
```

When the result set exceeds one page, narrow by keyword navigation:
`search(kind='paper', q='photocatalytic NOx reduction', view='toc')`.
Follow the keyword-filter calls to smaller sets. Or re-issue more specifically
(add a distinctive term, `scope=`, `exclude`) or less specifically (drop a
term, widen `queries=`).

Each row is one paper — the best hit's chunk handle `pc<chunk_id>`
(paste back into `get`/`link`), a `+N` count of that paper's other
hits, the paper's `pa<id>` + cite key, year and title, and the best
chunk's keywords; order is the relevance signal.
`unique_per='chunk'` (implied by `scope=`) returns one row per matching
chunk instead. A question about one known paper takes `scope=<paper handle>`,
which searches only that paper's chunks. Mechanics (mode=, bounded results, tags=): `precis-search-help`.

When one phrasing isn't finding it, escalate (details in
`precis-search-help` → Broad retrieval):

```python
# Broad: fuse rephrasings + hypothetical-answer passages in one call
search(kind="paper", q="…", args={"queries": ["…", "…"], "answers": ["…"]})
# Deep: async triage campaign — returns a job handle, poll for the curated result
search(kind="paper", q="…", args={"good": True})  # → poll get(kind='job', id=…)
```

The default free-text `q=` search matches paper **bodies** and folds
in the embedded metadata card, so a topic query works and a title query
*often* surfaces the paper. But `q=` is body-first: for a known title or
author, use the targeted byline lookup — `precis-paper-help~6` — it's
more reliable.

## Look a paper up by its title
## Find a paper by author
## I know the title (or an author) — get the record, not body hits

```python
search(kind="paper", args={"title": "attention is all you need"})  # → the paper record
search(kind="paper", args={"author": "Vaswani"})  # → papers with that author
```

`title=` / `author=` return paper **records** — one row per paper, each
leading with the `pa<id>` handle plus a one-line citation — not body-block
hits. They match the structured `refs.title` (trigram + full-text) and
`refs.authors` byline directly, held copies first, so an exact title or a
bare surname lands the paper *itself* rather than other papers that merely
mention those words. Each result row hands you a `get(id='pa<id>',
view='bibtex')` cite path and a `view='toc'` read path. Use these instead
of `q=` whenever you already know the byline; fall back to `q=` for topic
search. (Pass one of `title=`/`author=`, not both.)

Why this exists: a plain `q='attention is all you need'` gets stripped by
full-text search to its content words (`'attent' & 'need'`), so the exact
paper's short card can lose on rank to content-dense bodies elsewhere; a
bare author `q='Vaswani'` tends to surface *other* papers' reference-list
lines. The byline lookup sidesteps both.

Ref-level filters compose here the same way they do on the listings —
`unread`, `ingested_after`/`ingested_before`, `id_min`/`id_max`,
`min_quartile`, `tags=`, `serves`/`stamp`/`linked_from`/`lacks`,
`has_fulltext`, `record_type`, `ids`/`exclude`/`after`/`before`/
`since`/`until` and a dispatch-resolved `cited=`/`uncited=`/`hubbed=`
scope all narrow the records returned:

```python
search(kind="paper", args={"title": "catalyst screening", "unread": True})
```

Args the record listing can't express — `q=`, `scope=`, `mode=`,
`unique_per`, `good`, `chunk_kind`, `group_by`, `days`, ranking knobs —
refuse instead of dropping silently. A result longer than `page_size`
offers a widened `page_size` continuation that re-issues every active
filter; a dispatch scope can't respell, so the hint says to re-add it.

## See a paper's full byline with identity links
## Fix a wrong or missing author

```python
get(kind="paper", id="pa40", view="authors")  # position, source, links, verified tick
```

`view='authors'` renders one row per byline position — identity links,
the tier that wrote it, a `✓ verified` mark. Fixing a byline
(`authors=[…]` grammar, tier ladder, the verified sign-off it stamps):
[[precis-paper-ref-help]].

## Find a paper that mentions an exact term
## Grep papers for a unique token (compound, DOI, exact string)
## Where does any paper mention this specific string?

```python
search(kind="paper", q="LiBF4", mode="lexical")  # exact token, FTS only — no embedding
search(kind="paper", q="LiBF4")  # hybrid also floats rare tokens high
search(
    kind="paper", q="10.1038/nature10352"
)  # finds papers citing this DOI in body text
```

`mode='lexical'` pins the search to the Postgres full-text leg (no
embedding) — reach for it on an exact identifier, acronym, code token, or
numeric like `1.523 eV`, where embeddings blur the match. Note it's the
`english` FTS config, so it stems and drops stop-words — precise on rare
tokens, **not** a verbatim substring grep. Default hybrid also ranks rare
tokens high, so either works for a unique string. Searching a DOI this way
finds *citing* papers; use `get(kind='paper', id='<DOI>')` to fetch the
paper itself. A `q=` that *is* a bare DOI short-circuits to the held
paper's record — honouring the call's ref-level filters, so on a cohort
search (`unread`, an id bound, a `cited=`/`uncited=` scope) a filtered
paper answers "already in the local corpus … excluded by the filters"
rather than returning it or claiming it absent. A bad-import stub that
stays unfindable repairs in place (`title=`, `authors=`, `doi=`, `year=`,
`journal=`): [[precis-paper-ref-help]].

## Fix a paper record — duplicates, phantoms, stamps, attach a PDF
## `already belongs to ref id=N` — an identifier collision
## Two live refs are the same paper — how do I merge them?
## A worker stamped a paper done/failed — how do I re-run it?

See [[precis-paper-ref-help]] — identifier repair, `merge_into`,
`redirect_to`, `retire`, the body doors (`drop_body`/`move_body_to`/
`split_body`/`clear_abstract`), `clear_stamp`, and `put` with
`args={'pdf_url': …}` to attach a legal open copy to a body-less stub or
replace a short held preview with the full paper.
No fetchable copy? A person can also hand-drop the PDF into the papers
inbox — named `pa<id>.pdf` it folds into a body-less stub
([[precis-paper-ref-help]] covers the drop mechanics).

## Convert a misfiled slide deck to a presentation

```python
edit(kind="paper", id="pa123", args={"convert_to": "pres", "reason": "slide deck"})
```

`dry_run=True` previews without writing. The held PDF must be available
locally and match its stored hash; missing bytes refuse with a repair step.
The conversion creates a new `pr` handle with one slide block per PDF page,
keeps the PDF and cite key (creates one if absent), transfers identifiers and
compatible ref links, and retires the paper in one transaction. The receipt
names both handles; the retired `pa`
handle redirects to the presentation. Original chunks stay on the tombstone;
the worker fills embeddings for the fresh slides.

As with `convert_to='datasheet'`, eligible ref tags carry and paper-only
axes/CASCADE markers plus `DREAM` are omitted; `subtype:slides` is added.
SI records/parents and incompatible relations, including evidence edges
`establishes` and `corroborates`, refuse: unlink the named edges first.
Any link or evidence pin anchored to a paper chunk refuses and names the
link and chunk handles; remove or move those links before converting.
Chunk page estimates cannot identify PDF slides, so anchors are never
re-anchored. Other stored `pc` references do not redirect.
See [[precis-pres-help]]. `datasheet` conversion remains an in-place
kind change that preserves the existing chunks.

## Restore a paper that was merged into another ref (operator only)

An operator can make the retired loser independently addressable again:

```
precis garden restore pa509714 --reason 'wrong merge' --dry-run
precis garden restore pa509714 --reason 'wrong merge'
```

Use the loser's `pa` handle or numeric ref id (its retained cite-key slug
also works). This operator CLI command bypasses the supersede redirect and
sets human/operator trust inside the transaction. There is no MCP or web
restore-superseded door; no `args` key grants operator access.

The transaction clears `retired_at`, `meta.superseded_by` and `meta.dedup`,
removes incoming `supersedes` edges, and records the restoration. A live ref is
an explicit no-op. Garden-merged refs are refused: use the returned
`precis garden undo <merge-id>` command to restore their original links.
A plain retired ref instead uses
`edit(kind='paper', id='pa509714', args={'retire': False, 'reason': 'undo retirement'})`.

This is a limited un-merge: identifiers, body/PDF, bibliography references and
ordinary links stay where the merge put them. Audit history stays intact.
It does not reject a source, refresh from DOI, or move identifiers back.

`edit`, `link`, `put` and `tag` calls that follow a superseded handle announce
the actual target once in each response's hint footer:
`[info] resolved pa509714 -> pa512121 (superseded)`.
Reads keep a cooled-down `was merged into pa512121` note in the footer.
Deleting a superseded handle refuses and names the survivor's delete call.

## Get a paper's supplementary information (SI)
## The SI / supporting-information PDF is missing — how do I pull it?
## Does a search hit in SI text cite the SI or the paper?

```python
put(kind="paper", id="smith2023", mode="fetch-si")  # queue the SI fetch
```

This only **queues**: the fetch worker's next tick looks for the SI
(Figshare mirrors, Crossref component DOIs, the publisher landing page)
and downloads each PDF it finds. Calling again while pending is a no-op;
a fresh call re-arms it. `mode=` other than `'fetch-si'` raises `BadInput`.

Records on the paper: `meta.si_checked_at`, `si_found` (candidates),
`si_fetched` (PDFs queued), `si_misses` (`{url, source, reason}` —
`cloudflare_403` is a recorded miss, never retried around), `si_skipped`
(non-PDF files not ingested), and one `si_found` / `si_none` /
`si_blocked` / `si_error` row in `get(view='log')`. A paper with no SI
is not re-checked until you call again; a check cut short by budget or
transient failure retries up to 3 times.

You rarely need to call it: opening a paper that has a DOI (web page,
`get` overview, or a citing fisheye ring) queues **one** SI check
automatically, ahead of the stub backlog; only `fetch-si` forces a
re-check.

Each SI PDF becomes **its own paper ref** titled `Supporting Information:
<parent title>`, linked to the parent (`part-of` → parent, the edge
carrying `meta.role='supplement'`); its chunks are searchable like any
body text and it carries no DOI of the parent's. **An SI ref cites as its
parent:** a search hit in SI text reads `SI of <parent> — cite as
<parent>`, and a `pc<id>`/`pa<id>` handle of the SI exports as the
parent's citation. `get(id=<parent>)` lists its SI refs; `get(id=<SI>)`
names its parent. To attach an SI PDF by hand, inbox it with a sidecar
`role: supplement` and `ref_id` = the parent; to supply the PDF for a
wanted stub, inbox it named by the stub's handle (`pa<id>.pdf` or
`pa<id>-<anything>.pdf`) — it folds into that stub instead of minting a
twin; a handle naming a paper that has a body, a retired ref or a
non-paper is ignored and the file ingests as a new paper.
Or attach a PDF to a Discord message to asa in a served channel after
linking on `/account` — asa queues it and replies with the handle. One
`pa<id>` requests the same stub fold; multiple handles are refused as
ambiguous.
To declare an existing record as an SI after the fact:
`edit(kind="paper", id="<si handle>", mode="replace",
args={"supplement_of": "<parent slug or paN>"})`; refuses when the record
is already an SI, holds its own DOI, or the parent is itself an SI. Draft
exports cite an SI record as its parent with an SI postnote
(`\cite[SI]{parent}` in LaTeX, `\cites{a}[SI]{parent}` in a group,
"[n] (SI)" in docx).

## Refine a search that returns too many papers
## What if there are more hits than I see?

```python
search(kind="paper", q="photocatalytic NOx reduction selectivity", page_size=20)
```

Search returns a bounded top-N set, not continuation pages. Navigate it with
`view='toc'` (keyword facets, above), add distinctive terms, narrow `scope=`,
or reduce `page_size=`: `precis-search-help`.

## How many papers? Is ingestion landing full text?

```python
search(kind="paper", view="count")                                  # total
search(kind="paper", view="count", tags=["STUDYTYPE:review"],
       args={"group_by": "tag:STUDYTYPE", "since": "2026-10-01"})   # filtered + facets
search(kind="paper", view="count", args={"group_by": "fulltext"})   # full-text vs thin-text vs card-only vs stub + passages
search(kind="paper", view="ingest", args={"days": 14})              # landings per UTC day
```

`view='count'` = the numeric-ref count view (`precis-search-help`).
`view='ingest'`: per day, papers whose first **full-text** chunk landed
(non-card, non-references body chunk) vs **card-only** papers created
(only `card_*` abstract/title chunks so far).

`view='ingest'` and `view='status'` render fixed sets, so the cohort
args they can't express — `unread`, `ingested_after`/`ingested_before`,
`id_min`/`id_max`, `title=`/`author=` — refuse there: the no-`q` listing
or `view='count'` is the filterable door for those.

## Which of these papers are full text, abstract-card only, or stubs?

```python
search(kind="paper", view="status", args={"ids": ["pa123", "wang2020state"]})
get(kind="paper", id="pa123", view="status")   # same table, one paper
```

`view='status'` (up to 200 handles/slugs/DOIs): one row per *input* id,
in input order — `<asked> -> pa<id>  [cite_key]  <identifier>  <body state>`.
The identifier is the DOI, then an `arxiv:` or `s2:` fallback, or `—`.
Body state is `full-text`, `thin-text`, `card-only` or `stub`, followed by
the body chunk count, PDF status, newest fetcher outcome (`fetch: <event> <ts>Z
(<source>)`, event `fetch_ok`, `fetch_failed` or `no_oa_version`; or
`fetch: never tried`) and a title excerpt (`— <title>`). A `pc`
chunk handle lands on its owning paper's row; an input whose paper was
already shown reads `same as <asked>`; a miss reads `unresolved` and
the header counts it (`fetch status — M ids -> N papers (D duplicate
inputs, U unresolved)`, misses listed by name). A `dr`/`dc` container
id resolves to every paper it cites yet still prints ONE row — each
cited paper's status a `;`-separated segment. `thin-text` = ≤12 body
chunks **and** a `no_oa_version` newest verdict — an abstract or
skeleton, not article body. Backlog to chase: `view='stubs'`.

`view='chunks'` sizes one paper (or an `id=[...]` batch, ≤50): total
chunks, body count, and breakdowns by `chunk_kind` and `section_path`
tail:

```python
get(kind="paper", id="pa5044", view="chunks")
get(kind="paper", id=["pa5044", "pa31"], view="chunks")
```

## Filter a paper search to a cohort — serves, links, venue, stamps

To filter a paper search to a quest's servers / to full text only:

```python
search(kind="paper", q="ammonia", args={"serves": "quest:164903"})
search(kind="paper", q="ammonia", args={"serves": "quest:164903", "has_fulltext": True})
```

To list papers by their link neighbourhood — inbound-linked from a
kind, or lacking a link to a kind (`tags=` then filters the
`linked_from` side, not the papers):

```python
search(kind="paper", args={"linked_from": "memory", "lacks": "finding"})
```

Add `tags=[...]` to narrow the linking side — e.g. a `SPACE:` tag
filters the memories that link, not the papers.

No `q=` gives a paged listing; `view='count'` the filtered total. The
same `args=` cohort keys compose there — `serves`, `linked_from`/`lacks`,
`stamp`, `record_type`, `has_fulltext`, `min_quartile`, `unread`,
`ingested_after`/`ingested_before`, `id_min`/`id_max` — and args the
count can't express (`chunk_kind`, ranking/paging knobs) refuse instead
of dropping silently.

To filter papers by journal standing — Scimago SJR quartile plus OpenAlex
source stats, loaded into `venue_metrics` by `precis venue-metrics`
(`--scimago` yearly CSV / `--openalex` corpus ISSN sweep; JCR is licensed
and stays out):

```python
search(kind="paper", q="ammonia", args={"min_quartile": "Q1"})   # Q1 venues only
search(kind="paper", q="ammonia", args={"min_quartile": "Q2"})   # Q1 or Q2
```

`min_quartile` joins the paper's `meta.issn` to the venue's **latest**
SJR vintage; a venue the feed never covered (or never quartiled) drops
out. It composes with the other cohort keys and with `view='count'`,
and needs a driver — `q=`, `tags=` or a listing filter — alone it
refuses. The same stats ride the overview: a `venue metrics:` line
(SJR, quartile vintage, OA 2yr-mean-cited, OA h-index) appears under
the journal/year line when the paper's ISSN is covered.

To keep only chunks of one body kind — e.g. table-chunk hits across
the corpus, the search-side companion to `view='tables'`:

```python
search(kind="paper", q="sticking prefactor", args={"chunk_kind": "table"})
```

`args={'chunk_kind': …}` takes one slug or a list from the
`chunk_kinds` catalogue (`paragraph`, `table`, `caption`, `equation`,
`figure`, …), implies the per-chunk hit shape like `scope=`, and
refuses an unknown slug with the valid list.

To list or count the papers carrying a worker/enrichment stamp —
the `refs.meta` keys a pass writes to stop re-claiming a paper
(`s2_enrich_failed`, `s2_enriched_at`, …):

```python
search(kind="paper", args={"stamp": "s2_enrich_failed"})
```

With no `q=` this is a paged listing — handle, truncated stamp value,
title — with the cohort total in the headline; with `q=` it restricts
the hits to stamped papers. `edit(kind='paper', id=…,
args={'clear_stamp': '<key>'})` (precis-paper-ref-help) removes the
stamp to re-open the pass.

## Find open-access papers or papers with an arXiv ID

```python
search(kind="paper", q="photocatalysis", tags=["ACCESS:open"])
search(kind="paper", tags=["ACCESS:arxiv"])
search(kind="paper", view="count", tags=["ACCESS:open", "!ACCESS:arxiv"])
```

`ACCESS:arxiv` matches stored arXiv identifiers, including arXiv DOI and
paper-ID aliases and legacy arXiv metadata. `ACCESS:open` includes those
papers and papers whose `meta.openalex.oa_status` is gold, green, hybrid,
diamond or bronze. These read-only markers reflect the current metadata.
They describe public availability. Check the specific version's licence
before redistributing text or chunks. Add `args={"has_fulltext": True}`
to require a locally indexed body.

## List unread papers / papers ingested since a date
## Which papers did a read-paper pass never close?
## Sweep newly landed papers / walk the unread backlog

To list papers a read-paper pass never closed (no `read-paper:` note),
and/or bound by *ingest* time — a recurring's daily sweep or a retro
backlog walk:

```python
search(kind="paper", args={"unread": True, "ingested_after": "2026-11-12"})
search(kind="paper", tags=["area:nanoreactor"], args={"unread": True})
search(kind="paper", q="photocatalysis", args={"unread": True})
```

`unread` is paper-only (`False` keeps only read papers); a hidden note
(`delete` `args={'comment': K}`) re-opens the paper. `ingested_after`/
`ingested_before` take ISO dates/timestamps and bound the **landing**
stamp — first body chunk, ref-mint for card-only stubs (`since=`/
`until=` bound ref mint, `after=`/`before=` publish year) — so a stub
whose body lands today is in today's sweep. No `q=` gives the paged
listing; `q=` restricts hits; `view='count'` totals.

The listing orders by ref id newest-first and pages by keyset, not
`page=`/`offset=` (both refuse): the `next:` row re-issues the same
call with `args={'id_max': <last id − 1>}` added, so a backlog deeper
than `page_size` (≤100) still walks — `id_min`/`id_max` on their own
are also a plain "papers since id N" listing. Every filter on the call
(tags, cohort args, `exclude=`, year/created windows) rides the
continuation verbatim; a `cited=`/`uncited=`/`hubbed=` scope can't
respell and the note says to re-add it. A `q=` ranked page's steering
hints keep the same discipline — `see every matching chunk`, the
tighten-the-query steer and the empty-result widen all re-issue
`unread`/ingest/id bounds and every other active filter, so following
a hint never widens a filtered sweep.

## Filter papers by publication year
## Find recent papers / papers from a date range
## Only papers published after / before a year

```python
search(kind="paper", q="solid-state batteries", after=2019)  # 2019→present
search(kind="paper", q="solid-state batteries", before=2015)  # up to 2015
search(kind="paper", q="solid-state batteries", after=2019, before=2023)  # 2019–2023
```

`after=` / `before=` are **inclusive publication-year bounds** (the
corpus stores year, not full dates — `after=2019` means 2019 and later,
`before=2023` means 2023 and earlier). Paper kind only. Papers with **no
year on record are excluded** from a year-filtered search; when that
happens the response appends a `⚠ N matching paper(s) omitted` line so a
sparse result isn't mistaken for "nothing exists" — fix missing years via
`/papers/triage`, or drop `after=`/`before=` to include them.

## Read a paper or one of its sections
## Open a paper's TOC to see what's in it
## I have a handle — what's in this paper?

Opening a paper cold? Start with the TOC — it's the entry point for a
whole-document survey. Already holding a chunk handle `pc<id>` (a
search hit, a citation, a TOC row)? Skip the map and read it in
context directly.

```python
get(kind="paper", id="pa40", view="toc")  # whole-paper survey start
get(kind="paper", id="pa40", view="summaries")  # per-chunk gloss + keywords
get(kind="paper", id="pa40~49..151", view="summaries")  # inclusive chunk range
get(id="pc512")  # single block by chunk handle
get(id="pc512", extent="fisheye")  # a known block + its surrounding context
get(kind="paper", id="pa40", view="abstract")
get(kind="paper", id="pa40")  # full overview
get(kind="paper", id="pa40/toc")  # path form = view='toc'
get(kind="paper", id="<slug>~63..89")  # drill a slug range
get(kind="paper", id="<slug>~63..89", view="toc")  # sub-TOC of a range
```

TOC rows are drillable: each row leads with the block handle (`pc<id>`,
or a `pc<id>..pc<id>` range) — paste it back as `id=`. Each row shows
the segment's most-distinctive keywords. Segments are clustered
dynamically by content at request time.

The top-level (unscoped) TOC header also carries a readiness line —
`readiness: embedded 41/44 · summarised 44/44` (plus `· failed N` when
some chunks' embedding attempts genuinely failed) — so you can poll
"is my edit indexed yet" instead of guessing.

**Truncated body?** appears on the unscoped paper TOC when the held body
has fewer than 12,000 characters and the last chunk appears to stop
mid-sentence. The copy may contain only front matter and part of the
introduction. Re-fetch the full text before drawing conclusions from
missing sections or results.

`view='summaries'` lists each body chunk's `~ord` handle, `llm-v1` gloss
and keywords. Use the keywords where the summariser has yet to write
a gloss. The web reader's Semantic/Keyword sidebar uses the same data.

Views: `lookup`, `abstract`, `toc`, `summaries`, `tables`, `figures`, `measures`,
`refs` (`references`), `si`, `sentences` (the last three: see Read a
paper's own reference list), `bibtex` (`cite/bib`), `ris` (`cite/ris`),
`endnote` (`cite/endnote`), `links`, `claims`, `authors`,
`comments` (the note timeline — see Add a note to a paper),
`corrections` (human/inferred fixes of glyph-damaged passages — see
precis-glyph-correction-help; recorded with `put(id='pa…',
args={'correction': {…}})`),
`ops` (full-state JSON dump — see precis-paper-ref-help), `pdf_link`
(signed download URL for the held PDF — see precis-paper-ref-help). The
`view=` kwarg and `slug/<view>` path are equivalent (except for DOIs — see
above). All are ref-level except `sentences`, which is chunk-scoped: a
`pc<id>` chunk handle takes no other `view=` —
`get(id='pc512', view='abstract')` is refused; use the paper handle
(`pa<id>`) for every view but `'toc'` and `'summaries'` (which also
scope to `pa<id>~N..M`) and `'sentences'` (which needs the chunk —
`id='pc512'` or `pa<id>~N`).

## Find and read a paper's tables and extracted measures

`view='tables'` renders every `chunk_kind='table'` chunk as a parsed
grid — one `T<n>` section per *logical* table with its `pc` chunk
handle, the nearest `Table <n>` caption chunk, header-unit hints
(`(ML)`, `(kJ/mol)`), a `measures: N extracted` counter, and numbered
rows you cite back as `pc<id> row R col C`. A big table ingest split
into consecutive chunks stays one `T<n>` — continuations are labelled
`T1 (part 2)` and share the caption. A table chunk that isn't a
well-formed pipe grid renders raw rather than being guessed at.
`args={'table': 2}` (or a `pc` handle) renders one table;
`args={'match': 'sticking prefactor'}` keeps tables whose header,
caption or row labels hold any of the words. With no
`chunk_kind='table'` chunks at all, the view instead lists body chunks
that look like labeled tables (`Table <n>` caption — including
`**Table 2** …` and `Table S1:` — or a pipe grid) with their `pc`
handles. Read a chunk directly with `get(id='pc…')`. The fallback lists
up to 12 matches in body order. If more matches exist, follow the
`Next:` call with `args={'after_ord': N}` to list the next page.

The **Needs refinement** warning means the extracted text refers to pages
beyond the held PDF. Treat its extracted tables as untrusted and follow the
repair steps in [[precis-paper-ref-help]]. When a second reading is stored,
`args={'reading': 'vision'}` lists its zero-based PDF page indices and
`args={'reading': 'vision', 'page': N}` opens one page's independent
image-based reading with provenance. Use the vision selector on its own.

`view='measures'` lists outputs grouped by table (`T<n>`), then prose,
with the same eligibility as default paper-scoped measure search. It
matches the paper as subject or anchor owner and includes flagged outputs
with their `escalated: <rule>` reasons. `view='tables'` counts unsuperseded
outputs anchored in each table chunk.
Read a result with `get(kind='measure', id=…)` for its table-cell anchor.

```python
get(kind="paper", id="pa40", view="tables")
get(kind="paper", id="pa40", view="tables", args={"table": 2})
get(kind="paper", id="pa40", view="tables", args={"match": "sticking"})
get(kind="paper", id="pa40", view="measures")
```

## Number a chunk's sentences for a measure anchor
## Verify a sentence-span anchor before writing a measure
## Which sentence is 's3' on this chunk?

`view='sentences'` is the chunk-scoped numbered-sentence read, built for
verifying a measure anchor before writing it: `get(id='pc512',
view='sentences')` (or `get(id='pa40~63', view='sentences')`, the path
form `get(id='pa40~63/sentences')`, or a `~A..B` range) renders the
block split into sentence spans by the anchor
splitter, one `s<N>: …` line per span, numbered from `s0` per chunk.
The path form routes without `kind=` too — `get(id='pc512/sentences')`
self-identifies. The `s<N>` you see is the `span` a measure anchor
names — `anchor={'chunk': 'pc512', 'anchor_scheme': 'sentence', 'span': 's2'}`.
On the paper handle alone it refuses and names the chunk-addressed call —
sentence spans are per-chunk. When a page reaches the response budget,
follow the printed `rest: get(id='pa40~63/sentences/s40..80')` window to
continue with the original numbering. Use `s40..` to read from s40 to
the end of the chunk. If one sentence exceeds the page budget, the read
marks it as an excerpt and continues with the following sentence. Verify
a literal beyond the excerpt against the stored chunk text.
Recipe and write path: `precis-measure-help` §Evidence.

## Find a passage in a paper I have
## Locate where a topic comes up in a specific paper
## Where does this paper discuss X?

```python
search(kind="paper", q="Z-scheme", scope="pa40")  # scope by handle
search(kind="paper", q="Z-scheme band alignment", scope="pa40", page_size=10)
```

Same hybrid search as cross-corpus, scoped to one paper's blocks.

## Read a paper's own reference list (its bibliography)
## What papers does this one cite?
## List the references printed at the end of a paper

`view='refs'` renders the paper's OWN reference list — the bibliography
printed at the end of the paper, not the citations pointing at it
(that's `view='bibliography'`). Parsed rows show the real `[N]` marker,
the entry, its resolved DOI and a held-ref handle when we hold the
cited paper — read that one with `get(id='<pa>')` to mine its
bibliography in turn. If no parsed entries are available, the view shows
any raw `references` chunks. `view='references'` is the same view.

```python
get(kind="paper", id="pa40", view="refs")
```

`view='si'` lists a paper's supplementary-information records with
their handles (cite an SI as its parent, never directly); read on an SI
ref itself it names the parent. `view='sentences'` splits a block into
numbered sentences for precise quoting — it needs a chunk selector
(`pa<id>~N`/`~N..M`) or a `pc<id>` chunk handle. Sentence text comes
back verbatim so it matches the stored block, numbered `s<N>` by the
anchor splitter (see the measure-anchor read above).

For a citation slug, the view paths are `/lookup`, `/abstract`, `/toc`,
`/summaries`, `/figures`, `/refs` (also `/references`), `/si`, `/sentences`,
`/bibtex`, `/ris`, `/endnote`, `/cite/bib`, `/cite/bibtex`, `/cite/ris`
and `/cite/endnote`. Use `view=` for other views or when the ID is a DOI.
A `pc<id>` chunk handle supports scoped `toc` and `sentences` reads.

```python
get(kind="paper", id="pa40", view="si")
get(kind="paper", id="pa40~38", view="sentences")
get(kind="paper", id="pc512", view="sentences")  # split the chunk behind a pc handle
```

## Cite a paper
## Get a BibTeX or RIS entry for a paper
## I need to cite this in my manuscript

```python
get(kind="paper", id="pa40", view="bibtex")
get(kind="paper", id="pa40", view="ris")
get(kind="paper", id="pa40", view="endnote")
```

## Cite a figure (caption only — no image binaries)
## Reference a figure by its legend
## How do I cite Figure 3 of this paper?

`get(kind='paper', id='pa40', view='figures')` lists figure chunks and
standalone caption chunks without a leading numbered table label such as
`Table 1` or `Table S2`. Each row gives the `pc` and `~ord` handles.

Image files aren't served. The figure block holds a markdown image
marker; the legend is on the next block.

```python
get(id="pc517")  # the figure block, by handle
```

```text
Figure 3. Schematic representation of the structure of NU-1000…
```

Cite as "Figure 3 of `pa40`" and quote the legend. The
`![](...)` marker in the body is a relative path nothing serves —
don't invent URLs. (A figure *inside a paper* is this; the `figure`
kind is a different thing — a drawable SVG canvas, see
[[precis-figure-help]].)

## Add a note to a paper
## Attach an annotation that stays with the record
## Where do per-paper notes live?

```python
put(kind="paper", id="pa40", text="replication code: github.com/…")
get(kind="paper", id="pa40")                     # overview shows the newest note
get(kind="paper", id="pa40", view="comments")    # all notes, oldest first
delete(kind="paper", id="pa40", args={"comment": 2})          # hide a stale note
delete(kind="paper", id="pa40", args={"comment": 2, "restore": True})  # undo
```

Notes are append-only (`paper_note` chunks): `put(id=…, text=…)` on an
existing paper appends one and takes no other field in the call. They
are not body text — body search, embeddings, citations, `slug~N` chunk
reads and body rewrites (`drop_body`, `reextract`, merges, restores)
never see or touch them; `view='comments'` is their read surface, with
`args={'comment': K}` for one note and `args={'comments': K|'A-B'}` for
the newest K or a number window. `put(id=…)` without `text=` keeps its
other doors (`fetch-si`, `pdf_url`, `record_type`, `restore`).

## Tag a paper or cross-link it
## Annotate a paper with topic tags or relationships
## How do I mark this paper as topic:X?

```python
tag(kind="paper", id="pa40", add=["topic:photocatalysis"])
tag(kind="paper", id="pa40", add=["SRC:primary"])
tag(kind="paper", id="pa40", remove=["topic:photocatalysis"])
tag(kind="paper", id="pa40", remove=["quest:164903"], reason="backfill: quest retired")

link(kind="paper", id="pa40", target="pa57", rel="cites")  # target is also a handle
```

Closed-prefix axes for paper: `SRC:`, `CACHE:`. Open tags
(`topic:x`, ...) always allowed. Other axes (`STATUS:`, `PRIO:`, ...)
are rejected. Tag and link operate at the paper level — chunk
selectors and view paths are rejected.

Pass `reason=` on a retag to say why — one `agent/retagged` row per
affected paper lands on `ref_events`, visible in
`get(kind="paper", id="pa40", view="log")`; omit it and the retag
stays silent.

## See a paper's link graph
## What is this paper linked to — cites, cited-by, related-to?

```python
get(kind="paper", id="pa40", view="links")
```

Every link touching the paper, both directions — outbound
`cites`/`related-to`/… and inbound rows rendered in passive form
(`cited by`, `supported by`, …). Each row is expandable via the
`get(...)` call it prints.

## What Taproot claim hubs does this paper ground?
## Does this paper already establish/corroborate/contradict a claim?

```python
get(kind="paper", id="pa40", view="claims")
```

Every live Taproot claim hub this paper carries an inbound evidence edge
to (`establishes`/`corroborates`/`contradicts`), with each hub's posture
(`state`/`support`/`flags` — refuted/disputed/drifted show up here too)
and the `role` this paper's own edge plays. A hub grounded here but not
yet assigned a `pub_id` renders `pub_id: <uncited>` — it's a mint-
frontier signal, not a citable claim yet. This is NOT the patent kind's
`view='claims'` (that one is a patent's legal claim set).

## Chunk-level citation grounding (dark, opt-in)
## Does this specific claim actually check out against what it cites?

Cluster-side only (`inbound_chase` pass, dark behind the
`inbound_chase.enabled` settings flag — flippable via
`put(kind='job', id='/settings')` only after your explicit
confirmation, per `precis-job-observe-help`; a per-host service_config
priority stays the operator's stop): once
a paper has been read
at least once, a background pass exhaustively resolves *who cites it*
at chunk granularity, using an LLM to verify support
(yes/partial/no + caveats) — the inverse of the `finding`-chase
machinery (`precis-finding-help`). When live, a chunk that itself
cites another paper with a resolved verdict shows a capped
"Cites (verified):" sidecar under its body — best-first, ≤5 rows,
`no` verdicts dropped, each row a `get(...)` handle, never eagerly
expanded. Nothing to configure from the agent side; this is a reading
affordance that either shows up or doesn't.

## Reading-priority score (dark, opt-in)
## Which held paper is worth reading next?

Cluster-side only (`paper_rank` service, default off — flip from the
web Status → Services tab or `precis service prio '*' paper_rank <n>`):
a background pass scores every held paper 0-100 on query-independent
signals — citation impact and velocity, standing in the corpus's own
citation graph, rigor/artifact markers — answering "worth reading at
all", the complement of search's "matches this query". Retracted
papers are capped low regardless of their other signals. When live,
the score shows as the "read-first" row on the paper's web Meta panel.
Nothing to configure from the agent side.

## Request a paper the library doesn't have
## Get a paper we don't hold yet

`put` on a paper mints a **stub** (a request) — it never writes a body
(bodies are import-only). The `fetch_oa` worker chases an OA PDF for
any stub carrying an identifier:

```python
put(kind="paper", doi="10.1038/nature10352")  # best — resolvable id
put(kind="paper", arxiv="2401.00001", title="…")  # or an arXiv id
put(kind="paper", doi="10.1038/nature10352", tags=["reading-list"])  # tags= applied to the stub
put(kind="paper", title="Some Paper With No DOI Yet")  # title-only backlog stub
put(kind="paper", title="A Book", year=2024, tags=["is-book"], args={"authors": ["Doe, Jane"]})
put(kind="paper", doi=["10.1038/nature10352", "10.1126/…"])  # bulk: one ingest job per DOI
```

`authors=` (also accepted inside `args=`) stores the fresh stub's byline
and seeds its citation key. Use names or `{given, family}` dictionaries;
omit the field if unknown. A duplicate keeps the existing byline and the
receipt names `edit(kind='paper', id=…, authors=[…])` to change it.

Idempotent (already-held / already-wanted is a no-op). A DOI Semantic
Scholar doesn't know is tried against Crossref, then DataCite, before
refusal — only a DOI no registry knows is rejected as likely mistyped
(`verify=False` remains the explicit override). When S2 cannot answer
at all, a DOI is still checked against the registries; a stub minted
anyway is tagged `acquire:unverified`, and the tag clears itself once
S2 enrichment or the fetch confirms the paper (`meta.acquire_verified_by`).
Full contract + backlog view in `precis-stubs-help`.

**A slow put is expected.** Let it finish without re-issuing it.

Submit up to 50 DOIs in one `doi=[...]` call. The server queues a
`paper_ingest` job per eligible DOI and handles resolver backoff in the
worker. The receipt names jobs to poll with `get(kind='job', id=…)` and
reports DOIs already held, already queued, or failed within 24 hours.
Use single puts for `tags=`, `arxiv=`, or `verify=False`.

## Find the paper to request — walk a citation graph
## What does this paper cite? Who cites it?

To find the *right* primary source to stub, navigate a known paper's
citation graph via Semantic Scholar. Each row carries the neighbour's
DOI — feed it straight into a `put(kind='paper', doi=…)` request:

```python
get(
    kind="semanticscholar", id="refs:10.1038/nature10352"
)  # papers it cites (its bibliography)
get(
    kind="semanticscholar", id="cites:10.1038/nature10352"
)  # papers citing it (forward citations)
get(
    kind="semanticscholar", id="<title or topic>"
)  # or search by topic → ranked hits + DOIs
```

`<paper-id>` is any S2-resolvable handle — a bare DOI, an arXiv id
(`2401.00001`), a raw S2 hash, or a prefixed `DOI:` / `ArXiv:` /
`CorpusId:` / `PMID:` form. This is the high-precision way to fill a
citation gap: the held review's reference list almost always contains
the primary source you're missing. (Cached 30 days; capped at 50 rows
per hop.)

## See also

- [[precis-overview]] — verbs and kinds
- [[precis-search-help]] — search mechanics
- [[precis-literature-search-help]] — the end-to-end lit-search procedure
- [[precis-identify-publication-help]] — abstract/byline/citation to a verified manuscript
- [[precis-relations]] — related-to, disputes between papers
- [[precis-tags]] — axis vocabulary
- [[precis-paper-tag-axes]] — paper-specific axes
- [[precis-finding-help]] — chasing un-ingested DOIs
- [[precis-stubs-help]] — papers we still need to get
- [[precis-paper-ref-help]] — repair a record: bylines, identifiers, merges, retire, stamps, attach a PDF
- [[precis-cite-paper-help]] — how do I cite a paper? (the router)
- [[precis-check-source-help]] — find a citation, read surrounds, judge support
- [[precis-citation-help]] — verifier workflow for writing
- [[precis-memory-help]] — capturing thoughts from a paper
