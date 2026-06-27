---
id: precis-orcid-help
title: precis — ORCID author identity nodes
summary: Resolve a researcher's ORCID iD into a durable author node (dossier), link the works you hold, and LLM-gate fetching the rest
applies-to: get/search/tag/link (kind='orcid')
status: active
---

# precis-orcid-help — ORCID author identity nodes

`orcid` turns a researcher into a **first-class, durable node** in the
corpus, keyed on their [ORCID](https://orcid.org) iD. Two payoffs:

- **Author identity / dossier** — a disambiguated record (bio, keywords,
  affiliations with **ROR** ids, full publication list) you can read and
  cite when writing proposals (related-work / team / who-to-cite) and
  resolve "everything by this person" in one walk.
- **Corpus growth** — that publication list, diffed against what you
  hold, surfaces the missing DOIs as high-quality fetch candidates (the
  same person's other work, almost always on-topic).

Unlike `web` / `wikipedia` / `youtube`, an `orcid` node is **not**
cache-evicted — it is a link hub whose `authored` edges must persist.

## Resolve an author by iD

```python
get(kind='orcid', id='0000-0002-1825-0097')
get(kind='orcid', id='orcid:0000-0002-1825-0097')       # handle form ok
get(kind='orcid', id='https://orcid.org/0000-0002-1825-0097')  # URL ok
```

On resolve the handler:

1. pulls the ORCID Public API (`/person`, `/employments`, `/works`),
2. stores a durable ref (slug `orcid:<iD>`) whose `meta` holds names,
   biography, keywords, employments (with **ROR** ids), and `work_count`,
3. embeds a `card_combined` chunk (name + bio + keywords + affiliations)
   so the author is **semantically searchable**, and
4. runs the **works diff**: links the papers you already hold and
   **counts** the missing ones (see below).

A second `get` returns the stored record without re-hitting the API. If
it is older than the soft TTL (~30 days) the render carries a *may be
stale* hint; re-pull on demand with `args={'refresh': true}` (or
`mode='refresh'`). There is **no background refresh** — freshness is
your call.

## The works diff — LLM-gated enqueue

Resolving **links** every work you already hold (`authored` edge) and
**reports** how many are missing — but it does **not** auto-fetch. You
decide how many, if any, to pull:

```python
get(kind='orcid', id='0000-0002-1825-0097')                 # resolve + counts
get(kind='orcid', id='0000-0002-1825-0097', args={'enqueue': 10})  # mint 10 stubs
get(kind='orcid', id='0000-0002-1825-0097', args={'enqueue': 'all'})  # all of them
```

A render reports e.g. *"In corpus: 9 linked; missing: 31 with a
DOI/arXiv, 2 with no identifier"* plus the enqueue affordance. Each
stub you enqueue is idempotent (`set_by='orcid'`), `authored`-linked,
and auto-claimed by the `fetch_oa` worker, which grabs an open-access
PDF. No hard cap — `enqueue=N` is the control, so a prolific PI's 800
works never flood the queue unless you ask for them.

## Resolution paths (how you get an iD)

1. **Direct** — you already have the iD (above).
2. **Semantic Scholar** — `get(kind='semanticscholar',
   id='authors:<paper-id>')` lists a paper's authors, each row carrying
   their **ORCID** (and flagging the senior/last author). The bridge
   into this kind. See `precis-author-discovery-help`.
3. **Crossref** — a DOI's Crossref record lists author ORCIDs inline
   (the cheapest corpus-wide back-fill; no extra auth).

## The link model — authorship is ref → ref

`authored` / `authored-by` is a **document-level** edge (an author wrote
a *paper*, not a paragraph). Both endpoints are ref-level.

```python
# who authored this paper? (inverse auto-mirrors)
get(id='pa42', view='links')
# manually assert authorship
link(kind='orcid', id='0000-0002-1825-0097',
     target='paper:wang2020state', rel='authored')
```

Co-authorship is a **2-hop walk** (`orcid → paper → orcid`), not a
stored edge.

## Search authors

```python
search(kind='orcid', q='spintronics group leader')
search(kind='orcid', q='single-molecule magnetism PI')
```

Hybrid lexical + semantic over the embedded author cards — finds the
people in your corpus by topic, not just by name.

## Classify / cross-link

```python
tag(kind='orcid', id='0000-0002-1825-0097', add=['watchlist', 'topic:mofs'])
link(kind='orcid', id='0000-0002-1825-0097', target='memory:42')
```

The canonical record mirrors an external source of truth, so there is
no `edit` / `delete` of the bibliographic fields from the agent surface
— a fresh `args={'refresh': true}` re-syncs it; soft-delete only.

## Refresh — on demand, not scheduled

There is no background refresh pass. A stored node older than the soft
TTL (~30 days) renders with a *may be stale* hint; when freshness
matters for your task, re-pull with `args={'refresh': true}` — that
re-resolves the person and re-runs the works diff, so newly-published
work shows up as freshly-countable missing DOIs you can then enqueue.

## Auth

The ORCID Public API needs a read-public bearer obtained via the
client-credentials flow. Set **`ORCID_CLIENT_ID`** and
**`ORCID_CLIENT_SECRET`**; without them the kind is disabled (it never
blocks the rest of the surface).

## See also

```python
get(kind='skill', id='precis-author-discovery-help')   # the BFS recipe
get(kind='skill', id='precis-overview')                # verbs and kinds
get(kind='skill', id='precis-stubs-help')              # the stub → fetch pipeline
```
