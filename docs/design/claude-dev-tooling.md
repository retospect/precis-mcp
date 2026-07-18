# Plan — Claude Code repo-dev tooling

> **Altitude: plan (point-in-time).** Active multi-slice plan for the
> repo-*dev* tooling (how we develop precis-mcp), NOT the precis product.
> Per `docs/conventions/llm-facing-prose.md` this is a design doc — **delete
> it when the work ships** (git retains it). Cross-session recall pointer:
> memory `repo_dev_claude_tooling.md`. Terse; internals are the payload.
>
> _Branch: `docs-llm-facing-prose` (worktree `claude-dev-tooling`).
> Verified @ `75d2d64c`._

## Why

precis-mcp is BOTH the repo we develop AND an MCP whose product skills load
into the dev session — which caused real confusion (reaching for
`get(kind='skill')` as a code navaid). This work draws the line (two
surfaces) and builds the missing dev-side tooling: orientation docs, a prose
house-style, test/prod wrappers, and semantic code search.

**Guiding principle everywhere: point, don't copy.** Hold procedure +
pointers, never present-state. Keep artifacts few (each rots). Freshness
checked at ship (`/endsession`, `/go`).

## Done (committed on `docs-llm-facing-prose`, unshipped)

| Commit | What |
|---|---|
| `b2049469` | `docs/conventions/llm-facing-prose.md` (forked, dev-facing — internals are payload); `docs/codebase.md` (orientation altitude); two-surfaces note in CLAUDE.md; ship-time doc-freshness step + compact-nudge in `/endsession`+`/go`; AGENTS.md DoD |
| `065f8b1d` | `scripts/test` — worktree-aware, signal-only test runner (dev container, RAM test DB, `--no-sync`, terse flags). Wired into CLAUDE.md/AGENTS.md/commands |
| `3812b3b1` | `scripts/prod-psql` — the prod psql hop wrapped (SQL over stdin, terse); CLAUDE.md "Peeking at prod" points at it |
| `75d2d64c` | Code-search s1: `docker/code-search/compose.yaml` (Milvus standalone, verified healthy) + `.mcp.json` (claude-context, Ollama, local — checked in, portable) + `.gitignore` += `.testmondata` |

These came from **mining 20 sessions / 7322 shell commands**: tests
re-derived 8+ ways, prod-psql 124×, cluster ssh 216×, test-DB dance 202×.

## In progress — code search (claude-context + local Milvus)

Decided: **separate surface** (not dogfood precis's `python` kind), **local
Milvus + Ollama** (`nomic-embed-text`), **shared-main index** (worktrees read
main's collection read-only; don't re-embed 200k LOC per worktree).

- ✅ **s1** — Milvus up (etcd from gcr.io; quay.io TLS-timed-out), MCP wired.
- ✅ **s2** — born-indexed flow, **no CLI** (use the MCP directly). The
  collection is `hybrid_code_chunks_<md5(resolve(path))[:8]>` — keyed to the
  *absolute path*, storing **repo-relative** paths inside. So one index of the
  MAIN checkout IS the shared index: a worktree reuses it by searching with the
  main path, and every hit maps onto the identical relative path. Nothing to
  index per worktree ⇒ no indexing hook, no Node subproject/faiss build.
  - `scripts/hooks/code-search-up.sh` (SessionStart): `docker compose up -d`
    the code-search stack so the MCP can connect, and print the shared-index
    guidance (search with the main path). Never blocks/fails start.
  - Convention line in CLAUDE.md ("Semantic code search"); `guard-worktree-path.py`
    stays as the hard backstop against editing main from a worktree.
  - **SEEDED + validated 2026-07-18**: the MCP didn't load in-session (project
    `.mcp.json` servers need a fresh session + trust), so seeded from the shell
    via the core lib matching `.mcp.json` config (Ollama nomic@768, Milvus
    root:Milvus, hybrid default) → collection `hybrid_code_chunks_c11129df`,
    **1441 files / 19,130 chunks**. A hybrid RRF `semanticSearch("SSRF guard…")`
    returns `src/precis/utils/safe_fetch.py` + the http test + the seam doc, as
    repo-relative paths. So `search_code` works the moment the MCP loads.
    (Re-seed the same way, or `index_codebase(path=<main>)` from an MCP session.)
- ⬜ **s2-followups (deferred)** — a `scripts/code-index` Node CLI
  (`@zilliz/claude-context-core` → `Context.indexCodebase`) is worth building
  ONLY if lazy Merkle re-sync proves too stale and we need a *deterministic
  post-merge reindex from the shell* (hook off `scripts/ship`). Not needed for
  a navigation aid; filed, not built. `worktree.baseRef` setting likewise
  optional.
- ✅ **s3** — verified the session precis MCP's DB target. **Finding inverts
  the assumption:** it's NOT a local sandbox — `get(kind='skill',
  id='precis-status')` shows `precis_prod` on caspar:6432 as `agent_rw`
  (write-capable); it's the local 5th worker, a real prod participant. The
  "Sandbox PRECIS_ROOT" banner scopes only file-kinds. So the safe framing is
  **read-only dogfooding** (search/get/more) via the session MCP; write loops
  (put/edit/delete/tag) hit prod → use a dev-DB precis for write-path testing.
  Documented as a conventions-that-bite footgun in CLAUDE.md.

## Done — second wave (2026-07-18)

- ✅ **Test-gate optimization → `scripts/test --impacted`** (pytest-testmon).
  Grounded in a durations profile: the 140s gate is dominated by per-worker
  DB-clone **setup** (76s/50s/30s setups; slowest *call* is 15s), so the naive
  fast/slow split is low-ROI — the lever is running FEWER tests. Impacted mode
  runs only tests a working-tree change touches (cold builds the map, warm
  0.31s deselecting all). testmon in dev group; gate still runs full. The
  marker-based fast/slow split is **not worth it here** (setup-dominated) —
  dropped, not deferred.
- ✅ **`scripts/code-index`** — reproducible shell seed/refresh into the MCP's
  collection (full seed when empty, Merkle-incremental after). Closes the
  ephemeral-scratch-seed gap AND is the deterministic post-merge reindex the
  plan had deferred.
- ✅ **`navigator` agent** (`.claude/agents/navigator.md`, haiku) — the
  dissolution the plan predicted, made concrete: a pre-briefed read-only
  orientation subagent (codebase.md + maps + `search_code`, cite file:line).
  Cheap, offloads spelunking from the main context.
- ⛔ **Mutation testing — FILED, blocked on tool choice.** `mutmut` 3.6 runs
  pytest **in-process**, which is incompatible with our global `addopts =
  -n auto` (it builds `-n auto … -n0` and xdist can't fork inside the running
  pytest → stats collection crashes, every mutant "not checked"). Fixing means
  pulling `-n auto` out of `addopts` globally (breaks CI/scripts/test/the gate)
  — too invasive for a nice-to-have. **Fix = `cosmic-ray`** (runs the test
  command as a *subprocess*, so `pytest -n0` works cleanly). Scope to one
  pure-logic module (SSRF guard exemplar); nightly bucket; feeds the
  end-of-project unit-test skim.

## Backlog (land one at a time; keep only what earns weight)

- **Docs: historical→current-state triage.** THE root disease (172 docs/57k
  lines accreted as append-only record, read as current, rot). Cure =
  triage, **delete-default** ("rest in git for the archeologists"): (1)
  current-state maintained (codebase.md/state-map/glossary + live specs like
  storage-v2), (2) ADRs — Reto: OK to periodically **compile-and-cut** ("the
  now"), not immutable-forever, (3) everything else historical → DELETE (git
  retains). **Flip AGENTS.md "obsolete plans stay for context" → delete on
  ship.** Subsumes the prose compliance sweep (state-map 769L / AGENTS /
  glossary each own PR).
- **Mutation testing via cosmic-ray** (see FILED above) + the end-of-project
  unit-test review/skim it feeds.
- **`subsystem-analyst` (opus) agent** — only if the haiku navigator proves too
  shallow for deep "how does the whole X work" synthesis.
- **SSH config** — RESOLVED as a stale-memory fix, not a config change: bare
  `ssh <cluster-host>` already works (`~/.ssh` bakes in `IdentityAgent none`);
  the 2281× flag re-derivation was the `ssh-cluster-access` memory instructing
  a redundant flag. Fixed the memory. Nothing to build.

## Decisions log

- Code search: claude-context (separate surface) + local Milvus + Ollama +
  shared-main index. Milvus 3-container (embedded panicked; no brew; Lite is
  embedded-only/no-gRPC). `.mcp.json` checked in (portable, no secrets) over
  editing `~/.claude.json`.
- Prose convention **forked** from product `skill-authoring-style.md` (its
  cut-list inverts — there internals are noise, here they're payload).
- No tool-choice justification in LLM-facing prose (killed the `git town`
  legacy mentions).
- `.testmondata` / Milvus data / index vectors: NOT in git.
- **Context frugality, two moves.** (a) **`rtk`** (token-killer CLI proxy;
  `brew install rtk`) compresses noisy command output to signal — supersedes
  the hand-rolled `scripts/quiet` (retired: rtk does strictly more — per-tool
  proxies, tee'd full logs, savings accounting). Wiring per Reto: **in-repo,
  manual, no hook** — `.rtk/filters.toml` committed (the "weird filter" made
  explicit + version-controlled), a terse CLAUDE.md convention, and the first
  repo-dev **skill** `quiet-output` (`.claude/skills/`, surfaced at point of
  use; repointed at rtk; distinct from precis product skills). No global hook
  (would touch every project) and no checked-in hook (would break for devs
  without rtk). rtk output is a *digest* — re-run raw if a detail's missing.
  (b) **repomix/codesight DECLINED** — the pack-the-whole-repo model is the
  preload we replaced with codebase.md + semantic search; it's the biggest
  context sink, the opposite of (a).

## Open questions

- ADR compile-and-cut: cadence + target ("DECISIONS-now" in codebase.md vs a
  slimmed ADR README)?
- Ship these commits (`/endsession`) — combined squash message; branch name
  under-describes (now spans docs + scripts + code-search).

## PRE-SHIP gate — DONE (2026-07-18)

Re-ran the scan over **266 transcripts / 20,071 shell commands**. Verdict:
**no new wrapper needed** — the two we built already target the top *wrappable*
stones (raw pytest 2682×, raw prod-psql hop 1597×). The rest is either served
(docker-exec-into-dev 988× → `scripts/dev`/`scripts/test`) or config, not a
wrapper.

**The actual biggest stick was a rotted memory, not a missing tool.** The #1
raw incantation — `ssh -o IdentityAgent=none` for cluster hosts, **2281×** —
has been *unnecessary* since `~/.ssh/config` baked `IdentityAgent none` onto
the cluster `Host` block (bare `ssh caspar` verified working). Agents kept
re-deriving it because memory `ssh-cluster-access` still *instructed* it.
Fixed the memory (+ index) to say bare-ssh-works; flag is a documented escape
hatch only (ansible still needs `ANSIBLE_SSH_ARGS`). This validates the
session's whole thesis: stale *current-state* guidance is the tax, and the
cure is correcting the durable artifact, not adding tooling.
