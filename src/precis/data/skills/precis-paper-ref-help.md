---
id: precis-paper-ref-help
family: paper
title: precis — repair and maintain paper refs
summary: the doors that fix a paper record — bylines, identifiers, merging a duplicate pair, re-pointing a merged stub, retiring a phantom, re-arming a worker stamp, attaching a legal PDF; finding and reading papers is precis-paper-help
answers:
  - how do I fix a wrong or missing author on a paper?
  - two live refs are the same paper — how do I merge them?
  - an identifier collision blocked my edit — what now?
  - a merged stub resolves to the wrong paper — how do I re-point it?
  - how do I retire a phantom citation stub?
  - a ref holds the wrong paper's body — how do I detach or re-home it?
  - a ref carries one wrong identifier next to right ones — how do I remove just it?
  - the body is another paper's but the attached PDF is right — how do I re-extract it?
  - how do I undo a drop_body or rebuild a ref from its dump?
  - a worker stamped a paper done/failed — how do I re-run it?
  - how do I attach a legal PDF to a body-less stub?
applies-to: get/edit/put (kind='paper')
status: active
tags: [workflow]
kinds: [paper]
---

# precis-paper-ref-help — repair and maintain paper refs

A *ref* is the record a `pa<id>` handle names. Finding and reading
papers: [[precis-paper-help]]; the still-to-acquire backlog:
[[precis-stubs-help]]. Every door below is `edit(kind='paper', id=…)`
unless noted — `dry_run` previews each, and the `args=` doors require a
`reason` that lands in a ref_event.

## Quick reference — the edit doors

| I want to … | call |
|---|---|
| fix a wrong/missing author | `edit(id=…, authors=["Family, Given [iD]"])` |
| repair stub metadata | `edit(id=…, title=/doi=/arxiv=/year=/journal=/entry_type=/abstract=/volume=/issue=/pages=/url=)` |
| remove one wrong identifier row | `args={'drop_identifier': {'scheme': 'pubmed', 'value': '…'}, 'reason': …}` |
| fold a duplicate pair | `args={'merge_into': '<survivor>', 'reason': …}` |
| re-point a merged stub | `args={'redirect_to': '<pa>', 'reason': …}` |
| retire a phantom stub | `args={'retire': True, 'reason': …}` (`retire: False` restores) |
| re-home a wrong body | `args={'drop_body'/'move_body_to'/'split_body'/'clear_abstract': …, 'reason': …}` (drop_body takes `'keep_pdf': True` to keep the stored pdf) |
| export a ref's full state (undo) | `get(id=…, view='ops')` or `precis paper dump <id> --out <file>` |
| rebuild a ref from its dump | `put(id=…, args={'restore': <slice>, 'reason': …})` or `precis paper restore <file> --apply` |
| download a held PDF | `get(id=…, view='pdf_link')` — signed URL, no bytes over MCP |
| convert a misfiled datasheet | `args={'convert_to': 'datasheet', 'subtype'?: …, 'reason': …}` |
| re-arm a worker pass | `args={'clear_stamp': '<key>'}` (allow-listed keys only) |
| attach a legal PDF | `put(id=…, args={'pdf_url': '<url>'})` |
| declare a record as SI | `args={'supplement_of': '<parent>'}` — the SI section in [[precis-paper-help]] |

## See a paper's full byline with identity links
## Get ORCID / OpenAlex / Google Scholar links for a paper's authors
## Fix a wrong or missing author, and record the fix as verified

```python
get(kind="paper", id="pa40", view="authors")  # position, source, links, verified tick
edit(
    kind="paper",
    id="pa40",
    authors=["Goldsmith, Bryan R. [0000-0002-1825-0097]", "Zywucka, N."],
)
```

`view='authors'` renders one row per byline position: the display name,
the tier that wrote it, a `✓ verified <date>` mark once ORCID has
cross-checked the row, and up to three links — ORCID (`orcid.org/<iD>`
plus the `oi<id>` node handle when a `kind='orcid'` identity node is
linked), OpenAlex, and a Google Scholar name search (links-only —
Scholar has no API). A paper-level Scholar lookup (DOI-keyed when a DOI
is on file, else title) follows the table.

**Tier ladder** (a write only ever raises the paper toward a more
authoritative source): `orcid` (ORCID's own record) >
`crossref`/`openalex`/`s2` (publisher/index metadata) > `pdf`/`legacy`
(scraped from the PDF) — but `human` (a manual `edit`) always wins over
every later re-resolution.

`edit(kind='paper', authors=[…])` accepts the same tolerant name shapes
`author=` search does, **plus** an optional bracketed ORCID iD per line
— the row grammar `Family, Given Middle [0000-0002-1825-0097]` (the
bracket is optional; an invalid iD is dropped silently and the name is
kept). This always writes `source='human'` and stamps the paper's
`human_verified_at` sign-off — a manual byline correction *is* the
review that stamp records.

## Repair a stub's metadata in place — title, identifiers, venue

A paper whose metadata is missing (a bad-import stub) can be
unfindable; repair it with `edit(kind='paper', id='<slug>', title=…,
authors=[…])`, which rebuilds the card. A title-only stub's *identifier* is
repairable the same way — `edit(kind='paper', id='<slug>', doi='10.…')` (or
`arxiv=`) upgrades it in place, so prefer that over minting a second,
DOI-bearing stub and orphaning the first. A *wrong* identifier is removable
the same door — `doi=''` (empty string, not omitted) clears this ref's own
DOI without touching who owns it elsewhere; `arxiv=''` clears likewise.
Both are whole-scheme clears: every row of the scheme goes and the
`cleared_identifiers` marker stops sweeps re-deriving it. When the ref
holds a wrong value *next to* right ones — a merge that left the donor's
ids on the survivor — the surgical door is
`args={'drop_identifier': {'scheme': 'pubmed', 'value': '24970080'},
'reason': …}` (any scheme but cite_key; the value normalises like the
identifier lookups, so `'PMID:…'` forms work). One row goes, the rest of
the scheme stays, no cleared marker is stamped — a sweep can still land
the *correct* value later — and an `identifier_dropped` ref_event carries
the reason. It refuses with the ref's actual rows when the named pair
isn't held. A
mis-filed record's year and venue repair the same way — `year=2007`,
`journal='Nature Nanotechnology'` (also `entry_type=`, `abstract=`,
and the locator fields `volume=`/`issue=`/`pages=`/`url=` — the BibTeX
export reads them, so don't stuff them into the `journal=` string).

The needs-triage backfill (`precis.ingest.metadata_resolve`, "Bucket B")
guards against pulling in the *wrong* identity (gr353804): a Crossref/S2
registry hit whose title shares zero distinctive words with the paper's
own chunk-0 text (e.g. an SI PDF) gets the write withheld — tags the ref
`paper-meta:title-mismatch`, records the withheld `doi`/`title`/`reason`
under `meta.registry_mismatch` on the Meta tab, and clears the stored DOI
only for the zero-overlap case. Parked refs leave the triage sweep until
the tag is removed by hand; `human_verified_at` is never touched.

ALL-CAPS titles are repaired by the paper-meta enrich worker, not by
agents: for a DOI paper whose stored title is all caps, the agreeing
Crossref title replaces it (old kept as `meta.title_raw`);
`human_verified_at` freezes a title; a substantively different registry
title is left alone. Do not hand-normalise titles; BibTeX export
brace-protects acronym/formula tokens (`{DFT}`).

## Resolving a duplicate ref
## `already belongs to ref id=N` — what now?
## An identifier collision blocked my edit — how do I fix it?

Two *live* refs holding the same `doi`/`arxiv` is a duplicate-paper pair —
`edit`/`set_ref_identifier` raises `BadInput` rather than silently
stealing the other ref's claim. The fix is a merge, not a retry.

## Fold a confirmed duplicate pair — `merge_into`
## Two live refs are the same paper — how do I merge them?

```python
edit(kind="paper", id="pa498282", args={"merge_into": "pa508953", "reason": "same paper, stub vs held twin"})
```

Folds the `id=` loser into the `merge_into` survivor (a `pa<id>` handle,
slug or ref_id) in one transaction: external identifiers, links and bib
holds migrate, a body/PDF the survivor lacks moves over, the loser
retires with a `supersedes` redirect so its handle keeps resolving, and
both sides get ref_events carrying `reason` (required). `dry_run`
previews. A bodiless survivor takes the duplicate's body, `pdf_sha256`
and content ids, so a wanted stub can survive a merge with a bodied twin
— pick the side whose handle/identifiers people use.

Refuses, naming the fix: a self-merge; a missing `reason`; a loser
already merged (re-point with `redirect_to`, below) or plain-retired
(`retire: False` restores first); a target that isn't live (an
itself-merged target names the chain end to aim at); and a pair that
fails the shared title gate — trigram similarity ≥ 0.85, ≥ 5 distinctive
title tokens, known years within ±1 (a missing year needs near-identical
titles), agreeing first-author family. On a gate refusal that is still
the same paper, fix the disagreeing field with `edit` and retry.

The standing sweep (`precis reconcile-duplicates`, an operator CLI —
prod invocation via `docs/runbooks/prod-one-off-cli.md`) folds the
groups nobody pointed at and picks its own survivor.

## A merged stub resolves to the wrong paper — how do I re-point the redirect?

```python
edit(kind="paper", id="pa42495", args={"redirect_to": "pa2997", "reason": "mis-merge gr501446"})
```

A retired/merged ref's `meta.superseded_by` + `supersedes` edge are what
make its handle resolve to the survivor. `redirect_to` moves both in one
transaction, addressed at the tombstone itself (its `pa<id>` handle,
ref_id or slug — dispatch does not follow the redirect for this call).
Refuses a source that isn't a retired merge, a target that isn't live
(itself merged → aim at the chain end it names), and cycles. `reason` is
required and lands in the `redirect_repointed` ref_event with old+new
targets; undo is the same call aimed back at the old target.

## A phantom citation stub should not exist — how do I retire it?

```python
edit(kind="paper", id="pa454024", args={"retire": True, "reason": "invented citation, gr509441"})
```

Soft-deletes a stub that should never have been minted; `reason` lands
in a `deleted` ref_event and `retire: False` on the tombstone undoes it
(`restored` event). Refuses anything more than a phantom: body chunks,
an attached PDF, an external identifier, live inbound cites (edges,
`[pa<id>]`/`§<cite_key>` tokens in draft chunks, bib holds), or inbound
`supersedes` (move each tombstone's redirect with `redirect_to` first).
A duplicate of a real record wants a merge instead. `dry_run` previews.

## A ref holds the wrong paper's body — how do I detach or re-home it?

```python
edit(kind="paper", id="pa506039", args={"drop_body": True, "reason": "wrong pdf"})
edit(kind="paper", id="pa254044", args={"drop_body": True, "keep_pdf": True, "pdf_sha256": "<sha to keep>", "reason": "body is another paper's; re-extract own pdf"})
edit(kind="paper", id="pa1108", args={"move_body_to": "pa42053", "reason": "it's 42053's"})
edit(kind="paper", id="pa41", args={"split_body": {"title": "Companion paper"}, "reason": "different work"})
edit(kind="paper", id="pa174214", args={"clear_abstract": True, "reason": "junk abstract"})
```

`reason` required, `dry_run` previews, one DELETE+INSERT transaction.
Judge the body before you drop it — `get(id=…, view='tables')` renders
its `chunk_kind='table'` chunks as parsed grids (caption, units, a
`pc<id> row R col C` cell cite), and
`search(kind='paper', q=…, args={'chunk_kind': 'table'})` restricts
hits to table chunks.
`drop_body` deletes body chunks + embeddings, clears `pdf_sha256` and
content ids, keeps metadata — a live stub, refetchable via `pdf_url`.
Add `'keep_pdf': True` when the ref's own PDF is the right file and only
the body is wrong (e.g. it arrived by a dedup merge): the pointer and its
`pdf_sha256` identifier stay, so the stub is a bodiless-heal candidate —
the `paper_reconcile` pass or `precis bodiless-heal --apply` re-extracts
the stored file, no refetch needed. When the ref is attached to several
PDFs (several `pdf_sha256` identifier rows), `'pdf_sha256': '<sha>'` names
the one to keep — required then; naming an attached sha that is not the
pointer re-points `refs.pdf_sha256` at it. It refuses when the named sha
isn't attached or several are attached and none is named.
`move_body_to` gives body, pdf and content ids to a live ref holding
zero body chunks and no pdf (refuses otherwise: `drop_body`/
`merge_into`/`bodiless-heal`); the donor ends a live stub.
`split_body` mints a NEW ref from the dict
(`title` required) and moves the body onto it; a held DOI refuses
naming its ref — the move case. `clear_abstract` drops `meta.abstract`
+ the `card_abstract` chunk and rebuilds `card_combined`; blank
`abstract=''` refuses and points at it. Writes log `body_dropped`/
`body_moved`/`body_split`/`abstract_cleared` ref_events; chunk links
re-point on move/split, count on drop.

Each door's `dry_run` ends with an `undo:` line naming the export to
take first: `get(kind='paper', id='<pa>', view='ops')` dumps the ref's
full stored state — ref row, identifiers, tags, byline, bib entries,
inbound+outbound links (with chunk ords) and every chunk incl. cards —
as a fenced `precis-paper-dump/1` block, paged by
`args={'page': N, 'page_size': K}` (default 100, cap 500, 0 = the rest;
concatenate the slices' `chunks` arrays). The operator twin
`precis paper dump <id> --out <file>` writes every slice into one file.

## Undo a body door — restore a ref from its dump

```python
put(kind="paper", id="pa174214", args={"restore": <one dump slice>, "reason": "undo drop_body gr513878"})
```

The write half of `view='ops'`: send the dump's slices back **in window
order**, one `put` per slice (the block a `view='ops'` page or one
`precis paper dump` file yields — a merged file is one slice).
`reason` required, `dry_run` previews, one transaction per slice; each
lands a `paper_restore_slice` ref_event, the completing slice a
`paper_restored`.

**Target** (`args['target']`, default `'same'`): the dump's own
`ref_id`. Bodiless or card-only → rebuilt in place; retired → restored
and un-retired — but the un-retire lands only on the *completing*
slice, so a half-restored retired ref stays retired (a merge tombstone
refuses, naming the survivor); gone →
minted fresh under the dump's cite_key, as with `target='new'`, which
always mints — `args['handle']` picks the slug when the cite_key is
taken (the refusal's `next=` suggests a free one). A mint lands
*retired* too and un-retires at completion, so a half-restored copy
never reads as live. A minted copy
carries the dump's byline, tags and retraction state; its bib entries
are not carried (the ack lists them). A body ord that
*differs* from the dump refuses toward `drop_body` first; an intact
body missing only a card (`clear_abstract`'s shape) restores through —
chunks are INSERT-only, identical ords keep, cards swap DELETE+INSERT.

**Identity** is checked before the first write: a dumped identifier
owned by another *live* ref refuses with `next=`/`options` pointing at
the owner (merge it, or retire it first); on `target='new'` that same
identifier is instead skipped and listed in the ack — a copy cannot
hold the original's identifiers. A retired owner's row is
reclaimed; on `'same'` the dumped cite_key is skipped when the ref was
renamed after the dump (one live slug per ref);
`pdf_sha256`/`content_hash` land only while the `pdfs` row
exists. Between slices the frontier lives on the target's
`meta.restore`, so a resend is idempotent and an out-of-order slice
refuses naming the expected window. The last slice recomputes
`body_sha` over the stored rows and rewrites the link lists, remapping
chunk anchors through `(ref, ord)` — a link whose far end is retired or
missing is skipped and listed in the ack. Embeddings, keywords and
summaries stay NULL for the worker pass, as after ingest.

**Abort** an in-flight restore — abandoned, or the source was
re-exported under a new `body_sha` (that resend refuses naming this
door):

```python
put(kind="paper", id="pa174214", args={"restore_abort": True, "reason": "dump went stale"})
```

Clears `meta.restore` so a fresh slice 0 can start over *and* deletes
the chunk rows the run inserted — nothing half-restored stays behind.
Chunks that predate the restore stay, and a retired (or just-minted)
target stays retired. `dry_run` previews the counts.

CLI twin: `precis paper restore <file> [--apply] [--target new]
[--handle s]` — a merged `precis paper dump` file as one slice, dry
run unless `--apply`, events stamped `source='cli'`.

`get(id=…, view='pdf_link')` is the way a held PDF leaves the corpus:
it returns a signed `GET /files/pdf/<sha>?t=<token>` URL (sha, byte
size and page count, expiry, and the `curl` line), never the bytes.
The link is reusable until it expires — `args={'ttl_s': N}` (default
600 s, cap 3600 s). A ref with no PDF refuses and points at
`pdf_url`. When the reply's first line says `PRECIS_WEB_BASE_URL` is
unset the URL is path-only — prepend the precis_web origin yourself.

## A worker stamped a paper as done/failed — how do I re-run it?

```python
edit(kind="paper", id=[101, 102, 103], args={"clear_stamp": "context_sentence_no_abstract"})
```

Some worker passes stamp a converged/skip key into `refs.meta` and stop
re-claiming the paper; removing the stamp re-opens it. `id=` takes one
ref or a list; the ack reports, per key, refs cleared vs already clear,
each cleared ref getting a `metadata_edited` ref_event. `dry_run`
previews.

Only allow-listed retry stamps clear — `context_sentence_failed`,
`context_sentence_no_abstract`, `s2_enriched_at`, `s2_enrich_failed`,
`llm_band_failures`, `openalex` — anything else is refused naming the
allowed set. The registry is `precis/workers/retry_stamps.py`; there is
no generic `refs.meta` delete door, and `clear_stamp` combines with no
other edit field.

## I found a legal PDF for a body-less stub — how do I attach it?

```python
put(kind="paper", id="smith2023", args={"pdf_url": "https://host/paper.pdf"})
```

Queues a `paper_pull` job: a worker fetches the URL (SSRF-guarded, 50 MB cap,
`%PDF` required) and drops it in the ingest inbox with a sidecar naming this
stub (`source='agent_url'`, `url`), so the normal ingest attaches the body.
Refused up front for a non-paper and a paper that already has a body; a
bodiless paper holding a preview PDF is accepted. Outcome: `meta.paper_pull` (`dropped` / `failed` + `reason`). Use
it after `fetch_oa` failed and you know a legal open copy (repository, author
page, preprint); `fetch_oa` already covers OA via DOI. Shadow libraries
(Sci-Hub, LibGen, Z-Library, Anna's Archive and kin) are refused at put time
and again after redirects: `blocked_host`. Before anything is dropped the PDF
must name the stub (its DOI, or >= 80% of its title words in the first three
pages) and an article must exceed 2 pages: `identity_mismatch` means the URL
is another paper (check the title it quotes), `preview_suspected` means a
publisher preview (find the full text elsewhere); `identity_unchecked` on a
`dropped` record means the stub had no usable title or DOI, so check the
folded body yourself. The folded paper keeps the URL as `meta.source_url`.

## A datasheet/manual was ingested as a paper — how do I convert it?

```python
edit(kind="paper", id="pa1429", args={"convert_to": "datasheet", "reason": "it's a datasheet"})
edit(kind="paper", id="pa2494", args={"convert_to": "datasheet", "subtype": "reference-manual", "reason": "ref manual"})
edit(kind="paper", id="pa1003", args={"convert_to": "datasheet", "subtype": "app-note", "reason": "AN175"}, dry_run=True)
```

`refs.kind` flips; every ref-keyed row follows untouched — body chunks,
embeddings, links (incl. chunk-anchored), identifiers, the pdf pointer
and authors. Paper-only ref tags (categorizer axes + `DREAM:`) are
stripped; open tags and `SRC:`/`CACHE:` stay. `meta.subtype` (default
`'datasheet'`; `app-note` / `errata` / `reference-manual`) + a
`converted_from_kind` stamp are written; a `converted` ref_event logs.
Trust: a v0 row keeps its stamp; a v1 ref is restamped
`<caller origin>/hunch` by the trust guard (kind is semantic).
`dry_run` reports per-table counts, the stripped tags and the v1
restamp — writes nothing. Refuses
a retired/merged ref, an SI record, a paper with live SI children, and
links whose relation's endpoint-kind constraint excludes `'datasheet'`.
`pa<id>`/`pc<chunk>` keep resolving via the converted_from_kind
redirect — the record also answers `da<id>` and the same cite_key slug
under `get(kind='datasheet', id=…)`.

## See also

- [[precis-datasheet-help]] — read datasheets for PCB design
- [[precis-paper-help]] — find, read, cite papers
- [[precis-stubs-help]] — the acquisition backlog (papers we still need)
- [[precis-author-discovery-help]] — author-network discovery over the corpus
- [[precis-check-source-help]] — find a citation, read surrounds, judge support
- [[precis-doi-resolution]] — identifier lookup and resolution
