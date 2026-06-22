# Changelog

The v6.0 line is the ground-up redesign that started as the `v2`
branch and merged into `main` 2026-04-30. The last v1 release on
PyPI is `5.2.6`; everything below `## v6.0.0` represents the
post-merge state. Phases pre-merge are kept here as historical
context — see also `docs/phase*-plan.md` and `docs/design/v2-cutover.md`.

## Unreleased

### Changed (2026-06-22 — draft-work visibility + resumable plan_tick + dangling-finding flag)

From a prod transcript review of a stuck draft-enrichment todo (`#40577`
on draft `test01`), three fixes so a failed enrichment job stops being
invisible and self-perpetuating:

- **A `plan_tick` that hits `--max-turns` now *resumes* instead of
  bubbling.** A coroutine tick cut off at the 30-turn ceiling left work
  unfinished but is **resumable** — the next tick continues with a fresh
  budget. The executor now detects the `max_turns` terminal reason
  (`utils/claude_agent.stream_terminal_reason`) and marks such a tick
  succeeded-but-non-blocking (so `dispatch` re-mints a fresh tick next
  sweep) rather than `STATUS:failed` + a `child-failed` bubble that parks
  the parent. Bounded by a per-parent **streak cap**
  (`meta.plan_tick_max_turns_streak`, default 3, env
  `PRECIS_PLAN_TICK_MAX_TURNS_RESUMES`): a tick that runs out
  *repeatedly* still bubbles as a real failure — the task genuinely needs
  splitting. Any clean or non-max-turns tick resets the streak. The
  `job_result` audit reads `resumed (max_turns; streak n/cap)` so the
  forensics stay honest. (`workers/executors/claude_inproc.py`.) This is
  what failed `#40579`: 31 turns, $1.36, minted nothing, then bubbled and
  stalled the parent.
- **The draft view surfaces the work being done on it.** `get(kind=
  'draft', id=…)` (and the web reader) now end with a **`## Work in
  progress`** block — open todos in the draft's project subtree that are
  *blocked* (carry a `child-failed:*` bubble) or *in flight* (live /
  queued / failed child job), walked `draft → (draft-of) → project →
  subtree` (`store.draft_attached_work`). Previously a failed enrichment
  job parked its parent silently and never registered against the draft.
- **Dangling `[finding #<slug>]` markers are flagged on read.** The
  placeholder `[finding #amine-uptake]` / `[citation pending — finding
  #…]` form an author leaves in prose resolves to no finding ref (a
  finding is addressed by its base32 `pub_id`, not a made-up slug) — it
  never autolinks or exports. Reading a chunk now appends an **⚠
  unresolved finding reference** warning listing the slugs that don't
  resolve, so a reader can't mistake a placeholder for a live citation.
  Skill `precis-draft-help` updated. (`handlers/draft.py`.)

### Added (2026-06-21 — asks dismiss + draft compact rows)

- **Asks tab: dismiss with an ×.** Each ask row gains an X that closes
  the underlying todo (`STATUS:won't-do`) and strips its `ask-user`
  tags in one `tag` call — it leaves the queue without an answer and
  never re-enters the doable rotation. Sits beside the existing
  *Answer & unlock* form. (`routes/asks.py` `terminate`.)
- **Draft reader: on-demand PDF.** A **PDF** link in the reader header
  (`GET /drafts/{ident}/pdf`) exports the draft's LaTeX project and runs
  `latexmk`, **cached by the draft's version token** — the first hit for
  a version compiles, later hits serve the cached file; any edit bumps
  the version and triggers a fresh build. Degrades cleanly: a host
  without `latexmk` returns a friendly 503 (pointing at `precis draft
  export … --pdf`), and a LaTeX error returns the compile log tail
  rather than a 500.
- **Draft `get` outline reads as meaning.** `get(kind='draft', id=…)`
  now glosses each block with its `llm-v1` summary, falling back to the
  KeyBERT keywords, then the truncated first line (the prior behaviour)
  for blocks the workers haven't reached. Shared `store.block_views`
  backs both this and the web reader's view slider. The header
  pluralises correctly (`1 chunk` / `N chunks`) instead of `chunk(s)`.
- **Agent friction fixes from a prod transcript review (2026-06-22).**
  Reviewing plan_tick transcripts since the prior evening, every one of
  the 5 failures hit `max_turns (30)` — and the turns were burned on
  repeated tool errors, dominated by two patterns:
  - **`missing kind=` on `¶handle` ids** (26× in one tick → death). The
    skill teaches `get(id='¶xPJ5NF')` with no kind; the `¶→draft` /
    `§→paper` sigil routing that fixes it had **already** landed
    (`_infer_sigil_kind`), so deploying current `main` resolves this —
    the review confirmed it as the top failure cause.
  - **`put(kind='finding')` round-trips.** Required-field validation
    raised one field at a time (title → body → cited_in), so an
    under-specified call bounced repeatedly. It now reports **all**
    missing required fields in one error with a complete example.
- **`finding:<pub_id>` resolves in the web reader.** A `finding:ppxrf3`
  mention 404'd with "no such finding:ppxrf3": `finding` was a
  pure-numeric kind, so the resolver did `int('ppxrf3')` and bailed, and
  the slug lookup only matched `id_kind='cite_key'` anyway. `finding`
  now goes through the slug path, and both `_resolve_ref_id` (web) and
  `resolve_handle_ref` (write-time autolinker) match
  `id_kind IN ('cite_key', 'pub_id')` — so a finding resolves by its
  6-char base32 pub_id *or* its numeric ref id.
- **Draft hover-previews survive a live update.** Citation / `¶` hover
  popovers worked on first load but opened as an empty slot after the
  document refreshed: the draft reader's poll swapped fresh rows via raw
  `innerHTML`, which htmx doesn't auto-wire, so the injected chips had
  dead `hx-get` triggers. The swap now calls `htmx.process()` on the new
  rows.
- **Draft authoring guidance + write-time citation hint.** The
  `precis-draft-help` skill now prescribes `[§<cite_key>~<n>]` as *the*
  citation form (never a numeric ref id / bare `paper:` mention — those
  don't export to `\cite`), tells the writer to keep prose in every
  block (not bare structure/citations), and to avoid bold (sparing
  italics only). Backed by a tool-surface nudge: a draft `put`/`edit`
  whose text cites a paper as `paper:<id>` now appends a hint with the
  exact `[§<cite_key>~n]` replacement.
- **Console: tolerate comma-separated args.** `get kind=draft, id=test01`
  (Python-call style) now parses identically to the space-separated
  form. `shlex` kept the comma glued to the value, so `kind=draft,` was
  dispatched as the literal kind `'draft,'` and bounced with the
  self-contradictory *"unknown kind: draft,"* (which then listed `draft`
  among the options). `_parse_args` now strips surrounding commas off
  each token; a comma inside a quoted value is untouched.
- **Draft reader: close finished/failed change requests with ×.** The
  change-request chips in a block's meta column now carry a close-× on
  *done / won't-do* and *failed* requests as well as not-yet-started
  ones — only an actively-running request (a job minted, not yet
  terminal) has no × (can't delete it mid-run). Soft-deletes via the
  existing `/drafts/{ident}/todo/{id}/delete` route.
- **Draft reader: compact rows.** A `compact` toggle in the view
  controls. By default a block's row height is the *tallest* of its
  three columns, so a short paragraph with many connections/requests
  gets stretched by the meta column. Compact mode takes the meta
  column out of flow (`.draft-meta-inner` pinned + `overflow-y:auto`)
  so the **text drives each row's height** and the links/in-flight
  column scrolls internally. Works with the summary/keywords views
  too. **On by default**; persisted per draft in localStorage.

### Added (2026-06-21 — per-block Connections surface + agent anchor context)

- The draft reader's meta column gains a **Connections** surface: every
  ref linked *to or from* a chunk (`store.chunk_connections`) — `derived-from`
  provenance, `related-to` edges, and **dream-memories that reference the
  paragraph** — as terse `kind:id — title` hover-preview chips, collapsed
  behind a count. **Neighbour folding** rolls the prev/next paragraphs'
  connections in under a muted "nearby" sub-line. An **edit-churn** chip
  ("changed N× · ago", from `chunk_events`) shows how much a block has
  moved. This is the surface where dream output lands on the draft.
- The planner's **anchor block** (change-request prompt) now also lists
  what's linked to the anchored chunk (`chunk_connections`) so a
  change-request agent works *with* the provenance / dream context
  instead of blind.

### Changed (2026-06-21 — dreaming targets drafts, planner sees the anchor)

- **Planner prompt surfaces `meta.anchor`** — a web "around here…" /
  "review ▾" todo carries `meta.anchor='¶<handle>'`, but the per-tick
  prompt never showed it, so the agent saw only "remove this paragraph"
  and yielded `ask-user:` (the "see chunk 0" loop). An `## Anchor` block
  now names the chunk, shows its current text + linked connections, and
  says to act on it directly; a gone anchor tells the agent to ask a
  grounded question by `¶handle`.

### Added (2026-06-21 — draft LaTeX export, Tier-B increment 1)

- **`precis draft export <slug>`** renders a draft into a compilable
  LaTeX project (`main.tex` + `refs.bib` + a copy of the checked-in
  `preamble.tex`). The output is **disposable** (re-export, never
  hand-edit), which lets the resolution pass stamp machine labels on
  everything: each block gets `\label{chunk:<handle>}` and a `[¶h]`
  cross-ref becomes `\cref{chunk:h}`; `[§slug~n]` / bare `paper:slug~n`
  citations become `\cite{slug}` with a `refs.bib` generated from the
  cited paper refs (DOI/arXiv from `ref_identifiers` when known); every
  defined abbreviation becomes a `\newacronym` and each surface
  occurrence a `\gls{key}` (first-use-full / later-short, with the
  page-number "where it occurs" glossary list). Inline rendering reuses
  the single-sourced `mentions` grammar atoms (so it can't drift from the
  reader/parser) and handles `**bold**` / `` `code` `` / `<sub>`/`<sup>`
  / `$…$` math (verbatim) / LaTeX-special escaping. Authoring `[[…]]`
  links and bare thought mentions render to nothing (provenance only).
  Engine: `src/precis/export/latex.py`; preamble:
  `src/precis/data/templates/draft/preamble.tex`. Compile + LLM-repair
  loop and the Word/pandoc (docx + EndNote RIS) path are later
  increments.

### Changed (2026-06-21 — draft find bar, dreaming, abbrev recall)

- **In-draft find bar** on the reader: a verbatim (case-insensitive
  substring, document order) / semantic (cosine-ranked over the draft's
  chunk embeddings) toggle via `GET /drafts/{ident}/find`, with `‹ ›`
  cycling from the chunk currently in view, wrap-around, and auto-expand
  of a collapsed section when a hit lands inside it. Semantic degrades to
  verbatim when no embedder is wired.
- **Dreaming now targets `draft` chunks** — `draft` is added to the
  runtime's dream-target kinds (the salience `argmax` pool **and** the
  `angle` inspiration spray), so the dreamer wanders the live project
  write-up, not just the frozen corpus + crystallised memories. The
  selection queries (`select_salient`, `dreamable_region`,
  `_nearest_chunk`) gained a `c.retired_at IS NULL` guard so soft-deleted
  draft chunks don't surface (a no-op for paper/memory).
- **Abbreviation recall** in the reader: every occurrence of a defined
  abbreviation renders as a native `<abbr title="long form">` (hover
  definition, zero JS). Definitions come from `store.defined_abbrevs()`
  — explicit `term` chunks plus inline `Long Form (ABBR)` first-uses,
  explicit winning on a clash. `linkify_refs` gained an `abbrevs=` arg
  whose highlight pass runs over the rendered HTML's text runs only.

### Fixed (2026-06-21)

- The `/drafts/{ident}` reader 500'd on real Postgres: `_requests_by_handle`
  had literal `%` in a parameterised `LIKE` (`'ask-user:%'` /
  `'child-failed:%'`), which psycopg reads as a placeholder. Doubled to
  `%%`; added a real-Postgres regression test (the fake-store web tests
  don't parse SQL, so they couldn't catch it).

### Changed (2026-06-21 — drafts reader is now a per-block 3-column grid)

- The draft reader (`GET /drafts/{ident}`) is reworked from TOC-left into
  a **per-block row grid**: one row per chunk, three columns — **content**
  (raw source via `linkify_refs` + **KaTeX** for `$…$`, hierarchy-indented,
  headings collapse their subtree), **meta** (terse: the refs the block
  makes as chips + in-flight change-request todos anchored at it, with
  `STATUS:`), and a per-block **change box** ("around here…" → an anchored
  todo). Headings carry a ▸/▾ caret; collapse state persists in
  `localStorage`; a `#c-<handle>` deep link auto-expands its ancestors.
- **Live refresh** — the reader polls `GET /drafts/{ident}/version`
  (`max(chunk_events.event_id)`) every 4s and, when it bumps, swaps the
  row set from `GET /drafts/{ident}/rows` in place (collapse state
  preserved via `localStorage`, KaTeX re-rendered). It **defers to a
  manual "document changed — refresh" pill while a change box is
  focused**, so an agent edit landing mid-type never clobbers what
  you're writing; a ● live/paused toggle turns polling off. Rows render
  through a `draft_row` macro reused by the full page, the rows fragment,
  and a per-row `GET /drafts/{ident}/row/{handle}` endpoint.
  `GET /draft/{ident}` 303-aliases the plural reader.
- Ref chips reuse the shared superset grammar (`draft_markup` +
  `mentions`). Chip hover-previews are deferred (the `_anchor_html`
  popover machinery drops in later).
- Test hygiene: the two paper-triage→S2 route tests now
  `pytest.importorskip("habanero")` so they skip cleanly on the lean host
  venv (the `[paper]` extra is container/CI-only) instead of erroring,
  without losing the coverage where the dependency is present.

### Added (2026-06-21 — search gains an explicit lexical / semantic `mode=`)

- **`search(mode=…)`** lets the LLM pick the ranking instead of always
  getting hybrid fusion: `'hybrid'` (default, unchanged — RRF of lexical
  + semantic), **`'lexical'`** (Postgres FTS only — deterministic
  keyword / exact-phrase / identifier / numeric matching, and the honest
  tool when the embedder is down), `'semantic'` (embedding cosine only).
  Previously lexical-only happened only as an involuntary degrade when
  the embedder failed; there was no way to *request* it.
- One store entry point: `Store.search_blocks(mode=…)` dispatches over
  the existing `search_blocks_lexical` / `_semantic` / `_fused`.
  `search_blocks_semantic` gained `offset` + `exclude_ref_ids` for
  pagination parity. A `query_vec_for(embedder, q, mode)` helper skips
  the embed entirely for `mode='lexical'`.
- Threaded through every block-search handler (paper, plaintext, patent,
  the cache-base for memory/web/etc.) and the cross-kind fan-out
  (`mode='lexical'` skips the shared embed). `mode='semantic'` with no
  vector degrades to lexical, matching the embed-guard philosophy.
- Validated at the verb boundary (unknown mode → `BadInput`).
  `precis-search-help` documents the matrix + when to reach for lexical.
- Coverage: `test_search_blocks_mode.py` (5 cases — lexical-without-embedder,
  semantic, degrade, hybrid-parity, lexical-ignores-vector).

### Added (2026-06-21 — draft references: DRY superset highlight+extract, and a Tier-A web viewer/editor)

- **Draft inline references reuse the existing reference machinery
  rather than a parallel stack (ADR 0033 §8).** The bracket/sigil
  regex atoms now live in `utils/mentions.py` (the grammar SSOT —
  `AUTHORING_PATTERN` / `DISPLAY_LINK_PATTERN` / `BARE_BRACKET_REF_PATTERN`
  / `DRAFT_CITE_PATTERN`); `utils/draft_markup.py` parses against them
  and resolves via the shared resolvers (`resolve_handle_ref` /
  `chunk_to_pos`), adding `resolve_draft_handle` (`¶`→chunk) and
  `resolve_draft_link_targets`. The recognised set is the **superset**:
  bracket forms ∪ bare `kind:ref` mentions. `draft` joins
  `LINKIFY_KINDS`.
- **`linkify` highlights the superset in one pass.** Display links show
  their text, `¶handle`→`/c/<handle>`, `§paper~n`→paper anchor, `[[…]]`
  surfaces the inner handle, external URLs open a new tab; unrecognised
  `[x](y)` stays literal. The hover-preview popover markup is factored
  into `_anchor_html`, so `¶` chunk anchors get the **same** hover
  preview + click-navigate as every `kind:ref`.
- **`DraftHandler` auto-links on write.** `_sync_draft_links` mirrors
  the note autolinker: add/edit/retire recomputes the draft's whole
  reference set and replaces the `auto='mention'` `related-to` edges
  (intra-draft `¶` refs excluded — document-internal, not edges).
- **Drafts web tab (Tier A, ADR 0033 Phase 6).** `precis_web/routes/drafts.py`
  + templates: `GET /drafts` (list), `GET /drafts/{ident}` (TOC-left
  reader rendering **raw source** through `linkify_refs`, anchored
  `#c-<handle>`, with a links/backlinks panel and a per-chunk
  change-request box), `POST /drafts/{ident}/request` (a `todo` anchored
  at the chunk, parented on the draft's project), `GET /c/{handle}`
  (resolve a `¶` handle → redirect into the reader), `GET /preview/chunk/{handle}`
  (the `¶` hover-popover fragment). Resolved rendering (computed
  §-numbers, KaTeX, cross-ref/citation resolution) is the export engine
  (Tier B), shared across HTML/LaTeX/Word targets.
- Coverage: 10 linkify superset cases, 4 draft autolink cases, 7 draft
  route tests.

### Fixed (2026-06-20 — web: autoescape was OFF; Tasks buttons went dead on planner-prompt titles)

- **Jinja autoescape was disabled for the entire web UI.** The
  templates are named `*.html.j2`, but `select_autoescape()` only
  enables on `.html`/`.htm`/`.xml` — so every `{{ value }}` rendered
  **raw HTML**. A planner (`LLM:*`) todo whose title contained literal
  placeholder syntax (`q='<title or DOI>'`, `text='<claim>'`) opened a
  real `<title>` element on the Tasks page, flipping the HTML tokenizer
  to RAWTEXT and swallowing the rest of the document — every inline
  `<script>` after it (the filter-chip + collapse/expand handlers)
  stopped executing. Symptom: those buttons did nothing, with **no JS
  error** (the markup was silently consumed as text); `+Strategic` kept
  working because Alpine loads as a deferred external script. It was
  also a broad stored-XSS hole. Fix: `_make_jinja_env` now passes
  `enabled_extensions=("html","htm","xml","j2")` so escaping is ON.
- **`linkify_refs` no longer trusts its input as HTML.** It returns
  `Markup` (bypassing autoescape), so it must escape its own prose — it
  used to pass non-match text through verbatim and skip-zone `<code>` /
  `<pre>` / `<a>` blocks unescaped, the same injection surface. Now all
  text is HTML-escaped; only the generated anchors are live markup.
- **Footnote markers compose inside the escaping pass.** The memory
  detail page used to splice raw `<sup><a href="#ref-N">` HTML into the
  body before linkify (now neutralised by escaping). The numbering map
  is threaded through `linkify_refs(value, footnotes)` instead;
  `_inject_footnote_markers` (and its stale skip-zone regex) is gone.
- Regression coverage: `test_untrusted_html_is_escaped_not_rendered`,
  `test_script_injection_is_escaped`, plus the rewritten foreign-markup
  tests in `test_linkify.py`.

### Fixed (2026-06-20 — worker survives a down embedder at boot)

- **The system worker no longer crash-loops when the embedder is
  unreachable at startup.** `EmbedHandler.__init__` eagerly read
  `embedder.model` (a `GET /model` round-trip on the remote backend);
  if the embedder was down — e.g. mid-restart during a redeploy — that
  raised `RuntimeError` out of `_build_handlers` and launchd
  crash-looped the *entire* worker, taking `summarize` / `chase` /
  `fetch` / `dispatch` down with the one pass that actually needs the
  embedder. `model_name` / `name` are now resolved lazily (and cached)
  on first use, so construction is network-free: the worker boots and
  runs every other pass; only the embed pass bears the dependency, the
  runner skips just that pass while the embedder is unreachable, and it
  recovers on the next cycle once the embedder is back — no restart
  needed. The two `_build_handlers` call-sites that filtered handlers
  by `h.name.startswith("embed:")` now use the `isinstance` check alone
  (equivalent, and it avoids re-triggering the boot-time round-trip).
  Regression tests: `test_construction_does_not_touch_embedder`,
  `test_model_name_resolved_once_and_cached`. (Complements the
  `redeploy-precis.yml` ordering fix in the cluster repo, which gates
  the daemon bounce on embedder `/healthz`; this makes the crash-loop
  impossible regardless of deploy timing.)

### Added (2026-06-20 — OA fetcher: publisher APIs + aggregator legs)

- **Six new legs in the `fetch_oa` cascade.** It grows from
  publisher → Unpaywall → arXiv → S2 to a ten-source chain ordered to
  prefer the *version of record*, with green-OA + preprint as late
  fallbacks: **publisher → elsevier → wiley → unpaywall → crossref →
  openalex → europepmc → core → arxiv → s2**. Two are publisher
  full-text APIs (key/token-gated), three are keyless DOI aggregators,
  one is a key-gated green-OA repository net.
  - **`fetcher:wiley`** (`_try_wiley`, token-gated on
    `PRECIS_WILEY_TDM_TOKEN`) — Wiley TDM API
    (`tdm/v1/articles/<doi>` + `Wiley-TDM-Client-Token`) streams the
    full-text PDF for entitled + gold-OA Wiley/Blackwell DOIs
    (`10.1002`/`10.1111`). Verified end-to-end (4.36 MB PDF).
  - **`fetcher:core`** (`_try_core`, key-gated on `PRECIS_CORE_API_KEY`)
    — CORE search by DOI → repository `downloadUrl`s (green-OA copies
    of paywalled-publisher papers), tried in turn behind the `%PDF-`
    guard with a browser UA (CORE's API + many repositories
    Cloudflare-gate non-browser agents). Best-effort: lands the
    bot-friendly repositories, falls through on the rest.
  - Module-level `_BROWSER_UA`; `_download_first` / `_download_pdf`
    gained `extra_headers` forwarding (Wiley token, CORE UA).
    Tests: `TestTryWiley` / `TestTryCore`.
  - Cluster wiring: `PRECIS_WILEY_TDM_TOKEN` / `PRECIS_CORE_API_KEY`
    added to `precis_shared_env` (`vault_wiley_tdm_token` /
    `vault_core_api_key`).

  The first four of the six (below) shipped together:
  - **`fetcher:elsevier`** (`_try_elsevier`, key-gated on
    `PRECIS_ELSEVIER_API_KEY`) — the only auto-route for ScienceDirect.
    Elsevier has no keyless DOI→PDF (the PDF endpoint 403s bots; the
    PII isn't in the DOI), and Unpaywall/OpenAlex return only the
    doi.org landing page for hybrid-OA Elsevier articles. The Article
    Retrieval API (`content/article/doi/<doi>` + `X-ELS-APIKey` +
    `Accept: application/pdf`) streams the full text directly —
    verified end-to-end on `10.1016/j.amf.2025.200253` (11.4 MB PDF,
    no insttoken needed for OA content). Gated to the `10.1016`
    registrant prefix; silent no-op when the key is unset.
  - **`fetcher:crossref`** (`_try_crossref`) — tries Crossref
    `message.link[]` entries whose `content-type` is a PDF (publisher
    TDM links). Needs the polite `mailto` (= `PRECIS_UNPAYWALL_EMAIL`);
    the anonymous pool connection-resets.
  - **`fetcher:openalex`** (`_try_openalex`) — OpenAlex `pdf_url`
    locations; different coverage to Unpaywall, often a working
    green-OA repository copy where UPW returned a landing page.
  - **`fetcher:europepmc`** (`_try_europepmc`) — resolves DOI→PMCID via
    the Europe PMC search API and renders the OA-subset PDF
    (`europepmc.org/articles/<pmcid>?pdf=render`); high-yield for the
    biomedical corpus.
  - `_download_pdf` gained an `extra_headers` param (Elsevier key);
    headers ride redirect hops, so it's documented for trusted
    fixed-host endpoints only (the SSRF guard still caps redirects to
    public hosts). New shared `_download_first` tail for the
    list-of-candidate-URL legs. All legs reuse the `%PDF-` magic-byte
    guard + `safe_stream` SSRF revalidation; a non-PDF (paywall HTML,
    XML API error) degrades to `fetch_failed` and the cascade
    continues. Tests: `TestTryElsevier` / `TestTryCrossref` /
    `TestTryOpenalex` / `TestTryEuropepmc` + cascade-ordering cases.
  - Cluster wiring (separate `cluster` repo): `PRECIS_ELSEVIER_API_KEY`
    added to `precis_shared_env` (`vault_elsevier_api_key`).
  - **Backoff note:** every DOI now potentially logs up to ~6
    `fetcher:%` no-OA events per pass (was 2). Since the claim-query
    backoff counts events, a no-OA-anywhere stub reaches the 30-day
    cap after ~2 passes instead of ~3 — front-loaded effort, then
    monthly retries (unchanged philosophy).

### Added (2026-06-20 — OA fetcher: deterministic publisher PDF patterns)

- **New first leg in the `fetch_oa` cascade: `fetcher:publisher`.**
  Some OA publishers serve the full-text PDF at a URL derivable from
  the DOI alone (exactly like arXiv's `/pdf/<id>.pdf`). Unpaywall
  routinely misses these on fresh DOIs — it returns the HTML *landing
  page* as the "OA location", which our `%PDF-` magic-byte guard
  rejects (`fetch_failed`) — and S2 reports `no_oa_version` for days
  after publication. `_try_publisher` maps a DOI registrant prefix to a
  deterministic PDF endpoint and runs *before* the aggregators.
  Seeded with **Springer/BMC** (`10.1186` BMC/SpringerOpen + `10.1007`
  hybrid Springer → `link.springer.com/content/pdf/<doi>.pdf`) and
  **PLOS** (`10.1371` → `journals.plos.org/<journal>/article/file?id=<doi>&type=printable`,
  journal slug derived from the DOI infix). A non-registry prefix
  returns `None` (silent skip, no event — same contract as `_try_arxiv`
  for a missing arXiv id), so the long tail is unaffected; a wrong guess
  for a paywalled article fails the `%PDF-` guard and falls through to
  Unpaywall/arXiv/S2 rather than poisoning the ingest. Motivating case:
  `10.1186/s13027-026-00740-z` (BMC, fully OA) sat in the papers-needed
  backlog because Unpaywall pointed at the doi.org HTML page and S2 had
  no OA version — the Springer PDF is a deterministic URL away. Tests:
  `TestPublisherUrls` / `TestTryPublisher` + cascade-ordering cases in
  `tests/workers/test_fetch_oa.py`. The registry is the extension point
  for further deterministic OA publishers (Frontiers, eLife, PeerJ,
  bioRxiv/medRxiv) once each URL pattern is verified.
### Added (2026-06-20 — precis.skills plugin entry-point group)

- **Third-party packages can contribute LLM-facing skill docs.** New
  `precis.skills` entry-point group, mirroring `precis.handlers`.
  `_load_skills_map` (the single chokepoint feeding both
  `get(kind='skill')` via `_load_skill` and `search(kind='skill')` via
  `_get_index`/`FileCorpusIndex`) now also walks skill `*.md` roots
  resolved from the group, in addition to the built-in
  `precis.data.skills` package. Built-ins load first and win slug
  collisions; a broken plugin root is logged and skipped (same failure
  isolation as `dispatch._load_plugins`). `_walk_skill_root` factored out
  (duck-typed on `iterdir`/`is_dir`/`name`/`read_text` so a `Path` or an
  importlib `Traversable` both work). Lets the `precis-chain` plugin ship
  its `service`/`chain` skills. See ADR 0032; tests in
  `tests/test_skill_plugin_group.py`.

### Fixed (2026-06-20 — web untriage / paper tag+link by numeric id)

- **"Clear flag" / "Looks fine — remove needs-triage" now actually
  clears the flag.** The web addresses papers by numeric `ref_id`, but
  `PaperHandler._resolve_paper_slug` (used by `tag` and `link`)
  stringified the id and looked it up as a *cite_key* — a guaranteed
  miss that raised `NotFound`. The `POST /papers/{id}/untriage` route
  discards the dispatch error and redirects regardless, so the button
  appeared to work while the `needs-triage` tag survived. `_resolve_
  paper_slug` now takes the same numeric branch `_resolve_paper_ref_id`
  already had (slugs are never all-digits, so it's unambiguous), and the
  latter is now a thin wrapper over it. This also fixes any paper
  `link`/`tag` op addressed by numeric id from the web. The web route's
  existing test mocked the runtime, so it never exercised the real
  resolver — added handler-level coverage in `tests/test_paper.py`
  (`TestNumericIdTagLink`).
- **No web mutation route can silently swallow a dispatch error.** The
  `redirect-on-success / render-the-error-on-failure` wrapper that
  `routes/tasks.py` already had (`_redirect_or_error`) is promoted to
  `precis_web.deps.redirect_or_error` and is now the single mutation
  surface: `tasks`, the generic `refs` tag route, the `asks` answer
  unlock, and the papers `untriage` button all route through it. The
  untriage route is now a thin named *preset* over the generic `tag`
  verb — the same dispatch `/refs/{kind}/{ref_id}/tags` uses — rather
  than a parallel code path. A failed dispatch (bad tag, guard veto,
  unresolvable id) now renders the handler's own message + `next=` hint
  as a 400 instead of a 303 that pretends success. Regression test:
  `test_paper_untriage_surfaces_dispatch_error`. (Paper *delete* stays a
  direct `Store.soft_delete_ref` call by policy — not on the MCP
  surface — and already surfaced its own `NotFound`.)

### Added (2026-06-20 — skill search: body-only embedding twins)

- **Heading-stripped twin vectors for skill search.** The skill
  embedding chunker (`skill_index/chunker.py`, `CHUNKER_VERSION` 2→3)
  gains an opt-in `with_body_aliases=True` mode that, for each section,
  emits one extra `body_only=True` chunk carrying the section body
  *without* its heading line. The structural per-heading chunks fuse
  heading + body into one vector — great when the heading labels the
  body, noise when it doesn't (`## Gotchas` over a body about SSRF
  redirects); the twin de-noises that case. For an alias group the body
  is shared, so it's one twin regardless of alias count. Twins are an
  **embedding-surface** concern only: the `FileCorpusIndex` chunks with
  them on, but the `slug~N` chunk addresser and the TOC adapter keep the
  structural-only default and never see them (the TOC adapter filters
  `body_only` out before its 1:1 embedding alignment). The search
  `more` column also excludes twins so it still counts distinct matching
  sections. Cache invalidates lazily on the `CHUNKER_VERSION` bump.

### Added (2026-06-19 — papers triage: delete, untriage, duplicate resolver)

- **Delete a paper from the web triage UI.** The detail page's Delete
  button is now live (it had been a disabled placeholder) and the triage
  queue grows a per-row 🗑 quick-delete — so a non-paper that slipped in
  (e.g. a patent) can be removed. Delete is **web-only by policy**: the
  route (`precis_web/routes/papers.py`) calls `Store.soft_delete_ref`
  directly rather than dispatching, so `paper` keeps `supports_delete=
  False` and paper deletion is *not* exposed on the agent MCP surface.
  Soft delete (sets `deleted_at`); reversible at the DB level.
- **Manual untriage.** A successful metadata edit already clears
  `needs-triage`, but a paper that's actually fine (or fixed by hand)
  stayed stuck in the queue. New `✓ Looks fine — remove needs-triage`
  control on the detail triage panel + a per-row ✓ in the queue, both
  via a new `POST /papers/{id}/untriage` (dispatches a tag-remove).
- **Editable short handle (cite_key) with a suggestion.** The paper edit
  form gains a `cite_key` field pre-filled with a free, system-format
  suggestion (`surname` + 2-digit year, collision-suffixed) derived from
  the fixed author + year — so a triaged paper minted as `anon06b` can
  become `piela07`. New `Store.suggest_cite_key` does the DB-backed
  collision probe. Saving a changed handle re-slugs the paper
  (`set_ref_identifier(cite_key)`) **and moves the PDF** to its new
  sharded path on disk (`<corpus>/p/piela07.pdf`); a handle-only change
  skips the metadata dispatch, an illegal/taken handle surfaces inline.
  Web-only (the rename touches the filesystem), same policy as delete.
- **Duplicate-identifier resolver.** Saving a DOI/arXiv id that already
  belongs to another paper used to dump a raw 400 (`… already belongs to
  ref id=N`). The edit route now parses that conflict and renders
  `papers/edit_conflict.html.j2`: it links to the owning paper's detail
  **and PDF** (open in a new tab to compare) and offers a one-click
  delete of the copy being edited. `return_to` lands the operator back
  on the triage queue (constrained to `/papers*` — no open redirect).

### Added (2026-06-19 — `alert` kind + `/alerts` web tab)

- **New first-class kind `alert`** (migration `0029_alert_kind.sql`) —
  a home for machine-detected operational / health conditions (worker
  spin loops, orphaned todos, stalled recurrings, stale claims) that is
  **not** the memory kind. Previously the nursery pass wrote these as
  `kind='memory'` rows tagged `internal-thought`, conflating ops
  telemetry with reflective *thought*: it polluted the memory namespace
  (thousands of admin rows) and — because a live spin loop's finding set
  churns every second — the per-minute writer spun on *itself*, emitting
  >2,000 near-duplicate digests a day. Alerts are numeric, **not
  embedded** (body in `title` + `meta`, no `card_combined` chunk, so
  they never reach semantic search), deduped on `meta.fingerprint`, with
  an `alert-state:` open→resolved lifecycle and `alert-source:` /
  `severity:` tags. (`src/precis/alerts.py` producer, `handlers/alert.py`
  read side, `precis-alert-help` skill.)
- **Producer surface `precis.alerts`** — `raise_alert()` upserts on
  `(source, fingerprint)` (a repeat sighting bumps `seen_count`, never
  duplicates); `resolve_stale_alerts()` auto-resolves a condition that
  has cleared; `list_open_alerts()` is the shared read. Generic, so
  future producers (failed passes, sweeper, quota) can adopt it without
  schema changes.
- **Nursery now raises alerts** instead of writing a digest memory. Each
  detector (spin-loop / orphan / stale-claim / long-wait / stuck-doable
  / stalled-recurring) raises one `alert` per finding under
  `nursery:<category>` and auto-resolves cleared ones each pass. The
  TTL-purge / fingerprint-digest / write-throttle machinery is deleted.
  The `structural` / `deep_review` reviewers and the Status "Background
  Health" panel read the live open set instead of the old digest.
  (`workers/nursery.py`, `workers/structural.py`, `workers/deep_review.py`.)
- **`/alerts` web tab** — open alerts grouped by source, severity-sorted,
  with subject links + recurrence counts; `?state=resolved` shows recent
  history. (`precis_web/routes/alerts.py`, `templates/alerts/list.html.j2`,
  nav entry in `base.html.j2`.)

### Fixed (2026-06-19 — exponential chase backoff + nursery self-spin)

- **Finding-chase waiting backoff is now exponential.** The flat
  `WAITING_BACKOFF_MINUTES` (60) window re-poked a never-arriving
  frontier stub once an hour *forever* (24 no-op `waiting` events /
  ref / day). `claim_tracing_findings` now widens the skip window per
  consecutive `waiting` outcome — `base * 2^(waits-1)` capped at
  `WAITING_BACKOFF_MAX_MINUTES` (1440) — so a stuck finding settles to
  ~one poll/day while a chain that makes any progress resets to the
  base window. Mirrors the OA fetcher's exponential retry guard.
  (`workers/chase.py`)
- **Nursery digest writer no longer spins on itself.** When a real
  spin loop is active, its `spin-loop` finding set is the top-50 of a
  *rolling* 24h `ref_events` scan ordered by a count that drifts every
  second, so the `(category, ref_id)` fingerprint changed nearly every
  pass and defeated the dedup — the per-node, per-minute nursery
  emitted >2,000 near-duplicate `tier:nursery` memories in a day
  (several per second). Fixed structurally by moving nursery off the
  memory digest onto the new `alert` kind (deduped per *condition*,
  auto-resolving) — see the "Added — `alert` kind" entry above.
  (`workers/nursery.py`)
- **Note:** the chase/fetcher loops are code fixes; prod must be
  *redeployed* to benefit — the spin-loop spike that surfaced this was
  prod running a pre-backoff-fix checkout long after the merge.

### Added (2026-06-19 — per-release baseline snapshot, dual-track migrations)

- **`migrations/baseline/schema.sql`** — a generated snapshot of the
  schema (the numbered migration chain *compiled* to one file). A fresh
  `precis migrate` loads it in one transaction and applies only any
  migrations added since, instead of replaying the whole chain. Existing
  databases are untouched and migrate forward as before. The numbered
  migrations stay sealed in the tree — this is a dual-track scheme
  (Rails `schema.rb`), **not** a third greenfield. See ADR 0031.
- **`precis db dump-schema`** regenerates the snapshot (container op:
  needs `pg_dump` + a CREATEDB role). `precis migrate --from-scratch`
  ignores the baseline and replays the full chain.
- **`scripts/bump <version>`** ties snapshot regeneration to the version
  bump and keeps `pyproject.toml` + `src/precis/__init__.py` in lockstep
  (fixes a pre-existing drift the `precis-status` skill surfaced:
  package `8.17.0` vs `__version__` `8.1.0`).
- **Guards**: always-on text tests (ledger synth↔parse closure, baseline
  integrity), a DB-backed convergence test (`load baseline + tail` ==
  full replay, schema + ledger), and a CI release gate
  (`assert_baseline_at_head`) that fails a tagged build with a stale
  baseline.

### Added (2026-06-19 — hierarchical SOM cluster maps + `/clusters` grid)

- **`clusterize` worker pass + `precis_web` cluster grid.** A spatial,
  browseable view of the whole corpus: chunk embeddings are clustered
  into a hierarchical Self-Organizing-Map (a *grid* whose adjacent
  tiles are similar — unlike k-means, where tile position is
  meaningless), each tile labelled by a distinctive-keyword word cloud.
  Two independent maps (`scope='paper'` ≈1.1M chunks, `scope='memory'`)
  toggle in the UI; clicking a word lists the papers behind it.
  - Engine (`utils/cluster_map.py`, numpy-only, pure): batch SOM
    (vectorised — minisom's online loop does not survive ~1M vectors),
    adaptive-depth hierarchy (stops subdividing sparse branches),
    top-down `descend_to_leaf` for full-corpus assignment after
    sample-training, and **sibling-scoped c-TF-IDF** word clouds so a
    tile's terms are distinctive vs. its *siblings*, not the whole
    corpus (a curated stoplist drops academic boilerplate first).
  - **Address stability across daily rebuilds** — `build_hierarchy`
    **warm-starts** each grid from the previous run's centroids
    (`prior=…`), so a tile keeps its identity *and* grid position as the
    corpus drifts (the address `4.7.1` stays put). Deliberately *not*
    train-cold-and-Hungarian-relabel: relabeling would preserve identity
    but scramble the adjacent-tiles-are-similar topology the SOM exists
    to provide. `stability_report` (dep-free Hungarian
    `linear_sum_assignment`) measures whether identity held and the
    worker records it in the run `note` (`self_cos` / `identity`).
  - Worker (`workers/clusterize.py`, system profile): time-gated daily
    full rebuild per scope (`PRECIS_CLUSTER_INTERVAL_HOURS`, default
    20h) — trains on a bounded modulo-strided sample, streams the full
    candidate set through batched descent, COPYs assignments, computes
    word clouds in SQL, and prunes superseded runs. No-ops cleanly if
    numpy is absent.
  - Storage: migration `0027_clusterize.sql` — `cluster_runs` /
    `cluster_cells` (centroid + word cloud + grid position) /
    `cluster_assignments` (per-chunk leaf; ancestor membership is a
    `leaf_path` prefix scan).
  - Routes: `GET /clusters` (grid / drill-in / leaf papers),
    `GET /clusters/word` (htmx fragment). New "Clusters" nav tab.
  - numpy promoted to a direct dependency (was transitive).

### Added (2026-06-19 — duplicate reconciliation, Phases 1–2)

- **Shared dedup primitive** `ingest/dedup.merge_duplicate` — folds a
  duplicate paper ref into a survivor: migrates external identifiers +
  graph edges, records a `supersedes` edge + `meta.superseded_by`,
  soft-deletes the duplicate (reversible), audits both via `ref_events`.
  Mirrors `ingest/add._reconcile_orphan_stub`. Survivor rule
  (`pick_survivor`): DOI/arXiv → non-junk title → most authors → lowest
  ref_id (never "lowest id alone" — the deleted `dedupe-papers` bug).
- **Phase 1 — `fix-metadata` auto-dedups.** When a suspect's re-derived
  DOI already belongs to a different live ref, it's merged into that
  canonical (`action="deduped"`) instead of erroring `no_change` —
  retiring the manual SQL used for the 30 dups found on 2026-06-19. New
  `Store.identifier_owner` does the normalised owner lookup.
- **Phase 2 — `precis reconcile-duplicates`.** A re-runnable sweep that
  collapses live paper refs sharing a `pdf_sha256` (same file ingested
  twice) to the best survivor. Dry-run by default; `--apply` commits.
  Not yet auto-wired into nightly `maintenance` — run on demand until
  trusted. See `docs/design/duplicate-paper-handling.md` (Phase 3,
  fuzzy near-dup detection, remains planned).

### Added (2026-06-19 — needs-triage web UI)

- **Triage queue + paste-title→S2 flow** for papers `fix-metadata` tagged
  `needs-triage` (metadata it couldn't recover). New **Triage** nav tab →
  `/papers/triage` lists the queue; a paper's detail page opens a triage
  panel where the operator pastes the real title, the server looks it up
  on Semantic Scholar (`/papers/{id}/triage-lookup`), and re-renders the
  edit form pre-filled from the best match. Saving applies it via the
  existing `edit` verb and **clears the `needs-triage` tag**. The web path
  is DB-only (no NAS-corpus write); the cite_key rename + PDF move stay in
  the `fix-metadata` CLI. (`precis_web/routes/papers.py`,
  `templates/papers/triage.html.j2`, `detail.html.j2`.)

### Fixed (2026-06-19 — paper edit now rewrites search cards)

- **Editing a paper's title/authors/abstract now rewrites its `card_*`
  chunks.** Previously an edit (web or agent `edit` verb) updated the
  `refs` columns but left the derived `card_title`/`card_authors`/
  `card_combined` chunks — what title/author search actually matches —
  showing the stale pre-edit text. `PaperHandler.edit` now rewrites them
  and drops their embeddings/keywords for re-derivation, via the shared
  `ingest/cards.rewrite_cards` (factored out of `ingest/remediate`).

### Planned (2026-06-19 — duplicate handling)

- `docs/design/duplicate-paper-handling.md` — phased plan to fold
  keep-canonical/soft-delete-junk dedup into `fix-metadata` (Phase 1) +
  a periodic reconciliation sweep (Phase 2), with a correct survivor rule
  (DOI → non-junk title → most authors → lowest id), replacing the
  deleted `dedupe-papers`.

### Fixed (2026-06-19 — OA fetcher: lost PDFs, dead Unpaywall, zombie stubs)

- **OA fetcher dropped PDFs where no watcher looked.** The OA fetch
  pass downloaded arXiv/Unpaywall/S2 PDFs successfully but, with
  `PRECIS_WATCH_INBOX` unset, fell back to a host-local
  `~/work/new_papers/_oa_fetched` that the `precis watch` daemon never
  scanned — bytes landed, nothing ingested, and the stub re-qualified
  every pass. The cluster now wires `PRECIS_WATCH_INBOX` to the shared
  NAS inbox (single source of truth with the watch role). Docstring in
  `workers/fetch_oa.run_oa_fetch_pass` now states the inbox MUST equal
  the watcher's dir.
- **OA fetch pass is now env-gated + single-host.** New `PRECIS_OA_FETCH`
  gate (default off, mirrors `PRECIS_GP_FETCH`) so the fetcher runs on
  one node — the watchers race the shared inbox, so one fetcher feeds
  them all. Stops every cluster node re-claiming the same stubs.
- **Exponential fetch backoff.** `claim_stubs_to_fetch` widened the flat
  24h retry window to `base * 2^(attempts-1)` capped at 720h (30d), so a
  stub with no OA copy anywhere settles to ~monthly retries instead of
  daily forever. Never a permanent give-up (a paper can become OA later).
- **Zombie-stub reconciliation at ingest dedup.** When a fetched PDF
  content-dedups against a *different* ref than the stub it was fetched
  for (the two share no identifier — stub has DOI/arXiv from chase, twin
  has only content/pdf hashes), `precis_add` now folds the orphan stub
  (matched by the fetcher's `cite_key` filename) into the survivor:
  migrates external identifiers + graph edges, records a `supersedes`
  edge + `meta.superseded_by`, soft-deletes the stub. `probe_existing`
  now also filters soft-deleted refs so a retired duplicate can't
  resurrect. See `ingest/add._reconcile_orphan_stub`.

### Removed (2026-06-19 — dead `dedupe-papers` command)

- **`precis jobs dedupe-papers` deleted.** It was stale against the v2
  schema (queried `refs.id`/`refs.slug`/`meta->>'doi'` — none of which
  exist in v2; DOIs live in `ref_identifiers`, the PK is `ref_id`), so it
  errored on run, and its "keep lowest `id`, hard-delete the rest" rule
  would have deleted the canonical paper in favour of a junk duplicate.
  Unreferenced by any cron/maintenance/playbook. Duplicate *detection*
  now falls out of `fix-metadata` (a re-derived DOI that already belongs
  to another ref surfaces the dup); a future enhancement can fold the
  "keep canonical, soft-delete junk" action into it.

### Added (2026-06-19 — `wikipedia` kind: on-demand article fetch)

- **New `kind='wikipedia'`** — the on-demand alternative to bulk-
  embedding a Wikipedia dump (which would be ~30M chunks / ~200 GB
  resident HNSW and a permanent precision tax on every search).
  `get(kind='wikipedia', id='<query>')` runs the MediaWiki search API
  (CirrusSearch) to resolve the best article, fetches its plain-text
  extract (`prop=extracts&explaintext` — no wikitext parsing), and
  caches it 7 days. Cache-backed like `web`/`semanticscholar`; the body
  is block-split + embedded so `search(kind='wikipedia', q=...)` lands
  hits inside fetched articles. Free, no API key. Outbound fetch goes
  through `safe_fetch` with a Wikimedia-compliant descriptive
  User-Agent (`PRECIS_WIKIPEDIA_UA` / `PRECIS_WIKIPEDIA_LANG` env).
  Handler: `handlers/wikipedia.py`. Skill: `precis-wikipedia-help`.
- **`ORIGIN` provenance axis + `wikipedia` search fence.** Every
  `wikipedia` ref is auto-stamped with the closed-vocab tag
  `ORIGIN:wikipedia`, and that tag is **fenced out of default and
  cross-kind (`kind='*'`) search** — the same rail as
  `DREAM:speculative`. Tertiary encyclopedic prose never competes with
  the curated paper corpus for top-k slots. The fence lifts on an
  explicit `kind='wikipedia'` scope or a `tags=['ORIGIN:wikipedia']`
  opt-in. New `store/_tag_filter.wiki_fence` / `is_wiki_tag` / `WIKI_TAG`
  applied at the three search builders in `store/_blocks_ops.py`.
- Migration `0026_wikipedia_kind.sql` — seeds the `kinds` + `providers`
  registry rows (data-only; reuses shared `refs`/`chunks`/`cache_state`).

### Fixed (2026-06-19 — junk-metadata ingest + the "()" edit error)

- **Paper metadata-edit error page rendered as a blank `()`.** The
  `/papers/{id}/edit` and `/delete` routes passed the error template the
  wrong context keys (`body`/`is_error` instead of `title`/`detail`/
  `status`), so under Jinja's `ChainableUndefined` every field rendered
  empty — heading `()`, no detail. Now they pass the canonical keys; the
  real handler message is shown. (`precis_web/routes/papers.py`.)
- **Scanned / dvips PDFs ingested with junk metadata** (title
  `"No Job Name"`, author initials like `"DRP"`, or empty title minting
  an `anon…` slug). Root causes, all fixed:
  - `is_garbage_title` now catches authoring-tool default titles
    (`No Job Name`, `Microsoft Word - …`, `untitled`, `PowerPoint
    Presentation`, `Slide N`).
  - New `is_garbage_author` drops tool/account-stamp `/Author` values
    (bare initials, `Microsoft Office User`, `Administrator`, …); wired
    into the embedded-metadata fallback.
  - Built the **text-rescue step** the lookup cascade only referenced in
    comments: when the embedded title is junk and there's no DOI, mine a
    candidate title from first-page body text and re-query S2 —
    accepting the hit **only if `verify_metadata` confirms it** against
    the body (`candidate_title_from_text` + the new step in `lookup()`).
  - Plugged the leak in `extract_metadata_from_sources` that re-pulled
    the raw embedded `/Title` + `/Author` *after* the cascade had
    already filtered them.

### Added (2026-06-19 — `precis fix-metadata` remediation)

- **`precis fix-metadata`** re-derives metadata for local papers already
  ingested with a junk/empty title or no authors (the symptom above).
  Per paper it relocates the on-disk PDF, re-runs the (now-fixed)
  metadata cascade, and either **fixes** it — rewriting
  title/authors/year/`meta.abstract`, renaming the `cite_key` slug +
  moving the PDF to the matching `<corpus>/<letter>/<cite_key>.pdf`
  path, refreshing DOI aliases, and rewriting the
  `card_title`/`card_authors`/`card_combined` chunks (dropping their
  embeddings + keywords so the workers re-derive them) — or tags it
  `needs-triage` for a future manual paste-title→S2 flow. Dry-run by
  default; `--apply` commits; re-runnable and resumable. A full prod
  dry-run informed two refinements: `is_garbage_title` now also catches
  front-matter section titles (`Dedication`, `Index`, `References`,
  `Abstract`, `Editorial Board`, `Issue Information`, …), filename/path
  titles (`.dvi`/`.fm`, `C:/…`, `/home/…`), and pure numeric IDs — so
  book front-matter and filename junk route to `needs-triage` instead of
  being "fixed" with a junk title; and the remediation skips the cite_key
  rename + PDF move when no author was recovered (the slug stays `anon…`
  either way), which also removes the `anon00` CiteKeyOverflow.
  (`ingest/remediate.py`, `cli/fix_metadata.py`.)
### Added (2026-06-19 — generated schema diagram + system manual)

- **`precis schema-doc`** — new CLI subcommand that introspects
  `information_schema` (base tables, columns, PK/FK) and renders a
  Mermaid ER diagram into a Markdown file (default
  `docs/design/schema.md`). Two transports, one pure renderer:
  `--database-url` (psycopg, for the container/cluster) or
  `--from-tsv -` (pre-fetched rows). Because the diagram is *generated
  from the live database* it can't drift the way the hand-drawn
  `schema-v2.puml` did (which still showed the dropped `ref_segments`
  tables and missed `parent_id` / `prio`).
- **`scripts/gen-schema`** — wrapper that regenerates
  `docs/design/schema.md` from prod: it runs the introspection on
  caspar over ssh (where the pgbouncer password lives in `.pgpass`)
  and pipes the rows into `precis schema-doc`.
- **`docs/design/schema.md`** — the generated, current ER diagram
  (33 base tables) checked in so GitHub renders it inline.
- **`docs/architecture.md`** — a thin, link-heavy system manual
  (Markdown, not PDF) tying the seven-verb surface, kinds, storage,
  todo-tree, and workers together.
- **`docs/README.md`** — a documentation landing index (front door to
  the `docs/` tree). Both are linked from the top-level README.

Plan: `docs/design/schema-doc-and-manual.md`.

### Changed (2026-06-19 — user identity de-hardcoded + `asking-reto` removed)

- **`PRECIS_OWNER` config + de-hardcoded "reto".** The human running
  an instance was hard-coded as `"reto"` in the web "ask a follow-up"
  path (`ASKER`), while the live tag data calls the same person
  `user:elmsfeuer` and the web source convention calls them `owner`.
  New `PrecisConfig.owner` / `WebConfig.owner` (env `PRECIS_OWNER`,
  default `owner`) is now the single canonical handle: the follow-up
  question turn is authored by `cfg.owner`, not the literal `"reto"`.
  Reto's instance sets `PRECIS_OWNER=elmsfeuer`. See
  `docs/design/user-identity-and-ask-routing.md`.
- **Removed the deprecated `asking-reto` tag/view alias.** It had been
  a back-compat alias for `ask-user` since the rename; prod carried no
  `asking-reto` rows. `view='asking-reto'` is now an unknown view (use
  `view='ask-user'`), and the dual-match SQL/strip logic across
  `_todo_views`, `todo`, `nursery`, the coordinator/executor claim
  comments, and the web Asks/Tasks routes drops the legacy form.
  `render_asking_reto` → `render_ask_user`. **Breaking** for any
  caller still passing `view='asking-reto'`.

### Migrations

- `0028_normalize_owner_identity_tag.sql` — repoints the one stray
  bare `OPEN/reto` identity tag onto the canonical `OPEN/user:elmsfeuer`
  (merge, not rename — the target may already exist). No-op on DBs
  without the tag.

### Added (2026-06-19 — projects: workspace promoted to first-class)

- **A *project* is a strategic root that owns a `meta.workspace`.** No
  new ref kind — the existing workspace concept is surfaced three ways:
  - **Owner-path `project:<slug>` tagging.** When a todo is created
    carrying `meta.workspace` (set explicitly or inherited via the
    cascade), the handler derives `project:<slug>` from the workspace
    path and stamps it — previously this only happened inside a planner
    tick via `PRECIS_WORKSPACE`. So manually-filed refs join the
    cross-kind project surface (`search(tags=['project:<slug>'])`).
    Forward-only: it stamps the ref being created, not its existing
    subtree. Slug logic factored into
    `utils/workspace.project_tag_for_path` / `Workspace.project_tag`.
  - **Project brief.** New first-class `Workspace.brief` field
    (`meta.workspace.brief`) — standing project guidance ("voice,
    scope, what not to do"). Cascades to every descendant; the planner
    surfaces it as a `## Project context` block in the per-tick
    *variable* layer (`workers/planner_prompt._render_project_brief`),
    so a deep leaf reads the project frame without the owner repeating
    it per child.
  - **`search(kind='todo', view='projects')`** — dashboard of
    workspace-owning roots: slug, path, open-todo count across the
    subtree, on-disk file count, brief first line. Descendants that
    merely inherited the workspace are not listed as separate projects.
- **Todo view dispatch refactor.** The `search` view `if`-chain is now
  a `TodoView` `StrEnum` (closed vocabulary) + a `_TREE_SEARCH_VIEWS`
  dispatch table, with an import-time totality assertion so a
  half-wired view fails at load rather than as a runtime "unknown
  view". Removes the frozenset/branch duplication that could drift.

### Added (2026-06-19 — paperview ingest timeline, relative time, pagination)

- **Ingest timeline on the paper detail page.** `/papers/{id}` now
  shows when the paper was ingested, broken out by stage: `ref`
  (`refs.created_at`), `PDF` (`pdfs.ingested_at`, joined via
  `pdf_sha256`), and `first chunk` (`MIN(chunks.created_at)`). Each
  renders as relative time (`5h ago`) with the absolute UTC timestamp
  on hover; stages that haven't happened yet (stub with no PDF /
  un-chunked) read `—`. New `store.ingest_timestamps(ref_id)`
  (one round-trip, correlated subqueries).
- **Relative timestamps on the Papers-needed backlog.** The
  `last attempt` column renders `…ago` (absolute on hover) instead of
  a raw ISO string. Backed by a new shared `precis_web.timefmt`
  module (`ago` / `abs_ts`, tolerant of datetime *or* ISO string),
  registered as Jinja filters; `routes/status.py`'s `_ago` is
  refactored onto it so relative-time formatting is single-sourced.
- **Pagination on the remaining list views.** `/papers`,
  `/papers-needed`, `/asks`, and the `/tags` leaderboard gain
  offset-based `?page=N` pagination (one-extra-row "has next" probe,
  filter-preserving prev/next links) — matching the already-paged
  `/refs/{kind}` and `/tags/refs`. `store.stub_backlog` gains an
  `offset` kwarg.

### Added (2026-06-19 — ask-a-follow-up on a thought + Papers filters)

- **"Ask a follow-up" on any thought (Refs detail pages).** Each
  Refs detail page (memory / dream / any browsable kind) now carries
  a textbox + button: type a question and the server reasons about
  that thought, storing the answer. The Q&A is captured as a `conv`
  thread (one per source, slug `followup/<kind>/<id>`; chunk-scoped
  `…/c<pos>` when asked on a chunk) and linked back to the source via
  the `link` verb (`derived-from`), so the discussion is reachable
  from the thought it grew out of and accumulates follow-ups.
  Discussions are listed on the source page; conv pages spawned this
  way render a "continue this discussion" box. The thinking reuses
  the dreaming dispatch — `call_claude_agent` with the same
  `PRECIS_DREAM_SOUL_PATH` system prompt + `PRECIS_MCP_CONFIG` tools
  when present — run off the event loop. All DB writes go through the
  `put` / `link` verbs (single-sourced with MCP). New module
  `src/precis_web/ask.py`; routes `POST /refs/{kind}/{id}/ask` and
  `POST /refs/conv/{id}/continue`.
- **Papers list presence filters.** `/papers` gains `has_pdf` /
  `has_chunks` 0/1 toggles (plus a "chunks" badge per row). The
  recent-list path pushes them into `store.list_refs` (new tri-state
  `has_pdf` / `has_chunks` kwargs); the lexical-search path
  post-filters via the new `store.ref_ids_with_chunks` batch helper.

### Fixed (2026-06-19 — background-worker spin loops + UI surfacing)

- **OA fetcher no longer re-polls the same stub every pass.** The
  `claim_stubs_to_fetch` retry-window guard keyed exclusively on a
  recent `fetcher:unpaywall` event. With Unpaywall disabled in prod
  (no `PRECIS_UNPAYWALL_EMAIL`) the cascade only ran arXiv + S2 and
  never wrote an `unpaywall` event, so the window never armed and
  every stub re-qualified on every pass — observed at ~167 S2 polls
  per stub per day (54k+ `no_oa_version` events / 24h over 327 refs).
  The window now matches any `fetcher:%` source, so it arms after
  whichever provider actually ran. (`workers/fetch_oa.py`)
- **Finding-chase no longer spins on chunk-less frontier stubs.** A
  `waiting` outcome (frontier stub has no chunks yet) left
  `STATUS:tracing` unchanged, so `claim_tracing_findings` re-picked
  the same finding every pass on every node — ~1,300 `waiting` events
  per ref per day. The claim now skips a finding whose most-recent
  chase event is a `waiting` newer than `WAITING_BACKOFF_MINUTES`
  (60); any other latest outcome stays eligible, so real progress is
  unthrottled. (`workers/chase.py`)

### Added (2026-06-19 — background health surfacing)

- **Nursery `spin-loop` detector.** New SQL-only check flags any
  `(ref_id, source)` emitting more than `SPIN_LOOP_EVENTS_24H` (200)
  `ref_events` in 24h, so a runaway worker reaches asa-bot's attention
  view through the existing nursery digest instead of only being
  visible by reading `ref_events` by hand. (`workers/nursery.py`)
- **"Background health" panel on the Status page.** Surfaces active
  spin loops + failed worker passes (24h); renders a green "all clear"
  when both are empty. (`precis_web/routes/status.py`,
  `templates/status.html.j2`)

### Fixed (2026-06-18 — coroutine auto-close + job lease)

- **`child_job_succeeded` no longer auto-closes planner coroutines.**
  An `LLM:*`-tagged todo runs the `plan_tick` coroutine: each tick is a
  `kind='job'` that exits `STATUS:succeeded` on any clean run — including
  ticks that only *minted children* (`verdict: continue`) or *yielded*
  (`ask-user:`). A stale / hand-authored / legacy
  `meta.auto_check={'type':'child_job_succeeded'}` on such a todo would
  fire on the first successful tick and close the parent while its
  children were still open (observed: a paper cascade closed mid-Phase-1
  with open gather children). Two guards added in
  `auto_check_evaluators/child_job_succeeded.py`: the evaluator returns
  `None` (leave open) when the parent carries an `LLM:*` tag, or when it
  still has a live (non-`done`/`won't-do`) child todo — the latter
  mirrors the manual `STATUS:done` guardrail the auto-resolver was
  bypassing. The dispatcher (`workers/dispatch.py`) additionally *strips*
  a `child_job_succeeded` spec when minting a self-resolving (`plan_tick`)
  job, so the footgun can't survive on a coroutine parent (declining to
  inject — the prior behaviour — wasn't enough for an already-attached
  spec).
- **Job lease 30 min → 90 min** (`workers/executors/claude_inproc.py`).
  A `plan_tick` may request up to `timeout_s=3600` (60 min), and the
  executor writes summary/result chunks after the subprocess returns, so
  a 30-min lease could expire mid-run and let a second worker re-claim
  and double-run the job. 90 min covers the max tick plus post-processing
  with margin.

### Added (2026-06-18 — compiled-PDF + job audit in the Tasks UI)

- **Per-todo compiled-PDF viewer.** `GET /tasks/{id}/pdf` streams a
  todo's compiled workspace PDF inline (paper-viewer style), resolving
  `<PRECIS_ROOT>/<workspace.path>/<entrypoint-stem>.pdf` — distinct from
  the corpus-PDF path the papers viewer uses. A 📄 attention icon renders
  on a task row whenever that PDF exists on disk. Adds `precis_root` to
  `WebConfig` (from `PRECIS_ROOT`). See `precis_web/routes/tasks.py`
  `_resolve_workspace_pdf`.
- **`job_result` surfaced in the Tasks UI.** The structured per-tick
  audit chunk (parsed verdict + subtasks/citations/findings counts) now
  rides alongside `job_event` / `job_summary` in the job hover tooltip
  and the per-todo history panel.

### Added (2026-06-16 — paper-writing cascade hardening)

- **Stuck-job sweeper.** New worker pass `precis worker --only sweeper`
  (also part of the default `system` profile rotation). Finds
  `kind='job'` refs whose `STATUS:running` is older than
  `PRECIS_STUCK_JOB_HOURS` (default 1.0h) and transitions them to
  `STATUS:failed` with an `swept:claim-orphaned` open tag. The parent
  todo's `child-failed:<job_id>` bubble fires automatically so the
  cascade unblocks. Recovers from deploy-time orphans (the
  pre-deploy worker dies mid-claim, the post-deploy worker ignores
  already-running rows). See `src/precis/workers/sweeper.py`.
- **`\citequote` macro in workspace tex skeleton.** Every cite in body
  text must be `\citequote{key}{verbatim}` — the macro renders
  `\cite{key}` plus, in review mode, a footnote with the verbatim
  quote. `\showquotesfalse` hides the footnote in publish mode; the
  .tex source carries the audit trail unchanged. See
  `src/precis/data/workspace_templates/tex/main.tex`. The verbatim is
  the same `source_quote` persisted on `kind='citation'`
  (`precis-citation-help`).
- **DOI extract skill** (`precis-doi-extract-help`). Planner-facing
  guidance for reading sibling perplexity-research output + corpus
  search results, identifying cited papers, and calling
  `paper.acquire(identifier=…)` to mint stubs that the existing
  `fetch_oa` worker enriches.
- **Strict identifier validation on `paper.acquire()`.** Default
  `verify=True` rejects identifiers Semantic Scholar can't resolve,
  so hallucinated DOIs never land on the "Papers we need" backlog.
  `verify=False` opts out for known-real preprints S2 hasn't indexed
  yet; passing both an unresolved identifier *and* a `title=` hint
  mints with `acquire:unverified` open tag so the operator can
  re-check.
- **Review-pass skills.** Three new one-pass-per-skill review
  disciplines:
  - `precis-review-citation-faithfulness` — for each `\citequote`,
    fetch the cited paper's chunks and confirm the verbatim text
    appears. Mints findings on paraphrased / fabricated / wrong-paper
    cites.
  - `precis-review-paragraph-flow` — topic sentence + body +
    transition check per paragraph.
  - `precis-review-section-structure` — intro frames contribution,
    section list matches intro roadmap, conclusion follows from body.
- **"Papers Needed" web tab.** Lists chunkless paper stubs (the
  fetch_oa backlog), with optional `?awaiting=1` filter for the
  fetcher's next-pass queue. DOI / arXiv identifiers are clickable
  for one-click verification. Mirrors `precis stubs` CLI data shape.

### Changed (2026-06-15 — kind rename sweep)

- **`kind='fc'` → `kind='flashcard'`.** Numeric-ref flashcards kind
  renamed for clarity. Chunk_kinds renamed in lockstep:
  `fc_claim` → `flashcard_claim`, `fc_evidence` → `flashcard_evidence`.
- **`kind='think'` → `kind='perplexity-reasoning'`.** The Sonar
  Reasoning Pro cache-backed kind is now namespaced under its provider
  so the surface advertises what the tier is, not just what it costs.
- **`kind='research'` → `kind='perplexity-research'`.** Same story
  for Sonar Deep Research. The `precis-research-help` skill (corpus-
  grounded research methodology — unrelated to the Perplexity tier
  but ambiguously named) moves to `precis-perplexity-research-help`.
- Forward migration: `0018_kind_renames_fc_think_research.sql`
  rewrites `refs.kind`, the `chunk_kinds` slug catalog, and any
  meta.chunk_kind pointers. Hard cutover, no alias period — the MCP
  surface isn't a stable API (agents discover capabilities from the
  skill index at boot). Skill index headmatter carries `renamed-from`
  / `renamed-kinds` pointers so an agent with a stale skill cache that
  searches for the old name finds a "renamed to X" trail.

### Added (2026-06-15 session)

- **Layer-3 compile guard at STATUS:done** (`utils/compile_guard.py`).
  When the strategic root attempts `STATUS:done` and all child todos
  are resolved, the workspace's `latexmk -pdf -interaction=nonstopmode
  -halt-on-error <entrypoint>` runs. Pass → done sticks. Fail → done
  is rejected via `BadInput` carrying the last 30 lines of the build
  log so the LLM's next tick prompt has the actual error to fix.
  Time-capped at `PRECIS_LATEXMK_TIMEOUT_S` (default 120s). Degrades
  gracefully when `latexmk` isn't installed (logs + skips). Wired
  into `TodoHandler.tag` after the artifact guardrail.
- **Bib generator** (`utils/bib_gen.py`). `write_workspace_bib`
  enumerates `kind='citation'` refs tagged with `project:<slug>`,
  joins to the linked paper (`rel='cites'`), and emits a `refs.bib`.
  Rich-metadata sources render as `@article`, stubs as `@misc` with
  an explanatory note. Idempotent regen. Called by the compile guard.
- **`chunk_kind='job_result'` audit chunk per tick** (T1.6).
  `claude_inproc._build_job_result_text` writes a structured per-tick
  summary (ts / job / parent / model / duration / counts of
  subtasks-citations-findings minted / verdict). Parent re-tick
  prompts now consume these instead of dumping raw stdout.
- **Re-tick prompt rewrite** (`planner_prompt._build_user_prompt`).
  New "Workspace status" block lists files currently on disk (with
  get-by-slug hints); new "Children" block per-child shows
  `id / kind / STATUS / one-job_result-digest`. Dropped the giant
  "Prior child results" raw-stdout dump. Token cost per re-tick
  drops from ~30k to ~3k for a 6-child decomposition.
- **`STATUS:done` requires-artifact guardrail** (T1.7).
  Worker-sourced `STATUS:done` rejected unless: successful child
  job, all live child todos resolved, citation tagged with project,
  or a file written under the workspace in last 24h. Owner passes
  through. Stops the cheating mode.
- **Backdoor env injections** for runtime context:
  `PRECIS_CURRENT_TODO`, `PRECIS_CURRENT_MODEL`, `PRECIS_WORKSPACE`
  → `project:<slug>` tag. LLM stops needing to remember its own
  runtime context every call.
- **Slug-only API mode default**: `put(kind='tex', name='X', ...)`
  defaults `mode='create'`. Removes one footgun.
- **MCP tool-call audit log** (`tools/core._log_tool_call`). One
  structured line per put/get/search/tag/link/edit/delete with
  parent_todo correlation. `precis logs --process precis-serve`
  now shows the per-tick MCP trail.
- **Workspace init defensive**: `ensure_initialized` splits git
  init into stages so a `git add -A` failure on NFS doesn't leave
  `.git/` orphan-without-HEAD. Per-put commits retry independently.
- **Literature-hunt contract pattern**: planner contract gains a
  copy-paste template for the lit-hunt subtask (mandatory when the
  LLM identifies missing sources), prohibits "References needed"
  memory notes.
- **`mactex-no-gui` install** in `precis_worker_agent` role + plist
  `PATH` extended to include `/Library/TeX/texbin` so `latexmk` /
  `biber` are reachable to the agent worker's MCP subprocesses.
- **End-to-end validation**: 12 KB high-quality LaTeX section
  (`tex/manufacturing.tex`) produced by the cascade; compiled to a
  149 KB / 4-page `main.pdf` via the workspace's templated
  `main.tex` + `latexmk`.

### Added

- **Papers hover card — DOI / arXiv verification links + sharper
  abstract.** The papers-list popover (and the detail header) now
  surface clickable `doi.org` / `arxiv.org` links so a paper can be
  verified at a glance, fetched in one batched
  `Store.identifiers_for_refs` query for the whole page. The
  abstract-backfill heuristic (`abstract_previews`) now prefers an
  explicit "Abstract" chunk — matched by `section_path` or a leading
  label, which is stripped — before falling back to the first
  substantial leading paragraph.

- **Conversations tab renders a readable transcript.** Clicking a
  `conv` in the Conversations tab previously dumped the handler's
  agent-facing overview card (the `Next: {if you want to execute this
  call}` affordances meant for an LLM) into a `<pre>`. The web detail
  view now reads the turn chunks directly and renders a chat-style
  transcript — per-turn author (with a stable colour dot), timestamp,
  anchor, and body — with a turn count in the header. Other ref kinds
  are unchanged.

- **Multiple corpus roots for PDF serving.** `PRECIS_CORPUS_DIR` now
  accepts an `os.pathsep`-separated list of roots (e.g.
  `/opt/shared/corpus:/opt/nas/botshome/papers/corpus`); the web tries
  each `<root>/<letter>/<cite_key>.pdf` in order and serves the first
  that exists. This fixes the cluster reality where the same NFS share
  is mounted at different paths per host — one web config now finds the
  PDF wherever it's mounted instead of 404-ing. The "file not found"
  diagnostics list every path tried; the Status tab lists all roots.
  Single-path configs are unchanged.

- **Web Status tab — system telemetry (Claude spend, host liveness,
  core temps).** Three new panels answer "what is the cluster doing
  right now":
  - **Claude usage** rolled up from `ref_events.cost_usd` — 24h / 7d
    spend + call counts and a 7d per-model breakdown. No new data:
    every agentic `claude_agent` call already logs `agent:done` with
    cost + model.
  - **Machines** — per-host CPU temperature and load from a new
    `host_heartbeat` table (migration 0017), with a green/grey
    liveness dot and "reported Nm ago"; temps colour amber ≥70 °C and
    red ≥85 °C. A second strip shows log-derived liveness
    (`worker_logs` last-seen + 24h WARNING/ERROR counts).
  - Reporter: new `precis heartbeat` one-shot CLI each host runs on a
    timer. Load via `os.getloadavg()`; temperature best-effort
    (`PRECIS_TEMP_CMD` env → Linux `/sys/class/thermal` → none), so a
    macOS box without a sensor command still reports load + liveness.
    No new dependency. See ADR 0028 and
    `docs/design/system-status-telemetry.md`.
  - The Status header now shows the running `precis-mcp` version (for
    stale-server detection).
  - Fixed alongside: the Status "Recent activity" panel was silently
    empty because its query read a non-existent `created_at` column on
    `ref_events` (the table stamps `ts`) and the error was swallowed by
    the panel's defensive wrapper. Now reads `ts` and renders.

- **Workspace abstraction — project-scoped layout, slug-only API,
  per-put git commits.** The planner-coroutine cascade now produces
  durable on-disk artifacts (LaTeX papers, markdown writeups) under
  a structured project workspace, instead of leaving content stranded
  in `job_summary` chunks. New module `utils/workspace.py` defines
  `Workspace(path, format, entrypoint, style)` stored as
  `meta.workspace` on the strategic root and inherited at `put` time
  by every descendant. The planner runner sets `PRECIS_WORKSPACE` on
  the `claude -p` subprocess; file-kind handlers consume it.

  - **Slug-only API**: `put(kind='tex', name='intro', text='...')`
    routes to `<workspace>/tex/intro.tex` via the layout convention
    (`utils/workspace_layout.py`). LLM never sees physical paths.
    Classic `id=<path>` form still works as the escape hatch.
  - **Lazy workspace init**: first `put` in a fresh workspace runs
    `git init`, copies `.gitignore` + `main.tex` skeleton + empty
    `refs.bib` from templates under
    `data/workspace_templates/<format>/`.
  - **Per-put git commits**: every successful file write commits
    under a PG advisory lock (keyed on workspace path) so two
    concurrent puts in the same project serialize cleanly. Commit
    SHA returned in the response.
  - **Layer-1 mechanical fixes** (`utils/tex_mechanical_fix.py`):
    deterministic unicode escapes + missing `\\usepackage{}`
    detection applied silently inside `put(kind='tex', ...)`. No
    LLM in the loop for syntactically obvious corrections.
  - **Planner contract update**: extends the four output shapes
    (mint subtasks / yield / halt / done) with two new ones
    (write artefact / mint citation). Workspace is ambient; LLM
    expresses intent (file name + content), not paths. Paper-not-
    in-corpus recovery (mint `kind='finding'` + prose marker, never
    `\\cite{TODO}`) documented.


- **`edit(kind='todo', mode='replace', text=...)` — in-place text
  rewrite for todos.** Todos were create-only (no way to fix wording
  without delete + re-`put`, which severs every inbound edge and the
  tree position). `TodoHandler` now exposes `edit` (mirrors
  `MemoryHandler.edit`): same id; parent, links, and tags survive; the
  old body lands in `ref_events` as `body_replaced` (`view='log'`).
  Owner-only on strategic / tactical nodes (same authority veto as
  delete / reparent). Backed by the existing `Store.replace_ref_text`.
  Surfaced in precis-web: the task-tree `⋯` panel gains a *Save text*
  field (`POST /tasks/{id}/edit`). Skill: `precis-todo-help`.

- **Planner-coroutine slice — default-on auto-run for LLM:*-tagged todos.**
  Every open todo carrying a closed-vocab `LLM:opus|sonnet|haiku` tag
  is now dispatched automatically: the worker mints a `plan_tick` job,
  shells out to `claude -p` with the chosen model, captures stdout as
  a `job_summary` chunk, and re-ticks the parent on the next sweep
  once children resolve. The pattern is recursive — opus reads body
  + accumulated child summaries, mints subtasks (each carrying its
  own `LLM:*` tag), and yields. Children inherit a budget; parents
  re-fire as children complete; chain bottoms out when the planner
  tags itself `STATUS:done` or yields via `ask-user:*` / `halt:*`.
  See `src/precis/data/skills/precis-tasks-help.md` and the new
  depth-discipline skills (`precis-decomposition-help`,
  `precis-research-help`, `precis-write-paper-help`,
  `precis-review-paper-help`).

  - `LLM:` registered as a closed-vocab axis on `todo` in
    `precis.store.types._CLOSED_VOCAB` (values: opus / sonnet /
    haiku). Typos reject at write time.
  - `executor:` is the parallel namespace for code-path runners
    (`fetch`, `ingest`, etc.) — open tag, lowercase, allowlist via
    `_todo_guards._EXECUTOR_TAG_VALUES`. v1 ships no registered
    runners; the namespace is reserved.
  - Dispatch query rewrite in `workers/dispatch.py`:
    * candidate = `LLM:*` tag present OR `executor:*` tag present
      OR legacy `meta.executor` set;
    * coroutine idempotency — skip when any live (open) child todo
      or live (queued/running) child job exists.
  - New job_type `plan_tick` (`workers/job_types/plan_tick.py`)
    invoked by `claude_inproc`. Reads `meta.params.model`, shells
    `claude -p --model …`, captures stdout as `job_summary` chunk.
  - Prompt builder `workers/planner_prompt.py` returns
    `(system, user)` with strict cache layering: system carries
    the pinned `precis-tasks-help` skill, the skill index built
    from each skill's new `summary:` front-matter field, and the
    planner contract (the four output shapes + depth discipline).
    User carries TOON-formatted ancestry chain (`{id, title, from}`),
    the todo's body, and accumulated `job_summary` chunks from
    completed children.
  - Three guardrails (`workers/planner_guardrails.py`):
    * per-todo tick cap (`PRECIS_MAX_TICKS`, default 10) → auto
      `halt:tick-cap`.
    * per-todo cost cap (`PRECIS_MAX_TODO_USD`, default $2) → auto
      `halt:cost-cap`.
    * global daily ceiling (`PRECIS_DAILY_COST_CEILING`, default
      $20/day) → round-wide dispatch pause until rolling window
      clears.

- **`halt` namespace — `halt:<reason>` for system self-halts.**
  Extension of the previous bare `halt` mechanism. Workers may now
  self-halt with a reason (`halt:cost-cap`, `halt:tick-cap`,
  `halt:planner-stuck`); the attention view renders the reason
  inline. Guard `check_halt_remove` rejects removal of any
  `halt` / `halt:*` value from worker sources.

- **`ask-user` tag — generalises `asking-reto` to any user.**
  New canonical form: bare `ask-user` (any human will do),
  `ask-user:<handle>` (specific person), or `ask-user:<question>`
  (the freeform question itself for inline rendering). The legacy
  `asking-reto` / `asking-reto:` form continues to match in the
  doable-exclusion registry and SQL queries — existing production
  tags don't need migration. The attention view renders the section
  as "Ask user (N)".

- **Depersonalisation sweep.** Removed personal-name references
  (`Reto` / `reto`) from active runtime code, skill docs, and tests.
  Owner identity now passes via the generic `web:owner` source
  prefix; the guard still recognises `web:*` as owner authority.
  Sealed history (`docs/decisions/`, `docs/design/` historical plans,
  `docs/user-facing/` specs) unchanged.

- **Four depth-discipline skills.** New skills under
  `data/skills/`: `precis-decomposition-help` (split vs do, sibling
  sizing, depth-at-leaves principle), `precis-research-help`
  (corpus-grounded research with primary-source rule),
  `precis-write-paper-help` (claim-level citation density),
  `precis-review-paper-help` (adversarial review). Each carries a
  laundry-list quality bar so opus children produce depth rather
  than Perplexity-grade summaries.

- **`summary:` front-matter on every skill.** New field on the
  `SkillFrontmatter` dataclass. Hand-written one-liner per skill
  describing what topic / discipline the file covers — aggregated
  at server boot into the planner's skill-index block (cached in
  the system prompt). Full skill content stays on-demand via
  `get(kind='skill', id=…)`.

- **`halt` tag — explicit "robot stay away" marker on todos.** Workers
  may add it (escalation: "I think this needs human eyes"), only the
  owner may remove it (the resume edge). The doable view and the
  dispatch worker both honour it via a new shared
  `_DOABLE_EXCLUSION_TAGS` registry in `handlers/_todo_views.py` —
  one place to add future "robot stay away" reasons, no SQL drift
  between the two surfaces. Halted leaves surface under
  `view='attention'` so they don't vanish from the rotation while
  hidden from doable. New guard
  `check_halt_remove` rejects removal from worker sources;
  `TodoHandler.tag` wires it in alongside the existing level-tag
  guard. Skill: `precis-tasks-help` (tag vocabulary table).

- **precis-web: per-kind ref browsers, filters/sort, and task tags.**
  New `/refs/{kind}` surface (one generic route module +
  `refs/index.html.j2` / `refs/detail.html.j2`) with a top-nav tab per
  browsable kind: `memory, conv, oracle, gripe, patent, pres`. Lists
  read off the DB — ranked `search_refs_lexical` when a query is
  present, else `list_refs` with a date-window preset
  (`any/24h/7d/30d/90d` → `updated_after`), a tag filter, whitelisted
  sort, and offset pagination. Detail is read-only: it renders the
  handler's own `get` output through the in-process runtime (slug
  kinds addressed by slug, numeric kinds by id). `Store.list_refs`
  gains an `order_by` parameter resolved against a class-level
  whitelist (`_LIST_ORDER_BY`); unknown keys fall back to
  `updated_desc` instead of erroring (no caller string ever reaches
  the SQL). The Tasks tree gains tag editing —
  `POST /tasks/{id}/tags` dispatches the `tag` verb and the dashboard
  shows removable chips (excluding `STATUS:`/`level:`) plus an add
  input. Each task's `...` panel also gains a lazy (htmx) **History**
  fragment (`GET /tasks/{id}/history`) listing its job attempts
  (one row per `kind='job'` run, with STATUS) and its `ref_events`
  log (e.g. `status:done`). (Task *text* editing is intentionally not included: numeric
  refs are create-only and a title edit needs its own verb + card
  re-synthesis — see `docs/design/precis-web-refs-and-filters.md`.)

- **Reparent todos via `link(kind='todo', rel='parent')`.** Moving an
  existing todo in the tree was the one mutation without an MCP
  surface (ADR 0026 deferred it). `TodoHandler.link()` now intercepts
  the reserved `parent` relation and re-points `refs.parent_id`
  through the same cycle / depth / owner guards as create-time
  parenting, plus a new subtree-aware `check_reparent_depth`
  (accounts for the moved subtree's height, not just the parent
  depth). `mode='remove'` detaches the todo to a root; an optional
  `target=` on remove must name the current parent. The todo
  `view='links'` synthesizes a `## parent` section so the edge
  round-trips. To agents, `parent` reads as an ordinary todo relation;
  it is not added to the closed `Relation` vocabulary (it re-points a
  column, not a `links` row — see ADR 0027). Web: `POST
  /tasks/{id}/move` dispatches the same call, and the Tasks dashboard
  gains native drag-to-reparent (drop on a task to nest, on the top
  bar to promote to a root) plus a numeric "move under #__" fallback.
  The Tasks tree also surfaces both processing signals per node — a
  live `pg_locks` row lock (via new `Store.locked_ref_ids()`, a
  `FOR UPDATE SKIP LOCKED` probe) and the `STATUS:running` +
  `meta.lease_until` worker lease — with child `kind='job'` rows shown
  under their parent todo. Tests: `tests/test_todo_tree.py` (move,
  detach, cycle/depth/owner rejects, links round-trip),
  `tests/precis_web/test_routes.py` (move route dispatch). Skills
  `precis-todo-help`, `precis-link-help`, `precis-relations` updated.

- **Centralised worker logs via `worker_logs` + `precis logs` CLI.**
  Migration 0015 adds `worker_logs (log_id, ts, host, process,
  pass, level, logger, message, payload JSONB)` with three partial
  indexes — `(host, ts DESC)` for the canonical "what is this box
  doing", `(pass, ts DESC) WHERE pass IS NOT NULL` for per-pass
  filtering, `(level, ts DESC) WHERE level IN ('WARNING','ERROR')`
  for error grep without bloating the btree.
  `utils/db_log_handler.py` adds `BufferedDBLogHandler`, a
  Python `logging.Handler` that buffers records in-process (flush
  every 5s OR 50 records — both env-overridable via
  `PRECIS_LOG_MAX_INTERVAL_SECONDS` / `PRECIS_LOG_MAX_BUFFER`),
  uses a dedicated psycopg connection in autocommit mode so
  logging doesn't fight with the worker's main pool, demotes to
  the stdlib file handler on flush failure (so a DB outage drops
  to the existing `/var/log/precis-*.log` channel automatically),
  registers `atexit` for the tail-flush, and has a 10× hard cap
  on buffer size to bound memory during sustained DB outages.
  Records carry `host` (from `PRECIS_HOST_NAME` env, falls back to
  `socket.gethostname()`), `process` (from `PRECIS_PROCESS` env),
  and `pass` (derived from logger name `precis.workers.<X>`).
  `cli/worker.py` attaches the handler right after `Store.connect()`
  succeeds; failures to attach are non-fatal (the file handler
  keeps catching everything regardless). New `precis logs` CLI
  reads the table with `--since` (durations like `1h`/`7d` or ISO
  timestamps), `--host`, `--process`, `--pass`, `--level`,
  `--limit`, `--tail` (shortcut for `--since=24h --limit=50`),
  `--payload` (include the JSONB column), `--format` (toon /
  table / json — for jq pipelines). Tests:
  `tests/test_db_log_handler.py` (12 — unit tests with mocked
  psycopg + one DB integration test that round-trips a row through
  the live table). The text file at `/var/log/precis-*.log` stays
  in place as the bootstrap + fallback channel — operators tail
  it when the DB is itself the problem.

### Changed

- **precis-web: self-diagnosing PDF path.** When a held paper's file
  isn't found, the `/papers/{id}/pdf` 404 and the detail page now name
  the resolved path and `corpus_dir` and point at `PRECIS_CORPUS_DIR`;
  the detail page distinguishes a stub (queued for fetch) from a
  held-but-missing file. The Status tab shows the active corpus root.
  (Root cause of the operator's "No PDF on disk": `PRECIS_CORPUS_DIR`
  unset on the web host, so the default `~/work/corpus` was searched
  instead of the NFS mount.)

- **LLM-facing skill catalogue refreshed for Slices 3/4/5 +
  consolidation.** New `precis-dispatch-help` documents the
  `meta.executor` → dispatch worker → `kind='job'` bridge
  end-to-end, including the failure-bubble + retry decision tree. `precis-job-help` rewritten for the parent_id requirement
  + dispatch pattern + `child-failed:<job_id>` bubble.
  `precis-fix-gripe-help` shows the todo-first canonical pattern.
  `precis-tasks-help` documents the `child-failed:*` tag, the
  `⚙` job marker in `view='tree'`, and the doable view's
  exclusion rules. `precis-auto-tasks-help` adds the
  `child_job_succeeded` evaluator entry + a worked Pattern 4.
  `precis-overview` kinds table updated; both READMEs (CLAUDE.md
  + README.md) carry "what just landed" entries pointing readers
  at the toolpath index. No behavior change.

### Added

- **`view='attention'` + child-failed parents excluded from doable.**
  Union of `asking-reto` leaves + `child-failed:<job_id>`-tagged
  parents in one digest, with each child-failed entry quoting the
  most recent `job_event` chunk (truncated) as the failure reason.
  The chatter preamble can render this next to the doable queue so
  asa sees "what needs me" in one block. Companion change: the
  doable view's exclusion list now skips parents carrying any
  `child-failed:*` tag, so stuck parents drop out of the rotation
  until the bubble flag is cleared. 5 new tests in
  `tests/test_todo_views.py`.

- **Load-aware gate on heavy passes.** `utils/load_gate.py`'s
  `skip_if_high_load(name)` reads `os.getloadavg()[0]` and returns
  True when the 1-min load avg exceeds `PRECIS_LOAD_CEILING`
  (defaults to `os.cpu_count() * 1.5`, env-override-able). Applied
  in `workers/review.py` (structural + deep_review) and
  `workers/dream_agent.py` so the agentic passes self-throttle
  when the host is busy. SQL-only passes (dispatch, schedule,
  nursery, auto_check) skip the gate — they're idempotent and
  short enough that even a busy box drains them. Tests:
  `tests/test_load_gate.py` (7).

- **Worker profile flag — Slice-5 consolidation.** `precis worker
  --profile=system|agent` selects which rotation runs.
  `system` (default) covers the cheap SQL + chunk-level passes
  (embed, summarize, chunk_keywords, chase, fetch, tag_embeddings,
  auto_check, schedule, nursery, dispatch, job_claude_inproc);
  `agent` covers the heavy LLM reviewers (structural, deep_review).
  `--only X` still works and overrides the profile for ad-hoc
  backfills. `dream_agent` stays out of the profile because it has
  its own 15-min cadence via `dream-pass.sh` (which uses
  `--only dream_agent` explicit override). Cluster: new
  `precis_worker_agent` role + playbook `37-precis-worker-agent.yml`
  deploys the agent profile worker on melchior as hermes (OAuth
  for claude `-p`); existing `precis_worker` plist gains
  `--profile system`. Retired in the same commit: `precis_schedule`,
  `precis_nursery`, `precis_structural`, `precis_deep_review`
  roles + their playbooks (33-36) — none had reached production.

- **Slice 5 — jobs become children of todos.** The todo tree (intent)
  and the job substrate (execution) are unified via `parent_id` on
  `refs`: a `kind='job'` ref now requires a `parent_id` pointing at
  a live `kind='todo'`. New `dispatch` worker (`workers/dispatch.py`)
  walks open todos with `meta.executor` set, mints a child
  `kind='job'` under each, leaves the existing executor pool
  (`job_claude_inproc`) to actually run the work. Multi-host safe
  via `SELECT … FOR UPDATE OF r SKIP LOCKED` per parent. Dispatcher
  auto-injects `meta.auto_check={'type':'child_job_succeeded'}` when
  the writer didn't set one so the parent todo resolves to
  `STATUS:done` when the child job succeeds. New auto_check
  evaluator `child_job_succeeded` returns True when any non-deleted
  child of the leaf is a `kind='job'` ref with `STATUS:succeeded`.
  Failure-bubble: when a child job hits `STATUS:failed` (via the
  executor's `_record_failure` or `JobHandler.tag(['STATUS:failed'])`),
  the parent todo gets a `child-failed:<job_id>` open tag so the
  parent shows up in the nursery digest's stuck-leaf detectors. No
  auto-retry — the parent's owner (asa or human) decides next.
  `view='tree'` now walks `kind IN ('todo', 'job')` so child jobs
  surface in the subtree render with a `⚙` gear marker; `view='doable'`
  still excludes jobs (they're execution detail, not actions). CLI
  `--only dispatch` runs the spawner alone; default rotation includes
  it alongside `auto_check` + `schedule` + `nursery`. Evaluator
  protocol now passes `ref_id` as a kwarg so tree-scoped evaluators
  can look up children of the calling leaf; existing evaluators
  accept `**_kw`. Tests: `tests/test_dispatch_worker.py` (15),
  `tests/test_auto_check.py` grew by 5 (`child_job_succeeded`),
  `tests/test_todo_views.py` grew by 1 (`view='tree'` shows jobs).

- **Reviewer driver refactor — `Reviewer` config + `run_review_pass`
  driver.** `workers/structural.py` and `workers/deep_review.py` had
  ~80% duplicate plumbing (gate / dedup / prompt-format /
  digest-write / mcp-resolution). All of it now lives once in
  `workers/review.py` keyed off a frozen `Reviewer(name, tier_tag,
  gate_env, meta_prefix, model, max_turns, timeout_s,
  min_interval_hours, context_builder, prompt_template)` dataclass.
  Structural and deep_review collapse into thin shims that hold only
  the reviewer-specific context-builder SQL + prompt template +
  Reviewer instance + back-compat helper layer for existing tests.
  Adding a new reviewer is now ~150 LOC instead of ~300. The unified
  driver also reads `PRECIS_<NAME>_MODEL` env automatically.

- **Slice 3 structural + deep review tiers, on a unified
  `claude_agent` dispatch.** Two new opus-class reviewers:
  `workers/structural.py` (6h cadence, semantic review of branch
  outcomes / sibling contradictions / depth-fanout / drift) writes
  `tier:structural` digests; `workers/deep_review.py` (weekly
  Sunday-night Allen-review of archive candidates / pruning / 1/N
  rebalancing / long waits) writes `tier:deep` digests. Both gated
  by env (`PRECIS_STRUCTURAL_REVIEW=1`, `PRECIS_DEEP_REVIEW=1`) so
  cost can be muted without uninstalling; both dedup against the
  most recent digest of their tier (5h / 144h windows) so
  double-fires no-op. Multi-host safe via the shared-DB dedup
  window. CLI: `--only structural` / `--only deep_review`, both
  explicit-only (never in the default rotation). Ansible roles
  `precis_structural` (cluster repo) + `precis_deep_review` deploy
  the LaunchDaemons on melchior. Skills land in a follow-up.
  Tests: `tests/test_structural.py` (23), `tests/test_deep_review.py`
  (13).

- **Unified `claude -p` agentic dispatch — `utils/claude_agent.py`.**
  Peer to `utils/claude_p.py` (which stays as the one-shot JSON
  judge surface used by the chase verifier). `call_claude_agent`
  carries the agentic-shape flags
  (`--mcp-config`/`--strict-mcp-config`, `--append-system-prompt`,
  `--max-turns`, `--permission-mode`, `--output-format`, optional
  `--bare`, `--disallowed-tools`) and adds cost cap, wall-clock
  timeout, structured `log_event=(store, ref_id, source)` hook
  that appends an `agent:done` event with model + cost + duration
  to `ref_events`. Env defaults
  (`PRECIS_CLAUDE_AGENT_MODEL`/`_MAX_USD`/`_TIMEOUT_S`) so
  reviewers don't redeclare each. The structural + deep + dream
  passes all share this dispatch surface. Stub-binary tests via
  `PRECIS_CLAUDE_BIN` (same trick `claude_p` uses). Tests:
  `tests/test_claude_agent.py` (19).

- **Dream worker on `claude_agent` — `workers/dream_agent.py`.**
  Reads the directive prompt from `PRECIS_DREAM_PROMPT_PATH`,
  asa's SOUL from `PRECIS_DREAM_SOUL_PATH`, MCP config from
  `PRECIS_MCP_CONFIG`, dispatches via the unified helper with the
  same flag set the legacy `dream-pass.sh` had (no
  WebFetch/WebSearch, bypassPermissions, 20 turns). Gate:
  `PRECIS_DREAM_AGENT=1`. The cluster's existing
  `roles/precis_dream/files/dream-pass.sh` becomes a thin
  wrapper that just exports the env and execs
  `precis worker --only dream_agent --once` — the role now also
  installs `dream-prompt.md` to `/opt/asa/files/`. Tests:
  `tests/test_dream_agent.py` (5).

### Fixed

- **Brittle `int(body.rsplit("=", 1)[1])` parses across
  `test_memory.py` / `test_untags_on_put.py` /
  `test_mcp_critic_regressions.py`.** The TOON `Next:` trailer
  rendered by `_create_ack_next_hints` ends in
  `delete(kind='memory', id=N)`; the trailing `)` made the
  rsplit parse `N)` instead of `N`. Added a shared
  `tests.conftest.id_of(body)` helper (parses `id=N` after the
  first leading clause and strips trailing `,.()`) and ported
  ~28 sites to use it. Same helper now drives the Slice-1+
  test suites that already had inline equivalents.

- **Slice 3 nursery tier of `docs/design/todo-tree-plan.md`.**
  SQL-only pattern matcher that walks the todo tree for local
  incoherence (orphans without a `level:strategic` ancestor, stale
  claims older than 3 h, waits older than 7 d, doable leaves stuck
  >24 h, recurrings whose last spawned child has been open >1 h)
  and writes a markdown digest as a `kind='memory'` ref tagged
  `tree-review:YYYY-MM-DD` + `tier:nursery` + `user:asa` +
  `internal-thought`. asa-bot's preamble surfaces recent
  `internal-thought` rows already, so digests reach chatter
  without a dedicated channel. Fingerprint-dedup on
  `meta.nursery_fingerprint` — repeat passes with the same
  findings skip the write; empty findings never write. The Slice-3
  plan called for a sonnet call here, but the detectors are
  deterministic rules ("orphan", "stale", "stuck") that don't need
  reasoning, so the worker is pure SQL and shares the default
  `precis worker` rotation with `auto_check` + `schedule`. Run
  alone with `precis worker --only nursery`. Cluster: hourly via
  the new `precis_nursery` Ansible role on melchior. Skill:
  `precis-nursery-help`. Tests: `tests/test_nursery.py` (28).
  Multi-host safe via the fingerprint dedup (duplicate concurrent
  writes catch up on the next pass).

- **Multi-host schedule worker — `SELECT … FOR UPDATE SKIP LOCKED`
  claim on the recurring's `refs` row.** The Slice-4 schedule
  worker's spawn loop now holds a per-row exclusive lock from claim
  through mint + ref_event commit. Two workers (same host or
  different hosts) racing on the same recurring serialise on the
  refs row's tx lock; the loser walks past via `SKIP LOCKED`. Crash
  safety from the connection's tx lifetime — Postgres releases the
  lock on session close, no heartbeat / TTL reaper. New
  `precis_schedule` Ansible role (cluster repo) deploys the
  per-minute LaunchDaemon; supports deployment on every asa host.
  Test: `test_schedule_pass_row_lock_serialises_concurrent_workers`
  opens a held lock in one tx and confirms the parallel pass
  returns `claimed=0`.

- **Slice 4 of `docs/design/todo-tree-plan.md` — recurring schedule
  + PRIO column.** Scheduled work (dreams, weather, conference
  watches, birthday reminders) lives in the same tree as everything
  else: a `level:recurring` root carries the schedule + spawn rule,
  each tick mints a fresh `level:subtask` child. `0014_refs_prio.sql`
  adds a `prio SMALLINT` column (1..10, CHECK + partial index) so
  the doable ORDER BY sorts on `COALESCE(r.prio, 5)` instead of
  joining through `ref_tags`/`tags` for a closed-prefix `PRIO:`
  vocabulary; the rotation becomes `prio ASC, picks_7d ASC,
  ref_id ASC` so PRIO 1 (chat) and PRIO 2 (cron) preempt the
  strategic 1/N share. The `PRIO:urgent|high|normal|low` tag stays
  as a write-time alias that translates to a column write at the
  handler boundary. A seeded `Watches` umbrella (`meta.builtin=
  'watches-root'`, undeletable via `check_not_builtin`) is the
  default parent for `level:recurring` refs created without an
  explicit `parent_id`; `level:recurring` joins
  `level:strategic|tactical` on the owner-only authority gradient
  so workers can't mint a `* * * * *` cron. `meta.schedule` accepts
  a canonical `cron` field or `every:` shorthand (`Nm` / `Nh` /
  `1d` / `mon HH:MM`); the handler validates at write time and
  rewrites the block to the canonical cron form, so the runtime
  sees one shape. Idempotency is `meta.spawned_for_tick=
  'YYYY-MM-DDTHH:MM'` on the spawned child; collision policy is
  skip-when-previous-still-open so a stalled queue doesn't pile up.
  `backfill_missed` (default false) controls catch-up: weather drops
  missed ticks, birthdays don't. `precis worker --only schedule`
  runs the spawner; default rotation includes it alongside
  `auto_check`. `view='roots'` grows a second `## Watches` panel
  (orthogonal to picks-7d, so a noisy cron can't crowd a strategic
  out). Skill: `precis-recurring-help`. Tests:
  `tests/test_schedule.py`.

- **Inner-life skill — actionable future-facing items.**
  `precis-inner-life-help` gains a "Write future-facing items so
  they're actionable" section distinguishing the *scannable* axis
  (first-line discipline) from the *actionable* axis (trigger /
  action / why + anchor) for anything that resurfaces later
  (`internal-thought`, `interest:*`, `changed-mind:*`, `todo`,
  promoted dreams). Mirrors the matching guidance added to asa's
  SOUL (`grimoire/agents/asa.md`).
- **Web surface — `precis web` (ADR 0026,
  `docs/design/precis-web-build.md`).** New optional `precis-mcp[web]`
  extra ships a FastAPI service (`precis_web`, sibling package) with
  four server-rendered tabs over the Tailscale LAN (no auth in cut 1):
  **Tasks** (the hierarchical todo tree — reads off the DB, writes
  through the in-process runtime so the level-gradient guard / depth
  check / STATUS vocab stay single-sourced), **Papers** (corpus search
  + in-browser PDF viewer streamed from `corpus_dir`), **Console**
  (interactive seven-verb precis-query reusing `runtime.dispatch`),
  and **Status** (refs-by-kind, paper held/stub counts, todo status
  breakdown, recent `ref_events`). New `PrecisConfig.corpus_dir`
  (`PRECIS_CORPUS_DIR`, default `~/work/corpus`) names the PDF root
  laid out by `precis watch`. Authority reuses the existing
  `PRECIS_SOURCE` env (the web process runs as `web:reto` = owner);
  no new identity mechanism. Launch with `precis web --host … --port
  …`; binds loopback, reached over Tailscale. Stack: FastAPI + Jinja +
  HTMX/Alpine/Tailwind (CDN), single uvicorn worker. Tests under
  `tests/precis_web/` run without Postgres via a fake runtime/store.
- **Auto-check worker — Slice 1b of `docs/design/todo-tree-plan.md`.**
  A todo leaf can carry `meta.auto_check = {'type': '<evaluator>',
  ...}` so a SQL-checkable condition releases it without an LLM
  pass. `precis worker --only auto_check` (default-on in the
  rotation) polls open leaves with non-null `meta.auto_check`,
  dispatches the registered evaluator, and flips `STATUS:done` (+
  `auto-resolved` ref_event) when the verdict is true. Optional
  `timeout_at` flips a stuck leaf to `STATUS:auto-timeout` so the
  nursery sweep can triage it. v1 evaluator catalogue:
  `paper_ingested` (DOI / arXiv / S2 / PubMed → live paper +
  embedded chunk), `discord_reply_received` (memory tagged
  `replied-to:<msg_id>`), `time_past` (ISO timestamp), `tag_present`
  (any tag, optionally narrowed by kind). The handler validates
  `meta.auto_check.type` at write boundary so typos surface
  immediately. New skill: `precis-auto-tasks-help` with two worked
  patterns (paper-wait, discord-ask) and the timeout recipe.
  Implementation: `src/precis/workers/auto_check.py`,
  `src/precis/workers/auto_check_evaluators/`. Wires into
  `src/precis/cli/worker.py` via the same `RefPass` shape as
  chunk_keywords / chase.
- **Hierarchical todo tree — Slice 1 of `docs/design/todo-tree-plan.md`.**
  New `parent_id` column on `refs` wires todos into a tree
  (migration `0013_todo_tree.sql`). `kind='todo'` gains a tree-aware
  view family — `search(view=...)` accepts `roots`, `strategic`,
  `doable`, `waiting`, `blocked`, `asking-reto`; `get(id=N,
  view='tree')` renders the subtree under a ref. The doable filter
  walks the ancestor chain, skips paused subtrees, drops leaves with
  live `blocked-by` links / `waiting-for:*` / `asking-reto` tags,
  and orders by least-picked strategic in the rolling 7-day window
  (sourced from `ref_events`). Walk-on-read ancestry: every
  `get(kind='todo', id=N)` reply appends the chain from the strategic
  root down to the leaf. Authority gradient via `level:strategic`
  / `level:tactical` tags (owner-only, gated by `PRECIS_SOURCE`:
  `asa-*` is worker, everything else is owner). Hard depth-10 cap
  with the recovery hint baked into the error
  (`waiting-for:` / `blocked-by` instead of splitting). Cycle check
  on re-parent paths (read-only today, ready for the Slice 2 web
  editor). `STATUS:` closed vocab grows `paused` and `auto-timeout`.
  New skill: `precis-tasks-help`. Implementation lives in
  `handlers/_todo_guards.py`, `handlers/_todo_views.py`, and the
  extended `handlers/todo.py`. Auto-tasks (Slice 1b), worker
  integration (Slice 2), and review cadence (Slice 3) are queued.
- **`search(view='stubs')` — the "papers we still need to get"
  backlog over MCP.** Lists `paper` refs with an external identifier
  (DOI / arXiv / S2) but no PDF yet — the queue the chase worker and
  the dream `acquire` tool both feed. Paper-only; `q=` ignored, `n=`
  caps rows. Renders newest-first with a one-line state per stub and
  a `Next:` block. The CLI (`precis stubs`) and the new MCP view now
  render from one shared query (`Store.stub_backlog`). New skill
  `precis-stubs-help` teaches the backlog, the `DREAM:acquire` tag,
  and the `acquire` tool.
- **`precis watch` routes by inbox subtree, ingests slide decks as
  `kind='pres'`.** `inbox/papers/` → paper (current behaviour),
  `inbox/books/` → paper with auto `subtype:book` + `topic:book`,
  `inbox/presentations/` → new pres pipeline: one chunk per slide
  (`chunk_kind='pres_slide'`), `subtype:slides` on creation, slide
  titles derived from per-page first headings. Slide-deck PDFs
  land in `corpus_pres/<letter>/<slug>.pdf` (new sibling of
  `corpus_dir`). New CLI flag: `--corpus-pres-dir` (defaults to
  `<corpus-dir>.parent / corpus_pres`).
- **Path-derived tags via `tagging/` sentinel.** Any path component
  after a `tagging/` segment in the inbox tree becomes a
  `topic:<kebab-slug>` open tag, applied additively on both the
  fresh-ingest and `pdf_sha256`-hit branches so re-dropping a
  known PDF under a new tagging dir merges tags instead of
  silently no-op'ing. `PdfInput` and `PresInput` both grow an
  `extra_tags: tuple[str, ...]` field carrying the same payload.
- **Pres slug-collision policy.** `pdf_sha256` hit is idempotent
  (merge tags only). Slug taken with different bytes suffixes
  `-2`, `-3`, … with a warning log. Diverges from
  `make_cite_key`'s `a/b/c` style on purpose — pres slugs are
  user-typed in directory paths and `lecture-3-2` reads more
  naturally than `lecture-3a`.

No migration needed: `refs.pdf_sha256` is kind-agnostic, the
`pres` kind and `pres_slide` chunk_kind were already seeded in
`0008_pres_kind.sql`, and `probe_existing` queries
`ref_identifiers` without filtering on kind.

## v8.7.6

### Changed

- **Cold-start banner tersified for LLM consumption.** The
  `serverInfo.instructions` text every MCP client sees on connect
  shrank from ~450 chars over 9 lines to ~250 chars over 3-4 lines,
  same information, no prose padding. `_INSTRUCTIONS` collapsed
  from a multi-line "First action on any non-trivial request:..."
  paragraph to a single-line `precis: verbs ...; kind= discriminator.
  Discover: search(kind='skill', q='<goal>') | get(kind='skill',
  id='toc').`. Sandbox preamble collapsed similarly (e.g. `Sandbox
  PRECIS_ROOT (3 markdown): get(kind='markdown'|'plaintext'|'tex').
  tags=['workspace'] scopes.`). `Kinds loaded:` → `Kinds:`.
  Startup-skills warnings dropped their `PRECIS_STARTUP_SKILLS`
  prefix (the source is obvious from the warning glyph). Token
  budget anchor changed from `First action` to `Discover:`; both
  static-banner anchor tests + sandbox preamble tests updated to
  match.

## v8.7.5

### Added

- **`precis-self-consolidation-help` skill.** The dreaming-like
  reverse of accumulation: find clusters of related memories,
  abstract the gist into a new schema-level memory
  (`internal-state` or `internal-thought`), link the contributing
  episodes via `rel='superseded-by'`, tag them `retired`. Frames
  the operation in cognitive-psychology vocabulary (episodic →
  semantic; schema formation; reconsolidation; pruning) since
  consolidation is genuinely the precis surface for what
  cognitive science means by that term. Includes anti-patterns
  (eager abstraction, catalogue-list schemas, retirement without
  linking, schema-for-the-user) and a recovery flow when a pass
  was wrong. Surfaced via `PRECIS_STARTUP_SKILLS` so asa sees it
  on cold-start.

## v8.7.4

### Added

- **`precis-inner-life-help` skill.** Documents the tag protocol on
  `kind='memory'` that the asa-bot preamble uses to render its
  `## Inner life` section: `internal-state` (rolling self-doc),
  `internal-thought` (decaying fragments), `DREAM:speculative`
  (dream worker output), `user:asa` (identity anchor),
  `interest:<topic>` and `changed-mind:<topic>`. Covers capture,
  in-place edit vs. fresh write, retrieval beyond preamble caps,
  reinforcement via re-tag, dream promotion, and bulk decay
  introspection — the mechanics the renderer hints at but doesn't
  spell out. Surfaced via `PRECIS_STARTUP_SKILLS` on melchior so
  asa sees the path on every cold-start.

## v8.7.3

### Fixed

- **Patent ingest crashed with `ForeignKeyViolation` on every fresh
  patent id.** The greenfield `0001_initial.sql` seeds the
  `providers` table, but the `epo_ops` provider row was added to
  the sealed seed *after* the production cluster was migrated —
  ADR 0005's "forward-only migrations" rule means the row never
  reached prod via that file. Patent handler's
  `insert_ref(provider='epo_ops')` then tripped `refs_provider_fkey`
  with no patent ever persisting. Migration
  `0012_epo_ops_provider.sql` idempotently backfills the row;
  ON CONFLICT DO NOTHING so fresh installs (where the greenfield
  seed already covers it) and re-runs both no-op.

## v8.7.2

### Fixed

- **Patent kind reported available even when `python-epo-ops-client`
  was not installed.** The cold-start kind gate only enforced the
  EPO_OPS_CLIENT_KEY / EPO_OPS_CLIENT_SECRET / PRECIS_PATENT_RAW_ROOT
  env trio — the optional `[patent]` extra's Python lib was checked
  lazily on first call, so operators who set the env vars but
  hadn't installed the extras saw a misleading "patent: available"
  banner and only learned the truth when the first
  `precis(kind='patent', ...)` request raised
  `OpsError: python-epo-ops-client is not installed`. Now
  `dispatch.py` does an `importlib.util.find_spec('epo_ops')` probe
  alongside the env gate; missing package surfaces as a proper
  `Loadability(loaded=False, reason="missing python-epo-ops-client;
  install with pip install precis-mcp[patent]")` so the banner
  names the gap honestly and agents can route around it.

## v8.7.1

### Fixed

- **Chunk-based search silently returned empty after the v8.7.0
  Model A migration.** Five row-slice positions in
  `src/precis/store/_blocks_ops.py` (lexical, semantic, fused,
  semantic-region, single-row fetch) were hardcoded as `r[14:37]`
  / `r[37]`. Migration 0011 added two columns
  (`auto_refresh_days`, `refreshed_at`) to `_REFS_COLS_ALIASED`,
  shifting the score from index 37 to 39, but the slicers weren't
  updated. `_row_to_ref` then IndexError'd on every row, the
  search handler swallowed it, and `precis search(kind='paper',
  q='…')` came back empty across the entire library. Memory
  tag-search was unaffected (refs-only path, never touches
  `_blocks_ops`). Symptom in the wild: asa's librarian reported
  "zero indexed coverage on MOFs/DFT/CO₂ capture" — a
  ~2500-paper library claiming nothing on a topic with hundreds
  of hits.
- Slice positions are now derived from named constants
  (`_BLOCK_END`, `_REF_END`, `_SCORE_IDX`) wired to
  `_CHUNKS_COLS_LEN` and the new `_REFS_COLS_LEN` in
  `_mappers.py`. Adding a column to either projection list now
  updates every search method automatically.

## v8.6.2

### Changed

- **Compact conv recent-turn rendering.** `_render_recent` no
  longer emits the full slug header or the conv title; preamble
  callers already know which conv they're in. msg_id renders as
  the last 6 chars (Discord snowflake tail uniquely identifies
  within a conv). Timestamps drop microseconds + timezone. Saves
  ~50 tokens per turn × 5 turns ≈ 250 tokens/turn of preamble
  bloat. Old verbose form still available via
  `get(kind='conv', id='<slug>/transcript')` for human reading.

## v8.6.1

### Fixed

- **Conv handler parser corrupted chat-bridge slugs.** `_parse_conv_id`
  partitioned on the FIRST `/`, so
  `get(kind='conv', id='discord/<g>/<c>/<t>', recent=5)` got read as
  `slug='discord', view='<g>/<c>/<t>'` and NotFound'd. Now only the
  trailing path segment is treated as a view, and only when it
  matches a known view (`transcript`, `full`, `last-meta`). Discord
  / Slack / future-bridge slugs flow through unchanged. asa_bot's
  recent + digest preamble fetches were silently broken until this
  fix — visible in a turn-prompt dump as
  `[error:NotFound] conv slug 'discord' not found`.

## v8.6.0

### Added

- **`kind='cron'` — scheduled wakeups** (migration 0010).
  Numeric-id refs. `put(text='...', target='conv:discord/<g>/<c>/<t>',
  in_='10 minutes' | when='<iso>' | recurring='daily@09:00')`
  schedules a payload; the cron-tick CLI scans every 60s on
  melchior, fires `pg_notify('precis.cron', ...)` for due entries,
  and advances `next_fire_at` per recurrence + catch_up policy.
  asa_bot LISTENs and wakes Asa with the payload as a synthetic
  prompt. `compute_next` caps catch-up at one fire after long
  downtime. Skill: `precis-cron-help`.

- **`kind='message'` — proactive outbound** (migration 0010).
  Numeric-id refs. `put(text='...', target='discord/<g>/<c>/<t>',
  reason='cron:42 fired')` stores the ref AND fires
  `pg_notify('precis.messages', ...)` in the same transaction.
  asa_bot LISTENs and delivers. Every send is searchable history.
  Attachments via `attachments=[{filename, content_type,
  archive_path}]`. Skill: `precis-message-help`.

- **`ref_tags.expires_at` + tag TTL** (migration 0010). Every
  `tag(...)` call accepts `ttl_days=N` or
  `expires_at='<iso>'`. Re-tagging refreshes the TTL via
  `ON CONFLICT DO UPDATE`. Query-time filter excludes expired
  rows from search results and `has_tag` probes; expired rows
  stay in the table for audit. Unlocks the sticky-memory pattern
  used by asa_bot's preamble builder. See updated
  `precis-memory-help`.

- **Conv preamble views.** `get(kind='conv', id='<slug>',
  recent=N)` renders the last N turns verbatim;
  `digest=N + skip_recent=K` renders a keyword-only digest of
  mid-range turns (uses `chunks.keywords`; falls back to a text
  preview when keywords haven't been populated yet).
  `view='last-meta'` returns the most recent block's meta as a
  JSON blob. Designed for asa_bot's 4-tier per-turn prompt
  builder.

- **`precis cron tick` CLI subcommand.** Atomic claim of due
  cron entries with `FOR UPDATE SKIP LOCKED`, NOTIFYs each, and
  advances schedules per policy. Deployed via the
  `precis_cron_tick` ansible role (launchd timer
  `StartInterval=60`).

### Changed

- **Worker priority for `kind='conv'`.** Both the embed and
  chunk_keywords workers' claim queries now sort conv blocks
  ahead of papers. Chat history is hot — asa_bot reads recent
  turns every preamble build, so the digest tier needs keywords
  + the search surface needs embeddings ASAP after each turn
  lands.

## v8.5.0

### Added

- **`pres` kind — slide decks + unpublished writeups (migration
  0008).** New slug-addressed kind for internal artefacts kept
  separate from the academic paper library. One ref per deck or
  writeup; one block per slide (default `chunk_kind='pres_slide'`)
  or per paragraph (override `chunk_kind='paragraph'`) for prose.
  Surface: `get` (overview / `~N` / `/full`), `search` (lexical +
  cross-kind via `search_hits`), `put` (per-block append with
  `pos=` override + same-pos overwrite), `tag`, `link`. Subtype
  carried as a `subtype:slides|writeup|notes` open tag stamped on
  creation. Skill `precis-pres-help` ships the ingest recipes.
  PDF→slide-per-block auto-ingest is a follow-up (will reuse the
  existing marker pipeline + a new_pres drop folder watcher).

- **`conv` capture-on-write (`put`).** ConversationHandler now
  exposes a `put` verb so the chat-bridge can append turns:
  `put(kind='conv', id='<slug>', text=..., author=..., msg_id=...)`.
  First call mints the conv ref (using `title` and `ref_meta` as
  ref-level metadata); subsequent calls append one block per turn
  with the message body. Idempotency on `msg_id` — a Discord
  reconnect-replay does not duplicate turns. Block-level metadata
  carries `author` + `msg_id` + per-turn extras; chunk_kind is set
  to the existing seeded `conv_message` slug so the embed +
  chunk_keywords workers index chat history through the same
  cross-kind search surface as papers/memory. Skill
  (`precis-conv-help`) updated with the put-call shape. See
  `cluster/roles/hermes/README.md` for the bridge contract on the
  hermes side.

- **Dreaming capability — foundation (in progress; agent loop deferred).**
  A background "dreaming" pass that consolidates memories and surfaces
  missing papers, built on additive/guarded writes through the normal
  verbs (see `docs/design/dreaming.md`):
  - **Migration `0007_dreaming.sql`** — salience columns on `chunks`
    (`last_seen`, `last_dreamt`, `accesses`; metadata-only), the
    `bump_salience(ids)` set-based function, supersede relations, and
    the `dream_log` / transcript tables.
  - **Deterministic salience** — `score = last_seen - last_dreamt`
    (no decay, no sampling). Search hits bump salience across paper /
    memory / cache handlers; a `as_dream_actor` contextvar suppresses
    the bump on the dreamer's own reads.
  - **`supersede`** (guarded memory-merge tool) — hard-capped
    2..10 live memories, compress-only survivor text, soft-delete +
    link migration, survivor tagged on the closed `DREAM` axis.
  - **`acquire`** (gated dream tool) — idempotently mints a stub
    `paper` ref by identifier-collapse (`doi:`/`arxiv:`/`s2:` or bare
    DOI/arXiv), best-effort S2 enrichment, `DREAM:acquire` tag, and a
    provenance link; the existing `fetch_oa` pipeline takes over.
  - **`search` angle spray** — `angle=` (cosine in `[-1,1]`) +/-
    `like='kind:id'` returns `n` diverse, mutually-distinct items at
    that cosine from a seed (a cone sample, card-inclusive).
  - **`search(view='dreamable')`** — the focus region: the most-due
    salience seed + its nearest cosine neighbourhood; surfacing stamps
    `last_dreamt` so the region rotates out. No clustering dependency
    (plain ANN ring; sub-theming intentionally cut).
  - **`DREAM:speculative` fence** — speculative dream outputs are
    hidden from default search across all block-search paths; opt-in
    via the tag or an explicit flag.
  - **Dream agent loop (`precis worker --only dream`, ADR 0024)** — the
    in-process agentic janitor. Drives a local model (default the
    `qwen-heavy` litellm alias) over the OpenAI `/v1/chat/completions`
    wire with `tools=`, dispatching each tool-call back through the
    in-process runtime/handlers (no subprocess, no MCP socket). Builds
    the focus region + sparks, runs the turn loop under `as_dream_actor`
    suppression, stamps `last_dreamt` (the rotation), and records one
    `dream_log` row + `dream_transcripts` trace. Tool surface:
    `search`/`get`/`put`/`link`/`tag` (via dispatch) plus the gated
    handler tools `supersede` and `acquire` (the dream loop is their
    surface; not global MCP verbs). Gated off by default
    (`PRECIS_DREAM_LLM`; `PRECIS_DREAM_ACQUIRE` for `acquire`); never in
    the default worker pass set. Stdlib `urllib` transport seam
    (mirroring `RemoteEmbedder`) — no new dependency, fully
    offline-testable (`tests/test_dream.py`). Knobs:
    `PRECIS_DREAM_LLM_URL` (default `http://127.0.0.1:4000/v1`),
    `PRECIS_DREAM_MODEL`, `PRECIS_DREAM_MAX_TURNS`, `PRECIS_DREAM_TIMEOUT`,
    `PRECIS_DREAM_REGION_N`, `PRECIS_DREAM_SPARKS_N`.
- **Embedder-as-a-service + image split (ADR 0020 / 0021).** The
  embedder can now run as a standalone HTTP service so torch-free
  `serve` / `worker` processes embed remotely instead of each loading
  bge-m3 in-process:
  - `precis serve-embeddings` — stdlib HTTP service wrapping
    `BgeM3Embedder` (`/healthz`, `/readyz`, `/model`, `/embed`,
    `/metrics`; bounded-semaphore backpressure → `429` + `Retry-After`).
  - `RemoteEmbedder` — an `Embedder` HTTP client with ordered
    endpoint fallback, exponential-backoff retries, and a `/model`
    boundary check that refuses a dim/version mismatch loudly.
    Selected via `PRECIS_EMBEDDER=remote` + `PRECIS_EMBEDDER_URL`
    (`PRECIS_EMBEDDER_TIMEOUT`, `PRECIS_EMBEDDER_MAX_RETRIES`).
  - `precis.embedder_wire` — the request/response schema shared by
    client and service so they cannot drift.
  - **`precis worker` now threads the remote embedder.** Added
    `--embedder-url` / `--embedder-timeout` / `--embedder-max-retries`
    (env-defaulted to `PRECIS_EMBEDDER_URL` / `…_TIMEOUT` /
    `…_MAX_RETRIES`) and routed every embedder construction through one
    `_resolve_embedder` helper, so `precis worker --embedder remote`
    actually reaches a `serve-embeddings` service instead of raising
    "remote requires a URL". The helper passes the corpus embedding
    dimension as the boundary `expected_dim`. Required for the
    all-local fleet topology where each node's worker embeds via its
    loopback embedder.
  - **Dockerfile split** into role-scoped targets: `serve` / `worker`
    (torch-free, no models), `ingest` (marker + models), and
    `embedder` (sentence-transformers + bge-m3 cache). `bake-models.py`
    gained `PRECIS_BAKE_ONLY=all|marker|embed`.
  - **`scripts/build-all`** — builds all four images via
    `docker build --target`, threading git/build metadata and the
    `premodels` model-cache seed.
- **`precis-status` reports build / runtime / DB facts.** The
  synthesised `precis-status` skill (previously only an optional-
  dependency probe) now prepends three sections:
  **Build** (`precis.__version__` + 9 env-sourced fields:
  `PRECIS_GIT_LAST_TAG`, `PRECIS_GIT_SHA`, `…_SHORT`, `…_DIRTY`,
  `…_DESCRIBE`, `…_BRANCH`, `PRECIS_BUILD_TIME`, `…_HOST`,
  `…_USER`); **Runtime** (container hostname, OS platform, python
  version, pid, started-at, uptime); **Database** (parsed
  `dsn_host`/`port` + a single round-trip pulling
  `current_database()`, `current_user`, `version()`, and the last
  applied row from `public._migrations`). DB failures render
  `unreachable: <type>: <msg>` inline so the surface stays usable
  when the DB itself is the thing wrong. Env fields default to
  `"unknown"` when no build-args were passed — see
  `scripts/build-image` for the host-side capture.
- **`precis-status-help` skill.** Companion file-backed skill that
  makes the synthesised `precis-status` discoverable through
  `search(kind='skill', q=…)`. The synth's body is rendered live
  (uptime, pid, …) and never indexed; the help file is an alias-
  group ramp — every section bundles 6–8 natural-language H2
  phrasings of the same intent ("what version am I", "what release",
  "git sha", "what database", "migration version", …) sharing one
  body, so each phrasing gets its own embedding chunk and matches
  the obvious query. Listed in the `Orientation` category of the
  skill index alongside `precis-status` itself.
- **`scripts/build-image`.** Host-side wrapper that collects git
  facts (`git rev-parse`, `git describe --tags --abbrev=0`, dirty
  flag) plus `date`/`hostname`/`$USER` and runs
  `docker compose -f $PRECIS_COMPOSE build` with the right
  `--build-arg` flags. The matching `ARG`/`ENV` block sits late in
  the `runtime` and `dev` stages of `docker/Dockerfile` so changed
  values only invalidate a single cheap layer.
- **`gripe` promoted to first-class bug tracker.** The write-only
  capture box was useful but ended at filing; gripe is now the
  project's discoverable bug tracker. `get(kind='gripe', id=N)`
  reads the body + comment timeline, `search(kind='gripe', q=...)`
  finds matches across body and comments, `tag` / `link` / `delete`
  work normally. Body and comments are stored as chunks
  (`chunk_kind='gripe_body'` and new `gripe_comment`) so they pick
  up embeddings and keyword extraction from the standard workers
  automatically. Default lifecycle tag is `STATUS:open`; the
  documented progression is `open → triaged → ready_for_fix →
  in_review` plus `wontfix` as a final kept state, with `delete`
  as the absolute terminator. Comments are append-only: a second
  `put(kind='gripe', id=N, text='...')` adds a `gripe_comment`
  chunk rather than mutating.
- **`job` kind: substrate for offline LLM-driven work.** New
  numeric ref kind for "things that run offline and report back".
  Each job carries `meta.job_type` (the dispatcher key) and
  `meta.executor` (the runner-class key); status is a `STATUS:`
  tag (`queued → running → succeeded|failed|cancelled`); the
  comment timeline is `chunk_kind='job_event'` (forensics, hidden
  from default search) plus a final `chunk_kind='job_summary'`
  (human-readable, searchable). v1 ships one job_type
  (`fix_gripe`) and one executor (`claude_inproc`). Future
  consumers (notably `kind='sortie'` for "next step to execute")
  reuse the same substrate.
- **`fix_gripe` job type.** Submit
  `put(kind='job', job_type='fix_gripe', link='gripe:42',
  rel='fixes')` and an in-container worker clones the repo to
  `$PRECIS_FIX_WORK_DIR/clones/gripe_42`, runs `claude -p
  --dangerously-skip-permissions` on a `gripe_42` branch with
  the gripe body + comment timeline as the prompt, and pushes
  the branch to `origin` (the source repo) for human review.
  Submit auto-tags the gripe `STATUS:ready_for_fix`; success
  posts a `gripe_comment` with the SHA and fetch instructions
  and tags the gripe `STATUS:in_review`; failure rolls the gripe
  back to `STATUS:open` and retains the clone dir for forensics.
  Pre-push hook in every clone rejects pushes to anything not
  matching `gripe_*` so the agent can't touch `main`.
  Multi-repo is wired via a `repo:<name>` tag on the gripe + a
  `PRECIS_FIX_REPOS` JSON allowlist; un-tagged gripes use the
  `PRECIS_FIX_REPO_DIR` single-repo fallback; an unknown
  `repo:` tag is rejected at the put call rather than queueing a
  zombie job. New env vars: `PRECIS_FIX_REPO_DIR` (single-repo
  fallback), `PRECIS_FIX_REPOS` (multi-repo allowlist),
  `PRECIS_FIX_WORK_DIR`, `PRECIS_FIX_CLAUDE_BIN`,
  `PRECIS_FIX_CLAUDE_MODEL`, `PRECIS_FIX_TIMEOUT_SECONDS`,
  `PRECIS_FIX_CLONE_TTL_DAYS`. Compose-side: the precis container
  needs `~/.claude`, every host path in the allowlist (or the
  fallback), and `$PRECIS_FIX_WORK_DIR` bind-mounted; the image
  must include the `claude` binary. See
  `docs/design/fix-gripe-deployment.md`.

### Deprecated

- **`precis gripes` CLI.** Prints a deprecation notice on
  invocation pointing at the MCP `get` / `search` surface; will
  be removed in a follow-up release.

### Removed

- **`quest` ref kind retired.** Quest was envisioned as an
  inter-agent task queue, but the consumer side never landed —
  nothing claimed or processed quest rows. The "papers needing a
  PDF" workflow it was originally pitched for is covered by the
  stubs pipeline (`chase` worker → `fetch_oa` → `precis stubs`).
  The `patent_watch` runner's quest-summary mode is gone too:
  every watch now ingests directly (the former `--auto-get`),
  with overflow past `--max-per-pass` dropped and resurfaced on
  the next pass. Forward migration `0004_drop_quest_kind.sql`
  deletes any existing quest refs (cascading through chunks /
  events / tags / links), drops the `quest` row from `kinds`,
  drops the `quest_body` chunk-kind registry row, and removes
  `patent_watches.auto_get`. The `--auto-get` CLI flag and the
  `precis-quest-help` skill are deleted; `kind='quest'` calls
  now return an unknown-kind dispatch error.

### Changed

- **`search(kind='skill')`: hide unwired skills, add `more` column,
  surface an escalation line.** Unwired skills (those whose subject
  kind isn't loaded in this build, or whose frontmatter says
  `status: planned`/`aspirational`) no longer appear as result rows —
  an LLM with no cross-session memory gains nothing from reading
  recipes it can't invoke. They're surfaced instead in a single
  "Also matched in unwired skills: …" footer line so the agent
  retains the redirect signal ("spin up a build with kind X
  wired"). Over-fetches 5×`page_size` semantic hits so dropping
  unwired skills still leaves a full table of actionable matches.
  New `more` column (`+N` / `.`) counts additional matching H2
  sections per skill — same triage signal as the paper-mode `more`
  design in `backlog-search-unique-per-paper.md`.
  `src/precis/handlers/skill.py`, tests in `tests/test_skill.py`.

- **`view='toc'` (papers): uniform `(handle, keywords)` schema, plus
  Topics / Next hints.** The short-range fallback that emitted a
  per-chunk text "preview" column was dropped — the renderer now
  always emits the same two-column schema regardless of range size,
  so the agent contract no longer shifts under it. For ≥75% pervasive
  keywords the renderer prepends a lossless `Topics:` line (keywords
  *also* stay on each row, so promotion never hides which cluster a
  keyword came from). Any cluster large enough to re-bucket on its
  own (≥ `_BUCKETING_THRESHOLD = 30` chunks) gets a `Next:`
  drill-in hint that names the recursive `view='toc'` call. The
  bucket-count formula was bumped (`5·log10(N) → 7·log10(N)`) and
  `_MIN_CLUSTER_SIZE` lowered from 3 → 2 so requested clusters
  survive collapse — bose16 (N=148) now renders ~15 clusters instead
  of the 4 that survived after aggressive singleton collapse.
  `src/precis/utils/toc_db.py`, tests in `tests/test_toc_db.py`.

### Fixed

- **Watcher: drop spurious error reports for transient / race
  conditions.** A backfill audit of `tmp_errors/` (~560 .error.txt
  files across 560 timestamped buckets) showed that the watcher was
  treating every exception out of `precis_add` as a real failure,
  including conditions the next pass would self-heal:
  - **Multi-host inbox race** — another host moved the PDF between
    our `_wait_stable` success and the read inside `precis_add`
    (336 cases, surfacing as `FileNotFoundError: PDF not found:
    /…/inbox/<name>.pdf`). `process_pdf` now distinguishes "source
    PDF vanished" from a genuine missing-file bug and skips the
    error-bucket move; only a `FileNotFoundError` with the source
    PDF still on disk goes through the normal failure path.
  - **Transient DB outages** — `psycopg.OperationalError` from a
    server restart / network blip dropped 14 PDFs into
    `errors/<ts>/` requiring manual recovery. Now caught and left
    in the inbox for the next backfill pass to retry.

  `src/precis/cli/watch.py`, new tests in `tests/test_watch.py`.

- **Ingest: U+FFFD survival is no longer fatal.** `_repair_mojibake`
  (formerly `_repair_or_fail_mojibake`) keeps its em-dash repair pass
  (`LETTER ␣ FFFD ␣ LETTER → LETTER ␣ — ␣ LETTER`) but no longer
  raises on the remaining cases. U+FFFD is itself the canonical
  Unicode "byte sequence I could not decode" sentinel; leaving it in
  the chunk text is more honest than guessing a replacement, and the
  fail-fast policy was costing ~210 real papers per backfill to
  publisher PDFs with bad ToUnicode maps that the operator could do
  nothing about. Search (BGE-M3, PG-FTS) handles FFFD cleanly; in
  rendered output the diamond-`?` glyph is an unmistakable "this is
  not original content" tripwire. `src/precis/ingest/pipeline.py`,
  tests in `tests/ingest/test_pipeline.py`.

- **PDF metadata patch is now genuinely best-effort.** Two corrupt-
  PDF cases were aborting the entire ingest at the metadata-write
  step: `ValueError("is no PDF")` from `doc.set_metadata` (strict
  trailer validation fails after `fitz.open` succeeded) and
  `FzErrorFormat: code=7: object is not a stream` from
  `doc.get_xml_metadata` (malformed XMP packet). Widened the try
  block around all metadata read/write/save calls in
  `patch_pdf_metadata` so any pymupdf failure returns
  `PatchOutcome(skipped_reason="error")` and ingest of the extracted
  body continues. `src/precis/ingest/pdf_writer.py`, tests in
  `tests/ingest/test_pdf_writer.py`.

- **Strip NUL bytes from bibliographic metadata before DB insert.**
  Postgres TEXT rejects `\x00` with `psycopg.DataError`; one paper's
  ingest aborted at `INSERT INTO refs` because the embedded info /
  XMP cascade pulled a NUL byte into `title`. `_clean_text` already
  strips control chars from the *body* path, but the metadata
  cascade has its own extraction surface; added `_strip_nul_bytes`
  at the tail of `extract_metadata_from_sources` to scrub every
  text field (title, authors, doi, journal, publisher, abstract,
  keywords). NUL never carries meaning in a citation.
  `src/precis/ingest/pdf_metadata.py`, tests in
  `tests/ingest/test_pdf_metadata.py`.

- **`scripts/precis-shell --rebuild` now reuses the baked model
  cache.** Previously the wrapper called `docker build` without the
  `--build-context premodels=docker-image://precis-mcp:premodels`
  arg that the Dockerfile's Stage 2 (`models`) is designed around.
  With the seed absent, the `FROM scratch AS premodels` fallback
  kicked in and `bake-models.py` ran against an empty
  `/opt/precis/models`, re-downloading the Marker datalab models
  (~1.5 GB) and bge-m3 (~2.3 GB) on every rebuild whose
  `pyproject.toml` change invalidated the deps layer. Now the
  script always passes `--build-context premodels=...` (using
  `precis-mcp:premodels`, falling back to retagging
  `precis-mcp:latest` / `:dev` if no seed exists yet) and exposes
  a `--rebuild-base` flag for the rare case where the model layer
  itself needs refresh (e.g. a marker-pdf or bge-m3 pin bump). Same
  premodels mechanism documented in
  `docs/design/bake-models-into-image.md` and used by
  `infrastructure/compose.yaml`; just plumbed through the
  standalone wrapper.

- **Dockerfile restructure: dev tooling no longer reruns on source
  edits.** Previously `dev` extended `runtime`, which
  `COPY --from=builder /opt/venv` — and builder's venv changes on
  every source edit because the snapshot install of precis-mcp
  pins to the new tree. Net effect: a one-character change under
  `src/precis/**` invalidated the runtime layer, which invalidated
  every `dev` step, which re-ran ~170 s of `apt install`
  (postgresql-client, graphviz, plantuml, jre), `nodejs` setup +
  `npm install @anthropic-ai/claude-code`, and `uv pip install`
  for pytest / ruff / mypy / ipython — even though the RUN
  commands were byte-identical to the prior build. Three new
  intermediate stages give `dev` a parallel ancestry that bypasses
  the source-dependent venv:

  - `system-base` — shared minimal apt stack (libpq5, tini,
    procps, exiftool) + precis user + entrypoint script. Sibling
    parent for `runtime` and `dev-system` so the apt layer is
    computed once.
  - `dev-system` — `system-base` + dev apt list + node +
    claude-code + uv. Source-independent; cached unless the dev
    apt list / `NODE_MAJOR` / `CLAUDE_CODE_VERSION` change.
  - `dev-venv` — `deps`' runtime venv with dev Python tools
    (pytest, ruff, mypy, …) layered on top. Cached unless
    pyproject.toml / uv.lock / the dev pip list change.

  `dev` now `FROM dev-system`, then `COPY --from=dev-venv
  /opt/venv` + `COPY --from=models /opt/precis/models` + source
  COPY + editable install. Source-only rebuilds skip apt / node /
  dev-pip entirely; only the source COPY, `uv pip install -e
  /app`, and `chown -R /opt/venv` rerun. `runtime` is unchanged
  in observable behavior — just reparented onto `system-base`.

## v8.4.1 — pick_candidate + retraction cascade + CI hygiene (2026-06-05)

### Added

- **`edit(kind='finding', id=N, pick_candidate='<cite_key|ref_id>')`** —
  manual disambiguation for the `STATUS:multi_candidate` chase
  outcome. When the chase reaches a chunk citing multiple
  references (``[12,13]``) and can't pick automatically, it writes
  one `derived-from` link per candidate with `meta.candidate=true`
  and tags the finding `multi_candidate`. The new verb promotes
  one of those candidates (clears the `candidate` marker), drops
  the losing siblings, replaces the chain's frontier entry with
  the picked target, and flips `STATUS` back to `tracing` so the
  chase advances on the next pass. Accepts cite_key (slug) or
  numeric ref_id for `pick_candidate`; `id=` accepts ref_id or
  pub_id. 9 scenarios under
  `tests/test_finding.py::TestPickCandidate`. Closes design-doc
  open question #2 in `docs/design/finding-chase.md`.

- **Retraction → finding propagation.** `Store.set_retraction_status`
  now cascades into findings whose `meta.chain` cites the
  retracted ref: `STATUS` re-grades to `tracing`,
  `meta.retraction_caveats` appends a record with the offending
  ref_id + cite_key + reason, `human_verified_at` is cleared (a
  prior review can't cover a chain that's since shifted), and a
  `ref_events` row (`source='retraction_propagation'`) lands so
  `view='log'` shows the regrade. Idempotent on repeat
  confirmations; opt-out via `propagate_to_findings=False` for
  bulk backfills. 6 scenarios under
  `tests/test_finding.py::TestRetractionPropagation`. Closes
  design-doc open question #3.

### Fixed

- **Mypy stub for `add_tag` on `RefsMixin`.** The retraction
  cascade above calls `TagsMixin.add_tag` across the mixin
  boundary; mypy 2.1 couldn't see through it and raised
  `"RefsMixin" has no attribute "add_tag"`. Followed the existing
  `_validate_slug_for_kind` pattern documented in the module
  docstring: declare a `NotImplementedError`-bodied stub of
  `add_tag` on `RefsMixin` so mypy sees the signature in
  isolation while MRO resolves to `TagsMixin.add_tag` at runtime.
- **`docker/bake-models.py` retries the bge-m3 fetch on HF 429 /
  `LocalEntryNotFoundError`.** The v8.3.1 `publish-image` job
  failed at the bge-m3 stage when the `premodels` build-context
  cache wasn't populated and the cold fetch hit HF's rate limit.
  Wrapped `snapshot_download` in an exponential-backoff loop
  (5s / 15s / 45s / 120s / 300s, ~8 min total) so a transient
  429 window doesn't fail the release. Non-transient 4xx still
  fails fast.
- **`docker/bake-models.py::_patch_surya_config` works on surya
  0.17.x.** The hard import of `SuryaOCRConfig` from
  `surya.recognition.model.config` worked on surya 0.13 but the
  class was removed in 0.17 (broad upgrade in v8.3.0). Rewrote
  the patch as a best-effort sweep over both module layouts so
  the bake survives further surya renames.
- **`.pre-commit-config.yaml` pins ruff to v0.15.16** to match
  `uv.lock`. The hook was at v0.11.6 while CI's `uv run ruff
  format --check` was at 0.15.16, so the 0.15+ formatter rules
  silently passed pre-commit and failed CI. Header comment
  documents the `grep -A1 '^name = "ruff"' uv.lock` recipe for
  future bumps.
- **Windows `test_ingest_failure_str_format`** no longer fails on
  the POSIX-vs-`\\tmp\\x.md` separator mismatch — the assertion
  now compares against `str(path)`.

## v8.3.1 — migration runner: pg_dump compatibility (2026-06-05)

### Fixed

- **Migration runner now applies pg_dump-format files.** The
  v8.3.0 second-greenfield `0001_initial.sql` is a verbatim
  `pg_dump` of the cluster master, which mixes real SQL with two
  psql-only artefacts that psycopg's simple-query `cur.execute()`
  rejects: `\restrict` / `\unrestrict` PG-18+ dump markers
  (parser error) and `COPY ... FROM stdin;` data blocks
  (terminated by `\.`, requires the explicit `cur.copy()` API).
  `Migrator.apply_all` now routes each migration through a
  `_execute_dump_sql` preprocessor: psql `\restrict`/`\unrestrict`
  lines are dropped, `COPY ... FROM stdin;` blocks stream their
  tab-separated payload through `cur.copy()`, and everything else
  buffers between blocks into one `cur.execute()` call. Hand-rolled
  migrations (the pre-v8.3 shape) pass through unchanged because
  they contain no backslash-prefixed lines.
- **Migration ledger uses the schema-qualified
  `public._migrations`.** A pg_dump body sets `search_path = ''`
  early so its DDL is fully qualified; the same setting leaks into
  the runner's post-apply `INSERT INTO _migrations`, so the bare
  reference failed to resolve even though the table sat right
  there. Qualifying the ledger reads (`_applied_versions`) and
  writes (`apply_all`) closes the gap regardless of what the
  migration body did to `search_path`.
- **`store.tag_metadata` sample-refs query** referenced
  `ref_identifiers.value`, a v1 column name that v2 renamed to
  `id_value`. Surfaced once migrations could actually re-apply
  cleanly; broke the four `precis-tag` get-metadata paths.
- **Docker `publish-image` bake step no longer fails on surya
  0.17.x.** `_patch_surya_config` in `docker/bake-models.py`
  hard-imported `surya.recognition.model.config.SuryaOCRConfig`,
  which existed in surya 0.13 but was removed in 0.17. v8.2.0
  and v8.3.0 publishes both failed at the `models 4/4` stage with
  `ModuleNotFoundError: No module named 'surya.recognition.model'`.
  Rewrote the patch as a best-effort sweep over both module
  layouts (0.13's `surya.{recognition,foundation,layout,
  table_rec}` paths plus 0.17's `surya.common.surya.config` +
  renamed `SuryaTableRec{,Decoder}Config`). Missing classes are
  silently skipped, so a follow-on surya rename can't re-break
  the bake.

## v8.3.0 — second greenfield + deps catch-up (2026-06-05)

### Changed

- **Dependency catch-up via `uv lock --upgrade`.** Bumps `marker-pdf`
  1.6.1 → 1.10.2 (transitively closes the markdownify retention DoS
  alert by lifting `markdownify` 0.13.1 → 1.2.2, well above the
  0.14.1 fix); pulls `einops` + `openai` as new required deps
  (marker-pdf 1.10.x added LLM-assisted post-processing); and
  forces `opencv-python-headless` 4.13.0.92 → 4.11.0.86 via
  `surya-ocr` 0.17.1's hard pin (upstream constraint, not ours).
  Also: `cryptography` 47 → 48, `mypy` 1.20.2 → 2.1.0, `typer`
  0.23.1 → 0.26.7, `ruff` 0.15.12 → 0.15.16, `torch` 2.11 → 2.12,
  `pydantic` 2.12.5 → 2.13.4, `rpds-py` 0.30.0 → 2026.5.1 (upstream
  CalVer cutover), and ~50 other patch/minor bumps within existing
  constraints. The 6 remaining open `pillow` + `transformers` alerts
  stay open until marker-pdf lifts its `Pillow<11.0.0` and
  `transformers<5.0.0` upstream caps; the pyproject transitive-dep
  security-floor note remains accurate.
- **Repo-wide ruff format apply** (53 files reformatted). Ruff
  0.15.16's formatter joins short multi-line string literals onto
  one line; the diff is pure whitespace.

- **Second greenfield: migrations re-baselined** (ADR 0019). The
  sealed `0001_initial.sql` is now a fresh `pg_dump` of the cluster
  master after migrations 0001–0017 had landed (PG 17.10 schema
  snapshot + seed-vocabulary dump for actors / kinds / relations /
  providers / chunk_kinds / embedders / summarizers /
  artifact_kinds). The original 0001–0017 files are preserved under
  `src/precis/migrations/archive/` for history; the runner's
  `glob("*.sql")` is non-recursive so they're invisible to
  `precis migrate`. Motivation: four sealed files (`0001`,
  `0002`, `0009`, `0010`) had drifted post-seal during the bug-fix
  run, and the runner's checksum gate blocked applying the new
  `0016` (HNSW) and `0017` (tag_embeddings). The cluster master's
  `_migrations` ledger was rewritten in a single transaction to
  record only the new sealed `0001_initial`. Pre-flight backup at
  `~/work/backups/precis-cluster-20260605-143621.dump` (151 MB,
  restore-tested). Source comments and `tests/test_initial_migration.py`
  were updated to reference the cumulative seed state instead of
  the archived per-migration files.

### Added

- **`precis stats` CLI subcommand** — quick observability for the
  finding-chase pipeline. Default prints two sections: STATUS-count
  for `kind='finding'` ("how many findings are tracing /
  established / multi_candidate / dead_chain?") and a stub backlog
  count partitioned by `awaiting` vs `retry`. Flags `--findings` /
  `--stubs` isolate one section; `--format json` produces a single
  keyed object suitable for piping through `jq`. Complements the
  existing row-level `precis stubs` (which lists the backlog) by
  answering "how big is it?" without dumping every row.
- **`FindingHandler.search` override** — status-axis filter on
  finding searches. Default behaviour returns only
  `STATUS:established` rows so the common "what evidence do we
  have for X?" call doesn't surface in-flight noise. `status=` is
  a shorthand that desugars to a tag filter (e.g.
  `status='tracing'` ≡ `tags=['STATUS:tracing']`); `status='*'`
  bypasses the filter to inspect every cohort. Results render as
  a TOON table `id | title | setup | primary` matching the
  finding-chase design's "scannable list" shape; the begat-chain
  detail still lives behind `get(kind='finding', id=N)`. Closed-
  vocab `_CLOSED_VOCAB['STATUS']` extended to union the original
  todo-workflow values (`open`/`doing`/...) with the finding-chase
  values (`tracing`/`established`/...) so filter-time validation
  accepts both without per-kind hooks.
- **Misattribution-link rendering on `get(kind='finding')`.** When
  a finding carries outbound `misattributes` edges (e.g. a user
  flagged a chain hop as wrong via `link(... rel='misattributes')`),
  the begat detail surfaces them under a `misattributed via:`
  block alongside the begat chain. Closes the corresponding DoD
  bullet in `docs/design/finding-chase.md`.
- **Chase-worker scenario tests** (`tests/workers/test_chase.py`).
  Nine scenarios exercising `run_finding_chase_pass` against a
  real Postgres store with `_load_s2_references` mocked: terminal,
  stub-waiting, hop, cycle protection, three dead-chain modes
  (no resolvable cite / target deleted / empty chain),
  multi-candidate, and card-combined re-emit at chain termination.

### Fixed

- **Mypy 2.1 strictness catch-up.** Tightens nine pre-existing type
  bugs in tests that mypy 1.20 was lenient about: `_make_notice`
  in `test_provenance_rw.py` now uses the `RetractionStatus` and
  `Severity` Literal aliases so `Notice`'s Literal-typed fields
  type-check; the local `_StubHub` class in
  `test_finding`/`test_citation`/`test_verify`/`test_stats`/
  `workers/test_chase.py` is replaced with a real
  `Hub(store=store)` constructed from dataclass defaults; and
  `_seed_paper` in `test_paper.py` types its `authors` param as
  `list[dict[str, Any]] | None` to match `insert_ref`'s signature.
- **Windows CI no longer fails on
  `test_ingest_failure_str_format`.** The assertion now compares
  against `str(path)` rather than a hardcoded POSIX literal so
  `pathlib.Path("/tmp/x.md")` renders with the platform-native
  separator (`\tmp\x.md` on Windows) without failing the contains
  check.
- **Migration runner no longer masks SQL errors as
  `InvalidSavepointSpecification`.** `Migrator.apply_all` previously
  opened its connection with `autocommit=False`. The first `SELECT`
  for `_applied_versions` opened an implicit transaction, so the
  per-migration `with conn.transaction()` downgraded to a SAVEPOINT
  instead of issuing BEGIN. When a migration aborted mid-execution,
  the savepoint vanished and the context-manager exit raised
  `psycopg.errors.InvalidSavepointSpecification: savepoint
  "_pg3_1" does not exist` — burying the real error. Switched the
  connection to `autocommit=True` so `conn.transaction()` issues a
  real BEGIN/COMMIT and surfaces inner exceptions directly. Migration
  0010 (the noisy-segment-keyword backfill) had been failing this
  way; it now applies cleanly.
- **Docker build no longer deadlocks on bge-m3 fetch.** The bake
  stage now seeds `/opt/precis/models/` from a `precis-mcp:premodels`
  image (BuildKit `--build-context`) before invoking
  `bake-models.py`, and the bake script skips the download when the
  cache directory is already populated. The verification
  `SentenceTransformer("BAAI/bge-m3")` call runs with
  `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1` so it cannot
  fall back to the deadlock-prone xet-bridge path. Source-only
  rebuilds dropped from "indefinite hang" to ~55 seconds. See
  [ADR 0019](docs/decisions/0019-premodels-build-context.md) for
  rationale and bootstrap instructions (`docker tag precis-mcp:latest
  precis-mcp:premodels`).

## v8.1.0 — finding-chase + OA fetcher cascade + event log (2026-06-01)

### Added

- **New ref kind: `finding`** — chain head over a citation chase to
  a primary source. A finding carries the claim (`finding_body`
  chunk), the setup envelope (`refs.meta.scope` JSONB), and the
  ordered hops of the chase chain (`refs.meta.chain`). The chase
  worker walks the chain one hop per pass, terminating at a
  primary measurement; the chain-snapshot pass populates
  `meta.primary_cite_key` + `meta.via_cite_keys` and re-emits the
  `card_combined` chunk via DELETE+INSERT so search picks up the
  established phrasing.
  - Determinism: `make_finding_paper_id(body, scope, cited_in)` →
    `make_pub_id` produces a stable 6-char `pub_id` so two agents
    creating the same finding collapse at the
    `ref_identifiers (id_kind='pub_id')` UNIQUE constraint.
  - **Setup-context awareness**: same number under different
    setups → distinct findings. `meta.scope` is the structured
    slice for filtering; the skill mandates `search` before `put`
    keyed on `(claim, scope)`.
  - **Not externally citable**: `cite(kind='finding', ...)` raises
    `Unsupported`. The placeholder → primary substitution via
    `precis resolve` is the only path findings reach published text.
  - Migration `0004_finding_and_queue_family.sql` seeds the kind,
    two `chunk_kinds` (`finding_body`, `finding_context` — the
    latter dormant under Path B; setup is folded into body),
    two relations (`misattributes`/`misattributed-by`), and one
    actor (`chase`). Design: `docs/design/finding-chase.md`.
- **`precis.workers.chase.run_finding_chase_pass`** — sibling
  worker per ADR 0018 that advances `STATUS:tracing` findings
  one hop per pass. Walks the source paper's `chunks` + S2
  references list to detect inline cites; resolves each to a
  ref_id (existing via `ref_identifiers`, or new chase-minted
  stub when the cited paper isn't in the corpus yet). Cycle
  protection via `meta.chain` membership. Per-pass outcome
  (`advanced` / `terminated` / `dead_chain` / `multi_candidate` /
  `cycle` / `waiting`) writes to the new `ref_events` table
  (`source='chase'`). Wired into `precis worker --only chase`.
- **LLM hooks via `precis.utils.claude_p`** — `claude -p`
  subprocess wrapper (project-wide reusable utility). The chase
  uses three default-off hooks gated by `--with-llm` or env
  `PRECIS_CHASE_LLM=1`: `_disambiguate_candidates` (multi-cite
  picker), `_locate_chunk_in_target` (ANN top-1 confirmation),
  `_verify_support_with_caveats` (does this chunk support the
  claim under the setup; captures caveats + cited-others).
  Deterministic chase is fully functional without LLM; hooks
  improve quality where they fire.
- **ADR 0017 — Derived-queue family** — formalises the
  `*_artifacts` substrate + `artifact_kinds` registry as a
  cross-cutting pattern. Migration `0004_*.sql` creates
  `ref_artifacts` + `artifact_kinds`. §4 (WorkerHandler refactor)
  is superseded by ADR 0018's sibling-worker decision; substrate
  tables remain valid.
- **Generic event log: `ref_events` table** (migration
  `0009_ref_events.sql`) — cross-subsystem chronological audit
  trail. One row per event with `(ref_id, ts, source, event,
  payload, duration_ms, cost_usd)`. New `EventsMixin` on
  `Store` exposes `append_event` / `events_for` / `recent_events`.
  Consumers writing today: `chase`, `fetcher:unpaywall`,
  `fetcher:arxiv`, `fetcher:s2`. Read via
  `get(kind='finding'|'paper', view='log')` or directly.
- **`view='log'` on `FindingHandler` and `PaperHandler`** — renders
  the per-ref chronology with per-subsystem one-line summaries.
  FindingHandler scopes to `source='chase'`; PaperHandler is
  cross-source.
- **`precis.workers.fetch_oa.run_oa_fetch_pass`** — sibling
  worker that fetches OA PDFs for stub papers in a cascade:
  **Unpaywall → arXiv → S2.openAccessPdf**. First `fetch_ok`
  wins; intermediate failures (`no_oa_version` / `fetch_failed`)
  fall through to the next provider, each writing its own audit
  event. Downloads validate the PDF magic bytes before keeping
  the file (publisher HTML interstitials are rejected); polite
  User-Agent with email tag. Cost cap via per-pass `--limit`;
  24-hour retry window per stub. Wired into `precis worker
  --only fetch` (default `precis worker` runs alongside embed +
  summarize + segments + chase). Requires `PRECIS_UNPAYWALL_EMAIL`
  for the Unpaywall leg; arXiv and S2 always available.
- **`precis_add` stub-upgrade + multi-hash alias path** — new
  helper `register_aliases_and_maybe_upgrade` in `db_writer.py`.
  On `probe_existing` hit: always register the new
  `(pdf_sha256, content_hash)` rows in `ref_identifiers`
  (multiple hashes per ref are first-class — preprint vs
  publisher vs repository). When the existing ref is a stub
  (`pdf_sha256 IS NULL`), additionally promote it: UPDATE
  `refs.pdf_sha256`, insert the extracted chunks, embed via
  derived queue. Findings waiting on the stub resume on the
  next chase pass without any extra plumbing.
- **`precis resolve` CLI subcommand** — substitutes
  `[<pub_id>]` placeholders with the primary `cite_key` once
  findings establish. Plain / markdown / latex output;
  `--strict` exits 3 if any placeholder is still in flight
  (CI-gate friendly); `--keep-id` keeps dead-chain placeholders
  annotated. LaTeX `--bib` writes stub `@misc{...}` entries
  so documents compile during the in-flight period. Visible
  ⏳ marker (ASCII `*` via `--ascii`) so authors don't ship
  placeholders by accident.
- **`precis stubs` CLI subcommand** — lists paper refs with
  `pdf_sha256 IS NULL` joined with the latest fetcher event per
  ref. TOON-table output; `--awaiting` filters to "what would
  the fetcher try next".
- **New skill: `precis-finding-help`** — full workflow shape,
  when-to-create / when-NOT-to-create, mandatory
  search-before-create rule, FAQ on chase outcomes.

### Changed

- **`paper.py` empty-search DOI hint** — when a DOI lookup
  misses, the response now offers `put(kind='finding',
  cited_in='doi:...')` plus `precis stubs --awaiting` as the
  preferred structured path. The legacy
  `edit(kind='plaintext', id='./request_doi.md', ...)` queue
  is retained as a deprecated fallback (one release of
  warnings; remove in the next).
- **`precis worker --only`** gains `chase` and `fetch` choices
  alongside the existing `embed` / `summarize` / `segments`.
  Plus `--with-llm` (chase) and `--fetch-inbox` / `--unpaywall-email`
  (fetcher).
- **`PaperHandler.accepted_views`** gains `log`; renders via the
  shared `precis.handlers._event_log_render` module.

### Migrations

- `0004_finding_and_queue_family.sql` — finding kind + queue
  family scaffolding (ADR 0017).
- `0009_ref_events.sql` — cross-subsystem per-ref event log.

Both are additive (no ALTER on existing tables); apply cleanly
to a fresh DB and to a live v8.0.0 DB.

### Deprecated

- **`./request_doi.md` plaintext DOI queue** — superseded by
  the finding-chase + fetcher pipeline. Still works; the
  empty-search hint now labels the option `(legacy)`. Remove
  in the release after next.

## v8.0.0 — 2026-05-31

### Added

- **Discovery layer for `paper` kind** — persistent per-segment
  artifacts now back `view='toc'` and the search-result excerpt
  sub-lines. Two new tables (`ref_segments`,
  `ref_segment_sentences`), a ref-level worker
  (`precis worker --only segments`), and read-side store mixin
  (`SegmentsMixin`). Pipeline:
  - **DP-uniform-cost segmentation** (replaces TextTiling) on
    bge-m3 chunk embeddings with K = `ceil(body/20)` clamped
    `[3, 9]`.
  - **Matryoshka-ordered keywords** per segment — scored via
    KeyBERT-style cosine to segment centroid with a
    distinctiveness penalty against sibling centroids (λ ≈ 0.3),
    so `keywords[0]` is what's most-distinctive about this
    segment rather than most-frequent. Stored as JSONB
    `{long, short, aliases[], score}` with a denormalized
    GIN-indexed `forms TEXT[]` for cross-paper surface-form
    lookups.
  - **Per-sentence bge-m3 embeddings** — every body sentence
    gets an embedding + centroid score. TOC excerpts pick top-1
    by centroid score; search-result excerpts rerank against the
    query embedding via pgvector `<=>`.
  - Migration `0005_segments_and_sentences.sql` (requires
    `btree_gist` for the mixed-type segment-range GiST index).
  - Documented in ADR 0018; schema diagram refreshed in
    `docs/design/schema-v2.puml`.
- **New kind: `citation`** — verifier-workflow scaffold for
  writing-thread agents. Write-once `put(text=<claim>,
  source_handle, source_quote, char_offset, verifier_confidence,
  verifier_caveats, link='paper:<slug>', rel='cites')` persists
  a verified claim → source-quote pointer to `refs.meta`.
  Migration `0007_citation_kind.sql` seeds the kind; the `cites`
  relation was already in the vocab. See
  [`precis-citation-help`](src/precis/data/skills/precis-citation-help.md).
- **`chunks.numerics TEXT[]` lexical numeric-token index** —
  ingest extracts every `<number><unit>` token (eV/V/A/Hz/cm⁻¹/
  %/K/°C/Pa/M/nm/cycles/s/…) and stores them GIN-indexed for
  cheap exact-value lookups (path-2 from the tables-curveball
  discussion; structured `paper_facts` remains deferred).
  Migration `0006_chunk_numerics.sql`.
- **References detection at ingest** — `pipeline._retag_references`
  runs the boilerplate classifier on body chunks and rewrites
  detected bibliography rows to `chunk_kind='references'` before
  insert. `EmbedHandler` and `RakeLemmaHandler` carry
  `skip_chunk_kinds=("references",)` which extends the claim SQL
  with `AND c.chunk_kind <> ALL(%s)` — references never enter
  the work queue, bibliography stops polluting search.
- **pysbd-backed sentence splitter** — `precis.utils.sentences`
  wraps pysbd 0.3.4 with char-offset bookkeeping. Wired into
  `text_chunker` via a `SENTENCE_SEPARATOR` sentinel in the
  recursive splitter's fallback chain, so abbreviations like
  `"et al."`, `"Fig."`, `"i.e."`, `"e.g."`, `"vs."` no longer
  cause mid-clause splits. `CHUNKER_VERSION = "2.0+pysbd-0.3-1"`
  is now a real constant in `text_chunker.py`. Adds
  `pysbd>=0.3.4` to the `[paper]` extra.
- **Dehyphenation in the cleaner** — `marker._clean_text` gains
  a regex pass joining `-\s*\n\s*` when both sides are lowercase
  ASCII. Semantically-significant hyphens (`Z-scheme`, `Cu-MOF`,
  any compound with uppercase boundaries) are preserved, and the
  join never crosses paragraph breaks.
- **Retraction banner on paper views** — `kind='paper'` `view=
  'overview'`, `view='toc'`, and chunk drill-in views now lead
  with a `> [!] RETRACTED` (or EoC / corrected) banner when
  `refs.retraction_status` is set, with the retraction date and
  reason inline and a pointer at
  `get(kind='provenance', id='<doi>')` for the full notice.

- **New kind: `provenance`** — retraction and amendment monitoring
  for paper DOIs. Five phases, all shipped:
  - **Phase 1** — single-DOI Crossref check. Validates DOI shape,
    fetches `/works/{doi}`, classifies any `update-to` notices by
    severity (`retraction` → 🔴 blocker, `expression_of_concern` →
    🟠 review, `corrigendum`/`erratum` → 🟡 note,
    `addendum`/`clarification` → 🟢 info), and when the parent
    paper is in the local store: auto-ingests retraction / EoC
    notice DOIs as `paper` refs (slug rule:
    `<parent>-r<n>` / `-e<n>` / `-c<n>`), writes `retracted-by` /
    `concern-raised-by` / `corrected-by` links, sets
    `refs.retraction_status`, applies a `STATUS:retracted` /
    `:concern` / `:corrected` tag. Migration `0002_provenance.sql`
    adds the six new relation slugs and the `retraction_watch`
    provider. Notice refs carry `STATUS:notice`.
  - **Phase 2** — batch input via `q='doi1,doi2,…'`, `view='blockers'`
    (only 🔴/🟠 entries with a count of hidden 🟡/🟢), `view='json'`
    (structured payload for downstream tooling),
    `ThreadPoolExecutor(max_workers=8)` for the fan-out, order
    preservation, per-DOI failure isolation
    (`status='check_failed'` on transport errors never kills the
    batch). New CLI: `precis jobs check-provenance --refs <file>
    --view default|blockers|json --out <file>`.
  - **Phase 2.5** — `view='verify'` metadata verification. Catches
    "right DOI, wrong paper" — common with LLM-generated bibs.
    Token-set Jaccard on titles with NFKD normalisation
    (`Müller`→`muller`, `H₂O`→`h2o`, `ﬁ`→`fi`) plus German-phonetic
    alt for surnames (`Müller`↔`Mueller`, `Schröder`↔`Schroeder`)
    plus reverse-phonetic fold for the ASCII↔ASCII case
    (`Mueller`↔`Muller`) — trade-off: false positives on
    `Sue`↔`Su`, `Press`↔`Pres` accepted because the cost (a
    suppressed warning) is bounded. Year ±1 tolerance for
    online-first vs print drift. No hardcoded pass/fail
    thresholds; raw scores emitted in JSON for downstream rules.
  - **Phase 3** — Retraction Watch reason codes joined into the
    report (`+Falsification/Fabrication of Data` etc., not just
    "retracted"). Migration `0003_provenance_rw_cache.sql` adds
    the cache + sync ledger tables. New job
    `precis jobs sync-retraction-watch --mailto <email>
    [--source auto|labs|gitlab]` — tries Crossref Labs API
    primary, falls back to the GitLab mirror
    (`gitlab.com/crossref/retraction-watch-data`). ~40 MB CSV,
    ~50k rows, batched upsert in 10k-row chunks, idempotent on
    RW Record ID. Match strategy: exact notice-DOI match, with
    single-row-per-nature fallback.
  - **Phase 3.5** — Numbered `#N` output across all batch views
    matching the project's standardised LLM-output convention
    (`utils/search_merge.py:208`). Every batch result carries a
    1-based `input_index` reflecting *input* order (not thread-
    pool completion order); the same `#47` appears in default,
    blockers, and JSON views even when intervening entries are
    hidden. Eliminates LLM off-by-one errors when generating
    follow-up actions against a numbered report.
  - **Phase 4** — `transitive=True` flag enables depth-1
    cite-walk via Crossref's `message.reference` field. For each
    parent: shallow-checks every cited DOI, surfaces only
    ≥ 🟠 findings as `cited_findings`, skips corrigenda
    (too noisy at depth 1). Per-batch dedup cache so a cited
    paper shared by N parents hits Crossref once. Clean-itself
    papers that cite retracted work are promoted into the
    🟠 Review bucket so blockers view doesn't hide them.
  - **Phase 5** — three additions:
    - `paper view='health'` shim — looks up the paper's DOI from
      `ref_identifiers` and delegates to `provenance` so agents
      with a slug skip the manual DOI lookup.
    - `view='exists'` shortcut — compact ✓/✗ output for "does
      this DOI resolve?" without the retraction-classification
      overhead. Useful for validating a DOI list before doing
      real work.
    - `suggest_candidates=True` — when a DOI 404s *and* a
      `BibEntry` with bibliographic metadata is supplied, calls
      Crossref `/works?query.bibliographic=…&query.author=…`
      and attaches ranked candidates as **advisory hints** under
      the Unknown DOI section. **Never substitutes** — the
      supplied DOI's status stays `unknown`. Fuzzy
      auto-resolution was explicitly rejected; see
      `docs/design/provenance-kind-plan.md` § "Rejected: fuzzy DOI
      auto-resolution" for the rationale.
  - Source layout: `ingest/provenance.py`, `ingest/_text_norm.py`
    (Phase 2.5 helpers), `ingest/_rw_csv.py` (Phase 3 parser),
    `jobs/provenance_rw_sync.py`, `handlers/provenance.py`,
    `handlers/_provenance_report.py`, `cli/provenance.py`. Skill
    cards at `data/skills/precis-provenance-help.md` and
    `data/skills/precis-preflight.md`. Migrations `0002` and
    `0003` in `src/precis/migrations/`. Tests at
    `tests/ingest/test_provenance{,_verify,_rw,_transitive,_phase5}.py`.
  - Design doc: `docs/design/provenance-kind-plan.md`.

- **Phase 6.1 — RW cache as Crossref fallback.** `check_doi` now
  always consults the local Retraction Watch cache regardless of
  Crossref's outcome. Three concrete behaviours that the previous
  Crossref-first flow missed:
  - **Publisher never deposited an `update-to` relation.** Pre-CrossMark
    retractions (notably the Hwang stem-cell paper, retracted 2006)
    return clean from Crossref but appear in RW. We now synthesise a
    `Notice` from the RW row (`update_type=""` + `rw_notice_nature`
    populated) so they surface in the report with a `(RW)` source
    label.
  - **Crossref returned 404.** Previously `status='unknown'` and no
    further work. Now: if RW has data for the same DOI we surface
    the retraction with `status='ok'`, falling back the paper title
    from the RW row.
  - **Crossref timed out / transport error.** Previously
    `status='check_failed'`. Now: if RW has data, the report assembles
    from local-only data with `status='ok'`; the error string is
    preserved on `result.error` and the renderer surfaces a
    `⚠️ Crossref unavailable` banner so the reader knows the live
    source wasn't consulted.
  - The internal `_merge_crossref_and_rw_notices` helper covers both
    enrichment (Crossref notice matched to RW row → reasons attached)
    and synthesis (RW row with no Crossref match → new Notice). Dedup
    by `notice_doi` so a paper with both sources doesn't double-count.
    Renderer updated: `(RW)` label for synthesised notices, suppresses
    the "notice DOI: \`\`" line when the DOI is empty (common for
    older RW entries).
  - No schema change; uses existing `provenance_rw_cache` columns.
    Backwards-compatible with the Phase 3 enrichment-only helper via
    `_enrich_notices_with_rw(_synthesize_rw_only=False)`.

- **Postgres advisory-lock work claims for multi-host ingest**
  (`precis.ingest.claim.Claim`). Each PDF ingest opens a dedicated
  psycopg session and calls `pg_try_advisory_lock(key)` where `key`
  is the first 64 bits of `pdf_sha256`. If acquired, the host owns
  the work; if busy, `precis_add` returns `None` and the watcher
  leaves the file in place for the owning host to handle. The
  claim auto-releases when the session closes — clean exit,
  exception, container OOM, Mac crash, network partition. No
  heartbeat, no stale-row reaper, no schema migration. Enables
  multiple Macs to share a single `/inbox` (via SMB) talking to a
  central Postgres, with no risk of two hosts running Marker on the
  same content. See ADR 0016. Tests at
  `tests/ingest/test_claim.py::TestKeyDerivation` (pure) and
  `::TestClaimIntegration` (skipped without `PRECIS_DATABASE_URL`).

- **`Store.dsn`** attribute exposes the original connection string
  so callers needing a non-pooled connection (like `Claim`) can
  open one. ``None`` when the Store was constructed from a
  pre-built pool (tests).

- **`_move_to` / `_move_to_corpus` tolerate FileNotFoundError on
  rename**, treating it as "another host already moved this file."
  Required for multi-host operation against a shared inbox where
  the same physical file can be observed by N watchers
  concurrently.

### Changed

- **`precis_add` may return `None`.** New return type
  `IngestResult | None`. `None` means another host owns the
  advisory-lock claim on this PDF's hash; the caller should not
  touch the file. Existing single-host callers (one watcher,
  no other hosts) never see `None` in practice.

### Removed

- **Filesystem-based crash locks** (`.processing/*.lock`,
  `_acquire_lock`, `_release_lock`, `_lock_path_for`,
  `_recover_crashed`). Superseded by the Postgres advisory-lock
  claim above. The `--lock-dir` flag on the hidden
  `_watch_batch_ingest` subcommand is also gone. Leftover
  `.processing/` directories on disk from prior deployments are
  harmless and can be deleted manually.

### Added (continued)

- **Subprocess-per-batch backfill** (`precis watch
  --subprocess-batch-size N`). When positive, startup backfill
  spawns ``precis _watch_batch_ingest`` subprocesses of N PDFs each
  and waits for each before starting the next. Marker / surya /
  transformers memory leaks accumulate inside the long-running
  watcher process across consecutive ingests; subprocess isolation
  reclaims them at child exit. Default 0 (in-process — legacy
  behaviour); production `precis-watch` compose service now ships
  with `--subprocess-batch-size 50`. Cost: 1 Marker reload (~15 s)
  per batch. Benefit: bounded per-batch RSS rather than monotonic
  growth into OOM. Live (watchdog) events keep the in-process
  path since they're rate-limited by arrival. See ADR 0015.

- **In-process Marker cache cleanup**
  (`precis.ingest.marker._release_marker_caches`). Called at the
  end of every `extract_blocks_marker` to run `gc.collect()` and
  (when available) `torch.cuda.empty_cache()` /
  `torch.mps.empty_cache()`. All branches import-and-feature
  guarded; safe on CPU-only deployments without torch. ~10 ms/PDF
  overhead. Layered on top of subprocess isolation as a no-regret
  probe — may help with ref-cycle leaks but isn't a substitute for
  Fix B's structural isolation. See ADR 0015.

- **`python -m precis` entry point** (`src/precis/__main__.py`).
  Mirrors the `precis` console script. Used by the subprocess
  spawner above to avoid depending on `$PATH` resolution for the
  child invocation.

- **XMP write + signed-PDF detection in ``pdf_writer``.** The
  metadata write-back path now emits a minimal RDF/XMP packet
  carrying ``dc:title``, ``dc:creator`` (authors), ``dc:identifier``
  (DOI prefixed with ``doi:``), ``prism:doi`` (raw DOI), and
  ``prism:url`` (arXiv URL) alongside the standard Info-dict write.
  Exiftool's ``-Identifier`` flag now reads our DOI from the
  canonical XMP slot rather than just the Keywords fallback.
  Cryptographically-signed PDFs (``Signature`` widget present)
  return ``PatchOutcome(skipped_reason="signed")`` without touching
  the file — incremental save preserves signatures *usually*, not
  *always*. The check is bounded to ``doc.is_form_pdf`` so unsigned
  PDFs pay zero cost. AcroForms with only text widgets still patch
  normally. Closes the two follow-ups noted in the initial
  ADR 0014 cut. Tests at
  ``tests/ingest/test_pdf_writer.py::TestXmpWrite`` and
  ``::TestSignedPdfSkip``.

### Changed

- **`precis-watch` memory cap raised from 12 GiB to 64 GiB.** Was
  a backstop for single-PDF OOMs; now also absorbs cumulative
  Marker memory leakage across long backfills until the
  subprocess-per-batch isolation lands fully. The 16 GiB host-total
  budget in the older comment is now stale; compose deploys
  assume the host has enough headroom for this cap. Comment
  updated. See ADR 0015.

- **`precis-watch` backfill processes smallest PDFs first.** Sort
  key in ``_PdfHandler.backfill`` changed from path name to file
  size. Means a single giant PDF that OOMs the container only
  blocks itself; the small files behind it have already been
  ingested. Stat errors sort last so a broken symlink doesn't
  abort the whole backfill. Test
  ``tests/test_watch.py::TestBackfillOrder``.

## v7.1.0 — baked-in models, fast-path ingest, MCP cold-start budget (2026-05-28)

First v7.x line release on PyPI. Highlights: `precis-mcp:latest` ships
with bge-m3 + Marker weights baked in (no first-ingest download); a
sha256-keyed fast-path skips Marker on re-ingest of a known PDF; the
MCP cold-start banner / `tools/list` shrinks under a pinned token
budget with three operator env vars (`PRECIS_STARTUP_SKILLS`,
`PRECIS_KINDS_DISABLED`, `PRECIS_DEFAULT_TAGS`); new `precis-worker`
service drains the embedding/summary queue continuously; new `tex`
file kind; `PRECIS_ROOT` consolidates the prose-file env vars;
`search(exclude=[...])` enables ref-level pagination; nightly
`precis maintenance run` driver lands.

### Added

- **PDF metadata write-back during ingest.** New
  ``precis.ingest.pdf_writer`` module patches the resolved canonical
  Title / Author / DOI into each successfully-ingested PDF's Info
  dict (Title, Author, Subject, Keywords). Uses pymupdf's incremental
  save so the existing content stream stays byte-identical; only an
  update section is appended. Both the pre-patch and post-patch
  ``pdf_sha256`` land in ``ref_identifiers`` as separate alias rows
  pointing at the same ``ref_id``, so re-ingesting either byte
  sequence still hits the fast-path probe. ``PaperToWrite`` grows a
  ``pdf_sha256_aliases: list[str]`` field; ``write_paper()`` inserts
  one extra ``ref_identifiers`` row per alias. Default ON; operator
  off-switch ``PRECIS_PATCH_PDFS=0`` / ``false`` / ``no`` / ``off`` /
  empty. Skip cases: ``encrypted`` (DRM), ``noop`` (fields already
  match — keeps re-ingest of an already-patched PDF from drifting),
  ``disabled``, ``error`` (any exception during open/save logs
  WARNING and skips). Reverses the B4b removal of
  ``write_pdf_metadata()`` from ``acatome_extract``; safe now because
  v2's multi-row ``ref_identifiers`` model absorbs the hash drift
  that motivated the original removal. New tests at
  ``tests/ingest/test_pdf_writer.py`` (10 cases). See ADR 0014.

- **Wrapper scripts in ``scripts/``** for the day-to-day Docker
  workflow, all honouring ``PRECIS_COMPOSE`` if the infra repo
  lives outside ``~/work/infrastructure``:
  - ``scripts/precis-shell`` — standalone dev shell (no compose
    dep); auto-builds ``precis-mcp:dev`` on first use, mirrors the
    compose bind-mounts but degrades gracefully on missing host
    paths. ``--rebuild`` forces a fresh build.
  - ``scripts/precis-add <pdf | --doi | --arxiv>`` — one-shot ingest
    via ``precis-cli``. Auto-mounts any positional file argument at
    ``/inbox/<basename>``.
  - ``scripts/precis-watch [start|stop|restart|status|logs|fg|tail]``
    — manage the PDF ingestion daemon. Default ``tail`` brings the
    watcher up detached and follows logs.
  - ``scripts/precis-index [path] [--force] [--kinds md,plaintext,tex]``
    — one-shot ``precis jobs ingest`` to pre-warm prose files
    under ``PRECIS_ROOT``.
  - ``scripts/precis-embed`` and ``scripts/precis-summarize`` —
    symmetric wrappers around the worker queue. Subcommands
    ``once`` / ``status`` / ``start`` / ``stop`` / ``logs``. Both
    daemons share the ``precis-worker`` container (one daemon
    handles both handlers; running two would double-claim chunks).

### Fixed

- **``exiftool`` no longer warns "not found in PATH" in
  ``precis-watch`` logs.** Runtime image now apt-installs
  ``libimage-exiftool-perl`` in ``docker/Dockerfile`` stage 2.
  The warning surfaced once per ingest at
  ``src/precis/ingest/pdf_metadata.py:162`` and was non-fatal
  (extraction falls back to ``{}``), but it noised up the daemon
  log and reduced embedded-metadata recall on papers whose only
  DOI signal is the publisher-set ``-Identifier`` XMP field.

- **`BgeM3Embedder.model` now returns the precis registry key
  (`bge-m3`) rather than the HuggingFace id (`BAAI/bge-m3`).**
  The previous behaviour caused a ``ForeignKeyViolation`` on
  every ``chunk_embeddings`` insert because the column FKs
  against ``embedders.name`` (seeded as ``bge-m3``), and the
  worker wrote ``embedder.model`` straight into the column.
  HF id is now an internal constant (``_BGE_M3_HF_ID``) used
  only when ``SentenceTransformer`` actually loads weights;
  the registry key (``_BGE_M3_REGISTRY_KEY``) is what flows
  through ``EmbedHandler.write_ok``. Test
  ``tests/test_embedder.py::test_bge_m3_construction_is_lazy``
  updated to assert the registry-key contract.

- **`Store.embedding_dim()` no longer reads from the absent
  `system` table.** The method now sources the dim from
  ``embedders.dim WHERE is_default = TRUE`` — the migration
  already seeds that row (``bge-m3, 1024``). Search via
  ``precis tools search`` and ``precis serve`` were both
  failing on every fresh DB with ``UndefinedTable: relation
  "system" does not exist``. The companion ``get_setting`` /
  ``set_setting`` methods are unchanged and remain effectively
  dead until the ``system`` table is added by a follow-up
  migration; ``oracle_sync.py`` already swallows their
  ``AttributeError`` / ``Exception`` and re-ingests on miss,
  so its observable behaviour is unchanged.

### Changed

- **New `precis-worker` compose service** in
  ``infrastructure/compose.yaml``: continuously drains the
  derived-artifact queue (``chunk_embeddings`` +
  ``chunk_summaries``). Same image as ``precis-watch``, no
  ``/inbox`` or ``/data/corpus`` mounts, command
  ``precis worker --batch-size 32 --idle-seconds 5``. Bring
  up with ``docker compose up -d precis-worker``. The watcher
  writes chunks; the worker turns them into vectors +
  summaries. See ADR 0007.

- **`precis-mcp:latest` ships with model weights baked in.** A new
  ``models`` stage between ``builder`` and ``runtime`` in
  ``docker/Dockerfile`` runs ``marker.models.create_model_dict()``
  and ``SentenceTransformer('BAAI/bge-m3')`` at build time and
  COPYs the resulting caches into the runtime image at
  ``/opt/precis/models/`` (``HF_HOME=/opt/precis/models/hf``,
  ``MODEL_CACHE_DIR=/opt/precis/models/datalab/models``). First
  ``precis add`` / ``precis watch`` start no longer downloads
  ~3 GB from HuggingFace + datalab. Image grows from ~6.4 GB to
  ~10 GB; RAM behaviour unchanged (lazy mmap on first use).
  The GoNoto font ``marker`` writes to ``site-packages/static/
  fonts/`` is pre-populated in the ``builder`` stage so the
  read-only venv at runtime never trips on it. See
  ``docs/design/bake-models-into-image.md``.
- **`infrastructure/compose.yaml`: `precis-watch` no longer mounts
  `precis-cache:/home/precis/.cache`.** Models live in the image
  now; the named volume is retired. On hosts where it already
  exists, free it with
  ``docker volume rm precis-infra_precis-cache``.

### Fixed

- **Ingest fast-path: re-ingesting a known PDF no longer re-runs
  Marker.** :func:`precis.ingest.add.precis_add` now probes
  ``ref_identifiers (id_kind='pdf_sha256')`` for ``PdfInput``
  *before* invoking the extraction pipeline. A hit short-circuits
  to ``IngestResult(inserted=False, ref_id=...)`` without paying
  for the ~30–60 s Marker run. The slow-path probe (paper_id /
  DOI / arXiv / content_hash) still runs after extraction to
  catch "same paper, different bytes" collisions. No behavioural
  change for fresh ingests. Regression test:
  ``tests/ingest/test_add.py::TestPrecisAddIdempotent::test_fast_path_skips_marker_when_pdf_sha256_known``.
  See ``docs/design/extract-once.md``.


### MCP session ergonomics — cold-start token budget + kind enablement + default tags (2026-05-26)

Six-phase rollout (`docs/design/mcp-cold-start-token-budget.md`)
that shrinks the unconditional cold-start cost on a fresh MCP
session and adds three operator-controlled env vars for session
context. Every byte saved on the cold-start banner / `tools/list`
is a byte the agent gets to spend on the user's actual request.

- **Per-verb docstrings trimmed** (Phase 1). Each of the seven
  verbs (get, search, put, edit, delete, tag, link) keeps a
  ~5-line docstring that points at `precis-<verb>-help.md` for
  detail. The detail moved into ten new skill files in
  `src/precis/data/skills/` so it's still discoverable via
  `search(kind='skill', q=...)` — just not on every connecting
  agent's cold-start budget. CLI `--help` shows the same trimmed
  shape with per-arg help strings threaded through the CLI
  adapter. `precis-edit-protocol` renamed to `precis-edit-help`.
- **Cold-start banner re-framed** (Phase 2). `serverInfo.instructions`
  now leads with "First action: `search(kind='skill', q='<topic>')`"
  instead of a verb cheat sheet. Trailing `Kinds loaded:` line
  summarises the live registry so the agent sees what's actually
  wired without an exploratory call.
- **`PRECIS_STARTUP_SKILLS`** (Phase 3) — comma list of skill
  slugs to pin at session start. Cumulative body size capped at
  `PRECIS_STARTUP_SKILLS_CAP_KB` (default 50). Drop-tail truncation
  at the cap; banner notice surfaces both invalid ids and the
  truncation event. Pinned skills are tagged on the `prompts/list`
  response so MCP clients with server-pinned-prompt support can
  render them at handshake.
- **`PRECIS_KINDS_DISABLED`** (Phase 4) — comma list of kind names
  to prohibit at boot. Prohibition wins over resource availability
  (a disabled kind is hidden even when its env requirements are
  met). The patent handler's inline `EPO_OPS_KEY` / `EPO_OPS_SECRET`
  env gate moved into `PatentHandler.__init__` to converge with
  the gate machinery. Banner now carries a `Kinds unavailable:`
  line with distinct reasons (`prohibited` vs the specific
  `missing <ENV_VAR>`).
- **`PRECIS_DEFAULT_TAGS`** (Phase 5) — comma list of tags merged
  into every `put` on note-like kinds. `KindSpec.note_like: bool`
  flag (default False) opts kinds into the merge; nine handlers
  flipped to `True` (memory, todo, gripe, flashcard, quest,
  conversation, markdown, plaintext, tex); ingested / cache /
  generator kinds (paper, patent, web, youtube, math, oracle,
  skill, calc) stay False so they don't accumulate session tags.
  The `tag` verb emits a non-mutating suggestion hint listing
  defaults not yet present rather than auto-mutating — operator-
  explicit calls stay operator-explicit.
- **Regression guards** (Phase 6). `tests/test_token_budget.py`
  pins `tools/list` JSON < 12 KB (measured ~9 KB baseline),
  per-verb description < 1 KB, `serverInfo.instructions` < 2 KB
  on clean / < 4 KB with every Phase-3-5 feature engaged.
  Anchor strings (`First action`, skill-search CTA, `Kinds loaded:`)
  pinned. Cross-cutting test pins the shared comma-list parse
  semantics across `startup_skills.parse`, `kind_gate.parse_disabled`,
  and `default_tags.parse`.

New modules: `src/precis/startup_skills.py`, `src/precis/kind_gate.py`,
`src/precis/default_tags.py`. New config fields: `startup_skills`,
`startup_skills_cap_kb`, `kinds_disabled`, `default_tags`. New
hint topics: `default_tags.merged`, `default_tags.suggested`.

Test coverage: 88 new tests across `test_startup_skills.py`,
`test_kind_gate.py`, `test_default_tags.py`, `test_token_budget.py`,
plus extensions to `test_mcp_critic_regressions.py` for the new
banner shape. Suite: 1718 passed, 829 skipped, 5 xfailed.

### `find-citing-papers` — noise-reduction filters + bge-m3 rerank (2026-05-09)

The full sweep across the ~4k corpus surfaces ~900k unique citing
papers from S2 — way past human-readable. Five new aggregation flags
let you stack cheap filters into a digestible report without re-
fetching. All run in `--no-fetch` mode against the existing
`paper-ingest/.citing-papers-cache/` so iteration is seconds, not
hours.

- **`--min-co-cites N`** — drop citing papers that cite fewer than N
  of ours. Strongest single signal: 909k → 212k @ N=2; → 25k @ N=5;
  → ~3k @ N=10. The 294-of-our-papers review naturally floats to
  the top of the global sort.
- **`--min-citing-citations N`** — drop citing papers with fewer
  than N citations of their own. Filters out the 18% zero-cite
  fresh-preprint tail when you want "stuff that already got
  traction."
- **`--min-similarity X`** — bge-m3 cosine gate. Embeds the source
  corpus' title+abstract once, embeds surviving citing papers'
  title+abstract, drops anything whose max cosine across cited
  sources is below X. Typical usable threshold 0.50–0.65; ~30
  citing papers/sec on Apple Silicon. Uses the same
  `precis.embedder.make_embedder("bge-m3")` contract the rest of
  the package lives on.
- **`--top-n N`** — hard cap after sort.
- **`--per-source-top K`** — alternative aggregation: emit the top
  K most-recent / most-influential citations PER OUR paper instead
  of the global aggregate. Useful for "what's new for paper X"
  digests rather than corpus-wide topic surveys.

Sort precedence in global mode: co-citations DESC, similarity DESC
(when computed), publication date DESC, title. The
`AggregatedHit.similarity` field is surfaced both in the markdown
header (`sim 0.75` per-row meta) and in the JSONL feed
(`"similarity": 0.7501`) so downstream consumers can re-rank.

Files: `scripts/_find_citing_papers.py`, `scripts/README.md`,
`paper-ingest/README.md` (new).

### `search(... exclude=[slugs])` — ref-level pagination (2026-05-09)

New `exclude=` kwarg on the agent-facing `search` tool. Coarse / ref-
level skip-list of slugs (or DOIs, or copy-pasted hit handles like
`'wang2020~38'`) pushed down to both lex and sem CTEs in
`search_blocks_fused` so `LIMIT` runs **after** exclusion. The
canonical "I saw the top 5, give me hits 6..N" pagination idiom now
works without hacks.

- **`server.py::search`** — new `exclude: list[str] | None = None`
  kwarg, forwarded only when set (matches the `tags=` / `source=`
  conventions). Docstring teaches the shape; that's the cold-start
  discovery channel.
- **`paper.search` / `paper.search_hits`** — accept `exclude=`,
  resolve via the new `Store.fetch_ref_ids_by_slugs` bulk helper,
  forward `exclude_ref_ids=` to the store. Stale slugs are silent
  no-ops (the agent's exclude list may carry ids that no longer
  resolve; failing the whole call would be unfriendly).
- **`Store.search_blocks_fused` / `search_blocks_lexical` /
  `count_blocks_lexical`** — new `exclude_ref_ids=` kwarg. Applied
  via the shared `where_extra` clause so the predicate inlines into
  both CTEs in `search_blocks_fused` (otherwise RRF would fuse a
  filtered set against an unfiltered one — same reasoning as the
  existing tag filter). Total-count helper honours it too so the
  `N of K` header reflects the post-exclude universe.
- **`runtime._dispatch_cross_kind`** — forwards `exclude=` through
  the cross-kind fan-out via a new `_cross_kind_invoke_search_hits`
  helper that retries on `TypeError` (drops `exclude`, then `tags`)
  so handlers with smaller signatures degrade cleanly. Per-kind
  slug collisions are non-issues: `fetch_ref_ids_by_slugs` filters
  by kind, so a paper slug in the exclude list silently no-ops on
  memory et al.
- **`Next:` trailer** — multi-hit responses now render an
  `exclude=[...]` continuation pre-filled with the slugs of refs
  returned this page UNION'd with any prior `exclude=` the caller
  passed. The agent copy-pastes; no client-side bookkeeping. The
  singleton-hit branch keeps the existing `top_k=10` widen hint.
- **Docs** — new "Skip what you've seen" section in
  `precis-paper-help.md`, one-line example in `precis-overview.md`,
  shipped entry in `docs/user-facing/search-future-filters.md`.
- **Tests** — 4 store-level (`test_block_search.py`: drops listed
  refs, `LIMIT` post-exclude, lex-only fallback path, count-lex
  honours exclude) and 8 handler-level (`test_paper.py`: drops
  paper, slug-with-selector accepted, DOI accepted, stale slugs
  silent, header reflects remainder, trailer pre-fills, trailer
  unions prior exclude, singleton branch keeps widen).

### `PRECIS_ROOT` consolidation — single root for prose-file kinds (2026-05-02)

`PRECIS_MARKDOWN_ROOT`, `PRECIS_PLAINTEXT_ROOT`, and the just-shipped
`PRECIS_TEX_ROOT` have been collapsed into a **single** env var,
`PRECIS_ROOT`. The three handlers walk the same tree and filter by
extension. Hard cut — no backward-compat fallback (the package is
pre-PyPI, only used internally).

- **`PrecisConfig.precis_root`** replaces `markdown_root` /
  `plaintext_root` / `tex_root`. Env: `PRECIS_ROOT`.
- **`dispatch.boot(precis_root=...)`** registers all three file
  handlers under the same root. Whole trio hidden when unset.
- **`runtime.build()`** threads `config.precis_root` through.
- `precis jobs ingest-md` falls back to `PRECIS_ROOT` instead of
  `PRECIS_MARKDOWN_ROOT`.
- **Write-access boundary documented** in `precis-files-help`:
  `PRECIS_ROOT` is the only writable area for the prose-file trio.
  Every read/write goes through `Path.resolve()` +
  `Path.relative_to(self.root)` — rejects `../` traversal **and**
  symlink escapes (since `resolve()` follows symlinks). This was
  always the behaviour; the consolidation makes the boundary
  explicit and named.
- **No absolute paths in LLM-visible output.** The LLM sees
  `PRECIS_ROOT` as `./` — it doesn't know (and shouldn't see) the
  absolute filesystem path of the configured root. Every error
  message and rendered listing in the markdown / plaintext / tex
  handlers now names the symbolic `PRECIS_ROOT`, never `self.root`.
  Affected sites: `NotFound("... not found under PRECIS_ROOT")`,
  `_render_index` header + empty-state message, `BadInput("file
  already exists: '<slug>'")` (was leaking the absolute path).
  Pinned by `tests/test_plaintext_handler.py::test_*_does_not_leak_absolute_*`
  (4 tests covering index, empty index, NotFound, put-on-existing).
- **Auto-applied `workspace` flag tag.** Every ref ingested via the
  prose-file handlers (markdown / plaintext / tex) is stamped with
  the `workspace` flag on ingest, with `set_by='system'`. Gives the
  LLM a simple filter for "things in my working directory":
  ```python
  search(q='kinetics', tags=['workspace'])   # scope to PRECIS_ROOT
  ```
  Refs outside `PRECIS_ROOT` (papers, web caches, memories, todos)
  stay un-stamped. The tag is registered in `flag_names` via
  migration `0008_workspace_flag.sql`. Idempotent:
  `ON CONFLICT DO NOTHING` means re-ingest doesn't duplicate rows.
  Pinned by 5 tests in `test_plaintext_handler.py::test_workspace_flag_*`
  (first-ingest, idempotent, survives mtime-bump, survives content
  change, `set_by='system'`).
- **New CLI: `precis jobs ingest`** — walks `PRECIS_ROOT` and
  pre-warms every prose-file kind into the store. The handlers are
  lazy by default (so the MCP handshake doesn't trip the ~7s
  bge-m3 load timeout); running this command before `precis serve`
  means the LLM finds every workspace file via `search` from the
  first query.
  ```bash
  precis jobs ingest                  # md + plaintext + tex
  precis jobs ingest --kinds tex      # scope to one kind
  precis jobs ingest --force          # re-embed even unchanged files
  precis jobs ingest && precis serve  # launcher-script prefix
  ```
  Mtime-gated (cheap re-runs), per-kind summary output. The old
  `ingest-md` command stays one release cycle as a deprecation
  shim that forwards to `ingest --kinds md`. Pinned by 8 tests in
  `tests/test_cli.py::test_ingest_*`.
- Skill docs updated: `precis-files-help`, `precis-overview`,
  `precis-edit-protocol`, `precis-plaintext-help`, `precis-tex-help`.
  README env var table simplified.
- **Migration**: rename `PRECIS_MARKDOWN_ROOT` (and / or
  `PRECIS_PLAINTEXT_ROOT` / `PRECIS_TEX_ROOT`) to `PRECIS_ROOT`.
  If you had separate trees, pick one shared parent — the kinds
  filter by extension, so they coexist cleanly.

### `tex` kind — section-aware LaTeX file handler (2026-05-02)

A new R/W file kind for `.tex` files with section-aware block
boundaries and a recursive `/toc` view that expands `\input{}` /
`\include{}` across files. Gated on `PRECIS_ROOT` (shared with
`markdown` and `plaintext`).

- **Section-aware block grammar** — `\part`, `\chapter`, `\section`,
  `\subsection`, `\subsubsection`, `\paragraph`, `\subparagraph`
  (and their starred forms with optional short titles) drive block
  boundaries alongside blank lines. The sectioning command line is
  always its own one-line block, so an agent can edit a heading
  without touching the body. Each block records `section_level` /
  `section_title` (when it's a heading) and `section_path` (its
  ancestor stack) in `meta`. Search-result rendering can show "hit
  in Methods > Kinetics" without re-parsing. Parser:
  `src/precis/utils/tex_parse.py`.
- **`/toc` view** — `get(kind='tex', id='main/toc')` walks the
  file's section blocks in source order and recursively expands
  `\input{path}` / `\include{path}`, inlining each child file's
  sections at the correct indent. Cycles (`a → b → a`) terminate
  with a `⇺` marker rather than recursing forever. `\input{}`
  targets are resolved relative to the parent file's directory and
  passed through the same `Path.resolve()` + `relative_to(self.root)`
  gate as every other read — paths that escape `PRECIS_ROOT` are
  reported as `not found`, not followed.
- **`PlaintextHandler` refactored** to make the parsing pipeline
  subclass-extensible:
    - `_KIND` / `_EXTENSIONS` / `_DEFAULT_EXT` ClassVars + the
      `spec` define kind identity.
    - `_parse_blocks(content)` / `_block_meta(block)` are
      overrideable hooks. The base implementation just delegates to
      `parse_plaintext` and stores `line_start` / `line_end`;
      `TexHandler` overrides both to inject section metadata.
    - `_SUPPORTED_VIEWS` + `_render_view(view, ref, slug=...)`
      dispatch any `view=` argument. Base supports `raw`; `TexHandler`
      adds `toc` by overriding the dispatcher.
    - `_walk_files()` now canonicalises every candidate via
      `Path.resolve()` + `relative_to(self.root)` so a symlink whose
      target lives outside the root no longer surfaces in the index
      (it would have failed at read time anyway; this just makes
      the listing match reachability).
- **Write-access boundary documented** — `precis-files-help` now
  spells out the `Path.resolve()` + `relative_to(PRECIS_ROOT)`
  contract that gates every read and write (rejects `../` traversal
  *and* symlink escapes). Pinned by new regression tests:
    - `test_symlink_inside_root_targeting_outside_is_invisible` —
      a symlink whose target escapes the root must not appear in
      the index and must `BadInput` on direct access.
    - `test_input_outside_root_silently_dropped` — same gate
      applied to `\input{../escape}` in the recursive TOC walker.
- New skill: `precis-tex-help.md`. Cross-references updated in
  `precis-files-help`, `precis-overview`, `precis-edit-protocol`,
  `precis-plaintext-help`.
- Pinned by:
    - `tests/test_tex_parse.py` — 17 tests for the parser
      (sectioning levels, ancestry stack, `\input{}` extraction,
      slug stability, line spans).
    - `tests/test_tex_handler.py` — 30 tests including ingest with
      section meta, `/toc` rendering, `\input{}` recursion, cycle
      detection, path-traversal escape attempts.
    - `tests/test_plaintext_handler.py` — 33 tests including the
      new symlink-escape regressions (apply to all prose-file
      kinds via the shared `_walk_files` / `_resolve_path`
      contract).
- **Deferred** (next refinement): macro expansion, environment
  grouping, `\subfile{}` package, `\cite{}` cross-link to `paper`
  kind.

### Cache-backed bookmarking + nightly maintenance (2026-05-02)

Closes the remaining two phases of `gripe:3681` and lands a
cron-driver to compose them into a daily-driver loop.

- **`get(kind=<cache>, ..., tags=['bookmark'])`** — one-call
  bookmark for every cache-backed kind (`web`, `youtube`, `math`,
  Perplexity tiers). Tags + untags pre-validated **before** the
  upstream fetch, so a bad axis no longer pays the API cost
  before failing. Apply on cache hit OR miss; idempotent.
  Owner: `src/precis/handlers/_cache_base.CacheBackedHandler.get`.
  Pinned by `tests/test_cache_base.py::test_get_with_tags_*`.
- **`get(kind=<cache>, ..., mode='refresh')`** — bypass cache
  freshness and re-fetch upstream **in place**, preserving
  ref id, slug, tags, and links. New
  `Store.update_cache_entry()` does an `UPDATE refs` + replace-
  blocks rather than the previous `DELETE FROM refs` cascade
  that destroyed annotations on every TTL-expired re-fetch.
  Stale-cache re-fetches now also flow through this path, so
  a `bookmark` survives expiry without `mode='refresh'` being
  explicitly requested. Per-kind `_recover_key(ref, cache)`
  hook lets a slug-only refresh call (e.g. from cron) re-derive
  the canonical fetch input from `cache.meta` — implemented for
  `web`, `youtube`, `math`, Perplexity tiers.
  Pinned by `tests/test_cache_base.py::test_mode_refresh_*` and
  `test_stale_cache_refetch_preserves_tags`.
- **`WATCH:<interval>` closed-axis tag** — closed vocabulary
  `{hourly, daily, weekly, monthly}` enforced via
  `Tag.parse_strict`. Allowed on cache-backed kinds only
  (`web`, `youtube`, `research`, `think`, `websearch`).
  `math` is intentionally excluded (Wolfram results don't
  drift). A typo (`WATCH:dialy`) fails loud at write time
  with the four valid intervals listed in `options=`.
- **`precis maintenance run`** — top-level CLI for the
  nightly cron. Three phases, each independently togglable:
  - **WATCH refresh sweep** — iterates
    `search(tags=['WATCH:<interval>'])` shortest-interval-
    first, calls `get(..., mode='refresh')` on each match
    whose `cache.fetched_at` exceeds the interval cutoff.
    Per-pass cap (`--max-refresh-per-pass=200`) bounds API
    spend; remaining work resumes on the next tick.
  - **Soft-delete purge** — hard-deletes `deleted_at`-
    tombstoned refs older than `--purge-after-days=30`
    (cascades blocks / cache_state / tags / links via the
    schema's `ON DELETE CASCADE`).
  - **`VACUUM ANALYZE`** on the hot tables (`refs`,
    `blocks`, `cache_state`, `ref_tags_*`, `ref_flags`,
    `ref_links`) so pgvector + tsvector planner stats stay
    fresh after large ingest passes.

  Suggested cron: `17 3 * * *  precis maintenance run` (one
  invocation per day; `--intervals=hourly` separately wired
  for an hourly tick if needed).
  Implementation: `src/precis/cli/maintenance.py`.

### Open backlog cleanup

- **`OPEN-ITEMS.md`** introduced as the canonical durable
  backlog. Replaces the per-issue gripe trail (gripes 3667 +
  3681 retired 2026-05-02 after the seven-verb surface
  refactor closed their original framing).
- **`docs/precis-bench/`** moved out of `openclaw-cluster` into
  its own package skeleton at `pips/packages/precis-bench/`.

## v6.0.0 — seven-verb surface, twenty-one kinds (2026-05-02)

First stable release of the v6 line. The package surface is now
**seven verbs** (`get`, `search`, `put`, `edit`, `delete`, `tag`,
`link`) discriminated by a single `kind=` argument, replacing the
v1 era's per-kind bespoke tools. Twenty-one kinds ship across ref /
tool / discovery categories; help skills are surfaced via
progressive disclosure (`get(kind='skill', id='precis-help')` for
the live registry dump, plus a `precis-<kind>-help` per kind).

Highlights since `5.2.6`:

- **Seven-verb tool surface** — see
  [`docs/user-facing/seven-verb-surface-migration.md`](docs/user-facing/seven-verb-surface-migration.md)
  for the design rationale.
- **New kinds**: `python` (AST navigator), `patent` (EPO OPS),
  `random` (discovery), `fc` (flashcards / spaced repetition),
  `oracle` (curated wisdom), `quest`, `conv`, `plaintext`,
  Perplexity tiers (`websearch` / `think` / `research`).
- **Anchored edits** (`mode='edit'`, `mode='insert'`) on every
  R/W file kind, with content-anchored resolution and `dry_run`.
- **Hybrid search** — lexical `tsvector` + semantic `pgvector`
  (`bge-m3`) RRF fusion at block level. Cross-kind fan-out via
  `kind='*'` or `kind='paper,memory'`.
- **In-tree handler registry** + entry-point plugin surface —
  third-party kinds register via `precis.handlers` group; one bad
  plugin cannot brick the server.
- **MCP critic-driven hardening** — multiple review passes pinned
  by regression tests; latest in
  [`docs/mcp-critic-review-2026-05-02.md`](docs/mcp-critic-review-2026-05-02.md).
- **Attribution footers** on every external-data handler
  (Wolfram, YouTube, Perplexity, web, EPO patents) — legal
  compliance, not UX polish.
- **Open backlog** moved to [`OPEN-ITEMS.md`](OPEN-ITEMS.md)
  (durable replacement for the per-issue gripe trail).

Migration from v5.x: there is none. v6 is a clean break — different
schema, different verbs, different config surface. Pin `<6` if you
need to stay on the v1 line. The README's *Install* and *Run*
sections cover the fresh-start path.

## `python` code-navigator kind (April 2026)

**New kind** `python` joins the file-handler family: a multi-root,
AST-indexed navigator over Python repos. Configured at startup via
`PRECIS_PYTHON_ROOTS=alias:/abs/path,…`; hidden when unset (same
gating pattern as `markdown`). No DB persistence — index lives
in-memory in a per-root `RepoCache` keyed by `(file, mtime_ns,
sha256)`. First `get` builds the index; subsequent calls only
reparse files whose mtime changed.

**Two-track addressing.** Line-range track `alias/path/file.py~L42-58`
is durable across edits but resolves to whatever symbol overlaps;
qualname track `alias::pkg.mod.Class.method` is content-addressable
and survives line shifts. `_parse_id()` accepts both. The repo
overview (`get(kind='python', id=alias)`) is the entry into either.

**Read views.**

- `toc` — package tree per repo, modules grouped by package depth
- `outline` — file-level outline with class hierarchy + signatures
- `source` — raw lines for a resolved region with header
- `entries` — pyproject `[project.scripts]` console scripts plus
  every `if __name__ == '__main__':` guard, each linked to its
  callable for the agent to drill into
- `callgraph` — entry-rooted static call tree built from a
  per-module call-edge index. Cycle detection (`[cycle]`),
  deduplication (`[see above]`), depth limit (`[truncated]`),
  multiplicity for repeated edges, `[ext]` for unresolved
  stdlib/third-party/dynamic. Cross-repo resolution via
  `cross_repo=True` (looks up unresolved callees in sibling
  configured roots).
- `runtrace` — *dynamic* overlay. Spawns the entry under
  `sys.setprofile` in a subprocess (gated by
  `PRECIS_PYTHON_ALLOW_EXEC=1`), forwards `argv`/`env`/`timeout`,
  reads the JSON trace back, builds a `TraceNode` tree with
  per-call timings + multiplicity, renders it in the same box-
  drawn style as the static `callgraph`, then appends a
  `Static-only` diff listing qualnames the static graph reaches
  but the runtime didn't. Useful for finding dead code,
  config-flag-gated paths, and import-time vs call-time wiring.

**Stdlib subtree collapse.** `runtrace` argparse-heavy entries blew
the response token budget (a single `--help` produced 5030 events
across `argparse.*` / `gettext.*` / `re.*` / `os.*`). The renderer
now folds anything whose top-level module is in
`sys.stdlib_module_names` into its root and annotates the row with
`(+N stdlib)` so the agent sees what was elided. Default
`max_events` lowered from 10000 → 2000. Both behaviours are
opt-out via `args={'expand_stdlib': True}` and tunable via
`args={'max_events': N}`. The `Static-only` diff is computed on the
*un-collapsed* event stream so it stays accurate regardless of
display.

**Search.** Lexical hybrid over the symbol index — qualname tokens,
docstring excerpt, signature. `scope=` accepts repo / package /
file. Each hit comes back as a canonical address you can drill
into directly. The single most token-efficient way to orient in an
unfamiliar repo: better than `grep` because it understands
intent (`q='where do we handle stale data'` works), better than
reading files cold because it returns symbols not bodies.

**Write surface.** `put` accepts `create` / `append` / `replace` /
`delete` plus the new anchored modes (`edit` / `insert`) inherited
from phase 8. Three validation gates run on every write:

1. `ast.parse` validates the result is syntactically valid Python
   (rolls back on `SyntaxError`).
2. **Qualname-drop gate** — extracts the set of qualnames defined
   in the modified region before and after; rejects any write that
   silently disappears a name unless `allow_rename=True` is passed.
   Catches accidental renames in anchored edits where the search
   region overlapped a `def` / `class` line.
3. `ruff check --fix` followed by `ruff format` — applies safe
   autofixes and reformats. Reports added/removed/modified line
   counts in the response. Skipped (with a warning header) if
   `ruff` is unavailable, never blocks the write.

After a successful write, the affected file's cache entry is
invalidated so the next `get` reindexes from disk.

**Skill.** `precis-python-help` (in `data/skills/`) documents the
addressing grammar, every view, the `args=` dict's accepted keys,
the write surface with all three gates, and a recipe section
(orienting in unknown repos, mapping stack traces, finding
dead code via the static-vs-runtime diff). The MCP `get` tool
gained an `args: dict[str, Any] | None = None` kwarg so view-
specific payloads (`{'entry': ..., 'depth': ...}` for callgraph;
`{'entry': ..., 'argv': [...], 'expand_stdlib': True}` for
runtrace) flow through. Reserved keys (`kind`/`id`/`view`/`q`)
inside `args=` are rejected at the boundary.

Pinned by `tests/test_python_indexer.py` (25 cases),
`test_python_cache.py` (9), `test_python_handler.py` (44 read-path),
`test_python_handler_writes.py` (37 + 12 anchored-edit), 
`test_python_callgraph.py` (17), `test_python_entries.py` (18),
`test_python_runtrace.py` (39, with real-subprocess end-to-end),
`test_python_config_wire.py` (18), and `test_mcp_args_kwarg.py`
(13). Full design rationale in `docs/user-facing/python-kind-spec.md`.

## Anchored edit protocol — v1 (April 2026)

**New write modes** `mode='edit'` and `mode='insert'` join the
existing four (`create`/`append`/`replace`/`delete`) on every R/W
file kind. v1 ships for `markdown` and `python`; the protocol
surface is already universal so other R/W kinds (`plaintext`,
`rmk`, `docx`, `tex`, `book`) inherit it when they ship.

**Why.** `mode='replace'` swaps a *whole resolved region* (a
markdown block, a python qualname, a line range). Right granularity
for structural edits, far too coarse for surgical changes — every
"the fox jumps over **the** fence" → "...over **a** fence" round-
trip rewrote the whole paragraph. Cost: tokens, risk (model has to
reproduce the unchanged tail verbatim), lost provenance. The new
modes resolve by *content*: literal `find=`, optional `before=`/
`after=` anchors, `match='unique|first|all|nth'` policy.

**Pure resolver.** `precis.utils.edit_resolve` is no-I/O and shared
across kinds. `EditOp` dataclass with construction-time validation;
`find_candidates` (literal + anchor filter, overlapping matches
seen); `select_candidates` with sharp `BadInput` errors:

- *not found*: error includes up to 3 fuzzy-nearest lines via
  sliding-window `SequenceMatcher` (typo aid like "dpoamine" →
  "dopamine") and a `next=` hint to widen the region.
- *ambiguous* (`match='unique'` + ≥2 hits): error lists every
  candidate's line + 80-char context plus disambiguation hints
  (`before=` / `after=` / `match='all'` / `match='nth'`).

`apply_edit` splices end-to-start so byte offsets stay valid across
multi-match replacements. No-op detection — `find` == `text` raises
rather than silently writing nothing.

**Markdown wiring.** `MarkdownHandler.put` accepts the new modes;
`_put_anchored` resolves the search region (whole file when no
selector, the addressed block's text when `id='slug~BLOCK'`), runs
`apply_edit`, splices back via `_replace_lines`, atomic-writes,
re-ingests. The block's content-derived slug may change after an
edit; the next `get` re-tokens.

**Python wiring.** `PythonHandler.put` routes the new modes
through the same `_finalize_write` pipeline `replace`/`delete` use,
so the three gates (`ast.parse` validation, qualname-drop
prevention, `ruff check --fix && ruff format`) all apply
automatically. The qualname-drop gate's `pre_in_region` set is
computed as "any symbol whose source overlaps any edited span,"
catching anchored renames of `def` / `class` lines unless
`allow_rename=True`.

**Skills.** New `precis-edit-protocol` (tier 2) documents the
universal grammar; `precis-markdown-help`, `precis-python-help`,
and `precis-files-help` gain "Surgical edits" / "Anchored edits"
sections. Skill discovery: protocol skill is loaded on demand via
cross-links from per-kind skills and from error-message footers.

**`dry_run` for anchored edits.** Both `mode='edit'` and
`mode='insert'` accept `dry_run=True | 'diff' | 'full'`. The same
resolver, splice, and validation gates run; the disk write and
re-ingest are skipped. The response carries:

- a structured header (region label, edited spans, match policy,
  per-gate pass/fail — `ast.parse`, `qualname-drop`, `ruff` for
  python; `re-parse`, `hunks within/outside` for markdown), and
- a body that's either a unified diff (`dry_run=True` / `'diff'`,
  the default) with standard `--- a/<label>` / `+++ b/<label>`
  headers and 3 lines of context, or the post-edit lines around
  each edited span with `> ` markers (`dry_run='full'`).

Rationale: the diff is the right default — it answers "did the
resolver pick the right candidate?", "will the result look
right?", and "did anything unexpected happen (e.g. ruff
autofix)?" in one token-efficient view. `'full'` is the escape
hatch for cases where the agent wants to see the post-edit region
in its natural form. Ruff's incidental changes are surfaced
separately as `outside-spans` hunks so the agent isn't surprised
by autofixes that touch lines it didn't address.

**Still deferred to v2.** Regex (`regex=True` + `flags=`), atomic
multi-edit batches (`edits=[…]`), `expect_lines=N`, explicit
cross-region rejection. The v1+`dry_run` surface is still
additive; future deferred features will not break existing calls.

Pinned by `tests/test_edit_resolve.py` (59 unit tests covering
`normalize_dry_run`, `format_unified_diff`, `classify_diff_hunks`,
`render_dry_run_header`, `render_dry_run_full`),
`tests/test_markdown_handler.py` (20 anchored-edit tests including
7 dry_run cases), `tests/test_python_handler_writes.py` (20 anchored
edit tests including 8 dry_run cases that verify gates run + disk
untouched + diff/full formats). Full design rationale in
`docs/user-facing/edit-protocol-spec.md`.

## bge-m3 char-truncation guard (April 2026)

`BgeM3Embedder.embed()` now truncates each input string to
`_BGE_M3_MAX_CHARS` (16,000) before handing it to
`sentence_transformers.SentenceTransformer.encode()`. Defends against
malformed blocks that escape upstream chunking — e.g. a corrupted-OCR
markdown table block of 192,633 chars that triggered a 73 GiB MPS-OOM
during ingest of `animeshjana2024recent`. The 1:1
`len(texts) == len(returned_vectors)` contract is preserved (truncation
is lossy on the suffix, never splits or drops blocks), so the store's
`blocks ↔ vectors` mapping is unchanged.

This complements the structure-aware `split_table` /
`enforce_hard_max` helpers added in `acatome-extract@0.6.2`.
Splitting belongs at the producer (where retrieval-meaningful chunk
boundaries can be preserved with header context); truncation belongs
at the consumer (where the hard MPS/CUDA constraint actually lives)
and protects every other handler that bypasses acatome-extract
(markdown, docx, tex, web, voice). Pinned by
`tests/test_embedder.py::TestBgeM3CharTruncation` (6 cases).

## MCP critique re-probe fixes (April 2026, second pass)

The critic re-probed after the first round and approved with one
remaining MAJOR (image-marker leak hadn't been wired into the search
preview) plus a modality-coverage observation: the server was
tools-only and silent on `prompts`, `resources`, `sampling`, and
`elicitation`.  Five tracks landed.

**MAJOR — figure markers no longer leak in search previews.**
The previous cut wired `_render_block_body`'s marker substitution
into `_render_chunks` (the get-with-`~N` path) but left the search
hit preview going through `_excerpt(block.text)` untreated.  Three
re-probed papers all returned raw `<span id="page-3-0"></span>![]
(_page_3_Figure_3.jpeg)` in their preview lines.  Centralised the
substitution in `_scrub_block_text` and applied it at both call
sites.  Bonus: dropped the asset path from `_render_block_body`'s
placeholder ("for diagnostics" still leaked the same `_page_*`
token).  Pinned by `test_paper_search_preview_strips_image_markers`
plus a stricter `test_figure_block_renders_structured_placeholder`.

**MAJOR — skills surface as MCP prompts.**
New `precis.mcp_modalities` module wires every skill that passes
`_availability_gap` into `prompts/list`, with the body served by
the same `_load_skill` / `SkillHandler.get` path the `get` verb
uses.  No skill text is duplicated.  Each prompt carries `tags=
{precis, skill, tier-N, kind:<X>, …}` so modern clients can
group / filter; we deliberately do **not** hide tier-2 ("power-
user") or draft skills — the menu is small (~16 entries) and the
reviewer asked for exposure, not curation.  `prompts/get` route
hits the synthesised `precis-help` and `precis-status` renderers
without any special-casing.  Six tests in `tests/test_mcp_modali
ties.py`.

**MAJOR — refs surface as MCP resources.**
Two surfaces, both DRY:
- `resources/list` enumerates only the bounded sets — skills
  today.  **Papers are deliberately not enumerated** (3000+ refs
  would blow client context and make `listChanged: true` near-
  useless).
- `resources/templates/list` advertises URI templates for the
  high-cardinality kinds: `precis://paper/{id}`, `precis://
  memory/{id}`, `precis://todo/{id}`, etc.  Modern clients use
  templates for autocomplete; concrete URIs are constructed by
  callers (typically from search hits).
Resource reads dispatch through `runtime.dispatch("get", ...)` so
there is no parallel rendering pipeline.  Numeric-ref kinds
str→int coerce in the read path.

**MAJOR — `precis-status` synth skill probes optional deps.**
The first-round CRITICAL (sentence-transformers missing from
`[paper]`) would have been caught by a health probe; this skill
is that probe.  `get(kind='skill', id='precis-status')` walks an
`_OPTIONAL_DEP_PROBES` table (10 entries: sentence-transformers,
sympy, wolframalpha, youtube-transcript-api, httpx, trafilatura,
python-docx, lxml, python-epo-ops-client, matplotlib), reports OK
/ MISSING / ERROR per probe with install hints, plus an "Overall:
OK | DEGRADED" summary and the live registered-kinds list.
Adding a probe is one row.  Refactor: replaced the single
`_SYNTHESIZED_SLUG = "precis-help"` with a `_SYNTHESIZED_SKILLS`
dict so future synth skills are a one-line addition.

**NIT — version vs status numbering bridge.**
Added a one-line block at the top of `precis-overview.md` mapping
`status: phase-N` (skill front-matter, build-phase markers) to
`serverInfo.version` (the canonical release marker).  Cheaper
than touching 19 front-matter blocks.

**Test count:** 1282 passed, 1 skipped (was 1271/1).

## MCP critique fixes (April 2026)

Five findings from the latest MCP critic pass — one CRITICAL plus
four MAJOR/MINOR. Five new regression tests in
`tests/test_mcp_critic_regressions.py`; full suite 1270 → 1271 green
(one previously-skipped paper-search test now runs because
sentence-transformers actually lands), lint + format + mypy clean.

**CRITICAL — `[paper]` extra didn't carry sentence-transformers.**
`BgeM3Embedder._ensure_loaded` raises with the hint `pip install
'precis-mcp[paper]'`, but the `[paper]` extra previously declared
only `acatome-extract>=0.1` (whose own `[embeddings]` extra is
where sentence-transformers lives). Result: a fresh
`pip install 'precis-mcp[paper]'` venv crashed with -32603 on every
search() against an embedder-backed kind, even though tools/list
advertised the verb as supported. The extra now declares
`acatome-extract[embeddings]>=0.1` and pins
`sentence-transformers>=3.0` directly so the install hint matches
reality. Verified by `pip install -e '.[all]' && python -c
'from sentence_transformers import SentenceTransformer'`.

**MAJOR — searched-kind annotation must surface on error paths.**
`_dispatch_inner` previously only prepended `(searched kind='X')` to
successful responses, so a search() that crashed inside the
defaulted kind's handler left the caller blind to which kind was
actually tried. The dispatcher now wraps the handler call: a
`PrecisError` has its `cause` annotated; non-Precis exceptions are
re-raised as `Internal` with the prefix already in the message. Both
branches verified by `test_search_default_kind_annotates_error_path`.

**MINOR — calc recovery hint uses `q=` to match canonical example.**
`precis-overview` and `precis-help` show `q='2+3*4'` everywhere for
tool-kinds. The calc handler still accepts both `id=` and `q=`, but
its `next=` trailers no longer teach `id=` — that was training
agents to mix kwargs and trip over the q= vs id= split elsewhere.
Two next-strings updated in `handlers/calc.py`; pinned by
`test_calc_recovery_hint_uses_q_kwarg`.

**MINOR — empty numeric-ref + quest searches grow Next: trailer.**
`memory`/`todo`/`gripe`/`fc`/`quest` empty searches used to return a
single line ("no memory entries match 'X'") with no recovery
affordance, while empty *list* responses on the same kinds carried
a clean Next: block. Asymmetry fixed in
`handlers/_numeric_ref.py::search` and `handlers/quest.py::search`:
empty searches now suggest a broader query, dropping any tag filter
(when one was applied), and the recent-list view. Pinned by two
new tests.

**MINOR — `view='fig/<N>'` is reserved, not a typo.**
`precis-paper-help` advertises `view='fig/<N>'` as a future-reserved
affordance. The handler used to lump it into the generic "unknown
view" Unsupported error, making a caller who'd read the help skill
assume the docs were wrong. `PaperHandler._render_view` now special-
cases `view.startswith('fig/')` and surfaces a deliberate "reserved
view" error citing `precis-paper-help` and the caption-only
workaround. Pinned by `test_paper_view_fig_n_is_reserved_not_unknown`.

**Deferred (not in this cut):** the critic also suggested a
registry-time embedder probe that downgrades `tools/list` when the
optional dep is genuinely absent (e.g. on a stateless deployment
that intentionally skipped `[paper]`). Worth doing for parity with
how the markdown / python kinds gate themselves on roots, but not
required now that the install path actually works. Queued.

## Planned — Phase 9: `patent` kind (EPO OPS)

Read-only third durable knowledge corpus alongside `paper` and
`markdown`. Spec at `docs/user-facing/patent-kind-spec.md`; agent skills at
`src/precis/data/skills/precis-patent-help.md` (entry-level) and
`precis-patent-power.md` (raw CQL); deferred filter affordances at
`docs/user-facing/search-future-filters.md`.

Phase-1 surface:
- `search(kind='patent', q=, tags=, scope=, top_k=)` — merged
  local + remote OPS hits, `[local]` markers, 7-day cache on the
  remote leg.
- `get(kind='patent', id=<docdb-slug>)` — fetch-as-ingest from
  OPS (biblio + description + claims), ST.36 XML parsed and
  embedded, raw XML mirrored on disk under
  `$PRECIS_PATENT_RAW_ROOT/<cc>/<num>/<kc>/`.
- `get(kind='patent', id='/recent' | '/published')` — list views
  (ingest time vs publication date).
- Tags use open lowercase prefixes (`cpc:`, `ipc:`, `applicant:`,
  `country:`, `kind:`, `family:`, `topic:`) lifted to OPS CQL on
  the remote leg via `_TAG_TO_CQL` translation table.
- Migration `0006_patent_kind.sql` registers the kind, two
  providers (`epo_ops`, `epo_ops_search`), and adds
  `"patent": frozenset({"SRC", "CACHE"})` to
  `store/types.py::_KIND_ALLOWED_AXES`.
- Required env: `EPO_OPS_CLIENT_KEY`, `EPO_OPS_CLIENT_SECRET`,
  `PRECIS_PATENT_RAW_ROOT`.

Phase 2 (separate cut): saved CQL watches table, runner, CLI,
launchd plist on balthazar.

Decisions captured this round:
- Search signature stays cross-kind uniform — no patent-specific
  kwargs (`ti=`/`ab=`/`pd=`/etc.). Power users put raw CQL in
  `q=`; the simple-vs-power-user split is two skill files.
- Date-range / state-marker / `source=local|remote|both` knobs
  captured in `docs/user-facing/search-future-filters.md` for cross-kind
  consistency rather than implemented patent-only.
- Slug normalisation strips whitespace only (no dots) so DOIs /
  arXiv ids in other kinds aren't undermined by precedent.

## MCP critic phase-8 — deferred items: axis enforcement, total_hits, inverse-rewrite, paper+research+conv+oracle crosslink

Five of the six items the previous CHANGELOG flagged as "deferred"
landed this session. **894 → 970 tests green, 1 skip** in this
package's own suite (74 new regression tests across
`tests/test_critic_phase8.py`, `tests/test_paper_research_crosslinking.py`,
and `tests/test_conv_oracle_crosslinking.py`, plus three test
files updated for the new axis discipline).

### Cross-linking on read-only kinds (paper, research, think, websearch, conv, oracle)

Six previously-immutable kinds gained a *narrow* link/tag put
surface this session. Body content stays read-only — paper
ingests still come from `.acatome` bundles, transcripts arrive
from the chat-bridge, oracle bodies are seeded externally,
Perplexity reports come from the API or paste-import — but
``link``/``unlink``/``tags``/``untags``/``rel`` are now first-
class on every one of them. The motivating use case was
"paper-A cites paper-B"; the same surface lets a research
report link back to its prompting paper, a conversation
record-link the todo it produced, and an oracle reference the
papers underlying its rubric.

New module `precis/handlers/_link_tag_ops.py` factors the
validation and store-call wiring out of `NumericRefHandler.put`:

- `validate_link_args(link, unlink, rel, kind)` — mutual exclusion
  + bare-`rel=` rejection.
- `validate_relation(rel) -> Relation` — registered-vocab check
  with `BadInput` on miss; defaults to `related-to` when omitted.
- `apply_link_ops(store, src_ref_id, ...) -> (n_added, n_removed)`
  — wraps `parse_link_target` + `add_link`/`remove_link`.
- `apply_tag_ops(store, kind, ref_id, ...) -> (n_added, n_removed)`
  — wraps `Tag.parse_strict` (kind-aware) + `add_tag`/`remove_tag`.
- `format_link_tag_ack(...)` — the one-line response renderer
  used by both new handlers, dropping zero-op segments.

`PaperHandler.put`:

- Accepts only ``link``/``unlink``/``tags``/``untags``/``rel``
  with ``id=<slug>``. Defaults are unchanged (`related-to`).
- Rejects ``text=`` with a hint pointing at the bundle ingest CLI.
- Rejects ``mode=`` because there's no body mutation surface.
- Rejects chunk selectors (`slug~46`) and path views
  (`slug/cite/bib`) — link/tag ops are ref-level only. Re-uses
  `_parse_paper_id` so the parser-side rejection is consistent
  with the read surface.
- Per-kind axis enforcement: `STATUS:` and `PRIO:` are rejected;
  `SRC:` and `CACHE:` are accepted (matches `_KIND_ALLOWED_AXES`).

`_PerplexityBase.put` (research / think / websearch) now
dispatches:

- `mode='import'` → existing $0 cache-import path, unchanged.
- `mode is None` + any of `link`/`unlink`/`tags`/`untags`/`rel`
  → new `_put_link_tag_ops` path. Resolves `id=` as a slug
  (NOT a query — query resolution would require re-hashing the
  canonical key and wouldn't cover slugs from direct ingest).
- `mode='import'` + link/tag kwargs → `BadInput`. Mixing the
  two surfaces is a misuse; the error suggests splitting into
  two calls.
- Anything else → `BadInput` with both options enumerated.

`OracleHandler.put` and `ConversationHandler.put` follow the
same template as paper: link/tag-only, ``text=``/``mode=``
rejected with hints pointing at the proper write path (corpus
seed pipeline for oracles, chat-bridge for conv). `conv`
additionally rejects chunk selectors (`slug~12`) and path
views (`slug/transcript`) since link/tag ops are ref-level.

Spec changes: `supports_put=True` on `paper`, `oracle`, `conv`.
The Perplexity trio kept `supports_put=True` (already there for
import mode); description and put-modes documentation updated
to mention link/tag.

Per-kind axis enforcement covers paper (`{SRC, CACHE}`), the
cache trio (`{CACHE}`), and conv/oracle (empty — open tags only);
`STATUS:`/`PRIO:` on any of these raises `BadInput` at the agent
boundary, matching the discipline added elsewhere this session.

`NumericRefHandler` was *not* refactored to use the new helpers
this pass — it works, and rewiring it for marginal DRY would
add regression risk. The shared module exists for paper + cache
+ any future read-only kinds that gain link/tag surfaces.

## MCP critic phase-8 — deferred items: axis enforcement, total_hits, inverse-rewrite

Three of the six items the previous CHANGELOG flagged as
"deferred" landed this session. **894 → 923 tests green, 1 skip**
(27 new regression tests in `tests/test_critic_phase8.py`,
plus three test files updated for the new axis discipline).

### Per-kind axis enforcement

The MCP critic flagged ``STATUS:open`` on a memory as a smell —
memory has no workflow state, so the tag is decorative and a
filter query (``search(kind='todo', tags=['STATUS:open'])``)
cannot find it. The validator accepted it anyway because closed-
prefix tags weren't gated on the kind.

Fix: ``Tag.parse_strict`` and ``Tag.normalize_filter`` accept
an optional ``kind=`` kwarg. When set, the parser checks
``_KIND_ALLOWED_AXES`` (a new map in ``store/types.py``) and
rejects closed-prefix tags whose axis isn't whitelisted for
that kind. The map is conservative:

- **Workflow kinds** (`todo`, `gripe`, `quest`) — `{STATUS, PRIO}`
- **Free-form notes** (`memory`) — `{}` (open tags only)
- **Flashcards** (`fc`), **conversations** (`conv`),
  **oracles**, **skills** — `{}` (no closed axes today)
- **Papers** — `{SRC, CACHE}` (primary/secondary lit + cache state)
- **Cache kinds** (`research`, `think`, `web`, `websearch`,
  `youtube`) — `{CACHE}`

Kinds absent from the map remain unrestricted (backwards-
compatible). Callers that don't know their kind at validation
time (filter queries that span kinds, migrations) pass
``kind=None`` and get the global vocabulary check unchanged.

Threading: every `_numeric_ref.search`/`put`, plus
`PaperHandler.search`, now passes `kind=self.kind` (or the
literal slug) into `Tag.normalize_filter`/`parse_strict`. Open
tags and bare flags are unaffected — the gate only fires on
closed-prefix tags.

Tests updated: `test_memory.py`'s closed-prefix tests moved to
todos (where the axis is real) or rewritten to exercise the
open-tag accumulation pattern. `test_search_tag_filter.py`'s
memory cases switched to open tags (`topic-co2-capture`).
`test_untags_on_put.py`'s closed-prefix value-match test moved
to TodoHandler. The new `test_critic_phase8.py::TestPerKindAxisEnforcement`
class pins the contract end-to-end.

### Auto-mirror inverse relations — read-side rewrite (not auto-insert)

The MCP critic flagged ``cites`` from A to B as not auto-
discoverable as ``cited-by`` from B to A — `links_for(B,
relation='cited-by', direction='out')` returned nothing because
the schema stores exactly one row per edge.

There were two ways to fix this:

- **Option A (auto-insert):** insert *two* rows on every
  asymmetric link. Pro: the outbound query "just works".
  Con: doubles ``direction='both'`` cardinality, contradicts
  the explicit one-row-per-edge design in
  `migrations/0005_link_relations.sql`, and creates a drift
  surface (the two halves of the edge can disagree if one is
  mutated independently).
- **Option B (read-side rewrite):** keep one row per edge,
  rewrite the relation filter at *query* time. The user
  picked B after I laid out the tradeoff.

Implementation: `Store.links_for` now consults
`_INVERSE_RELATIONS` (a Python mirror of `relations.inverse_slug`).
When the requested `relation=` has a registered inverse, the
WHERE clause becomes a disjunction:

- "literal-relation in the requested direction" OR
- "inverse-relation in the opposite direction"

So `links_for(B, relation='cited-by', direction='out')` matches
both literal `cited-by` rows where B is src (rare; only if
explicitly inserted) and `cites` rows where B is dst (the
canonical encoding of the same edge from the cited side).
Returned `Link` rows keep their *stored* relation slug — the
caller compares against the requested filter to label them,
exactly the same job the renderer already does for
`direction='both'`.

`add_link` is unchanged: still one row per edge. `remove_link`
likewise stays single-row — there's no shadow row to clean up.

The relations covered: every asymmetric pair listed in
`migrations/0001_initial.sql` and `0005_link_relations.sql`
(`blocks`/`blocked-by`, `contradicts`/`contradicted-by`,
`cites`/`cited-by`, `derived-from`/`derived-into`,
`supports`/`supported-by`, `generalises`/`specialises`).
Symmetric (`related-to`) and inverse-less (`see-also`) skip
the rewrite — they don't appear in `_INVERSE_RELATIONS`.

### `total_hits` header in search responses

Every search handler now emits a leading line of the form

    # 10 of 1234 paper matches for 'photocatalysis'

The MCP critic flagged the missing "of K" readout as a
pagination footgun: an agent that asks for `top_k=10` and gets
exactly 10 hits couldn't tell whether it had everything or
just the first page. Now the header makes the cardinality
explicit.

Implementation:

- New `Store.count_refs_lexical(q, kind, tags)` and
  `Store.count_blocks_lexical(q, kind, scope_ref_id, tags)` —
  same WHERE clauses as their `search_*_lexical` companions
  (including the noise-floor `char_length(btrim(text)) >= 4`
  guard from phase 7), no LIMIT. Two queries per search call;
  on TSV-indexed columns the COUNT is sub-millisecond.
- New helper `precis.utils.search_header.format_search_headline`
  centralises the header wording. The "N of K" suffix is
  suppressed when `total <= n_returned` (no value in saying
  "10 of 10") and when `total is None` (semantic-only search,
  where every embedded block is a hit at some distance).
  Pluralisation handles `-ch`/`-sh`/`-s`/`-x`/`-z` endings so
  `paper match` → `paper matches` rather than `matchs`.
- Wired into every search handler: `_numeric_ref.py`,
  `paper.py`, `markdown.py`, `oracle.py`, `quest.py`,
  `conversation.py`, `skill.py`, `python.py`. The Python and
  Skill handlers compute total in-process (`total = len(hits)`
  before the `top_k` slice).

For RRF-fused searches (`paper`, `markdown`, `conv`), the
total is the *lexical* count — RRF only re-ranks lexically-
matching rows, so the lexical universe is the meaningful "K".
The handler comment notes this; the docstring on
`count_blocks_lexical` does too.

### Brief: why I'm not adding `prompts/list` and `meta/registry`

Both are optional MCP protocol extensions. `prompts/list`
advertises named prompt templates — our skill system already
covers this via `get(kind='skill')` and would just duplicate
the surface in a more rigid shape. `meta/registry` exposes the
tool/kind schema as structured data; today agents discover
kinds via `precis-overview`, and every frontier model handles
the prose form. Skipping both keeps us flexible against the
MCP spec evolving.

### Out of scope this session

Still queued from the phase-7 deferred list:

- **Relevance floor on rank scores.** Soft threshold beyond
  the noise-floor filter. Needs empirical per-kind tuning.
- (Done — see the cross-linking entry above.) Paper, research,
  think, websearch, conv, and oracle all gained the link/tag put
  surface this session.

## MCP critic phase-7 follow-up — schema, skill index, search hardening

The second MCP critic pass (Apr 2026) found 3 CRITICAL, 9 MAJOR,
and 6 MINOR issues against the live phase-7 build. **862 → 894
tests green, 1 skip** (32 new regression tests in
`tests/test_mcp_critic_phase7.py`).

### CRITICAL #1 — `rel=` / `unlink=` / `untags=` weren't on the MCP `put` schema

The handler layer landed these kwargs in the previous pass, but the
FastMCP tool wrapper in `precis/server.py` only forwarded
`{kind, id, text, mode, tags, link}`. `rel='cites'` was silently
discarded; every linked claim collapsed to `related-to`.

Fix: tool schema now declares `untags`, `unlink`, `rel`. Tool
description rewritten to drop the retired colon-suffix shortcut
("`'target_slug:relation'`") and document the canonical
`'kind:identifier[~selector]' + rel='…'` form. Mode hint clarified
to name only `delete` (the only widely-supported mode on numeric
kinds) plus the file-kind list.

### CRITICAL #2 — unknown `mode=` values silently no-opped

`mode='untag'` and `mode='unlink'` returned `updated memory id=N`
without doing anything — the handler's `if mode == 'delete'` branch
was followed by the regular update path, which simply ignored the
unknown mode. The critic identified this as the worst possible
failure mode for an agent loop (silent state divergence on undo).

Fix: `NumericRefHandler.put` now validates `mode=` against
`_SUPPORTED_PUT_MODES = ("delete",)` up front. Anything else
(`'note'`, `'untag'`, `'unlink'`, typos like `'deelete'`) raises
`BadInput` with the supported list and a hint pointing at the
correct kwargs (`untags=`, `unlink=`, `mode='delete'`).

### CRITICAL #3 — skill index advertised skills for unregistered kinds

`get(kind='skill')` listed 17 skills as authoritative; 3 of them
(`precis-markdown-help`, `precis-python-help`, `precis-files-help`)
documented kinds the registry rejected. Following any of them
produced `NotFound` cascades on the agent's first call.

Fix: `SkillHandler` now filters the index via `_availability_gap()`,
which checks two gates per skill:

- **Subject-kind gate.** Slugs of the form `precis-<kind>-help`
  with `<kind>` not in `registry.kinds()` are filtered.
- **Status gate.** Front-matter `status: planned` /
  `status: aspirational` filters the skill regardless of name.

Filtered skills remain **retrievable by exact slug** with a
`> **Heads up:**` banner explaining why they're hidden — the docs
stay accessible if you know what you're looking for, they just
don't pollute discovery.

The trailer at the bottom of the index also swapped its second
suggestion from `precis-navigation` (now aspirational) to
`precis-tags` (active).

### MAJOR #4 — `precis-cache` documented `kind='ask'` that doesn't exist

`ask` was never wired in this build. The skill listed it as one
of four cached kinds with a TTL table that was wrong on every
row.

Fix: rewrote `precis-cache.md` against the live handlers. The
TTL table now reads from the canonical source (`math` pinned,
`web` 7d, `websearch` 7d, `think` 30d, `research` pinned,
`youtube` 30d) with a note that the handler classes are the
single source of truth. Every example uses a live kind. Same
fix in `precis-memory-help.md` (`kind='ask'` → `kind='research'`)
and `precis-navigation.md` (which was already gated by status:
aspirational, see #7).

### MAJOR #6 + #7 — `precis-density` and `precis-navigation` are aspirational

Neither skill describes the live runtime: `precis-density`
documents `view='representatives'/'echoes'/'coverage'` and a
`DENSITY:*` tag prefix, none of which exist; `precis-navigation`
contains 9 recipes, 8 of which contain at least one
non-functional call.

Fix: front-matter `status: planned` (density) / `aspirational`
(navigation), with explicit "Heads up" banners enumerating the
runtime mismatches. The skill-index filter then hides them from
discovery, as does `precis-files-help` (which documents file
kinds queued for a later phase).

### MAJOR #8 — unregistered `UPPERCASE:` tag prefixes were silently accepted

`Tag.parse_strict` validated values inside *registered* prefixes
(`STATUS:bogus` rejected) but let unregistered prefixes pass
through unchecked. So `DENSITY:sparse` and `CONFIDENCE:moderate` —
both documented in `precis-tags.md` as "would be rejected" — were
accepted at runtime and silently joined the corpus. Same for
typos like `STATSU:open`.

Fix: any uppercase prefix not in `_CLOSED_VOCAB` raises `BadInput`
with the registered axis list and a recovery hint (use a
registered axis, or write as a lowercase open tag like
`density-sparse`). This catches typos too — `STATSU:open` no
longer survives into queries silently. Two memory tests that
relied on the old behaviour were updated to use `PRIO:` (a real
registered axis); the `confidence-*` examples in
`precis-memory-help.md` were rewritten as open tags with the
``untags=`` removal idiom for swapping.

### MAJOR #9 + MINOR m1 — paper default overview leaked `<jats:*>` XML

`view='abstract'` stripped JATS namespace tags, but the default
`get(kind='paper', id=…)` overview rendered the abstract verbatim.
Worse, even `view='abstract'` produced
`AbstractMetal–organic frameworks…` (heading word fused with the
body's first word).

Fix: `_render_overview` now calls the same `_strip_jats` helper
the abstract view uses, so both paths produce identical clean
output. `_strip_jats` was also rewritten:

- `<jats:title>Abstract</jats:title>` is dropped outright (the
  view name and rendering context already name the section, and
  the heading word was the only thing causing the mash).
- Closing tags get a single space rather than empty-string, so
  adjacent paragraphs don't fuse word-to-word.
- Whitespace runs collapsed to keep paragraph structure clean.

Two new tests pin both the no-mash and the no-`<jats:` invariants.

### MAJOR #10 — `top_k` was unbounded; `top_k=9999` returned 7 326 hits

`search(kind='paper', q='photocatalytic', top_k=9999)` returned
~2.7 MB in a single response, large enough to exhaust a 7B
model's context window in one call. The schema didn't document
a maximum.

Fix: `precis/server.py` enforces `top_k ∈ [1, 100]` at the MCP
boundary (the `_SEARCH_TOP_K_MAX` constant is exported and pinned
by a regression test). Larger values raise `BadInput` with the
cap value and a hint to narrow with `scope=` or paginate. The
internal-caller path (tests, SDK consumers calling `dispatch`
directly) is unchanged — the cap is the agent-facing-only
guardrail.

### MAJOR #11 — search returned punctuation-only blocks as hits

Adversarial queries (`q='🚀💩'`, `q='xyzzy_definitely_no_match_…'`)
returned 10 hits dominated by punctuation-only chunks (".", ",",
"kinetics."). These are formatting artefacts whose embeddings
cluster near the noise floor; cosine similarity puts any query
close to them.

Fix: every block-search method (`search_blocks_lexical`,
`search_blocks_semantic`, `search_blocks_fused`) adds
`char_length(btrim(b.text)) >= 4` to its WHERE clause. Blocks
shorter than 4 stripped chars don't appear in any search output —
they're not deleted (so direct chunk reads via `id=slug~N` still
work), just filtered from search results.

A relevance floor on rank scores would land in a follow-up; the
current fix removes the lowest-quality hits without needing
empirical threshold tuning.

### MAJOR #12 — cross-kind error options listed kinds that don't support search

`search(kind='all', q='…')` raised `NotFound: unknown kind: all`
with `options=` containing **every** active kind, including
`calc`, `math`, `web`, `websearch`, `think`, `research`, and
`youtube` — all of which support `get` only. Agents retrying
against the suggested options hit a second `Unsupported` error.

Fix: the runtime catches the registry's `NotFound` in
`_dispatch_inner` and re-raises with `options=
self._kinds_for_verb(verb)` — the verb-filtered subset. The
comma-list catch-all does the same now. Same one-line shape as
the no-kind path that was already correct.

### MAJOR #13 — `mode='note'` was advertised but no kind accepted it

The tool description listed `'note'` as a supported mode; the
critic checked `precis-memory-help`'s prose and found it
referenced `mode='note'` but never called it. No handler
implements it.

Fix: `_SUPPORTED_PUT_MODES` (numeric kinds) is now strictly
`("delete",)` and the tool description names only the modes
that have working handlers. The reference in
`precis-memory-help.md` was rewritten to point at the real
pattern (create memory + `link=` back to the ref with a
specific `rel=`).

### MINOR m2 — empty-list responses on read-only kinds had no Next: trailers

`oracle`, `conv`, and `quest` empty-list views returned bare
strings ("no oracles defined yet") with no recovery hint — the
critic flagged this as a consistency violation against `gripe`
and `fc` which already emitted trailers.

Fix: each empty-list path now emits a one-line `Next:` block
with a concrete next-call shape. Three small handler edits.

### MINOR m3 — paper-id underscore returned `BadInput: unparseable`

7B models reflexively use snake_case. `get(kind='paper', id='nonexistent_paper_xyz')`
raised `BadInput` instead of either `NotFound` (slug doesn't exist)
or a clear "underscore is illegal" message. The slug regex's
permissive prefix-match meant the trailing `_paper_xyz` ended up
in the chunk-selector branch and produced a generic error.

Fix: post-match check that catches non-`~`/non-`/` rest. If the
first leftover char is `_`, the error names the rule
("underscores not allowed; slugs match `[a-z0-9-]+`"); for any
other illegal char, the error names that char specifically.

### MINOR m4 — `calc` echoed gibberish identifiers as symbolic expressions

`calc(id='malformed**broken')` returned `malformed**broken =
malformed**broken` because sympy parsed the identifiers as free
symbols and `simplify(symbol**symbol)` is itself. No signal to
the agent that the input wasn't a math expression.

Fix: when the simplified result is identical to the input string
**and** contains free symbols, raise
`BadInput: expression simplifies to itself` with a hint pointing
at concrete patterns (`solve(Eq(…))`, `integrate(…)`). Pure
numerics (`2+3*4`) and real symbolic calculus
(`integrate(sin(x), x)`) still work — the heuristic only fires on
the "no operator simplified anything" case.

### MINOR m6 — single-block trailers rendered as `~N..N`

The chunk-range trailer formatter emitted `~77..77` for a
degenerate single-block range, training agents to write
`~5..5` for unrelated singletons later. The canonical
single-block form is `~N`.

Fix: trailer collapses `lo == hi` to `~N` with the label
`"next chunk"` (vs `"next chunk range"` for true ranges).

### Out of scope / deferred

These are real findings I haven't shipped yet:

- **Relevance floor on rank scores (MAJOR #11a).** The noise-floor
  filter handles the most egregious case (punctuation-only
  blocks); a soft threshold on rank scores still wants empirical
  tuning per kind.
- **`total_hits` in search responses (MAJOR #10b).** Pagination
  intent needs a header field that says "you're seeing N of K".
  Not landed yet — would require changes to every search
  handler's render.
- **Cross-kind link CRUD on file-/cache-backed handlers
  (MAJOR #4 follow-up).** Numeric refs accept `link=` /
  `unlink=` / `rel=`; paper / markdown / python / cache kinds
  don't yet.
- **Auto-mirroring inverse relations.** `cites` from A to B is
  not automatically inserted as `cited-by` from B to A. The
  renderer shows both directions correctly via two queries, but
  a "who cites me?" filter still requires `direction='in'`.
- **`prompts/list` and `meta/registry` resource.** The critic
  recommended these as MCP-spec extensions; not in scope this
  pass.
- **Per-kind axis enforcement.** The critic noted `STATUS:open`
  on a memory is a smell (memories don't have a STATUS axis),
  but per-kind axis restriction is a bigger feature than the
  CRITICAL/MAJOR items here. Still queued.

## Link CRUD — `link=` / `unlink=` / `rel=` end to end

The previous critic pass flagged `link=` as silently no-op
(`if link is not None: pass  # links CRUD lands later`). That
placeholder is now replaced with a working end-to-end implementation
on every numeric-ref kind (`memory`, `todo`, `gripe`, `fc`, `quest`,
`conv`). **819 → 861 tests green, 1 skip** (42 new link CRUD tests).

### Migration: full relations vocabulary

`migrations/0005_link_relations.sql` seeds the relations the docs
have always promised but the schema never carried:

- Citation graph: `cites` / `cited-by`.
- Provenance: `derived-from` / `derived-into`.
- Evidential support: `supports` / `supported-by`.
- Generalisation: `generalises` / `specialises`.
- Asymmetric pointer: `see-also` (no inverse).

Each row has its `inverse_slug` populated for the renderer's
direction-aware labelling. **Inverse handling stays app-level** —
adding a `cites` link does *not* auto-mirror as `cited-by`. The
`view='links'` renderer queries both directions and shows the
relation as stored, with arrows for direction.

The `Relation` typing literal in `precis/store/types.py` was
extended to mirror the seed exactly. A regression test
(`TestRelationsVocabularyMatchesSchema`) pins that every literal
slug exists in the seeded table, so future drift between Python
and SQL fails loudly at test time rather than silently at INSERT.

### Canonical syntax: `kind:identifier[~selector]` + separate `rel=`

The agent-facing skill docs were inconsistent: `precis-todo-help`
said `link='target_id:relation'` was the only syntax and there was
no `rel=` parameter; `precis-relations` and `precis-memory-help`
showed the opposite. Both forms had cases where they were ambiguous
(numeric ids vs slugs vs relation suffixes all sharing `:`).

Settled on the strict form:

```python
put(kind='memory', id=47,
    link='paper:wang2020state~38',
    rel='cites')

put(kind='memory', id=47, unlink='paper:wang2020state', rel='cites')
get(kind='memory', id=47, view='links')
```

- **Kind prefix is mandatory** — no bare slugs, no implicit fallback
  to the source kind.
- **`rel=` is a separate kwarg** — no overloaded colon-suffix.
- **Block selector** unchanged: `~pos` (numeric) or `~slug`.

The kwarg parsing rejects two genuine misuses up front:
- `link=` and `unlink=` together → `BadInput` ("mutually exclusive").
- `rel=` without `link=`/`unlink=` → `BadInput` ("rel= requires
  link= or unlink=") — silent swallowing here would let typos
  vanish.

### Target parser

New module `precis/handlers/_link_target.py`:

```python
@dataclass(frozen=True, slots=True)
class LinkTarget:
    ref_id: int
    pos: int | None
    kind: str
    raw: str

def parse_link_target(target: str, *, store: Store) -> LinkTarget: ...
```

The parser hits the store to resolve the target *before* any
mutation, so a `link='paper:does-not-exist'` on a put-create is
rejected up-front and doesn't leave a half-created memory row in
the corpus (regression test
`test_bad_link_target_rejected_before_create` pins this).

Validation paths:
- Missing `:` prefix → `BadInput` with canonical-form hint.
- Empty kind / identifier / selector → `BadInput`.
- Unknown kind → `BadInput` with the full options list (queried
  from the live `kinds` table, so file-kinds added in 0004 show
  up automatically).
- Numeric kind with non-numeric id → `BadInput`.
- Slug kind ref not found → `NotFound`.
- Block pos out of range / block slug missing → `NotFound` with
  hint to `get(kind=…, id=…)` for the block list.
- Negative pos → `BadInput` (the runtime sentinel `-1` is
  internal-only).

### Store CRUD

Three new methods on `Store`:

- `add_link(...)` — idempotent on the unique tuple via
  `ON CONFLICT (...) DO UPDATE SET set_by = links.set_by RETURNING *`.
  Schema-level `CHECK (NOT (src_ref_id = dst_ref_id AND src_pos =
  dst_pos))` is mirrored as a `BadInput` at the runtime boundary
  (sharper hint than the DB error). Same-ref different-pos links
  are allowed (block~5 → block~7 within one ref).
- `remove_link(...)` — `relation=None` removes all rows between
  the (src, src_pos, dst, dst_pos) tuple regardless of relation;
  the handler-level `unlink=` without `rel=` uses this. Returns
  rowcount; missing rows are silent no-ops.
- `links_for(ref_id, *, direction=..., relation=...)` — `out` /
  `in` / `both`, with optional relation filter. Used by the
  `view='links'` renderer.

`_row_to_link` maps the DB sentinel `pos = -1` back to Python
`None` at the boundary so callers always see "ref-level" as
Pythonic absence.

### Handler integration

`NumericRefHandler.put` accepts `link=`, `unlink=`, `rel=` on
both create and update paths. `_validate_relation` provides the
canonical-form check at the agent boundary, raising `BadInput`
with the full options list — sharper than the FK violation the
DB would otherwise raise.

`NumericRefHandler.get(view='links')` renders both directions:

```
# memory 42 — links

## outbound
→ paper:wang2020state~38  (cites)
→ memory:7  (related-to)

## inbound
← memory:55  (derived-from)
```

Endpoints are bulk-fetched with one round trip
(`SELECT … WHERE id = ANY(%s)`) so the render is O(N) on link
count, not O(N) on `link × DB-query`. Soft-deleted targets render
with a `(deleted)` marker rather than vanishing — agents need to
know when a link points at a tombstoned ref.

`view=` is now eagerly validated against the per-handler views
tuple (currently just `('links',)` on numeric kinds). Subclasses
with extra views override `get()` in the usual way.

### Documentation reconciliation

- `precis-relations`: rewritten as the canonical reference. Full
  vocabulary table with inverses, validation-error catalogue,
  semantics notes (idempotent, position-aware, kind-agnostic,
  inverse-as-documentation).
- `precis-todo-help`: `link='158:blocked-by'` → `link='todo:158'`,
  `rel='blocked-by'`. The "no `untags=`" line — outdated since
  last session — was replaced with a pointer to `precis-tags`.
  The "tag-filter not yet implemented" disclaimer was replaced
  with a working example.
- `precis-memory-help`: bare-slug examples → `paper:`/`research:`
  prefixed.
- `precis-navigation`: same.
- `precis-markdown-help`: already canonical (`markdown:notes/x.md`),
  no change.

### Tests

`tests/test_link_crud.py` — 42 tests across:

- **`TestParseLinkTarget`** (14): all syntax forms, rejection
  paths, missing kind/ref/block, negative pos, empty selector.
- **`TestStoreLinkCRUD`** (10): basic add, idempotency, multi-
  relation rows, ref-level self-loop rejection, block-level
  self-loop allowed, specific-relation removal, broad removal,
  no-op missing removal, direction filter, relation filter.
- **`TestRelationsVocabularyMatchesSchema`** (1): the
  Python literal mirrors the seeded SQL.
- **`TestMemoryHandlerLink`** (11): create-time link, explicit
  rel, block target, update-time link, unlink with rel, unlink
  without rel (broad), mutual-exclusion check, rel-without-
  link/unlink rejection, unlink-on-create rejection, unknown
  relation, atomic rejection of bad target before insert.
- **`TestMemoryHandlerLinksView`** (6): no-links hint, outbound
  arrow, inbound arrow, block-pos rendering, unknown view
  rejection, deleted-target marker.

### Out of scope this pass (still queued)

- **Block-level link source positions on numeric refs.** The
  schema and target parser support `src_pos`, but the handler
  surface only writes ref-level (`src_pos = None`) sources. Memory
  has no block subdivision today; the moment a numeric kind grows
  blocks, this becomes interesting.
- **Cross-handler link CRUD.** PaperHandler / MarkdownHandler /
  PythonHandler don't accept `link=` yet — only the numeric refs
  do. The schema doesn't care about source kind, but the
  agent-facing kwargs need to be wired per handler.
- **Symmetric-relation auto-mirroring.** `cites` from A to B is
  *not* auto-inserted as `cited-by` from B to A. The renderer
  shows both directions correctly, but a query like "who cites
  me?" still requires `direction='in'`. Auto-mirroring tempts
  consistency bugs (which side is canonical?); leaving it manual
  for now.
- **Bulk link operations.** `link=` accepts one target per call.
  Multi-link should land if a real workflow needs it; for now
  iteration on the agent side is enough.

## MCP critic follow-up — TOC heuristic + tag filter + untags

Closing the deferred items from the previous critic pass.
**730 → 819 tests green, 1 skip** (53 TOC-rejection tests, 24
tag-filter tests, 12 untags tests).

### Paper TOC — reject metadata as headings

The hierarchical TOC heuristic (`_paper_toc.detect_heading`) was
treating publisher metadata blocks (`**DOI: 10.1002/...**`,
`**Keywords: ...**`, `**Received: 12 Mar 2024**`) as H2 headings,
because they're bold-only single-line blocks. The MCP critic
flagged a paper where 357 of 460 blocks landed under a single
``DOI:…`` pseudo-heading, making the hierarchical TOC useless.

`detect_heading` now applies an anti-pattern filter:

- `_METADATA_PHRASE_RE` — always-metadata phrases (`©`, `Copyright`,
  `License`, `Funding`, `Article history`, `Supplementary`,
  `Conflict of interest`, `Available online`, `Cite this article`).
  Match anywhere → reject.
- `_METADATA_LEAD_RE` ∧ `_METADATA_SHAPE_RE` — conditional-metadata
  leads (`DOI`, `Keywords`, `Authors`, `Affiliations`, `Received`,
  `Accepted`, `Published`, `Corresponding`, `Email`, `ORCID`,
  `Cite this`, `Submitted`, `Revised`) only count as metadata when
  the title also carries a metadata-shape signal (`:`, em-dash,
  en-dash, `@`, or any digit). This keeps a real subsection title
  like "DOI tracking subsection" — no colon, no digit — from being
  false-flagged.
- DOI strings (`10.NNNN/...`) and URLs (`https?://`) anywhere in the
  title → reject.
- Title length > 60 chars → reject (real subsection titles are short).

The filter applies uniformly to H1, H2, MD-H1, and MD-H2 paths so a
``■ **DOI: 10.x/y**`` artefact also gets caught even though it
carries the H1 marker. **This is v2-only** — v1 (`precis-mcp`)
indexed from typed nodes that already carried heading level, so no
fix is owed there.

### `tags=` filter on search — DRY at the SQL layer

The schema already had a unified `ref_tags` view over the three
narrow tag tables (`ref_closed_tags`, `ref_flags`, `ref_open_tags`),
indexed for prefix-and-value lookups. The "missing piece" was a
single helper that emits a SQL fragment usable by every store
query that selects refs.

New module `precis/store/_tag_filter.py`:

```python
def build_tag_filter(
    tags: list[str] | None,
    *,
    ref_alias: str = "r",
    block_level: bool = False,
) -> tuple[str, list[Any]]:
    """Build the SQL AND fragment + params for a tags filter."""
```

Returns `("", [])` for `None`/`[]` so callers splice
unconditionally. The fragment uses `r.id IN (SELECT ref_id FROM
ref_tags WHERE tag IN (...) AND pos IS NULL GROUP BY ref_id HAVING
COUNT(DISTINCT tag) = N)` — AND semantics across all tags through a
single subquery, so the planner narrows on the indexed prefix-value
columns before the lexical/semantic ranking runs.

Wired into six store methods:

- `list_refs(tags=...)` — list view filter.
- `count_refs(tags=...)` — paginated headers.
- `search_refs_lexical(tags=...)` — title search.
- `search_blocks_lexical(tags=...)` — block lex search.
- `search_blocks_semantic(tags=...)` — pgvector cosine search.
- `search_blocks_fused(tags=...)` — RRF, applied to **both** CTEs
  (a regression test pins this — fusing a filtered against an
  unfiltered set defeats the filter).

`list_refs` and `count_refs` were also re-aliased to `r` for
consistency with the rest of the search surface; this is a private
breaking change but everything internal to the package was updated
in the same edit.

### Validation: `Tag.normalize_filter`

New classmethod on `Tag` that runs `parse_strict` over each tag and
returns canonical-form strings. Same rejection set as `put(tags=)`:
unknown closed-vocab values (`STATUS:bogus`) and bare flags that
collide with a closed value (`'urgent'`) raise `BadInput` with the
canonical alternative in the next: hint.

Surfaced in:

- `PaperHandler.search(tags=...)` — block search with topic filter.
- `NumericRefHandler.search(tags=...)` — covers memory, todo,
  gripe, fc, conv, quest by inheritance.

Empty result responses on numeric-ref kinds now mention the active
filter in the body so an agent that wrote `tags=['topic-typo']`
sees why the search returned nothing.

### `untags=` parameter on put

Closed-prefix overwrite already handled "switch STATUS to done"
via `tags=['STATUS:done']`, but there was no path to *remove* a
lowercase or bare tag once set. Added on `NumericRefHandler.put`:

```python
put(kind='memory', id=48, untags=[
    'topic:co2-capture',  # remove this lowercase tag
    'star',               # clear the flag
    'STATUS:done',        # remove only if STATUS is currently 'done'
])
```

- **Value-matched** for closed prefixes (`STATUS:open` against a
  `STATUS:done` ref is a silent no-op — same shape as a SQL
  DELETE finding zero rows).
- **Idempotent** — removing a tag that isn't there is a no-op.
- **`STATUS:` empty form rejected** at parse time, so removing
  "any STATUS regardless of value" is impossible by accident.
- **Rejected on create** (`id=None`) — there's nothing to remove
  from yet; raises `BadInput`.
- Same `Tag.parse_strict` validation as `tags=` — bare-flag
  collisions and unknown closed-vocab values raise the same
  `BadInput` shape they do on the write path.
- The "at least one of" check on update was widened to accept
  `untags=` alone as a sufficient update.

Sub-class `_remove_tags(ref_id, untags)` mirrors `_apply_tags`, so
every NumericRef kind picks up the parameter without per-class
wiring.

### Documentation

- `precis-tags`:
  - "Remove tags" section added with value-matched semantics.
  - "Filter by tags" section added with AND semantics, perf note,
    validation-error reference.
  - `applies-to` updated to `put (tags=, untags=) and search (tags=)`.
  - "Not yet implemented" pruned to the genuinely-deferred items
    (list-view-level filter exposure; block-level positional
    tag filter).

### Tests

- `tests/test_paper_toc_metadata_rejection.py` — 53 tests covering
  the metadata rejection vocabulary, length cap, DOI/URL anywhere,
  H1/H2/MD-H1/MD-H2 paths, the synthetic 357-of-460 regression,
  and the metadata-only-paper "no fake headings" check.
- `tests/test_search_tag_filter.py` — 24 tests across
  `build_tag_filter` unit tests, store-level (`list_refs`,
  `count_refs`, `search_refs_lexical`, `search_blocks_lexical/
  semantic/fused`), handler-level validation, and the ref-vs-block
  pos-boundary regression.
- `tests/test_untags_on_put.py` — 12 tests across removal flows,
  closed-prefix value matching, idempotency, validation symmetry
  with `tags=`, and the create-path rejection.

### Out of scope this pass (still queued)

- **Cross-kind / comma-list search.** Real feature, runtime
  rejects with a precise hint today.
- **Slug dedupe one-shot.** It's an `.acatome` bundle-file
  workflow, not a runtime fix; tracked for the bundle-repo
  session.
- **Block-level (positional) tag filtering** end-to-end. The
  helper has the `block_level=True` path and the schema supports
  it, but no handler writes block-level tags or surfaces a kwarg
  to filter on them. Picked up when a real consumer needs it.
- **`tags=` on agent-facing `get(kind=K)` list views.** Already
  exposed at `Store.list_refs(tags=...)`; piping through the list
  view path is a small extension once the right handler surface
  decides which list-views accept which kwargs.

## MCP critic pass — surface honesty + tag validation

Tightened the agent-facing surface in response to the April 2026
MCP critic findings (2 CRITICAL, 11 MAJOR, 4 MINOR). Documentation
no longer over-promises; the runtime no longer silently accepts
invalid tags.
**712 → 730 tests green, 1 skip.**

### Honest scoring + clearer errors

- **Search render drops misleading `score=`.** RRF fused scores are
  rank-based by construction (`1/(k+rank_lex) + 1/(k+rank_sem)`) and
  the same staircase appears for every query. List position is the
  only honest relevance signal, so we now render
  `## 1. <slug>~<pos>` — no numeric score.
- **`move` with no implementing kind** now returns
  `Unsupported: no active kind currently supports move` with a
  pointer to `put`. Previously the error looked like a per-kind
  quirk and prompted retry-loops on small models.
- **Cost trailer dedup.** The runtime no longer prepends `— cost: `
  to the handler's already-bracketed `[cost: ~$0.0020]`, so we no
  longer emit the double-`cost:` form
  (`— cost: [cost: ~$0.0020]` → `[cost: ~$0.0020]`).
- **Cross-kind search hint** now enumerates every kind whose spec
  has `supports_search=True` rather than the hard-coded `<one of:
  calc>` placeholder. Comma-list kinds (`paper,memory`) are caught
  with a precise `comma-list kind not supported` error.

### Paper handler

- **`view='cite/bib'` ⇄ `view='bibtex'` symmetric.** The id-path
  form (`id='slug/cite/bib'`) and the kwarg form
  (`view='cite/bib'`) now share an alias map. `cite/ris` and
  `cite/endnote` likewise.
- **`view='abstract'` strips `<jats:*>` namespace tags** before
  render, so abstracts read as clean prose instead of leaking
  `<jats:title>Abstract</jats:title><jats:p>…</jats:p>`.
- **Paper list view paginates and shows the total.** Caps at 50,
  appends `(50 of N)` for larger corpora, ends with a Next: trailer
  pointing to `search` and `get(id='<slug>')`.
- **Slug minter strips glued-initials prefix.** `A.Clark` no longer
  produces `aclark1998extended`; the leading run of single-letter
  dotted segments is treated as initials and dropped, so the slug
  is `clark1998extended`. `St.Pierre` and other multi-letter
  prefixes are preserved.
- **`Store.count_refs(kind=, provider=)`** primitive added so
  paginated list views can render `(N of M)` without a second
  unbounded `list_refs` call.

### Tag validation

- **`Tag.parse_strict`** rejects two classes of bad input loudly
  with a `BadInput` carrying the canonical alternative:
    - **Unknown closed-vocab values** (`STATUS:bogus`) — error
      lists the valid set.
    - **Bare flags that collide with closed-vocab values**
      (`'urgent'`) — error suggests `'PRIO:urgent'`.
- Closed vocabularies registered in `store.types`:
    - `STATUS`: `open / doing / blocked / done / won't-do`
    - `PRIO`: `low / normal / high / urgent`
    - `SRC`: `primary / secondary`
    - `CACHE`: `fresh / stale / pinned`
- `_numeric_ref._apply_tags` calls `parse_strict` for every
  user-supplied tag, so all NumericRef kinds (memory, todo, gripe,
  fc, etc.) enforce the discipline uniformly.
- The previous permissive `Tag.parse` is preserved for internal
  call sites that aren't agent-driven.

### Numeric-ref list trailers

- `_list_view('recent')` now emits a Next: trailer in both the
  populated and the empty case, so `get(kind='memory', id='/recent')`
  no longer trails off silently. Empty-state hint points at
  `put(kind=K, text='...')` to populate.

### Documentation

- **`precis-overview`** dropped the four fictional kinds
  (`clock`, `rng`, `plot`, `ask`); renamed `ask` to the actual
  three Perplexity kinds (`websearch`, `think`, `research`); fixed
  the `wang2020state` example to a slug that's actually ingested
  (`abazari2024design`); softened the `move` row to "reserved";
  rewrote the cross-kind search paragraph to match runtime
  behaviour; dropped the unsupported `due='friday'` put example.
- **`precis-todo-help`** STATUS vocabulary aligned with runtime
  (`open` not `active`); `view='today'` / `'overdue'` etc. removed
  (not implemented); `due=` / `untags=` / `rel=` examples replaced
  with the `link='target_id:relation'` form actually accepted by
  the schema.
- **`precis-tags`** STATUS vocabulary aligned; `DENSITY:` /
  `CONFIDENCE:` removed from the closed list (not registered);
  validation errors documented with example messages; "Not yet
  implemented" section flags `tags=` filter on search and
  `untags=` on put.
- **`precis-paper-help`** all `wang2020state` examples replaced
  with `abazari2024design`; symmetric view-alias semantics
  documented; honest description of search ordering (no numeric
  score); JATS strip noted.
- **`server.py`** `move` tool docstring says "No active kind in
  this build implements `move`" so the schema-level description
  matches the runtime behaviour.

### Tests

- New `tests/test_mcp_critic_regressions.py` (18 tests) — one per
  fix, named to match the critic's labels so future failures
  point straight at the relevant CHANGELOG line.

### Deferred (acknowledged but not implemented this pass)

- **Cross-kind / comma-list search.** Real feature, not just a
  doc fix; the runtime now rejects with a precise hint and the
  docs no longer claim the default behaviour exists.
- **`view='today'` / `'overdue'` etc. on todo.** Needs a real
  due-date field. Removed from the docs; users can still tag
  `due:2026-05-01` lowercase and post-filter.
- **`due=` / `untags=` / `rel=` parameters on put.** Removed from
  the docs; users compose state via `tags=['STATUS:done']` and
  `link='target:rel'` against the documented schema.
- **`tags=` filter on search.** Removed from the docs; fetch a
  list view and filter client-side until the parameter lands.
- **Paper TOC section detector improvements.** The critic flagged
  one paper where 357 of 460 blocks landed under a single
  "DOI: …" pseudo-section heading. Heuristic work; deferred.
- **Slug dedup tool** (`precis jobs dedup-paper-slugs`). The
  minter no longer produces the bug; existing duplicate slugs in
  production corpora need a separate one-shot pass.

## Perplexity polish (`/recent`, imported badge, bulk CLI, no-key usability)

Follow-up to the import flow. Imports are now first-class for Pro
subscribers who don't have an API key at all.
**699 → 712 tests green, 1 skip.**

- `get(kind=<perplexity>)` with no `id=` (or `id='/'` / `id='/recent'`)
  renders a newest-first listing of up to 20 cached refs of that
  kind, showing slug, title, provenance (`imported` vs `fetched`),
  and date. `id_required=False` on all three KindSpecs to reflect it.
- `requires_env=("PERPLEXITY_API_KEY",)` dropped from all three
  specs. Imports, `/recent`, and cache hits all work without a
  key; only a cache-miss `get` still raises `Upstream` when the
  key is absent. Previously users had to set a dummy
  `PERPLEXITY_API_KEY` just to unhide the kind.
- `_cost_str` override: cache hits whose `cache_state.meta.source`
  is `"imported"` render as `[cost: free — imported]` so agents
  can tell user-supplied bodies from API-cached ones at a glance.
- New CLI: `precis jobs import-perplexity <dir>`. Walks a directory
  of markdown reports and bulk-calls `put(mode='import')` for each.
  `--kind` selects the tier, `--query-from h1|filename` picks the
  id-derivation heuristic (H1 heading with filename fallback, or
  always filename), `--dry-run` previews the derived queries.
- Updated `precis-perplexity-help` skill with the new verbs,
  listing view, and CLI.
- 13 new tests across `tests/test_perplexity.py` and
  `tests/test_cli.py`: empty-state listing, mixed provenance
  listing, per-kind scope isolation, bad-view typo rejection,
  listing-without-API-key, imported-badge on hit, fetched keeps
  `cached` badge, CLI dry-run with H1 + filename-fallback, CLI
  filename-strategy override, CLI end-to-end write, CLI
  empty-file handling, CLI missing-dir handling.

## Perplexity import (`put(mode='import')`)

Pro subscribers can run deep research in the Perplexity web UI for
free, paste the result into precis, and have it land in the *same*
cache row a paid `get` would have produced. Future `get` on the same
query then returns the imported body for $0.
**555 → 565 tests green, 1 skip.**

- `_PerplexityBase.put(id=<query>, text=<report>, mode='import')` —
  validates inputs, parses the body via the existing `parse_markdown`
  splitter (so reports become per-heading / per-paragraph / per-list
  blocks with stable content-derived slugs), embeds the blocks via the
  active `Embedder` if one is configured, and calls
  `Store.put_cache_entry(provider='perplexity', cost_usd=0,
  ttl_seconds=None)`.
- The cache key matches what `get` would compute (`<model>:<query>`)
  so the import populates the row a future paid call would hit. Both
  `refs.meta.source` and `cache_state.meta.source` are set to
  `"imported"` for provenance.
- `WebsearchHandler` / `ThinkHandler` / `ResearchHandler` flip
  `supports_put=True` and advertise `modes=("import",)`. Each kind
  imports under its own model — so the same `id=` imported under
  `research` and `websearch` lives in two distinct cache rows by
  design.
- `_PerplexityBase.__init__` accepts an optional `embedder=`; the
  registry passes the active embedder through. With no embedder
  configured (e.g. stateless test runs) imports still land but
  without per-block vectors.
- New skill: `precis-perplexity-help` — documents the three Sonar
  tiers and the import flow side by side.
- 10 new tests in `tests/test_perplexity.py` cover: import → cache
  hit at $0; multi-block parsing; import without embedder; idempotent
  re-import (replace, not duplicate); per-kind cache isolation;
  `meta.source='imported'` provenance; mode/id/text validation;
  imported blocks are findable via fused block search.
- No DB schema change. No new kinds. No new env vars. No CLI changes.

## Phase 6a — Markdown file handler

The first file-backed kind. Read and edit `.md` files under a
configured root with the same four verbs every other kind uses.
**450 → 520 tests green, 1 skip.**

- `precis.utils.md_parse` — pure-logic markdown splitter. Recognizes
  ATX headings (1–6), fenced code (` ``` ` / `~~~`), pipe tables
  (with separator row), ordered + unordered lists, paragraphs.
  Thematic breaks are dropped; blank lines separate blocks.
  Per-block slugs are content-derived: heading slugs from the
  heading title (`# Hello World` → `hello-world`), other-kind slugs
  from `<5 leading words>-<6 hex>`. Stable across re-ingest.
- `precis.utils.md_parse.file_slug_from_path` — encodes a relative
  file path as a ref slug (`notes/meeting.md` → `notes--meeting`).
  `--` is the segment separator; segments are normalized to
  lowercase a–z 0–9 `_` `-`. `is_valid_file_slug` enforces this on
  every call (defence-in-depth against path traversal even though
  the handler also resolves+checks against the configured root).
- Migration `0004_file_kinds.sql` registers `markdown`, `plaintext`,
  `rmk`, `docx`, `tex` in the `kinds` table (only `markdown` has a
  handler in this session — others queue for phase 6b).
- `precis.config.PrecisConfig.markdown_root` (env:
  `PRECIS_MARKDOWN_ROOT`). The handler is hidden when unset.
- `MarkdownHandler` (slug-addressed, supports get/search/put):
  - `get(id='slug')` — overview + flat heading list (H1 + H2)
    + `Next:` hint trailer.
  - `get(id='slug~SLUG')` — one block by stable slug.
  - `get(id='slug~N')` — one block by 0-indexed pos.
  - `get(id='slug/toc')` — full hierarchical TOC (reuses
    `_paper_toc.build_toc` + `render_toc`).
  - `get(id='slug/raw')` — full source text.
  - `get()` / `get(id='/')` — index of every `.md` file under root.
  - `search(q='...', scope='slug')` — block-level fused-search
    (lexical + vector if embedder).
  - `put(mode='create', id='slug', text=...)` — create new file.
  - `put(mode='append', id='slug', text=...)` — append paragraph.
  - `put(mode='replace', id='slug~SLUG', text=...)` — rewrite one
    block in place.
  - `put(mode='delete', id='slug~SLUG')` — drop one block.
- **Lazy re-ingest**: every `get` first stats the file. If
  `meta.mtime_ns` matches, the cached blocks are served. If mtime
  differs but sha256 matches, only meta is bumped. If sha256
  differs, the file is re-parsed and blocks are atomically replaced.
  Block slugs survive across re-ingest (content-derived). Deleted
  files trigger soft-delete of the ref so the index stays clean.
- **Atomic writes**: every put writes via tmpfile + `os.replace`.
  After write the handler force-re-ingests so the next get sees
  the new state.
- **Path-traversal safety**: ref slugs are validated by
  `is_valid_file_slug`; the resolved path is checked to be under
  the configured root with `Path.relative_to`.
- CLI: `precis jobs ingest-md <root> [--force]` — pre-warm a
  directory (the handler ingests lazily on first `get` anyway, but
  pre-warming is useful before launching long-running searches).
- 70 new tests across 2 files: `test_md_parse.py` (37) covers
  the parser + slug helpers; `test_markdown_handler.py` (33)
  covers handler get/search/put/lazy-reingest end-to-end.
- Skill: `precis-markdown-help.md` documents address shapes,
  block kinds, put modes, CLI usage, and limits.
- Live verification: created `/tmp/precis-md-demo/` with two files,
  ingested, walked the TOC, edited a block via `put(mode=replace)`,
  appended a paragraph, created a new file. All atomic, all
  reflected on next `get`.

## Phase 5 — State kinds (todo, gripe, fc, quest, conv, oracle, skill)

The bulk of the agent-facing API for personal state. Six new kinds
plus the shared base that finally makes adding a new ref kind trivial.
**447 tests green, 1 skip.**

- `precis.handlers._numeric_ref.NumericRefHandler` — extracts the
  shared CRUD shape (get / search / put-create / put-update /
  delete / list-recent) that MemoryHandler had grown organically.
  Subclass contract is tiny: `spec`, `kind`, `sense`,
  `default_tags_on_create`, optional `_render_one` /
  `_render_search_hit` / `_list_view` / `_render_create_ack`.
- `precis.handlers.memory` — refactored to a 30-line subclass of
  the new base. All 20 memory tests still green.
- `TodoHandler` — STATUS:open default-on-create; status transitions
  via closed-prefix tag replacement (STATUS:doing supersedes
  STATUS:open atomically); `/open`, `/doing`, `/blocked`, `/done`,
  `/queue` list views; aligned Next: trailers on every view.
- `GripeHandler` — minimal numeric-ref kind. No default tags,
  free-form body. Lexical search.
- `FlashcardHandler` (`fc`) — knowledge statements with SM-2 review
  state in `ref.meta`. `/due` view surfaces cards whose
  `next_review` is in the past plus an "upcoming within 3 days"
  block. The actual SM-2 grader is deferred until the review-feedback
  agent surface lands.
- `QuestHandler` — slug-addressed work-queue kind with auto-mint:
  `put(text=...)` derives a slug via `slug_from_text`, appends
  `-2`/`-3` on collision. Same STATUS: vocabulary as todos. `/open`
  / `/doing` / `/blocked` / `/done` filters.
- `ConversationHandler` — read-only durable transcripts; one block
  per turn. Three views: overview (`slug`), full transcript
  (`slug/transcript`), single turn (`slug~N`). Block-level
  fused-search via `slug` scope.
- `OracleHandler` — slug-addressed authoritative reference nodes
  (e.g. saved rubrics, prompts). Read-only in phase 5; future `put`
  adds versioning.
- `SkillHandler` — markdown skills served from
  `precis.data.skills` package data via `importlib.resources` (so
  it works from a wheel). `get(kind='skill')` lists every skill with
  its title; `get(kind='skill', id='precis-overview')` returns the
  raw markdown; `search(kind='skill', q='...')` does case-insensitive
  full-text search across all skills. Front-matter `title:` is
  surfaced in the index. Read-only by design — skills are versioned
  with code.
- 51 new tests across 3 files: `test_todo.py` (16), `test_state_kinds.py`
  (24), `test_skill.py` (11).

## Phase 4b — Perplexity Sonar trio

Three new cache-backed kinds sharing one shared base. **396 tests
green, 1 skip.**

- `precis.handlers.perplexity._PerplexityBase` (subclass of
  `CacheBackedHandler`). Subclasses set `model`, `timeout`,
  `cost_per_call_usd`, `ttl_seconds`, and an attribution string.
- `WebsearchHandler` — `sonar`, 30s timeout, 7-day TTL,
  ~$0.001/call.
- `ThinkHandler` — `sonar-reasoning-pro`, 120s, 30-day TTL,
  ~$0.005/call.
- `ResearchHandler` — `sonar-deep-research`, 600s, **pinned**
  cache (these cost ~$0.50 each — never expire automatically),
  ~$0.50/call.
- Cache key is `<model>:<query>` so the same prompt under different
  tiers never collides on the `(provider='perplexity',
  request_hash)` unique index.
- Per-Perplexity-ToS attribution: every response carries a footer
  noting AI generation, model used, citations are not primary
  sources, and ToS disclosure requirements.
- Cache-hit Next: trailer suggests the next tier up
  (websearch → think → research) and a deep-link to fetch the
  first cited URL via `kind='web'` for primary-source verification.
- Migration `0003_perplexity_kinds.sql` registers the three kinds
  in the `kinds` table.
- 23 new tests with mocked httpx + env. All HTTP error cases
  (401/429/5xx/timeout/network) map to the correct `Upstream`
  variants.

## Phase 4a — Cache-backed kinds (math, youtube, web)

Three new kinds plus the shared infrastructure they need. 331 tests
green, 1 skip.

- Migration `0002_cache_providers.sql` adds the `web` provider row
  (others ship in 0001).
- `Store.get_cache_entry(provider, request_hash)` and
  `Store.put_cache_entry(...)` — atomic ref + `cache_state` upsert,
  hard-replaces existing refs with the same kind+slug so re-fetches
  cleanly cascade away stale blocks.
- `CacheBackedHandler` base in `handlers/_cache_base.py`. Shared
  cache flow: hash → lookup → freshness check → fetch-on-miss →
  attribution footer → cost trailer. Subclass contract is small:
  `provider`, `ttl_seconds`, `attribution`, `corpus_slug`,
  `_canonical_key`, `_fetch`. `FetchResult` dataclass wraps the
  upstream result.
- `MathHandler` (Wolfram Alpha): hand-rolled httpx GET to bypass two
  upstream `wolframalpha` library bugs (asyncio.run-in-loop, strict
  Content-Type assertion). Pod → markdown formatter ported from v1.
  Per-query deep-link + paste-ready academic citation appended to
  attribution. Cache pinned (results deterministic).
- `YouTubeHandler`: cache key is the bare 11-char video id, so URL
  variants (youtu.be / watch?v= / shorts / embed / live / mobile)
  collapse onto one row. Language preferences are part of the key
  (en/es cache separately). `view='languages'` side query lists
  available tracks. 30-day TTL.
- `WebHandler`: page-fetch mode. Canonical URL is the cache key
  (drops tracking params, default ports, fragments on non-SPA hosts).
  Article extracted with trafilatura → markdown body. 7-day TTL.
  Phase 4a ships fetch-mode only; bookmark mode + Wayback deferred
  to phase 4b.
- `precis.utils.url` ports v1's URL canonicalization
  (`canonical_url`, `slug_from_url`, `is_http_url`, `host_of`).
- All three kinds wire into the registry behind a try/ImportError
  guard: missing optional dep (`[external]` extra) silently hides
  the kind without breaking server startup.
- Skill drafts: `precis-math-help.md`, `precis-youtube-help.md`,
  `precis-web-help.md`.

## Phase 3.5 — Navigation parity

The user-facing navigation that made v1 distinctive, restored. **373
tests green, 1 skip.**

- `precis.utils.next_block` — `format_next_block` and
  `render_next_section` helpers. Column-aligned `(call, description)`
  pairs with em-dash separators; the formatter is shared across all
  handlers that emit `Next:` trailers.
- `precis.handlers._paper_toc` — heading detection (acatome
  `■ **NAME**` / `**Name**` / markdown `# Name` / `## Name`), section
  grouping, range-scoped clipping for drill-down, hierarchical
  rendering. Pure logic; no DB dependency.
- `PaperHandler.get(view='toc')` now produces a structured jump table
  with section/subsection ranges, block counts, indented children, and
  a "Next:" trailer pointing at the largest section to drill into.
  Replaces the flat "block 0 / block 1 / block 2 …" listing.
- `PaperHandler` accepts the combined drill-down id form
  `slug~A..B/toc` — TOC scoped to that range. Recursive: each child
  section is itself addressable.
- Aligned `Next:` trailers added to every PaperHandler view:
  - **overview**: TOC, first chunks, BibTeX, scoped search
  - **chunks**: next/previous range (sized to match the current
    range), full TOC, range-scoped TOC, BibTeX
  - **TOC**: drill into largest section, read largest section, BibTeX
- Live verified against the real `acheson2026automated` paper (177
  blocks → 20 detected sections; METHODS has 4 H2 children; RESULTS &
  DISCUSSION has 2). Drill-down to `~74..116/toc` correctly clips to
  just RESULTS & DISCUSSION + its children.

## Phase 3 — Paper kind + bundle ingest

End-to-end paper handling: ingest from `.acatome` bundles, hybrid block
search, citation views, CLI cutover commands. 216 tests green.

- `utils/slug.py`: deterministic `<surname><year><word>` minter with
  collision suffixing; pure logic, no DB
- `embedder.py`: `Embedder` Protocol, `MockEmbedder` (deterministic,
  used by all unit tests), shell `BgeM3Embedder` for the optional
  sentence-transformers backend
- `Store` block CRUD: `insert_blocks`, `get_block`,
  `list_blocks_for_ref`, `count_blocks`, `update_block_density`,
  `update_block_embedding`, `blocks_missing_embeddings`
- `Store` block search: `search_blocks_lexical` (tsvector +
  `ts_rank_cd`), `search_blocks_semantic` (pgvector cosine),
  `search_blocks_fused` (RRF, k=60, falls back to lex-only when no
  query vector supplied)
- `ingest.py`: bundle parsing, density classifier, embedding fill,
  slug minting glue
- `Store.ingest_bundle()`: idempotent on DOI; reuses bundle vectors
  when dim matches, re-embeds otherwise; applies `SRC:bundle` tag and
  density tags per block; one transaction per bundle
- `PaperHandler`: slug-addressed read-only kind. `get(id=slug)`
  overview, `id=slug~N` / `id=slug~N..M` chunk selectors,
  `id=slug/cite/bib`/`/abstract`/`/toc` view paths, `view='bibtex'`
  /`'ris'`/`'endnote'`/`'abstract'`/`'toc'` kwargs.
  `search(q=…, kind='paper', scope=slug)` block-level RRF search
- CLI: `precis migrate [--dry-run] [--database-url …]`,
  `precis jobs ingest-bundle <file>`,
  `precis jobs ingest-bundles <dir> [--dry-run] [--limit N]`
- `docs/design/v2-cutover.md`: ops runbook for the v1 → v2 switch

## Phase 2 — DB backbone (sync, psycopg 3) + memory handler

End-to-end ref-backed kind via local postgres. Sync top-to-bottom below
FastMCP. 88 tests green.

- `psycopg[binary,pool]` 3.2; pgvector codec via `pgvector.psycopg`
- `Store` (sync): corpus, ref CRUD, tag CRUD, system settings
- `Migrator`: forward-only SQL migrations with sha256 checksum guard
- `MemoryHandler`: first ref-backed kind. Numeric id, get/search/put,
  closed-prefix tag replacement
- Schema fixes: renamed `symmetric` → `is_symmetric` (postgres reserved
  word); `pos = -1` sentinel for ref-level (PK/UNIQUE without partial
  indexes)
- `tests/conftest.py` ephemeral-DB fixture (no docker, no testcontainers)

## Phase 1 — Walking skeleton (4 verbs + calc + HintBus)

End-to-end MCP server with one stateless kind. No DB. 39 tests green.

- `errors.py`: `PrecisError` hierarchy with `next=` breaking hint
- `hints.py`: `HintBus` contextvar collector, dedup with cooldown ring
- `runtime.py`: `PrecisRuntime` verb dispatch + error rendering
- `server.py`: FastMCP stdio server exposing `get/search/put/move`
- `cli.py`: `precis serve | migrate | jobs`
- `handlers/calc.py`: sympy-backed stateless calculator

## Design artefacts (pre-phase-1)

Ground-up rewrite. v1 history preserved in `main` branch upstream and on
the `v1-local` git remote. Breaking redesign — nothing wire-compatible
with v1.

- Schema: `src/precis/migrations/0001_initial.sql`
- Python store interface sketch: `docs/store_sketch.py`
- Paper-ingest spec: `docs/user-facing/paper_ingest.md`
- Phase-3 plan: `docs/design/phase3-plan.md`
