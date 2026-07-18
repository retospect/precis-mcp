# 0039 — ORCID author kind & network discovery

- **Status**: accepted — implemented (2026-06-26; `orcid` kind live — `src/precis/handlers/orcid.py`)
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
   (the same disambiguation pattern as people — names in, identity out). The
   response is **explicitly typed** so the LLM knows which phase it got back:
   a `disambiguate: "org"` marker + the ROR candidate list (vs. the normal
   person-candidate payload). The agent picks a ROR id and re-calls with
   `ror=<id>`.
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
- **Link `meta` carries authorship position (best-effort)**:
  `{"author_position": 7, "n_authors": 7}` **when known**. Note: ORCID is
  *per-person* — a work record does **not** enumerate the paper's full author
  list or this person's position, so position meta is only available on the
  **S2-mediated path** (the `authors:<paper>` endpoint, §5), absent on an
  ORCID-only link. We do **not** store a derived `is_senior` flag — "senior /
  last author" is a judgement the **LLM makes** from `author_position` /
  `n_authors`; storing the raw position is enough (and cheap to fill when S2
  gives it).
- **Stubs link the same way.** When the missing-DOI diff mints a stub paper
  ref, we immediately `authored`-link the ORCID node to that **ref** (which
  has no chunks yet). When the fetch pipeline later promotes the stub
  (PDF + chunks land), the **ref id is stable**, so the edge survives — we
  link to the ref precisely so chunks can arrive afterward without re-wiring.
- **Co-authorship is a walk, not a stored edge** (v1): two ORCID nodes that
  both `authored` the same paper are co-authors via a 2-hop traversal
  (`orcid → paper → orcid`). Materializing a `coauthor` relation is a later
  optimization if the walk proves hot.

### 4. Walking the works list → links, then LLM-directed enqueue

On resolve/refresh, pull `/{id}/works` and for **each** work extract its
identifiers (DOI preferred, then arXiv / PMID), its title/year, and any
**ORCID-provided URL** (the work summary `url` and/or an external-id's
`url` — often a publisher landing or PDF link). Resolve every work against
the corpus locally (`store.find_paper_ref_by_identifier(doi/arxiv)` — one
cheap indexed lookup each) and **classify**:

- **In corpus** → **`authored`-link** the existing paper and render its
  `pa…` handle in the works list. (Linking is free and always happens.)
- **Missing, has an identifier** → a *candidate* — rendered with its DOI,
  title, year, and (if present) the ORCID URL. **Not fetched yet.**
- **Missing, no identifier** → just **shown** (title/year + ORCID URL if
  any). We do **not** auto-run title-search discovery on resolve (it would
  be ~N extra API calls and slow the resolve); the LLM can ask to discover a
  specific one if it wants it.

**Enqueueing is the LLM's call, not automatic.** The resolve render leads
with the counts — *"42 works: 9 in corpus, 31 missing-with-DOI, 2
no-identifier"* — and the affordance: *"enqueue up to 31 for fetch with
`get(kind='orcid', id=…, enqueue=N)` (or `enqueue='all'`), or list them to
look through with `…`."* The model decides **how many, if any** — this is
how we resolve the prolific-PI tension: no arbitrary hard cap, no silent
flood; the agent sees the size and chooses. A chosen enqueue mints stubs
(`store.upsert_stub_paper(…, set_by="orcid")`, idempotent), `authored`-links
them, attaches any ORCID URL as a fetch hint (below), and they fetch
out-of-band. No-identifier picks first run title-search discovery (S2 /
Crossref title+author+year, reusing the `acquire` enrich); discovered DOI →
normal stub, otherwise URL-only stub (below).

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
(`base * 2^(attempts-1)`, capped at `_RETRY_BACKOFF_MAX_HOURS`), identical to
the OA-source and chase backoffs. **No bespoke backoff** — reuse the
convention; the widened claim `EXISTS` (url-only stubs) inherits the same
window for free since it keys on `fetcher:%` `last_ts`.

**URL-only stubs give up after 3 days.** A normal identifier-bearing stub
never gives up (a closed paper can become OA later — existing behaviour). But
a **URL-only** stub (no DOI/arXiv/S2, only an ORCID fetch-hint) has nothing
else to try: if the hint URL hasn't yielded a PDF within **3 days**, mark it
terminal (`fetch:gave-up` tag, dropped from the claim set) instead of
retrying at the backoff floor forever. The `authored` link and the shown
metadata remain; only the fetch attempts stop.

**Attribution.** Every orcid-minted stub carries the `set_by="orcid"` tag so
the work is attributable and the nursery can see it. With enqueue now
LLM-gated (above) there's no hard cap to flood past.

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

### 6. ORCID API auth (client credentials)

The Public API requires a **read-public bearer token**, obtained once via
the OAuth **client-credentials** flow — not per-user, no user consent:

1. **Env**: `ORCID_CLIENT_ID` + `ORCID_CLIENT_SECRET` (read at handler
   `__init__`; missing ⇒ kind degrades to disabled with a WARN, never an
   `InitError` that blocks boot — the S2-key pattern).
2. **Exchange**: `POST https://orcid.org/oauth/token` with
   `grant_type=client_credentials&scope=/read-public` → `{access_token,
   expires_in}` (tokens are long-lived, ~20 years, but we don't assume it).
3. **Cache**: hold the token in process memory with its expiry; reuse across
   calls. No DB/secret-store row — it's re-derivable from the env secret.
4. **Refresh-on-401**: if a call returns 401/expired, re-run the exchange
   **once** and retry; a second 401 is a hard `Upstream` error.
5. **Calls**: `Authorization: Bearer <tok>`, `Accept: application/json`,
   against `https://pub.orcid.org/v3.0/…`, via `safe_get` (fixed host, but
   convention). The token-exchange host (`orcid.org`) is likewise fixed.

A small `_OrcidClient` (token cache + `_get(path)`) owns this; the handler
and the missing-DOI walk call through it. ROR (`api.ror.org`) and Crossref
need **no** auth — plain `safe_get`.

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
- **Cost / risk**: enqueue is LLM-gated (`enqueue=N`) so a resolve can't
  balloon the fetch queue on its own; `set_by='orcid'` attribution +
  nursery spin-loop guard bound the rest. ORCID coverage is uneven (many
  older papers lack ORCIDs — Crossref back-fill is partial); name-based S2
  resolution is fuzzy (treat as a candidate, confirm via the iD before
  linking).
- **Deferred**: affiliation graph & org-chart-as-clustering;
  materialized `coauthor` edges; funding/peer-review sections; ORCID
  *write* (depositing into a researcher's record — explicitly never).

## Open questions

- Soft-TTL value for the staleness hint (start ~30d; tune by how often
  agents actually hit "stale" and refresh).
- Backoff base + the 3-day give-up are the only new fetch tunables — confirm
  3 days is right once we see real URL-only-stub hit rates.

## Implementation status (v1 shipped)

This ADR is the design of record; the first implementation is a deliberate
subset.

**Shipped**

- `kind='orcid'` durable node — resolve / store / embed `card_combined` /
  semantic `search(q=…)` (`handlers/orcid.py`, `ingest/orcid.py`,
  `0039_orcid_kind.sql`); handle code **`oi`**.
- `authored` / `authored-by` ref→ref relations (`0040_orcid_relations.sql`,
  `store/types.py`); the `link` verb attaches them.
- **LLM-gated** works diff (§4): resolve links held papers + reports the
  missing counts; `get(..., args={'enqueue': N | 'all'})` mints stubs.
- **On-demand refresh** (no background pass): soft-TTL staleness hint,
  `args={'refresh': true}` re-pulls.
- S2 author endpoints (§5): `authors:<paper>` / `author:<id>`.
- Client-credentials auth, in-memory token cache + 401 re-mint (§6).
- Crossref `orcids_for_doi` helper (§2 resolution path).

**Deferred (designed here, not yet built)**

- **Live people-finder + ROR org disambiguation** (§1a/§1b): `search` is
  semantic-over-corpus only; the `name=/org=` expanded-search + `ror=` path
  are not implemented.
- **ORCID download-URL fetch passthrough** (§4): the work `url` is recorded
  on the `authored` link meta, but `fetch_oa` does not yet honour a per-stub
  `fetch_hints` URL, nor the widened claim / `fetcher:orcid_url` backoff /
  **3-day give-up** for URL-only stubs. `fetch_oa.py` is unchanged.
- **No-identifier title-search discovery** (§4): no-id works are counted /
  shown, not auto-discovered.

## Resolved (this round)

- **Enqueue is LLM-gated, not a hard cap** (§4): resolve reports the diff
  counts; the agent chooses how many, if any, to fetch.
- **Search param split** (§1a): structured `name`/`org` → live registry;
  `q` → corpus.
- **No `is_senior` stored** (§3): position meta is best-effort/S2-sourced;
  seniority is an LLM judgement.
- **ORCID auth** (§6): client-credentials, in-memory token cache,
  refresh-once-on-401.
- **URL-only stub give-up after 3 days** (§4); identifier stubs unchanged.
- **ROR two-phase response** is explicitly typed (`disambiguate: "org"`).
