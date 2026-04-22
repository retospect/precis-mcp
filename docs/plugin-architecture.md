# Precis plugin architecture — v2 design

**Status:** draft, not yet implemented
**Audience:** precis-mcp contributors; agent-infra maintainers
**Scope:** refactor the handler/plugin model to support (a) external read-only services (web, math, research, etc.), (b) capability-named kinds, (c) per-agent masking with per-kind verb restriction, (d) cost reporting, (e) exception isolation. Caching, active health probes, budget guards, and dynamic schema reload are deferred — see §19 Future enhancements.

---

## 1. Current state

Precis already has a plugin layer:

- `Plugin` dataclass in `src/precis/protocol.py` — declares `name`, `handler_cls`, `schemes`, `file_types`, `corpus_id`, `write_policy`, seed metadata.
- `_discover()` in `src/precis/registry.py` — loads builtins, then entry points from group `precis.plugins`, then legacy groups.
- `PRECIS_PLUGINS` (allowlist) and `PRECIS_DISABLE_PLUGINS` (denylist) env vars for masking.
- `Handler` base class in `src/precis/protocol.py` — already carries a `views: set[str]` slot (underused).
- Dispatch: `server.py::_to_uri()` maps bare ids → `paper:` default, extension-based → `file:`, known scheme prefixes kept as-is.

Gaps driving this refactor:

1. **No capability-level metadata.** `schemes` is an implementation detail. The LLM-facing enum (`kind`) must come from the plugin, not be implied by scheme.
2. **No cost reporting.** External services (perplexity, wolfram) need a way to report `$/call`.
3. **No runtime isolation.** A plugin exception currently propagates to the tool response as an unstructured crash.
4. **Per-kind verb masking not possible.** Current `PRECIS_PLUGINS` is all-or-nothing per plugin; can't restrict a kind to read-only.
5. **Wrong built-in tier.** Storage-backed handlers (paper/todo/flashcard) are built-in but crash at import time when the store isn't available. External API handlers (web/math/wiki) aren't built in. The tiers should be reworked: stateless external-API handlers available unconditionally; storage handlers auto-hide when PG is unreachable rather than crashing at import time (§6.2).

---

## 2. Goals

- **Capability naming.** The agent-facing enum is `{paper, memory, web, math, wiki, research, …}`, not vendor names or scheme strings.
- **Minimal declaration.** Plugins declare the kind name, a one-line description, and required env keys. The standard verbs, params, views, and put modes (see §4) are universal. Extras are discovered via errors or hints, not declared upfront.
- **Discovery via behaviour.** Non-availability of standard things → surfaced in error messages. Availability of extras (handler-specific filters, views, modes) → surfaced in result hints when they'd help.
- **Per-agent masking.** `PRECIS_KINDS` with bracket syntax (`paper,memory,doc[get,search]`) narrows what each server exposes, per-kind verbs included.
- **Cost visibility.** Handlers that cost money report the cost per call in a response footer. Free kinds show $0 for consistent presence.
- **Exception isolation.** A handler crash must not take down the server or affect other kinds.
- **Monolithic install.** `pip install precis-mcp` ships every kind precis owns. No capability-gating extras. Kind exposure per agent is controlled at runtime by `PRECIS_KINDS` (§13), not at install time.
- **Two kind tiers by infrastructure requirement:**
  - **Stateless kinds** — `wiki`, `url`, `youtube`, `code`, `math`, `web`, `think`, `research`. Pure API calls or local-file parsing. Present whenever required env vars are set. Zero PG.
  - **State-backed kinds** — `paper`, `memory`, `todo`, `flashcard`, `conversation`, `news`, `log`, `gripe`, `link`. All require Postgres reachable at startup; auto-hidden from the tool enum if PG is unreachable, with a stderr warning listing what's disabled.
- **One DB, many schemas.** All state-backed kinds write to the same `cluster` database, each in its own schema (`acatome.*`, `journal.*`, `news.*`) plus top-level tables (`logs`, `links`, `gripes`). One backup, one PgBouncer, one admin surface. Per-schema roles control access.
- **Stateless kinds always work without PG.** Laptops, CI, first-boot dev environments all get `url`, `wiki`, `web`, `think`, `research`, `youtube`, `code`, `math` out of the box. State-backed kinds auto-hide; no code crash, no broken imports.
- **Non-breaking.** Existing handlers (word, tex, markdown, plaintext, paper, todo, flashcard) keep working. New capabilities are additive.

### 2.1 Vocabulary: `type` (agent-facing) vs `kind` (internal)

One concept, two names, used consistently by audience:

- **Agents see `type=`.** All MCP tool params, error messages, hints, and examples use `type='paper'`, `type='memory'`, etc. This reads as a question: *what type of thing are you looking for?*
- **Python code uses `kind`.** Internal class names (`KindSpec`), dicts (`KINDS`), attributes (`.kind`), and variables all use `kind` because `type` shadows a Python builtin. The tool-schema layer translates between them once.
- **Prose in this doc** uses whichever term is natural — usually "kind" when describing the abstract concept, "`type=`" when referencing the tool param literally.

Nothing changes semantically; it's just vocabulary hygiene on the boundary.

---

## 3. Plugin contract v2

### 3.1 `KindSpec` (slim)

```python
@dataclass
class KindSpec:
    name: str                                  # canonical enum value: "web", "paper", ...
    description: str                           # one-liner for the tool-schema enum docs
    aliases: list[str] = field(default_factory=list)    # e.g. ["perplexity"] → web
    requires: list[str] = field(default_factory=list)   # env vars that must exist to enable
    cost_hint: str | None = None               # freeform: "~$0.002/call" | "free" | None
    examples: list[str] = field(default_factory=list)   # reserved for single-kind specialization
```

**Not declared:** `verbs`, `views`, `modes`, `params`. Those live in the standard surface (§4) plus handler extras surfaced via errors and hints (§5).

### 3.2 `Plugin`

```python
@dataclass
class Plugin:
    # identity
    name: str
    handler_cls: type[Handler]

    # existing storage / URI routing (unchanged)
    schemes: list[str] = field(default_factory=list)
    file_types: list[str] = field(default_factory=list)
    corpus_id: str | None = None
    write_policy: str = "ingestion"
    block_type_seeds: list[tuple] = field(default_factory=list)
    link_type_seeds: list[tuple] = field(default_factory=list)

    # NEW: capability declaration
    kinds: list[KindSpec] = field(default_factory=list)
```

Plugins without `kinds` still load — the registry synthesises a default `KindSpec` per scheme using `description=f"{scheme} store"`.

### 3.3 `Handler`

```python
class Handler(abc.ABC):
    scheme: str = ""
    writable: bool = False

    @abc.abstractmethod
    def read(self, path, selector, view, subview, query, summarize, depth, page) -> str: ...
    def put(self, path, selector, text, mode, **kw) -> str: ...

    # NEW (optional hooks)
    def cost_of(self, ctx: CallContext) -> str | None:
        """Return a cost_hint string for the just-completed call, or None."""
        return None

    def hints(self, result, ctx: HintContext) -> list[str]:
        """Contextual suggestions appended to successful responses. See §5.3."""
        return []

    def notifications(self, ctx: NotificationContext) -> list[str]:
        """Boot-time 'current business' lines for this kind. See §5.5.
        Called once at tool-description build. Return [] when nothing to report.
        Typical for state-backed kinds (todo/flashcard/gripe); empty for stateless."""
        return []
```

No `probe()`. No structured `CostModel` class hierarchy. Strings and optional hooks.

---

## 4. Standard surface

Every handler is expected to know about the standard verbs, params, views, and put modes. It either implements them or returns a clean, explanatory error. Handlers may support **more** (extra params, views, modes); those are revealed via hints (§5.3) only when they would help.

### 4.1 Standard verbs

`search`, `get`, `put`, `move`. Any of these may be declared "not supported" by a handler — the error message is how the agent learns.

### 4.2 Standard params per verb

| Verb | Params |
|---|---|
| `search` | `kind`, `query`, `grep`, `top_k`, `depth` |
| `get` | `kind`, `id`, `depth`, `page` |
| `put` | `kind`, `id`, `text`, `mode`, `selector` |
| `move` | `kind`, `id`, `after` |

### 4.3 Standard put modes

`append`, `replace`, `delete`, `note`. Handlers may define more (e.g. `state`, `review`, `comment`); those show up via hints when appropriate.

### 4.4 Standard views

Addressed as URI suffix on `get`: `/summary`, `/toc`, `/meta`, `/stats`. Handlers may offer many more (see §8 for the full typical set).

### 4.5 Standard response footer

Every successful call appends:

```
---
source: <kind> (<backend>)
cost: <cost_hint>
```

Missing fields rendered as `—` or omitted. Always present.

---

## 5. Discovery channels

Three channels. Principle: cheap schema upfront, progressive revelation in context.

### 5.1 Declared (upfront)

- Kind names and descriptions (from `KindSpec`)
- Standard verbs, params, views, modes (§4)
- Aliases (resolved at URI parse; hidden from enum)

### 5.2 Errors (reveal non-availability of standard things)

Shape: what the agent tried, what's not there, what to do instead.

```
ERROR: type='wiki' does not support put.
  Supported verbs: search, get.
  To save wiki content, store it with type='memory' (mode='append').
```

```
ERROR: type='paper' view '/histogram' is not known.
  Known views: /summary, /toc, /meta, /abstract, /cite/bib, /fig, /links, /links-in.
```

Every error carries enough context for the agent to pick the next move without a second probing call.

### 5.3 Hints (reveal availability of handler extras)

Hints surface **only when they'd help**, driven by result shape:

| Result state | Hint behaviour |
|---|---|
| **Zero results** | Relaxation: broader grep, drop filters, try alternative kinds |
| **Sweet spot** (1–30) | No hints. Clean output. |
| **Too many** (>~50) | Pruning: extra filters this handler supports, `top_k=`, grep dialect |
| **Partial success** | What failed and why; alternatives for the missing pieces |

Example — too many results:

```
<162 todos listed, truncated to 20>

Hints: 162 total. Narrow with:
  grep='state:in_progress'    — active work only
  grep='priority:high'         — urgent
  grep='due:today'             — scoped by time
  get(id='/stats')             — breakdown
```

Example — zero results:

```
No web results for 'sky blue at low pressure'.

Hints:
  - try broader query: 'why is the sky blue'
  - try type='research' for deep synthesis
  - try type='wiki' for canonical explanations
```

Clean success gets **no hints**. This keeps the common case quiet.

### 5.4 Implementation

Hints live in `Handler.hints(result, ctx)`. The `invoke_handler()` wrapper computes `ctx.result_count`, calls `hints()`, appends the list to the response. Default implementation empty.

### 5.5 Boot-time notifications

Some state-backed kinds carry "current business" the agent should know about at session start — today's todos, due flashcards, unresolved gripes. These surface **once at session start** as a dedicated block in the tool description, silent when there's nothing to report.

**Mechanism.** Each handler optionally implements:

```python
class Handler(abc.ABC):
    ...
    def notifications(self, ctx: NotificationContext) -> list[str]:
        """Return 0-or-more notification lines for session start.

        Called once at tool-description build. Each line should be:
          '<count> <thing>' + ' → ' + '<call-to-fetch>'

        Return [] when there's nothing to report. Default: empty.
        """
        return []
```

**Examples.**

- `TodoHandler.notifications()` →
  ```
  ["20 todos due today → get(type='todo', id='/today')",
   "3 overdue todos → get(type='todo', id='/overdue')"]
  ```
- `FlashcardHandler.notifications()` →
  ```
  ["9 flashcards due → get(type='flashcard', id='/due')"]
  ```
- `GripeHandler.notifications()` (admin agents only) →
  ```
  ["2 unresolved gripes from the last 24h → get(type='gripe', id='/recent')"]
  ```
- `WebHandler.notifications()` → `[]` (stateless; nothing to report).
- `UrlHandler.notifications()` → `[]`.

**Aggregation.** At tool-description build, precis calls `notifications()` on every registered handler, concatenates results, and if the combined list is non-empty prepends a block to the tool description:

```
Notifications:
  - 20 todos due today → get(type='todo', id='/today')
  - 3 overdue todos → get(type='todo', id='/overdue')
  - 9 flashcards due → get(type='flashcard', id='/due')
```

If every handler returns `[]` (typical on a fresh system), the Notifications block is **absent entirely** — no blank heading, no "no notifications", nothing.

**Caps.**

- Per handler: max 3 lines. Handlers with more should compress ("8 categories of overdue todos → …").
- Overall: max 10 lines. Ranked by `level` field if present (see below), otherwise input order.

**Cost.**

- Cheap: one PG query per state-backed kind at session start. Indexed lookups, not scans.
- Cached for the session; no per-call recomputation. Agents wanting fresh status query the kind directly.

**Shape of a notification line.** Free-form string, but the convention is `<count> <noun> [<qualifier>] → <call-to-fetch>`. The call-to-fetch is a copy-pasteable MCP invocation using canonical views.

**Optional `NotificationContext`.** Carries agent-identifying data (agent_id, PRECIS_KINDS mask) so a handler can filter — e.g. gripe notifications suppress themselves for non-admin agents. Default: no filtering.

**Stateless kinds never notify.** By convention, stateless kinds return `[]`. There's no state to catch up on for `url` / `wiki` / `web` / `think` / `research`.

---

## 6. Kind / capability taxonomy

Two tiers by infrastructure requirement: **stateless** (works anywhere; pure API calls or local files) and **state-backed** (requires Postgres reachable at startup). All kinds ship in the single `precis-mcp` monolith; tier is a runtime property, not a package boundary.

### 6.1 Stateless kinds

| Kind | Req | Cost | Notes |
|---|---|---|---|
| `word`, `tex`, `markdown`, `plaintext` | — | free | Existing local-file handlers |
| `url` | — | free | httpx readability fetch |
| `wiki` | — | free | Wikipedia REST |
| `youtube` | — | free | Transcript by video id |
| `code` | `GITHUB_TOKEN` (opt) | free | GitHub / Stack Overflow search |
| `math` | `WOLFRAM_APP_ID` | $/tier | Wolfram Alpha |
| `web` | `PERPLEXITY_API_KEY` | ~$0.002/call | Perplexity Sonar — quick web synthesis, ~2s |
| `think` | `PERPLEXITY_API_KEY` | ~$0.01/call | Perplexity Sonar Reasoning Pro — multi-step reasoning, ~10–30s |
| `research` | `PERPLEXITY_API_KEY` | ~$0.10+/call | Perplexity Sonar Deep Research — multi-source synthesis, 2–10min |

The three Perplexity modes (`web`, `think`, `research`) are distinct kinds, not parameters on a single kind. Agents pick by intent: fast answer → `web`; complex reasoning → `think`; comprehensive synthesis → `research`. The `news` kind is not stateless — see §6.5.

**Long-running beyond research:** Tasks needing hours (full literature sweeps, deep-read agents) don't belong in precis. Dispatch via hermes and have the result delivered to memory as a drawer. Precis surfaces a hint when a `research` query looks like it would benefit:

```
Hints: research returned 4m12s with 11 sources. For days-long reading, dispatch via:
       hermes dispatch feynman "<query>" --deliver-to memory:inbox
```

### 6.2 State-backed kinds: availability rule

All kinds below require Postgres reachable at startup. If PG is unavailable, these kinds are **hidden from the tool enum**; a one-time stderr warning lists what was dropped:

```
WARNING: Postgres not reachable at <DSN>.
  State-backed kinds disabled: paper, memory, todo, flashcard, conversation,
                               news, log, gripe, link.
  Available: url, wiki, web, think, research, youtube, code, math.
```

Stateless kinds continue to work normally. No crash, no broken imports. Once PG becomes reachable (after restart), state kinds reappear.

### 6.3 `paper` — academic corpus

| Kind | Req | Cost | Notes |
|---|---|---|---|
| `paper` | PG + `cluster.acatome.*` schema populated | free | Immutable academic content: chunks, figures, citations. Served by handler delegating to `precis_core.stores.acatome`. Written by `acatome-extract` ingestion CLI. See §7 for id formats. |

### 6.4 Journal kinds — agent state

| Kind | Id type | Storage shape | Embedding |
|---|---|---|---|
| `memory` | **slug** | parent + chunks (papers-shaped) | per-chunk |
| `todo` | **integer** | single table | per-row |
| `flashcard` | **integer** | single table | per-row |
| `conversation` | **session slug or uuid** | parent + turns + chunks | per-chunk (chunks can span turns) |

Tables live in `cluster.journal.*`. Schema owned by the `precis-core` package (see §16); applied idempotently (`CREATE TABLE IF NOT EXISTS`) by precis-mcp at first PG connect. Formal migration tooling (alembic etc.) deferred until there's real data to migrate.

**Why integer ids on todo/flashcard?** Todos and flashcards are short content — "Buy milk", "Q: X / A: Y". The content is its own name; a slug derived from content would echo the same string and bring collision risk on duplicates. Numeric `bigserial` matches the short-lifecycle use case, and agents address these by state/priority/due-date rather than by slug recall.

**Schema sketches** (`cluster.journal.*`, applied idempotently by precis-core):

```sql
-- memory: long-form drawers (same chunked pattern as papers)
CREATE TABLE journal.memories (
  slug          text PRIMARY KEY,
  title         text NOT NULL,
  tags          text[] DEFAULT '{}',
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  metadata      jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE journal.memory_chunks (
  id            bigserial PRIMARY KEY,
  memory_slug   text NOT NULL REFERENCES journal.memories(slug) ON DELETE CASCADE,
  chunk_index   int NOT NULL,
  text          text NOT NULL,
  embedding     vector(1024),
  UNIQUE (memory_slug, chunk_index)
);

-- todo: single table, short content, per-row embedding for similarity
CREATE TABLE journal.todos (
  id            bigserial PRIMARY KEY,
  title         text NOT NULL,
  body          text DEFAULT '',
  state         text NOT NULL DEFAULT 'pending',     -- pending|in_progress|done|blocked|cancelled
  priority      text NOT NULL DEFAULT 'normal',      -- low|normal|high|urgent
  tags          text[] DEFAULT '{}',
  due_at        timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  embedding     vector(1024)
);
CREATE INDEX todos_state_idx  ON journal.todos (state);
CREATE INDEX todos_due_idx    ON journal.todos (due_at) WHERE state IN ('pending', 'in_progress');

-- flashcard: single table, SM-2 state inline, per-row embedding
CREATE TABLE journal.flashcards (
  id            bigserial PRIMARY KEY,
  question      text NOT NULL,
  answer        text NOT NULL,
  tags          text[] DEFAULT '{}',
  ease          real NOT NULL DEFAULT 2.5,
  interval_days int  NOT NULL DEFAULT 0,
  due_at        timestamptz NOT NULL DEFAULT now(),
  reps          int  NOT NULL DEFAULT 0,
  lapses        int  NOT NULL DEFAULT 0,
  created_at    timestamptz NOT NULL DEFAULT now(),
  embedding     vector(1024)
);
CREATE INDEX flashcards_due_idx ON journal.flashcards (due_at);

-- conversation: session + turns + (possibly cross-turn) chunks
CREATE TABLE journal.conversations (
  slug          text PRIMARY KEY,              -- session id or human-chosen slug
  agent_id      text,
  started_at    timestamptz NOT NULL DEFAULT now(),
  ended_at      timestamptz,
  title         text,                          -- optional post-session summary title
  tags          text[] DEFAULT '{}',
  metadata      jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE journal.conversation_turns (
  id              bigserial PRIMARY KEY,
  conversation    text NOT NULL REFERENCES journal.conversations(slug) ON DELETE CASCADE,
  turn_index      int NOT NULL,
  role            text NOT NULL,               -- 'user' | 'assistant' | 'system' | 'tool'
  content         text NOT NULL,
  ts              timestamptz NOT NULL DEFAULT now(),
  UNIQUE (conversation, turn_index)
);
CREATE TABLE journal.conversation_chunks (
  id              bigserial PRIMARY KEY,
  conversation    text NOT NULL REFERENCES journal.conversations(slug) ON DELETE CASCADE,
  turn_start      int NOT NULL,                -- may span multiple turns
  turn_end        int NOT NULL,
  text            text NOT NULL,
  embedding       vector(1024)
);
```

**Chunking behaviour.** All chunked kinds (`memory`, `conversation`) use the shared `precis_core.chunking.split_text` (§16) — character-based, recursive boundary-aware. No per-kind tuning for v1. Embeddings use `BAAI/bge-m3` @ 1024 dim, same vector space as paper blocks and news chunks, enabling cross-kind semantic search.

**Todo/flashcard embedding.** Optional for v1 — `embedding` column exists but can be null. The immediate use case is lookup by state/due-date (indexed fields), not similarity. Populate embeddings opportunistically when `put` writes happen; query-time falls back to keyword search if embedding is null.

### 6.5 `news` — news aggregator

`news` is store-backed only. Without PG, the kind is absent — agents wanting web news should use `web` with a "today" hint.

| Kind | Req | Cost | Notes |
|---|---|---|---|
| `news` | PG + `cluster.news.*` schema populated | free | Aggregates RSS feeds (Guardian, Reuters, BBC, HN, configurable list), opt-in site-specific scrapers. Local search only; no inline Perplexity fallback. |

**Ingestion is out of band** — a background service (`precis-news-ingest`, systemd timer) polls feeds and writes to `cluster.news.*`. Precis only reads.

**Storage schema** (`cluster.news.*`, owned by `precis-news-ingest` as a plain SQL file applied idempotently on service start):

```sql
CREATE TABLE news.articles (
  id              uuid PRIMARY KEY,
  source          text NOT NULL,             -- 'guardian', 'bbc', 'hn', ...
  source_url      text NOT NULL,             -- canonical article URL (dedup key)
  title           text NOT NULL,
  authors         text[] DEFAULT '{}',
  published_at    timestamptz,               -- nullable if unknown
  retrieved_at    timestamptz NOT NULL DEFAULT now(),
  summary         text,                      -- RSS summary or extracted lede
  tags            text[] DEFAULT '{}',
  metadata        jsonb NOT NULL DEFAULT '{}',
  UNIQUE (source, source_url)
);

CREATE TABLE news.article_chunks (
  id              uuid PRIMARY KEY,
  article_id      uuid NOT NULL REFERENCES news.articles(id) ON DELETE CASCADE,
  chunk_index     int NOT NULL,
  text            text NOT NULL,
  embedding       vector(1024),          -- BAAI/bge-m3 default (see precis-core embedding config)
  UNIQUE (article_id, chunk_index)
);
```

Two tables: metadata lightweight (`articles`) for listings and filters; body lives as embedded chunks (`article_chunks`) for semantic search. Search: embed query → vector search on chunks → group by `article_id` → rank by top-chunk score → fetch article metadata.

Dedup key is `(source, source_url)` — syndicated articles from multiple sources stay separate (cross-source clustering is Future).

**Views:** `/recent`, `/source/guardian`, `/topic/climate`, `/today`.

### 6.6 `log` — cluster log reader

| Kind | Req | Cost | Notes |
|---|---|---|---|
| `log` | PG + shipper service running | free | Read-only structured events from precis, hermes, acatome, sortie, and anything using the writer library. No `put`. |

**Three-role architecture** (detailed in §16 Phase 10):

1. **Writer library** `precis-logger` (imported by every cluster service): structlog-based, JSON to stderr + optional file (`CLUSTER_LOG_FILE`). **Zero network, zero PG deps.** Pip namespace is `precis-*` across all shared packages (§17, resolved).
2. **Shipper daemon** (per-host, systemd): tails `/var/log/cluster/*.jsonl`, ships to configured sinks. Handles buffering, retries, rotation-follow.
3. **Sink(s)** (configured): primary is `cluster.logs` table; alternatives (Loki, Vector, file backup) are pluggable via shipper config.

```yaml
# /etc/precis-log-shipper/sinks.yml (example)
sinks:
  primary:
    type: pg
    dsn: postgresql://log_shipper@caspar/cluster
    table: logs
    batch_size: 500
    flush_interval_s: 10
```

**Writer library has no PG dependency** — writes only to stderr + optional file. The shipper daemon is the only component that touches PG. Services importing the writer never pull psycopg into their venv.

**Table schema** (top-level `cluster.logs`):

```sql
CREATE TABLE logs (
  id          bigserial PRIMARY KEY,
  ts          timestamptz NOT NULL,
  service     text NOT NULL,
  host        text NOT NULL,
  level       text NOT NULL,                 -- 'debug' | 'info' | 'warn' | 'error'
  event       text NOT NULL,                 -- short event name, e.g. 'handler_call'
  trace_id    text,                          -- correlation across services (§6.6.1)
  payload     jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX logs_ts_idx       ON logs (ts DESC);
CREATE INDEX logs_service_idx  ON logs (service, ts DESC);
CREATE INDEX logs_event_idx    ON logs (event, ts DESC);
CREATE INDEX logs_trace_idx    ON logs (trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX logs_payload_gin  ON logs USING gin (payload);
```

**Retention** — 30 days rolling, enforced by pg_cron:

```sql
SELECT cron.schedule('precis-log-retention', '0 3 * * *',
  $$DELETE FROM logs WHERE ts < now() - interval '30 days'$$);
```

Configurable via `CLUSTER_LOG_RETENTION_DAYS`.

**Default log file path** — `/var/log/cluster/<service>.jsonl` for system daemons (ansible-managed, most cluster services). Dev laptops and per-user processes override via `CLUSTER_LOG_FILE` env var. Stderr is always on regardless of file config.

**Views:** `/recent`, `/errors`, `/service/precis`, `/trace/<trace_id>`, `/event/<event_name>`.

#### 6.6.1 `trace_id` propagation

A `trace_id` ties related log events across services into one coherent thread. Example chain: Claude message → hermes → precis-mcp (stdio subprocess) → precis-core.stores.acatome (Python call) → PG query. Without a trace_id, those events are a soup; with one, `get(type='log', id='/trace/<id>')` returns the whole thread ordered by `ts`.

Propagation mechanisms, in order of use:

- **Entry points generate.** Hermes on new message: `trace_id = os.environ.get('CLUSTER_LOG_TRACE_ID') or uuid4().hex`. Same in any other ingress.
- **Env var for subprocesses.** Parent sets `CLUSTER_LOG_TRACE_ID=<id>` before exec. Child's log writer reads it at init, embeds in every event.
- **contextvars intra-process.** `precis_logger.set_trace_id(id)` on entry to a handler; `get_logger()` picks it up automatically.
- **HTTP headers (future).** W3C `traceparent` when any two cluster services talk HTTP directly.

For v1: env var + contextvars. HTTP propagation deferred until needed.

### 6.7 `gripe` — agent complaints

| Kind | Req | Cost | Notes |
|---|---|---|---|
| `gripe` | PG + shipper running | free | Agent-authored complaints. Put + read/search. Goes through the same shipper pipeline as logs. |

**Use case.** Agents file a gripe when something errored unexpectedly, a result was nonsensical, or behaviour didn't match the documented contract. Errors that aren't the agent's fault (timeouts, handler crashes, unavailable upstreams) surface a hint like:

```
Hints: if this error looks like a bug, gripe about it:
       put(type='gripe', text='<what you expected vs what happened>', id='<trace_id_or_uri>')
```

**Write path.** `put(type='gripe', text=..., id=...)` produces two things:

1. A `gripe_filed` log event written via the writer library (so it appears in `/trace/<id>` views alongside the error it's about).
2. A structured JSON-line record written to the gripe file (`/var/log/cluster/gripes.jsonl`), which the shipper lifts into `cluster.gripes`.

Both writes are fire-and-forget from the handler's perspective — no network, no PG wait. The shipper is the only PG-touching component. If PG is down, gripes queue in the file and catch up on recovery.

**Schema** (`cluster.gripes`):

```sql
CREATE TABLE gripes (
  id          uuid PRIMARY KEY,
  ts          timestamptz NOT NULL DEFAULT now(),
  service     text NOT NULL,                 -- which service the agent was talking to
  host        text NOT NULL,
  agent_id    text,                          -- hermes-supplied when available
  trace_id    text,                          -- correlate with the bad call
  context_ref text,                          -- URI the complaint is about ('web:last', 'paper:wang2020state', trace id)
  complaint   text NOT NULL,                 -- the gripe text
  error_code  text,                          -- matching error code from the triggering error (if any)
  payload     jsonb NOT NULL DEFAULT '{}'    -- additional structured context: last request, environment, recent events
);
CREATE INDEX gripes_ts_idx      ON gripes (ts DESC);
CREATE INDEX gripes_service_idx ON gripes (service, ts DESC);
CREATE INDEX gripes_trace_idx   ON gripes (trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX gripes_code_idx    ON gripes (error_code) WHERE error_code IS NOT NULL;
CREATE INDEX gripes_payload_gin ON gripes USING gin (payload);
```

**Context capture.** The handler populates `payload` with as much context as practical at the moment of the gripe: the last request the agent made, recent trace events (if available in the current process), environment flags, and any error message that was just returned. This is for human debugging — more context is better, bounded by the 64KB payload cap.

**Read.** `get(type='gripe', id=<uuid>)` and `search(type='gripe', query=..., service=..., since=...)` serve agent triage workflows and admin review.

**Views:** `/recent`, `/service/<name>`, `/trace/<trace_id>`, `/unresolved` (future — status field for triage lifecycle).

**Relation to the standalone `gripe-mcp` package:** shares the writer format. `gripe-mcp` continues to publish independently for non-precis users. Precis-mcp folds the capability in so agents have one fewer MCP to juggle.

### 6.8 Aliases

Hidden from enum, accepted at URI parse (for legacy content). **Rejected in `PRECIS_KINDS` config** (§13).

- `wolfram → math`
- `perplexity → web`

Alias error format explicitly disclaims equivalence:

```
Note: 'wolfram' is an alias for 'math' — they are identical, just different names. Use 'math'.
```

This kills "let me try the other one to see if it's different" reasoning.

### 6.9 Plugin name conflicts

Two plugins declaring the same kind name is a **design error**, not a runtime condition to paper over. Startup fails with:

```
ERROR: type name collision. Plugin 'precis-alt-web' declares type='web', already provided by 'precis-web'.
  Resolve by renaming one plugin's capability, or set PRECIS_PLUGINS to load only one.
```

No silent winner. Name your capabilities distinctly.

### 6.10 Identifier schemes folded into `paper`

`doi:`, `arxiv:`, `pubmed:`, `pmcid:`, `isbn:`, `issn:` are accepted id formats on `type='paper'`, not separate kinds. See §7.

---

## 7. Paper id auto-detection

`get(type='paper', id=X)` accepts many id formats. Detection order:

| Pattern | Example | Detect |
|---|---|---|
| `10.NNNN/…` | `10.1021/jacs.2c01234` | **DOI** — unambiguous (IANA-reserved prefix) |
| `NNNN.NNNNN[vN]` | `2301.12345`, `1909.03550v2` | **arXiv new** — distinctive shape |
| `category/NNNNNNN` | `hep-th/0509038` | **arXiv old** — closed category vocabulary |
| `PMC\d+` | `PMC3234532` | **PMCID** — distinctive prefix |
| `(978\|979)-?(digits×10)` | `9780306406157` | **ISBN-13** — 978/979 + length 13 |
| `N-NNN-NNNNN-N/X` hyphenated | `0-306-40615-2` | **ISBN-10 hyphenated** |
| `NNNN-NNNN` | `1234-5678` | **ISSN** — four-dash-four |
| `(pmid\|doi\|arxiv\|pmcid\|isbn\|issn\|pubmed):…` | `pmid:12345678` | **Explicit prefix** — strip and use |
| `\d+` pure digits | `12345678` | **Ambiguous** — try as slug; miss → hint toward `pmid:` / `isbn:` |
| `[a-z][a-z0-9-]+` | `wang2020state` | **Slug** — local store lookup |

**Multiple identifiers per record.** A book can carry DOI + ISBN + ISSN-of-series simultaneously; a journal article carries DOI + ISSN. The paper ref stores all known ids in a nested `identifiers: {doi, isbn, issn, arxiv, pmid, pmcid}` field; lookup by any of them returns the same record.

**No fallback.** `paper` is a state-backed kind (§6.2). If PG isn't reachable or the `cluster.acatome.*` schema isn't populated, the kind is absent from the enum. No CrossRef/arXiv/PubMed metadata-only mode — if you want information about a paper you don't own, use `type='research'` or `type='wiki'`.

**External identifier resolution** (ISBN / ISSN / DOI lookup on ingest): via OpenLibrary (primary, no key) → Google Books (fallback, `GOOGLE_BOOKS_API_KEY` optional for higher quota). Happens at ingest time through `acatome-extract`, not at `get` time.

### 7.1 Integer ids for `todo` and `flashcard`

Unlike `paper` (slug-based) or `memory` (human-named slug), `todo:42` and `flashcard:7` use plain integer ids assigned by the PG `bigserial`. The URI parser accepts bare digits for these kinds:

```
todo:42                 # resolves via cluster.journal.todos.id = 42
flashcard:7             # resolves via cluster.journal.flashcards.id = 7
```

Bare digits without a type prefix stay ambiguous (could be any of several integer-id kinds); the parser requires the prefix.

---

## 8. Views

### 8.1 Principle

Views are named renderings of the same resource, addressed as URI suffix on `get`:

- `/summary` — same content, condensed
- `/cite/bib` — same content, formatted as BibTeX
- `/fig/3/legend` — just the caption of figure 3
- `/sources` — just the cited URLs of a web answer
- `/links` — outgoing edges (whatever the source kind is)

Views are **not declared** on `KindSpec`. Handlers implement whichever make sense and return a clean error for unknown ones. The standard four (`/summary`, `/toc`, `/meta`, `/stats`) are expected from anything with structured content.

### 8.2 Typical views per kind (reference, not declaration)

**Universal** (most kinds with structure):

| View | Semantics |
|---|---|
| `/summary` | One-paragraph condensed |
| `/toc` | Structural outline |
| `/meta` | Title / author / date / source |
| `/stats` | Call counts, cost, recent activity, health notes |

`get(id='/stats')` (no kind) returns aggregate server-wide stats: kinds enabled, session cost, uptime, startup warnings. Not hidden, not privileged — anyone can read.

**Stored content (paper, memory, doc, todo, flashcard, conversation):**

| View | Semantics |
|---|---|
| `/cite/bib`, `/cite/apa`, `/cite/ris` | Formatted citation (paper) |
| `/abstract` | Abstract field (paper) |
| `/fig`, `/fig/N`, `/fig/N/legend`, `/fig/N/image`, `/fig/N/image/export` | Figures (paper, doc) |
| `/links` | Outgoing edges — cross-kind references (§9) |
| `/links-in` | Incoming edges — reverse references |
| `/notes` | Annotations attached to this ref |

Replaces the paper-specific `/cites` and `/cited-by` vocabulary. `/links` is universal; paper citations are just one case of link.

**Oracle / external:**

| View | Applies to |
|---|---|
| `/sources` | web, news, think, research |
| `/steps`, `/pod/N`, `/image` | math |
| `/chapters`, `/quote/TIMESTAMP` | youtube |
| `/recency` | web, news |

**Memory-specific:**

| View |
|---|
| `/wake-up` — recent + relevant brief |
| `/recent` — drawers written in last N days |

**Todo-specific:**

| View |
|---|
| `/today` — today + overdue |
| `/due` — everything with a due date |
| `/state` — state machine info |

**Conversation-specific:**

| View |
|---|
| `/session/N` — transcripts of session N |
| `/recent` — recent turns across sessions |

### 8.3 Pagination meaning per kind

`page` is a standard `get` param (§4.2), but meaningful only for paginated content. Non-paginated handlers return an empty-page with a self-teaching hint:

| Kind | `page` means |
|---|---|
| `paper`, `word`, `tex`, `markdown` | document page / chunk block |
| search results (any kind) | next page of the result list beyond `top_k` |
| `youtube` | N/A — single transcript |
| `web`, `news`, `think`, `research` | N/A — single synthesis |
| `wiki`, `math` | N/A — single answer |

When `page > 1` on a non-paginated kind:

```
<empty>

Hints:
  - 'page' has no effect on type='web' — results are a single synthesis
  - for more breadth, use top_k= (currently 5) or try type='research'
```

No error. Self-teaching via hint.

---

## 9. Links — cross-cutting primitive

Links are typed edges between URIs across any pair of stored kinds. They're the mechanism for the "paper graph", "agent knowledge graph", and any cross-kind reference.

### 9.1 Data model

```
link: (from_uri, to_uri, relation, meta, created_at, created_by)
```

Examples:

- `paper:wang2020state → memory:drawer-abc` (relation: `notes`)
- `todo:1234 → paper:einstein1905` (relation: `references`)
- `conversation:session-42 → todo:9876` (relation: `produced`)
- `memory:draft-intro → url:https://...` (relation: `cites`)

### 9.2 Where links live

In the **`precis-core`** shared package. Schema + `LinkStore` API are used by all state-backed kinds in precis-mcp (journal / news / memory / paper citations). Single shared table. If PG is unreachable, link-related verbs/views cleanly error per §6.2.

Schema (Postgres):

```sql
CREATE TABLE links (
    id          bigserial PRIMARY KEY,
    from_uri    text NOT NULL,
    to_uri      text NOT NULL,
    relation    text NOT NULL DEFAULT 'references',
    meta        jsonb DEFAULT '{}'::jsonb,
    created_at  timestamptz DEFAULT now(),
    created_by  text,
    UNIQUE (from_uri, to_uri, relation)
);

CREATE INDEX links_from_idx ON links (from_uri);
CREATE INDEX links_to_idx   ON links (to_uri);
CREATE INDEX links_rel_idx  ON links (relation);
```

Single shared table in the `cluster` DB. Every state-backed kind queries through the same `LinkStore` impl from `precis-core`.

### 9.3 Verbs and views

No new verbs. Links are first-class via existing surface:

| Operation | Form |
|---|---|
| **Create** | `put(type='<source>', id='<id>', mode='link', text='relation: <rel>, to: <target_uri>')` |
| **List outgoing** | `get(type='<type>', id='<id>/links')` |
| **List incoming** | `get(type='<type>', id='<id>/links-in')` |
| **Delete** | `put(type='<source>', id='<id>', mode='unlink', text='to: <target_uri>')` |

Put mode `link` / `unlink` is a non-standard extension of put. Handlers that have a link store available expose it via hints:

```
Hints:
  - link this to another ref: put(type='paper', id='X', mode='link', text='to: memory:Y, relation: notes')
```

### 9.4 Cross-backend links (target kind not enabled)

When traversing `/links` and a target URI's kind is absent (because it's masked by `PRECIS_KINDS` or PG is unreachable), the link is shown with a note:

```
/links for paper:wang2020state:
  - paper:smith2021fwd   (relation: cites)
  - memory:drawer-abc    (relation: notes, target kind 'memory' not enabled — widen PRECIS_KINDS to follow)
```

The data is preserved; the agent just can't traverse until the target kind is available.

### 9.5 Links when PG is unreachable

When precis-mcp runs without a reachable Postgres (§6.2), state-backed kinds and the link store are both absent. The stateless kinds that remain (`url`, `wiki`, `web`, etc.) are external and can't host outbound edges anyway. Attempting `put(..., mode='link')` returns:

```
ERROR [unavailable]: link store unreachable.
  where: type='<type>' verb='put' mode='link'
  cause: no state-backed type is active; link store requires PG reachable at startup
  next: ensure CLUSTER_DATABASE_URL is set and PG is accepting connections, then restart
```

---

## 10. Discovery & lifecycle

1. **Process start.** `_discover()` runs once — same as today.
2. **Env gating.** For each `KindSpec`, check `requires` env vars. Missing → kind is omitted from the enum and URI resolver. Warning emitted via the startup-warning channels (§10.2).
3. **Kind registry.** `KINDS: dict[str, RegisteredKind]` keyed by canonical name. Aliases live in a sibling `ALIASES: dict[str, str]` redirect map.
4. **Plugin conflict check.** If two plugins declare the same kind name → startup **fails** with a clear error (§6.5). No silent winner.
5. **Masking.** `PRECIS_KINDS` bracket syntax (§13) applied. Aliases in config are rejected (startup error); canonical names only.
6. **Tool schema emission.** FastMCP tool descriptions auto-generate the one-line-per-kind table from `KindSpec.description`.

No background probes. No startup health checks. No schema reload notifications. Every call's outcome is logged; state tracking and dynamic enum drops are deferred (§19 Future enhancements).

### 10.1 Fatal vs non-fatal at startup

| Condition | Fatal? | Channel |
|---|---|---|
| Plugin import error | No | stderr + gripe + `/stats` warnings |
| Missing required env key | No | stderr + gripe + `/stats` warnings |
| Kind name collision between plugins | **Yes** — exit | stderr |
| `PRECIS_KINDS` has unknown kind | No — skip the unknown | stderr + gripe + `/stats` |
| `PRECIS_KINDS` has alias (e.g. `wolfram`) | **Yes** — exit | stderr |
| `PRECIS_KINDS` has unknown verb in brackets | **Yes** — exit | stderr |
| `PRECIS_KINDS` has empty brackets or duplicate kind | **Yes** — exit | stderr |
| Required schema missing on PG (check at boot via `information_schema`) | **Yes** — exit | stderr |

### 10.2 Where warnings surface

| Channel | Audience | Shape |
|---|---|---|
| **stderr** | humans tailing logs, systemd journal | one-liner per warning |
| **gripe event** | observability (`gripe tail`) | structured `{kind, code, detail, ts, source}` |
| **`get(id='/stats')`** | agents, admins at runtime | `startup_warnings: [strings]` field |
| **MCP `initialize` response** | MCP-aware clients (IDE, Hermes) | non-standard `precis_warnings` field |

All four channels emit for every warning. Fatal errors hit stderr only (process dies before anything else can see them).

Example `/stats` output:

```
kinds enabled: paper, memory, web, wiki, todo
calls this session: 127
cost this session: $0.23
uptime: 2h 14m

startup warnings:
  - kind 'fruitbat' in PRECIS_KINDS is unknown; skipped
  - PERPLEXITY_API_KEY missing; 'web', 'news', 'think', 'research' disabled
  - plugin 'precis-experimental' failed import: ModuleNotFoundError
```

---

## 11. Exception isolation + error formatting

Every handler call routes through `invoke_handler()`. Its jobs:

1. Wrap the dispatch in a `timeout()` context.
2. Catch every exception, produce a maximum-information error string, never propagate a crash.
3. Compute hints from multiple sources, aggregate, dedup, rank, cap at 5.
4. Append the standard response footer.

### 11.1 `invoke_handler`

```python
def invoke_handler(kind, verb, **args) -> Result:
    ctx = CallContext(kind, verb, args, started=now())
    try:
        with timeout(kind.timeout_s):
            raw = kind.handler.dispatch(verb, **args)
        hint_ctx = HintContext.from_result(raw, ctx)
        hints = _aggregate_hints(kind, raw, hint_ctx)
        cost  = kind.handler.cost_of(ctx)
        return Result.ok(raw, kind=kind, cost=cost, hints=hints)
    except TimeoutError:
        return Result.error(_format_error("timeout", ctx, cause=f"exceeded {kind.timeout_s}s"))
    except RateLimitError as e:
        return Result.error(_format_error("rate_limited", ctx, cause=str(e), next_hint=f"retry in {e.retry_after}s"))
    except PrecisError as e:
        return Result.error(_format_error(e.code, ctx, cause=e.cause, options=e.options, next_hint=e.next))
    except Exception as e:
        log.exception("handler crash in %s", kind.name)
        return Result.error(_format_error("unexpected", ctx, cause=f"{type(e).__name__}: {e}"))
```

Guarantees:

- No uncaught exception reaches the MCP transport.
- Every error string is structured and self-explanatory.
- Crashes emit a gripe event with traceback.

### 11.2 Unified error format

```
ERROR [<code>]: <one-line summary>
  where: type='<type>' verb='<verb>' id='<id_if_any>'
  cause: <concrete reason>
  options: <comma-separated valid alternatives>
  next: <one concrete action>
```

All fields except `cause` are optional and omitted when not meaningful. Single helper `_format_error(code, ctx, cause, options=None, next_hint=None)` produces every error string in the server. One code path, one shape.

Examples:

```
ERROR [view_unknown]: paper view '/histogram' not recognized.
  where: type='paper' verb='get' id='wang2020state/histogram'
  cause: handler does not expose this view
  options: /summary, /toc, /meta, /abstract, /cite/bib, /fig, /links, /links-in
  next: pick one of the known views
```

For errors that **aren't the agent's fault** (handler crashes, upstream timeouts, unavailable backends), the `next` field carries a gripe hint so the agent can flag the issue:

```
ERROR [unexpected]: web handler crashed: KeyError: 'choices'
  where: type='web' verb='search' query='low-pressure argon optics'
  cause: perplexity response missing expected field
  trace_id: a3f9c1...
  next: retry once; if reproducible, gripe about it:
        put(type='gripe', text='web handler crashes on some queries', id='a3f9c1...')
```

The gripe hint is emitted by `_format_error()` for codes `unexpected`, `timeout`, `unavailable`, and `rate_limited`. Not for user-side errors (`type_unknown`, `view_unknown`, `id_not_found`) — those are the agent's cue to fix the call, not file a complaint.

### 11.3 Standard error catalogue

| Code | Trigger | `options` | `next` |
|---|---|---|---|
| `kind_unknown` | Kind name not registered | Available kinds | Pick from list |
| `kind_unavailable` | Kind requires env key or PG, and it's not reachable | — | Set `<env>` / check PG connectivity |
| `verb_unsupported` | Handler doesn't implement verb | Supported verbs for this kind | Use one of those |
| `view_unknown` | Unknown view suffix | Known views for this kind | Pick from list |
| `mode_unsupported` | Put mode not allowed | Supported modes | Pick from list |
| `id_not_found` | Resource doesn't exist | — | Check id; try `search` |
| `id_ambiguous` | Multiple matches | Matching ids (truncated) | Qualify with prefix |
| `id_malformed` | Unparseable id | Accepted formats | Reformat |
| `param_invalid` | Bad param value | Valid values / range | Fix value |
| `readonly` | Put on read-only kind | Kinds that accept `put` | Use a writable kind |
| `denied` | Masking prohibits this verb on this kind | Enabled verbs for this kind | Adjust profile |
| `timeout` | Call exceeded `timeout_s` | — | Retry; consider async |
| `rate_limited` | Upstream 429 | — | Wait `<seconds>` and retry |
| `upstream_error` | 5xx from upstream | — | Retry later; try alternate kind |
| `unavailable` | State-backed kind needs PG (or a required env var) that isn't reachable/set | — | Check `CLUSTER_DATABASE_URL` and required env; see startup warnings |
| `unexpected` | Handler crash | — | Admin notified; try another kind |

Handlers raise `PrecisError(code, cause, options, next_hint)` for domain failures. The wrapper handles transport failures.

### 11.4 Hints aggregation

Hints come from three sources in order of priority:

1. **Wrapper-driven** — result-count heuristics (`too_many`, `zero_results`, `partial`). Generic, always first.
2. **Handler-specific** — `Handler.hints(result, ctx) → [(text, priority)]`. Per-kind knowledge (filters, views, related kinds).
3. **Meta** — id-format detection results, alias notices, deprecation warnings.

Merge rules:

- Dedup by ~40-char prefix.
- Rank by priority (wrapper heuristics highest, meta lowest).
- Cap at 5.
- Each hint ≤ 80 chars.

Format:

```
<result body>

Hints:
  - <hint 1>
  - <hint 2>
  - <hint 3>
```

Single `Hints:` block per response, blank line before. Silent on sweet-spot results (see §5.3).

### 11.5 Error vs hint vs note vs warn

Four distinct signals, visually separable:

| Signal | Prefix | Format | When |
|---|---|---|---|
| **Error** | `ERROR [code]: ...` | Structured block (§11.2) | Operation failed |
| **Warn** | `Warn: ...` | One line, separate from body | Non-fatal operational issue (rate-limit close, deprecation) |
| **Hint** | `Hints:\n  - ...` | Markdown list, suffix | Operation succeeded; narrower/broader would help |
| **Note** | `Note: ...` or `(note: ...)` | Inline, one sentence | Meta-commentary (alias resolved, version info) |

Zero results = **success with hints**, not an error. Reserve errors for actual failures.

---

## 12. Cost reporting

### 12.1 Cost declaration

Handlers advertise an unstructured `cost_hint` string on the `KindSpec` (e.g. `"~$0.002/call"`, `"free"`, `"$/tier"`, `None`). Per-call exact cost comes from `Handler.cost_of(ctx)`, also a string. No structured `CostModel` class hierarchy.

### 12.2 Response footer

Every successful call appends:

```
---
source: web (perplexity/sonar)
cost: $0.0023 this call · $0.14 session
```

Free kinds show `$0.00 · $0.00 · …` — consistent presence is better than conditional.

### 12.3 Running totals

- **Session:** in-process counter, resets on restart.
- **Daily / lifetime:** later, via redis or gripe. Deferred until a concrete use case.

### 12.4 Exposed via `/stats`

No separate `/cost` admin URI. Session cost totals are part of `get(id='/stats')`:

```
kinds enabled: paper, memory, web, wiki, think, research
calls this session: 127
cost this session: $0.37
  web       $0.14   (62 calls)
  think     $0.12   (5 calls)
  research  $0.11   (1 call)
  math      $0.00   (free tier)
  others    $0.00
uptime: 2h 14m
```

No budget guard. Reporting only. Hard cutoffs are Future (§19).

---

## 13. Per-agent masking

**One env var, bracket syntax.**

```
PRECIS_KINDS = KIND_SPEC [, KIND_SPEC …]
KIND_SPEC    = KIND [ '[' VERB [, VERB …] ']' ]
VERB         = search | get | put | move
```

- Bare kind → all verbs allowed.
- Bracketed → whitelist those verbs only.
- Kinds not listed → absent from enum.
- `PRECIS_KINDS` unset → all registered kinds exposed with all verbs. (No "admin mode" distinction — `/stats` is always public, see §8.)

### 13.1 Examples

```bash
# writer: full paper/memory, read-only docs
PRECIS_KINDS=paper,memory,doc[get,search]

# reviewer: read everything, write nothing
PRECIS_KINDS=paper[get,search],memory[get,search],doc[get,search],web,wiki

# research-agent: external read, internal read/write-to-memory
PRECIS_KINDS=paper[get,search],memory,web,wiki,research,url

# flashcard-review: narrow scope
PRECIS_KINDS=flashcard

# coder: writes only to memory
PRECIS_KINDS=memory,web,wiki,url[get],youtube[get],code,paper[get,search]
```

### 13.2 Tool schema effect

Kinds appear in only the tools their allowed verbs are used for. With `PRECIS_KINDS=paper,memory,doc[get,search]`:

- `search()` enum: `{paper, memory, doc}`
- `get()` enum: `{paper, memory, doc}`
- `put()` enum: `{paper, memory}` ← doc absent
- `move()` enum: `{paper, memory}` ← doc absent

Constrained decoding ensures the agent cannot emit `put(type='doc', …)`.

### 13.3 Parser behaviour

Regex-based, ~30 LOC. Validates on startup:

| Condition | Behaviour |
|---|---|
| Unknown kind | **Warn** and skip (§10.2 channels). |
| Alias in config (e.g. `wolfram[search]`) | **Fatal** — exit with message pointing to canonical name. Aliases are runtime-only. |
| Unknown verb in brackets | **Fatal** — exit with allowed verbs. |
| Empty brackets `doc[]` | **Fatal** — exit. |
| Duplicate kind | **Fatal** — exit. |
| Whitespace | Tolerated. `paper, memory , doc[ get , search ]` normalises. |
| Mixed case | Lowercased before match. `Paper[Get]` → `paper[get]`. |

Fatal means: print one-line explanation to stderr and `exit(2)`. Keep the agent-facing config strict; ambiguity is never silently fixed.

### 13.4 Discovering canonical names

If a config uses an alias, the startup error lists valid alternatives:

```
ERROR: PRECIS_KINDS contains alias 'wolfram'. Use canonical name 'math'.
  Aliases are for runtime URI compatibility only, not for config.
  Canonical kinds: paper, memory, web, news, think, research, wiki, math,
                   youtube, url, code, todo, flashcard, conversation,
                   gripe, log, doc.
  Aliases (accepted in URIs only): wolfram → math, perplexity → web.
```

The canonical list + aliases is also available to agents via the MCP resource `precis://kinds` (registered at startup).

---

## 14. Per-agent customization across environments

Five options, preference in order:

### Option A — per-process instance with env (default, implemented)

Every MCP client launches the server as a subprocess. Each gets its own env. Works for every MCP-aware IDE and for Hermes.

```yaml
# Hermes: ansible/roles/hermes/templates/profiles/writer.yaml.j2
mcps:
  precis:
    command: precis-mcp
    env:
      PRECIS_KINDS: paper,memory,doc[get,search]
```

```json
// Claude Desktop / Cursor / Windsurf user config
{
  "mcpServers": {
    "precis-research": {
      "command": "precis-mcp",
      "env": {
        "PRECIS_KINDS": "paper,memory,web,wiki,research",
        "PERPLEXITY_API_KEY": "sk-..."
      }
    }
  }
}
```

### Option B — `PRECIS_CONFIG=/path/to/profile.toml` (Future)

One env var pointing to a TOML config file. For environments restricted to a single env var, or when config should be git-tracked.

### Option C — launcher wrapper scripts (Future)

Ansible-rendered per-profile shell scripts. Client launches `precis-research`; the wrapper sets env and exec's the binary. For clients that only accept a bare command path.

### Option D — URL-path routing (Future)

For a shared HTTP/SSE precis daemon: `https://precis.local/mcp/research-agent` vs `.../mcp/writer`. Each path loads a different profile. Auth via bearer token.

### Option E — MCP `initialize` client-identity lookup (Future)

Use `clientInfo.name` from the handshake to pick a profile. Convenience default, not a security boundary.

**Always-on defense in depth:** per-session call counter, structured audit log via gripe.

For v1 only **Option A** is implemented. B–E are Future (§19).

---

## 15. Failure modes catalogued

| Failure | Detection | Action | Agent-visible effect |
|---|---|---|---|
| Missing required env key | `requires` check at startup | Kind omitted from enum; warning emitted (§10.2) | Kind absent |
| Plugin import error | `_register_builtins` try/except | Plugin skipped; warning emitted | Kind absent |
| Kind name collision | Startup kind-registry build | **Fatal** — exit with diagnostic | Server fails to start |
| `PRECIS_KINDS` alias / bad verb / empty brackets | Config parser | **Fatal** — exit with canonical names / allowed verbs | Server fails to start |
| `PRECIS_KINDS` unknown kind | Config parser | Warn and skip | Kind absent |
| Call timeout | Per-kind `timeout_s` wrapper | `ERROR [timeout]` with context | Error |
| Upstream 5xx | Catch in `invoke_handler` | One retry with jitter, then `ERROR [upstream_error]` | Error |
| Upstream 429 | `RateLimitError` | Respect Retry-After (cap 30s), `ERROR [rate_limited]` | Error with retry hint |
| Unexpected exception | Bare except in `invoke_handler` | Gripe with traceback; `ERROR [unexpected]` | Error: "crashed; admin notified" |
| Unknown verb on kind | Handler raises `PrecisError` | `ERROR [verb_unsupported]` lists verbs | Error |
| Unknown view on kind | Handler raises `PrecisError` | `ERROR [view_unknown]` lists views | Error |
| Unknown mode on put | Handler raises `PrecisError` | `ERROR [mode_unsupported]` lists modes | Error |
| Link on no-store install | Handler raises `PrecisError` | `ERROR [unavailable]` pointing to extras | Error |
| Link target kind not enabled | Traversal of `/links` | Show link with note (§9.4) | Note on each affected edge |
| Shared upstream cascade | Independent failures | Each kind reports its own error | Affected kinds error |
| Revoked API key | First call returns 401 | `ERROR [upstream_error]` surfaces 401 | Error hints toward admin |

Reactive health (auto-hiding kinds after sustained failures) and budget guards are Future (§19).

---

## 16. Alteration plan

Phased work, each phase independently shippable.

### Package inventory

Five packages span the whole plan:

| Package | Role | Consumes | Size |
|---|---|---|---|
| `precis-logger` | Writer library — structlog-based, stderr + file, zero PG | — | ~200 LOC |
| `precis-log-shipper` | Daemon — tails log files, ships to PG sink (and future Loki/Vector/etc.) | `precis-logger`, psycopg | ~500 LOC |
| `precis-core` | Shared data layer for the whole cluster. Contents: URI types, plain SQL schema files for every non-log state table (`acatome.*`, `journal.*`, `news.*`, `links`, `gripes`), thin Python store classes per schema, `LinkStore` API, **shared chunker** (character-based, recursive, hoisted from `acatome-extract`), **shared embedding** (local `sentence-transformers` + `BAAI/bge-m3` @ 1024 dim, matching acatome's existing default). | `precis-logger`, `psycopg`, `pgvector`, `sentence-transformers` | ~1500 LOC |
| `precis-mcp` (monolith) | All kinds + MCP server entry point. Read handlers delegate to `precis-core` stores. | `precis-logger`, `precis-core` | ~3000 LOC |
| `precis-news-ingest` | Daemon — RSS poll + scrape + chunk + embed → `cluster.news.*` via precis-core stores | `precis-logger`, `precis-core` | ~600 LOC |
| `acatome-extract` (existing) | Ingestion CLI for papers — fetch + chunk + embed + write via `precis-core.stores.acatome` | `precis-logger`, `precis-core` | existing |
| `acatome-meta` (existing) | Metadata fetching (CrossRef, arXiv, OpenLibrary, Google Books) | `precis-logger` | existing |

**`acatome-store` folded into `precis-core`.** The original rationale for a separate store package was to let people use the paper corpus without pulling in precis-mcp. With PG mandatory and precis-mcp monolithic, that separation no longer earns its keep. The paper schema and data layer move into `precis-core.stores.acatome` alongside journal / news / links / gripes stores. `acatome-extract` and `acatome-meta` keep their identity as ingest tooling (they have independent user-facing value); they import `precis-core` for data access.

**No capability extras on `precis-mcp`.** State-backed kinds appear or disappear based on PG reachability at startup (§6.2), never on install-time extras. `PRECIS_KINDS` is the only per-instance exposure control.

### Shared chunker + embedding config (in precis-core)

**Chunker.** Hoist the existing `acatome_extract.chunker.split_text` into `precis_core.chunking.split_text`. It's pure Python, zero ML deps — a recursive character-based splitter that prefers natural boundaries (paragraph → newline → sentence → word). Defaults from the acatome version stand as cluster defaults:

- `DEFAULT_CHUNK_SIZE = 800` (characters, not tokens)
- `DEFAULT_CHUNK_OVERLAP = 150`
- `DEFAULT_SEPARATORS = ["\n\n", "\n", ". ", ", ", " "]`

All four chunking consumers use this same function — character defaults held constant so cross-kind chunks are comparably sized:

- `acatome-extract` (paper body → `acatome.blocks`) — already uses this; now imports from `precis-core`
- `precis-news-ingest` (article body → `news.article_chunks`)
- `precis-mcp` memory writes (drawer body → `journal.memory_chunks`)
- `precis-mcp` conversation ingest (session text → `journal.conversation_chunks`)

**Embedding.** Local-only via `sentence-transformers`. Match the acatome-store default so existing embeddings don't need re-computation at migration time:

- Model: **`BAAI/bge-m3`**
- Dimension: **1024**
- Provider: `sentence-transformers` (runs on the host CPU or GPU; no API calls)
- Overridable via `CLUSTER_EMBEDDING_MODEL` / `CLUSTER_EMBEDDING_DIMENSION` env vars if a cluster deployment wants a different local model (e.g. smaller for low-RAM hosts)
- Single model across the cluster so cross-kind semantic search works: one query vector, many indices (papers + memories + news chunks share the same vector space)
- All schema `vector(N)` columns use the configured dimension (`vector(1024)` by default)

### Phase 0 — Foundations (no behaviour change)

Protocol + infra foundations land together so later phases don't keep churning the core:

- **Slim `KindSpec`, `CallContext`, `HintContext`, `PrecisError`** in `protocol.py`.
- **Plugin protocol version.** Add `PLUGIN_PROTOCOL_VERSION = "1"` constant. Plugins declare compatible range; precis refuses to load incompatible majors with a clean error (no silent failure).
- Extend `Plugin` with optional `kinds: list[KindSpec]`. Plugins without it still load — registry synthesises a default spec per scheme.
- Add `cost_of()` and `hints()` default implementations to `Handler`.
- Add `invoke_handler()` wrapper in `registry.py`: exception isolation + unified `_format_error()` + hint aggregation + response footer + gripe-hint emission on non-user errors. State tracking hooks reserved but not implemented.
- Add `KINDS: dict[str, RegisteredKind]` and `ALIASES: dict[str, str]` alongside `SCHEMES` / `FILE_TYPES`.
- Standard error catalogue (§11.3) implemented as a frozen enum.
- **Tool-schema translation layer**: tool params named `type=`; handler code receives `kind` kwarg. One-line adapter in the tool wrapper.
- **`precis-logger` writer library shipped alongside.** Small separate package: structlog-based writer, stderr + optional `CLUSTER_LOG_FILE` output, zero PG deps. Precis imports it as a hard dep and replaces ad-hoc logging. See Phase 10 for the shipper/reader side.
- **`precis-core` package shipped alongside.** Absorbs what was `acatome-store` and adds the other cluster-wide primitives: URI types, plain SQL schema files (`acatome.*`, `journal.*`, `news.*`, `links`, `gripes`), thin Python store classes per schema, `LinkStore` API, shared chunker (`split_text` hoisted from `acatome-extract`), shared embedding config (local `sentence-transformers` + `BAAI/bge-m3` @ 1024 dim). Both `precis-mcp` and the ingest tools (`acatome-extract`, `precis-news-ingest`) depend on it. **No migration framework in v1** — schemas are applied via idempotent `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`. Formal migrations deferred until the first schema change needs to move real data (§19 Future).
- **`GripeHandler` scaffolded as a state-backed kind.** Writes two things per `put(type='gripe', ...)`: (a) a `gripe_filed` log event via the writer library (so it shows up in trace views); (b) a JSON-line record to `/var/log/cluster/gripes.jsonl` that the shipper will eventually lift into `cluster.gripes`. Handler does not touch PG directly. Schema for `cluster.gripes` ships in `precis-core` as a plain SQL file; precis-mcp applies it idempotently at PG connect.
- **PG-reachability probe at startup.** On boot, precis-mcp attempts a connection to `CLUSTER_DATABASE_URL`. Success → register state-backed kinds. Failure → hide state-backed kinds, emit stderr warning per §6.2, continue serving stateless kinds. No crash.
- Tests: existing tests pass; new tests for `KindSpec` default synthesis, `invoke_handler` error formatting, hint aggregation caps, alias resolution, protocol-version incompat, tool-param `type` ↔ internal `kind` mapping, gripe write path (file + log event), gripe-hint emission on handler crash, PG-unreachable startup behaviour.

### Phase 1 — Capability-driven enum + masking

- Tool schemas derive `type` enum from `KINDS` keys filtered by `PRECIS_KINDS`.
- `PRECIS_KINDS` parser supporting bracket syntax (§13) replaces `PRECIS_DISABLE_PLUGINS` as the agent-scoping primary; old var kept as deprecated alias for one release.
- **Fatal-error paths** for alias-in-config, unknown verbs, empty brackets, duplicate kinds, kind collisions across plugins.
- `_to_uri()` learns `type=` → scheme routing.
- Alias resolution at URI parse (hidden from enum, accepted at runtime only).
- Tool descriptions auto-generate the one-line-per-kind table from `KindSpec.description`.
- Existing builtins explicitly annotated with `KindSpec`.
- `/stats` view implemented with startup-warnings list.
- Tests: bracket parsing; alias resolution at URI; alias rejection in config; enum filtering; per-tool enum variation; plugin-collision fatal error.

### Phase 2 — Cost reporting

- `cost_hint` field on `KindSpec`.
- Response footer formatter (always present, including free kinds).
- Session totals via in-process counter.
- Cost block folded into `get(id='/stats')` — no separate `/cost` URI.
- Tests: footer present on all calls; free kinds show $0; session accumulation; `/stats` shape.

### Phase 3 — Perplexity 3-mode split + Hermes profile rollout

- Register `web` (Sonar), `think` (Sonar Reasoning Pro), `research` (Sonar Deep Research) as distinct kinds sharing the `WebHandler` class but with different `KindSpec` and model endpoints. `news` is NOT in this set — it's state-backed (Phase 11), not a Perplexity mode.
- Update Hermes profiles in `ansible/roles/hermes/templates/profiles/` to use bracket-syntax `PRECIS_KINDS` for every agent.
- Deprecate per-service MCP entries (`wolfravant-mcp`, `tubescribe-mcp`) from profiles that get the same capabilities via precis.
- `feynman-mcp` stays as a separate dispatched service invoked via hermes, not via precis. Precis surfaces the hint (§6.1).
- Playbook: `ansible-playbook playbooks/21-hermes.yml`.
- Tests: each perplexity kind routed to correct endpoint with correct cost_hint.

### Phase 4 — External stateless handlers

Pyproject updates (monolith, no optional-dependencies):

```toml
[project.dependencies]
httpx = ">=0.27"
wolframalpha = ">=5.0"
youtube-transcript-api = ">=1.0"
python-docx = ">=1.1"
feedparser = ">=6.0"             # for conversation-ingest and future use
psycopg = {version = ">=3.2", extras = ["binary", "pool"]}
pgvector = ">=0.3"
sentence-transformers = ">=2.7"  # local embeddings via precis-core
precis-logger = ">=0.1"          # writer library; structlog-based, zero PG deps
precis-core = ">=0.1"            # URI types, all schemas, stores, chunker, embedder
```

- Implement `WikiHandler`, `YouTubeHandler`, `UrlHandler`, `MathHandler`, `CodeHandler`. Each declares its `KindSpec` with `requires` env vars and a `cost_hint` string.
- `GripeHandler` scaffolded in Phase 0; remains state-backed (hidden when PG unavailable).
- Existing separate MCPs (`wolfravant-mcp`, `tubescribe-mcp`) stay installable; eventual deprecation once precis reaches parity.

### Phase 5 — Paper id auto-detection

- `_classify_paper_id()` helper in `precis/handlers/paper.py`. Requires PG + `cluster.acatome.*` populated; registered only if both are present at startup (§6.2).
- Paper handler delegates data access to `precis_core.stores.acatome`; uses `precis_core.links.LinkStore` for citation edges.
- Accepts DOI / arXiv new / arXiv old / PMCID / ISBN-13 / ISBN-10 hyphenated / ISSN / explicit prefix / slug.
- Ambiguous bare digits → slug lookup first; miss → hint toward `pmid:` / `isbn:`.
- Storage: nested `identifiers` field on paper refs (owned by the `acatome` schema in precis-core).
- External identifier resolution at ingest time only (OpenLibrary → Google Books for ISBN/ISSN; CrossRef for DOI; arXiv for preprints). Happens in `acatome-extract`, not in precis.
- **Migration of existing `acatome-store` package**: contents move into `precis-core.stores.acatome`. Update `acatome-extract` and `acatome-meta` imports. Drop the `acatome-store` package. Ansible role for paper ingestion updated.
- Tests: every id format resolves correctly; collisions fail gracefully; error messages hint usefully; `paper` absent from enum when PG unreachable; migrated acatome tests still pass under new import path.

### Phase 6 — Journal kinds (memory / todo / flashcard / conversation)

- Journal schemas (`cluster.journal.*`) ship in `precis-core` as plain SQL, applied idempotently by precis-mcp at first PG connect.
- `MemoryHandler` — verbatim drawers with tags (slug-based ids), pgvector search, `/recent` and `/wake-up` views. Flat, no wings/rooms.
- `TodoHandler` — existing code relocated into the monolith. **Integer ids** (§7.1); relocate any slug-based references.
- `FlashcardHandler` — existing code relocated into the monolith. **Integer ids** (§7.1).
- `ConversationHandler` — new. Streamed-live and batch-ingested session transcripts, `/session/N` and `/recent` views. Id format: session timestamp slug or uuid.
- Put modes per-kind as documented in §4.3 and extensions.
- All four kinds auto-hide if PG unreachable (§6.2).
- Tests: tag search, dedup, wake-up view shape, session streaming, state-machine transitions on todo (integer ids), SM-2 on flashcard (integer ids), id-type validation rejects slug on integer-id kinds and vice versa.

### Phase 7 — Links (cross-cutting primitive)

- Schema + `LinkStore` API in `precis-core` package (landed in Phase 0):
  - Postgres schema file for `cluster.links`, applied idempotently.
  - `LinkStore` protocol with `add_link`, `remove_link`, `links_from(uri)`, `links_to(uri)`.
- `precis-mcp` wires the store to the cluster DSN and exposes link operations on every state-backed kind.
- `acatome` store (now inside precis-core) records citations via the same `LinkStore`, replacing any internal citation table.
- `put(mode='link' | 'unlink')` on any stored kind → forwards to `LinkStore`.
- `/links` and `/links-in` views on every stored kind.
- Cross-kind traversal shows unlinked-target notes (§9.4).
- Tests: bidirectional lookup, dedup, cross-kind links, absent-target behaviour, link removal, acatome citations appear via same API.

### Phase 8 — Hints + notifications + context-aware responses

- `Handler.hints()` implementations for paper, todo, memory, web, think, research at minimum.
- Result-count-driven heuristics in `invoke_handler()` populating `HintContext`.
- Hint cap, dedup, ranking (§11.4) enforced in aggregator.
- `Handler.notifications()` implementations for state-backed kinds that have "current business" to surface: todo (`/today`, `/overdue`), flashcard (`/due`), gripe (`/recent` for admins), conversation (none for now). Stateless handlers return `[]`.
- Tool-description builder aggregates notifications across all registered handlers; prepends a single "Notifications:" block to the tool docstring only if the combined list is non-empty (§5.5). Per-kind cap 3, overall cap 10.
- Tests: hints appear only when thresholds crossed; silent on sweet-spot results; zero-result relaxation hints; too-many pruning hints; hint cap at 5. Notifications appear in tool description only when non-empty; absent entirely on a fresh empty system; per-handler and overall caps enforced; stateless kinds contribute nothing.

### Phase 9 — Long-running operations + `deliver_to=`

- MCP progress notifications emitted for `research` (synchronous with progress, default).
- Non-standard `deliver_to=memory:<drawer>` param for `search(type='research')`: returns immediately with a job id, result arrives as a tagged memory drawer. Drawer auto-created with `tag=inbox` if it doesn't exist.
- `get(type='research', id=<job_id>)` view to check status or retrieve result.
- Hints surface `deliver_to=` only when call latency would matter (e.g. research with depth≥full).
- For tasks longer than research (hours/days), precis surfaces a hint pointing to `hermes dispatch`. No `feynman` kind in precis.
- Tests: progress notifications fire at expected intervals; deliver_to lands as drawer; status lookup works; long-running hint appears on deep-enough queries.

### Phase 10 — Cluster log shipper + `log` kind reader

Writer library landed in Phase 0; this phase adds the shipper, the `cluster.logs` schema, and the `log` kind reader.

- **`logs` schema** applied to `cluster` DB as plain SQL (table + indexes per §6.6), idempotent. Top-level `logs` table (not schema-namespaced — high-volume, keep it flat). Retention job (`pg_cron`, 30 days) installed.
- **`precis-log-shipper` package** — separate deploy, systemd service:
  - Tails `/var/log/cluster/*.jsonl` files (including `gripes.jsonl` from Phase 0).
  - Config: `/etc/precis-log-shipper/sinks.yml` with pluggable sinks. V1 supports `pg` sink; `file`, `loki`, `http` sinks stubbed with clean extension points.
  - Bulk-inserts to PG sink (batch size / flush interval configurable).
  - Per-file offset state in `/var/lib/precis-log-shipper/offsets.json` for restart safety.
  - Rotation-follow via inode tracking; `logrotate` handles actual rotation.
  - Ansible role creates `/var/log/cluster/`, installs shipper service + logrotate config + PG credentials for the `log_shipper` role.
- **`LogHandler` in precis-mcp monolith** (no separate reader package):
  - Canonical views `/recent`, `/errors`, `/service/<name>`, `/trace/<trace_id>`, `/event/<event_name>`.
  - `search(type='log', query=..., service=..., level=..., since=...)` — payload filters via JSONB operators, full-text via `event` and `payload::text`.
  - Read-only (no `put`).
  - Registered only if PG is reachable at startup.
- **`GripeHandler` read/search wired up** — `get(type='gripe', ...)` and `search(type='gripe', ...)` read from `cluster.gripes` (populated by shipper). Write path unchanged from Phase 0 (file + log event, shipper moves to PG).
- Migrate other cluster services to the writer library — hermes, sortie, acatome-extract, acatome-meta, precis-core. One-shot refactor; straightforward.
- Tests: shipper tailing and offset persistence; rotation handling; bulk-insert batching; sink pluggability (stub non-pg sinks); `log` kind search and view paths; `/trace/<id>` correlates across services; gripes land in `cluster.gripes` via shipper.

### Phase 11 — `news` aggregator

- **Schema** (`cluster.news.*`, owned by `precis-news-ingest` as a plain SQL file):
  - `articles` + `article_chunks` tables per §6.5 sketch.
  - pgvector extension required (already present per acatome).
  - Schema applied idempotently by the ingest service on start; precis-mcp reads only.
- **`precis-news-ingest` service** (separate deploy, systemd timer):
  - Config file listing RSS feeds by source name and URL; per-source scraper overrides.
  - Polls feeds, fetches article bodies (if scraper exists), chunks body (~512 tokens), embeds chunks, writes to `cluster.news.*`.
  - Dedups on `(source, source_url)` before insert.
  - Scrapers are opt-in Python modules under `precis_news_ingest.scrapers.<name>`. RSS-first; scrapers fill in body when RSS summary is thin.
- **`NewsHandler` in precis-mcp monolith** — reads from `cluster.news.*` via vector search on chunks → aggregation by article. State-backed kind (hidden when PG unreachable; no Perplexity fallback). Agents wanting same-day web news without the aggregator should use `web` with a "today" qualifier.
- Views: `/recent`, `/source/<name>`, `/today`, `/topic/<tag>`.
- Tests: RSS parse and dedup; chunker produces expected windows; embedding pipeline roundtrip; scraper plugin loading; news handler absent when PG unreachable; view endpoints.

### Phase 12 — `quest` kind (fold `acatome-quest-mcp` into precis)

**Status: sketch.** Design captured from the initial fit discussion; refine once Phases 5–11 are done (schema details, API consolidation shape, and notification/hint wording will all benefit from the groundwork those phases lay).

**Motivation.** `acatome-quest-mcp` manages the paper-request lifecycle between "agent wants a paper we don't have" and "`acatome-extract` ingests a PDF dropped in the inbox". It already uses Postgres and an out-of-band runner daemon — exactly the shape of a precis state-backed kind. Folding it in removes one MCP from every agent's stack and gives the new surface clean integration with notifications, hints, and links.

**Scope of this phase.** Retire the MCP layer of `acatome-quest-mcp` entirely (no separate stdio server, no agent-facing CLI — that's work for the agent via precis). Keep a headless `acatome-quest-runner` as the polling daemon only. Precis gets a new `quest` kind. Human PDF drops stay on the runner CLI side (`acatome-quest-runner submit-file --path ...`), so the agent-facing MCP surface never has to pass base64.

**Kind mapping** (from the existing 4 tools):

| Quest tool today | New `quest` kind |
|---|---|
| `submit(ref)` | `put(type='quest', text='<doi>')` or `put(type='quest', ref={...})` |
| `status(id)` | `get(type='quest', id='<id>')` |
| `status(filter={status:needs_user})` | `get(type='quest', id='/needs-user')` |
| `update(id, mode='confirm', choice=0)` | `put(type='quest', id='<id>', mode='confirm', choice=0)` |
| `update(id, mode='repoint', doi=...)` | `put(type='quest', id='<id>', mode='repoint', doi=...)` |
| `update(id, mode='flag', code=...)` | `put(type='quest', id='<id>', mode='flag', code=...)` |
| `update(id, mode='priority', priority=5)` | `put(type='quest', id='<id>', mode='priority', priority=5)` |
| `update(id, mode='cancel')` | `put(type='quest', id='<id>', mode='cancel')` |
| `submit_file(url, request_id)` | `put(type='quest', id='<id>', mode='file', url=...)` — URL only, base64 stays CLI-side |

**Views:** `/recent`, `/queued`, `/needs-user`, `/failed`, `/ingesting`, `/document/<file>` (requests tied to a source doc via `source={document, line}`), `/agent/<id>` (by `created_by`).

**Get shapes on an individual quest:**

- `get(type='quest', id='<id>')` — one request with resolved metadata + current status
- `get(type='quest', id='<id>/candidates')` — disambiguation options (for `needs_user` with multiple resolver hits)
- `get(type='quest', id='<id>/misconceptions')` — flags on this request (`doi_invalid`, `retracted`, `duplicate_of`, …)

**Search:** `search(type='quest', query='anion exchange membranes', status='needs_user', created_by='asa')` — text over title/authors, filter by status/creator/source.

**Schema location.** Promote to its own schema: `cluster.quest.*` (with a `requests` table plus supporting side-tables). Keeps lifecycle rows out of `cluster.acatome.*` where ingested paper content lives — different retention, different access patterns. Schema ships in `precis-core` alongside `acatome.*`, `journal.*`, `news.*`, `links`, `gripes`. Idempotent `CREATE TABLE IF NOT EXISTS`; no migration tooling in v1 (§19).

**External-metadata consolidation.** Resolver calls (Crossref, Semantic Scholar, arXiv, Unpaywall, OpenAlex, Europe PMC) go into **one metadata module** so the stack can answer meta-questions about papers too — "cites", "cited-by", "related by author", "retracted?", "preprint-of?". Three options to pick from in the refinement pass:

1. Extend the existing `acatome-meta` package (today: Crossref + arXiv + OpenLibrary + Google Books) with Unpaywall/OpenAlex/S2/EPMC. Quest + paper kind both import it.
2. Spin a new `precis-meta` package (fresh start, both acatome-meta and quest consume it). More work, but cleaner naming in the precis-* namespace.
3. Fold into `precis-core.meta`. Minimises package sprawl but bloats precis-core with upstream API clients that are not strictly "data layer".

*Leaning 1 for now — extend `acatome-meta`, keep its name, let the precis-* umbrella tolerate one acatome-prefixed infrastructure package since it predates the namespace choice. Revisit in the refinement pass.*

**Meta-question views on the `paper` kind** (this phase, not earlier): once the metadata module lands, `paper` handler gains `/cites`, `/cited-by`, `/related`, `/versions` views backed by the same citation graph. Requires the citation edges to be materialised into `cluster.links` at ingest time — coordinate with Phase 5 (paper id detection) and Phase 7 (links) on whether to populate now or backfill here.

**Notifications + hints (the whole point of §5.5 + §11.4).**

- `QuestHandler.notifications(ctx)` → `["3 quests need user input → get(type='quest', id='/needs-user')"]` at session start when the backlog is non-empty. Silent otherwise.
- `QuestHandler.hints(result, ctx)` on misconception-flagged results → surface the concrete next action: `"consider repoint: put(id='<id>', mode='repoint', doi='<corrected>')"` for `doi_title_mismatch`, `"retracted per S2 — mark cancel or flag"` for `retracted`, etc. These hints are already in the misconception codes table; just plumb them through.

**Links integration.** On successful resolution, the runner creates a link `quest:<id> → paper:<slug>` with relation `resolved_to`. Agents on a paper can trace back "how did this arrive here?" via `get(type='paper', id='<slug>/links-in')`. Free via Phase 7's `LinkStore`.

**Package changes.**

- **Retire:** `acatome-quest-mcp` (the FastMCP package). Replaced by the `quest` kind in `precis-mcp`.
- **Rename + trim:** `acatome-quest-runner` = the polling daemon + human CLI (`report`, `runner`, `reconcile`, `submit-file --path`). Ansible launchd/systemd unit points here; no MCP entry point.
- **Extend:** `acatome-meta` with Unpaywall/OpenAlex/S2/EPMC (pending refinement decision on consolidation shape).
- **Extend:** `precis-core` with `cluster.quest.*` schema + `QuestStore` Python wrapper.

**Tests.** Existing quest tests migrate with the handler. New tests for: state-backed kind hidden when PG unreachable; views filter correctly; put modes dispatch to store mutations; notifications emit only when backlog > 0; hints appear for each misconception code; link edge materialises on `ingested` transition.

**Refinement-pass open questions** *(revisit right before starting this phase)*:

- Schema detail — current `papers.requests` fields vs. what we actually need once citations are materialised as links (might drop `candidates` JSONB if we use side-tables).
- Metadata module consolidation shape — which of the 3 options above.
- Paper-kind meta views (`/cites`, `/cited-by`, `/related`) — land in Phase 5 or here?
- `submit_file` URL path security — the existing quest validates PDF magic bytes + rejects HTML; reconfirm that holds for arbitrary `put(mode='file', url=...)` from an agent.
- Runner polling cadence + per-agent cap defaults — inherit from quest's today (`QUEST_POLL_INTERVAL=30`, `QUEST_MAX_OPEN_PER_AGENT=50`) or tune.
- **Quest is really a tool surface + ≥3 skills + a policy layer.** The 4-verb queue (`submit` / `status` / `update` / `submit_file`) is atomic; the agent-facing value sits in three skills — *find-paper*, *triage-backlog*, *handle-dropped-pdf* — plus a cross-cutting rule layer (OA-only, retractions terminal, no fabrication). Today those skills live in `grimoire/agents/quest-agent.md` (an agent prompt) and `ansible/roles/feynman/templates/skills/cluster-library.md.j2` (a deploy-time skill file). Folding the tool layer into precis is cheap; the skill layer wants a first-class home — see Phase 12b below.

### Phase 12b — `skill` kind + skill-surfacing hooks

**Status: sketch.** Aligned with the de facto Agent Skills standard (Anthropic Claude Code, adopted across Cursor, Gemini CLI, Warp, community tooling). Designed alongside Phase 12; lands either with it or immediately after. Standalone from Phase 12 in principle — any kind with a non-trivial workflow benefits.

**Motivation.** Phase 8 gave precis structured errors + reactive hints. Phase 7 gave us links. What's missing is a *first-class home for skills* — the recipes that compose primitives into agent-facing workflows. Quest is the clearest case (tool surface + 3 skills), but `flashcard` (SM-2 review is non-obvious), `tex` (raw-file access pattern), and multi-kind compositions like "research a paper" (precis → web → quest) all want somewhere to live that isn't a system prompt or an Ansible template.

**Alignment with the ecosystem.** The Agent Skills standard is a filesystem convention: each skill is a directory containing a `SKILL.md` file with YAML frontmatter (`name`, `description`, optional `argument-hint`, `user-invocable`, `allowed-tools`, `path-scoping`) plus a markdown body and optional `references/` / `scripts/` subfolders. Claude Code scans `~/.claude/skills/` + `.claude/skills/` at session start and injects only `name + description` into the system prompt; the full body loads on-demand via a Skill tool when the agent decides the description matches. Precis adopts this format verbatim so every Claude Code skill works in precis out of the box, and vice versa.

**What precis adds on top**: a corpus surface (`get(type='skill')` / `search(type='skill')`) with ranked semantic search, linking to kinds via Phase 7 edges, in-band annotation, and state-triggered surfacing that the agent-side-only Claude Code model cannot do.

**v1 scope (filesystem-native, no PG):**

- `SkillHandler` with scheme `skill:`, reads from the filesystem. Scan paths in precedence order: `./skills/` (project-local), `~/.precis/skills/` (user-global), `~/.claude/skills/` (ecosystem interop; read-only from precis's side).
- Parses standard `SKILL.md` YAML frontmatter — same field names as Claude Code.
- Writes go to `~/.precis/skills/` only (precis respects the ecosystem; doesn't mutate other tools' directories).
- Dual export: `get(type='skill')` for precis-native clients, MCP `prompts/list` + `prompts/get` adapter for plain MCP clients.
- `/help` view on every `RefHandler` / `FileHandlerBase` — returns the handler's declared `onboarding_skill` body inlined.
- `Handler.onboarding_skill: str | None` class attribute — kinds with non-trivial workflows declare their entry-level skill slug.
- `CallContext.seen_kinds: set[str]` — gate for first-call onboarding injection.
- `_enrich_error` next-hint extension — on the agent-confusion codes (`PARAM_INVALID`, `MODE_UNSUPPORTED`, `VIEW_UNKNOWN`) the enricher appends `see get(id='skill:<onboarding_skill>')` when the handler declares one.

**v1.1 scope (state-triggered surfacing, precis extension):**

- Parse precis extensions in the SKILL.md frontmatter: `applies-to: [quest, paper]` and `state-trigger: {kind: quest, condition: "needs_user_count > 0"}`.
- `Handler.notifications(ctx)` reads state and emits skill pointers when thresholds met (e.g. `5+ todos → prompt:todo-triage`).
- Auto-materialise `skill:X ─[applies_to]→ kind:Y` edges in the Phase 7 link graph on startup scan.
- Ship seed skills for the kinds declared below.

**v1.2 scope (corpus-backed authoring, later):**

- `cluster.skills.*` PG schema for versioning, draft/active status, review workflow.
- `put(type='skill', text='…', title='…')` authoring via agents — lands as `status='draft'`, operator promotes to `active` after review.
- Indexed by pgvector for scaled search when the skill library grows past ~100.
- Defer until the filesystem approach shows its limits (probably never, for a single-operator cluster).

**Kind mapping (v1 operations):**

| Operation | Shape |
|---|---|
| Render a skill | `get(id='skill:find-paper')` — full SKILL.md body |
| Parameterised render | `get(id='skill:find-paper', doi='10.x/y')` — frontmatter declares `argument-hint` |
| Skill metadata | `get(id='skill:find-paper/meta')` — frontmatter as a dict |
| Skills by kind | `get(id='skill:/kind/quest')` — all skills whose frontmatter has `applies-to: [quest, …]` |
| Skills by topic | `get(id='skill:/topic/papers')` — tagged via frontmatter |
| Newly added | `get(id='skill:/recent')` — by mtime |
| Search | `search(type='skill', query='how do I acquire a paper')` — ranked over `name + description` (v1 grep; v1.2 pgvector) |
| Author | `put(type='skill', text='…', title='…')` — writes a new SKILL.md under `~/.precis/skills/<slug>/` |
| Annotate | `put(id='skill:find-paper', mode='note', text='…')` — appends a note file in the skill directory (v1.2 PG) |
| Link | auto-materialised from `applies-to` frontmatter at scan time |

**Standard SKILL.md shape (verbatim frontmatter from the Agent Skills convention, plus precis extensions):**

```markdown
---
name: find-paper
description: >
  Acquire a scientific paper given a DOI, arXiv id, or title.
  Use when user asks "can you get this paper" or mentions a DOI.
argument-hint: [doi, arxiv-id, title]
user-invocable: true            # enables /skill command
allowed-tools: [get, put, search]
path-scoping:                   # optional — limits scope
applies-to: [quest, paper]      # precis extension — materialises to link graph
state-trigger:                  # precis extension — wires notifications()
  kind: quest
  condition: needs_user_count > 0
kind-onboarding: quest          # precis extension — this skill is kind quest's onboarding
---

## When to Use
- Triggers: "get this paper", "acquire DOI 10.x/y", "is <paper> in the library"

## Steps
1. Check precis-papers first — `get(type='paper', id='<slug>')`
2. If absent, normalise to structured ref (doi/arxiv/title)
3. `put(type='quest', text='<doi>')` to enqueue

## Output Format
One-line outcome + detail + next action (see ../grimoire/agents/quest-agent.md)
```

Claude Code / Cursor / Gemini CLI ignore the precis-extension fields (`applies-to`, `state-trigger`, `kind-onboarding`) — they're additive, not breaking.

**Dual export.** `SkillHandler` is the source of truth. Precis's MCP server exposes:
1. `get(type='skill', id='<slug>')` — precis-native surface (ranked search, link graph, annotation)
2. Standard MCP `prompts/list` → enumerate all SKILL.md with `user-invocable: true`, return `name + description` per the MCP prompts spec
3. Standard MCP `prompts/get` → return the full body, mapping `argument-hint` fields to MCP prompt arguments

Plain MCP clients (Claude Desktop, Cursor) see the prompts surface; precis-native clients see the richer kind. Same files, two reads.

**Skill-surfacing hooks.** Five triggers, three reuse existing Phase 8 hooks:

| # | Trigger | Hook | Payload |
|---|---|---|---|
| 1 | First use of the kind this session | `Handler.notifications(ctx)` gated on `ctx.seen_kinds` | pointer: `first time using quest? get(id='skill:find-paper')` |
| 2 | Kind state meets threshold (5+ open todos, `needs_user` backlog, 10+ overdue flashcards) | `Handler.notifications(ctx)` reading kind state; wired from frontmatter `state-trigger` in v1.1 | pointer: one line per triggered skill |
| 3 | `PARAM_INVALID` / `MODE_UNSUPPORTED` / `VIEW_UNKNOWN` on the kind | `_enrich_error` `next=` slot | appends `see get(id='skill:<onboarding_skill>')` when the handler declares one |
| 4 | Explicit help request | new `/help` view on every `RefHandler` / `FileHandlerBase` | **full SKILL.md body inlined** |
| 5 | *(Deferred)* Pattern-triggered — agent repeats primitives where a batch skill exists | — | Future; speculative until we have the trajectory data to justify it |

**Pointer-granularity on auto-trigger; full text on explicit pull.** Agents that already know the skill pay zero tokens; agents that need it grab the full thing in one `get(id='…/help')` or `get(id='skill:<slug>')`. Keeps the auto-inject cost to a single line per trigger.

**New handler class attributes (opt-in, default `None`):**

```python
class QuestHandler(RefHandler):
    scheme = "quest"
    onboarding_skill = "find-paper"        # → get(id='skill:find-paper'); triggers 1, 3, 4
    policy_skill = "quest-policies"        # cross-cutting rules (v1.1)

    def notifications(self, ctx):          # trigger 2 (handler-owned in v1.1)
        needs_user = self._count_status("needs_user")
        if needs_user == 0:
            return []
        notes = [f"{needs_user} quests need user input → get(id='quest:/needs-user')"]
        if needs_user >= 3:
            notes.append("disambiguation skill: get(id='skill:quest-disambiguate')")
        return notes
```

One attribute declaration powers triggers 1, 3, and 4; `notifications()` owns the state-dependent logic for trigger 2 (v1.1).

**New `CallContext` field.** `seen_kinds: set[str]` — tracks which kinds this session has already touched. In-memory, keyed by `ctx.session_id`, no persistence. Session reset → fresh onboarding, which is the right default (new context = worth re-pointing).

**Sensible v1 defaults per kind.** Handlers declare their own thresholds; cluster-level config can override later.

| Kind | `onboarding_skill` (v1) | State trigger (v1.1) | State-triggered skill (v1.1) |
|---|---|---|---|
| `quest` | `find-paper` | any `needs_user` | `quest-disambiguate` |
| `flashcard` | `sm2-basics` | overdue ≥ 10 | `spaced-review` |
| `todo` | *(none — CRUD is obvious)* | open ≥ 5 | `todo-triage` |
| `paper` | *(none)* | corpus ≥ 20 refs | `library-search` |
| `tex` | `tex-workflow` | `\bibliography` present | `citations` |
| `memory`, `conversation`, `web`, `math`, `youtube` | *(none)* | — | — |

Simple kinds declare nothing; the hook is silent. Only kinds with genuinely non-trivial workflows opt in.

**Notification shape upgrade path.** v1 returns `list[str]` (prose with slugs embedded) on the existing infrastructure. v2 promotes to a structured `Notification` dataclass carrying `text`, `skill_pointer: str | None`, and `severity: Literal["info", "action_needed"]` — once the pattern consolidates and we want machine-parseable skill pointers. Start v1, upgrade when usage justifies.

**Authoring loop.**

- **v1**: skills live as SKILL.md directories under `precis-core/skills/` (deploy-time seed set for each kind's onboarding + state-triggered skills), `~/.precis/skills/` (user-global), and `./skills/` (project-local, git-committed). PR review for changes.
- **v1.2**: agent authoring via `put(type='skill')` writes to a PG-backed corpus with `status='draft'`, operator promotes to `active` after review. Same curation shape as gripes (Phase 9) — reuse that review UX.

**Tests.**

- `SkillHandler` scan: finds SKILL.md across configured paths, precedence order respected on name collisions, invalid YAML logged but not fatal.
- Frontmatter parser: required fields (`name`, `description`) enforced; optional fields tolerated; precis extensions (`applies-to`, `state-trigger`, `kind-onboarding`) parsed into structured form.
- Views: `/recent` (by mtime), `/kind/<scheme>` (filter by `applies-to`), `/topic/<tag>` (filter by tags), `/meta` (full frontmatter dump).
- Dual-export: MCP `prompts/list` includes only `user-invocable: true` skills; `prompts/get` returns body identical to `get(id='skill:<slug>')`.
- Onboarding injection: fires exactly once per `session_id` per kind; silent on repeat calls within a session.
- `/help` view: returns full SKILL.md body of the handler's `onboarding_skill`, raises `ID_NOT_FOUND` if declared skill doesn't exist on disk.
- `_enrich_error` skill pointer: appended only for `PARAM_INVALID` / `MODE_UNSUPPORTED` / `VIEW_UNKNOWN`; never on `ID_NOT_FOUND` (that needs search, not a primer) or on infra codes.
- v1.1: state-triggered injection fires when threshold met, silent below; frontmatter `state-trigger` parsed and wired to the right handler's `notifications()`.
- v1.1: auto-materialised `applies-to` edges appear in link-graph queries.

**Refinement-pass open questions** *(revisit before starting this phase)*:

- **Trigger eval sandbox** — `state-trigger.condition` needs a safe expression language (something narrower than Python eval). Options: ast-based allowlist evaluator (simple), jmespath/jq (query-style), hardcoded predicate names per kind (no user code). Start with hardcoded names in v1.1; expand if the skill library demands.
- **Dual-export argument mapping** — MCP `prompts` `arguments` field maps from our `argument-hint` list, but MCP wants names + descriptions + required flags. Extend frontmatter with structured `arguments: [{name: doi, description: "…", required: true}]` when we need more than the simple hint list.
- **Per-agent skill sets** — should some skills be hermes-only / feynman-only? Cheap implementation: add `audience: [hermes, feynman]` frontmatter field, filter at `notifications()` on `ctx.agent_id`. Defer until we actually need it.
- **Skill versioning** — filesystem model: versions branch via new slugs (`find-paper`, `find-paper-v2`) rather than in-place edits. v1.2 corpus model adds draft/active status atop that.
- **Policy-skill vs onboarding-skill** — onboarding is a *workflow*; policy is a *rule set* (retractions terminal, OA-only). Different enforcement shape: workflow = "read this when you're new"; policy = "enforced at handler level with specific error codes + pointed at from policy-violating errors". Probably worth a distinct slot on the handler (`policy_skill`) that gets pointed at only from policy-violating errors, not from onboarding.
- **Claude-Code-interop edge cases** — Claude Code skills sometimes use `disable-model-invocation: true` and other fields we don't honour. Decision: parse but ignore; precis treats them as no-ops. Document the subset we act on.

---

## 17. Open issues

Items settled in conversation have moved to §16 (Alteration plan), §18 (Non-goals), or §19 (Future). Remaining open items, grouped by category.

### A. Design decisions still open

1. **Memory capture of external results.** Manual for v1 (agent copies text, calls `put(type='memory')`). Auto-capture (`put(type='memory', from='web:last')`) is Future if needed.
2. **Conversation ingest mode.** Stream-live (every turn appends a drawer) vs batch-ingest (post-session digest) — both, or pick one default? Leaning: both, with stream-live as default and a post-session summarizer that compresses to `memory`.
3. **`deliver_to=` drawer semantics.** `memory:<drawer-id>` URI form — auto-create with tag `inbox` if missing (locked). Confirm whether the agent can name the drawer arbitrarily or must match a pattern like `inbox/*`.
4. **Research job id lifecycle.** How long does `research:<job_id>` remain retrievable? Session only, or persisted? Leaning: session-local, expires at process restart. Deferred until real use.

### B. Defaults to pick

5. **Handler timeouts.** Proposed: 30s external quick (web, wiki, url, news, math, code, gripe), 60s medium (think), 10s local (paper, memory, todo, flashcard, log), 600s for `research` (upstream has its own ceiling). Review per-kind.
6. **Search `top_k` default.** Proposed: 10 standard; 5 for paper/web; 20 for memory; 30 for log (short rows).
7. **Hint thresholds.** Proposed: >50 results for "too many"; >100 for get-list.
8. **Depth default.** Current precis uses `0 = all`. Keep.

### C. Edge cases and polish

9. **Paper DOI collisions.** Slug is canonical local id; DOI is metadata. First-match wins on lookup; a maintenance view should surface conflicts. Deferred.
10. **arXiv versioning.** `2301.12345` vs `2301.12345v2`. Preserve user input; v-less returns latest with hint showing available versions.

### D. Shared dependencies / rate limiting

11. **Perplexity rate-limit sharing.** `web`, `think`, `research` share the perplexity API key. Shared `RateLimitState` keyed by upstream-service name so a 429 on one affects all three.
12. **Multi-process API quota.** Multiple precis instances share a key across hosts. Not precis's problem — document only.
13. **Handler-to-kind mapping.** `WebHandler` serves three perplexity kinds; crash affects all three. Hints are per-kind so behaviour stays correct, but **a hard bug (e.g. import error) takes the set down together**. Acceptable; `invoke_handler` init-time isolation mitigates (§15).

### E. Package naming

14. *(Resolved — see Resolved this session: pip namespace is `precis-*`.)*

### F. Async handling

15. **Async handlers.** Sync protocol with a `run_async()` helper (handlers call out to async internally when needed). Commit before Phase 9 lands.

### G. Logs and gripes specifics

16. **Log shipper sinks beyond PG.** V1 is PG only; spec the pluggability interface so `loki`, `vector-http`, `file-backup` sinks can drop in without core shipper changes. Pre-Phase-10.
17. **News ingest cadence.** RSS poll every 15 minutes by default, configurable per-source via `poll_interval_minutes`. Confirm.

### H. Observability conventions

18. **Log structured-field conventions.** Name consistency across services: `type`, `verb`, `service`, `level`, `duration_ms`, `cost_usd`, `trace_id`, `error_code`. Codify in writer-library docs.
19. **Log payload size cap.** JSONB column can hold MB but should we cap? Lean: 64KB per event; truncate with `...truncated` marker. Errors with tracebacks stay under that.

### Resolved this session

The following items are **locked** and folded into §2, §6, §16, or §18:

- **Pip namespace for shared packages** — `precis-*` across the board. Writer library is `precis-logger`; shipper is `precis-log-shipper`; shared data layer is `precis-core`; RSS daemon is `precis-news-ingest`. PyPI verified available for all four. *Cluster-the-concept* stays unchanged — PG database `cluster`, schemas `cluster.acatome.*` / `cluster.journal.*` / `cluster.news.*` / `cluster.logs` / `cluster.gripes`, env vars `CLUSTER_LOG_FILE` / `CLUSTER_LOG_TRACE_ID` / `CLUSTER_LOG_RETENTION_DAYS` / `CLUSTER_DATABASE_URL`, and log directory `/var/log/cluster/<service>.jsonl`. Only pip packaging is rebranded, so non-precis services (hermes, sortie, acatome-extract) import `precis-logger` / `precis-core` as infrastructure without implying ownership (§2, §16 Phase 0).
- **`quest` kind (Phase 12)** — fold `acatome-quest-mcp` into precis as a state-backed kind (§16 Phase 12). Retire the quest MCP entirely; keep `acatome-quest-runner` as a headless polling daemon + human CLI only. Own schema `cluster.quest.*` in `precis-core`. Consolidate resolver APIs (Crossref/S2/arXiv/Unpaywall/OpenAlex/EPMC) in one metadata module so meta-questions like "cites" / "cited-by" / "retracted?" work across the stack (leaning: extend `acatome-meta`, decide in refinement). URL-only `mode='file'` on the MCP surface; base64 PDF drops stay CLI-side. Phase is currently a sketch — revisit right before building.
- **CLI consolidation** — dropped entirely. Single stdio entry point (§18).
- **Logs DB location** — `cluster.logs` table in shared `cluster` DB (§6.6).
- **Log retention** — 30 days via pg_cron, configurable via `CLUSTER_LOG_RETENTION_DAYS` (§6.6).
- **trace_id propagation** — env var + contextvars for v1 (§6.6.1).
- **News dedup key** — `(source, source_url)`; syndicated articles stay separate (§6.5).
- **Journal / News DB model** — single `cluster` DB, schemas `journal.*` and `news.*`. No separate `precis_journal` / `precis_news` databases (§2).
- **Todo / flashcard id format** — integer via `bigserial`. `todo:42`, `flashcard:7` URI forms (§6.4, §7.1).
- **PG-mandatory for state-kinds** — state-backed kinds auto-hide when PG unreachable; stateless kinds work anyway (§6.2).
- **No capability extras on precis-mcp** — monolith with runtime `PRECIS_KINDS` gating (§2, §16).
- **News thin-perplexity-wrapper mode** — dropped. News is aggregator or absent.
- **Gripe write path** — file + log event via writer library; shipper carries to `cluster.gripes`. No dual-mode direct-PG path (§6.7).
- **Gripe → log emission** — every `put(type='gripe', …)` also emits a `gripe_filed` log event, so gripes appear in trace views alongside the triggering error (§6.7).
- **Gripe storage focus** — DB is the destination, `cluster.gripes` table with wide context capture (agent_id, trace_id, context_ref, error_code, payload). File is only an intermediate buffer for the shipper, not a user-facing surface (§6.7).
- **`CLUSTER_LOG_FILE` default path** — `/var/log/cluster/<service>.jsonl` for system daemons; override via env var for dev or per-user (§6.6).
- **Role naming suffix** — `_reader` for read-only roles (not `_r`). Full matrix:

  | Role | Grants |
  |---|---|
  | `acatome_app` | RW on `acatome.*` |
  | `journal_app` | RW on `journal.*` |
  | `news_ingest` | RW on `news.*` |
  | `news_reader` | SELECT on `news.*` (used by precis) |
  | `log_shipper` | INSERT on `logs`, `gripes` |
  | `log_reader` | SELECT on `logs` (used by precis) |
  | `gripe_reader` | SELECT on `gripes` (used by precis) |
  | `links_app` | RW on `links` |

  Codified in ansible role-provisioning task at `roles/postgres/tasks/roles.yml`.

---

## 18. Non-goals

- **Paper progressive enhancement / fallback.** No `paper` kind without PG + `cluster.acatome.*` populated. No CrossRef/arXiv metadata-only mode. Use `research` / `wiki` for papers you don't own.
- **Feynman as a precis kind.** Long-running deep reads don't fit request-response. Precis surfaces a hint; hermes dispatches. Precis is info-fetching up to ~10 minutes.
- **Direct Postgres logging from the writer library.** Writer is file/stderr only. Postgres is a shipper-service concern, never a library runtime dependency.
- **Dedicated Postgres DBs per capability.** One `cluster` DB, many schemas. No `precis_journal`, `precis_news`, `cluster_logs` standalone databases.
- **Capability extras for install-time gating.** No `precis-mcp[acatome]`, `[journal]`, `[news]`, `[logs]`. `precis-mcp` is a monolith. Kind exposure is runtime-gated by `PRECIS_KINDS` + PG reachability (§6.2).
- **Separate `*-store` packages per capability.** No `precis-journal-store`, `precis-news-store`, `precis-log-reader`, and no `acatome-store` either. All schemas, stores, and the chunker/embedder live in `precis-core`; all handlers live in `precis-mcp`. Ingest tools (`acatome-extract`, `acatome-meta`, `precis-news-ingest`) stay separate only because they have independent user-facing value (paper ingestion CLI, metadata lookups, RSS daemon) — they import `precis-core` for data access.
- **`precis-logger` writer library pulling PG deps.** Writer stays tiny (~200 LOC, structlog + stdlib). Every cluster service imports it; adding psycopg there would bloat all their venvs. PG lives in the shipper daemon only.
- **CLI with subcommands.** `precis-mcp` has **one entry point** — the stdio MCP server. No `precis-mcp ingest`, no `precis-mcp stats`, no `precis-mcp list-kinds`. Admin functions are either MCP tools (agents query them), stderr diagnostics at startup (ansible checks exit code), or owned by other packages (`acatome-extract` for paper ingest). Drop the `precis` command; keep only `precis-mcp` as stdio entry.
- **Auto-discovered news scrapers.** Opt-in list in ingest config; no magic site sniffing. RSS-first.
- **Caching layer.** Deferred — every external call is hot for v1. Architecture leaves the hook in `invoke_handler()`.
- **Active / background health probes.** Reactive only, via the wrapper, when it lands later.
- **Structured `CostModel` class hierarchy.** Strings only (`cost_hint`). Machine-parseable cost tracking is Future.
- **Budget guards.** No hard cutoffs. Report only.
- **Wings / rooms / closets hierarchy on memory.** Flat with tags.
- **KG handler** (temporal triples with validity windows). Precis links cover basic needs; temporal semantics are Future.
- **Separate `PRECIS_VERBS` env var.** Single env with bracket syntax.
- **"Admin mode" detection.** No hidden/gated views. `/stats` is always public.
- **Dynamic tool schema reload** (mid-session schema updates). stdio doesn't support it; don't try.
- **Per-user rate limiting.** Upstream services handle it.
- **Proxy / aggregator mode** (precis-to-precis). No demand.
- **Writing to external services.** `put` is internal-only. No "publish to blog" or "post tweet" kinds.
- **Writing to logs via precis.** `log` kind is read-only. Writers are `precis-logger` library calls inside each service; precis doesn't accept log submissions.
- **Writing to news via precis.** `news` kind is read-only. Ingestion is the job of `precis-news-ingest`, not of MCP-dispatched writes.
- **Localization.** English-only tool descriptions.

---

## 19. Future enhancements

Consolidated list of deferred work, grouped by trigger:

**If real operational pain emerges:**

- Reactive health state machine with hysteresis (auto enum drop after sustained failures; use `invoke_handler` hooks).
- Per-kind concurrency caps / backpressure.
- Budget guards (`PRECIS_BUDGET_DAILY_USD`).
- Handler subprocess isolation (stronger than try/except, when a plugin misbehaves badly enough).

**If specific use cases emerge:**

- `PRECIS_CONFIG=*.toml` config file support (Option B in §14).
- URL-path-routed shared-daemon HTTP mode (Option D).
- MCP `initialize`-time client-identity profile selection (Option E).
- Launcher wrapper script templates in Ansible (Option C).
- KG handler (temporal triples with validity windows).
- Auto-capture of external results into memory (`put(type='memory', from='web:last')`).
- `refresh=True` non-standard param on paper (force upstream re-fetch of metadata).
- Multi-backend link tables across separate Postgres instances (only relevant if the single-cluster-DB assumption ever changes).

**Schema evolution (when real data exists to migrate):**

- Formal migration tool (alembic or equivalent) with ordered migration files, version table, forward/backward migration support.
- Per-package migration ownership: `precis-core/migrations/` owns schema changes for `acatome.*`, `journal.*`, `links`, `gripes`. `precis-news-ingest/migrations/` owns `news.*`. `precis-log-shipper/migrations/` owns `logs` schema evolution. Each package applies its own migrations at startup.
- Startup behaviour: precis-mcp refuses to start if its required migrations haven't been applied (prevents silent schema drift).
- Data-preserving changes only via migrations; additive changes (new tables, new nullable columns) can still use idempotent `CREATE ... IF NOT EXISTS` for convenience.
- Trigger for promotion: first schema change that requires touching existing rows (e.g. backfilling a new NOT NULL column, splitting a table).

**Log and observability:**

- Full-text search index on `logs.event` + `logs.payload` with PG's `tsvector` for quick string-match across events.
- Loki / Vector sink as alternative to Postgres (if volume outgrows PG).
- `/metrics` view on `log` kind — rollup by event name, service, error_code for a window.
- Alerting: agent-queryable "recent spike" view (`/anomalies`).
- OpenTelemetry trace integration once services emit spans.
- Log query subscriptions (MCP-streamed `/tail`) — agent watches a service live.

**News:**

- Per-user feed preferences (saved searches, followed sources).
- News → memory auto-handoff on star (`put(type='news', id=X, mode='save')` → drawer with tag `saved-news`).
- Topic clustering across sources (single story, multiple articles).
- Cross-source dedup using content embedding similarity beyond URL/title.

**Paper metadata complexity bucket.** A cluster of related issues to tackle together when real workflow needs arise:

- Retraction surfacing in `/meta` when CrossRef marks it.
- Supplementary information (SI) attachment model for papers with supplements.
- Corrigenda / errata linking.
- Preprint ↔ published-version tracking (arXiv → journal).
- Version history (arXiv v1/v2/v3, updated DOIs).
- Open-access vs paywalled version hints.
- Multiple versions of the same paper (preprint, accepted manuscript, version of record).

**If constrained decoding proves valuable:**

- Single-kind tool specialization — when `len(enabled_kinds) == 1`, rewrite tool description and collapse enum for that tool.

**Transport / perf:**

- Caching with `fresh=True` / `no_cache=True` bypass and per-kind TTLs.
- Async handler protocol (or async-helper wrapper).

**Database indexes and maintenance:**

- HNSW index on `blocks.embedding` is now auto-created by `acatome-store` `_ensure_embedding_column()`. Without it, every vector search does a sequential scan over all embeddings (~1.6s for 750K vectors → ~10ms with HNSW). Fixed in store; verify it's present on all deployments (local + cluster).
- Audit all state-backed kind schemas (`journal.*`, `news.*`, `logs`, `gripes`) for missing ANN indexes on `embedding` columns. Every table with `vector(1024)` needs an HNSW index (`vector_cosine_ops`, `m=16`, `ef_construction=64`).
- Consider `probes` parameter tuning at query time (`SET hnsw.ef_search = N`) for recall vs speed tradeoff — default (40) is fine for most use cases but may need bumping for high-recall research queries.
- `list_papers()` has an N+1 query: per-ref `count(blocks)` call. Should use a single aggregating join or window function.
- `search_text()` enriches each hit by calling `store.get(pid)` individually — batch into a single `WHERE ref_id IN (...)` query.
- SentenceTransformer model load (`BAAI/bge-m3`) on first `search_text()` call takes several seconds. Consider pre-warming at startup or moving to a persistent embedding service.

**Long-running operations (beyond Phase 9 basics):**

- Job-based async with `start`/`status`/`cancel` verbs (Option B from the async discussion) — only if `deliver_to=` proves insufficient.
- Hermes message-bus integration for research results (Option D from the async discussion) — precis publishes hermes events when a long-running task completes.
