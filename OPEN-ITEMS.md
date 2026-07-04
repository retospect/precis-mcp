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
- **Widen `scripts/ship` auto-fix surface** → open (polish). Auto-fix + amend
  anything the gate can resolve without judgment (import sort, trivial mypy
  stubs); only real logic failures reach the model.

Deferred (revisit later): **holdout scenarios** (StrongDM-style anti-overfit
eval outside the repo — not needed while Opus shows no test-gaming; ADR 0047
gold sets are the seed); **digital-twin fidelity** (richer stubs so
green-in-twin/red-in-prod gaps close — the current `FakeStore`/`MockEmbedder`/
`PRECIS_CLAUDE_BIN` twins are good enough for now); **auto-deploy as a daemon**
(vs `/go`-chained — only if chaining proves insufficient).

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
