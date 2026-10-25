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
| fold a duplicate pair | `args={'merge_into': '<survivor>', 'reason': …}` |
| re-point a merged stub | `args={'redirect_to': '<pa>', 'reason': …}` |
| retire a phantom stub | `args={'retire': True, 'reason': …}` (`retire: False` restores) |
| re-home a wrong body | `args={'drop_body'/'move_body_to'/'split_body'/'clear_abstract': …, 'reason': …}` |
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
DOI without touching who owns it elsewhere; `arxiv=''` clears likewise. A
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
edit(kind="paper", id="pa1108", args={"move_body_to": "pa42053", "reason": "it's 42053's"})
edit(kind="paper", id="pa41", args={"split_body": {"title": "Companion paper"}, "reason": "different work"})
edit(kind="paper", id="pa174214", args={"clear_abstract": True, "reason": "junk abstract"})
```

`reason` required, `dry_run` previews, one DELETE+INSERT transaction.
`drop_body` deletes body chunks + embeddings, clears `pdf_sha256` and
content ids, keeps metadata — a live stub, refetchable via `pdf_url`.
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

## See also

- [[precis-paper-help]] — find, read, cite papers
- [[precis-stubs-help]] — the acquisition backlog (papers we still need)
- [[precis-author-discovery-help]] — author-network discovery over the corpus
- [[precis-check-source-help]] — find a citation, read surrounds, judge support
- [[precis-doi-resolution]] — identifier lookup and resolution
