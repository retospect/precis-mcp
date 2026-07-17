# Claude Code — project brief

> A **lean router**, loaded every session. It holds only what you need
> *before* the first tool call: the ship workflow, the conventions that
> bite, and pointers to where deeper detail lives. The present-tense map of
> the discovery / task / worker / review subsystems moved to
> **`docs/architecture/state-map.md`** (read it before touching one) — kept
> out of here so this file stays small. `AGENTS.md` is the conventions /
> workflow / definition-of-done guide (humans + agents); the skills
> (`get(kind='skill', id=…)`) are the authoritative per-kind reference. No
> CHANGELOG — the dated story is the **git history** (`git log`).
>
> **Keeping docs true:** a change that alters a subsystem updates
> `state-map.md` in the same commit; this file changes only when the
> workflow, a convention, or the subsystem *set* changes.

## Session workflow (worktree → ship)

Best practice for a unit of work:

1. **Start in a worktree.** Launch with `claude -w <name>` (alias of
   `--worktree`). Claude Code creates an isolated worktree at
   `.claude/worktrees/<name>/` on a new `worktree-<name>` branch, so
   the work is isolated from `main` and from sibling sessions.
2. **Do the work** in that worktree — implement, test, iterate.
3. **End with `/endsession`** (ship) **or `/go`** (ship **+ deploy**).
   Both run the deterministic `scripts/ship`: commit WIP → sync
   (`git fetch` + `git merge` main) → the container integration gate (auto-fix ruff, then
   authoritative `ruff` + `mypy` + `pytest`) → squash-merge to `main`
   (only if green) → reset the branch to the shipped `main` → local-main
   fast-forward. `/go` additionally runs `scripts/deploy` on a green ship to
   push `main` to the cluster (`ansible-playbook redeploy-precis.yml` — the
   dark-factory one-keystroke). Both **abort and report** on any gate
   failure; fix and re-run (the scripts are idempotent). Landing on `main` —
   and, via `/go`, on the cluster — is the end goal of a feature branch.

`scripts/ship` is **plain git — no git-town dependency**. It integrates
`origin/main` with `fetch` + `merge`, squashes the branch onto `main` via
`commit-tree` + a `--force-with-lease` CAS push, then **resets the feature
branch to the shipped `main`** so the next ship starts at zero divergence.
NB the merge target is `main`, not `master` — the repo has no `master`.

**In-flight worktrees — `scripts/inflight` + `.claude/purpose`.** Many
Claude sessions run sibling worktrees at once (each on its own
`worktree-<codename>` branch), so before starting a unit of work, **scan
for someone already doing it**. `scripts/inflight` prints a live table
(per worktree: live session, dirty count, ahead/behind vs `main`, PURPOSE,
last commit — all derived from git at call time). A `SessionStart` hook
runs it automatically and injects the table; **read it and flag overlap.**
Git can derive everything *except* what a random-codenamed worktree is
about, so **once your task is clear, write one line to `.claude/purpose`**
(gitignored; per-worktree, self-cleaning). The footer lists **removable**
candidates (merged + clean + no live session) but only *reports* — never
auto-remove (a locked session, uncommitted WIP, or a salvageable design
doc must not be nuked).

## Subsystem map (detail on demand)

Read `docs/architecture/state-map.md` before touching one of these; the
`precis-*-help` skills are the on-demand per-kind reference. One line each:

- **Todo tree (five slices)** — the `kind='todo'` hierarchy: strategic/tactical
  gradient, `auto_check` leaves, `level:recurring` watches, jobs (intent vs
  compute lane, ADR 0044), planner coroutines, views, projects.
  → state-map §"The todo tree"; skills `precis-tasks-help`, `precis-dispatch-help`.
- **Review tiers** — `nursery` (SQL, per-minute, the only `critical` alerts:
  worker-restart / dead-worker / dispatch-stall), `structural` + `deep_review`
  (opus). → state-map §"Review tiers"; skill `precis-nursery-help`.
- **Workers** — two profiles (`system` on every node, `agent` on melchior),
  four LaunchDaemons; notable passes (`cast_audio`, `card_forge`, `sweeper`,
  `corpus_reconcile`, `paper_reconcile`, `fetch`/`chase` backoff); the
  `claude_agent` dispatch + the switchable LLM router (ADR 0046).
  → state-map §"Workers".
- **Discovery layer (F20)** — per-chunk KeyBERT (`chunks.keywords`), `view='toc'`;
  ADR 0018 is superseded. → state-map §"Discovery layer";
  `docs/conventions/discovery-layer-policy.md`.
- **Chunk-tag classifier (ADR 0047)** — the cascade (regex → `role3` local →
  optional escalate); `ROLE3:own` = citation-grounding filter, default-OFF.
  → state-map §"Chunk-tag classifier"; `docs/design/chunk-classifier-cascade.md`.
- **Live affordances** — the other kinds + surfaces: `folder`, `plan`, `figure`,
  `mermaid`, `gripe`, `anki`, `concept`, `quest`, `llm`, `alert`, `agentlog`,
  `job`/sandbox, `structure`, `citation`, `cfp`, term registry, `cad`/`pcb`,
  broad+deep search, `precis web`, SSRF guard, ingest hygiene.
  → state-map §"Other live affordances"; the matching `precis-*-help` skill.
- **Skill index** — start at `precis-toolpath-help` (call sequences) and
  `precis-overview` (master kinds table). → state-map §"LLM-facing skill index".

## Where to find context

| Task                             | Read |
|----------------------------------|------|
| Subsystem detail (present-state) | `docs/architecture/state-map.md` |
| Coined / overloaded terms → files| `docs/architecture/glossary.md` (what does `tier`/`card`/`tote`/`bubble` mean, and where's the code) |
| To-do list / what's planned next | `OPEN-ITEMS.md` |
| Conventions / workflow / DoD     | `AGENTS.md` |
| Mission / pitch narrative + facts| `docs/mission.md` (positioning, not architecture) |
| Master kinds table + call recipes| skills: `precis-overview`, `precis-toolpath-help` |
| Dated history of every change    | `git log` (no CHANGELOG file) |
| Full schema (prose / visual)     | `docs/design/storage-v2.md` (F20-amended); `schema-v2.svg` (drift note) |
| Worker queue pattern             | `docs/decisions/0007-derived-queue-no-block-jobs.md`, `0017` |
| ADR index + supersession graph   | `docs/decisions/README.md` |
| Ingest pipeline                  | `src/precis/ingest/{marker,pipeline,text_chunker,db_writer}.py` |
| Worker code                      | `src/precis/workers/` |
| Web UI                           | `src/precis_web/` |
| Discord bridge (asa)             | `src/asa_bot/` — sibling package, `[asa]` extra; talks to `precis serve` over stdio |
| SSRF guard                       | `src/precis/utils/safe_fetch.py` |

## Conventions that bite

- **Forward-only migrations.** Never edit a sealed `*.sql` file
  under `src/precis/migrations/`. If you find a bug in a sealed
  file, ship a new forward migration that corrects it. Rationale
  lives in `docs/decisions/0005-greenfield-migrations.md`. A fresh
  DB does **not** replay the whole chain: it loads the generated
  `migrations/baseline/schema.sql` snapshot (the chain compiled to
  one file, self-stamping the ledger) and applies only the tail.
  Regenerate the snapshot with `scripts/bump` / `precis db
  dump-schema` — **never hand-edit it** (release-time only, not
  per-feature); it is checked against the files. Dual-track scheme,
  not greenfield (ADR 0031).
- **`uv` for everything.** Bare `pip` / `pytest` / `mypy` are
  not reproducible. Use `scripts/dev pytest …` inside the
  container, or `uv run …` on the host.
- **Run the FULL suite in the dev container, not the host.** The
  host venv is deliberately torch-free (no `[paper]` / `[embed]` /
  most extras), so a host `uv run pytest` reports dozens of spurious
  `ModuleNotFoundError` failures (`sympy`, `marker`, `lxml`,
  `sentence_transformers`, …) that are **not real bugs**. The
  `precis-mcp:dev` image bakes **all extras** into `/opt/venv` and
  wires `PRECIS_TEST_PG_URL`, so the canonical green run is:

      scripts/dev pytest            # full suite, all extras, DB wired

  Under the hood that is `docker compose … run --rm precis-dev`,
  which bind-mounts THIS repo at `/app`. A long-lived
  `precis-mcp-dev-*` container works too: `docker exec -e
  PRECIS_TEST_PG_URL=<dsn> <ctr> bash -lc 'cd /app &&
  /opt/venv/bin/python -m pytest …'` (note `/app` is **read-only**
  there — point `COVERAGE_FILE` / `-o cache_dir` at `/tmp`).
- **Host pytest is subset-only.** The host is torch-free, so only run
  targeted, extra-free subsets there. `scripts/dev` mounts the MAIN
  repo (not your worktree), so to test *worktree* edits on the host set
  `PRECIS_TEST_PG_URL=postgresql://postgres:<pw>@localhost:5432/precis_test`
  (pw from the `postgres-postgres-1` container env; the secret DSN in
  `~/.secrets/pw/PRECIS_TEST_PG_URL` uses `host.docker.internal` —
  rewrite it to `127.0.0.1` on the host). Worker tests importing
  `precis.ingest.citations` need the S2 client — `uv run --with
  semanticscholar pytest …` avoids the heavy `[paper]` extra.
- **Container-first ops.** `scripts/dev` → dev shell;
  `scripts/db` → psql (LOCAL `precis` / `precis_test` only — the
  dev pgvector container is published at `127.0.0.1:5432`,
  `POSTGRES_USER=postgres`). Compose file lives outside the repo at
  `~/work/infrastructure/compose.yaml`.
- **Peeking at prod.** To inspect the live `precis_prod` DB, hop through a
  cluster node and psql the pgbouncer:
  `ssh -o IdentityAgent=none melchior 'psql -h 100.126.127.107 -p 6432
  -U agent_rw -d precis_prod -c "…"'` (caspar works too). `agent_rw`
  has SELECT; the local `scripts/db` creds do **not** reach prod. The
  `-o IdentityAgent=none` works around the flaky ssh-agent forwarding.
- **Skills are runtime docs.** Updating a skill file under
  `src/precis/data/skills/` is the agent-facing channel — the
  MCP server reads them at boot and serves them via
  `get(kind='skill', id='…')`.
- **Embeddings populated by the worker, not at ingest.** Per ADR
  0007: ingest stores chunks with `embedding IS NULL`; the
  `embed:bge-m3` worker picks them up. Callers must not call
  `fill_embeddings` from the ingest path.
- **Don't mutate body chunks.** `chunks` is append-only for body
  rows (`ord >= 0`); only `ord < 0` card variants may be DELETEd and
  re-INSERTed by a registered synthesis pass. To "update" a chunk's
  text, DELETE + INSERT so the embedding/summary cascade re-runs —
  an in-place UPDATE leaves stale `chunk_embeddings` / `chunk_summaries`.
- **Outbound HTTP goes through `safe_fetch`.** Any new code that
  fetches an agent-supplied URL — directly or after a redirect —
  must use `safe_get` / `safe_stream` from
  `src/precis/utils/safe_fetch.py`. Raw `httpx.Client(...).get(url)`
  with `follow_redirects=True` is an SSRF.
- If another branch left trivial drift (a file that needs `ruff`), just fix it.
