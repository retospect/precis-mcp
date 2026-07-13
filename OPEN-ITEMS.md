# precis-mcp — Open Items

Durable backlog. Replaces the per-issue gripe trail (gripes 3667 +
3681 retired 2026-05-02 after the seven-verb surface refactor closed
their original framing) with a single canonical entry-point.

The mcp-critic review at
[`docs/mcp-critic-review-2026-05-02.md`](docs/mcp-critic-review-2026-05-02.md)
remains as the historical observation log; this file tracks only
what's still open.

> **Convention**:
> - **Status**: `open` / `blocked` / `deferred` / `done`
> - **Severity**: `critical` (blocks release) / `feature` / `polish`
> - **Owner**: rough estimate of where the fix lives
> - **Test**: name of the regression test that pins it (when fixed)

---

## 🔐 secrets vault — remaining rollout + follow-ups (2026-07-13)

v1 shipped on `main` 5a5de3ac (ADR 0055, migration 0059): encrypted
`vault.secrets` + `vault.list/mask/reveal/set_secret/delete_secret`, resolver
`src/precis/secrets.py` (env→vault→file, cached), `precis secret` CLI,
`/secrets` web editor, DSN scrubbed from env at boot. Ships **dark**
(env-override-wins). Remaining:

- **Ops — the one bootstrap step to make it live** — `blocked` (needs Reto +
  `.vault-pass`), Owner: `~/work/cluster`. Add `app.secret_key` to
  `inventory/group_vars/all/vault.yml`; apply on caspar via `ALTER SYSTEM SET
  app.secret_key = ...; SELECT pg_reload_conf()` in the postgres role. Until
  set, `reveal`/`set` fail (resolver degrades to env/file). Then `/go` to
  deploy the migration + `/secrets` page.
- **Migrate the remaining call sites** — `feature`, Owner:
  `handlers/*`, `workers/fetch_oa.py`. Only `PERPLEXITY_API_KEY` moved onto
  `get_secret` so far. Still raw `os.environ`: `WOLFRAM_APP_ID`, `EPO_OPS_*`,
  `ORCID_CLIENT_SECRET`, `PRECIS_ELSEVIER_API_KEY`, `PRECIS_WILEY_TDM_TOKEN`,
  `PRECIS_CORE_API_KEY`, `PRECIS_OPENALEX_CONTENT_KEY`, `SEMANTIC_SCHOLAR_API_KEY`,
  `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`. Migrate each to
  `secrets.get_secret` (behaviour-identical under env-override).
- **`requires_secret` gate** — `feature`, Owner: `protocol.py` / `kind_gate.py`.
  Kinds gate on `requires_env`; once a secret is vault-only (env pulled in
  phase D), those kinds would hide. Add a `requires_secret` axis checking the
  resolver before pulling any secret out of env.
- **`/secrets` web smoke test** — `polish`, Owner: `tests/`. The route is only
  covered by app-boot import today; add a FastAPI TestClient test (list renders,
  set writes, blank submit no-ops).
- **Deferred by design (ADR 0055)**: per-service roles (`precis_secrets` /
  `precis_web` / `asa`) + per-name ACL; `pg_notify`-driven cache invalidation
  (currently a 60s TTL); out-of-process extension broker.

---

## 🎨 `figure` kind — deferred slices (2026-07-12)

Slice 1 shipped: the `figure` kind (interactive SVG canvas), migration 0057,
`handlers/figure.py` + `precis/figure/{svg,turn}.py`, the `/figure` web editor
(draw-with-me turn loop, compile + out-of-bounds lints, sanitize, bounded
auto-heal), skills `precis-figure-help` + `precis-figure-svg`. All **feature
extensions**, not bugs — ordered roughly by value:

- **PNG / animated-raster export** — `feature`, Owner: a `figure_render`
  derived-lane job + a rasterizer. No SVG rasterizer is a dep today
  (cairosvg/resvg need system libs / rust — the reason it's deferred). Design:
  own the timeline as **declarative keyframes on named nodes**, interpolate +
  render each frame as static SVG via `resvg`, encode GIF/APNG/WebP — **no
  headless browser** (raw SMIL/CSS wouldn't survive export). Still PNG is the
  first step.
- **three.js / `scene3d` mode** — `feature`. One kind, `meta.render ∈
  {svg,scene3d}`; 3D uses a **declarative scene IR + trusted client renderer**
  (never eval raw three.js — XSS). Add `precis-figure-scene3d` skill.
- **Per-node chunk split** — `feature`, Owner: `figure/svg.py` + handler.
  Today the source is one `figure_node` chunk (the whole `<svg>`); split into
  one chunk per top-level element/group (`fn<id>` each) once per-node *edits*
  (batch transaction) land — the payoff that justifies the XML round-trip.
- **Draft-embedding** — `feature`. A draft includes a figure's rendered raster
  as an **asset** (not a document export — orthogonal to `corpus_role='none'`;
  reuse `export/sources.py`'s asset resolver). Add a `figure-in`→draft link.
- **`read(handle)` reference tool in the turn loop** — `feature`. Let the
  model pull any `dc…`/`fn…`/`pc…` handle into the turn (vocab-by-reference:
  "eyes like dc1234"). One read-only tool, on-demand into the variable layer.
- **Pin full `precis-figure-svg` skill text into the turn prompt** — `polish`,
  Owner: `figure/turn.py` + route. The turn currently inlines a *condensed*
  operating manual in `build_prompt`; wire the real skill body as the pinned
  cached layer (`run_turn(..., skills=…)` is already the seam).
- **Formalized-convention hard-checks** — `polish`. Optional opt-in: promote a
  *specific* formalizable convention (e.g. an explicit hex palette allowlist
  declared in the vocab) to a mechanical lint. Most conventions stay the
  model's job (held via the vocab), never a general "convention linter".

## 🔵 Turn-as-job routing + context DSL — WIP design (2026-07-07)

- **Status**: `deferred` (design captured, not sliced) —
  **Severity**: `feature` — **Owner**: `handlers/job.py` +
  `workers/dispatch.py` + `utils/handle_registry.py` +
  `utils/prompt/` (assembler) + a scheduler affinity layer; router
  touches ADR 0046 / `utils/claude_agent.py`.
- **Design of record**:
  [`docs/proposals/turn-routing-and-context-dsl.md`](docs/proposals/turn-routing-and-context-dsl.md).
  Every turn is a `kind='job'`; Part 0 = thread persona + cache-ordering
  gradient + affinity scheduling; Part 1 = delegate-on-confidence
  routing (Opus drives, assigns helpers); Part 2 = the stateful context
  DSL (ADR 0036 handles + fidelity ladder, receipt-default collapse).
  Taxonomy: Followup / Call / Spawn. First slice = persist turn-as-job +
  shadow router.

## 🟠 Planner "new writing task" wizard — don't auto-dispatch on create (2026-07-07)

- **Status**: `open` — **Severity**: `feature` — **Owner**:
  `precis_web/routes/tasks.py` (create-root form) + `handlers/todo.py`
  (`TodoHandler.put` LLM-tag auto-stamp, lines ~525–549).
- **Why**: today a strategic root is born with `LLM:opus` +
  `level:strategic` already attached, so `dispatch` mints a `plan_tick`
  the instant it exists and the planner fans the whole doc into
  section-todos — each of which auto-gets `LLM:opus` and dispatches its
  own tick. Reto watched this run away three times in one session on a
  "Suitcase Design" paper (roots 52247, 52306) and had to SQL-kill the
  subtrees. The **stop/start buttons shipped 339b77f4** make the kill
  one click, but the root cause is *creation auto-starts the planner*.
- **Spec (agreed this session)**: replace the bare new-root form
  (`dashboard.html.j2:185-203`) with an old-timey **wizard** that
  *collects intent before outputting*:
  - a **description** textbox ("what are we doing / what's in the doc"),
  - a **doc-type** select (paper / draft / pres / cfp / …),
  - a **"Start planning now"** checkbox, **default OFF**.
  `POST /tasks/roots` gains `doc_type` + `start`. **OFF** → create the
  root **with no `LLM:` tag** (stash description + `meta.doc_type`); it
  sits parked, invisible to `dispatch` (absence of the planner tag *is*
  the gate — no dispatch change needed). **ON** → stamp `LLM:opus` as
  today. The **▶ start** button on a parked root then stamps `LLM:opus`
  (+ seeds the chosen doc-type workspace) to begin.
- **Open design point** (Reto leaned "every planner todo gated" earlier,
  then reframed to the wizard): once a root is started, do its
  section-children flow automatically (recommended — else ▶ per
  section), or stay individually gated? The shipped ⏹/▶ already give
  per-subtree control on demand, which argues for root-only gating.
- **Test**: a `POST /tasks/roots` with `start` unset creates a todo
  carrying **no** `LLM:`/executor tag (dispatch-invisible); with
  `start=on` it carries `LLM:opus`.

---

## 🟡 Unified item view (`/items`) — one DRY cross-kind list/search

**Status**: slices 1–3a shipped + deployed; rest of slice 3 + slice 4 open
· **Severity**: feature · **Owner**: `precis_web/routes/items.py`,
`precis_web/item_view.py`, `handlers`/`store` search surface
· **Design**: [`docs/proposals/unified-item-view.md`](docs/proposals/unified-item-view.md)

One surface where the human's filter == the LLM's retrieval scope (a
tailored view is a context set). Author/source is a `kind` facet, not a
separate page.

- **Slice 1 — reading-intent flags → shipped + deployed** (`94a5dcc1`).
  `read-later`/`must-read`/`skim` toggle buttons (kind-agnostic
  `POST /flags/{kind}/{ref_id}` + `_flag_buttons.html.j2` + batched
  `Store.ref_tag_values`), first on `/papers-needed`; ride through ingest.
- **Slice 2 — cross-kind search primitive → shipped + deployed** (`f1139ef7`).
  `Store.search_chunks_across_kinds` (RRF lexical+semantic over `refs.kind =
  ANY(...)`, per-ref best chunk, `created_at` window, relevance|recency);
  `search` verb gained `kinds`/`sort`/`since`/`until` → `_dispatch_source_search`.
- **Slice 3a — `/items` page + presenter seed → shipped + deployed**
  (`efce60df`…`f2c027b3`): read-only cross-kind search page; per-item tag
  chips + grouped `[kind][state]` markers; New dropdown; kind chips
  (All/None, cookie-remembered); tag autocomplete→chips filter
  (`GET /items/tags/suggest`); "recently added" default landing; stub filter
  (papers-to-get); UoL/Scholar/DOI find-links (shared `precis_web/paper_links.py`).
  `ItemPresenter` is a plain class with a generic default (`open_url`/`state`/
  `links`/`preview`), not yet the abstract contract.
- **Rest of slice 3 — open.** Promote `ItemPresenter` to the full contract
  (`preview(query)->text|image`, `hover_preview`, `thumbnail`, `actions`) and
  to `@abstractmethod` once every kind adopts (check-time totality); result
  pagination (currently capped at 30, no paging); author/source facet +
  folders + thumbnails/hover for visual kinds; retire `/drive` /
  `/papers-needed` / `/papers/triage` / `/refs` / `/tags/refs` into `/items`
  filters.
- **Coupled — kind-taxonomy audit — open.** Reconcile `role`/`corpus_role`
  drift (datasheet `evidence`+`stream`; pres `corpus`+`none`), collapse
  near-dup kinds (perplexity-*/websearch/web/wikipedia; calc/math/oracle),
  rewrite `precis-*-help` skills. No-legacy-alias license (a fresh LLM
  re-reads skills each session): interface free to change, data isn't.
- **Slice 4 — "write a document from this view" — open.** A tailored filter
  is a serialized query → mint an authoring job scoped to exactly those refs.
- **Verification residual.** The `/items` filter-bar JS (Alpine
  tag-autocomplete + chip add/remove) is backend-tested but not visually
  verified — eyeball the live page.

## 🟢 Draft inline editor (click-to-edit prose, no LLM)

**Status**: shipped + deployed (core complete; only optional extensions + a
verification residual remain, below) · **Severity**: feature · **Owner**:
`precis_web/routes/drafts.py`, `static/`, `handlers/draft.py`
· **Design**: [`docs/design/draft-inline-editor.md`](docs/design/draft-inline-editor.md)

Direct human editing of `draft` prose from the reader — click a paragraph,
edit raw text, save on click-out, `+`/delete paragraphs — bypassing the LLM
change-request path. **Model B** (a box per chunk, wired caret handoff,
contained edits; the full rationale + refs-as-decorated-text, spellcheck,
split/merge, and the vendored-PM-bundle spike are in the design doc).

- **Slice 1 — validation core → shipped.** `DraftHandler._newly_dangling`
  (old-vs-new dead-ref diff) + extracted `_dangling_chunk_tokens` /
  `_dangling_finding_tokens` + advisory `_dangling_edit_hint` wired into the
  MCP/CLI edit path (previously the edit path gave *no* dangling feedback).
  Test: `test_edit_flags_newly_introduced_dangling_ref`.
- **Slice 2a — text-edit MVP → shipped + deployed.** `POST /drafts/{ident}/text`
  (hard `_newly_dangling` 422 gate → `edit` verb → soft-warning tail); per-block
  ✎ editor (plain textarea, saves on blur/⌘↵, Esc cancels) on prose kinds only
  (`_EDITABLE_KINDS`); `base_sha` optimistic concurrency (sha computed in
  `_build_rows`, no dataclass change); on success a `draft:edited` event drives
  `draftDoc.rehydrateOne` to refresh the block in place + a toast. Tests:
  `test_newly_dangling_returns_only_new_breakage`; live-verified on melchior
  (markup served + endpoint JSON contract). **No ProseMirror yet.**
  Browser-confirmed working by Reto (edits save persistently). Two `x-data`
  quoting bugs found + fixed post-deploy (the editor's `| tojson` double-quotes,
  and a **pre-existing** wordcount-badge one) — both now have parse-level
  regression tests (`test_inline_editor_xdata_is_single_quoted`,
  `test_wordcount_badge_xdata_is_attribute_safe`). **Lesson:** verify the
  rendered attribute *value* (parse it), not substring presence.
- **Slice 2b-i — inline add / delete blocks → shipped + deployed.**
  `POST /drafts/{ident}/block` (empty paragraph after an anchor via
  `store.add_chunks`, since `put` rejects empty text) + `POST
  /drafts/{ident}/block/{handle}/delete` (`delete` verb, `cascade=1` for a
  heading's subtree, kind-aware confirm). Client: ＋¶ / 🗑 hover controls on
  editable blocks; a new block auto-opens its editor (`__draftAutoEdit` flag
  consumed by the row's `init()`). Test:
  `test_add_empty_block_inserts_paragraph_after_anchor`; live add+delete
  round-trip verified net-zero on dream-review (21→22→21). *Scoped to editable
  prose kinds — figures/tables keep their own controls.*
- **Slice 2b-ii — ProseMirror editor + live squiggle → shipped + deployed.**
  Vendored `static/prosemirror.bundle.mjs` (206 KB / 63 KB gz — only the modules
  used: state/view/model/keymap/commands/history/TextSelection; **not** stock
  prosemirror-markdown — it escapes our ref brackets). Minimal schema
  (`doc > block > text|hard_break`, identity round-trip, headless-verified);
  `POST /drafts/{ident}/validate-refs` (reuses the dangling-token helpers) drives
  a debounced **red wavy squiggle** (`.ref-bad`) on unresolved refs as you type —
  the live face of the save-time gate. Editor **replaces** the rendered text in
  place (rendered view is `x-show="!editing"`, editor a sibling), caret
  auto-placed at end, raw toggle retired. Mirrors into the hidden textarea so the
  save/validate flow is unchanged, and **falls back to that textarea** if the PM
  module fails to load/mount (can't regress). Editable prose kinds only. Browser-
  QA'd by Reto. *Verification gap:* no headless-browser test — bundle load,
  schema round-trip, endpoint, `.mjs` MIME, and syntax are checked, but the
  contenteditable/decoration rendering rides on the fallback + Reto's eyes.
- **Slice 2b-iii — editor-feel (caret flow + split/merge) → shipped + deployed.**
  **Enter** splits at the caret (`/block/{h}/split`: current keeps `before` + its
  handle, new chunk gets `after`, opens caret-at-start); **Shift-Enter** = soft
  break. **Arrow up/down past the top/bottom line** hands off to the neighbouring
  editable block (`draft:goto` → `_neighbour` skips figures/tables → `openEditor`
  at end/start; a registry `__dEditors` + a retry loop make it robust to
  live/placeholder/recycled rows). **Backspace at start** merges into the previous
  block (`/block/{h}/merge-prev`: client sends live text so unsaved keystrokes
  survive; empty block → just delete + go to prev end; caret at the join offset —
  doc pos is `1+offset`; no-ops rather than folding a heading away). Split-point
  math headless-verified (before+after reconstructs across 61 caret positions);
  tests `test_split_keeps_handle_and_inserts_tail_after`,
  `test_merge_prev_joins_text_and_deletes_block`; endpoints live-verified
  net-zero on dream-review. Same headless-browser verification gap as 2b-ii.
- **Slice 3 — polish → shipped + deployed.** (1) **`[`-autocomplete**:
  `GET /drafts/{ident}/ref-search` title-searches held papers
  (`find_papers_by_title` + `fetch_refs_by_ids`) → citation tokens; a PM
  dropdown (arrow/enter/tab, coordinates with the split/leave keymap via an
  `ac.active` gate) inserts `[§slug]`. (2) **Reveal-on-cursor**: a selection-aware
  `chipPlugin` styles ref tokens (`REF_TOKEN` regex) as chips, showing raw only
  when the caret is inside — the in-editor complement to the reader's already-
  pretty non-editing view. (3) **Removed the dead `tailwind.js`** Play CDN (the
  static build is confirmed good). All wrapped so a failure degrades gracefully
  (autocomplete `try`-guarded; chips are display-only). Live-verified:
  `ref-search?q=attention` → real papers; `tailwind.js` 404, `tailwind.css` 200.
  - *The editor is complete.* The deferred extensions and the verification
    residual are broken out as their own backlog entries below.
- **Draft editor — deferred extensions** → backlog (optional, none block use):
  - **`[`-autocomplete over non-paper kinds** (chunks / findings), not just held
    papers — extend `GET /drafts/{ident}/ref-search` + the picker's result set.
  - **Resolved-title chips** — reveal-on-cursor shows the raw token today; a small
    resolve endpoint would let the chip show the target's title/section instead.
  - **Structured-block creation from the editor** — a slash-menu to insert a new
    table / figure / code block inline (today those use the existing buttons).
  - **Per-draft language selector** for browser spellcheck (defaults to OS lang).
- **Headless-browser verification in CI** → backlog (testing infra, high-value).
  The interactive editor + virtual-scroller JS has **no gate coverage**, and
  several browser-only bugs reached prod this session — the `x-data` `| tojson`
  quoting, Safari `group-hover`, `forceRefresh` early-return, and the
  programmatic-open **focus** bug. An **ad-hoc Playwright-over-SSH-tunnel harness**
  (2026-07-05) both *found* and *proved* the focus bug (system Chrome via
  `channel:'chrome'`, tunnel to `melchior:8000`, `page.evaluate` probes of
  `window.__dEditors`). Wire a slim version into `scripts/ship`/the gate: boot the
  web app on the test DB with a seeded draft, load a page, assert a **clean
  console** + a couple of core interactions (open editor → focused; arrow → the
  neighbour opens focused). Closes the recurring "ship blind, Reto QAs" loop that
  cost several round-trips this session.

---

## 🔵 Retire the `equation` chunk kind → math as `$…$` / `$$…$$` in prose

**Status**: decided · **Severity**: feature (simplification) · **Owner**:
`draftimport/`, ingest/paper pipeline, `precis_web/routes/{papers,drafts}.py`,
`export/latex.py`, a forward migration

**North star.** No dedicated `equation` kind. Math is LaTeX *inside* prose —
inline `$…$`, display `$$…$$` — KaTeX-rendered on read, edited as raw source.
It behaves like every other block; no special reader/editor/export branch, no
`needs-math-review` quarantine.

**Scope reality (verified 2026-07-05, `precis_prod`).** ~54,920 live `equation`
chunks, and they are **overwhelmingly papers, not drafts**:
`paper = 54,642 · draft = 278`. So the earlier draft-scoped project prompt
covers the *minority* — **papers are the real target** and have different
mechanics. The two fronts:

- **Drafts (278) — sorted.** Mutable chunks from the LaTeX importer
  (`draftimport/tex.py`, `_MATH_ENVS`, `needs-math-review`); draft reader; bodies
  are bare LaTeX + `\label{eq:…}`. Plan = the project prompt held by Reto
  (2026-07-05): stop minting `equation` in the importer, migrate to `$$…$$`
  paragraphs, drop the reader/editor special-casing.
- **Papers (54,642) — the bulk, needs its own handling.** Different in three
  ways: (1) **append-only body chunks** — migration is DELETE+INSERT (which
  re-runs the embed/summary/keyword cascade at 55k scale), not in-place edit;
  (2) produced by the **Marker/PDF ingest** pipeline, *not* the LaTeX importer —
  the "stop producing `equation`" fix lives there; (3) rendered by the **two-pane
  paper reader** (`routes/papers.py`), not the draft reader. Paper equations carry
  source numbering like `\tag{A.5}`, e.g.
  `H^{+}(aq) + e^{-} \leftrightarrow \frac{1}{2} H_2, \tag{A.5}`.

**Shared work (both fronts).** A KaTeX-safe body normalizer (strip
`\label`/`\tag`, unwrap `\begin{equation}`, map `align`→`aligned`, etc. — a pure
tested `body → "$$…$$"` fn with a gold set); the numbering / `\label` / `\ref`
decision (drop, auto-number, or map to `[¶handle]`); LaTeX export of `$$…$$`
(and inline `$…$`); and `strip_markers`/card-combined handling so `$$` doesn't
break embeddings. Forward-only migration, dry-run → `--commit`, reversible until
browser-confirmed (use the Playwright-over-tunnel harness above). **Interim
alternative** if the full retire isn't scheduled: just make `equation` *render*
(wrap bodies in `$$` for KaTeX in both readers) — first-class display without the
migration.

---

## 🟢 Dark-factory build/deploy workstream

**Status**: in progress · **Severity**: feature · **Owner**: `scripts/`,
`.claude/commands/`, `CLAUDE.md`

North star: `claude -w <feature>` → describe the spec → `/go` → the change
is implemented → gated → merged → deployed, with the LLM asked only "OK?" or
handed a genuinely broken test. Every mechanical step is a script (token-cheap,
reproducible); the model spends tokens on judgment, not CI/CD plumbing.

- **`scripts/deploy` + `/go`** → **shipped this workstream.** `scripts/deploy`
  is the non-interactive ansible-redeploy backbone (twin of `scripts/ship`,
  no LLM in the loop); `/go` = `scripts/ship` then `scripts/deploy` on green
  (the one-keystroke ship+deploy). `/endsession` stays deploy-free.
- **Token-lean session boot** → **partly done.** `## Other live affordances`
  in CLAUDE.md compressed to a one-line-per-kind index (detail already in the
  `precis-*-help` skills) — ~33% fewer boot bytes. Ties into the existing
  cold-start work (`docs/design/mcp-cold-start-token-budget.md`,
  `PRECIS_STARTUP_SKILLS`). Next: apply the same discipline to the
  `~/work/cluster` CLAUDE.md; measure boot token delta.
- **`/whatneedsdoing`** → **shipped this workstream.** One triage view over the
  **two work substrates** — *repo dev work* (`OPEN-ITEMS.md` + open gripes,
  `get(kind='gripe', id='/open')`; fixed by editing this repo → `/go`) and the
  *prod factory queue* (open/doable todos, `search(kind='todo', view=…)`; the
  loop runs these on the cluster) — plus a latent-bug source: LLM-confusion
  mined from prod `plan_tick` transcripts (feeds new gripes into substrate 1).
  It keeps the substrates separate rather than flattening them, flags which
  todos are autonomous vs stalled, and names the *bridge* — a prod todo failing
  because of a repo bug.
- **Backlog groomer (close the loop) — gripe slice shipped; OPEN-ITEMS half open.**
  The dark-factory move: promote declared repo dev work into the acting queue so
  `dispatch` builds it, bridging repo dev work *into* the prod factory queue.
  - **Gripe side → shipped.** `workers/backlog_groom.py` (`run_backlog_groom_pass`)
    mints one `kind='todo'` per open gripe carrying
    `meta.executor='claude_inproc'` + `meta.job_type='fix_gripe'` +
    `meta.params={'gripe_id': N}`, hung under a `level:strategic` groomer root
    (find-or-create, so children aren't nursery orphans). Deduped on
    `meta.params.gripe_id` (no re-mint even after the fix todo is done);
    `no-groom` open tag is the human opt-out; cadence-throttled
    (`backlog_groom:last_run`, `PRECIS_BACKLOG_GROOM_REFRESH_HOURS` default 6) +
    single-runner `pg_try_advisory_xact_lock`. Registered **default-OFF** in
    `cli/worker.py` (`--only backlog_groom` / `PRECIS_BACKLOG_GROOM_ENABLED=1`) —
    enabling it starts handing repo bugs to `claude_inproc`, a deliberate flip
    like the classifier. Tests: `tests/test_backlog_groom.py` (incl. the
    end-to-end hand-off — the groomed todo is a valid `dispatch` candidate that
    mints a `fix_gripe` job).
  - **OPEN-ITEMS half → open (residual, filed 2026-07-06).** Not groomed, for two
    concrete reasons: (1) `OPEN-ITEMS.md` lives at the repo root and is **not**
    packaged into the installed wheel, so a deployed worker can't read it — needs
    a packaged/DB-backed source of the backlog first; and (2) there is **no**
    `build_feature` job_type for a free-text feature item to hand off to
    (`fix_gripe` is gripe-specific) — needs a build executor. Both are
    prerequisites before a feature item can become a dispatchable todo.
  - **Activation (ops, when ready).** Flip `PRECIS_BACKLOG_GROOM_ENABLED=1` on a
    system-profile worker to start draining open gripes into `fix_gripe` todos;
    watch the first pass's mint count + the fixer's throughput before widening.
  Pairs with `/checklogs` + cheap-model tiering.
- **Post-ship residual follow-through** → **shipped this workstream.** `/go`
  and `/endsession` now end with a tiered follow-through step: after a green
  ship, harvest the latent bugs the session parked — gated to **Opus-4.7+
  finders** (this session or an opus reviewer memory; nursery-SQL / haiku
  findings are filed, not chased) — persist them durably (so they survive the
  harness's self-compaction), fix the in-reach ones in their own worktree→ship
  cycles now, and file the investigations as todos/gripes. The "file the rest"
  half feeds the Backlog groomer above; the "fix now" half is the in-session
  interim until that groomer lands.
- **`/testfeature <prompt>`** → open. Agent loop that exercises the precis MCP
  surface (`scripts/exercise-mcp` is a seed), finds bugs, applies fixes, then
  `/go`. Bounded by a turn/cost cap.
- **`/checklogs`** → open. Read the recent LLM-error surface (prod `agentlog` +
  `alert` + failed `kind='job'` + error `ref_events`; local `.claude` logs +
  `/var/log/precis-worker-agent.log`), cluster the top-N recurring failures,
  fix root cause, `/go`.
- **Cheap-model tiering** → open. Route mechanical LLM work (`llm_summarize`,
  triage children, CI-fix escalation) to a small 4B–14B model; reserve Opus for
  build/planner/reviewer judgment.
- **Out-of-band DB-liveness monitor** → open (ops-observability). The
  2026-07-05 ~03:00→11:00 prod outage (caspar's Postgres/pgbouncer host
  degraded from ~22:00, flapped, then died) ran **~8h completely unalerted**:
  every alerting path we have (`nursery` → `kind='alert'`, `precis-heartbeat`)
  is **DB-backed**, so when the DB itself dies the alerter can't fire — and
  can't even write the alert saying it's down. Blind spot: total DB death is
  invisible to the system's own monitoring. Needs an external liveness check
  that does **not** depend on `precis_prod` being up — e.g. a tiny watcher on a
  different host (or the fixer host / a cron on the laptop) that `SELECT 1`s the
  pgbouncer endpoint every N min and pushes to Discord/PushNotification on
  failure. The precursor was visible ~5h early (per-host `worker_logs` volume
  halving from 22:00), so a degradation trend-alarm is a cheap second signal.
  Pairs with `/checklogs`.
- **Widen `scripts/ship` auto-fix surface** → open (polish). Auto-fix + amend
  anything the gate can resolve without judgment (import sort, trivial mypy
  stubs); only real logic failures reach the model.

Deferred (revisit later): **holdout scenarios** (StrongDM-style anti-overfit
eval outside the repo — not needed while Opus shows no test-gaming; ADR 0047
gold sets are the seed); **digital-twin fidelity** (richer stubs so
green-in-twin/red-in-prod gaps close — the current `FakeStore`/`MockEmbedder`/
`PRECIS_CLAUDE_BIN` twins are good enough for now); **auto-deploy as a daemon**
(vs `/go`-chained — only if chaining proves insufficient).

## 🟠 Worker liveness + observability (2026-07-05)

**Status**: slice 1 shipped · **Severity**: critical (was a silent 1.5-day
outage) · **Owner**: `workers/nursery.py`, `cli/worker.py`, `alerts.py`,
cluster repo · **Origin**: the mofs-for-electrodes plan_tick stall — melchior's
agent worker (only host running `plan_tick`) was jetsam-culled ~50-200×/day
under llama.cpp wired-RAM pressure, orphaning every in-flight tick, invisible
for 1.5 days because nothing watched daemon health.

**Slice 1 — observability → shipped + deployed (81a197c7).** Boot-event row at
`cli/worker.run`; nursery `worker-restart` + `dead-worker` critical detectors;
`raise_alert` → `(ref_id, is_new)`; one-shot `notify_critical_alert` → Discord
webhook `PRECIS_OPS_ALERT_WEBHOOK`. Tests in `test_nursery.py` / `test_alerts.py`.

### Residuals — docx / EndNote export session (filed 2026-07-05, Opus-authored)

Shipped this session: docx paper theme (black Times New Roman, 1-in margins);
docx handle-citation resolution fix (`[pa<id>]`/`[pt<id>]`/`[fi<id>]` — imported
drafts were exporting with **no** References section); and native EndNote CWYW
export (`precis/export/endnote.py`, `citations=endnote` / `?citations=endnote`)
emitting `ADDIN EN.CITE` + `EN.REFLIST` + `EN.*` doc-vars with the full record
as a traveling library. Format reverse-engineered from a real EndNote sample and
independently confirmed by web research (Journal Article=17, DOI in
`electronic-resource-num`, one field/cite, `EN.REFLIST` is a marker, traveling
library reformats with no library open). **Also shipped** (f1b6f82f): `[pc<id>]`
chunk citations embed that chunk's exact text as the record's `<research-notes>`
(traveling provenance, per-cite-site not per-paper) via `Store.chunk_text_by_id`
— on nanobuds 54/93 cites carry their source passage.

- **EndNote round-trip is validation-pending (not a code bug).** The CWYW format
  is undocumented/version-sensitive; correctness can only be confirmed by opening
  the export in real Word+EndNote and running "Update Citations and Bibliography".
  Reto has Word+EndNote and is testing; a sample was generated straight off prod
  via the `PRECIS_DATABASE_URL` secret (rewrite `host.docker.internal`→`127.0.0.1`)
  + `export_docx(citations='endnote')`. If it doesn't reformat cleanly, likely
  culprits: per-document output-style storage (publicly undocumented — recipient
  may need to pick the style once in EndNote's Word toolbar), or `db-id` collision
  with an open library.
- **`EN.Layout` style is hardcoded to `"Annotated"`** (`endnote.install_document_vars`).
  Fine as a default (recipient can change it), but a numbered/IEEE default might
  suit a references-heavy manuscript better. Make it a param if requested.
- **docx `[dc<id>]` cross-refs render as plain surface text, not Word
  cross-reference fields** (the LaTeX exporter emits `\cref`). Pre-existing
  fidelity gap, low priority — bare `[dc]` with no surface text still renders
  nothing. A real Word `REF`/bookmark cross-ref field would close it.
- **Cited-passage embedding — SHIPPED (f1b6f82f), round-trip still unverified.**
  `[pc<id>]` cites now carry the chunk text as `<research-notes>`. Caveat holds:
  EndNote **drops** Abstract/Notes/Research-Notes when a traveling library is
  imported into a real library, so the passage is visible in the field data +
  survives a reformat-in-place, but does **not** persist into the recipient's
  library. If persistence is wanted, retry with a `<custom1>` field (may survive)
  — needs the same Word+EndNote round-trip test. `pa<id>` ref-level cites carry no
  passage (correct — no chunk). Cap `_NOTE_MAX_CHARS=4000` per note.

### Residuals (filed 2026-07-05; all Opus-authored this session — harvest-eligible)

- **Activate the page (ops, in-reach).** The critical push is dark until
  `PRECIS_OPS_ALERT_WEBHOOK` is set on the system-profile workers (ansible env,
  cluster repo). Until then worker-restart / dead-worker alerts only land in
  `/alerts` — visible but not proactive. Set it to actually get paged.
- **#2 Tier B — lease as the single job-substrate liveness authority.** Today
  two clocks: `claim_executor_jobs` re-claims only `STATUS:queued` (lease-keyed),
  so a crashed `STATUS:running` job is unreachable and only the sweeper's
  independent `PRECIS_STUCK_JOB_HOURS` clock rescues it (fail→bubble→retry). Let
  the reclaim path take over a `running` job whose lease has expired
  (requeue-from-checkpoint: `meta.coordinator_state` for the coordinator, fresh
  tick for plan_tick), then **retire the sweeper's hours clock** — lease becomes
  sole authority. Needs a per-job attempt cap (the sweeper's terminal-fail is
  today's backstop). Riskier; gated behind slice 1 (now shipped) so misbehaviour
  pages. Owner: `executors/_common.py`, `sweeper.py`, `executors/coordinator.py`.
- **#3 short — de-SPOF the agent worker.** `plan_tick` jobs are NOT node-pinned
  (`claim_executor_jobs` node gate is null for `claude_inproc`); the melchior-only
  confinement is purely operational (hermes `~/.claude` OAuth + `PRECIS_MCP_CONFIG`
  live only there). Provision a **second agent host** (caspar/balthazar) with the
  OAuth state + an agent-profile daemon → one worker dying no longer stalls all
  planning. Ops/ansible, no code. Highest-value #3 move.
- **#3 medium — co-location relief.** Get the ~73 G `mlock`'d llama.cpp weight off
  the agent host (or drop `--mlock`/cap it) so jetsam stops targeting the worker.
  `ProcessType=Interactive` (shipped on cluster `master` 7e1258f) is a mitigation,
  not immunity. Ops.
- **#3 long — sandbox substrate.** The `sandbox_run`/`claude_docker` substrate
  (ADR 0048, `docs/proposals/sandbox-run-substrate.md`) runs ticks in isolated
  containers, immune to host memory pressure and naturally multi-host — subsumes
  both the SPOF and co-location. Big lift; the durable north star.
- **Config-drift guard (cluster repo).** The `ProcessType` fix regressed once
  because it sat on an unmerged branch while deploys render from `master`. Add a
  deploy assert that deployed launchd plists match the rendered templates (analogue
  of the existing venv-commit convergence assert). Owner: `redeploy-precis.yml`.
- **Convergence assert races the autonomous fixer → FIXED** (cluster `master`
  `3ff4fc2`, 2026-07-08). The install, the pre-flight gate, and the convergence
  assert each re-sampled `git ls-remote origin main` independently, so a commit
  landing mid-deploy (the hephaestus fixer, a sibling `/go`) left the venvs on the
  sha they installed while the assert compared against a NEWER HEAD → spurious
  "DEPLOY DID NOT CONVERGE" on a uniform cluster (hit 2–3× per deploy while the
  fixer was active; a quiet-window re-run converged). Fix: `redeploy-precis.yml`
  step 0 resolves the ref to ONE commit via a single `run_once` `git ls-remote`,
  broadcasts it to all hosts, and pins the three `precis_*_git_ref` install vars +
  a `precis_target_sha` compare target to it; install (`@<sha>`), pre-flight, and
  assert all use the frozen sha, so a `main` that advances under an in-flight deploy
  can't false-fail it. `-e precis_worker_git_ref=<branch>` still wins. Validated
  live: deployed cleanly first-try *while* the fixer was shipping (`main` moved
  `6c9c8a01`→`aa74b0d1` mid-run), pinned + converged on all 4 hosts, `failed=0` —
  the exact condition that needed a manual re-run twice before, now green in one
  pass. Retires the old workaround ("confirm your sha is an ancestor of
  origin/main and re-run"). Owner: `redeploy-precis.yml` (cluster repo).

## 🟢 Chunk-tag classifier (ADR 0047) — remaining work

**Status**: open · **Severity**: feature · **Owner**:
`src/precis/workers/classify.py`, `src/precis/data/axes/`, cluster env

The `junk`→`ROLE3` cascade is **shipped + deployed + validated** (worker
pass ran green on melchior, `claimed=16 ok=16 failed=0`; 1,521 `ROLE3`
tags on prod from the bounded backfill). Design:
`docs/design/chunk-classifier-cascade.md`; numbers: `scripts/classify/
EVAL_RESULTS.md`. What's left:

- **Enable continuous corpus tagging** — the worker pass is deployed
  **default-OFF**. Flip `PRECIS_CLASSIFY_ENABLED=1` on the system-worker
  daemon (melchior, or cluster-wide) to drain the remaining ~1.29M chunks
  on the free `summarizer` model. Deliberate large backfill; watch load.
- **Tier-2 escalation (optional)** — set
  `PRECIS_CLASSIFY_ESCALATE_MODEL=claude-haiku-4-5` to re-judge `own`
  chunks and push own-claim precision past 91%. Was HTTP-429 blocked during
  dev (proxy Anthropic quota); retry when free. Cost tradeoff, ~$200-400 on
  the residual vs ~$1.3-2.6k all-haiku.
- **Ref-axis production runner (`classify-papers`)** — not built. Only
  `material` (93%) and `transport` (97%) clear the gate on the free model;
  `domain`/`studytype`/`property` need a stronger model. Walk `paper` refs,
  apply `applies_when` gates, write ref tags + `meta.processing.<axis>`.
- **Better table detection (polish)** — the free Tier-0 `numeric_ratio`
  heuristic catches only 0.1% (tables aren't digit-dense; labels+spaces).
  Tables currently fall to the LLM (handled, but not free). A pipe/tab/
  repeated-token heuristic would recover the ~free furniture drop.

## 🔵 `serverInfo.title` not set

**Status**: blocked on upstream `FastMCP`
**Severity**: polish
**Owner**: `src/precis/server.py:129`
**Test**: `tests/test_server_init.py::test_serverinfo_carries_title`

MCP spec 2025-06-18 §A1 recommends a human-facing
`serverInfo.title` alongside the machine name. Today's
`FastMCP("precis-mcp", instructions=_INSTRUCTIONS)` constructor
takes no `title=` kwarg — we get `serverInfo.name = "precis-mcp"`
and no `title` field. One-line fix once `FastMCP` accepts
`title="Precis"`. Track upstream:

- https://github.com/modelcontextprotocol/python-sdk/issues — file
  the request when the next mcp-critic pass surfaces it again.

## 🟠 LLM-confusion bugs mined from prod plan_tick transcripts (2026-07-03)

Mined 48h of `kind='job'` `meta.transcript` on `precis_prod`: **702**
`[error:*]` tool-call errors, 544 `BadInput`. Two clusters. The **tex
workspace-authoring** cluster (the top ~450 errors) is **fixed on this
branch** (`worktree-serverconfusion`): `put(mode='find-replace')` now
redirects to `edit`; the "unknown view" error suggests the `--` slug form
when an extensionless path collapsed into a view; the slash-in-`name=`
error tells the LLM to pass the bare slug; `precis-tex-help` now documents
the workspace `name=` form + the load-bearing extension. Remaining:

- **DONE — extensionless slash-path collapse (root fix).** `_parse_file_id`
  now takes the handler's `_SUPPORTED_VIEWS`: when a slash-path's tail isn't a
  real view it's encoded to its `--` slug (`tex/graphene` → `tex--graphene`,
  `projects/x/tex/graphene` → `projects--x--tex--graphene`) via
  `file_slug_from_path`, so it addresses the file instead of splitting into a
  bogus view. `slug/raw`,`slug/toc` still resolve as views; an unsupported
  view via the explicit `view=` kwarg still raises `Unsupported`. Regression
  tests added (tex/plaintext/markdown).

- **DONE (A1) — bare-numeric paper id ref_id fallback.** `resolve_live_slug_ref`
  now resolves a bare all-digits id as the kind's `ref_id` for slug-addressed
  kinds (paper/draft/tex/…) and emits a `warn` admonishing the agent to use the
  `pa<id>` handle and never write bare numbers into cited text. (The intended
  addressing already existed: `pa1876` is the ADR 0036 handle; `get(id='pa1876')`
  works with no `kind=`; `kind='pa'` is an alias.)

- **DONE (B) — merged-duplicate handles now redirect (universally).**
  `reconcile` already stamps `meta.superseded_by` on the loser;
  `Store.follow_supersede` + `resolve_handle` + `parse_link_target` transparently
  follow it to the live survivor (chains capped/cycle-guarded). The redirect
  hint now fires from the **store layer**: `Hub.__post_init__` wires
  `store.hint_bus = hub.hints`, so `resolve_handle` emits the "please use the new
  handle" nudge on **every** path (get, all `link=` incl. `apply_link_ops`,
  `exclude=`, citation `source_handle`) with no per-callsite `hub` threading. The
  A1 admonish moved to the same bus. Residuals cleared: `apply_link_ops` covered
  (via the store bus); citation `source_handle`'s paper-existence check now
  follows supersede too.

- **P0 operational: `nanotrans_auto` planner spin — root cause found.** One
  plain-tex-workspace project re-minted **47 `plan_tick` ticks in 48h** since
  2026-07-01, creating orphaned duplicate `\section{…}` refs (`workspace=∅`)
  every tick while `latexmk` stayed broken. **Root cause:** every tick exits
  `STATUS:succeeded` with **no** `resume_reason` / `resume_streak` — the
  coroutine "succeeds" (verdict: continue) each tick but never converges
  because tex authoring kept failing. The resume-streak cap
  (`meta.plan_tick_resume_streak`, default 3) only guards *exhaustion*
  (max-turns/timeout) loops, **not** clean-but-unproductive ticks, so nothing
  bubbled. **Immediate fix:** the tex authoring fixes on this branch let the
  LLM actually write the sections → the task progresses; verify after deploy.
  **Defense-in-depth — DONE:** nursery now has a `plan-tick-spin` detector — a
  parent minting > `PLAN_TICK_REMINT_24H` (16) `plan_tick` jobs in 24h raises a
  `warn` `kind='alert'` (`nursery:plan-tick-spin`), mirroring the `ref_events`
  spin-loop detector, so a stuck planner surfaces even though the resume-streak
  cap can't catch a clean-but-unproductive loop.

- **DONE (ops) — redeploy embedder-warmup race.** `scripts/deploy` failed once
  per run on whichever host's bge-m3 was mid-warm when the `/healthz` gate
  checked. Fixed in `~/work/cluster`: the `Install precis-mcp[embed]` git-pip
  task now retries (3× / 10s) so a transient git/wheel hiccup on one host doesn't
  fail the whole redeploy; the `/healthz` gate windows widened 40→80 (≈4 min) on
  both macOS + Linux, and the embedder-role probe 10→20 (≈1 min), covering a cold
  warm on a slower Mac.

### Residuals parked from the 2026-07-04 session (persisted; not in-reach fixes)

The confusion-mining root causes are all fixed + deployed. These remain — none
is a bounded correctness fix, so they're filed, not chained:

- **Chunk-handle (`pc<id>`) of a merged paper doesn't redirect** (design
  limitation, not a bounded fix). `resolve_handle` follows `superseded_by` for
  *record* handles (`pa<id>`) only; a merged paper's chunks are soft-deleted and
  the survivor has *different* `chunk_id`s, so there's no clean chunk→chunk
  remap. Low frequency (link/handle to a merged paper's specific chunk). A real
  fix would need a chunk-level supersede mapping at merge time — investigate
  before building.
- **`plan-tick-spin` detects but doesn't auto-pause** (behavior extension). The
  new nursery detector *surfaces* a spinning planner as an alert; it doesn't halt
  the parent, so it keeps burning ticks until acted on. Auto-pausing (e.g. an
  `open` tag the doable view excludes, like `child-failed`) would stop the burn —
  but risks halting legitimate long-running planners and needs a
  progress-signal, not just a count. Backlog, not this session.
- **Ops: cull orphaned tex refs from the nanotrans_auto spin.** The spin created
  dozens of duplicate `\section{…}` refs with `workspace=∅` (never attached to
  the project). Prod data hygiene — a one-off cleanup query, not a repo bug.

## 🔵 Tool-friction reflection + dream diversification (2026-07-04)

Spec: [`docs/design/tool-friction-reflection-and-dreams.md`](docs/design/tool-friction-reflection-and-dreams.md).
Idle-time self-improvement. Part A + the Part B lens seed are **built**
(`utils/friction_reflect.py`, `utils/dream_seed.py`); the rest is filed.

- **Part A — end-of-run tool-friction reflection → BUILT, default-OFF.**
  `utils/friction_reflect.py` appends a terminal binary-first "did any
  tool get in your way?" footer to `--append-system-prompt` at the
  `utils/claude_agent.py` chokepoint, gated on `PRECIS_FRICTION_REFLECT`
  + MCP present + `--max-turns >= 8`. "friction: none" is the honored
  default; a genuine fumble files one `friction`-tagged gripe. Ships
  **off** because once on it rides *every* production agentic run
  (planner/reviewers/dream) — enable deliberately, like the classifier.
  Residuals below.
- **Part B lens seed → BUILT, then REHOMED (shipped d7368c28,
  2026-07-05).** The single-stance persona lenses moved out of
  `data/dream_lenses.yaml` into first-class **oracle traditions**
  (`data/oracle/{scientists,leadership,artists}.yaml`) and are drawn via
  a named **lens** policy (`utils/oracle_lens.py`): the dream's default
  `sci` lens draws 50% from `scientists` and 50% evenly across the other
  traditions (even across *traditions*, not entries). `dream_lenses.yaml`
  now holds only the Disney **process** lens (multi-phase, doesn't fit
  the oracle one-block shape); the worker runs it on a fraction of cycles
  (`PRECIS_DREAM_PROCESS_PROB`, default 0.15). Round-robin coverage gave
  way to random-with-a-diversity-floor. The oracle's randomness is now
  documented as *p-hacking made honest*. `get(kind='oracle',
  args={'lens': ['sci']})` exposes the draw on the agent surface.

### 🟢 Orphaned oracle refs from boot-time re-ingest race — FIXED (2026-07-06, Opus-authored)

**Surfaced while verifying the persona→oracle deploy (d7368c28).** Prod
carried **13 orphaned `kind='oracle'` refs** — live (`deleted_at IS NULL`),
*no* `cite_key` (so `slug=None`), each a full duplicate of a real tradition
with all its blocks. The 2026-07-05 deploy produced a clean 12; the orphans
were pre-existing debris from the **07-04 00:12** and **07-05 12:20**
oracle-corpus changes.

**Root cause — three compounding bugs (all now understood + fixed):**
1. `jobs/oracle_sync` took a **session-level** `pg_try_advisory_lock` on a
   pool connection returned immediately — through **pgbouncer `pool_mode =
   transaction`** the lock strands on a recycled backend and re-acquires
   *re-entrantly* (false success) → **zero mutual exclusion**, so all 4
   post-deploy boots re-ingested concurrently.
2. `insert_ref` attaches the cite_key with `ON CONFLICT (id_kind,id_value)
   DO NOTHING` — so a racing loser's ref + blocks commit but its cite_key
   is *silently dropped* → the slug-less orphan (rather than a safe
   unique-violation rollback).
3. `ingest_paper(overwrite=True)` did DELETE-ref + INSERT-new-ref → a fresh
   `ref_id` every corpus bump, which also **churns the `or<id>` handle and
   dangles any citation/link to an oracle entry** (an independent bug).

**Fix (shipped this session):**
- **A — real lock + atomic tx.** `oracle_sync.maybe_reingest` now takes
  `pg_try_advisory_xact_lock` inside one `store.tx()` spanning the whole
  re-ingest (transaction-scoped → pinned to the tx backend → works through
  transaction pooling; auto-releases on commit; loser bails). State markers
  write on the same tx (`_write_state_conn`) so a crash can't leave the
  marker ahead of the data. Non-PG stores (test stubs, `pool is None`)
  degrade to the direct path. Old `_try_advisory_lock` /
  `_release_advisory_lock` removed.
- **C — idempotent in-place overwrite.** `ingest_paper` now `update_ref` +
  `DELETE chunks` + re-insert under the **stable ref_id** (cite_key never
  moves). Fixes handle-churn *and* means a race converges on one row
  instead of orphaning. Tests: `test_overwrite_keeps_ref_id_stable`,
  `test_shared_conn_ingest_is_atomic`, `test_advisory_xact_lock_sql_is_valid`.
- Skipped **B** (loosening `insert_ref`'s shared `ON CONFLICT` — too broad;
  C means the oracle path never re-claims a cite_key anyway).
- **Prod cleanup done:** the 13 orphans soft-deleted 2026-07-06 (reversible
  `UPDATE refs SET deleted_at=now() WHERE kind='oracle' AND deleted_at IS
  NULL AND NOT EXISTS(cite_key)`); prod now 12 clean traditions, 0 orphans.

**Follow-up — DONE (2026-07-06).** `workers/paper_reconcile.py` used the
*same* pooler-unsafe session-`pg_try_advisory_lock` pattern (a dedicated
*autocommit* connection, so every statement was its own transaction and the
lock-holding backend recycled immediately). Converted to
`pg_try_advisory_xact_lock` held inside one open transaction spanning the
whole pass (transaction-scoped → pinned through pooling, auto-releases on
commit; the dedicated conn only holds the lock, the reconcile work runs on
the `store` pool). Non-corrupting either way (the reconcilers are
idempotent), but now the single-runner guarantee is real.

### Residuals (filed 2026-07-04)

- **Enable Part A in prod.** Flip `PRECIS_FRICTION_REFLECT=1` on the
  agent-profile worker (melchior) once the downstream grouping/dedup
  lane exists to absorb `friction` gripes — otherwise raw wishes pile
  up untriaged. Gauge junk-rate; dial `--max-turns` floor if the
  planner's budget suffers.
- **Gripe → agentlog link (Part A).** The spec wants each `friction`
  gripe linked to the run's 30-day `agentlog` (model + transcript). The
  filing agent doesn't know its own agentlog id at `put` time, so this
  needs post-hoc stitching (join `friction` gripes to agentlogs by
  time+source) — or an agentlog id threaded into the run context.
  Currently the gripe self-tags `friction-model:<model>` as a stopgap.
  Confirm too that every *eligible* run emits an agentlog to link to
  (the web follow-up path may not).
- **Dream mode rotation (Part B).** Rotate the cycle's *deliverable*
  (connection / library-gap / open-question / consolidation /
  analogy-transfer), not just the lens. Deferred: it needs
  deliverable-logic surgery on `dream-prompt.md` (the connection shape
  is currently hardcoded into Step 6). Lens rotation shipped first as
  the low-risk half.
- **Deferred — active dreams (DFT / CAD / compute lanes).** *We want
  this, not yet.* An `active-build` dream mode that kicks a derived-lane
  job (DFT relax on the GPU node, `cad_propose`, structure relax) on a
  subject its wandering surfaced, then connects the *result* back into a
  memory — turning idle time into speculative build progress. Gate
  behind the load ceiling + a budget cap; derived jobs are
  content-addressed (ADR 0044), so a re-request is a cheap cache hit.

### Residuals parked from the paper-dedup/hygiene/resolve session (shipped ea7ac1ac)

Byline search + dedup Phase 3 + `paper_reconcile` (reconcilers + hygiene heals)
+ Bucket B resolver are shipped & deployed. Follow-ups, ops-gated (not repo bugs):

- **Run Bucket B on prod.** `precis resolve-metadata` (dry-run) on-cluster over
  the 94 `needs-triage` — inspect the auto/review/discard lanes, then `--apply`.
  Network-bound (Crossref/S2), so it can't be exercised from the dev sandbox.
  Expected shape from analysis: ~20 DOI-track + up to ~40-ish title-track auto,
  the rest review/discard. Book-cruft (5) + held-without-chunks (4) print for a
  human soft-delete decision. Runs on-cluster only.
- **`paper_reconcile` first prod pass** self-heals on its 24h cadence: retires
  3 dup-of-held id-less stubs (3 more to review), rebuilds ~173 drifted cards,
  collapses 1 superseded chain, migrates 2 dangling links. Watch the first pass
  in `/var/log/precis-worker.log`; no action unless it logs failures.
- **Standing worker for future id-less stubs** (Bucket B track 2 as a pass, not
  just the one-shot CLI) — build after the CLI proves the resolution on prod.
- **id-bearing stubs that title-match a held paper (49)** are deliberately NOT
  auto-merged (an authoritative id asserts distinctness). Real merges among them
  need cross-id (S2) equivalence proof → review lane, future work.

## 🔵 Platform-specific test bugs (Windows + macOS Python 3.12)

**Status**: open
**Severity**: polish
**Owner**: `tests/test_python_handler_writes.py`,
`tests/test_python_runtrace.py`,
`tests/test_python_config_wire.py`
**CI workaround**: `continue-on-error` on the affected matrix legs
in `.github/workflows/check.yml` (Linux + macOS-3.11/3.13 still
gate the release).

**Windows** — 27 tests fail because the python-handler write path
opens directory FDs with `os.O_DIRECTORY` for fsync, and that
constant is Unix-only:

- `test_python_handler_writes.py::*` (26 tests) —
  `AttributeError: module 'os' has no attribute 'O_DIRECTORY'`.
  Fix: branch on `sys.platform`; on Windows, fall back to a
  no-op fsync (or open the parent file by handle).
- `test_python_config_wire.py::test_parse_expands_tilde` —
  test asserts `~` expands to a Linux-style path; Windows expands
  to `C:/Users/runneradmin`.  Fix: assert against
  `os.path.expanduser("~")` instead of a hardcoded prefix.

**Python 3.12 setprofile + urllib.parse circular import** — 5
runtrace tests fail because the spawned tracer subprocess raises
`AttributeError: partially initialized module 'urllib.parse' …
(most likely due to a circular import)`.  First spotted on
`/Library/Frameworks/Python.framework/Versions/3.12/`; as of
2026-05-22 also reproduces in the Linux ``precis-dev`` container's
Python 3.12.  3.11 and 3.13 are unaffected; Homebrew Python 3.12
also works.  Suspect: `sys.setprofile` hook intercepts an internal
``urllib.parse`` import during a partially-initialised module
state when the user entry triggers ``argparse`` (which lazy-imports
urllib for help-text fallbacks).  Likely fix: defer the profile
install until after ``urllib.parse`` has been imported by the
bootstrap, or run the tracer in a fresh interpreter via ``-S`` +
explicit ``site.main()``.

The five subprocess-spawning tests carry
``@pytest.mark.xfail(strict=False)`` gated on Python 3.12 so they
still execute (we notice an XPASS on a non-bugged interpreter)
but don't fail the suite on bugged ones:

- ``tests/test_python_runtrace.py::test_runtrace_captures_call_tree``
- ``tests/test_python_runtrace.py::test_runtrace_argv_is_forwarded``
- ``tests/test_python_runtrace.py::test_runtrace_collapses_stdlib_by_default``
- ``tests/test_python_runtrace.py::test_runtrace_expand_stdlib_keeps_full_tree``
- ``tests/test_python_runtrace.py::test_runtrace_max_events_truncates``

Both clusters are tracked here so we don't lose them between
release and the post-release patch window.

## 🔵 OQ-11 — verify FastMCP server-pinned-prompt support

**Status**: open (verification only; design ships either way)
**Severity**: polish
**Owner**: `src/precis/mcp_modalities.py::register_skill_prompts`
**Plan artefact**: `docs/design/mcp-cold-start-token-budget.md` §Open questions
**Test**: none yet

Phase 3 of the MCP session-ergonomics rollout
(`PRECIS_STARTUP_SKILLS`) tags pinned skills on `prompts/list` and
also surfaces them via a `Pinned skills:` line in
`serverInfo.instructions` as a belt-and-suspenders fallback. The
question is whether MCP 2025-06-18 + FastMCP 1.x lets a server
flag a `prompts/list` entry as "render at session start", or
whether the tag is purely a client-side convention.

Action: read FastMCP source for `prompts/list` handler shape,
read MCP 2025-06-18 §prompts. Either way the design ships — the
banner notice carries the discovery channel — but the answer
determines whether we can stop carrying the redundant banner
line in a future cleanup.

## ⏸️ Snoozed — blocked upstream (recheck dates)

Real but unactionable until an upstream unblock. Each entry carries a
machine-parseable `Recheck-after: YYYY-MM-DD` and an `Unblock-when:`
condition. `/whatneedsdoing` reads this section and **suppresses** a
matching Dependabot alert until its recheck date, then resurfaces it as
"recheck due" for a re-probe (act, or re-snooze +2 weeks).

- **Dependabot #44 — `transformers` <5.3.0 RCE (high).**
  `Recheck-after: 2026-07-18`.
  `Unblock-when:` `marker-pdf` drops its `transformers>=4.45.2,<5.0.0` cap so
  that `transformers>=5.3.0` resolves. Today **every** `marker-pdf` (≤1.10.2)
  pins `transformers<5.0.0`, and precis needs marker in the `[paper]` extra,
  so `transformers>=5.3.0` is **unsatisfiable** — `uv lock --upgrade-package
  transformers` stays at 4.57.6, and forcing `>=5.3.0` makes the whole
  resolution fail. So the fix requires bumping **both** transformers *and*
  marker; it cannot land as a lockfile bump alone.
  **Why it's tolerable meanwhile:** the exploit surface here is ~nil — precis
  only ever loads the trusted local **bge-m3** embedder, never a user-supplied
  model path or `trust_remote_code`, which is what these `transformers` RCEs
  require.
  **When it unblocks** the bump is a *major* 4→5: validate a sample re-embed
  for cosine drift before trusting mixed old/new vectors, and if material,
  re-embed via an embed-model-version bump so the `embed` worker re-claims the
  corpus (keywords self-heal the same way via `KEYWORDS_VERSION`). Stored rows
  are never corrupted by the bump — the only risk is old-vs-new vector
  comparability.
  **Recheck procedure (on/after the date):** re-run `uv lock
  --upgrade-package transformers`; if it now reaches ≥5.3.0, take the fix →
  `/go`; if still capped by marker, bump `Recheck-after` +2 weeks.


## 🔵 Paper-ingest `equation` chunk kind — retire later (2026-07-05)

**Status**: deferred · **Severity**: feature · **Owner**:
`ingest/marker.py`, `ingest/pipeline.py`, `ingest/literature.py`

Companion to the **draft** equation-kind retirement (drafts→`$$…$$`
paragraphs, done on `worktree-mission-doc`). The draft work deliberately
left the **paper** side alone. Prod split as of 2026-07-05: **54,636**
`equation` chunks belong to `kind='paper'` (99.5%) vs only 278 to drafts —
so the `equation` `chunk_kind` is overwhelmingly a *PDF-ingest* artifact,
minted by the Marker path (`ingest/marker.py:_classify` → `pipeline.py:99`
Marker-type map), not the draft importer.

**Why it wasn't folded into the draft retirement:**
- **Different reader.** Papers render as the two-pane **PDF** reader
  (`routes/papers.py` + pdf.js), *not* the prose/chunk reader — so the
  "equation renders as raw `<p>`, not KaTeX" motivation doesn't apply to
  papers at all.
- **Deliberately un-embedded.** `ingest/literature.py` lists `equation` in
  `SKIP_EMBED_TYPES` ("LaTeX/MathML doesn't embed well with text models"),
  so paper equation chunks carry NULL embeddings by design. Migrating 54.6k
  of them to `paragraph` would either leave odd un-embedded paragraphs or,
  if embedded, dump 54k LaTeX blobs into the search index — a retrieval
  regression + a large embed load. Reopening that requires deciding the
  embed policy first (strip-to-placeholder? keep skipping? a `math`-marker
  paragraph the embedder skips?).

**If/when taken:** decide the paper-equation embed policy, change the Marker
ingest classification + `SKIP_EMBED_TYPES`, batch-migrate the 54.6k paper
chunks (throttle any cascade), then the `equation` slug can finally be
`deprecated_at`-stamped in `chunk_kinds` once *no* live chunk of any owner
kind carries it. Until then the FK row stays alive for the paper path.


## 🔵 CAD — spoked-wheel spokes don't bridge rim↔hub + no job-log link on the page (2026-07-06)

**Status**: open · **Severity**: feature · **Owner**: `cad/` (geometry
authoring / connectivity), `precis_web/routes/cad.py` (job-log link)
· **Reported on**: `/cad/make-a-spoked-wheel-with-a-mounting-bracket-v2`

Two separate issues surfaced from one CAD page:

1. **Spokes don't connect the rim to the hub.** In the renderer, the glTF,
   and the exported SCAD each spoke penetrates the rim and sticks out both
   sides while never reaching the hub — it reads as "a ring with spikes,"
   not a wheel. The connectivity lint agrees: *"2 disconnected bodies:
   wheel+bearing | hub."* The model's spoke op is
   `spoke  cyl:r2.5h28  polar n16 r26 z` (16 spokes, radius 26, axis z),
   but the rim is `torus:R40r6` (major radius 40) and the inner hub is
   `cyl:r12h16` — so a spoke centred at r=26 spanning ±14 reaches neither
   the rim wall (~34–40) nor the hub outer wall (12). This is a
   model-parameterisation problem (the edit-by-prompt / propose step
   authored geometry that doesn't span the gap), possibly worth a
   spoke-radial-length lint or a connectivity check fed back into the
   propose loop so a disconnected result is caught before it lands.
2. **No link to the failing job from the CAD page.** The page shows
   "answer failed — see the job log" (job r50911) but renders no link to
   that job, so there's no click-through to the forensics. The CAD route
   should surface a link to the owning job (`/cad/<slug>` → job r50911's
   log) when a propose/derive step fails.

---

## 🔵 OA acquisition + structured ingest + external search (2026-07-06)

**Status**: open (roadmap; nothing built) · **Severity**: feature · **Owner**:
`workers/fetch_oa.py`, `ingest/`, search/discovery layer

Root diagnosis from three "it's OA but we don't have it" reports
(`10.1002/open.70197` ref 50597, `10.1101/2024.09.13.612990` ref 50559,
`10.1126/sciadv.adx3969` no stub). All three are genuinely OA; the common
wall is **publisher-side Cloudflare/anti-bot `403`** on `onlinelibrary.wiley.com`,
`biorxiv.org`, `science.org` — the fetcher's `_BROWSER_UA` doesn't pass. The
aggregators either expose no direct PDF URL or point at the Cloudflare-gated
landing page. This is why Reto pulls them by hand via the UoL library proxy.
**Key discovery:** 2 of the 3 are in the **PMC OA subset** (Wiley→PMC13130153
CC-BY, Sci Adv→PMC12787524 CC-BY-NC) — freely + legitimately downloadable from
NCBI/EBI infra with **no Cloudflare**. So the biggest win needs no proxy and no
librarian. (Sandbox egress blocked FTP/some HTTPS here; prod cluster nodes have
open egress — the existing `europepmc` leg already succeeds 104× there.)

Interdependent items (the structured-ingest ones ride on the fetch legs):

1. **PMC OA / Europe PMC fetch leg** *(keystone — do first).* DOI→PMCID
   (`pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/`) → PMC OA service
   (`.../oa/oa.fcgi?id=PMCID`) → download the OA package (`.tar.gz`: JATS XML +
   figures + **supplementary**) or `oa_pdf`. Order it *ahead* of the
   Cloudflare-gated legs. Fixes the current `europepmc` leg's flaky
   `?pdf=render` path. Immediately lands ref 50597 + a to-be-acquired sciadv stub.
2. **bioRxiv/medRxiv S3 leg** — for `10.1101` preprints not in PMC: bioRxiv API +
   AWS `s3://biorxiv-src-monthly/` (requester-pays). Note ref 50559's VoR *is*
   the Sci Adv paper (#1 covers it) → add preprint→VoR dedup.
3. **Paid Web-Unlocker proxy leg** — last resort for Cloudflare-only-OA not in
   PMC/S3 (Zyte API / ScraperAPI / Bright Data / Oxylabs; pay-per-success,
   real-browser fingerprint). Config-gated, **off by default**. ToS caveat: this
   evades bot protection — defensible for CC-licensed content we're entitled to,
   never for paywalled. **No Sci-Hub** (copyright infringement; won't build on it).
4. **Supplementary / methods ingestion** — the PMC OA `.tar.gz` already bundles SI;
   design the storage shape (child refs linked `has-supplement` vs extra chunks
   under the paper). Same embed pipeline.
5. **JATS/XML structured ingest** — clean seam: a `extract_blocks_jats(xml, paper_id)`
   emitting Marker's block-dict shape (`{node_id,page,type,text,section_path,…}`,
   `marker.py:415`) reuses the whole downstream (`_blocks_to_chunks` →
   `_retag_references` → `_build_cards` → `write_paper`) + the NULL-embedding
   cascade + `mathnorm.normalize_math()` for MathML→`$$…$$`. **Phase 1** (new
   papers, prefer-XML, keep PDF for the reader/`pdf_sha256`) is low-risk.
   **Phase 2** (re-ingest existing PDF-Marker papers) is a registered *reversible*
   ref-pass — **hazard:** citations anchor by string `source_handle="slug~ord"`
   in `refs.meta` (not an FK), so a re-chunk restales them; must **reanchor by
   `source_quote` text**, snapshot old chunks at *ref* scope (chunk_events cascades
   away), add an `ingest_source` marker column (none today), and gate/prioritize
   (re-embed cost). **Phase 3** — stable per-chunk `handle` + citation-by-quote so
   re-chunks stop destabilizing anchors. Wrinkle: JATS has no pages → synthetic
   `page_first/last`, coarser PDF-highlight anchoring.
6. **Parallel scholarly-graph providers** — S2 alone under-covers chemistry/materials
   citation edges. Fan out to `{OpenAlex, Crossref, OpenCitations, Europe PMC, Lens}`
   and **RRF-fuse** (rank-based → robust to cross-lingual score gaps), dedup by
   DOI→title-fuzzy. OpenAlex + Crossref clients already exist (fetch legs);
   promoting them to *search/graph* providers is low effort. Lens adds paper↔patent
   linkage (synergizes with `patent` kind). Two modes: discovery-search + citation-
   graph-edge-union. Same fan-out-and-fuse subsystem as #7/#8.
7. **Chinese-lit abstract discovery** — scope to *abstract-level* discovery via
   OpenAlex/Crossref (they index Acta Chimica Sinica 化学学报 etc. with Chinese
   abstracts + DOIs) + translation; **not** CNKI full-text scrape (paywalled,
   anti-bot, low ROI — the frontier is English-first per Reto's research).
8. **Historical & foreign-language archive import** — distinct *bulk, scan-derived,
   identifier-less* class (not per-DOI). Parts: bulk fetcher (Internet Archive
   `internetarchive` lib / HathiTrust / J-STAGE) · **copyright-era gating** (pre-~1930
   PD = full-ingest; in-copyright = index/abstract-only) · **specialized OCR tier**
   (Fraktur/Cyrillic/CJK; prefer IA hOCR, re-OCR on low confidence) · historical
   cite-key identity (vol/page/year, title-fuzzy fallback). **Pilot: German
   *Chemische Berichte* (1868–1997) via IA + HathiTrust** — largest coherent PD run,
   OCR done, direct ancestor of MOF SBU/single-site catalyst theory. Legit routes
   only (IA/HathiTrust/J-STAGE); **no Sci-Hub**; CNKI = East View / institutional.
9. **Measure bge-m3 cn↔en placement for technical content** *(Reto's explicit
   ask — measure, don't assume).* bge-m3 is genuinely strong at en↔zh (BAAI =
   heavy Chinese training) and already multilingual, so cross-lingual retrieval
   works *in principle* without translating the corpus — but two open questions
   for *technical* content: (a) specialized chemistry jargon is thinner in
   multilingual pretraining; (b) the language-clustering bias systematically
   lowers cross-lingual cosine, so relevant zh chunks can be pushed below en in a
   single fused list (RRF-per-language-pool mitigates). **Probe:** hit the live
   embedder (`POST /embed`, port 8181, `embedder_service.py`) with N zh technical
   abstracts + their English equivalents; report cross-lingual vs same-language
   cosine gap + top-k retrieval of the zh chunk against distractors. Numerics/
   formulae/Latin-script terms are language-agnostic anchors and should help.

Suggested next step: one `docs/design/` roadmap doc capturing all nine with the
dependency graph (structured-ingest → fetch legs; #7/#8/#9 share the multilingual
layer), then build #1 (the keystone).

### 2026-07-08 update — MDPI/Akamai wall, OpenAlex Content co-keystone, the bulk arm

New evidence from a batch of "failed to ingest" reports (refs 53423, 53481,
53495, 53533, 53536, 53537 — mostly MDPI `10.3390`, plus IOP/APS). **Diagnosis
confirmed + sharpened, and the keystone re-ranked.**

**A. The wall is publisher-agnostic and PMC does NOT rescue MDPI.** MDPI's PDF
host `www.mdpi.com` is behind **Akamai** bot management (error page cites
`errors.edgesuite.net`), returning a hard `403 Access Denied` to *every* client
we can field — polite UA, full Chrome UA, complete browser header set + HTTP/2 —
from both a laptop IP and cluster nodes (caspar/melchior). It is TLS/fingerprint/
IP-reputation, **not** a User-Agent gate, so the `_BROWSER_UA` idea is dead for
this class. Worse: MDPI *Chemosensors* (53423) is **not in PMC** ("Identifier not
found"), **not in Europe PMC** (0 hits), CORE returned a bogus id, and DOAJ only
points back to the blocked MDPI page. So roadmap **#1 (PMC-OA leg) whiffs on the
MDPI inflow entirely** — PMC is biomedical, MDPI's chemistry/materials/eng
journals aren't indexed there. We need a route that isn't the publisher host and
isn't PMC.

**B. OpenAlex Content API is that route — verified against 53423, promote to
co-keystone (new roadmap #1b).** OpenAlex caches full-text content and serves it
from `content.openalex.org`, *not* the publisher. For 53423 the keyless metadata
already advertises it:

```
has_fulltext: True
has_content: {'pdf': True, 'grobid_xml': False}
content_urls.pdf: https://content.openalex.org/works/W4386410574.pdf   (401 w/o key)
```

So the leg is: read the (free, keyless) work metadata → check `has_content` →
if `grobid_xml` fetch the **TEI** (structured; feeds #5), elif `pdf` fetch the
**PDF** (Marker path) — both from `content.openalex.org`, a fixed trusted host
(safe_fetch still applies). **This kills the whole Akamai/Cloudflare-403 class in
one publisher-agnostic leg** instead of the per-publisher whack-a-mole of #1/#2/
#3. Key-gated (`PRECIS_OPENALEX_API_KEY`) + ~$0.01/file, but gated by
`has_content` so we only pay on a hit. **Cascade order:** free legs first
(publisher-deterministic → PMC-OA JATS → arXiv → Crossref/OpenAlex `oa_url`, all
$0, version-of-record), then **OpenAlex Content as the first *paid* fallback,
ahead of the web-unlocker proxy (#3)** which is costlier and ToS-greyer. Gate the
paid leg with a per-pass budget cap (mirror the cost caps on the agent legs). A
later optimization: when the only `oa_url` is a known-blocked host
(mdpi/wiley/science), short-circuit straight to the Content leg.

**C. XML/TEI vs PDF — the decision (generalizes #5 Phase 1).** OpenAlex content is
**PDF and/or GROBID TEI** — *not* publisher JATS and *not* LaTeX source.
- **Prefer TEI for text/chunks/embeddings when present; still store the PDF.**
  TEI → the `extract_blocks_*` seam (#5), skips the expensive/GPU/OCR Marker
  step, gives clean `section_path` + MathML→`$$` via `mathnorm`. But GROBID TEI
  is itself PDF-derived (parse errors happen) and **has no page geometry**, so a
  TEI-only ingest loses the reader's PDF-highlight anchoring (the #5 "JATS has no
  pages" hazard). Therefore fetch **both** when both are cached: PDF for the
  reader + `pdf_sha256` + highlight coords, TEI for the structured blocks. If only
  PDF is cached (like 53423), Marker it as today.
- **TeX/LaTeX source is a different route** — neither OpenAlex nor S2ORC ships it;
  the only real `.tex` source is **arXiv e-print tarballs**
  (`arxiv.org/e-print/<id>`), relevant only for arXiv preprints. Note, don't
  build now.

**D. The bulk arm (Reto's ask — "set up for a big pass") — unify with the
historical/foreign-language importer (#8).** On-demand fetch legs and bulk
snapshot import are two *front ends* onto the same `chunks → embed` backend. The
bulk front end wants a shared **bulk-ingest substrate**, and the Russian-lit +
old-German (Chemische Berichte, #8) importers are the *same shape* — bulk,
identifier-messy, non-per-DOI, structured-or-scanned text that must skip the
chase→fetch→Marker path. Common machinery:
- **Free vs paid — the key money fact.** OpenAlex ships TWO products: the **data
  snapshot** (S3 `s3://openalex --no-sign-request`, **FREE**, ~monthly) is
  **metadata only** — titles/authors/citations/OA-status/`has_content`/`oa_url`/
  abstract-inverted-index, **no body text**. OpenAlex **full text** (PDF + GROBID
  TEI, ~60M works) is the **PAID** per-file Content API (§B), *not* in the free
  snapshot. So the free bulk-full-text routes are **S2ORC** (S2 Datasets API,
  needs a key, no per-file charge) and **CORE** (`fullText` + bulk dump, free
  key). The architecture this implies: **OpenAlex free snapshot = the planning/
  index layer** (mine it to decide what+priority — this is where §E's "things we
  already have stuff on" is computed); **S2ORC + CORE = the free bulk full-text
  backbone**; **OpenAlex Content API (paid) = gap-filler for the blocked residual
  S2ORC/CORE miss** (the MDPI/Akamai case), pay-per-file only on the tail.
- **`BulkSource` adapters — the roster** (one per corpus; each yields a normalized
  "document = metadata + (pre-parsed text | TEI/JATS | to-OCR scan)"). **Build
  order: S2ORC first** (biggest free full-text win, feeds the pilot), then CORE,
  then the scan sources.
  1. **`s2orc`** — Semantic Scholar S2ORC / S2AG **Datasets API**: bulk
     machine-parsed full text, sharded gzipped JSONL, continuously updated. Needs
     a free S2 key; **no per-file charge**. *The priority-one adapter + the "big
     pass" backbone.*
  2. **`core`** — CORE (core.ac.uk) aggregates OA **full texts from ~10k
     repositories worldwide** (~300M works). Two modes: the **REST API**
     (`fullText` field + `downloadUrl`, free key — per-work, the existing
     `fetcher:core` leg's richer sibling) and the **bulk data dump** (the whole
     corpus as a snapshot — the *bulk-harvest* mode for this arm). OCR-ish
     plaintext; the broad green-OA net S2ORC misses. *Second after S2ORC.*
  3. **`oai_repositories`** — direct harvest from institutional / disciplinary
     repositories via **OAI-PMH** (`ListRecords`, incremental by datestamp) or
     their REST APIs: **Zenodo**, **PubMed Central OA**, **arXiv**, the **UoL /
     university repositories**, and disciplinary archives. This is where CORE's
     coverage or freshness lags — go to the source. OAI-PMH gives Dublin-Core /
     often JATS metadata + a link to the PDF/XML; a generic OAI harvester +
     per-repo endpoint list is the reusable core (dedup by DOI/handle against the
     corpus, copyright-gate as below). Complements #2 (CORE *aggregates* these;
     direct harvest gets what it hasn't indexed and stays current). arXiv here is
     the *bulk* path (the per-DOI arXiv leg already exists in `fetch_oa`).
  4. **`openalex_snapshot`** — free S3 metadata snapshot (`--no-sign-request`).
     **Index/planner only, NOT a full-text source** — mines *what* to ingest and
     in what priority (feeds §E); the paid Content API (§B) is the per-file
     full-text gap-filler, a *separate* thing.
  5. **`internet_archive` / `hathitrust` / `jstage`** — scan-derived corpora for
     the historical/PD run (#8; old-German *Chemische Berichte* pilot). hOCR in,
     OCR tier for low-confidence.
  6. **`east_view` / institutional** — Russian-lit full text (paywalled/licensed;
     copyright-gated per below).
- **Structured-text → blocks** — reuse the #5 `extract_blocks_*` seam so S2ORC
  JSON / TEI / JATS / IA hOCR all land in Marker's block-dict shape → existing
  `_blocks_to_chunks → _retag_references → _build_cards → write_paper` → NULL-
  embedding cascade. **Skips Marker entirely** (text already parsed) except the
  scan/OCR tier.
- **Dedup + identity** — DOI → title-fuzzy → cite-key against the live corpus
  (reuse `dedup.py` / `paper_hygiene`), so a bulk pass folds into held/stub refs
  instead of minting millions of duplicates.
- **Copyright-era gating** (from #8) — pre-~1930 PD = full-ingest; in-copyright,
  non-OA = index/abstract-only. CC-licensed OA = full.
- **Specialized OCR tier** (from #8) — Fraktur/Cyrillic/CJK; prefer source hOCR,
  re-OCR on low confidence.
This is a **new subsystem**, not a fetch leg — the deliverable "set up for a big
pass" = land this design + the S2ORC `BulkSource` scaffold, then run a gated
pilot. **Decisions needed before executing:** (a) S2 API key availability +
storage target for a multi-hundred-GB snapshot, (b) target scale (tens-of-
thousands vs millions — sets on-demand-per-file vs bulk-snapshot), (c) chemistry/
materials-first vs broad (S2ORC under-covers MDPI/Chinese-lit; OpenAlex broadest).

**E. Embedding-prioritization — OPEN design question, deliberately NOT solved
(Reto: "let's not complete that part").** A bulk pass dumps millions of NULL-
embedding chunks; bge-m3 throughput is finite + load-gated, so naive FIFO starves
fresh on-demand papers behind the cold-import flood for weeks. Reto's instinct:
**"prioritize the things we already have stuff on."** Candidate priority signals to
weigh later — ref referenced by a todo/draft/project/citation/link (warm set);
recently viewed/searched/flagged (`last_viewed_at`, flags); explicit `PRIO` /
in-a-project; ref-creation recency (on-demand fresh > bulk backfill); topical
adjacency to existing high-signal chunks (chicken/egg — needs an embedding to
know; use cheap lexical/keyword overlap as a proxy). Mechanism sketch: an
embed-priority ordering in the claim query, bulk chunks stamped a low-priority
`meta.ingest_source='bulk'` that **trickles behind live traffic** (same principle
as `llm_summarize` on melchior). **Not a decision yet — captured so the bulk pass
doesn't ship without a queue policy.**

**F. Small concrete items found in the dig:**
- **CORE leg bug** — the 53423 log shows `fetcher:core` tried to download the URL
  `"587670336"` (a bare CORE work-id where a `downloadUrl` was expected) →
  "refusing non-http(s) URL". `_query_core_pdf_urls` passes the field through
  unvalidated. Fix: validate it's an http(s) URL, and/or switch CORE to its
  `fullText` field (bulk-arm-relevant anyway). Owner: `workers/fetch_oa.py`.
  **STILL OPEN.**

**G. OpenAlex free-metadata enrichment — WANTED (Reto: "we want that meta").**
The OpenAlex *work* object is free + keyless (`api.openalex.org/works/doi:<doi>`,
49 fields) and far richer than what we hold. Slurp it into the ref — independent
of (and cheaper than) the paid content pull. Field → home:
- `referenced_works` (OpenAlex IDs of cited works, 110 on 53423) → **citation-graph
  edges** (`links` `cites`, resolvable to DOIs → link held papers / mint stubs).
  The highest-value field — it densifies the graph S2 alone under-covers.
- `authorships` → **authors** (ORCID per author + institution **ROR** + country)
  into the `authors` JSONB byline.
- `topics` / `concepts` / `keywords` → controlled **`ref_tags`** (topic axis).
- `funders` / `grants`, `fwci`, `cited_by_count`, `sustainable_development_goals`,
  `mesh` → `ref.meta`.
- `is_retracted` → cross-check `retraction_status`; register `openalex:W…` in
  `ref_identifiers`.
Home: a metadata source alongside CrossRef/S2 in `ingest/metadata_resolve.py`,
**or** a dedicated `openalex_enrich` ref-pass (idempotent upsert, polite `mailto`
pool, fixed host so no SSRF concern). Runs at stub-promotion/ingest **and** as a
backfill over existing paper refs. Same free API also = the §D
`openalex_snapshot` planner at per-record granularity (live API for a handful; the
free S3 snapshot for millions). **BUILT this session (unshipped) — see below.**
Deferred within G: `referenced_works` **edge materialization** (W-ids → DOIs →
`links` `cites`) rides on the scholarly-graph fan-out (#6) — the raw W-ids are
captured in `meta.openalex.referenced_works` now so no re-fetch is needed later;
topics→`ref_tags` waits on the OPEN-namespace teardown; wiring the backfill CLI
into a scheduled worker pass is a follow-up (the CLI covers the sweep today).

**BUILT this session (unshipped, green: ruff+mypy+targeted pytest):**
- **OpenAlex Content leg** (`workers/fetch_oa.py` `_try_openalex_content`) — the
  §B rescue, Phase 1 (PDF only; TEI deferred to #5). Reads free `has_content`,
  downloads from `content.openalex.org` with `?api_key=`, records `cost_usd`
  ≈$0.01, key never leaves the query (not in the payload). LAST in the cascade,
  **double-gated**: `PRECIS_OPENALEX_CONTENT_KEY` **and**
  `PRECIS_OPENALEX_CONTENT_AUTO` (default OFF) so it merges dark and can't
  auto-bill the backlog.
- **`precis fetch-openalex <doi|ref_id>`** (`cli/fetch_openalex.py`) — the manual
  "penny now" one-shot (bypasses the auto gate); downloads into
  `PRECIS_WATCH_INBOX`, writes the stub-fold sidecar when given a ref_id. This is
  the path to prove 53423 the moment the key + funded balance exist.
- **Failure-reason surfacing** (`store/_refs_ops.py`) — `/papers-needed` now
  renders the concrete why ("fetch failed: mdpi.com 403 — will retry in 24h")
  instead of a bare `fetch_failed`; payload threaded into `_stub_state_summary`,
  host+HTTP-status extracted. Verified end-to-end against real Postgres.
- **§G OpenAlex free-metadata enrichment** — `ingest/openalex_meta.py`
  (`fetch_openalex_work` + pure `normalize` + `enrich_ref`) writes the
  `meta.openalex` block (abstract, topics, funders, fwci, cited_by, 110
  `referenced_works` W-ids, ORCID+ROR authorships), registers `openalex:W…`, and
  fills the byline only when empty. CLI `precis enrich-openalex <doi|ref_id>` +
  `--backfill --limit N`. Verified live against 53423 (fetch+normalize) and
  end-to-end against real Postgres (write). 11 unit tests.
- **NOT yet built:** the TEI/`grobid_xml` structured-ingest path (#5), the CORE
  bug fix, the bulk arm (§D), the auto-leg budget cap for when AUTO is flipped on.
  **Verify on first real key:** OpenAlex Content auth is `?api_key=` (per their
  docs + the URL format) — confirm on the first live 200.

### Residuals — stub↔ingest dedup-split fix (SHIPPED c6152950, 2026-07-06; Opus-authored)

The "fetched 16h ago but not ingested" cards (stubs 50698/50754) were a
**dedup split**: the OA fetcher's stub and the PDF-derived identity didn't
intersect (Marker truncated the DOI, or extracted none), so ingest minted a
duplicate ref and left the stub `pdf_sha256 IS NULL`. Fixed forward with an
**acquisition sidecar** (`ingest/fetch_sidecar.py`) carrying the stub `ref_id`
so `precis_add` folds into it in place, **plus** the root-cause fix
(`_reconcile_orphan_stub` now also runs on the new-ref branch, not just the
dedup-hit branches). Residuals parked (all harvest-eligible, Opus-authored):

1. **Multi-host inbox race — spurious `no such file` errors (deliberately
   deferred).** 28/30 ingest `error.txt`/day are the 4 watchers racing the
   shared NFS inbox: the loser's Marker run dies with `FileNotFoundError` when
   the winner moves the PDF mid-extraction. The winner ingests fine (not data
   loss) but the loser writes a bogus `error.txt` — the `errors/` dir lies. The
   "vanished mid-ingest, skip silently" guard (`watch.py:619`) misses it because
   pymupdf/pdftext **wraps** the `FileNotFoundError`, so it hits the generic
   `except Exception`. Fix: recognize a wrapped file-vanished error (check
   `pdf.exists()` / walk `__cause__`) and skip silently instead of erroring.
   Owner: `cli/watch.py`. Severity: polish (noise + wasted Marker cycles).
2. **Metadata-poor extraction leaves the ref titleless.** For 50995 (`anon00ag`)
   Marker put the title in chunk 0 ("CONTINUOUS DEFORMATIONS…") but the
   **ref-level `title` stayed empty** (`[no metadata]`), cite_key degraded to
   `anon00ag`, blocking after-the-fact title-similarity reconcile. Prod
   population (2026-07-06): **187 titleless chunked papers — 32 with a DOI, 155
   with no external id**. **Do NOT** backfill title from chunk 0 (a wrong title
   is worse than none). The confident fix already exists: `metadata_resolve.py`
   (`precis fix-metadata`) never trusts PDF text as a title — Track 1 re-resolves
   CrossRef by DOI (the 32); Track 2 S2-title-searches with chunk-0's first line
   and **auto-applies only at similarity ≥ 0.85 + compatible year + recovered-DOI
   not already owned**, everything else → `needs-triage` (human). Reversible,
   source-stamped, dry-run-previewable. **Progress (SHIPPED, 2026-07-08):**
   `resolve-metadata` now (i) scans the **first ~4 body chunks** for the title
   query — not just chunk-0's first line, which is a masthead/received-line/bare
   author list ~half the time — filtering body furniture + stripping markdown,
   trying each candidate and keeping the best-similarity S2 hit (recall up,
   precision unchanged: the 0.85 gate still guards every write); and (ii) its
   `_triage_refs` cohort is **widened** to include any titleless chunked paper,
   not just `needs-triage`-tagged ones (the 135 untagged of 187 were previously
   unreachable). **Remaining:** (a) run the dry-run over the cohort → verdict
   distribution + gold-check the `auto` set, then `--apply`; (b) **schedule it**
   (manual-only today) into `paper_reconcile`/hygiene so titleless refs self-heal.
   The shipped sidecar fold means the 187 is a fixed backlog, not growing. Owner:
   `ingest/metadata_resolve.py`, `cli/resolve_metadata.py`. Severity: feature.
3. **Verify the 7 existing orphans self-heal post-deploy.** 50698, 50754, 49915,
   50223, 50227, 50335, 49503 are already split (content under duplicate refs).
   They should self-heal when `requeue_stranded_fetches` re-fetches them at >48h
   (the re-fetch now writes a sidecar → folds into the stub instead of
   re-splitting), OR immediately if re-queued now (`meta.oa_requeued`). Confirm
   the cards resolve; if a metadata-poor re-fetch (no sidecar-fold) leaves a
   residual junk dup (e.g. 50995), that's covered by #2 + a title-sim reconcile
   extension to id-bearing chunkless stubs. Owner: verify on prod.

---

_Last updated: 2026-07-08 (added the "2026-07-08 update" block to the OA section:
MDPI/Akamai wall confirmed publisher-agnostic + PMC-doesn't-cover-MDPI; OpenAlex
Content API verified reaching 53423 → promoted to co-keystone #1b, killing the
Akamai/Cloudflare-403 class in one leg; XML/TEI-vs-PDF decision (prefer TEI, keep
PDF; TeX only via arXiv e-print); the bulk arm unified with the historical/
foreign-language importer #8 (S2ORC/CORE = free bulk full text, OpenAlex free
snapshot = index/planner only, OpenAlex Content paid = blocked-residual gap-
filler) + Russian-lit; embedding-prioritization left OPEN per Reto; CORE bare-id
bug + failure-reason surfacing noted. Same day, later: made the `BulkSource`
roster an explicit named-adapter list — **`s2orc` priority-one** + `core` +
`openalex_snapshot` (index-only) + IA/HathiTrust/J-STAGE + East View; added item
**G — OpenAlex free-metadata enrichment (WANTED)** with the field→home map
(referenced_works→citation edges, ORCID/ROR→authors, topics→tags); and built
(unshipped) the OpenAlex Content leg + `precis fetch-openalex` CLI + failure-
reason surfacing). Prior: 2026-07-06 (added the stub↔ingest
dedup-split residuals block under the OA section — shipped c6152950: acquisition
sidecar + new-ref-branch reconcile; 3 residuals parked: multi-host `no such file`
race, titleless metadata-poor refs, verify the 7 existing orphans self-heal).
Prior same day:
added the OA-acquisition + structured-ingest +
external-search roadmap — 9 interdependent items from the "it's OA but we don't
have it" diagnosis: publisher Cloudflare-403 is the common wall, PMC OA subset is
the free unblock for 2/3; keystone = a PMC-OA fetch leg; incl. JATS re-ingest with
the citation-reanchor hazard + the bge-m3 cn↔en measurement Reto asked to store);
also added the CAD spoked-wheel disconnected-spokes geometry bug + missing
job-log link on the CAD page. Prior: 2026-07-05 (added the paper-ingest
`equation`-kind
retirement as deferred backlog — companion to the draft equation→$$
retirement on `worktree-mission-doc`; 54.6k paper equation chunks vs 278
draft, different reader + deliberately un-embedded, so paper side needs its
own embed-policy decision first). Prior: 2026-07-04 (added the ADR 0047
chunk-tag classifier remaining-work section — enable continuous tagging /
Tier-2 escalation / ref-axis runner / table heuristic; pruned the
Recently-retired graveyard + done CI item — both in git; snoozed Dependabot
#44 transformers RCE until 2026-07-18, blocked by marker-pdf's
transformers<5 cap)_
