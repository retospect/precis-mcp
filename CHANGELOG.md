# Changelog

## 5.2.3

Bug-fix release for the `math:` Wolfram Alpha handler.  Three
upstream + local bugs combined to make every live query fail.

### Fixed

- **`asyncio.run()` cannot be called from a running event loop.**
  Upstream `wolframalpha.Client.query` (v5.x) wraps the async fetch
  with `asyncio.run`, which raises inside FastMCP's event loop.  We
  now bypass `Client.query` entirely and issue the HTTP GET via a
  synchronous `httpx.Client` (no event loop needed).
- **Empty `Wolfram Alpha API error:` from over-strict content-type
  assert.** Upstream `aquery` asserts `Content-Type ==
  'text/xml;charset=utf-8'` (no space) but the real Wolfram API
  returns `'text/xml; charset=utf-8'` (with a space), raising a bare
  `AssertionError` with no message.  Our direct fetch path simply
  doesn't impose that assertion.
- **Single-subpod pods silently dropped from output.** `xmltodict`
  collapses a one-element `<subpod>` list to a dict, but the
  formatter's `for sub in pod.get("subpod", [])` then iterated dict
  keys (strings) and skipped them all, producing the empty
  "Query succeeded but returned no displayable text" output for
  basic queries like `2+2`.  The formatter now coerces single-dict
  shapes to a one-element list.

### Tests

- Added regression tests for all three bugs in
  `tests/test_phase4_external.py`:
  `test_read_works_inside_running_event_loop`,
  `test_run_query_handles_real_world_content_type`,
  `test_single_subpod_as_dict_extracted`.
- Live smoke tests (`PRECIS_TEST_WOLFRAM_LIVE=1`) all pass against
  the real API.

## 5.2.0

New kind `rmk:` — push PDFs and EPUBs to a reMarkable e-ink reader
tablet (rM1 / rM2 / rMPro) via the reMarkable Cloud API.  Hidden
from the agent tool-schema enum unless `REMARKABLE_TOKEN` is set, so
the kind is invisible on hosts without a registered tablet.

### Added

- **`rmk:` scheme — write-only cloud upload.** Single verb
  `put(id='rmk:/<absolute-path>', mode='push')` uploads a local
  `.pdf` or `.epub` to the tablet's root folder.  Optional `text`
  argument overrides the on-tablet display name (default: file
  stem).  Response includes the tablet-side document ID so the agent
  can link the tablet copy back to whatever source ref prompted the
  push.  Internally wraps
  `remarkable_mcp.sync.load_client_from_token` + `RemarkableClient
  .upload` — the ``remarkable-mcp`` import is lazy, so precis still
  imports cleanly on hosts where the package isn't installed.
- **`[remarkable]` optional extra** (declared-but-empty).  The
  ``remarkable-mcp`` package is not on PyPI and PyPI rejects
  git-URL deps, so the extra is a stub that documents the intent;
  operators install ``remarkable-mcp`` separately (the cluster
  ansible role already does this — see
  ``ansible/roles/mcps/tasks/main.yml``).

### Gating

- **`REMARKABLE_TOKEN`** env var — from
  `remarkable-mcp --register <one-time-code>` after fetching a code
  from https://my.remarkable.com/device/desktop/connect.
  Unset → kind hidden from tool-schema enum, one-shot startup
  warning emitted, direct URI calls raise `KIND_UNAVAILABLE`.
- **`remarkable-mcp` importability** — gated inside
  `RmkHandler._get_client`.  Missing package raises
  `KIND_UNAVAILABLE` with a
  `pip install 'precis-mcp[remarkable]'` hint (even though the
  extra is a stub — the hint carries the correct canonical install
  path).

### Scope

Read-side helpers (highlight extraction, handwriting OCR, tablet
library listing) are deliberately out of scope for this release.
The `remarkable-mcp` package covers them via `extract.py`; a future
phase can wrap them as additional verbs or a sibling kind.

### Tests

36 new tests under `tests/test_rmk_handler.py` cover:
class-attribute shape, client lazy-init + caching, both env-gating
paths (missing package / missing token), pre-client validation
(mode, path, extension, directory-vs-file), happy-path upload
kwargs for PDF and EPUB, display-name override, whitespace
fallback, case-insensitive extension matching, upstream-error
wrapping, help text, and registry / visibility behaviour.

## 5.1.0

Post-5.0 cleanup: naming consistency + stale doc-path fixes.  No
behaviour changes; safe in-place upgrade from 5.0.0.

### Changed

- **`tests/test_web_handler.py` → `tests/test_web_bookmark_handler.py`.**
  Disambiguates from `tests/test_phase3_websearch.py` (the Perplexity
  `websearch` handler tests).  `WebHandler` was a handler name shared by
  the old Perplexity kind (pre-5.0) and the new bookmark kind (5.0+);
  the test-file name now says which handler is under test.  Internal
  change — only visible if you collect/run tests by explicit path.

### Fixed

- **Docs: `grimoire/agents/quest-agent.md` → `quest.md` path refs.**
  The cluster-side grimoire renamed agent prompt files to align with
  hermes profile names (`coding-agent.md` → `coder.md`, `quest-agent.md`
  → `quest.md`, etc.).  Two references in `docs/plugin-architecture.md`
  (Phase 12 refinement-pass list, Phase 12b skill-kind example) pointed
  at the old path; both updated so readers clicking the path land on
  the real file.

## 5.0.0

**Breaking.** The Perplexity Sonar kind `web` has been renamed to
`websearch`.  The scheme `web:` is freed for a forthcoming stored-bookmark
kind (Phase 1 of `docs/websites-plan.md`).  No alias is retained — agents
calling `type='web'` for live search must update to `type='websearch'`.

### Changed

- **`web:` → `websearch:`** — plugin, handler, scheme, and KindSpec all
  renamed.  `WebHandler` → `WebsearchHandler`; module
  `precis.handlers.web` → `precis.handlers.websearch`; plugin name
  `"web"` → `"websearch"`.  `ThinkHandler` and `ResearchHandler` are
  unchanged (they kept their literal names — only the cheapest Perplexity
  mode was ambiguously named after the scheme).
- **`_WebBase` → `_PerplexityBase`** — the shared base class for all
  three Perplexity-Sonar handlers.  The old name referred to the (now
  gone) `web` scheme; the new name reflects what the class actually is.
- **Internal callers** — server docstring examples, registry examples,
  plugin-architecture doc, smoke-test plan, and test kind-labels all
  updated from `web` to `websearch`.

### Migration

Rename in your own config / prompts:

| Before                               | After                                      |
|--------------------------------------|--------------------------------------------|
| `get(type='web', id='…')`            | `get(type='websearch', id='…')`            |
| `search(query='…', type='web')`      | `search(query='…', type='websearch')`      |
| `get(id='web:CEO of Anthropic')`     | `get(id='websearch:CEO of Anthropic')`     |
| `PRECIS_KINDS=paper,web`             | `PRECIS_KINDS=paper,websearch`             |

### Added — cross-corpus search

- **`search(type='all')` — search every ref-backed corpus at once.**
  Dispatches a single `store.search_text(corpora=[...])` call that
  returns a unified ranking across all corpora (papers, memories,
  websites, books, todos, flashcards, conversations), then renders
  the hits grouped by kind with kind-specific badges.  One query,
  one ranking pass, grouped output.
- **`search(type='paper,memory,web')` — comma-separated kind lists.**
  Resolves each kind to its corpus, dedupes, and dispatches
  cross-corpus.  Whitespace around commas is tolerated.  Rejects
  non-ref-backed kinds (`websearch`, `calc`, etc.) with a clear
  structured error since they have no corpus to merge.
- **Per-kind semantic search.**  Memory, web, book, todo, flashcard,
  and conversation `search(type='X')` calls now actually hit the
  vector index instead of falling back to keyword grep.  Before
  this release the per-kind handlers `_search_or_grep` carved out
  non-paper kinds to substring-only because the shared vector index
  couldn't filter by corpus — that fallback is gone now that the
  `corpora=[corpus_id]` filter scopes embeddings-backed search
  cleanly.  Still falls back to grep if the embedder is missing at
  runtime (ImportError), so `PRECIS_KINDS` mask users aren't broken.
- **New `precis.cross_corpus` module.**  Pure helpers:
  `is_cross_corpus_request(type_arg)`, `expand_type_to_corpora(type)`,
  `kind_to_corpus_id(kind)` / `corpus_id_to_kind(corpus_id)`, and
  the dispatcher `search_across_corpora(query, corpora, top_k)`
  that both `server.search` and external callers can use.
- **`archive: bool | None` kwarg on the MCP `put()` tool** —
  forwarded to `web:` only when explicitly set.

### Added — bookmarks + book notes (Phase 1)

- **`web:` kind — website bookmarks** (Phase 1 of
  `docs/websites-plan.md`).  Stored in the new `websites` corpus
  (acatome-store seed bump).  Canonical-URL dedup on create, inferred
  `kind` (tool / article / repo / db / video / paper / other), tag
  histogram, shallow meta with `captured_at` + optional
  `wayback_url`.  Views: `/recent`, `/tags`, `/kinds`.  URL
  canonicalisation strips `utm_*`, `fbclid`, `gclid`, `mc_cid`,
  `_ga`, `igshid`, `ref_src`, drops trailing slashes + default
  ports, lowercases scheme/host, preserves fragment only for
  declared SPA hosts (`arxiv.org`, `github.com`, `notion.so`).
  Pure canonicaliser lives in the new `precis.url_canonical`
  module for easy reuse.
- **`web.archive.org` integration** — every `put(type='web', …)`
  triggers a Save Page Now call by default.  Opt out per-call
  with `archive=False` (new kwarg on `put()`), or globally via
  `PRECIS_WEB_AUTO_ARCHIVE=0`.  Private URLs are **never**
  archived regardless of the flag — loopback, RFC1918, Tailscale
  CGNAT (`100.64/10`), and `.local` / `.internal` / `.lan` /
  `.home.arpa` / `.test` / `.invalid` suffixes are all guarded
  before the HTTP call.  Rate-limited to 10 saves / 60 s globally
  (below archive.org's documented 15/min anonymous cap).  Failures
  never block the bookmark write — the skip reason is recorded in
  `meta.archive_skipped_reason` so agents can surface it.
  Implementation in the new `precis.web_archive` module.
- **`book:` kind — curated book notes** with optional ISBN (Phase 1).
  Stored in the new `books` corpus.  Slug derived from
  `<surname><year><title>` with graceful fallbacks for unknown
  year / unknown author / ISBN-only.  `isbn:` is accepted as an
  alternative id format (e.g. `get(id='isbn:9780201021158')`)
  — mirrors the `doi:` / `arxiv:` family on `paper:`.  Read
  status (`to-read` / `reading` / `read` / `abandoned`) tracked
  in meta; `/to-read`, `/reading`, `/read`, `/by-author`,
  `/by-year` views.  Cross-links to deep-ingested books via
  `meta.paper_slug`.
- **`archive: bool | None` kwarg on the MCP `put()` tool** —
  forwarded to `web:` only when explicitly set.
- **`docs/websites-plan.md`** — design doc, locked-in decisions
  for Phase 1 (this release) and Phase 2 dynamic fetcher.

## 4.2.0 — 2026-04-22

New `plot` kind — declarative matplotlib renderer driven by a
pydantic-validated JSON spec.  Additive; nothing breaks the 4.1 API.

### Added

- **`plot` kind** — local, stateless, matplotlib-backed plot
  renderer.  No code execution: the caller posts a validated JSON
  spec via `put(id='plot:', text='<spec>', mode='render')` and
  receives either an inline PNG data URL (default), inline SVG /
  WebP (via `/svg` or `/webp` suffix), or a file written under
  `./figures/` (via `/export` or `/export/<filename>`).  Five plot
  types ship: `line`, `scatter`, `bar`, `hist`, `errorbar`.  Line
  and scatter support an optional `fit` overlay (`linear`, `log`,
  `exp`, `arrhenius`) with a slope / intercept / R² report line
  prepended to the output.  Scheme: `plot:` (opaque-path so export
  filenames with `/` and `.` pass through to the handler intact).
  Onboarding skill `skill:plot-basics` ships with the package.
- **`[plot]` optional-dep group** in `pyproject.toml` — pulls in
  `matplotlib>=3.7` and `pydantic>=2.0` (~70 MB on disk).  `[all]`
  includes it automatically.
- **`PlotHandler` entry point** — `plot = "precis.handlers.plot:PlotHandler"`
  under `[project.entry-points."precis.schemes"]`.
- **`plot-basics` onboarding skill** — full schema reference
  (common fields, per-type data shape, fit kinds, export formats)
  with Arrhenius-plot example, matching the `calc-basics` /
  `todo-triage` shape agents already know.
- **Comprehensive `plot` unit tests** — 66 tests in
  `tests/test_plot_handler.py` across `TestParsePlotPath`,
  `TestParseSpec`, `TestLinearRegression`, `TestInlineRendering`,
  `TestExportRendering`, `TestModeAndMisc`, `TestPlotIsOpaque`, and
  `TestPlotRegistration`.  Covers path parsing, spec validation,
  each render format, every plot type, fit-overlay reports, export
  to disk (PNG / SVG / PDF), security rejects (absolute paths,
  `..` traversal, unknown extensions), mode enforcement, and the
  opaque URI round-trip.

### Changed

- **`_OPAQUE_PATH_SCHEMES`** in `precis.uri` now includes `plot`
  alongside `calc` / `doi` / `arxiv`.  The handler parses its own
  `/svg`, `/webp`, `/help`, `/export`, and `/export/<file>`
  suffixes so filenames like `/export/deep/chart.pdf` survive URI
  parsing.
- **`[all]` optional-dep group** — now pulls in `[plot]` alongside
  `[word, tex, paper, flashcards, quest, external, calc]`.

### Tests

- **1,160 unit tests passing** (up from 1,094 at 4.1.x — a
  66-test expansion for the new kind).  `ruff check` + `mypy`
  clean.

## 4.1.0 — 2026-04-22

New `calc` kind (free local SymPy calculator), expanded tool-schema
discoverability for every kind, and a gated live-Wolfram integration
test.  All additive; nothing breaks the 4.0 API.

### Added

- **`calc` kind** — local SymPy-backed calculator.  No network, no
  env vars, no cost.  Handles exact arithmetic, symbolic roots,
  fractions, calculus (integrate / diff / limit / series),
  equation-solving, linear algebra (matrices / determinants /
  eigenvalues), base conversion (hex / bin / oct), and unit
  conversion.  Views: `/pretty`, `/latex`, `/numeric`, `/help`.
  Scheme: `calc:`.  Two seed skills ship with it:
  `skill:calc-basics` (arithmetic) and `skill:calc-advanced`
  (calculus + linear algebra + units).
- **`[calc]` optional-dep group** in `pyproject.toml` — pulls in
  `sympy>=1.12` (~30 MB).  `[all]` includes it automatically.
- **Compute section in the `get()` / `search()` tool docstrings** —
  every kind (`calc`, `math`, `web`, `research`, `think`, `youtube`,
  plus all stateful kinds) now appears with a concrete example in
  the agent-facing tool schema.  Previously the LLM only saw the
  document / paper kinds and had to guess the rest; qwen3.5:9b could
  not reliably discover `math` or `calc` before this change.  Both
  docstrings stay inside the 600-token / 2000-token budget enforced
  by `tests/test_llm_tool_use.py::TestDescriptionBudget`.
- **`TestCalc` and `TestMath` in `tests/test_llm_live.py`** — five
  live LLM-routing tests (3 calc, 2 math) exercising the real
  ollama qwen3.5:9b inference loop against the published MCP tool
  schemas.  Verifies the LLM routes arithmetic to `type='calc'`
  and world-data queries to `type='math'`.  Skip gracefully when
  the LLM answers directly without calling a tool (same
  fail-soft pattern as `_require_dispatched_paper_uri`).
- **`TestWolframLive` in `tests/test_phase4_external.py`** —
  opt-in live smoke test gated on `PRECIS_TEST_WOLFRAM_LIVE=1` +
  `WOLFRAM_APP_ID`.  Three tests (~3 Wolfram API calls): basic
  arithmetic returns `4`, attribution footer present on live
  responses, nonsense queries don't crash.  Skipped by default in
  CI so we don't burn Wolfram's free-tier 2000/month quota on
  every merge; run locally after bumping the `wolframalpha` pin or
  touching `_format_result`.
- **Comprehensive `calc` unit tests** — 46 tests in
  `tests/test_calc_handler.py` across
  `TestParsePath`, `TestCalcIsOpaque`, `TestSanitize`,
  `TestCalcBasics`, `TestCalcBaseConversion`, `TestCalcCalculus`,
  `TestCalcLinearAlgebra`, `TestCalcUnits`, `TestCalcViews`,
  `TestCalcSafety`, `TestCalcAttribution`, and
  `TestCalcRegistration`.  Covers parse-path disambiguation,
  opaque-scheme handling, dangerous-input rejection, and every
  view.

### Changed

- **`get()` tool docstring** — compressed the Papers section
  (dropped redundant figure variants and explicit-prefix duplicates)
  to make room for new Compute / External / Stateful example
  blocks.  Still fits the 600-token budget.  The `Documents`
  section header became `Files` with the example filename changed
  from `doc.docx` to `report.docx` — the LLM was copying `doc.docx`
  literally on unrelated prompts and routing to an unregistered
  `doc:` scheme.
- **`search()` tool docstring** — trimmed narrative prose, added
  compact example blocks for every supported kind including both
  compute kinds and the full stateful family.  Under the per-tool
  budget.
- **`[all]` optional-dep group** — now pulls in `[calc]` alongside
  `[word, tex, paper, flashcards, quest, external]`.

### Tests

- **1,046 unit tests passing** (up from 978 at 4.0.1 — a
  68-test expansion covering calc + LLM routing + Wolfram live).
- **21 LLM-live tests passing** when ollama + qwen3.5:9b are
  available (including the five new TestCalc + TestMath tests).
  Gracefully skip when ollama isn't running.
- **Full suite runs in ~24 s** excluding the live-LLM and
  live-Wolfram suites.  `ruff check` + `mypy` clean.

### Docs

- README updated to document the `calc` kind alongside `math`, add
  Compute examples to the `get()` / `search()` sections, list
  `calc:` in the URI grammar's scheme-prefix set, and clarify the
  cost trade-off (`calc` is free/offline, `math` is paid/online).
- Install command table adds `pip install 'precis-mcp[calc]'`.

## 4.0.1 — 2026-04-22

Packaging fix only — no behaviour change.  The 4.0.0 wheel was
rejected by PyPI with
`400 Invalid distribution file. ZIP archive not accepted: Duplicate
filename in local headers.`  Every `SKILL.md` was present twice in
the wheel because hatchling's `packages = ["src/precis"]` discovery
was already including them and the separate
`[tool.hatch.build.targets.wheel.force-include]` block re-added the
same tree.  Older PyPI validators tolerated this silently; the
current one rejects it.

### Fixed

- Dropped the redundant
  `[tool.hatch.build.targets.wheel.force-include]` entry in
  `pyproject.toml`.  Each `SKILL.md` now appears exactly once in the
  wheel.  No change to installed-file layout: the skills still land
  at `precis/skills/<name>/SKILL.md`.

## 4.0.0 — 2026-04-22

Major revamp.  Twelve new kinds, plugin protocol v2, unified error
envelope, cost reporting, skills as a first-class kind, Perplexity
Sonar integration, external stateless handlers (Wolfram Alpha,
YouTube), journal kinds (memory, conversation), paper ID
auto-detection (DOI / arXiv / PMCID / ISBN / ISSN), cross-kind link
graph, tracked-change writes, `type=` kwarg on every tool, and a
three-verb smoke-test plan.  Every handler was rewritten on top of
the new `RefHandler` base + view-registry dispatch.

Full phase-by-phase development log below under
_"4.0.0 pre-release dev log"_.

### Breaking

- **Bare-slug `get(id='wang2020state')` no longer auto-routes to the
  `paper` kind.**  Without a scheme prefix (`paper:` / `doi:` / …), a
  file extension, or a DOI / arXiv / PMCID / ISBN / ISSN pattern, the
  server now emits `KIND_UNKNOWN` with `type='paper'` listed as the
  first option.  Same parity rule as `search()` and `put()`.  Use
  `get(type='paper', id='wang2020state')` or
  `get(id='paper:wang2020state')`.
- **URI selector separator changed from `#` to `~` and then to `›`**
  over the 3.x series; `›` (U+203A SINGLE RIGHT-POINTING ANGLE
  QUOTATION MARK) is the 4.0 canonical.  `~` still works as a legacy
  alias in selectors; `#` is hard-rejected.
- **`fc:` scheme alias retired.**  The flashcard kind is now
  registered only under its canonical `flashcard:` scheme.  URIs
  starting with `fc:` emit `KIND_UNKNOWN`.  Data migration for
  legacy `fc:…` slugs in stored content is the caller's responsibility.
- **`conv:` scheme alias retired.**  Conversations are now
  `conversation:` only.  Same KIND_UNKNOWN envelope on legacy
  `conv:…` URIs.
- **Plugin protocol v1 plugins without a `KindSpec` are refused at
  registration time.**  Plugins must bump their `protocol_version` to
  `"1"` and declare a `KindSpec` per kind.  The registry synthesises
  a minimal spec from the plugin's schemes + docstring when one isn't
  declared, but third-party plugins are expected to declare explicit
  specs for agent-visible metadata.
- **Kind-name collisions across plugins are now fatal.**  Previously
  a warning.  Raises `RegistryError`; the second plugin leaves no
  trace in `PLUGINS` / `SCHEMES` / `KINDS`.
- **Legacy `PrecisError(bare_string)` form removed.**  Every raise
  site must now supply a structured `ErrorCode`.  Constructing an
  error without one raises `TypeError` at call time.
- **`Handler._dispatch_view()` subclass hook removed.**  Replaced by
  the `views: dict[str, str]` class attribute + view-registry
  dispatch in `RefHandler`.

### Added

- **Twelve new kinds** layered on top of the original paper / docx /
  tex / markdown / plaintext / todo surface:
  - `skill` — filesystem-backed SKILL.md index with
    `/kind/<name>`, `/topic/<tag>`, `/recent`, `/help` views.
    Ships three seed skills (find-paper, todo-triage, sm2-basics).
    Writes confined to `~/.precis/skills/`; ecosystem-supplied
    skills (`~/.claude/skills/`, `.opencode/skills/`) read-only.
  - `quest` — `acatome-quest-mcp` paper-request lifecycle folded in.
    Backed by `papers.requests` in Postgres via `psycopg3` +
    `psycopg_pool` (fully sync, no asyncio bridge).
    `/recent` / `/queued` / `/needs-user` / `/failed` / `/agent/<id>`
    / `<uuid>/candidates` / `<uuid>/misconceptions` views.
    Schema-missing / DB-unreachable surface `UNAVAILABLE` with an
    actionable `next:` hint.
  - `memory` — long-term verbatim agent-memory drawers.  Read, grep,
    put, note, link.  ImportError-gated on `acatome-store`.
  - `conversation` — conversation recording + replay.  Same plumbing
    as memory; shares the acatome-store corpus seeds.
  - `web` — Perplexity Sonar synchronous web search.  Attribution
    footer per Perplexity ToS.
  - `research` — Perplexity Sonar deep-research (long-form,
    citation-heavy).  Paid; `cost_hint="~$0.04/call"`.
  - `think` — Perplexity Sonar reasoning model with the `think`
    budget.  `cost_hint="~$0.02/call"`.
  - `math` — Wolfram Alpha wrapper.  Requires `WOLFRAM_APP_ID`.
    Mandatory Wolfram attribution footer + academic-citation
    template per [Wolfram ToS](https://www.wolframalpha.com/termsofuse).
  - `youtube` — transcript fetcher via `youtube-transcript-api`.
    `/languages` view lists available transcript languages.
    Mandatory uploader-attribution footer.
  - `flashcard` — SM-2 spaced-repetition flashcards (corpus-backed,
    `/due` / `/new` / `/learning` views).  Canonical scheme is
    `flashcard:`; the legacy `fc:` alias was retired in this release.
- **Plugin protocol v2** — `KindSpec` dataclass declaring each kind's
  name, description, aliases, required env, cost hint, and examples;
  `CallContext` / `HintContext` / `NotificationContext`; unified
  `Result` envelope with `.ok()` / `.err()` constructors and a
  `.render()` producing the final agent-visible string; optional
  `Handler.cost_of()` / `Handler.hints()` / `Handler.notifications()`
  hooks with safe no-op defaults.  Spec in `docs/plugin-architecture.md`.
- **Unified error envelope** — every failure renders as
  `ERROR [<code>]: <summary>\n  where: …\n  cause: …\n  options: …\n  next: …`.
  `ErrorCode` enum catalogues 16 standard codes.  Non-agent-fault
  codes (`UNEXPECTED` / `TIMEOUT` / `UPSTREAM_ERROR` / `RATE_LIMITED`
  / `UNAVAILABLE`) auto-append a gripe-next-hint.
- **Cost reporting** — every tool response carries `[cost: …]` as a
  footer.  `stats()` tool exposes per-kind session stats (calls,
  errors, last-cost) and startup warnings.  Three-level cost fallback:
  per-call `cost_of()` → static `KindSpec.cost_hint` → `"free"`.
- **`type=` kwarg on every tool** (`search` / `get` / `put` / `move`).
  Alias-aware: `type='pmid'` routes to the paper kind but preserves
  the `pmid:` scheme in the dispatched URI.
- **Paper ID auto-detection** — new `precis.paper_id` module with
  `classify_paper_id()` and normalisers for DOI, arXiv (new + old
  forms), PMCID, ISBN-10/13 (full checksum validation), ISSN (mod-11
  checksum).  Bare identifiers route to the right scheme without
  `type=` for DOIs / arXiv ids / PMCIDs / ISBNs / ISSNs.
- **Cross-kind links** — `put(id=…, link='dst:relation')` and
  `unlink='dst[:relation]'` on every state-backed kind.  Links are a
  first-class primitive, not paper-specific.  Directional query via
  `/links/<direction>`.
- **Tracked-change writes** — DOCX `put(mode='replace' | 'after' |
  'before' | 'delete')` emits tracked changes by default.
  `tracked=False` suppresses them.  Margin comments via
  `mode='comment'`.
- **`PRECIS_KINDS` env var** — per-agent kind masking with a bracket
  grammar (e.g. `PRECIS_KINDS='paper,todo[search,get]'`).
  Alias-in-config, unknown-verb, duplicate-kind, and stray-bracket
  issues are fatal `ConfigError`s; unknown kinds are dropped with a
  warning so the server still starts.
- **`grep=` on `search()`** (new in this release, post-3.2 triage) —
  metadata pre-filter applied before the vector search; paper kind
  over-fetches hits and post-filters by filtered slug set.
- **Skill-surfacing hooks** — `Handler.onboarding_skill: str | None`
  (auto-appended to help views) and `_enrich_error` pointer
  (appends `see skill:<slug>` on agent-confusion codes).
- **View registry** — `views: dict[str, str]` class attribute on
  every `RefHandler` subclass; dispatch via the shared
  `_dispatch_view()` helper instead of stacked if/elif ladders.
- **`_reset_instance_cache()`** test hook for resetting memoised
  handler instances between tests.
- **`.precis/` user config directory** — scanned at startup for
  user-defined skills and handler overrides.
- **Figure handling** — `get(id='slug/fig')`, `get(id='slug/fig/3')`,
  `/legend` / `/image` / `/image/export` subviews.
- **Multi-ID batch reads** — `get(id='slug1›4,slug2›9')` returns
  both chunks in one call.
- **Handler-instance memoisation** — `resolve()` now caches one
  instance per scheme / file extension in `_SCHEME_INSTANCES` /
  `_FILE_TYPE_INSTANCES` guarded by `threading.Lock`.  Warm DB
  pools, HTTP clients, and scanned on-disk indexes survive across
  tool calls.
- **Structured error envelope on the `_dispatch` raw-fallback path**
  (BUG-E) — unknown kinds that raise now render via `_format_error`
  with preserved `options=` / `next=` from any `PrecisError` they
  threw.
- **psycopg error translation on the quest DB adapters** (BUG-H) —
  `UndefinedTable` → `UNAVAILABLE` with an
  `acatome-quest status --count` migration hint;
  `OperationalError` / `InterfaceError` → `UNAVAILABLE` with a
  `DATABASE_URL` pointer.

### Changed

- **`_dispatch` wraps every tool call** — all four tool entry points
  (`search` / `get` / `put` / `move`) route through `_dispatch` →
  `invoke_handler`, giving every response the `[cost: …]` footer and
  a unified exception path.
- **View dispatch refactor** — `_ref_base.py` replaced its if/elif
  ladder in `read()` with a view-registry lookup.  Subclasses declare
  their views via `views: dict[str, str]` class attr; the base class
  resolves + invokes.
- **`_to_uri` routes through `classify_paper_id()`** instead of the
  legacy DOI-only regex.  File-extension routing still runs first so
  `report.docx` stays a file.
- **List-renderer hardened against `None` ref fields** (BUG-A) — every
  metadata field (`doi`, `year`, `authors`, `title`) is coerced to
  `""` before join so a partially-ingested paper doesn't TypeError
  the entire ref list.
- **Paper overview renderer routes `authors` through `_author_names`**
  (BUG-D) so JSON-encoded author arrays decode on the landing page,
  matching the cite formatter behaviour.
- **Skill `_search` tokenises on whitespace and AND-matches across
  tokens** (BUG-G) — multi-word queries (e.g. `'acquire paper'`) now
  hit skills where every word appears anywhere in the combined
  `name + description` blob.
- **`_WebBase.read` absorbs unknown kwargs** (BUG-I) — `search()`
  dispatcher forwards `top_k` but the Perplexity handlers don't use
  it; no more `TypeError` pre-flight on
  `search(type='<web|research|think>', query=…)`.
- **Every tool response ends with the cost footer** — even free-tier
  kinds.  Removes ambiguity about whether a call was billable.
- **`QuestHandler` is fully sync** — dropped `_DB_INSTANCE`,
  `_get_db()`, `_set_db_for_testing()`, `asyncio.run()` bridge.
  Instance state, lazy pool construction, `db=` kwarg for tests.
- **`get()` docstring now shows `type='paper'`** on every bare-slug
  example so LLMs reading the MCP tool schema learn the 4.0
  convention.

### Fixed

- BUG-A through BUG-I from the 2026-04-22 19:30 smoke-test session —
  see `docs/mcp-smoke-test-plan.md` "Session log" for the live-check
  mapping.  Every fix has a regression test.
- 2026-04-22 17:40 triage #1–#11 — covered by the earlier
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/docs/mcp-smoke-test-plan.md`
  sessions.  Highlights: BibTeX formatter, `skill:/kind` parsed-URI
  slug leak, grep + query plumbing, flashcard canonical rename.
- Thread-safety race in `resolve()` where two concurrent MCP tool
  calls could both observe an empty cache and both instantiate a
  handler, leaking one.  Now guarded by a registry-level
  `threading.Lock`.
- ~134 raise sites across the handler tree converted from
  bare-string `PrecisError` to structured `(code, cause, options,
  next)` form so every error path carries the unified envelope.
- DOCX XML parsing hardened against XXE / billion-laughs attacks
  (imported from the 3.2 security release, retained here).

### Removed

- **`precis.handlers.quest._DB_INSTANCE`** and friends (see Changed).
- **`Handler._dispatch_view()`** class method and every subclass
  override of it (see view-registry change above).
- **Legacy `PrecisError(bare_string)`** constructor (see Breaking).
- **`fc` and `conv` scheme aliases** (see Breaking).
- **Auto-route-to-paper on bare slug** via `get()` (see Breaking).

### Tests

- **978 tests passing** (up from 414 at 3.0.0 — a 2.4× expansion).
- New test modules landed in 4.0 dev cycles:
  `test_phase2_cost.py`, `test_phase3_web.py`, `test_phase4_external.py`,
  `test_phase5_paper_id.py` (→ `test_paper_id.py`),
  `test_phase6_journal.py`, `test_phase7_links.py`,
  `test_phase8_errors.py`, `test_phase12_quest.py`,
  `test_phase12b_skill.py`, `test_kinds_config.py`,
  `test_visibility.py`, `test_server_phase1.py`,
  `test_invoke_handler.py`, `test_protocol_v2.py`,
  `test_llm_live.py` (live qwen3.5:9b tool-call verification).
- Regression tests for every bug fixed in the 17:40 + 19:30 smoke
  runs: `TestListRendererTolerateNones`,
  `TestOverviewAuthorsNormalisation`, `TestSearchWithGrep`,
  `TestSearchToolForwardsGrep`, `TestAmbiguousKindErrors`,
  `TestPgErrorTranslation`, and more.
- ruff + mypy clean.  Full suite runs in ~100s (~25s excluding the
  live-LLM suite).

### Docs

- `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/docs/plugin-architecture.md`
  — 1,879-line spec of the v2 plugin protocol (kinds, verbs, errors,
  cost, views, hints).
- `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/docs/mcp-smoke-test-plan.md`
  — 1,500-line three-verb smoke-test plan.  Drives the live-run
  session logs; every bug discovered links back to a §N.M bullet.
- README rewritten for the 4.0 surface — `type=` on every example,
  new kinds listed, URI grammar updated for the classifier-based
  routing.

---

## 4.0.0 pre-release dev log

_The sections below are the per-phase development log that accumulated
between the 3.0.0 release (2026-04-01) and the 4.0.0 cut.  They are
preserved verbatim for historical reference; the consolidated 4.0.0
notes above supersede them for agent-visible behaviour._

## Unreleased — Phase 12a consolidation (sync stack + handler caching)

Pre-12b cleanup pass on top of the 12a read-surface.  Flips the whole
quest path to sync, removes the `asyncio.run` bridge, and turns handler
instances into process-lifetime singletons so warm DB pools / HTTP
clients survive across tool calls.  No agent-visible behaviour change
beyond faster subsequent quest calls.

### Changed

- **Handler instances are now memoised per scheme / file extension.**
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/registry.py:1068-1121`
  `resolve()` used to call `handler_cls()` on every tool dispatch,
  throwing away the freshly-built handler at the end of the call.  It
  now caches one instance per scheme in `_SCHEME_INSTANCES` /
  `_FILE_TYPE_INSTANCES`, guarded by `threading.Lock` for the
  first-resolve() race.  Handlers that hold warm state (DB pools, HTTP
  clients, scanned on-disk indexes) now reuse it across the whole
  process.  The `math`, `youtube`, and `skill` handlers' lazy-init
  patterns are now actually lazy-once rather than lazy-per-call.
- **`QuestHandler` dropped the module-level DB singleton + `_run()` /
  `asyncio.run()` bridge.**  `self._db` is now instance state, created
  on first use.  The constructor accepts `db=` for test injection
  (replacing the old `_set_db_for_testing` hook).  All DB adaptors
  (`_db_get`, `_db_find`, `_db_find_by_prefix`) are plain sync methods.

### Added

- `precis.registry._reset_instance_cache()` — test hook to drop cached
  handler instances between tests that construct their own fixtures.
- `examples=` populated for the `paper`, `todo`, and `flashcard`
  `KindSpec`s (alongside the already-populated `quest`, `skill`,
  `memory`, `conversation`, `web`, `math`, `youtube`, `think`, and
  `research` entries).  Not yet rendered to agents — awaiting the
  shared `/help` view in a later phase.

### Removed

- `precis.handlers.quest._DB_INSTANCE`, `_get_db()`, `_set_db_for_testing()`,
  `_run()` — all replaced by instance state on `QuestHandler`.

### Fixed

- Thread-safety race in `resolve()`: two concurrent MCP tool calls could
  previously both observe an empty cache and both instantiate a handler,
  leaking one.  Now guarded by a registry-level `threading.Lock`.


## 3.6.0-dev — Phase 12a: `quest` kind (read surface)

First instalment of the `acatome-quest-mcp` fold-in (§12 of the plugin
architecture doc).  Adds the `quest` kind to precis as a state-backed
read surface; writes and the MCP-layer retirement land in 12b and 12c.

### Added

- `QuestHandler` at
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/handlers/quest.py`.
  Subclasses `Handler` directly (not `RefHandler` — quest records are
  UUID-keyed with jsonb columns, no block/slug structure).  Bridges
  precis's sync dispatch to the upstream async `DB` layer via
  `asyncio.run()` at the method boundary.  Module-level DB singleton
  with a test-injection hook (`_set_db_for_testing`).
- Read-surface views registered in `QuestHandler.views`:
  - `quest:`                       — bare recent list
  - `quest:<uuid>` / `quest:<8-hex>` — single card (short-prefix resolution)
  - `quest:/recent`                — most-recent, any status
  - `quest:/queued`                — waiting for runner
  - `quest:/needs-user`            — awaiting disambiguation / repoint
  - `quest:/failed`                — union of `failed` + `extract_failed`
  - `quest:/ingesting`             — union of `fetching` + `ingesting`
  - `quest:/agent/<id>`            — filter by `created_by`
  - `quest:<id>/candidates`        — disambiguation options
  - `quest:<id>/misconceptions`    — attached flags
  - `quest:/help`                  — inline the onboarding skill body
  - `search(type='quest', query='…')` — case-insensitive substring over
    titles (v1; pgvector in v1.2)
- `quest` scheme entry point in
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/pyproject.toml`,
  `[quest]` optional extra pulling in `acatome-quest-mcp>=0.1.0`, and
  `quest` added to `[all]`.
- `onboarding_skill = "find-paper"` declaration on `QuestHandler` →
  `/help` view + error-enrichment skill pointers on agent-confusion
  codes.
- Three seed skills bundled in the wheel:
  - `src/precis/skills/find-paper/SKILL.md`          — DOI/arXiv/title
    submission loop, three-step workflow, anti-patterns, outcome
    surfacing.
  - `src/precis/skills/quest-disambiguate/SKILL.md`  — confirm /
    repoint / flag / cancel decision tree, misconception-driven
    playbook per code.
  - `src/precis/skills/handle-dropped-pdf/SKILL.md`  — URL path via
    MCP + file-path path via CLI, Discord CDN failure modes,
    `pdf_mismatch` handling.
- Registry entry in
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/registry.py`
  with `KindSpec.examples` covering the most common read shapes.
  ImportError-gated so a lean install (`pip install precis-mcp`
  without `[quest]`) hides the kind cleanly.
- 29 tests in
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_phase12_quest.py`
  across `TestBareAndSingleId`, `TestRegistryViews`,
  `TestSubSelectorViews`, `TestHelpView`, `TestSearch`,
  `TestOnboarding`, and `TestRegistration`.  Mock `FakeDB` fixture
  with async method surface matching the real `DB`.

### Rationale

`acatome-quest-mcp` already uses PG + an out-of-band runner — the
shape of a state-backed precis kind.  Folding it in removes one MCP
from every agent's stack and lets quest participate in notifications,
hints, links, and the new skill-surfacing channels.  The three
skills that used to live in `grimoire/agents/quest-agent.md` and
`ansible/roles/feynman/templates/skills/cluster-library.md.j2` now
have a first-class home they can be read from and linked to.

Read-only in 12a is a deliberate scope cut — an agent can browse the
backlog, surface `needs_user` quests to the user, and understand the
shape of a quest card, without any risk of double-submits from a
draft implementation.  Writes in 12b go through the same async
`QuestService` the existing MCP server already uses, preserving all
the resolver / dedup / idempotency logic.

### Test totals

- **924 passed** (+29 from 12a).
- mypy: 32 source files, 0 errors.
- ruff: clean.
- `pip install precis-mcp` (no extras) still works — quest kind just
  doesn't appear in `stats()`.

### Deferred to Phase 12b

- `put(type='quest', text='…')` — submit
- `put(id='quest:<id>', mode='confirm'|'repoint'|'flag'|'priority'|'cancel')`
- `put(id='quest:<id>', mode='file', url='…')` — attach user-supplied PDF
- `Handler.hints()` with misconception-driven next-action hints
- Link-edge materialisation `quest:<id> ─[resolved_to]→ paper:<slug>`
  on `ingested` transitions
- Full port of the existing `acatome-quest-mcp` test suite

### Deferred to Phase 12c

- Retire `acatome-quest-mcp/src/.../server.py` (MCP layer)
- Rename package to `acatome-quest-runner`
- Migrate schema from `papers.requests` to `cluster.quest.*`
- Ansible role update — runner daemon only, no MCP entry point

## 3.5.1-dev — Phase 12b v1.1: skill surfacing hooks

Having a `skill:` kind is only useful if the agent finds the right
skill at the right moment.  v1.1 wires two passive surfacing channels
and ships three seed skills to exercise them.

### Added

- `Handler.onboarding_skill: ClassVar[str | None]` at
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/protocol.py:620`.
  Subclasses opt in by setting a skill slug.  Unset → no surfacing.
- `/help` view on `RefHandler`
  (`@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/handlers/_ref_base.py:320`)
  and on `FileHandlerBase`
  (`@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/handlers/_file_base.py:186-214`).
  `get(id='fc:/help')` / `get(id='todo:/help')` / `get(id='file:paper.tex/help')`
  now inline the full onboarding-skill body.  Delegates to
  `SkillHandler._render_skill()` — same rendering path as direct
  `get(id='skill:<slug>')`.  Graceful errors when `onboarding_skill`
  is unset (`VIEW_UNKNOWN`) or when the declared slug has no SKILL.md
  on disk (`ID_NOT_FOUND` with a "create skill:x in ~/.precis/skills/"
  hint).
- `_enrich_error` skill-pointer extension in
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/registry.py:1133-1146`.
  On `PARAM_INVALID` / `MODE_UNSUPPORTED` / `VIEW_UNKNOWN` the
  enricher appends `see get(id='skill:<onboarding_skill>') for the
  workflow` to the `next=` slot.  `ID_NOT_FOUND` is deliberately
  excluded — that error wants a `search()` / `/recent` hint, not a
  workflow primer.
- Bundled seed-skill directory at `src/precis/skills/` (shipped in
  the wheel via hatch `force-include`).  Automatically added to
  `SkillHandler` scan paths at lowest precedence so user / project /
  Claude Code skills can shadow.
- Three seed skills:
  - `src/precis/skills/sm2-basics/SKILL.md` — SM-2 review workflow,
    quality scale, tips against rote-grading.
  - `src/precis/skills/todo-triage/SKILL.md` — four-move triage
    loop (close / defer / reprioritise / split) for accumulated
    todo lists.
  - `src/precis/skills/tex-workflow/SKILL.md` — tex: URI grammar,
    node editing, .bib citations, raw line-range access.
- `onboarding_skill` declarations on `FlashcardHandler`
  (`sm2-basics`), `TodoHandler` (`todo-triage`), `TexHandler`
  (`tex-workflow`).
- 19 new tests in
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_phase12b_skill.py`
  across five test classes: `TestOnboardingSkillAttribute`,
  `TestBundledSeedSkills`, `TestRefHandlerHelpView`,
  `TestEnrichErrorSkillPointer`, `TestHelpViewE2E`, and
  `TestFileBaseHelpView`.

### Changed

- `FileHandlerBase.views` gained `"help"`.  (The set-shaped views
  advertise `/help` even though dispatch is via the inline if-ladder —
  `_enrich_error` uses views.keys() for `VIEW_UNKNOWN` options.)
- `SkillHandler._default_scan_paths()` now returns four paths
  (project-local, user-global, Claude interop, package-bundled).
  Documented the precedence order in the docstring.

### Rationale

Prior to v1.1 skills existed but nothing pointed to them.  The agent
had to discover them by reading the plugin-architecture doc, which
never happens.  v1.1 flips that: errors *pull* skills into the
agent's context when understanding is in doubt, and `/help` offers an
explicit escape hatch.  Both channels respect the "pointer first"
philosophy — we don't auto-inject full skill bodies into unrelated
responses; the agent decides whether to follow the pointer.

### Test totals

- 895 passed (+19 from this cycle, +60 total across Phase 12b so far).
- mypy 31 source files, 0 errors.
- ruff clean.

### Deferred to Phase 12b v1.2

- `CallContext.seen_kinds` + first-call skill injection (needs
  `invoke_handler` middleware; scope creep for now).
- `state-trigger` frontmatter field + `Handler.notifications()` hook
  for state-dependent pointers (e.g. "5+ todos → skill:todo-triage",
  "due flashcards → skill:sm2-basics").
- Auto-materialised `skill:X ─[applies_to]→ kind:Y` edges in the
  Phase 7 link graph.
- Seed skill for quest onboarding (needs Phase 12 quest fold-in).

## 3.5.0-dev — Phase 12b v1: `skill` kind + view-dispatch refactor

Two things in this cycle.  First, the view-dispatch mechanism on
`RefHandler` subclasses was hairy (stacked if/elif ladders in `read()`
plus a `_dispatch_view()` hook method that subclasses reimplemented);
we replaced it with a uniform registry.  Second, we landed the v1 of
the `skill:` kind — a filesystem-backed Agent Skills reader aligned
with the de facto standard (Anthropic Claude Code, adopted across
Cursor, Gemini CLI, Warp, community tooling).

### Added

- `SkillHandler` at
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/handlers/skill.py`
  — filesystem-backed reader/writer for SKILL.md directories.  Scans
  `./skills/` (project-local), `~/.precis/skills/` (user-global), and
  `~/.claude/skills/` (ecosystem interop, read-only) in precedence
  order.  Parses standard YAML frontmatter (`name`, `description`,
  `user-invocable`, `argument-hint`, `allowed-tools`, `path-scoping`)
  plus precis extensions (`applies-to`, `kind-onboarding`, `tags`).
  Always-on: no PG schema, no `ImportError` gating — pure stdlib +
  PyYAML.  35 tests covering frontmatter parsing, directory scan +
  precedence, read surface (bare list, single render, `/meta`,
  `/recent`, `/kind/<k>`, `/topic/<t>`), search, write surface
  (`append` / `replace` / `delete` with write confinement to
  `~/.precis/skills/`), and registration.
- `skill` scheme + entry-point registration in
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/pyproject.toml`
  and
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/registry.py`.
- `extract_kwargs(kwargs, keys, *, required=(), context="")` helper at
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/protocol.py:143`.
  Tuple-return validator: rejects unknown kwargs with `PARAM_INVALID`
  + `options=` auto-filled from the allowed list, enforces required
  kwargs with the same error shape, returns values in `keys` order
  for direct unpacking.  Used at the top of every view dispatch
  method so unknown kwarg names are caught method-locally with
  actionable errors.  7 tests covering valid extraction, missing
  optional, unknown rejection, missing required, context-in-cause,
  dispatch integration.
- `pyyaml>=6.0` as an explicit dependency (was a transitive).

### Changed

- **View dispatch refactor** — `_ref_base.py` replaced the if/elif
  ladder in `read()` and the `_dispatch_view()` subclass hook with a
  `views: dict[str, str]` registry mapping view names to dispatcher
  method names.  Every view dispatcher has the uniform signature
  `(self, store, ref, selector, subview, **kwargs) -> str` and calls
  `extract_kwargs()` at the top.  Subclasses extend via dict-merge:
  ```python
  class PaperHandler(RefHandler):
      views = {
          **RefHandler.views,
          "abstract": "_read_abstract_view",
          "cite":     "_read_cite_view",
          ...
      }
  ```
  Unknown views raise `VIEW_UNKNOWN` with `options=` auto-filled from
  the dict keys (no more scattered `_views_base` union logic).
  Applied across `paper.py`, `todo.py`, `flashcard.py`, `memory.py`,
  `conversation.py` — every `_dispatch_view()` method is gone, every
  `_views_base` reference is gone.
- `Handler` base class — `views` typed as `ClassVar[set[str] |
  dict[str, str]]`, `allowed_modes` as `ClassVar[set[str]]`.  Accepts
  the set shape (stateless handlers like `web`, `math`, `youtube`
  inline their dispatch and advertise views via a flat set) or the
  dict shape (state-backed handlers with dispatch methods).  The
  enricher iterates keys in either case; single code path.
- `_enrich_error` in `registry.py` — reads `handler.views` directly
  (works for both set and dict); dropped the `_views_base` union.

### Removed

- `Handler._dispatch_view()` method and every subclass override of
  it.  The view registry replaces it entirely.
- `RefHandler._views_base: set[str]` attribute and the handful of
  tests that asserted its presence.  `RefHandler.views` is now the
  single source of truth.

### Refactoring discipline

- All edits were type-checked against the declared `Handler`
  vocabulary (`views`, `allowed_modes`); mypy stays green (31 source
  files, 0 errors).
- 876 tests pass (+41 from this cycle: 6 for `extract_kwargs`, 35 for
  `SkillHandler`).
- Legacy tests that referenced `_dispatch_view` / `_views_base`
  updated to read from `views` directly.

### Deferred to Phase 12b v1.1

- Frontmatter `state-trigger` parsing + `Handler.notifications()`
  wiring for state-dependent skill pointers (e.g. "5+ todos → see
  `skill:todo-triage`").
- `Handler.onboarding_skill` attr + `/help` view cross-cut across
  every `RefHandler` / `FileHandlerBase`.
- `_enrich_error` extension that appends a skill pointer to `next=`
  on agent-confusion codes.
- `CallContext.seen_kinds` field for first-call onboarding debounce.
- Seed skills for quest / flashcard / tex / todo onboarding.
- Auto-materialised `skill:X ─[applies_to]→ kind:Y` edges in the
  Phase 7 link graph.

### Deferred to Phase 12b v1.2

- `cluster.skills.*` PG schema for versioning + draft/active status.
- `put(type='skill', mode='note')` — in-band annotation.
- pgvector-ranked search when the skill library grows past ~100
  entries.

## 3.4.0-dev — Phase 8: Structured errors + auto-enriched hints

Every `PrecisError` now carries a machine-readable `ErrorCode`, a
concrete `cause`, a list of `options`, and a `next` step hint.  The
framework auto-fills `options`/`next` from the handler's declared
vocabulary (`views`, `allowed_modes`, `writable`), so raise sites stay
terse while the agent gets a uniform, actionable multi-line error
shape on every failure.

### Added

- `ErrorCode` enum — 16 standard codes covering the full failure
  space: kind availability (`KIND_UNAVAILABLE`, `KIND_UNKNOWN`), id
  resolution (`ID_NOT_FOUND`, `ID_MALFORMED`, `ID_AMBIGUOUS`), verb /
  view / mode support (`VERB_UNSUPPORTED`, `VIEW_UNKNOWN`,
  `MODE_UNSUPPORTED`), parameter validation (`PARAM_INVALID`), write
  policy (`READONLY`, `DENIED`), infra (`TIMEOUT`, `RATE_LIMITED`,
  `UPSTREAM_ERROR`, `UNAVAILABLE`), and `UNEXPECTED` as the explicit
  "nothing more specific fits" sentinel.  Lives on
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/protocol.py`
  with the ordered tuple `ERROR_CODES` for stable iteration.
- `GRIPE_HINT_CODES` — the subset of codes
  (`UNAVAILABLE`, `RATE_LIMITED`, `UPSTREAM_ERROR`) for which the
  enricher appends a "file a gripe" next-hint so the agent surfaces
  upstream / infra failures to the operator.
- `PrecisError(code, cause, *, options=, next=)` structured
  constructor — the only supported signature.  Passing a bare string
  as the first argument now raises `TypeError` with a helpful message
  pointing at the enum.  Handler-supplied values always win over
  auto-fill.
- `Handler.allowed_modes: set[str]` class attribute — declared by
  every writable handler so the enricher can populate
  `options=` automatically on `MODE_UNSUPPORTED` / `VERB_UNSUPPORTED`.
- `_enrich_error(exc, handler, ctx)` in
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/registry.py`
  — auto-fills `options=` from `views` / `allowed_modes` / `VERBS`
  based on the code, and `next=` from a code → advice table (e.g.
  `ID_NOT_FOUND` → `search(query='...') to find refs`;
  `KIND_UNAVAILABLE` → `get(id='/help/install') to see bundles`).
  Wired into `invoke_handler` so every error that escapes a handler
  is normalised before it reaches the client.
- `_format_error(exc)` — unified multi-line output per §11.2:
  `!! ERROR <code>`, `   cause: <cause>`, `   options: ...`,
  `   next: <next>`.
- `tests/test_phase8_errors.py` — 25 tests covering signature
  hardening, enrichment rules per code, invoke_handler integration,
  and golden-output format.

### Changed

- **134 raise sites converted** across `handlers/_file_base.py`,
  `handlers/_ref_base.py`, `handlers/todo.py`, `handlers/flashcard.py`,
  `handlers/tex.py`, `handlers/paper.py`, `handlers/word.py`,
  `handlers/markdown.py`, `handlers/plaintext.py`, `handlers/web.py`
  (already structured), `handlers/youtube.py` (already structured),
  `registry.py`, `tools.py`, `_store.py`, and `protocol.py`'s own
  default stubs.  Every raise now passes an explicit `ErrorCode`;
  cause text is lowercase without a trailing period; options and next
  hints are either explicit or enricher-derived.
- `protocol.py` `Handler` base class — default `put()` stub raises
  `MODE_UNSUPPORTED` (was bare string); default `_write_note()` stub
  likewise.  These are structurally cleaner for handlers that inherit
  without overriding.
- `registry.py` `resolve()` — unknown extension is `PARAM_INVALID`
  with `options=` listing supported extensions; unknown scheme is
  `KIND_UNKNOWN` with `options=` listing registered schemes plus
  `file`.
- `tools.py` write-policy violations — ingestion-only and
  system-managed corpora now raise `READONLY` with explicit `next=`
  hints at the allowed alternatives (`mode='note'`, `link=`).
- Error-text style guide — cause strings are lowercase, terse, no
  period, quote agent-supplied tokens with `!r`; next hints are
  imperative and include one concrete next call; options are plain
  token lists (no prose) that the framework joins with commas.

### Removed

- **Legacy `PrecisError(bare_string)` form.**  Constructing an error
  without an `ErrorCode` now raises `TypeError` at call time.  Every
  in-tree raise site was audited and converted; no compatibility
  shim is provided (the phased upgrade in Waves 2a–2d completed
  before Wave 3 flipped the signature).

### Fixed

- Test drift: ~20 legacy tests in `test_markdown_handler.py`,
  `test_plaintext_handler.py`, `test_tex_handler.py`,
  `test_word_handler.py`, `test_todo_handler.py`,
  `test_flashcard_handler.py`, `test_registry.py`, and
  `test_phase7_links.py` updated to match the lowercased / restyled
  cause text and to read the `next` hint from `exc.next` instead of
  `exc.cause` where it moved.
- `xfail` placeholder in `test_phase8_errors.py` flipped to a live
  assertion now that Wave 3 landed.

## 3.3.0-dev — Phase 7: Links (cross-cutting primitive)

Links graduate from "paper-citations-only" to a first-class primitive
shared across every state-backed kind.  Adds the `unlink=` parameter
(dual of `link=`), an inbound-only `/links-in` view on every
RefHandler subclass, and fixes the scheme-prefixed-slug handling so
cross-kind link specs (memory → paper, todo → memory, …) work end
to end.

### Added

- `put(..., unlink='dst_slug[:relation]')` — new parameter on
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/tools.py`.
  Deletes every matching outbound link from the addressed ref/block
  to the target.  When a relation is supplied, only links with that
  relation are removed; otherwise every relation between the pair is
  cleared.  Always allowed regardless of write_policy (links are
  metadata, not content — same precedent as `link=`).  Raises
  `PrecisError` when no match is found, with a hint pointing at
  `get(id='<src>/links')` for inspection.
- `/links-in` view on every `RefHandler` subclass — inbound-only
  link listing (i.e. "what cites me", "what references this memory").
  Complements the existing `/links` view which shows both directions.
  Wired into `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/handlers/_ref_base.py`'s
  base view dispatcher; `_views_base` updated so every state-backed
  kind advertises it in its `views` set automatically.
- `_parse_link_spec(spec, default_relation)` helper — shared between
  `_create_link` and `_delete_link`.  Resolves the scheme-prefixed-slug
  ambiguity (`memory:a` means "full slug, no relation", not
  "slug=memory, relation=a") by checking the left side of the
  `rsplit(':', 1)` against the registered `SCHEMES` dict.
- `_store_slug_for(scheme, path)` helper — reconstructs the
  scheme-prefixed slug form that acatome-store uses for
  non-paper-family kinds (todo / fc / memory / conv).  Papers use
  bare slugs; everything else stores `<scheme>:<slug>`.  The URI
  parser's scheme-strip loses that prefix, so the link primitive
  needs to put it back before querying the store.
- Empty-state hints on `/links-in` — when no inbound links exist, the
  output says "no inbound links — nothing references this ref yet"
  rather than suggesting `put(link='...')` (which would be the wrong
  remedy for an inbound query).

### Changed

- `_read_links(store, ref, selector, *, direction='both')` — gains a
  keyword-only `direction` parameter.  Default `'both'` preserves
  existing `/links` behaviour; `/links-in` passes `'inbound'`.  Next:
  hints in the rendered output now adapt to the direction (inbound
  view suggests `/links` for the full picture; outbound/both view
  suggests `/links-in`).
- `acatome_store.models.CORPUS_SEEDS` — `memories` and `conversations`
  corpus seeds added (see Phase 6 entry below).  The `journal` seed
  stays for backward compat but is superseded by the two new specific
  kinds.

### Tests

- `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_phase7_links.py`
  — 14 tests across four classes:
  - `TestUnlinkDispatch` — 6 tests: unlink by dst only, by dst+relation,
    PrecisError on no-match, hint points at `/links` on wrong-relation,
    block-selector narrowing, `unlink=` short-circuits before
    mode-based write dispatch.
  - `TestLinksInView` — 4 tests: inbound-only rendering, empty-state
    message distinct from outbound empty, default direction is
    `'both'`, Next: hints adapt to direction.
  - `TestRefHandlerViewRegistration` — 2 tests: `links-in` is in
    `_views_base`, every state-backed subclass (todo, fc, memory,
    conversation) exposes it.
  - `TestCrossKindLinks` — 2 tests: memory → paper link creation
    and removal via the unified `link=` / `unlink=` interface.

### Deferred to Phase 7b / Phase 10

- Integer-id kinds (todo, fc) linking via their numeric URI (e.g.
  `todo:42`) still requires the store to resolve `ref_id → slug`
  before calling `get_links`.  The Phase 7 helpers handle
  scheme-prefixed string slugs correctly; integer-id resolution
  will land when the `precis-core` migration factors the store
  wrapper (§16 / Phase 10).
- `/links` views with unlinked-target notes (§9.4) — when a link
  points at a slug that doesn't exist, surface that in the rendered
  output.  Pending because the store currently rejects such links at
  creation time; loosening that is a Phase 7b concern.

## 3.3.0-dev — Phase 6: Journal kinds (memory, conversation)

Adds two new state-backed kinds to the monolith: `memory` for
long-term verbatim agent-memory drawers, and `conversation` for
session-level transcripts with turn-per-block streaming.  Both go
through acatome-store with new corpus seeds; both ImportError-gated
on the store.

### Added

- `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/handlers/memory.py`
  — `MemoryHandler` subclassing `RefHandler`.  Scheme: `memory:`.
  Corpus: `memories`.  Slug-based ids (auto-derived via `_slugify` or
  explicitly provided).  Views: base + `/recent` + `/tags`.  Write
  surface: `put(mode='append')` creates (errors as `ID_AMBIGUOUS`
  when the slug already exists), `put(mode='replace')` rewrites the
  content block, `put(mode='delete')` soft-deletes via meta flag.
- `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/handlers/conversation.py`
  — `ConversationHandler` subclassing `RefHandler`.  Scheme:
  `conversation:` (alias: `conv:`).  Corpus: `conversations`.
  Session-slug or UUID ids; each block is one speaker turn with
  `section_path=[speaker, timestamp]`.  Views: base + `/recent` +
  `/session` (full transcript rendering).  Write surface:
  `put(mode='append')` creates on first call / appends a turn on
  subsequent calls — matches the streaming agent workflow.
- `acatome_store.models.CORPUS_SEEDS` — two new seed rows.  `memories`
  (handler `memory`, pattern `memory:{title}`, write_policy `direct`)
  and `conversations` (handler `conversation`, pattern
  `conv:{session}`, write_policy `direct`).  Idempotent insert via
  the existing `seed_corpora()` path — no migration needed for fresh
  installs.
- Two new plugins registered in
  `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/src/precis/registry.py`'s
  `_register_builtins`.  Both gated on `ImportError` catching a
  missing acatome-store.  KindSpecs include agent-facing `examples`
  so the tool schema advertises concrete usage.
- `conv` alias on the `conversation` kind — ergonomic shorthand
  matching the `conv:<slug>` URI convention.

### Tests

- `@/Users/bots/Documents/openclaw-cluster/pips/packages/precis-mcp/tests/test_phase6_journal.py`
  — 35 tests across seven classes:
  - `TestSlugify` — 4 tests: basic, special-char stripping,
    length truncation, empty rejection.
  - `TestMemoryRegistration` — 3 tests: kind + scheme registered,
    `cost_hint="free"`, no env requirement.
  - `TestMemoryRead` — 6 tests: bare-scheme overview, empty state,
    `/recent` view, `/tags` histogram, empty tags, deleted memories
    excluded from `/recent`.
  - `TestMemoryWrite` — 8 tests: `append` requires text,
    slug-from-title derivation, explicit slug honoured, malformed
    slug rejection, tag list passthrough, comma-string tag splitting,
    duplicate slug → `ID_AMBIGUOUS`, delete marks meta.
  - `TestConversationRead` — 4 tests: empty overview, `/recent`
    turn counts, `/session` full-transcript rendering with speaker
    headers, empty session.
  - `TestConversationWrite` — 4 tests: `append` param validation,
    first-append creates ref, bare-slug normalisation
    (`2026-04-21-asa` → `conv:2026-04-21-asa`), delete marks meta.
  - `TestConversationRegistration` — 2 tests: kind registered,
    `conv` alias resolves.
  - `TestURIDispatch` — 3 tests: `type='memory'` builds
    `memory:<id>`, `type='conversation'` builds `conversation:<id>`,
    bare slug without prefix still routes via classifier.

### Deferred

- `/wake-up` view on memories (§7 Phase 6 doc) — ties into link-count
  ranking and cross-kind freshness signals.  Deferred to Phase 7b
  once the link primitive has real data flowing through it.
- Session streaming via PG `LISTEN/NOTIFY` — the put-append surface
  supports turn-per-call now, but real-time read-follow ("tail -f
  this conversation") is Phase 9 work when `deliver_to=` lands.
- Strict integer-id validation on todo/flashcard — the plan calls
  for rejecting slug-shaped ids on integer-id kinds.  Existing
  handlers are permissive (both forms route through `store.get`);
  since that's more user-friendly and hasn't caused issues, the
  hardening is deferred.

## 3.3.0-dev — Phase 3: Perplexity Sonar — web / think / research

Ports `perplexity-sonar-mcp` into the precis monolith as three distinct
kinds backed by a shared handler class family.  Implements the Phase 3
three-mode split from `docs/plugin-architecture.md` §13, with
escalating cost and latency so agents can pick the cheapest mode that
answers the question.

### Kinds

| kind       | model                      | timeout | cost_hint      | use case                                    |
|------------|----------------------------|---------|----------------|---------------------------------------------|
| `web`      | `sonar`                    | 30 s    | ~$0.001/call   | quick facts, definitions, current events    |
| `think`    | `sonar-reasoning-pro`      | 120 s   | ~$0.005/call   | comparisons, multi-source synthesis         |
| `research` | `sonar-deep-research`      | 600 s   | ~$0.50/call    | multi-step investigation (2–10 MIN)         |

All three require `PERPLEXITY_API_KEY` and are ImportError-gated on
`httpx` (part of the `[external]` extra).  When the env var is absent,
`visible_kinds()` hides all three from the agent enum and surfaces a
one-shot startup warning per §6.2.

### Attribution (mandatory)

Every successful response carries the Perplexity disclosure footer per
the Phase 4 attribution policy and Perplexity's
[Terms of Service](https://www.perplexity.ai/hub/legal/terms-of-service).
Footer names the specific model used, reminds the reader that
Perplexity is **not** a primary source (the numbered inline `[N]`
citations are), links to the ToS, and discloses the Standard/Pro
non-commercial-use restriction.  Inline `[N]` markers are preserved
verbatim from the Sonar content and a `Sources:` list renders the
underlying URLs.

### Added

- `precis/handlers/web.py` — `_WebBase` shared base class + three
  subclasses (`WebHandler`, `ThinkHandler`, `ResearchHandler`).  Each
  subclass sets `_MODEL` and `_TIMEOUT` class attributes; all shared
  logic (auth, HTTP call, error-code mapping, response formatting,
  attribution) lives on the base.
- `precis.handlers.web._format_response(data, model)` — pure function
  surfacing Perplexity content + citations + attribution.  Defensive
  against null `choices` / `citations` / empty `content`.
- `precis.handlers.web._attribution(model)` — Perplexity footer
  template.  Always names the specific model so downstream readers
  can gauge answer quality.
- `_call_sonar()` maps httpx errors to structured `PrecisError` codes:
  - HTTP 401 → `ErrorCode.DENIED` (bad API key)
  - HTTP 429 → `ErrorCode.RATE_LIMITED`
  - HTTP 4xx/5xx → `ErrorCode.UPSTREAM_ERROR`
  - `httpx.TimeoutException` → `ErrorCode.TIMEOUT`
  - `httpx.HTTPError` → `ErrorCode.UPSTREAM_ERROR`
- Three new entries in `_register_builtins` with per-kind `cost_hint`
  and agent-facing `examples`.

### Tests

- `tests/test_phase3_web.py` — 44 tests across eight groups:
  - `TestModeAttributes` — 5 tests pinning each subclass to its
    Perplexity model, scheme, timeout ordering, and read-only status.
  - `TestPerplexityAttribution` — 9 tests locking in the mandatory
    footer (names Perplexity, names model, links to ToS, warns
    "not a primary source", tells user to verify citations, discloses
    non-commercial restriction, present on every branch).
  - `TestFormatResponse` — 6 tests for content passthrough, citation
    numbering, no-sources-section when empty, whitespace stripping,
    null tolerance, empty-message placeholder.
  - `TestCallSonar` — 9 tests for HTTP-layer error mapping (401
    → `DENIED`, 429 → `RATE_LIMITED`, 500 → `UPSTREAM_ERROR`,
    timeout → `TIMEOUT`, connect error → `UPSTREAM_ERROR`), plus
    payload shape verification (correct model, correct query, correct
    auth header).
  - `TestRead` — 6 tests for end-to-end handler dispatch: empty-path
    rejection, query-param fallback, content/citations/attribution on
    success, per-kind model routing (think uses reasoning-pro,
    research uses deep-research).
  - `TestRegistration` — 5 tests: all three kinds register, all three
    hide without `PERPLEXITY_API_KEY`, all three visible with key,
    cost-hints ordered by depth, every kind declares the env
    requirement.
  - `TestServerDispatch` — 4 tests: `type='web'/'think'/'research'`
    build the right URI, explicit scheme prefix preserved.
  - `TestBaseContract` — 1 test: `_WebBase._MODEL` defaults to empty
    so an accidental direct registration fails loudly rather than
    silently querying a non-existent model.

### Deferred (Phase 9)

- `research` kind blocks synchronously for up to 10 minutes.  The
  Phase 9 `deliver_to=` async-dispatch primitive will unblock this —
  until then, agents must wait.
- Hermes profile rollout (`ansible-playbook playbooks/21-hermes.yml`
  to remove `perplexity-sonar-mcp` from profiles now that precis
  covers the same surface) lives in the `ansible/` subtree and is
  tracked separately.

## 3.3.0-dev — Phase 4: external stateless handlers (Math, YouTube)

Ports `wolfravant-mcp`'s Wolfram-Alpha client and `tubescribe-mcp`'s
YouTube transcript fetcher into the precis monolith as first-class
kinds.  Both handlers ImportError-gated (so the core install stays
lean) and env-gated via `KindSpec.requires` (so they auto-hide from
the agent enum when credentials are absent).

### Legal / attribution (mandatory)

- `MathHandler` appends a **Wolfram Alpha attribution footer** to every
  output path (success, failure, empty, did-you-mean).  Footer carries
  "Computed by Wolfram|Alpha", a deep-link to the specific query page
  (`https://www.wolframalpha.com/input?i=<url-encoded-query>`), a
  © Wolfram Alpha LLC marker, and the recommended academic-citation
  template.  Required per
  https://www.wolframalpha.com/termsofuse and the API commercial
  terms.  Implemented as `precis.handlers.math._attribution(query)`;
  query is URL-encoded with `quote_plus` so `+` becomes `%2B` and
  spaces become `+` (Wolfram's deep-link convention).
- `YouTubeHandler` appends a **source-attribution footer** to every
  successful output path (transcript fetch + `/languages` view).
  Footer carries the canonical watch URL, notes copyright belongs to
  the uploader (or YouTube's auto-generator), and asks the user to
  verify quotes against the original video before citing.
- Cross-handler policy memorialised in a workspace memory:
  "External-data handler attribution policy (precis-mcp)".  Every
  future stateless external-data handler (Perplexity, Wikipedia, URL
  fetch, etc.) inherits the same pattern — module-level
  `_<SOURCE>_ATTRIBUTION` template + `_attribution(id)` helper
  appended to every return path, with an explicit
  `Test<Source>Attribution` test class locking the footer in.

### Added

- `precis.handlers.math.MathHandler` — Wolfram Alpha wrapper.  Requires
  `WOLFRAM_APP_ID`; `cost_hint="~$0.0001/call"`.  Ported formatting
  from `wolfravant-mcp/src/wolfravant_mcp/server.py` verbatim for
  output parity with the standalone server.  Scheme: `math:`.
- `precis.handlers.youtube.YouTubeHandler` — YouTube transcript fetch
  via `youtube-transcript-api`.  No auth required; `cost_hint="free"`.
  Ported video-id extraction + language parsing from
  `tubescribe-mcp/src/tubescribe_mcp/transcript.py`.  Scheme:
  `youtube:`.  Supports `/languages` view.
- `pyproject.toml` `[external]` optional-dep group: `wolframalpha>=5.0`,
  `youtube-transcript-api>=1.0`, `httpx>=0.27`.  Rolled into `[all]`.
- Both handlers raise structured `PrecisError` with appropriate
  `ErrorCode` (`KIND_UNAVAILABLE`, `PARAM_INVALID`, `UPSTREAM_ERROR`,
  `ID_MALFORMED`, `ID_NOT_FOUND`, `UNAVAILABLE`) so `invoke_handler`'s
  unified error formatter surfaces the agent-readable shape.
- `KindSpec.examples` populated on both handlers so the tool schema
  builder can show concrete usage to the agent.

### Changed

- `_register_builtins` gains Phase 4 section registering both handlers
  after the state-backed kinds.  Missing-pip-extra triggers
  `ImportError` caught at the usual gate; missing env triggers
  `visible_kinds` to hide the kind (one-shot warning on first probe).
- `test_server_phase1.py::test_stats_shows_no_warnings_when_empty`
  now stubs `WOLFRAM_APP_ID` and resets `_ENV_WARNED` so the Phase 4
  math-hidden warning doesn't leak into its assertions.

### Tests

- `tests/test_phase4_external.py` — 50 tests across nine groups:
  - `TestExtractVideoId` — 9 URL-form variants (watch, youtu.be,
    shorts, embed, live, mobile) + malformed rejection.
  - `TestParseLanguages` — comma-list, whitespace, empty-entry
    fallback.
  - `TestYouTubeHandler` — transcript fetch, language preference,
    `/languages` view, `PARAM_INVALID` on empty path,
    `ID_MALFORMED` on non-YouTube URLs.
  - `TestFormatResult` — Wolfram pod formatter (success, failure,
    did-you-mean, empty, malformed-subpod defensiveness).
  - `TestMathHandler` — env-gated error, empty query, client dispatch,
    upstream-exception mapping.
  - `TestPhase4Registration` — kinds appear in `KINDS` / `SCHEMES`
    when deps installed; `math` auto-hides without
    `WOLFRAM_APP_ID`; `youtube` always visible.
  - `TestPhase4ServerDispatch` — `type='youtube'` / `type='math'` URI
    construction.
  - `TestWolframAttribution` — 9 tests locking in the mandatory
    attribution footer: Wolfram link present, deep-link URL encoding
    of `+` (`%2B`) and parens (`%28`/`%29`), `"Wolfram Alpha LLC"`
    copyright marker, academic-citation template, and
    attribution-on-every-branch (success, failure, empty,
    did-you-mean).
  - `TestYouTubeAttribution` — 5 tests: watch URL present,
    source-video id surfaced, verification/Cite warning, attribution
    on transcript fetch, attribution on `/languages` view.

## 3.3.0-dev — Phase 5: paper id auto-detection

Adds `classify_paper_id()` — a pure-function classifier that auto-
detects DOI / arXiv / PMCID / ISBN / ISSN / explicit-prefix / slug.
Ports DOI and arXiv regex/normalisers from `acatome-quest-mcp` and
adds the rest.  Wired into `_to_uri` so bare identifiers route to
the right scheme without requiring `type=`.

### Added

- `precis.paper_id` module — new.  Exports `PaperIdentifier` dataclass
  (scheme + value + note), `classify_paper_id(raw)`, plus pure-function
  normalisers: `normalize_doi`, `normalize_arxiv`, `normalize_pmcid`,
  `normalize_isbn`, `normalize_issn`.
- **DOI**: lifted `_DOI_IN_TEXT`, `_DOI_PREFIXES`, and `normalize_doi`
  from `acatome-quest-mcp/src/acatome_quest_mcp/models.py`.  Anchored
  bare-DOI pattern added for unambiguous classification.
- **arXiv**: `_ARXIV_ID_RE` (new form), `_ARXIV_OLD_RE` (old form with
  optional `.NN` subclass), `_ARXIV_PREFIXES`, `normalize_arxiv` —
  ported from the same module.
- **PMCID**: new regex `^PMC\d{5,10}$` (case-insensitive), URL-embed
  extraction, case-normalisation to upper.
- **ISBN-10 & ISBN-13**: new classifiers with full mod-11 / mod-10
  checksum validation.  Hyphens and spaces stripped.  Lowercase-`x`
  checksum normalised to upper.
- **ISSN**: new classifier with mod-11 checksum; accepts both
  hyphenated (`NNNN-NNNX`) and unhyphenated (`NNNNNNNX`) forms.
- **Explicit scheme prefixes**: `paper:` / `doi:` / `arxiv:` / `pmid:` /
  `pmcid:` / `isbn:` / `issn:` honoured verbatim, value still
  normalised before returning.
- **Ambiguous bare digits**: dispatch to `paper:` with a `.note`
  explaining that `pmid:N` is the next thing to try (§13.5 rule).
- Papers plugin extended: schemes now `["paper", "doi", "arxiv",
  "pmid", "pmcid", "isbn", "issn"]` and `KindSpec.aliases` mirrors
  that set so `type='pmid'` / `type='isbn'` resolve to the `paper`
  kind but preserve the identifier-type scheme in the URI.

### Changed

- `server._to_uri` — the legacy DOI-only auto-detect (`_DOI_RE`) is
  replaced by `classify_paper_id`, which covers every supported
  format.  File-extension routing still runs first so `report.docx`
  stays a file.  Selector suffixes (`›chunk`, `/view`) ride along
  through classification.
- `server._to_uri` — Phase 5 refinement to the Phase 1 kind-hint path:
  when the user-supplied `kind` is BOTH a `KindSpec` alias AND a
  registered scheme (e.g. `pmid` / `doi` / `arxiv` under the `paper`
  plugin), the URI emits the scheme name directly instead of collapsing
  to the canonical kind.  Lets `PaperHandler` branch on identifier
  type via `parsed.scheme` without losing the `type=` agent affordance.
- `test_server_phase1.py::test_alias_kind_resolves_to_canonical`
  renamed to `test_alias_kind_that_is_also_a_scheme_preserves_scheme`
  with updated assertion reflecting the Phase 5 refinement.

### Tests

- `tests/test_paper_id.py` — 76 tests across six groups:
  - `TestNormalizeDoi` — 7 tests (bare, prefixed, URL variants,
    trailing punctuation, rejection).
  - `TestNormalizeArxiv` — 8 tests (new form, old form, prefix, URL,
    PDF-URL, rejection).
  - `TestNormalizePmcid` — 5 tests (bare, lowercase, URL-embed,
    digits-only rejection, too-short rejection).
  - `TestNormalizeIsbn` — 9 tests (ISBN-10 + ISBN-13, hyphenated,
    X-checksum, case-insensitive, wrong-checksum rejection,
    wrong-length rejection).  Real-world test data
    (`9783161484100` from Wikipedia, `080442957X` from *The Elements
    of Style*).
  - `TestNormalizeIssn` — 5 tests (canonical, PNAS's actual ISSN,
    unhyphenated, checksum rejection, length rejection).
  - `TestClassify` — 21 tests exhaustively covering explicit
    prefixes, URL forms, bare structural patterns, ambiguous-digits
    hinting, and empty-input fallback.
  - `TestToUriClassifierIntegration` — 13 tests confirming
    `_to_uri` plumbs through to `classify_paper_id` with
    selector-suffix preservation and file-extension precedence.
  - `TestRegressionCases` — 3 tests (URL-looking slugs,
    parens/colons in DOI suffix, case-insensitive explicit prefix).

## 3.3.0-dev — Phase 2: cost reporting + always-on response footer

Every tool response now carries a `[cost: ...]` footer.  Session stats
accumulate per kind and surface via the `stats()` tool.  Per-call cost
resolution follows a three-level fallback (dynamic handler → static
`KindSpec` → default `"free"`).

### Added

- `precis.registry.CallStats` dataclass — `calls` / `errors` / `last_cost`
  counters per kind.
- `SESSION_STATS: dict[str, CallStats]` — process-local accumulator,
  populated by `invoke_handler()` on every call (success and error).
- `record_call(kind, cost_hint, *, errored=False)` — the writer-side
  helper.  Dedupes on kind; overwrites `last_cost` so agents see the
  most recent vendor string.
- `get_session_stats()` — returns a shallow copy for read-only
  consumption by `stats()`.
- `clear_session_stats()` — test helper.
- `cost_hint_for(kind, per_call)` — the three-level resolver:
  `per_call` > `KindSpec.cost_hint` > `"free"`.  Used by
  `invoke_handler()` on both success and error paths.
- `server._kind_from_uri(uri)` — extracts the canonical kind name from
  a URI scheme, running through `resolve_alias()` so `doi:` / `arxiv:`
  both resolve to `paper`.
- `server._dispatch(kind, verb, call, args)` — the Phase 2 wrapper
  that routes every tool call through `invoke_handler()` and renders
  the resulting `Result`.  Back-compat path: if the URI's kind isn't
  in `KINDS` (unusual — would mean a plugin registered a scheme
  without a `KindSpec`), falls through to raw `call()` with a
  best-effort error string.
- **Session-stats section in `stats()` tool** — per-kind `calls`,
  `errors`, and `last_cost` in stable sorted order.  Silent when no
  calls have happened yet ("(no calls yet)").
- **`cost_hint="free"` on built-in `KindSpec`s** (`paper`, `todo`,
  `flashcard`) — explicit, self-documenting, short-circuits the
  fallback at level 2.

### Changed

- **All four tool entry points (`search`, `get`, `put`, `move`) now
  route through `_dispatch`.** Every response gains `[cost: ...]` as
  its footer — agents see cost visibility on every call, even free
  kinds (§11 "always-on footer").  Per-call crashes are caught by
  `invoke_handler` and rendered as unified error strings.
- `invoke_handler()` records every call into `SESSION_STATS` (both
  success and error).  Error-path cost uses the static-fallback chain
  only — we never asked the handler for a per-call cost because it
  crashed.

### Tests

- `tests/test_phase2_cost.py` — 26 tests across five groups:
  - `TestCostHintFor` — three-level fallback semantics (per-call
    override, `KindSpec` fallback, ultimate `"free"`, empty-string
    treatment).
  - `TestRecordCall` — session-stats accumulator (create, increment,
    error flag, last-cost overwrite, copy semantics on read).
  - `TestInvokeHandlerCostAndStats` — success + error paths both
    record calls; paid handler's `cost_of` flows through; rendered
    responses always carry the footer.
  - `TestServerDispatchFooter` — `_dispatch` wraps `search` / `get` /
    `put` / `move`; tool outputs carry `[cost: free]`; session stats
    accumulate across tool calls.
  - `TestStatsSessionSection` — `stats()` renders the session block
    correctly (empty, populated, sorted, errors reported).
  - `TestPaidKindFooter` — per-call cost beats static `KindSpec`
    hint; `KindSpec` hint is the static fallback.
- `tests/test_invoke_handler.py::test_invoke_handler_cost_of_exception_does_not_break_success`
  updated to assert `r.cost == "free"` (new fallback semantics)
  instead of `r.cost is None` (Phase 0 "omit on crash" behaviour).

590 tests total (564 pre-existing Phase 0+1, all green).  ruff + mypy
clean.

## 3.3.0-dev — Phase 1: capability-driven enum + masking

Turns the Phase 0 scaffolding into agent-visible behaviour.  `PRECIS_KINDS`
becomes the primary per-agent masking env var; tools accept a `type=`
kwarg; kind-name collisions across plugins are fatal.

### Added

- `VERBS: frozenset[str]` constant in `precis.protocol` — the four
  agent-facing verbs, consumed by the parser and the registry.
- `precis.kinds_config` module — `parse_precis_kinds(value, *, aliases,
  known_kinds, warnings_out)` implements the bracket grammar from §13.
  Grammar and semantics are deliberately strict: alias-in-config,
  unknown verb, empty brackets, duplicate kind, and stray/nested
  bracket issues all raise `ConfigError`.  Unknown kind names are
  non-fatal — they get dropped with a warning so the server can still
  start.  `load_from_env()` is the env-var wrapper.
- `precis.registry.STARTUP_WARNINGS` — ordered list of accumulated
  non-fatal startup messages, surfaced via the new `stats()` tool.
- `precis.registry.RegistryError` — raised on kind-name collisions
  across plugins (§6.9).  Caught by `server.main()` and converted to
  `exit(2)` with a one-line stderr message.
- `set_kinds_mask()` / `clear_kinds_mask()` / `get_kinds_mask()` — the
  Phase 1 mask-state API on the registry.
- `visible_kinds(verb) -> list[RegisteredKind]` — applies the
  `PRECIS_KINDS` mask plus `KindSpec.requires` env gating, returns the
  kinds the agent should see for a given verb in stable sorted order.
  Missing-env rejections auto-log one warning per kind via
  `STARTUP_WARNINGS`.
- `resolve_alias(name) -> str` — canonical-name lookup via `ALIASES`,
  used at URI parse and by the parser's alias-fatal check.
- `add_startup_warning(msg)` / `clear_startup_warnings()` — writer-side
  helpers; dedupe on append.
- `server._to_uri(id, kind="")` — accepts a kind hint that resolves
  aliases and stamps the canonical scheme.  Back-compatible with the
  bare-id legacy path (no kind supplied).
- `server._load_kinds_mask()` — startup loader that parses
  `PRECIS_KINDS`, installs the mask, and funnels non-fatal warnings
  into `STARTUP_WARNINGS`.  Fatal `ConfigError` → exits with code 2.
- **`type=` kwarg on all four tools** (`search`, `get`, `put`, `move`).
  Optional (back-compat).  When set, dispatch goes through
  `_to_uri(..., kind=type)` — alias resolution included.
- **`stats()` tool** — read-only server introspection per §8 / §10.2.
  Shows active mask state, enabled kinds per verb, and any accumulated
  startup warnings.  Always public (no admin mode).
- Built-in plugins (`papers`, `todos`, `flashcards`) now declare
  explicit `KindSpec` descriptions — no more synthesised descriptions
  for the core kinds.

### Changed

- **Kind-name collision across plugins is now fatal** (§6.9).  Phase 0
  shipped it as a warning with the note "becomes fatal in Phase 1".
  Raises `RegistryError` with a message listing both plugins; the
  second plugin leaves no trace in `PLUGINS` / `SCHEMES` / `KINDS` /
  `CORPUS_PLUGINS`.
- `_register_plugin()` restructured into a dry-run phase (validate all
  declared kinds against existing `KINDS` first) followed by a commit
  phase.  A failed plugin never mutates registry state.

### Tests

- `tests/test_kinds_config.py` — 39 tests covering the bracket grammar
  (no-filter, bare kinds, bracketed verbs, mixed), every fatal path
  from §10.1 (unknown verb, empty brackets, stray/leading/trailing/
  doubled commas, duplicate kind, alias-in-config, nested/unclosed/
  unopened brackets, colon-in-name), unknown-kind warning behaviour,
  and the `load_from_env` wrapper.
- `tests/test_visibility.py` — 25 tests for the mask API, env-gating,
  `visible_kinds(verb)` per-verb filtering, `resolve_alias`, and the
  `STARTUP_WARNINGS` accumulator (including dedup and idempotence
  across multiple `visible_kinds` calls).
- `tests/test_server_phase1.py` — 24 tests for `_to_uri` kind-hint
  path, `_load_kinds_mask` fatal/non-fatal branches, `stats()` output
  shape, and the presence of `type=` on every tool signature.
- `tests/test_registry.py::test_kind_collision_is_fatal` replaces the
  Phase 0 warning-only test.

564 tests total (540 pre-existing, all green).  ruff + mypy clean.

## 3.3.0-dev — Phase 0 foundations (additive, no behaviour change)

Lays down the plugin-protocol v2 types and error-handling scaffold described
in `docs/plugin-architecture.md`.  Existing handlers and tools are unchanged;
these additions are consumed by later phases.

### Added

- `PLUGIN_PROTOCOL_VERSION = "1"` constant in `precis.protocol`.
- `KindSpec` dataclass — agent-facing capability declaration (name,
  description, aliases, required env vars, cost hint, examples).
- `CallContext`, `HintContext`, `NotificationContext` dataclasses for
  threading per-call / per-session state through handler hooks.
- `ErrorCode` (StrEnum) — frozen catalogue of 16 standard error codes
  covering the agent-facing error shape in §11.3.
- `Result` dataclass — unified response envelope with `.ok()` / `.err()`
  constructors and a `.render()` producing the final agent-visible string
  (result + Hints block + cost footer).
- `Handler.cost_of()`, `Handler.hints()`, `Handler.notifications()`
  optional hooks with safe no-op defaults.
- `Plugin.kinds: list[KindSpec]` field (optional, defaults to empty so v1
  plugins are untouched) plus `Plugin.protocol_version` for compatibility
  gating at registration time.
- `precis.registry.KINDS` and `precis.registry.ALIASES` dicts alongside
  `SCHEMES` / `FILE_TYPES`, populated from declared `KindSpec`s or
  synthesised defaults (first scheme canonical, remaining schemes become
  aliases, description lifted from the handler class docstring).
- `precis.registry.RegisteredKind` — registry wrapper around a kind plus
  its owning handler class and plugin.
- `precis.registry.invoke_handler()` — exception-isolated wrapper
  producing unified error strings via `_format_error()` and aggregating
  hints (cap 5 with dedup).  Not yet wired into `server.py`'s MCP tools;
  tests exercise it directly.  Non-user errors (`UNEXPECTED`, `TIMEOUT`,
  `UPSTREAM_ERROR`, `RATE_LIMITED`, `UNAVAILABLE`) gain an auto
  gripe-next-hint in their error output.

### Changed

- `PrecisError` now carries structured `(code, cause, options, next)`
  fields.  **Backward compatible**: the legacy single-string form
  `raise PrecisError("message")` still works unchanged, defaulting to
  `ErrorCode.UNEXPECTED`.  New code should prefer the structured form.

### Tests

- `tests/test_protocol_v2.py` — 28 tests covering the new dataclasses,
  `ErrorCode` catalogue, `PrecisError` legacy + structured compatibility,
  optional `Handler` hook defaults, and `Result.render()` output.
- `tests/test_invoke_handler.py` — 20 tests covering `_format_error`
  shape per §11.2 (where/cause/options/next/auto-gripe-hint), hint
  aggregation (dedup + cap + exception swallowing), and `invoke_handler`
  success/failure paths.
- `tests/test_registry.py` — extended with `TestSynthesiseKindSpecs` and
  `TestRegisterPluginKinds` for default spec synthesis, alias
  registration, collision warnings, and protocol-version mismatch refusal.

476 tests total (414 pre-existing, all green).  ruff + mypy clean.

## 3.0.0 — 2026-04-01

### Breaking

- **URI selector separator changed from `#` to `~`** — all selectors now use
  `~` (e.g. `paper:slug~38`, `doc.docx~PLXDX`). The `#` separator is no longer
  accepted.

### Added

- **MarkdownHandler** — read/write `.md` and `.markdown` files. Parses headings,
  paragraphs, fenced code blocks, tables, and lists. Zero extra dependencies.
- **PlainTextHandler** — read/write `.txt` and `.text` files. Paragraph-based
  parser (blank-line separated). Zero extra dependencies.
- **TodoHandler** — corpus-backed task management with state machine
  (pending → in_progress → done, blocked, cancelled). Requires `acatome-store`.
- **RefHandler** base class — extracted common corpus-backed read operations
  from PaperHandler. Provides TOC, chunk reading, search, summaries, links,
  and notes for any corpus-backed reference type.
- `PathCounter` in protocol for consistent node path generation.
- Entry points for new handlers (`.md`, `.markdown`, `.txt`, `.text`, `todo:`).
- Auto-create empty `.md` and `.txt` files on first access.

### Changed

- **PaperHandler** refactored to extend RefHandler (no API change).
- Registry now registers MarkdownHandler, PlainTextHandler, and TodoHandler
  as built-in plugins.
- All hint strings, error messages, and docstrings updated for `~` separator.

### Fixed

- Bump requests 2.32.5 → 2.33.0 (security).

## 2.2.1 — 2026-03-19

- Figure handling: `get(id='slug/fig')`, export to file
- List and table roundtrip in DOCX
- Citation validation and malformed-reference warnings
- Tracked changes and comment support in DOCX

## 2.2.0 — 2026-03-19

- Plugin registry with entry-point discovery
- Multi-ID batch reads: `get(id='slug1~4,slug2~9')`
- Grep and depth filtering in file handlers

## 2.1.1 — 2026-03-19

- LaTeX handler improvements
- URI parser with subview tails

## 0.4.1 — 2026-03-13

- Initial public release
