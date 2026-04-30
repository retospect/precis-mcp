# `patent` — read-only EPO OPS kind

> Status: **draft spec**. Sized as 1 phase + an Ansible follow-up
> for the watch runner. Optional deps group `patent`. The kind
> registers itself only when **`EPO_OPS_CLIENT_KEY`** and
> **`EPO_OPS_CLIENT_SECRET`** are set in the server environment
> — same gating pattern as `markdown`/`voice`. When the keys are
> missing both the kind and the `precis-patent-help` /
> `precis-patent-power` skills are hidden from the agent index.

This spec matches the agent-facing surface in
`src/precis/data/skills/precis-patent-help.md` (entry-level) and
`src/precis/data/skills/precis-patent-power.md` (raw CQL
power-user surface) exactly. Read the skills first; this is the
implementation behind them.

Deferred filter affordances (date ranges as kwargs, extra search
state markers, family deduplication, cited / cited-by graph,
cross-kind search) are tracked in
`docs/search-future-filters.md`.

## Why

Agents on the cluster need patent search alongside paper search.
The two corpora answer different questions — *prior art and freedom
to operate* (patents) versus *peer-reviewed knowledge* (papers) —
and we want them in the same address space.

`patent` extends precis with a third durable knowledge corpus
(after `paper` and `markdown`). The implementation uses **EPO Open
Patent Services (OPS)** as the sole backend in v1:

- Free, no per-call cost (4 GB / week fair-use quota).
- DOCDB worldwide coverage: EP, GB, US, WO/PCT, CN, JP, KR, IN, +80
  more authorities accessible from one English-language API.
- Stable WIPO ST.36 XML schema for downstream parsing.
- A maintained Python client (`python-epo-ops-client`).

USPTO PAIR / bulk-XML are explicitly out of scope for v1 — see
"Alternatives considered" below.

## Mental model

Two layers, no agent-visible tiers:

| Layer | Postgres            | Created by                           | TTL    |
|-------|---------------------|--------------------------------------|--------|
| 1     | `cache_state`       | `search(...)` — OPS hit-list cache   | 7 days |
| 2     | `refs` + `blocks`   | `get(id=...)` — durable, embedded    | pinned |

`search` always returns layer 1 + layer 2 results merged by DOCDB
id. `get(id=...)` is the **only** way to materialise a layer-2 row;
on cache miss it fetches from OPS, parses ST.36 XML, embeds the
description + claims, and inserts. There is no separate "ingest"
verb — *getting a patent IS the ingest*.

This is intentionally simpler than the original three-tier draft
(transient hits / cached biblio / durable ref). Tier 2 (single-record
biblio cache) is folded into Tier 3 — the marginal cost of a full
ingest over a biblio fetch is trivial (one extra OPS call for
description + claims, plus local CPU/GPU embedding), and it removes
a confusing intermediate state. If an agent calls `get(id=...)` it's
expressing real interest; we may as well embed.

## Surface

The `Handler.search` signature is **the cross-kind contract** —
`q=`, `tags=`, `scope=`, `top_k=`, no patent-specific kwargs. This
is a deliberate reversal of the v1 draft (which proposed
`ti=`/`ab=`/`pd=`/etc. as separate kwargs); see *Alternatives
considered* below.

```python
# ── Search: merged local + remote, cached 7d on the remote leg ──────
search(kind='patent', q='photocatalytic NOx reduction')
search(kind='patent', q='photocatalytic NOx reduction', top_k=20)

# Tag filters lift to OPS CQL automatically — they apply to BOTH legs
search(kind='patent', q='photocatalysis',
       tags=['cpc:B01J27/24', 'country:ep'])
search(kind='patent', q='MOF synthesis',
       tags=['applicant:siemens-ag', 'kind:b1'])

# Power-user: raw OPS CQL via q= passes through (see precis-patent-power)
search(kind='patent',
       q='(ti=graphene OR ab=graphene) and pd within "2020 2025" and not pa=basf')

# ── Get: render (and persist on cache miss) ─────────────────────────
get(kind='patent', id='ep1234567b1')                  # overview
get(kind='patent', id='ep1234567b1', view='abstract')
get(kind='patent', id='ep1234567b1', view='claims')
get(kind='patent', id='ep1234567b1', view='description')
get(kind='patent', id='ep1234567b1', view='family')
get(kind='patent', id='ep1234567b1', view='legal')
get(kind='patent', id='ep1234567b1', view='cite/bib')

# Chunk navigation (paper-style)
get(kind='patent', id='ep1234567b1~5')
get(kind='patent', id='ep1234567b1~5..12')
get(kind='patent', id='ep1234567b1~5..12/toc')

# List local patents — by ingest time (cross-kind convention)
get(kind='patent')
get(kind='patent', id='/recent')

# Patent-specific list views
get(kind='patent', id='/published')   # by publication date, newest first
get(kind='patent', id='/expiring')    # granted patents nearing renewal/expiry (future)

# put: explicitly unsupported
put(kind='patent', ...)            # raises Unsupported
```

### Disambiguation rules

- `search(...)` → always merged local + remote (remote skipped if
  no OPS creds).
- `get(id=)`, ref exists → render local.
- `get(id=)`, ref missing → OPS fetch + ingest + render.
- `get(id='/recent')` or no `id=` → list local refs.
- `put(...)` → `Unsupported` with `next='use get(id=...) to fetch'`.

### Slug shape

Canonical slug = lowercased DOCDB id, no separators. Validation
regex: `^[a-z]{2}\d+[a-z]\d?$`. Country code must be a known ISO
patent authority (closed list in the handler).

Inputs are **whitespace-stripped and lowercased** before
validation:

| Input              | Result                              |
|--------------------|-------------------------------------|
| `ep1234567b1`      | `ep1234567b1`                       |
| `EP1234567B1`      | `ep1234567b1`                       |
| `EP 1234567 B1`    | `ep1234567b1`                       |
| `EP.1234567.B1`    | `BadInput` — next: `try ep1234567b1` |

That's the entirety of "normalisation" — strip whitespace,
lowercase, validate. **Dots are deliberately not stripped**: DOIs
and arXiv ids carry semantic dots, and a normaliser that silently
reshapes them would set a bad cross-kind precedent. The dotted
DOCDB form (`EP.1234567.B1`) is a third-party convenience EPO
doesn't actually emit — we let it raise `BadInput` with a
recovery hint.

### CQL assembly: `q=` + tag-lift

The handler builds one CQL string for the remote leg by combining
(a) `q=` (auto-promoted if bare keywords, passthrough if CQL) and
(b) the closed-prefix tags from `tags=` lifted to their CQL
equivalents.

```python
_TAG_TO_CQL: dict[str, str] = {
    "cpc:":       "cpc",     # cpc:b01j27/24 → cpc=B01J27/24
    "ipc:":       "ipc",     # ipc:h01m      → ipc=H01M
    "applicant:": "pa",      # applicant:hewlett-packard → pa="Hewlett-Packard"
                              # (looked up from meta.applicants — see below)
    "country:":   "pact",    # country:ep    → pact=EP
    "kind:":      "kind",    # kind:b1       → kind=B1
    "family:":    "famn",    # family:12345678 → famn=12345678
}


def build_cql(*, q: str | None, tags: list[str] | None,
              store: Store) -> str:
    parts: list[str] = []
    if q is not None and q.strip():
        parts.append(_promote_or_passthrough(q))
    for tag in tags or []:
        prefix, _, val = tag.partition(":")
        cql_field = _TAG_TO_CQL.get(prefix + ":")
        if cql_field is None:
            continue   # open-prefix tag (e.g. topic:) — local-only filter
        if prefix == "applicant":
            phrase = _resolve_applicant(val, store)
        elif prefix in ("country", "kind"):
            phrase = val.upper()      # ISO codes / kind codes are uppercase in CQL
        elif prefix in ("cpc", "ipc"):
            phrase = _cpc_to_canonical(val)   # b01j27/24 → B01J27/24
        else:
            phrase = val
        parts.append(f'{cql_field}="{_escape(phrase)}"')
    if not parts:
        raise BadInput("search requires q= or a CQL-liftable tag")
    return " and ".join(parts)


def _resolve_applicant(slug: str, store: Store) -> str:
    """Slugged applicant tag → canonical OPS phrase.

    Strategy:
      1. Find any local patent ref tagged with this applicant slug.
      2. Read meta.applicants[]; return the first canonical name
         whose own slugification matches.
      3. Fall back to naive unslug ('hewlett-packard' → 'Hewlett Packard')
         if nothing local has been ingested yet.

    Why: tag-storage is lossy on case and on space-vs-hyphen. The
    canonical name lives in meta.applicants from biblio parsing, so
    use it whenever available. The fallback covers cold-start —
    when no local patent for this applicant exists yet, the agent
    still gets a reasonable OPS query (and OPS phrase matching is
    forgiving on case + punctuation).
    """
    refs = store.find_refs_by_tag(
        kind="patent", tag=f"applicant:{slug}", limit=1,
    )
    if refs:
        for app in refs[0].meta.get("applicants", []):
            if _slugify_applicant(app["name"]) == slug:
                return app["name"]
    return slug.replace("-", " ").title()


def _promote_or_passthrough(q: str) -> str:
    """If q looks like CQL, pass through; else wrap in (ti OR ab)."""
    if any(op in q for op in (" and ", " or ", " not ", "=", " within ")):
        return f"({q})"
    safe = q.replace('"', '\\"')
    return f'(ti="{safe}" OR ab="{safe}")'
```

Key points:

- **Tags work on both legs.** The local SQL filter uses
  `tags=` directly via the unified `ref_tags` view (paper-style);
  the remote leg gets the lifted CQL appended.
- **Open-prefix tags (`topic:`, `project:`) only narrow the local
  hits.** They have no OPS equivalent; the handler silently skips
  them when building remote CQL.
- **Tag values are stored lowercased** (precis convention via
  `Tag.open()`). The CQL lift re-uppercases ISO country codes,
  kind codes, and CPC/IPC classes; for **applicants** the lift
  consults `meta.applicants` on a local patent so canonical
  spelling (`Hewlett-Packard`, `Siemens AG`, `三菱重工業`) survives
  the slug round-trip. Naive unslug is the cold-start fallback.
- **Cache key uses the assembled CQL**, so equivalent
  `q=`+`tags=` and raw-CQL forms collapse to the same cache row.

### Power-user CQL

The full OPS CQL grammar (`pd within "..."`, `not pa=...`,
`ct=...` for citation searches, kind-code wildcards, etc.) is
documented in the `precis-patent-power` skill, not here. The
handler does **no validation** beyond the auto-promote sniff —
OPS's parse error is forwarded as `BadInput` with the
canonicalised CQL it sent.

### Search merge

```
local_hits   = paper-style hybrid search over refs.kind='patent'
                (returns block hits → group by ref → take best block per ref)
remote_hits  = cache_state[provider='epo_ops_search', request_hash=h]
                or live OPS call on cache miss
merged       = interleave by relevance, dedupe by docdb_id
                — a docdb_id present in local marks the row [local]
                — local rows show the matching block excerpt
                — remote-only rows show the OPS abstract preview
```

Relevance interleaving: take all hits, score each by RRF rank from
its source list (local rank from the fused search, remote rank from
OPS hit position), interleave by combined score. Local hits get a
small constant bias (since the agent has already engaged with them
enough to ingest), but not enough to bury a fresh remote match.

`top_k` is the merged total returned. Default 20.

## DB layout

### Migration `0006_patent_kind.sql`

```sql
-- Register the patent kind
INSERT INTO kinds (slug, title, description, supports_get, supports_search,
                   supports_put, is_numeric, is_file)
VALUES ('patent',
        'Patent',
        'Read-only patent record from EPO OPS. DOCDB-id slugged.',
        TRUE, TRUE, FALSE, FALSE, FALSE);

-- Two new providers
INSERT INTO providers (slug, title, base_url, kind, attribution_template)
VALUES
    ('epo_ops',         'EPO Open Patent Services', 'https://ops.epo.org',
     'patent',  '_Source: EPO OPS — https://worldwide.espacenet.com/patent/search/family/{family_id}/publication/{docdb_id}_'),
    ('epo_ops_search',  'EPO OPS — search hits',    'https://ops.epo.org',
     'cache',   '_See Espacenet: https://worldwide.espacenet.com/patent/search?q={query}_');

-- Patent tags use OPEN lowercase prefixes — cpc:, ipc:, applicant:,
-- country:, kind:, family:, topic:.  No tag_prefixes registration
-- needed: that table is for closed UPPERCASE axes only (STATUS,
-- PRIO, SRC, CACHE, …). The values are stored in ref_tags via
-- Tag.open() and lower-cased on insert. Verified against the
-- real schema in 0001_initial.sql:94 and the Tag class in
-- store/types.py.
```

*(There is no `INSERT INTO tag_prefixes ...` for patents. The
v2-draft of this spec invented columns (`kind_slug`, `axis_kind`)
that don't exist on `tag_prefixes`; that block has been removed.)*

### Code-side change: `_KIND_ALLOWED_AXES`

Closed-axis whitelist for `patent` is added to
`store/types.py::_KIND_ALLOWED_AXES`:

```python
# Patent refs use SRC (e.g. SRC:primary for the patent we ingested
# direct, SRC:secondary for refs found via family-walk) and CACHE
# (cluster-wide cache discipline). STATUS doesn't apply — patents
# don't have a workflow lifecycle; the ingestion-pending bookkeeping
# lives on the associated `quest` row.
"patent": frozenset({"SRC", "CACHE"}),
```

With that addition, `tags=['STATUS:open']` on a patent raises
`BadInput` at the agent boundary — same per-kind axis discipline
the MCP critic enforced for paper / cache trio.

### Saved-watch table

Landed in phase 2 alongside the runner and CLI.

```sql
CREATE TABLE patent_watches (
    id           SERIAL PRIMARY KEY,
    name         TEXT UNIQUE,
    cql          TEXT NOT NULL,
    interval_s   INT NOT NULL DEFAULT 604800,
    last_run_at  TIMESTAMPTZ,
    last_seen_pn TEXT[],
    auto_get     BOOLEAN DEFAULT FALSE,
    created_at   TIMESTAMPTZ DEFAULT now(),
    created_by   TEXT
);

CREATE INDEX patent_watches_due_idx
    ON patent_watches (last_run_at NULLS FIRST, interval_s);
-- Picks watches due to run cheaply: NULL last_run_at sorts first
-- (never-run), older runs next.

-- Suggested vacuum: last_seen_pn arrays grow with each pass and
-- never shrink as long as the watch is active. Schedule
--   VACUUM (ANALYZE) patent_watches;
-- weekly via the cluster vacuum cron (already covers other tables).
```

*(See § "Build order" — watches are step 8–9, gated behind the
phase-1 ingest+search loop landing first.)*

Note: only **one** new cache provider (`epo_ops_search`) — the old
spec's `epo_ops_biblio` is gone because single-record fetches go
straight to layer 2.

### `refs` schema reuse

```
refs.kind        = 'patent'
refs.slug        = '<docdb-id-lowercased>'
refs.title       = patent title (English; falls back to original lang)
refs.provider    = 'epo_ops'
refs.meta        = {
   "docdb_id":          "EP1234567B1",
   "country":           "EP",
   "doc_number":        "1234567",
   "kind_code":         "B1",
   "publication_date":  "2024-03-15",
   "filing_date":       "2021-09-08",
   "priority_date":     "2020-09-09",
   "applicants":        [{"name": "...", "country": "DE"}, ...],
   "inventors":         [{"name": "...", "country": "..."}, ...],
   "abstract":          "...",
   "abstract_lang":     "en",
   "cpc_classes":       ["B01J27/24", ...],
   "ipc_classes":       ["H01M..."],
   "family_id":         "12345678",
   "family_members":    [{"docdb_id": "...", "country": "..."}, ...],
   "legal_status":      [{"date": "...", "code": "...", "text": "..."}, ...],
   "ops_etag":          "...",
   "ingested_at":       "2026-04-29T14:00:00Z",
   "fair_use_bytes":    41234
}
```

### `blocks` schema reuse

| `pos` range | Section                                              |
|-------------|------------------------------------------------------|
| 0..N1       | Description (one block per ST.36 `<p>` or per heading section, capped) |
| N1+1..N2    | Claims (one block per independent claim + dependent group) |

Density classification + content-hash slugs from `precis/ingest.py`
are reused. Embeddings via the active embedder.

### Raw XML cache on disk

Alongside the parsed-and-embedded state in Postgres, every OPS
response is persisted to disk under `$PRECIS_PATENT_RAW_ROOT`.
This mirrors how `voice` puts audio on NFS while keeping
transcripts in Postgres — *Postgres is for parsed/queryable
state; filesystem is for raw upstream artefacts*.

Layout (hierarchical, mirrors the OPS URL space — navigable by
hand for forensic re-parse):

```
$PRECIS_PATENT_RAW_ROOT/
└── ep/                              # country code (lowercase)
    └── 1234567/                     # doc number
        └── b1/                      # kind code (lowercase)
            ├── biblio.xml           # always fetched on ingest
            ├── description.xml      # always fetched on ingest
            ├── claims.xml           # always fetched on ingest
            ├── family.json          # only if view='family' was ever requested
            ├── legal.json           # only if view='legal' was ever requested
            └── ingest.log           # JSONL: timestamps + bytes-out per OPS call
```

Size is small — ~30 KB / patent average, ~200 KB tail. 5k patents
≈ 150 MB.

Why this exists:

- **Parser changes don't require re-fetch.** Schema migration or
  ST.36 parser improvements re-run from disk, no OPS bandwidth.
- **Postgres rebuild from disk.** If the precis DB is wiped,
  `precis jobs reingest-patent --from-disk` rebuilds refs +
  blocks from the cached XML.
- **Forensics.** When extraction looks weird, the original XML is
  right there next to the parsed output.

**This is not the `.acatome` format.** Acatome is paper-specific
(PDF + Marker output + DOI metadata + computed embeddings). For
patents we just keep raw OPS XML in its native form; no manifest
file, no precomputed embeddings on disk. Postgres holds the
derived state.

`$PRECIS_PATENT_RAW_ROOT` is **required** for the kind to
register (along with the OPS creds). On the cluster this is
shared NFS at `/opt/nfs/shared/patents/` so any node can re-parse
from disk.

### `cache_state` for search hits

```
provider     = 'epo_ops_search'
request_hash = sha256(canonical_cql)[:32]
body         = rendered remote-hit list markdown (just the remote leg;
                merge with local happens at request time)
meta         = {"cql": "...", "total": N,
                "hits": [{"docdb_id": ..., "title": ..., "applicants": [...],
                          "year": ..., "abstract_preview": "..."}, ...]}
ttl_seconds  = 604800   -- 7 days
```

Same `_cache_base.py` flow as `web` / `youtube` / `math`.

## Implementation

### Files

```
# Phase 1 — ingest + search + get + tags
src/precis/handlers/patent.py                  # ~500 LOC, hybrid handler
src/precis/handlers/_patent_xml.py             # WIPO ST.36 parser → ParsedPatent
src/precis/handlers/_patent_cql.py             # build_cql + tag→CQL lift (~80 LOC)
src/precis/handlers/_patent_merge.py           # local + remote merge, ranking
src/precis/patent_ingest.py                    # OPS fetch → refs+blocks pipeline
src/precis/migrations/0006_patent_kind.sql
src/precis/data/skills/precis-patent-help.md   # entry-level skill
src/precis/data/skills/precis-patent-power.md  # raw CQL power-user skill
tests/test_patent_handler.py
tests/test_patent_xml.py
tests/test_patent_cql.py
tests/test_patent_merge.py
tests/test_patent_ingest.py

# Phase 2 — saved CQL watches (landed)
src/precis/jobs/__init__.py
src/precis/jobs/patent_watch.py                # run_one_pass + fair-use accounting
src/precis/jobs/_patent_quest.py               # quest-body composer
src/precis/handlers/_patent_watch_db.py        # DAO over patent_watches
src/precis/migrations/0007_patent_watches.sql
tests/test_patent_watch_db.py
tests/test_patent_watch.py
tests/test_patent_watch_cli.py
```

### Optional dependency group

```toml
[project.optional-dependencies]
patent = [
    "python-epo-ops-client>=4.0",
    "lxml>=5.0",
]
all = ["precis-mcp[paper,docx,tex,calc,plot,external,patent]"]
```

Same gating pattern as `markdown`/`voice`: handler hidden via
try-import in `registry.py`, plus env-var gating
(`EPO_OPS_CLIENT_KEY`, `EPO_OPS_CLIENT_SECRET`) on
`KindSpec.requires_env`. The `Registry` constructor checks
`KindSpec.is_available()` (which ANDs every required env var) and
silently skips handlers whose env isn't satisfied.

The user-visible effect when keys are missing:

- `kind='patent'` raises `NotFound: unknown kind: patent`.
- The bundled skills (`precis-patent-help`,
  `precis-patent-power`) are absent from the synthesised
  `precis-help` index.
- Any locally-stored patents from a previous run remain in the DB
  but are unreachable until creds come back. (Acceptable for v1;
  if this becomes a foot-gun we can revisit by separating "local
  read" from "remote fetch" availability.)

The earlier draft of this spec had "kind still registered, only
first-time fetches raise Upstream"; that pattern is what
`perplexity` does (gating happens at fetch time, not registry
time). For `patent` we want the stronger guarantee — no creds, no
kind — because the skill is the agent's only entry point and
silent partial functionality is more confusing than a clean
"unknown kind".

### Backend abstraction

```python
class _OPSClient:
    """Lazy-loaded singleton over python-epo-ops-client."""
    def search(self, cql: str, range_: tuple[int, int]) -> bytes: ...
    def biblio(self, docdb_id: DocDbId) -> bytes: ...
    def description(self, docdb_id: DocDbId) -> bytes: ...
    def claims(self, docdb_id: DocDbId) -> bytes: ...
    def family(self, docdb_id: DocDbId) -> bytes: ...
    def legal(self, docdb_id: DocDbId) -> bytes: ...
```

Mockable via DI on the handler. Unit tests fake response bytes; one
integration test gated on `PRECIS_PATENT_TEST_LIVE=1` exercises real
OPS.

### Get-as-ingest pipeline

```
get(id='ep1234567b1')
   │
   ├── ref exists? ── yes ── render view(s); maybe re-fetch deeper view if not yet stored
   │
   └── no
       ├── OPS biblio + description + claims (parallel)
       ├── write each XML to $PRECIS_PATENT_RAW_ROOT/<cc>/<num>/<kc>/*.xml
       ├── parse_st36_xml(<cached_paths>) → ParsedPatent
       ├── store.insert_ref(kind='patent', slug=..., meta={...})
       ├── store.insert_blocks([description_blocks, claims_blocks])
       ├── fill_embeddings(blocks, embedder=active)
       ├── apply_auto_tags(['cpc:...', 'ipc:...', 'applicant:...',
       │                    'country:...', 'kind:...', 'family:...'])
       ├── append to ingest.log (timestamp, fair-use bytes)
       └── render the requested view from the freshly-stored ref
```

`view='family'` and `view='legal'` are pulled lazily on first
request and persisted to `family.json` / `legal.json` next to the
XML, then cached in `refs.meta` for subsequent calls.

Idempotency: re-`get`-ing the same id within 30 days is a no-op
refresh. After 30 days, the etag is rechecked; on change, replace
blocks (preserve `refs.id`, user-applied `topic:` tags, and `link`
rows). Block slugs are content-hash deterministic so unchanged
paragraphs survive replacement.

### Lazy view depth

To minimise OPS bandwidth, the first-time fetch pulls **biblio +
abstract + claims + description**. `view='family'` and
`view='legal'` are pulled lazily on first request and cached in
`refs.meta` thereafter. `view='images'` is out of scope.

If we later want to be miserly on bandwidth, the first fetch could
shrink to biblio + abstract, with `description`/`claims` pulled
on-demand. Tracked by a `meta.fetch_depth` field. Defer until usage
shows it matters.

### CLI commands

```
# Phase 1
precis jobs ingest-patent <docdb_id>           # alias for get(id=...) — useful from shell
precis jobs ingest-patent --from-search '<cql>' [--limit N] [--dry-run]
precis jobs sweep-patent-stale                  # re-fetch refs older than 90 days

# Phase 2 — watches (landed)
precis jobs watch-patents '<cql>' --name <slug> [--every 7d] [--auto-get] [--max-per-pass N]
precis jobs watch-patents --name <slug> --delete
precis jobs list-patent-watches [--show-cql]
precis jobs run-patent-watches [--name <slug>] [--dry-run] [--fair-use-limit-gb N]
```

`ingest-patent` is a CLI affordance — the agent mostly drives ingest
through `get(id=...)` from the MCP. Both end up in the same
pipeline.

### Background runner

The runner is invoked as a launchd / cron job on `balthazar`
(same host as `acatome-quest-mcp`):

```
# /Users/deploy/Library/LaunchAgents/com.precis.patent_watch.plist
# StartCalendarInterval: every hour, throttled by per-watch interval_s.
ExecStart = /opt/precis/venv/bin/precis jobs run-patent-watches
```

For each saved watch:

1. Run OPS search; diff result publication numbers against
   `last_seen_pn`.
2. For each new pn:
   - if `auto_get=TRUE` → call `get(id=pn)` (full ingest).
   - else → append to `quest:patents-pending-review` with the
     hit's title + abstract preview, leaving the agent to decide.
3. Update `last_seen_pn`, `last_run_at`.

Fair-use accounting: track bytes returned per OPS call in
`meta.fair_use_bytes` per ref + per cache row. If rolling 7-day
total exceeds `PRECIS_PATENT_FAIR_USE_LIMIT_GB` (default 3), pause
ingest and watches; log a warning. Pure cache hits keep flowing.

## Configuration summary

```bash
# Required for the kind to register
EPO_OPS_CLIENT_KEY=<your client key>
EPO_OPS_CLIENT_SECRET=<your client secret>
PRECIS_PATENT_RAW_ROOT=/opt/nfs/shared/patents   # raw OPS-XML cache on disk

# Optional
EPO_OPS_USER_AGENT="precis-mcp/6.0 (+https://github.com/retospect/precis-mcp)"
PRECIS_PATENT_FAIR_USE_LIMIT_GB=3           # warn-and-pause threshold
PRECIS_PATENT_DEFAULT_TOP_K=20              # search default
```

**Local development**: point `PRECIS_PATENT_RAW_ROOT` at
`~/.acatome/patents/` (or anywhere in your home dir) and run
precis2 directly against your dev DB. Patents ingested locally
stay there until you sweep them out.

**Cluster deployment**: shared NFS at `/opt/nfs/shared/patents/`
so any node can re-parse from disk. The directory needs to exist
with `deploy:deploy` ownership before the first ingest.

> **TODO (deploy)**: when patent ships, add an Ansible playbook
> for the shared mount + watch runner. Pattern is
> `@/Users/bots/Documents/openclaw-cluster/ansible/playbooks/26-quest.yml`
> (similarly-shaped MCP); NFS share already lives in
> `@/Users/bots/Documents/openclaw-cluster/ansible/playbooks/01-nfs.yml`
> and would gain one more bind mount. New playbook would slot in
> as `28-patent.yml` (after `27-extract-watch.yml`).

Cluster MCP config snippet:

```json
"precis2": {
  "command": ".../precis-mcp-new/.venv/bin/precis",
  "args": ["serve"],
  "env": {
    "EPO_OPS_CLIENT_KEY": "${vault_epo_ops_client_key}",
    "EPO_OPS_CLIENT_SECRET": "${vault_epo_ops_client_secret}",
    "PRECIS_PATENT_RAW_ROOT": "/opt/nfs/shared/patents"
  }
}
```

### OAuth2 token refresh

`python-epo-ops-client` handles token acquisition using the
client-credentials flow against
`https://ops.epo.org/3.2/auth/accesstoken`. The library caches the
bearer token in-process and refreshes automatically on 401 or on
token expiry (~20 minutes). **No persistent token state is
needed** — across MCP server restarts the lib re-acquires from
`EPO_OPS_CLIENT_KEY` + `EPO_OPS_CLIENT_SECRET` on first request.
This means we don't need a token file on disk and there's no
cross-process locking concern.

## Attribution

Per the precis legal-attribution rule, every successful response
carries an Espacenet footer:

- Search hit list: `_See Espacenet: https://worldwide.espacenet.com/patent/search?q=<encoded-cql>_`
- Single record: `_Source: EPO OPS — https://worldwide.espacenet.com/patent/search/family/<family_id>/publication/<docdb_id>_`
- Tier 2 ref overview: same source line in `view='biblio'` and the
  default overview footer.

EPO OPS terms allow free re-use with attribution, and require
clarity that the data is not the official European Patent Register.
The footer matches the language pattern `wolframalpha` and
`youtube` use today.

## Test coverage

### Phase 1

- **Slug normaliser** — every input in the table (`ep1234567b1`,
  `EP1234567B1`, `EP 1234567 B1`, `EP.1234567.B1` → `BadInput`).
- **CQL assembly** (`_patent_cql.py`):
  - Bare-keyword `q=` → `(ti="..." OR ab="...")` auto-promotion.
  - CQL-shaped `q=` (contains `=`/`and`/`or`/`not`/`within`) →
    passthrough wrapped in parens.
  - Tag-to-CQL lift for every prefix in `_TAG_TO_CQL` —
    `cpc:b01j27/24` → `cpc=B01J27/24`, `country:ep` → `pact=EP`,
    `kind:b1` → `kind=B1`, `family:12345678` → `famn=12345678`.
  - Applicant resolution: with local match
    (`meta.applicants[]` lookup) and cold-start fallback
    (`hewlett-packard` → `Hewlett Packard`).
  - Open-prefix tags (`topic:foo`) silently skipped from CQL.
  - Empty inputs (no `q=`, no liftable tags) → `BadInput`.
- **Handler unit tests** with mocked `_OPSClient`:
  - `search(q=...)` runs both legs, merges, marks `[local]`.
  - `search(q=..., tags=[...])` lifts tags, both legs see the
    filter, merge correct.
  - `search(scope='ep1234567b1')` restricts to one ref.
  - `get(id=)` first call: OPS fetch, XML written to disk,
    refs+blocks inserted, embeddings filled, auto-tags applied.
  - `get(id=)` second call: cache hit, zero OPS calls.
  - `get(id=...)` on missing patent → `NotFound`, no rows
    inserted, no XML on disk.
  - `get(id='/recent')` and `get(id='/published')` return
    correct sort order on a 5-patent fixture.
  - `put(...)` → `Unsupported` (no body mutation).
  - `put(id=..., tags=['STATUS:open'])` → `BadInput` from
    per-kind axis enforcement (verifies
    `_KIND_ALLOWED_AXES['patent']` is wired correctly).
  - `put(id=..., tags=['SRC:secondary'], link='other:cites')`
    → succeeds (paper-style narrow link/tag surface).
- **Env-gating** — handler missing from registry when any of
  `EPO_OPS_CLIENT_KEY` / `EPO_OPS_CLIENT_SECRET` /
  `PRECIS_PATENT_RAW_ROOT` is unset; `kind='patent'` raises
  `NotFound: unknown kind`.
- **WIPO ST.36 fixture parser** (`_patent_xml.py`) — parse three
  real EPO records into `ParsedPatent`; assert biblio,
  description block count, claims count, abstract text.
- **Search merge** (`_patent_merge.py`): local-only hit,
  remote-only hit, both with same DOCDB id (dedup with `[local]`),
  ranking interleave with one constant local-bias.
- **Raw-cache layout** — ingest writes `biblio.xml`,
  `description.xml`, `claims.xml` under
  `<root>/<cc>/<num>/<kc>/`. Re-parse from disk works without
  OPS.
- **Live integration test** gated on `PRECIS_PATENT_TEST_LIVE=1`
  — round-trip a known public EP patent (e.g. `ep1000000a1`).

### Phase 2 (landed)

- Watch runner: ephemeral postgres + `FakeOpsClient`; inject
  CQL → fake hit list → diff against `last_seen_pn` → assert
  quest entries
  (default) or `get(id=...)` calls (`--auto-get`).
- Watch CQL strictness: bare-keyword auto-promote allowed in
  ad-hoc `q=` but rejected at watch-create time via
  ``validate_strict_cql``. Watches run unattended for years; this
  prevents meaning-drift if auto-promote rules ever change.

## Build order

1. Slug normaliser + `DocDbId` dataclass + tests.
2. `_patent_cql.py`: `build_cql`, tag-to-CQL lift, auto-promote.
3. `_OPSClient` shim around `python-epo-ops-client` + fake.
4. `_patent_xml.py` ST.36 parser; fixture-driven tests.
5. `patent_ingest.py`: OPS fetch → ParsedPatent → refs+blocks pipeline.
   Share `classify_density` / `mint_block_slug` / `fill_embeddings`
   from `precis/ingest.py`.
6. `PatentHandler.get(id=)` — render local; on miss call ingest.
7. `_patent_merge.py` + `PatentHandler.search(...)` — remote leg
   cache-backed via `_cache_base.py`, local leg paper-style fused
   search, merge with `[local]` markers.
8. CLI: `ingest-patent`, `sweep-patent-stale`. (Watch CLI is
   phase 2.)
9. **—— phase 1 ships here ——**
10. *Phase 2 (landed)*: `patent_watches` migration
    (`0007_patent_watches.sql`), DAO
    (`_patent_watch_db.py`), runner
    (`src/precis/jobs/patent_watch.py`), quest composer
    (`src/precis/jobs/_patent_quest.py`), CLI
    (`watch-patents`/`list-patent-watches`/`run-patent-watches`).
    Ansible playbook for the launchd timer is the only deferred
    deploy artefact.
11. Skill cross-link from `precis-overview.md` and `precis-help`
    index regeneration.
12. Ansible playbook for shared NFS mount
    (`/opt/nfs/shared/patents/`) — see deployment TODO above.
13. Ansible launchd plist on balthazar (deploy step;
    runner code itself landed in phase 2).
14. End-to-end live test on cluster.

Estimated size: ~1 phase, comparable to `paper` plus the `voice`
ansible follow-up.

## Alternatives considered

### Three explicit tiers (transient hits / cached biblio / durable ref)

**Rejected** (was the v1 of this spec). The middle tier added a
confusing intermediate state — agents had to reason about whether a
patent was "biblio-cached" vs "fully ingested". Folding biblio cache
into `refs` keeps the cost low (one extra OPS call + local
embedding) and the model trivial: *getting a patent is the ingest*.

### Standalone `patent-mcp` package

**Rejected.** Same reasoning as `voice`: patents are first-class
searchable knowledge that should live in the same address space as
papers. A separate MCP forces awkward two-step flows. Optional deps
group solves the heavy-deps concern.

### USPTO PAIR / bulk XML as the primary backend

**Rejected for v1.** USPTO ODP gives richer US-only prosecution
detail (file-wrapper events, office actions, PTAB) — but the user
explicitly asked for "EU patent thing", and OPS already covers US
publications via DOCDB. Add `provider='uspto'` in v2 if
US-prosecution-detail use cases appear.

### Storing patent XML in Postgres

**Rejected.** OPS XML is verbose and we already extract every field
into `meta` JSONB + blocks. Re-fetching on rare cache miss is
cheaper. (Same tradeoff as `voice` not storing audio bytes.)

### Synchronous live search inside `get(id=)` if no ingest

**Considered, kept.** This is the design — `get(id=)` always
materialises a layer-2 row. The alternative (a transient response
that doesn't store) was the v1 design and we rejected it for
simplicity (see "three tiers" above).

### `search(q=...)` runs only local OR only remote, never both

**Rejected.** The user wants a single mental model: "did anyone
patent this?" The merged answer is the right level of abstraction;
the `[local]` marker handles the small subset of cases where the
agent needs to know which side.

### Structured CQL kwargs (`ti=`/`ab=`/`pa=`/`pd=` etc.) on `search`

**Rejected** (was the v2 of this spec). The proposed signature
`search(kind='patent', ti=..., ab=..., cpc=..., pd=..., ...)`
diverged from the cross-kind `Handler.search(*, q, scope, tags,
top_k)` contract — no other kind has specialised search kwargs,
and reinventing one for patents would have leaked CQL idioms
into a uniform surface. The current design is:

- agents use `q=` + `tags=` (closed-prefix, lifted to CQL on the
  remote leg) for everyday queries;
- power users put raw CQL in `q=` for everything richer (Boolean
  combinations, date windows, citation-graph filters);
- `precis-patent-power` documents the raw-CQL grammar.

Date-range filtering as a first-class kwarg becomes a cross-kind
protocol question (papers, patents, web, perplexity all have
dates in meta). Tracked in `docs/search-future-filters.md`.

### `put(kind='patent', mode='ingest')` from the MCP

**Considered, deferred.** `get(id=)` already does the ingest, so
`put` adds nothing in v1. Revisit only if explicit "ingest these
ten" semantics become useful from agent code.

## Open questions

1. **Local vs remote ranking bias** — current plan: small constant
   bias for `[local]` hits. Tune empirically once we have logs.
2. **Quota accounting precision** — fair-use is bytes-out from EPO,
   not request count. Plan tracks response sizes; good enough.
3. **Auto-get threshold for watches** — opt-in per watch. Probably
   never default. May want a budget cap (`--max-per-pass=10`).
4. **Family-aware deduplication** — should all `family_members` of
   an ingested patent be treated as the same ref? v1 keeps them
   separate (one ref per DOCDB id); a `linked-via=family` relation
   between refs covers the use case without conflating national
   variants.
5. **`view='full'` for raw ST.36 XML** — skip for v1; the raw XML
   already lives on disk under `$PRECIS_PATENT_RAW_ROOT` for
   forensic re-parse, so a `view='full'` would just be a
   convenience response. Defer.
6. **`/published` ordering tie-breaks** — confirmed:
   `ORDER BY (meta->>'publication_date')::date DESC NULLS LAST,
             slug ASC`. Stable, indexable on a small expression
   index over `(meta->>'publication_date')` if perf needs it.
7. **Image retrieval** — `view='images'` deferred. When
   implemented: fetch on-demand from
   `published-data/images` to
   `$PRECIS_PATENT_RAW_ROOT/<cc>/<num>/<kc>/images/fig-N.tiff`,
   return the path (and `image_url` if served via nginx). **Image
   bytes never touch Postgres** — same boundary as `voice` keeps
   audio on NFS. Hosted TIFF→PNG/WebP conversion for inline
   display is a separate downstream concern and is also deferred.

## Cross-references

- `src/precis/data/skills/precis-patent-help.md` — entry-level skill
- `src/precis/data/skills/precis-patent-power.md` — raw CQL power-user skill
- `src/precis/data/skills/precis-tags.md` — tag conventions used by tag-to-CQL lift
- `docs/search-future-filters.md` — deferred filter affordances (date ranges, state markers, family dedup, citation graph, cross-kind search)
- `docs/paper_ingest.md` — paper ingest pipeline (parallels Tier 2)
- `docs/voice-kind-spec.md` — pattern for optional-deps + env-gated kinds
- `pips/packages/acatome-quest-mcp/` — quest queue used by the
  watch runner when `auto_get=FALSE`
