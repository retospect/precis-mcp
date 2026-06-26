# 0039 — ORCID author kind & network discovery

- **Status**: proposed (2026-06-26)
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0030 — job / finding / cron stay separate](./0030-job-finding-cron-stay-separate.md)
    (the stub-paper → fetch pipeline this kind feeds).
  - [ADR 0036 — Universal handles](./0036-universal-handles.md) (the
    `orcid:<id>` slug is the canonical handle for an author node).
  - [ADR 0029 — Multi-root corpus PDF](./0029-multi-root-corpus-pdf.md)
    (stubs land in the watch inbox the same way as any other acquired paper).

## Context

precis stores authors as **bare name strings** in `paper.meta["authors"]`
(`handlers/paper.py`). There is no first-class author node, so we cannot
ask "everything by this person", cannot disambiguate two "J. Smith"s, and
cannot use authorship as a graph edge for discovery.

[ORCID](https://orcid.org) gives us a durable, disambiguated identity for a
researcher plus their **complete** publication list. The Public API
(`https://pub.orcid.org/v3.0/`) exposes:

| Endpoint | Gives |
|---|---|
| `/{id}/person` | names, `biography`, keywords, researcher URLs, country, other-names |
| `/{id}/works` | work *summaries*; each carries `external-ids` (DOI / arXiv / PMID) and often a `url` |
| `/{id}/employments`, `/educations`, `/qualifications` | org name + **disambiguated-org id (ROR/GRID)** + role + dates |
| `/{id}/fundings`, `/peer-reviews` | grants, review activity |

What ORCID **does not** have: any reporting hierarchy or "direct reports".
There is **no org chart**. What we *can* synthesize later is an
*affiliation graph* (co-employment via the ROR id) and a *co-authorship
graph* (shared papers) — both derived, not a single API call. Out of scope
for v1.

Auth: the Public API is **not open** — it needs a read-public token via the
**client-credentials** flow (`ORCID_CLIENT_ID` + `ORCID_CLIENT_SECRET` →
one long-lived bearer). Same env-at-`__init__` pattern as the S2 key
(`handlers/semanticscholar.py:171`); missing creds ⇒ kind degrades to
disabled, never an `InitError` that blocks boot.

### The discovery thesis

Author identity is not the point — **corpus growth** is. Two levers:

1. **Completion.** An ORCID record is the author's full DOI list. Diff it
   against the corpus and the missing DOIs are high-quality fetch
   candidates — the same person's *other* work, almost always on-topic.
2. **Breadth-first traversal.** On a good paper, the **senior (last)
   author** is usually the lab PI / subject-matter expert, and their back
   catalogue is the densest vein of related key work. A BFS over
   paper → author → paper (and out to co-authors) surfaces strong
   candidates a citation-graph walk alone misses (it finds *unrelated-by-
   citation* work by the same group, and pre-prints not yet cited).

Both levers terminate in the **existing** stub→fetch pipeline:
`store.upsert_stub_paper(...)` mints a `paper` ref with a registered
identifier and no body; `workers/fetch_oa.py:claim_stubs_to_fetch` already
claims `kind='paper' AND pdf_sha256 IS NULL AND EXISTS(ref_identifiers …)`
and runs the OA fetcher cascade. We add **no new fetch machinery** — ORCID
is a new *source* of stubs.

## Decision

### 1. New `kind='orcid'` — a stored, refreshable author node

Named `orcid` for consistency with the other source-named live kinds
(`web`, `youtube`, `semanticscholar`), **not** `author` — the identifier
scheme *is* the kind, matching house convention. (If a non-ORCID identity
source is ever added, it becomes its own kind and links across, rather than
overloading this one.)

- **Slug / handle**: `orcid:0000-0002-1825-0097` (the iD verbatim; ADR 0036
  handle form).
- **Storage**: a durable `paper`-like ref — **not** a `CacheBackedHandler`,
  because the node is a *link hub* and cache eviction must never drop its
  edges. `meta` holds the structured record (names, `biography`, keywords,
  employments with ROR ids, work-count, `fetched_at`). A `card_combined`
  chunk (`ord=-1`, upserted) of `name + bio + keywords + affiliations`
  is **embedded**, so authors are semantically searchable ("the corpus's
  spintronics PIs"). Per the append-only rule, refresh DELETE+INSERTs the
  card; body rows are untouched (there are none).
- **Refresh**: a system-worker pass (`refresh_orcid`, TTL ~30d) re-pulls
  and re-enqueues any newly-missing DOIs. Reuses the `CacheBackedHandler`
  TTL idea without the cache *storage* model.
- **Verbs**: `get` (resolve + store + return), `search` (semantic over the
  embedded card), `link` (authorship edges), `tag`. No `edit`/`delete` of
  the canonical record by agents (it mirrors an external source of truth);
  soft-delete only.

### 2. Resolution paths (in order of trust)

1. **Direct** — caller supplies the iD. `get(kind='orcid', id='0000-…')`.
2. **Crossref** — a DOI's Crossref record lists each author's ORCID inline.
   Cheapest corpus-wide enricher; needs no extra auth; back-fills ORCIDs
   onto papers we already hold.
3. **S2 author endpoint** (see §4) — name/paper → S2 authorId →
   `externalIds.ORCID`. Fuzzier (name disambiguation); fallback only.

### 3. The link model — *what links to what*

Answering the open question directly: **authorship is a document-level
edge, ref → ref, never to chunks.** A person authors a *paper*, not a
paragraph.

- New `Relation` pair in `store/types.py`: **`authored`** /
  **`authored-by`** (inverse-mapped in `_INVERSE_RELATIONS`; seeded in the
  relations migration). `add_link(src_ref_id=<orcid ref>,
  dst_ref_id=<paper ref>, relation="authored", src_pos=None, dst_pos=None)`
  — ref-level (both `*_pos` None).
- **Link `meta` carries authorship position**: `{"author_position": 7,
  "n_authors": 7, "is_senior": true, "is_corresponding": false}`. This is
  what makes the senior-author heuristic a cheap edge-filter rather than a
  re-fetch.
- **Stubs link the same way.** When the missing-DOI diff mints a stub paper
  ref, we immediately `authored`-link the ORCID node to that **ref** (which
  has no chunks yet). When the fetch pipeline later promotes the stub
  (PDF + chunks land), the **ref id is stable**, so the edge survives — we
  link to the ref precisely so chunks can arrive afterward without re-wiring.
- **Co-authorship is a walk, not a stored edge** (v1): two ORCID nodes that
  both `authored` the same paper are co-authors via a 2-hop traversal
  (`orcid → paper → orcid`). Materializing a `coauthor` relation is a later
  optimization if the walk proves hot.

### 4. Auto-enqueue: the missing-DOI diff → stubs

On resolve/refresh:

1. Pull `/{id}/works`; collect every work's DOI (prefer DOI; fall back to
   arXiv; ignore works with no usable identifier). ORCID work summaries
   also carry a `url` — recorded in stub `meta` for provenance, but the
   **identifier** (DOI) is what the fetcher keys on, not the URL.
2. For each, `store.find_paper_ref_by_identifier(doi)`:
   - **hit** → ensure an `authored` link to the existing paper ref.
   - **miss** → `store.upsert_stub_paper(identifiers=[("doi", …)],
     title=…, year=…, set_by="orcid")` (idempotent), then `authored`-link.
     The stub is now visible to `fetch_oa.claim_stubs_to_fetch` and fetches
     **automatically** — this is the auto-enqueue (decision confirmed: do
     not gate on human approval).
3. A per-resolve cap (`PRECIS_ORCID_MAX_STUBS_PER_RESOLVE`, default ~50)
   and the `set_by="orcid"` tag bound and attribute the blast radius — a
   prolific PI can carry 800 works, and we don't want one resolve to flood
   the fetch queue. Excess is logged, not silently dropped (nursery rule).

### 5. Semantic Scholar author endpoint — network navigation

So an LLM (and the BFS) can hop the graph, extend the `semanticscholar`
handler (`handlers/semanticscholar.py`, currently `get`/`search` only) with
two nav keys alongside the existing `refs:` / `cites:`:

- `get(kind='semanticscholar', id='authors:<paper-id>')` — the paper's
  authors (S2 `/paper/{id}/authors`) with fields
  `name,externalIds,hIndex,affiliations` → surfaces each author's **ORCID**
  (the bridge into `kind='orcid'`) and flags the senior author by position.
- `get(kind='semanticscholar', id='author:<authorId>')` — that author's
  top papers (S2 `/author/{id}/papers`) → the outbound frontier for BFS.

This closes the **paper → author → paper** and **co-author** loops S2-side,
and the ORCID iDs it returns are exactly the keys for §1.

## Skills

New `precis-orcid-help.md` (verb mechanics, slug form, auth note, the
link/stub model) **plus** a discovery-oriented skill — author-network BFS
is a *strategy*, not just an API, and belongs in the toolpath catalogue:

- **`precis-orcid-help`** — `get`/`search`/`link` on the kind; the
  `authored`/`authored-by` relations; how stubs auto-fetch; the refresh
  cadence; resolution paths.
- **`precis-author-discovery-help`** (or a section in
  `precis-decomposition-help`) — the BFS recipe:
  1. From a high-value paper, `get(semanticscholar, 'authors:<id>')`; take
     the **senior (last) author** first, then co-authors.
  2. `get(kind='orcid', id=<their iD>)` → resolves + auto-enqueues their
     missing DOIs as fetching stubs.
  3. Score the frontier (shared affiliation / shared venue / recency),
     expand BFS to depth 1–2, stop on a budget.
  This is a natural `LLM:*` planner coroutine: each tick resolves one
  author, the stubs fetch out-of-band, the next tick reads what landed.
- **Index updates**: add the `orcid` row to the master kinds table in
  `precis-overview` and `precis-help`; add an author-discovery scenario to
  `precis-toolpath-help`.

## Consequences

- **Good**: authors become first-class, searchable, linkable nodes; "all
  work by X" is one walk; the corpus self-extends along the strongest
  real-world signal (a researcher's own output); no new fetch machinery.
- **Cost / risk**: auto-enqueue can balloon the fetch queue (mitigated by
  the per-resolve cap + `set_by` attribution + nursery spin-loop guard);
  ORCID coverage is uneven (many older papers lack ORCIDs — Crossref
  back-fill is partial); name-based S2 resolution is fuzzy (treat as a
  candidate, confirm via the iD before linking).
- **Deferred**: affiliation graph & org-chart-as-clustering;
  materialized `coauthor` edges; funding/peer-review sections; ORCID
  *write* (depositing into a researcher's record — explicitly never).

## Open questions

- Should `refresh_orcid` only re-enqueue, or also re-pull bio/affiliations
  (drift is slow; lean re-enqueue-only with a longer full-refresh TTL)?
- Where does the senior-author heuristic live — link `meta` at write time
  (chosen above) vs recomputed at read? Write-time wins unless author order
  proves unreliable across sources.
- BFS as a hand-run skill first, promoted to a planner coroutine once the
  scoring heuristic is validated.
