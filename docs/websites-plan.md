# Websites + books as first-class precis kinds

**Goal.** Let the user (and agents) stash links and books into precis, with
summaries + tags that are searchable.  Later: fetch the page content on
demand via a polite, cookie-aware fetcher.

**Author.** Reto Stamm · 2026-04-24
**Status.** Phase 0 landed.  Phase 1 design locked 2026-04-24; implementation starting.

---

## Naming

| Kind         | Purpose                                               | Writable |
|--------------|-------------------------------------------------------|----------|
| `websearch:` | Perplexity Sonar live web search (formerly `web:`).   | no       |
| `web:`       | Stored website bookmarks + summaries (Phase 1, NEW).  | yes      |
| `book:`      | Stored book notes + summaries (Phase 1, NEW).         | yes      |

Rationale: agents type `search(type='web', query='glow proteins')` and expect
a **catalogue** of things the user curated, not a fresh live search.  The
live-search behaviour moves to `websearch:` where the name is literal.
Perplexity is one of three `websearch`-adjacent kinds (`think:`, `research:`)
and those keep their existing names.

## Phase 0 — Rename `web:` → `websearch:` (landed)

Mechanical rename, no alias.  `web:` is now unused and free for Phase 1.

Touched files:

- `src/precis/handlers/web.py` → `src/precis/handlers/websearch.py`, class
  `WebHandler` → `WebsearchHandler`, `scheme = "websearch"`.
- `src/precis/registry.py` — plugin name, schemes, KindSpec.
- All tests / docs / ansible hermes profiles / grimoire refs.
- CHANGELOG entry + major version bump (5.0.0).

Breaking change: any external caller that used `type='web'` for Perplexity
must update to `type='websearch'`.  No deprecation alias because `web` is
needed for the Phase 1 bookmark kind.

## Phase 1 — Stored bookmarks + books (offline, no fetching)

Two new kinds, both backed by `acatome-store`:

### `web:` — website bookmarks

- **Corpus**: `websites`.  Seeded in `acatome-store.models.CORPUS_SEEDS`.
- **Slug**: derived from URL host + first path segment, fall back to title.
  E.g. `https://github.com/modelcontextprotocol/servers` →
  `web:github-com-modelcontextprotocol`.
- **Meta JSON**:
  ```json
  {
    "url": "https://...",
    "canonical_url": "https://... (tracking stripped, host lowercased)",
    "kind": "tool | article | repo | db | video | paper | other",
    "captured_at": "2026-04-24T12:00:00Z",
    "status": "ok | stale | gone",
    "last_checked_at": null,
    "notes_count": 0
  }
  ```
- **Blocks**: `blocks[0].text` = the user's summary (what the site is, what
  it does, why it matters).  Agents can append follow-up notes as additional
  blocks via `put(mode='append')`.
- **Write API** (follows the `memory:` pattern exactly):
  ```
  put(type='web', url='https://...', title='...', text='summary',
      tags=['tool', 'bio'])
  put(id='web:<slug>', text='...', mode='append')      # add a note
  put(id='web:<slug>', text='...', mode='replace')     # rewrite summary
  put(id='web:<slug>', mode='delete')                  # soft-delete
  ```
- **Read API**:
  ```
  get(id='web:/recent')             # last 20
  get(id='web:/tags')               # tag histogram
  get(id='web:/kinds')              # tool/article/repo/db breakdown
  get(id='web:<slug>')              # overview: url, kind, tags, summary
  get(id='web:<slug>/notes')        # all appended notes
  search(type='web', query='...')   # grep over title+summary+tags+url
  ```
- **Idempotency**: `put` with an existing canonical URL returns the existing
  slug; `mode='replace'` lets the user update the summary.
- **URL normalisation**: lowercase host, strip `utm_*` / `fbclid` / `gclid`,
  strip trailing slash except root, strip fragment `#...` unless the URL
  belongs to a known SPA host (arxiv abs, github blob — keep fragment).

### `book:` — book notes

- **Corpus**: `books`.
- **Slug**: `book:<author-lastname><year><short-title>` (e.g.
  `book:feynman1963lectures`).  Books without a known year fall back to
  `book:<author-lastname>-<short-title>`; books with no author fall back
  to `book:<title-slug>`.  Pre-ISBN / self-published / obscure books are
  first-class — ISBN is optional throughout.
- **`isbn:` as id format**, not a separate kind.  Mirrors how
  `paper:` accepts `doi:` / `arxiv:` / `pmcid:`:
  ```
  get(id='isbn:9780306406157')   # resolves to the book record
  get(id='isbn:0-306-40615-2')   # 10-digit form, hyphens stripped
  ```
  Registered as an accepted scheme prefix in the URI parser that routes
  to `BookHandler` after normalising the ISBN.  The registry keeps one
  kind (`book`); the `isbn:` scheme is declared on `BookHandler.schemes`
  alongside `book:`.
- **Meta JSON**:
  ```json
  {
    "title": "The Feynman Lectures on Physics, Vol. 1",
    "authors": ["Richard P. Feynman", "Robert B. Leighton", "Matthew Sands"],
    "year": 1963,
    "publisher": "Addison-Wesley",
    "pages": 544,
    "isbn": "9780201021158",        // optional
    "isbn10": "0201021153",          // optional, back-compat
    "status": "to-read | reading | read",
    "rating": null,                  // 1–5 when status=read
    "captured_at": "...",
    "paper_slug": null,              // optional cross-link — see below
    "tags": ["physics", "textbook"]
  }
  ```
- **Write / read API**: same shape as `web:`, with status views
  `book:/to-read`, `book:/reading`, `book:/read`, plus `book:/by-year`
  and `book:/by-author`.

#### Large-book ingestion — cross-link to `paper:`

`book:` is deliberately shallow: author, ISBN, status, your notes.  If
you want the full content of a book searchable by chunk + figure (e.g.
the Feynman Lectures, a Ramakrishnan textbook), run it through the
`acatome-extract` pipeline — it lands as a multi-chunk `paper:` ref
exactly like a PDF would.

The `book:` ref then carries `meta.paper_slug = '<ingested_slug>'`
pointing to the paper record.  From `book:feynman1963lectures` you see
your reading-notes overview; following the `paper_slug` gets you the
chunked, embedding-searchable, figure-addressable body.

For multi-volume works, each volume becomes its own paper slug
(`feynman1963lectures1` / `2` / `3`) and the `book:` ref links to all
of them via a `volumes: [slug, ...]` meta array.  No special-casing in
`BookHandler` — the `paper:` kind already handles per-volume ingestion.

### Search behaviour

Both kinds use the existing `RefHandler._search_or_grep` fallback: for
non-paper corpora the search is keyword-grep over title + tags + url +
summary blocks (same as `memory:`, which also doesn't hit pgvector today).
A future improvement is to wire the embedding store so `search(type='web',
query='...')` returns true semantic hits — but that's its own project; see
"pgvector for non-paper corpora" further down.

### Why not reuse `memory:` with a tag?

1. Agents must distinguish "the cluster DB user is cluster_app" (memory)
   from "the ENZYME website is a searchable database" (website).  Different
   overviews, different schemas, different default views.
2. Bookmarks carry URL metadata (canonical, status, last_checked) that
   doesn't belong on a generic memory.
3. A dedicated kind lets Phase 2 attach a fetcher without complicating
   memory.

### Files to add / touch

- **New**: `src/precis/handlers/website.py` (`WebsiteHandler`,
  `BookHandler`, or split into two files — leaning toward split).
- **New**: `tests/test_website_handler.py`, `tests/test_book_handler.py`.
- **Modify**: `src/precis/registry.py` — register both builtins (gated on
  acatome-store import).
- **Modify**: `acatome-store/src/acatome_store/models.py` — add `websites`
  and `books` corpus seeds.  Requires a store version bump.
- **Modify**: `README.md`, `CHANGELOG.md`.

### Archive.org snapshot on capture (policy: ON by default)

Every `put(type='web', url='...')` triggers a Wayback "Save Page Now"
request against `https://web.archive.org/save/<url>` so the captured
page is preserved even if the source dies or changes.

**Defaults:**

- **On by default.**  Users can opt out per call with `archive=false`
  and globally with `PRECIS_WEB_AUTO_ARCHIVE=0`.
- **Never archived** (leak-prevention guard, runs before the HTTP call):
  - `localhost` / `127.0.0.0/8` / `::1`
  - RFC1918 ranges: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`
  - Tailscale CGNAT: `100.64.0.0/10`
  - `file://` / `data:` / non-http(s) schemes
  - Any URL with `.local` / `.internal` / `.lan` / `.home.arpa` host
  - Any URL whose host resolves to a private IP at save time
- **Rate-limit polite**: single save per URL, fire-and-forget with a 5 s
  timeout; failures are logged but don't block the bookmark write.  We
  batch-limit to 10 archive calls/min globally (well below archive.org's
  15/min anonymous cap) and back off to a queue if we exceed it.
- **Meta records the snapshot URL** on success:
  `meta.wayback_url = "https://web.archive.org/web/<ts>/<url>"`.  That
  URL is surfaced in the overview and `/meta` view so the user can
  always reach the archival copy.
- `put(... archive=false)` records `meta.wayback_url = null` with
  `meta.archive_skipped_reason = 'user_optout' | 'private_url' | ...`.

Implementation lives in a separate module `src/precis/web_archive.py`
so it's trivially mockable in tests and has no import-time side-effects.

## Phase 2 — Dynamic fetching

On-demand: `put(type='web', url='https://...')` **without** `text=` queues
the URL for background fetch + LLM summarisation.  The agent can still read
the placeholder slug immediately and see `status: fetching`.

### Fetcher stack (tried in order)

1. **Plain HTTPS** via `httpx` with a rotating pool of browser UAs.  Strips
   to main content with `trafilatura` (fast, no browser).  Works for most
   articles, docs, blog posts, GitHub README.
2. **Playwright headless Chromium** — for JS-heavy SPAs (Notion, Twitter,
   LinkedIn).  Heavy dep (~200 MB) so gated behind `[web-fetch]` extra and
   an opt-in env var `PRECIS_WEB_BROWSER=1`.  Keeps a persistent storage
   state dir (`~/.precis/web-state/<host>/`) so per-host cookies persist.
3. **Reader proxies** when 1 and 2 fail or are unavailable:
   - `https://r.jina.ai/<url>` — free, returns markdown of main content.
   - Archive.org Wayback (`https://web.archive.org/web/<url>`) for sites
     that block direct scraping.
4. **Commercial fallback** (opt-in): Firecrawl / ScrapingBee / Browserless
   when `FIRECRAWL_API_KEY` etc. are set.  Each gated behind its env var
   so the core install stays free.

After raw HTML / markdown is captured, we:

- Extract main text with `trafilatura` (+ readability fallback).
- Detect `kind`: `.github.com/<user>/<repo>` → repo; `.youtube.com` → video;
  ending in `.pdf` → paper (route through quest); generic otherwise.
- Call LiteLLM with a summarisation prompt against `agent_model_heavy` or
  `coder2` (35 B or 80 B MoE): "summarise in 200 words, list capabilities
  as bullets, suggest 5 tags".
- Write the summary as `blocks[0]`, raw content as `blocks[1..N]` chunked
  for page-size retrieval via `get(id='web:<slug>:2..5')`.
- Update meta `status=ok` + `last_checked_at`.

### Rate limiting + politeness

- **Per-host token bucket** in memory: default 1 req / 10 s, burst 3.
  Configurable via `PRECIS_WEB_RATE_<HOST>` env and `~/.precis/web-rate.toml`.
- **Respect robots.txt** by default; `allow_ignore_robots` flag per-host
  in the toml file for sites the user has explicit permission to scrape.
- **User-Agent** includes contact info (configurable via
  `PRECIS_WEB_USER_AGENT`) so operators can reach us if we misbehave.
- **Backoff** on 429 / 503: exponential with jitter, cap 5 min.

### Auth + cookies

- Per-host credentials stored in macOS Keychain (or `~/.precis/web-secrets`
  encrypted with `age`).  Loaded lazily only when the fetcher sees a host
  that has a keychain entry.
- Cookie jar persists in the Playwright storage-state dir.  Manual "login
  once" flow: `precis web-login <url>` pops a visible browser, user logs
  in, state saves.
- **Never log secrets**: sanitise headers on error paths.

### Captchas

Out of scope.  If the fetcher detects a captcha page (Cloudflare challenge,
Google reCAPTCHA) it:

1. Marks the ref `status=needs_user` and the note "captcha blocked —
   paste content manually via `put(id='web:<slug>', text='...',
   mode='replace')`".
2. Returns immediately without retrying; does not burn quota.

### Runner architecture

Option A — inline.  `put(type='web', url='...')` starts a background thread
that fetches + summarises.  Simple, but dies if the MCP process exits.

Option B — dedicated runner daemon (recommended).  A LaunchDaemon on
balthazar polls `cluster.websites.fetch_queue` for `queued` rows, locks
via `FOR UPDATE SKIP LOCKED`, runs the fetcher, writes the summary back.
Same pattern as `acatome-quest-runner` and `sortie-runner`.  Handles
restart cleanly, respects rate limits across processes.

Option C — a new tiny MCP `webfetch-mcp` that exposes `fetch(url)` and
`summarise(url)` as tools, which `precis` calls when a `web:` record needs
filling.  Separates deps (Playwright in webfetch, not in precis).  This is
my favourite because it also lets agents call the fetcher directly for
one-off reads without storing.

### Fetch filters (user-configurable)

A `~/.precis/web-filters.toml`:

```toml
[default]
max_bytes = 2_000_000
allow_domains = []           # empty = allow all except deny_domains
deny_domains = ["facebook.com", "linkedin.com"]
respect_robots = true
summarise = true             # false = store raw content only
summary_model = "coder2"

[host."github.com"]
extract = "readme_plus_about"  # pull README + repo description + topic tags
summarise = false              # README is already a summary

[host."arxiv.org"]
route_to = "quest"             # arXiv PDFs go through paper ingestion
```

Filters run per host and fall back to `[default]`.  The `route_to = "quest"`
hook means the bookmark kind recognises arXiv / DOI URLs and punts them to
`acatome-quest-mcp` instead of storing as a raw website — the result ends
up as a proper paper in the library.

## Phase 3 — Polish

- **Dead-link detector**: nightly cron re-checks `web:` refs, marks
  `status=gone` on 404 / DNS fail.
- **Digest**: weekly summary of new bookmarks, delivered via Discord.
- **Discord `/bookmark <url> <tags>`**: one-line add from chat.
- **Kind icons + favicon domain hints** in the `/recent` view.
- **Citations**: if a book/website is cited from a paper or another ref,
  surface the backlinks in the overview.

## pgvector for non-paper corpora (separate project)

Today `memory:`, `websites:`, `books:` fall back to keyword grep.  Fixing
this means adding `corpus_id` to the embedding index so `search_text` can
filter by corpus, and batch-embedding all non-paper blocks.  Tracked as a
follow-up.

## Design decisions locked 2026-04-24

Four questions resolved before implementation started:

1. **`book:` is a separate kind** from `web:` (not merged via tag).
   Divergent metadata (ISBN/authors/status vs URL/fetch-status),
   divergent lifecycles (read vs revisit), divergent overviews.
2. **Raw + summary both stored** once Phase 2 fetcher lands.  Phase 1
   is summary-only (the user writes the summary directly).  Phase 2
   persists the fetched raw markdown in `blocks[1..N]` alongside the
   summary in `blocks[0]`.
3. **Archive.org snapshot ON by default**, opt out per call with
   `archive=false`, opt out globally with `PRECIS_WEB_AUTO_ARCHIVE=0`.
   Private URLs (localhost, RFC1918, Tailscale CGNAT, `.local`, etc.)
   are *never* sent to archive.org regardless of the flag — leak guard.
   Details in the Phase 1 "Archive.org snapshot on capture" section.
4. **`isbn:` is an accepted id format on `book:`**, not a separate kind.
   `get(id='isbn:9780306406157')` resolves to the same record as
   `get(type='book', id='<slug>')`.  Mirrors the `doi:` / `arxiv:`
   id-format family on `paper:`.  Books without ISBN (old, obscure,
   self-published) are first-class — ISBN is optional.  Large books are
   ingested via the `paper:` pipeline and cross-linked from the `book:`
   ref via `meta.paper_slug`.

## Further open questions (not blocking Phase 1)

- **Per-URL vs per-host `kind`** — `kind='repo'` for github.com as a
   whole, or only for `/user/repo` paths?  Leaning per-URL inferred at
   capture.
- **pgvector for non-paper corpora** — see dedicated section above.
- **Dead-link rechecker** — Phase 3 cron; frequency + notification
   channel tbd.
