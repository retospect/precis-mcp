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

### Two payoffs

**(A) Author identity is itself a first-class goal** — not merely a means to
discovery. A queryable, disambiguated author node directly serves
**proposal writing** (the `proposal-writing` workspace, `precis-proposal-*`
skills): who are the subject-matter experts in a field, who should we cite,
who are the candidate collaborators / competing PIs, what is this person's
affiliation, body of work, and standing. An ORCID node is an **author
dossier** — bio + keywords + full publication list + affiliations (with ROR
ids) — that a proposal's *related work*, *team*, and *citation* sections
draw on directly. Disambiguation (two "J. Smith"s) and "everything by this
person" are first-class queries this enables.

**(B) Corpus growth.** Author identity also *drives* discovery. **One growth
lever is in scope here: completion** — an ORCID record is the author's full
DOI list, so diffing it against the corpus yields high-quality fetch
candidates (the same person's *other* work, almost always on-topic), which
we auto-enqueue (§4).

A second lever — **breadth-first traversal** of the author network
(paper → author → paper → co-author) — is **explicitly out of scope**. This
ADR only *enables* it by shipping the basic interfaces a future traversal
would stand on (the `orcid` resolve, `authored` links with author-position
meta, and the S2 author endpoint §5). The traversal strategy itself — frontier
scoring, which authors to expand (senior/last-author, h-index, affinity, …),
depth/budget control — is **not built here**; see *Non-goals* and the
sketched discovery skill.

Both completion and any later traversal terminate in the **existing**
stub→fetch pipeline:
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

- **Slug / handle**: `orcid:0000-0002-1825-0097` (the iD verbatim). The
  kind's **2-char universal-handle code is `oi`** (ADR 0036 / 0038 §7), so a
  resolved node also addresses as `oi<base62>` (e.g. `oi12312`) and `kind=`
  accepts `oi` as an alias for `orcid`.
- **Storage**: a durable `paper`-like ref — **not** a `CacheBackedHandler`,
  because the node is a *link hub* and cache eviction must never drop its
  edges. `meta` holds the structured record (names, `biography`, keywords,
  employments with ROR ids, work-count, `fetched_at`). A `card_combined`
  chunk (`ord=-1`, upserted) of `name + bio + keywords + affiliations`
  is **embedded**, so authors are semantically searchable ("the corpus's
  spintronics PIs"). Per the append-only rule, refresh DELETE+INSERTs the
  card; body rows are untouched (there are none).
- **Refresh**: **LLM-on-demand, not a background pass.** `meta.fetched_at`
  drives a staleness check in `get`; once past a soft TTL the rendered node
  carries a **hint** ("this record was resolved on <date> and may be stale —
  refresh with `get(kind='orcid', id=<iD>, refresh=true)`"). The model
  decides whether freshness matters for the task and triggers the re-pull
  itself (which re-resolves the person + re-runs the missing-DOI diff). No
  `refresh_orcid` worker, no automatic re-fetch — the cost is paid only when
  an agent actually needs current data.
- **Verbs**: `get` (resolve + store + return), `search` (two modes, below),
  `link` (authorship edges), `tag`. No `edit`/`delete` of the canonical
  record by agents (it mirrors an external source of truth); soft-delete
  only.

### 1a. Verb surface — the four questions

The two flavours of `search` are split by **parameter namespace** so they
never collide (and the LLM can't be ambiguous about which it wants):
structured people-finder params (`name`/`given`/`family`/`org`) hit the
**live ORCID registry**; `q=` runs **local semantic search** over the
embedded dossiers already in the corpus.

| Goal | Call | Returns |
|---|---|---|
| **Resolve a known iD** → materialize the dossier | `get(kind='orcid', id='0000-0002-1825-0097')` (or `get(id='oi…')` once stored) | the author node — names, **bio**, keywords, affiliations (ROR), and the works list (each marked *in-corpus* `pa…` vs *stub-pending*); plus the staleness hint |
| **Find a person by name + org** (disambiguation) | `search(kind='orcid', name='Jane Smith', org='MIT')` → live ORCID **expanded-search** (Lucene `q`, e.g. `family-name:Smith AND affiliation-org-name:MIT`) | ranked **candidates**: iD + display name + institutions. *Not stored* — the agent picks the right iD, then `get`s it |
| **Find authors already in the corpus** | `search(kind='orcid', q='spintronics PI')` (semantic over the embedded card) | stored author nodes ranked by relevance |
| **Get the bio** | it's a section of the `get` render (and *is* the embedded card body); sub-address `oi<id>#bio` later if needed | — |
| **Get to the stubs / papers** | the `get` render lists works with `pa…` handles + a *pending* marker; programmatically walk `link`s: `links_for(<orcid ref>, relation='authored')` | paper refs (real, ingested) + stub refs (auto-enqueued, fetching out-of-band) |

The flow ties together: `search(name=…, org=…)` → pick iD → `get(id=…)`
(resolves, embeds the bio, auto-enqueues missing-DOI stubs, draws `authored`
links) → read the works list / link-walk to the papers, which fill in as the
fetch pipeline lands them.

### 1b. Org disambiguation via ROR

`org='MIT'` is ambiguous two ways: it could mean several institutions, **and**
a bare free-text `affiliation-org-name:MIT` match misses every record that
spells the affiliation out ("Massachusetts Institute of Technology"). Names
are not identities. So when `org=` is supplied we **resolve it to a canonical
[ROR](https://ror.org) id first**:

1. Query the ROR API (`https://api.ror.org/organizations?query=MIT` — free,
   no auth) → ranked org candidates with canonical name + city + country.
2. If ambiguous, **surface the org candidates** for the agent/user to pick
   (the same disambiguation pattern as people — names in, identity out).
3. Constrain the ORCID expanded-search by `ror-org-id:<id>` (ORCID indexes
   ROR/GRID org ids on affiliations) — precise and spelling-independent,
   instead of fuzzy name matching.

Callers who already know the org pass `ror='05a0ya142'` directly and skip the
disambiguation hop. This is consistent end-to-end: the author node's stored
affiliations already carry the disambiguated-org **ROR id** (from ORCID
employments), so the same id that *finds* a person also labels their
affiliation and powers the later co-affiliation graph. ROR is a fixed host —
`safe_fetch` is belt-and-suspenders, used anyway per convention.

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

### 4. Auto-enqueue: walking the works list → links + stubs

On resolve/refresh, pull `/{id}/works` and for **each** work extract its
identifiers (DOI preferred, then arXiv / PMID), its title/year, and any
**ORCID-provided URL** (the work summary `url` and/or an external-id's
`url` — often a publisher landing or PDF link). Then, per work, one of three
branches:

1. **In corpus already** — `store.find_paper_ref_by_identifier(doi/arxiv)`
   returns a ref → just **`authored`-link** the existing paper. Done.
2. **Not in corpus, but has an identifier** — `store.upsert_stub_paper(
   identifiers=[("doi", …)], title, year, set_by="orcid")` (idempotent),
   `authored`-link the stub. It's now claimable by
   `fetch_oa.claim_stubs_to_fetch` and fetches **automatically** (no human
   gate — confirmed).
3. **No usable identifier at all** — still mint a stub from the title/year
   and **send it for searching**: a discovery step (S2 / Crossref title +
   author + year lookup, reusing the `acquire` path's enrich) tries to find
   the DOI; on success it becomes a normal case-2 stub, on failure it stays
   a stub addressable only by the ORCID URL (below). `authored`-link either
   way.

**Download-link passthrough (must be *used*, not just recorded).** When
ORCID hands us a work URL, we don't merely stash it for provenance — we
attach it as a **fetch hint** on the stub (`meta.fetch_hints = [{"source":
"orcid", "url": …}]`) and `fetch_oa` **tries it first** (SSRF-guarded via
`safe_fetch`) before the generic OA-source cascade. The point of pulling the
link from ORCID is wasted if the fetcher ignores it — so the contract is
"hint present ⇒ hint attempted, and the attempt is logged in `ref_events`
whether it hit or missed." Two concrete `fetch_oa` changes this requires
(today the fetcher is DOI/arXiv/S2-only — `claim_stubs_to_fetch` qualifies a
stub via `EXISTS ref_identifiers id_kind IN ('doi','arxiv','s2')` and the
cascade keys on those):

- **Claim**: widen the qualifying `EXISTS` so a stub **also** qualifies when
  it carries `meta.fetch_hints` — otherwise a URL-only stub (case 3 with no
  discovered identifier) is never picked up.
- **Cascade**: add a first-priority fetch source that reads `meta.fetch_hints`
  and `safe_get`s each URL before the OA providers run.

**Exponential backoff, as usual.** The hint source must log its attempt as a
`fetcher:orcid_url` (i.e. `fetcher:%`) `ref_event`, so it rides the
*existing* exponential-backoff window in `claim_stubs_to_fetch`
(`base * 2^(attempts-1)`, capped at `_RETRY_BACKOFF_MAX_HOURS`) — a dead
ORCID URL settles to one retry per ~30d instead of re-hammering the
publisher every pass, identical to the OA-source and chase backoffs. **No
bespoke backoff** — reuse the convention, and the widened claim `EXISTS`
(url-only stubs) inherits the same window for free since it keys on
`fetcher:%` `last_ts`.

**Blast-radius control.** A per-resolve cap
(`PRECIS_ORCID_MAX_STUBS_PER_RESOLVE`, default ~50) and the `set_by="orcid"`
tag bound and attribute the work — a prolific PI can carry 800 works, and
one resolve must not flood the fetch queue. Excess is logged, not silently
dropped (nursery rule).

### 5. Semantic Scholar author endpoint — network navigation

A basic navigation interface (so an LLM — or any future traversal — can hop
the graph). Extend the `semanticscholar` handler (`handlers/semanticscholar.py`,
currently `get`/`search` only) with two nav keys alongside the existing
`refs:` / `cites:`:

- `get(kind='semanticscholar', id='authors:<paper-id>')` — the paper's
  authors (S2 `/paper/{id}/authors`) with fields
  `name,externalIds,hIndex,affiliations` → surfaces each author's **ORCID**
  (the bridge into `kind='orcid'`) and the author order (position).
- `get(kind='semanticscholar', id='author:<authorId>')` — that author's
  top papers (S2 `/author/{id}/papers`).

This exposes the **paper → author → paper** and **co-author** hops S2-side
and returns the ORCID iDs that key §1 — the raw interface a traversal needs,
without prescribing the traversal itself.

## Skills

New `precis-orcid-help.md` (verb mechanics, slug form, auth note, the
link/stub model). A discovery-oriented skill is *sketched but out of scope*
(see *Non-goals*):

- **`precis-orcid-help`** — `get`/`search`/`link` on the kind; the
  `authored`/`authored-by` relations; how stubs auto-fetch; the refresh
  cadence; resolution paths; reading a node as an **author dossier** (bio,
  affiliations, publication list) for proposal *related-work* / *team* /
  *citation* sections — cross-ref `precis-proposal-*`.
- **`precis-author-discovery-help`** — *future, not part of this ADR's
  deliverable.* Sketched here only to show the interfaces are sufficient;
  the BFS recipe (a downstream effort) would read:
  1. From a high-value paper, `get(semanticscholar, 'authors:<id>')`; pick
     whom to expand first. The **senior (last) author** is often the lab
     PI / SME and a strong default, but it is **one heuristic among
     several** — also high h-index, shared affiliation, venue, recency.
  2. `get(kind='orcid', id=<their iD>)` → resolves + auto-enqueues their
     missing DOIs as fetching stubs.
  3. Score the frontier, expand BFS to depth 1–2, stop on a budget.
  This is a natural `LLM:*` planner coroutine: each tick resolves one
  author, the stubs fetch out-of-band, the next tick reads what landed.
- **Index updates**: add the `orcid` row to the master kinds table in
  `precis-overview` and `precis-help`; add an orcid resolve/search scenario
  to `precis-toolpath-help`.

## Non-goals (this ADR)

We ship **basic interfaces**, not strategies built on them:

- **Author-network BFS / traversal** — *enabled* (§5 + `authored` links +
  resolve), **not built**. No frontier scoring, no expansion policy, no
  planner coroutine. The sketched `precis-author-discovery-help` is
  illustrative only.
- **Affiliation / co-author graphs** — co-authorship is a 2-hop walk, not a
  materialized edge; affiliation clustering (the reframed "org chart") is
  later.
- **Crossref / S2-author bulk back-fill** of ORCIDs onto existing papers is a
  separate pass; this ADR's resolve path uses them opportunistically, not as
  a corpus-wide sweep.
- **ORCID write** (depositing into a researcher's record) — explicitly never.

## Consequences

- **Good**: authors become first-class, searchable, linkable nodes; "all
  work by X" is one walk; an author **dossier** feeds proposal writing
  (experts to cite, collaborators/competing PIs, affiliations, standing);
  the corpus self-extends along the strongest real-world signal (a
  researcher's own output); no new fetch machinery.
- **Cost / risk**: auto-enqueue can balloon the fetch queue (mitigated by
  the per-resolve cap + `set_by` attribution + nursery spin-loop guard);
  ORCID coverage is uneven (many older papers lack ORCIDs — Crossref
  back-fill is partial); name-based S2 resolution is fuzzy (treat as a
  candidate, confirm via the iD before linking).
- **Deferred**: affiliation graph & org-chart-as-clustering;
  materialized `coauthor` edges; funding/peer-review sections; ORCID
  *write* (depositing into a researcher's record — explicitly never).

## Open questions

- Soft-TTL value for the staleness hint (start ~30d; tune by how often
  agents actually hit "stale" and refresh).
- Where does the author-position metadata live — link `meta` at write time
  (chosen above) vs recomputed at read? Write-time wins unless author order
  proves unreliable across sources.
