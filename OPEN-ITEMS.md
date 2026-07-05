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
- **Backlog groomer (close the loop)** → open. Today nothing promotes repo dev
  work into the acting queue automatically — `/whatneedsdoing` only *reads* both
  substrates. The dark-factory move: a `level:recurring` watch that reads
  `OPEN-ITEMS.md` + open gripes and mints `kind='todo'` rows with `meta.executor`
  (a `fix_gripe` job for bugs; a build tick for features), so `dispatch` builds
  them — i.e. it bridges repo dev work *into* the prod factory queue. Pairs with
  `/checklogs` + cheap-model tiering. Until this lands, the backlog is a level-3
  artifact the factory can't act on.
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
library reformats with no library open).

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
- **Feature idea (nice-to-have, not a gap): embed cited chunk passages in the
  traveling-library record** (`<custom1>`/`<research-notes>`) so EndNote shows the
  exact cited passage. Caveat: EndNote drops Abstract/Notes/Research-Notes on
  library import (custom fields may survive) — needs a round-trip test. Offered to
  Reto; build as its own cycle if wanted. Today `_cite` drops the `~chunk` address
  and keys on the paper, so the passage identity is discarded before the record.

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
- **Convergence assert races the autonomous fixer** (observed 2026-07-05). The
  final "each venv matches deployed ref" assert does a *fresh* `git ls-remote origin
  main` per host, so when the hephaestus fixer ships mid-deploy the installed commit
  ≠ HEAD and the assert fails — even though the installs succeeded and the cluster is
  uniform (bit 2–3× per `/go` deploy while the fixer was active; a re-run in a quiet
  window converges). Fix: capture the target sha ONCE at play start (or assert
  against the commit each venv actually *installed*, not a fresh ls-remote), so a
  `main` moving under an in-flight deploy can't false-fail. Owner: `redeploy-precis.yml`.

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
- **Part B lens seed → BUILT.** `utils/dream_seed.py` loads
  `data/dream_lenses.yaml` and rotates one lens per cycle (bucket by
  ~15-min cadence) into the dream prompt's variable layer
  (`workers/dream_agent.py`); the dream's own Step 6c gripe hook is
  removed (Part A covers friction now). 8 figure personas + Disney
  process lens.

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


---

_Last updated: 2026-07-04 (added the ADR 0047 chunk-tag classifier
remaining-work section — enable continuous tagging / Tier-2 escalation /
ref-axis runner / table heuristic; pruned the Recently-retired graveyard +
done CI item — both in git; snoozed Dependabot #44 transformers RCE until
2026-07-18, blocked by marker-pdf's transformers<5 cap)_
