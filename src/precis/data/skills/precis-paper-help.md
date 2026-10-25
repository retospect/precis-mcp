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
get(kind="paper", id="pa40", view="toc")  # TOC — the reading entry point
get(kind="paper", id="pa40", q="NOx")  # passages inside the paper (find-in-ref)
get(kind="paper", id="10.1038/nature10352")  # bare DOI resolves via metadata
get(kind="paper", id="10.1038/nature10352", view="abstract")  # DOI + view = kwarg only
get(kind="paper", id="10.1038/nature10352", view="toc")
```

The handle is the thing to keep. The DOI is a *lookup key* for papers
already held — once the paper is held, `pa<id>` is its canonical address.
DOI suffixes can contain `/`, so DOI + view needs the `view=` kwarg — the
`id/view` path form is ambiguous.

## The DOI isn't indexed yet — how do I get the paper?

`get` never fetches. A DOI that is not held answers "not ingested" and
names the next call: `put` mints a stub and the fetch worker pulls an
open-access PDF on its next pass. `get` does not make that call for you
because the fetch can cost: when no open-access copy exists, the last
leg is the paid OpenAlex Content API (~$0.01 a file).

```python
put(kind="paper", doi="10.1126/sciadv.aeh2779")  # mint stub, queue fetch
get(kind="paper", id="10.1126/sciadv.aeh2779")  # works once it is held
```

## I only have a citation string (journal, volume, page, year)

```python
put(kind="paper", citation="Phys. Rev. B 47, 558 (1993)")
```

The string goes to Crossref's bibliographic query. Exactly one record
matching every part of it (year, volume, first page, journal) is stubbed
by its DOI. Anything weaker returns candidates with a `conf=` score and
mints nothing; pick one and `put(kind="paper", doi="<its DOI>")`.

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

Each row is one paper — the best hit's chunk handle `pc<chunk_id>`
(paste back into `get`/`link`), a `+N` count of that paper's other
hits, and the best chunk's keywords; order is the relevance signal.
`unique_per='chunk'` (implied by `scope=`) returns one row per matching
chunk instead. Mechanics (mode=, pagination, tags=): `precis-search-help`.

When one phrasing isn't finding it, escalate (details in
`precis-search-help` → Broad retrieval):

```python
# Broad: fuse rephrasings + hypothetical-answer passages in one call
search(kind="paper", q="…", queries=["…", "…"], answers=["…"])
# Deep: async triage campaign — returns a job handle, poll for the curated result
search(kind="paper", q="…", good=True)  # → poll get(kind='job', id=…)
```

The default free-text `q=` search matches paper **bodies** and folds
in the embedded metadata card, so a topic query works and a title query
*often* surfaces the paper. But `q=` is body-first: for a known title or
author, use the targeted byline lookup below — it's more reliable.

## Look a paper up by its title
## Find a paper by author
## I know the title (or an author) — get the record, not body hits

```python
search(kind="paper", title="attention is all you need")  # → the paper record
search(kind="paper", author="Vaswani")  # → papers with that author
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
paper itself. A bad-import stub that stays unfindable repairs in place
(`title=`, `authors=`, `doi=`, `year=`, `journal=`):
[[precis-paper-ref-help]].

## Fix a paper record — duplicates, phantoms, stamps, attach a PDF
## `already belongs to ref id=N` — an identifier collision
## Two live refs are the same paper — how do I merge them?
## A worker stamped a paper done/failed — how do I re-run it?

See [[precis-paper-ref-help]] — identifier repair, `merge_into`,
`redirect_to`, `retire`, the body doors (`drop_body`/`move_body_to`/
`split_body`/`clear_abstract`), `clear_stamp`, and `put` with
`args={'pdf_url': …}` to attach a legal open copy to a body-less stub.

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

## See additional papers after a search
## Page through more search results
## What if there are more hits than I see?

```python
search(kind="paper", q="photocatalytic NOx reduction", page=2)
search(kind="paper", q="photocatalytic NOx reduction", page=3, page_size=20)
```

Pagination mechanics (defaults, `page_size=` cap): `precis-search-help`.

## How many papers? Is ingestion landing full text?
## Which of these papers are full text, abstract-card only, or stubs?

```python
search(kind="paper", view="count")                                  # total
search(kind="paper", view="count", tags=["studytype:review"],
       args={"group_by": "tag:studytype"}, since="2026-10-01")      # filtered + facets
search(kind="paper", view="ingest", args={"days": 14})              # landings per UTC day
search(kind="paper", view="status", args={"ids": ["pa123", "wang2020state"]})
```

`view='count'` = the numeric-ref count view (`precis-search-help`).
`view='ingest'`: per day, papers whose first **full-text** chunk landed
(non-card, non-references body chunk) vs **card-only** papers created
(only `card_*` abstract/title chunks so far). `view='status'` (up to 200
handles/slugs/DOIs): per paper `full-text | card-only | stub`, PDF
present, and the newest fetcher outcome (`fetch_ok`, `fetch_failed`,
`no_oa_version`, or never tried). Backlog to chase: `view='stubs'`.

`view='chunks'` sizes one paper (or an `id=[...]` batch, ≤50): total
chunks, body count, and breakdowns by `chunk_kind` and `section_path`
tail:

```python
get(kind="paper", id="pa5044", view="chunks")
get(kind="paper", id=["pa5044", "pa31"], view="chunks")
```

To filter a paper search to a quest's servers / to full text only:

```python
search(kind="paper", q="ammonia", args={"serves": "quest:164903"})
search(kind="paper", q="ammonia", args={"serves": "quest:164903", "has_fulltext": True})
```

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

Start with the TOC — it's the entry point for any non-trivial paper.

```python
get(kind="paper", id="pa40", view="toc")  # start here
get(kind="paper", id="pa40", view="summaries")  # per-chunk gloss + keywords
get(id="pc512")  # single block by chunk handle
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

`view='summaries'` is the flat, per-chunk companion to the clustered
`view='toc'`: one row per body chunk — its `~ord` handle, its `llm-v1`
gloss (often empty — the summariser is a deliberate trickle), and its
extracted keyword string. Use it to scan every chunk's gist at once (the
web reader's Semantic/Keyword sidebar reads the same data); fall back to
the keyword column where a gloss hasn't been written yet.

Views: `abstract`, `toc`, `summaries`, `bibtex` (`cite/bib`), `ris`
(`cite/ris`), `endnote` (`cite/endnote`), `links`, `claims`, `authors`,
`ops` (full-state JSON dump — see precis-paper-ref-help), `pdf_link`
(signed download URL for the held PDF — see precis-paper-ref-help). The
`view=` kwarg and `slug/<view>` path are equivalent (except for DOIs — see
above). All are ref-level: a `pc<id>` chunk handle takes no `view=` —
`get(id='pc512', view='abstract')` is refused; use the paper handle
(`pa<id>`) for every view but `'toc'`, which also scopes to `pa<id>~N..M`.

## Find a passage in a paper I have
## Locate where a topic comes up in a specific paper
## Where does this paper discuss X?

```python
search(kind="paper", q="Z-scheme", scope="pa40")  # scope by handle
search(kind="paper", q="Z-scheme", scope="pa40", page=2)
```

Same hybrid search as cross-corpus, scoped to one paper's blocks.

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

## Tag a paper or cross-link it
## Annotate a paper with topic tags or relationships
## How do I mark this paper as topic:X?

```python
tag(kind="paper", id="pa40", add=["topic:photocatalysis"])
tag(kind="paper", id="pa40", add=["SRC:primary"])
tag(kind="paper", id="pa40", remove=["topic:photocatalysis"])

link(kind="paper", id="pa40", target="pa57", rel="cites")  # target is also a handle
```

Closed-prefix axes for paper: `SRC:`, `CACHE:`. Open tags
(`topic:x`, ...) always allowed. Other axes (`STATUS:`, `PRIO:`, ...)
are rejected. Tag and link operate at the paper level — chunk
selectors and view paths are rejected.

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

Cluster-side only (`inbound_chase` service, default off — flip with
`precis service prio '*' inbound_chase <n>` or `/categorizers`): once
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
```

Idempotent (already-held / already-wanted is a no-op). A DOI Semantic
Scholar doesn't know is tried against Crossref, then DataCite, before
refusal — only a DOI no registry knows is rejected as likely mistyped
(`verify=False` remains the explicit override). When S2 cannot answer
at all, a DOI is still checked against the registries; a stub minted
anyway is tagged `acquire:unverified`, and the tag clears itself once
S2 enrichment or the fetch confirms the paper (`meta.acquire_verified_by`).
Full contract + backlog view in `precis-stubs-help`.

**A slow put is backoff, not a wedge.** Minting a stub resolves the
identifier against Semantic Scholar, and S2 rate-limits burst traffic —
after several rapid puts, the next one can sit in auto-backoff for
minutes (and the serial server makes later calls wait too). Don't
re-issue the put; space out bulk stub minting and let it finish.

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
