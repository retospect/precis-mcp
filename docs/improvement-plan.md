# Improvement plan — codebase best-practices

> **Living document.** Prioritized quality/best-practices program from the
> 2026-08-02 six-dimension review (core server, workers/ingest, web UI,
> security, DB layer, tests) verified @ `f2a517f1`. **Maintain like
> OPEN-ITEMS.md:** when an item ships, *delete* it here in the same commit
> (`git log` is the record). An item that becomes active work may graduate
> to OPEN-ITEMS.md or a gripe — leave a one-line pointer, don't duplicate.
> Re-run the full review when this file is mostly empty or ~6 months stale.
> Cite code by durable anchor (`file.py::symbol`), not line numbers
> (`docs/conventions/code-anchors.md`).
>
> **Owner tier** follows CLAUDE.md agent sizing: script / haiku / sonnet
> (`coder`, `test-author`) / opus (design).

## Scorecard (2026-08-02)

| Dimension | Health | Headline |
|---|---|---|
| Security | Strong | No highs; safe_fetch DNS-pinning genuinely well-engineered |
| Core server | Good | Clean dispatch/error/base-class architecture; 3-site ack-scrape anti-pattern |
| Workers/ingest | Good | Derived-queue core excellent (resource-bound gaps fixed 2026-08-02) |
| DB layer | Good | Pool/lock/migration discipline mature (baseline drift fixed + gated 2026-08-02) |
| Web UI | Fair | Hygiene solid (escaping, param SQL, async); no auth/CSRF — accepted risk, see below |
| Tests | Good | 9.3k disciplined tests; gaps cluster in asa_bot + 13 web routes |

## Accepted risks (dated; revisit if posture changes)

- **No web auth / CSRF** *(accepted 2026-08-02: local, single-user, tailnet-only
  deployment)*. `precis_web/config.py::WebConfig.auth_token`
  (`PRECIS_WEB_AUTH_TOKEN`) stays dead config; `routes/console.py` remains an
  unauthenticated generic verb runner as `web:owner`; mutating POST forms carry
  no CSRF token. Narrower `/factory` slice tracked as gripe 171512.
  **Revisit trigger:** the app becomes reachable beyond the tailnet, or gains
  a second user — then wire `auth_token` into Starlette middleware gating
  POSTs + `/console`, and add a same-origin check as interim CSRF.
- **No asa_bot per-user spend caps** *(declined 2026-08-02: trusted-user
  Discord, per-thread `$50` ceiling in `claude_invoke.py::_MAX_USD_CEILING`
  is enough)*. Revisit only if the bot gains untrusted users.

## P2 — robustness and design debt

1. **Render-sandbox Phase 2: network/filesystem jail.** Design written
   2026-08-02 → `docs/proposals/render-sandbox-network-jail.md` (jail
   ladder: podman `--network=none` → macOS seatbelt → Phase-1 floor with
   warning; 3 open questions, notably the Mac runtime posture). Next step:
   resolve open Qs + `/ready`, then it's fixer-buildable. *opus review · effort M*
3. **Real-PG route-SQL tests — close the audited gap.** Policy codified in
   `docs/conventions/testing.md` + full route audit done 2026-08-02: of 18
   routes with raw SQL, 5 have real-PG coverage, 12 are FakeStore-only, and
   `agentlogs.py` has **no tests at all**. Write `tests/precis_web/
   test_<module>_sql.py` companions (shape: `test_status_sql.py`), ranked
   by SQL volume: `tasks.py` (9 raw calls), `preview.py`, `clusters.py`,
   `categorizers.py`, `cad.py` (6 each), `factory.py` (5), `drafts.py` (3),
   `agentlogs.py` (2, untested — do first despite rank), then the 5
   single-query modules (refs, papers, gripes, asks, alerts). Each test
   just executes every raw query once incl. adversarial `%`/`_` input.
   *test-author batches · effort L, slice it*
## P3 — hygiene, scale, coverage

7. **Finish eradicating the ack-scrape idiom.** P2-1 added the structured
   path (`Response.ref_id`/`reused`, `Hub.sibling`) and converted the three
   core sites; the same regex-on-ack idiom survives in `quest/loop.py`,
   `quest/search.py`, `workers/executors/_context.py`, and the plugin
   packages (`precis_bio/protein.py`, `precis_chem/route.py`,
   `precis_pathway/handler.py`). Mechanical now that the API exists.
   *sonnet coder · effort S*

8. **Split `handlers/draft.py` (2 744 lines).** Extract the ~9 hint
   methods → `_draft_hints.py`, table CRUD → `_draft_tables.py`,
   matching the `paper.py`/`_paper_*.py` precedent. Same medicine later
   for `precis_web/routes/drafts.py` (2 361), `status.py` (promote its
   existing `_*_ctx` seams), `refs.py`, `tasks.py`. *sonnet · effort M*
9. **Test-coverage gaps, in value order:** (a) `asa_bot` — 6/13 modules
   untested incl. `bot.py` message loop and `pg_listen.py`
   reconnect/backoff; (b) the 13/34 untested web routes, ops-facing
   `gripes.py` + `clusters.py` first; (c) `workers/executors/
   claude_docker.py` claim/spawn path; (d) spot-check the 21 handler and
   15 utils modules with no test-name match (`_todo_guards.py`,
   `conversation.py`, `compile_guard.py`, `_claude_subprocess.py` stand
   out). *test-author batches · effort L overall, slice it*
10. **Store mixin collision guard.** `store/store.py::Store` composes 22
    mixins on convention alone — add a unit test walking `__mro__`
    asserting no method defined by >1 mixin (cheap, catches silent
    shadowing as the count grows). *haiku/test-author · effort S*
11. **Query-shape cleanups:** N+1 enrichment in `workers/classify.py::
    _enrich` + `workers/axis_pass.py` (3 SELECTs/row → one LAG/LEAD
    window query); per-project recursive CTE loop in
    `handlers/_todo_views.py` (one CTE + GROUP BY); unbounded `/gripes`
    listing (`routes/gripes.py::_rows` — repo already learned this lesson
    in tags paging). *sonnet · effort S each*
12. **`meta` JSON-path filter convention.** ≥19 call sites filter on
    `meta->…` with no GIN/expression index — safe only because always
    paired with indexed `kind`. Either index the hot paths or write the
    "pre-filter by an indexed column" rule into storage docs so the next
    kind doesn't violate it unknowingly. *opus decide, script/sonnet apply · effort S*
13. **Explicit transactions on DELETE+INSERT cascades.**
    `workers/paper_glossary.py`, `store/_blocks_ops.py::
    _replace_card_combined` / `upsert_card_combined` rely on pool
    implicit-commit semantics for the most load-bearing invariant in the
    codebase — wrap in `with conn.transaction():` for auditability; no
    behavior change. *sonnet · effort S*
14. **HNSW recall check at current scale.** `chunk_embeddings` >1 M rows on
    default build params and default `ef_search=40`, never benchmarked —
    measure recall@k; consider `SET LOCAL hnsw.ef_search` per query.
    *sonnet + opus read of results · effort M*
15. **Config nits:** ruff `target-version = "py311"` vs
    `requires-python >= 3.12` (some UP rules never fire); decide coverage
    measurement posture (none exists today — a deliberate "no" is fine,
    document it in `docs/conventions/testing.md`). *script/haiku · effort S*
16. **One-line fixes (batchable in a single tidy pass):** stale
    `handlers/_patent_ingest.py` docstring still lists inline
    `fill_embeddings` (pre-ADR-0007 — invites a bad "restore");
    `tools/core.py::set_runtime` missing its param annotation; bare cursor
    in `store/_identifiers_ops.py::insert_ref_identifiers`; `SELECT *` in
    `pcb/catalog.py`; `routes/cad.py` export filename should key on
    `ref.id` not the raw slug fallback; trust-boundary comment on
    `cli/heartbeat.py`'s `shell=True` (operator-env only); comment/lint
    convention near `store/pool.py` for the five deliberate bare
    `psycopg.connect()` lock-holder sites. *haiku tidy/coder · effort S*

## Explicitly not problems — don't "fix" these

- **safe_fetch** (`utils/safe_fetch.py`): DNS resolve-and-pin at the
  httpcore connect layer (closes rebinding TOCTOU), IPv4-mapped-IPv6
  handling, correct TLS hostname/pool keying, fail-closed backend assert.
- **Raw httpx on fixed keyless API hosts** (`openalex_meta.py`, `orcid.py`,
  S2, Wolfram; `_edgar_client.py` redirects justified in-code) — the
  safe_fetch convention is applied with judgment, not cargo-culted;
  reviewers keep re-flagging these, they're fine.
- **Derived-queue core** (`workers/base.py`, `runner.py`, `embed.py`):
  claim/process/write separation, `EmbedderUnavailable` deferral, poison
  markers, capped waiting-backoff in `chase.py`.
- **Pool tuning** (`store/pool.py` constants tied to pgbouncer semantics),
  `FOR UPDATE SKIP LOCKED` claim discipline, migration checksum
  enforcement + sealed-file PreToolUse guard.
- **Web XSS posture**: Jinja autoescape + `linkify.py` escape-then-Markup +
  named regression test from a real incident; zero `|safe`.
- **Test infra**: per-session DB cloning, connection-leak hard-fail,
  load-gate pinning; no rotting skips/xfails. FakeStore doubles seed from
  `tests/_fakes.py` (shared base + documented can't-parse-SQL limitation).
- **CI**: full suite on real pg17+pgvector, Py 3.12+3.13, SHA-pinned
  actions. Baseline staleness + migration-prefix uniqueness gated in
  `tests/test_schema_baseline.py` (2026-08-02).
