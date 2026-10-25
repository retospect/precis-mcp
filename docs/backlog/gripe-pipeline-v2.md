---
status: draft
title: Gripe pipeline v2 (duplicate parents, triage tick, verify-to-close)
pillar: platform
---

# Gripe pipeline v2

Filing stays trivial: no model call, success always (Reto 2026-10-24/25).
Everything after filing is manual today. This spec makes it a ticking
pipeline with one new state, `verify`.

## Problem and evidence

Census 22:45Z 2026-10-24 (`/tmp/gripe-fix/queue.txt`): 376 open, **521 filed
in 24h**, **170 closed**. The 2026-10-25 groom started at 126 open (31
UNBRIEFED, 30 HELD-SPEC, 27 DEPLOY:r105, 10 HELD-OPS, 6 HELD-RETO, 6
BUILDER, 11 QUEUED); only 2 of 5 closes were duplicates. Intake outruns
closure about 3:1 at peak, throttled only by a coordinator's attention.
"Deployed, close when r105 is live" is a hand-kept 27-row list; nothing asks
"did the fix work" (515633 sat at "deployed r101, confirm at runtime").

## What exists vs what is new

| Piece | Exists | New in v2 |
|---|---|---|
| Filing | `GripeHandler.put` + `_insert_gripe` (`handlers/gripe.py`; inserts `refs(kind, title, meta)`), `file_gripe_readonly` (0079, ro-gripes caps) | `gripe_parents` pass after embedding (no model); flood tag |
| Dedup | `args={'observation': True}` files closed; theme registry `_gripe_themes.py`: 10 slugs, folds a match into the `theme:<slug>` keeper as a `+1` comment; `link(rel='supersedes')` (`precis-gripe-help`) | embedding-nearest potential duplicate parents on every gripe; `x N` in listings |
| Statuses | `open triaged ready_for_fix in_review done wontfix`, DB-checked by 0176 triggers; `_KIND_STATUS_VALUES` (`store/types.py`) | one value: `verify` (forward migration, ~12 enumerations, see Slice 1) |
| Semantic search | `search(kind='gripe', q=...)`; `gripe` is in `_ANN_PARTIAL_KINDS` (`store/_chunks_ops.py`): own partial HNSW index on `chunk_embeddings` (0226) | reused by the parents pass; no new index |
| Triage today | `workers/gripe_triage.py`: deterministic, no LLM, daily `gripe_triage` Cadence (`scheduler.py`), dark unless a `service_config` row lights it; one `triage:` PRIO/thread suggestion comment per stale gripe, max 10/fire, never retags or closes | extended, not replaced: see Triage tick |
| Fix lane | `backlog_groom.py` (OFF), `job_types/diagnose_gripe.py`, `fix_gripe.py`; `fixer/` (`writeback.py`, `claude_inproc` flip a landed build to `in_review`); `scripts/gripe-note` comments gripes on deploy | groomer stays OFF; the tick is NOT a `claude_inproc` tool-agent job |
| Manual flow, cost | `/tmp/gripe-fix` census/groom, `issue-closer`, `workers/auto_gripe.py`, `scripts/board`, `fleet-wave ctx`, `precis llm cost` | tick emits the same classes as tags; verify takes the "deployed" half; flow metrics |

## State machine

```
open --triage--> triaged --batch+size--> ready_for_fix --claim--> in_review
  ^                 |                                               | confirmed deploy
  |                 +-- not a bug: triaged + disagreement (Reto)    v
  +---- reopened (STATUS:open + evidence) <--- probe fails ------ verify
                                          done <-- probe ok -------+
```

- `in_review` means "a fix is in flight" (claimed through landing and
  deploy); documented in `precis-tags.md` and `precis-gripe-help.md`
  (today: "a fix landed on a branch; awaits merge", which `fixer/writeback.py`
  and `claude_inproc` already set after a build). `verify` is the only new
  STATUS: entered when a runtime-CONFIRMED release contains the fix sha, or,
  for fixes with no deployable sha, on the `prevents:` comment; left only by
  a probe result.
- `reopened` is `STATUS:open` + tag `reopened` + evidence comment +
  `meta.pipeline.reopened_at`; the next tick takes it first. Only a
  `fix-sha:` comment newer than `reopened_at` counts for the next verify
  entry, so a stale sha cannot bounce it back. `disagreement` is a flag tag
  on a `triaged` gripe, never a STATUS or a close.

## Data model

No new table. Tags, links and `refs.meta` (`jsonb NOT NULL DEFAULT '{}'`;
`_insert_gripe` already writes it).

| Fact | Stored as |
|---|---|
| fix type, size | tags `fix:skill\|code\|mcp\|web\|docs\|infra\|ops\|spec\|reto`, `size:S\|M\|L` |
| cluster / thread | tag `cluster:<slug>` (free-form `ns:value` works on gripes), plus `thread:<slug>` when it maps |
| priority | existing `PRIO` column, 1..10, lower is hotter (written by the tick) |
| dup candidates | `refs.meta.dup_parents`: `[{id, sim, status}]`, once, by `gripe_parents`; the title card shows `potential duplicate parents: gr1(0.91) gr2(0.84)` |
| dup count | `supersedes` links onto the keeper, shown as `x N`. Theme folds are only `+1` comments, so `x N` would undercount: the fold also writes a `supersedes` link (recommended) |
| timestamps | `refs.meta.pipeline`: `triaged_at, fix_claimed_at, deployed_at, verified_at, reopened_at, claimed_at` (UTC `Z`) |
| fix sha | comment `fix-sha: <sha>` by the builder |
| recurrence answer | comment `prevents: skill\|check\|test <path>` or `prevents: none - <reason>` |
| verify probe | comment `probe: ops/probes/gr<id>_*.py` |
| Reto question | tag `reto:ask`, recommendation in a `recommend:` comment |

Migration: 0176 deferred triggers hold the six-value STATUS vocabulary, so
`verify` needs one forward migration replacing it, plus the Slice 1
enumerations. Number: next free after 0243 (latest on main; 0244 and 0245
are on open branches), re-checked at build. The tick writes `STATUS` through
`tag()` so existing guards (terminal needs `resolved:`) apply.

## Intake: potential duplicate parents, no model call

Reto 2026-10-25: no model is called during filing. A worker step attaches
**potential duplicate parents** (embedding search only); the LLM triage tick
sorts through them. Embeddings come from the worker (repo invariant), so the
search cannot run inside `put` without an embedder round trip per filing
(rejected). **Latency is minutes:** the `materialize` Cadence fires every
300 s (`workers/scheduler.py`) but mints an `embed_batch` job only when the
unembedded backlog passes `PRECIS_EMBED_BACKLOG_HIGH` (500) or the oldest
unembedded chunk has waited `PRECIS_EMBED_BACKLOG_AGE_MINUTES` (30)
(`workers/materialize.py`): up to ~5 minutes on a busy backlog, ~35 in a
quiet trickle.

1. `_insert_gripe` is unchanged. A worker pass `gripe_parents` (new Cadence,
   every 5 minutes) takes gripes with an embedding and no `meta.dup_parents`
   and writes the list; key presence is the marker, no hits is `[]`.
2. **Query shape.** The ANN returns chunks, not gripes, and gripes carry
   comment chunks too: `chunk_kinds=['gripe_body']`, oversample 3x
   (`LIMIT 3k`), dedupe per ref keeping the best chunk, cut to k. Candidate
   statuses are pre-filtered with `tags=` on `STATUS:*` (a post-filter
   under-fills), after `_prepare_filtered_ann(conn, limit)`
   (`store/_chunks_ops.py`: iterative scan so the filtered LIMIT fills;
   `gripe` routes to its partial index). Exclude self and gripes already
   linked by `supersedes`.
3. **Defaults (config): k = 8, cosine floor 0.80**; expect ~0.85 after
   calibration.
4. **Candidate statuses.** Recommendation: all non-terminal (`open`,
   `triaged`, `ready_for_fix`, `in_review`, `verify`); `open`+`triaged`
   alone misses a duplicate of a gripe already being fixed or verified.
   Pending Reto. Closed gripes are never candidates.
5. One indexed vector query per gripe, zero LLM tokens. Triage never waits:
   a gripe without `dup_parents` is classified now, `dedup-pending` in meta.
   A theme match at filing still folds as today and skips this pass.

## Flood control

100 gripes per filer per hour (`source:`/`agent:` tag, else DB role). Past
it filing still succeeds; the row gets tag `flood` and skips the parents
pass. The tick folds `flood` rows into one `cluster:flood-<filer>` keeper
and posts a board line; a global 400/hour alarm posts `blocked`. Nothing is
dropped; ro-gripes caps stay tighter.

## Triage tick

**Extends `workers/gripe_triage.py`; no `triage_gripes` job type.** That
module is already scheduler-registered (daily `gripe_triage` Cadence,
`service_config` dark gate, `run_gripe_triage_pass`). Slice 3 keeps its
name, gate and registration and changes the pass: cadence 15 minutes,
`spends=True`, writes tags and PRIO instead of `triage:` suggestion
comments (retired in the same slice, so two triage writers never run);
`load_threads` survives as a `thread:<slug>` prompt hint. It is a **direct
LLM call** (batch in, JSON out, validated), NOT the `claude_inproc`
`diagnose_gripe`/`fix_gripe` tool-agent pattern, and not a coordinator
session (full-context wake-ups are the expensive part of today's flow).

- **Batch:** the 40 oldest `STATUS:open` gripes, `reopened` first (160/hour,
  above the 520/day intake peak). **Per gripe it writes:** `fix:<type>`, `cluster:<slug>` (reusing a slug
  from the live list in the prompt), `size:`, `PRIO`, `STATUS:triaged`;
  `fix:reto` also gets `reto:ask` and a `recommend:` comment. Priority =
  severity (1..5, fixed rubric: wrong result 5, blocked 4, papercut 2,
  wish 1) x (1 + dups) x (1 + age_days / 7), bucketed to PRIO 1..10
  (thresholds in config).
- **Dedup resolution, same call:** for every gripe with a non-empty
  `dup_parents`, the model judges "same root cause?" per pair. Same:
  **suggestion only** (comment `dup-suggest: gr<N> - <why>` plus the pair in
  `meta`); no `supersedes`, no close. Auto-close stays off until a
  labelled-set eval gates it (zero false "same", 95%+ pair agreement on the
  2026-10-25 groom pairs and `/tmp/gripe-fix/triage/A-summary.md`); then
  slice 4 flips it: link `supersedes`, copy distinct facts across (the
  groom's rule), close with `resolved: duplicate of gr<N>`. Different:
  `dup-checked` in meta, never re-judged. Unsure: leave both; severity 4+
  goes to the Reto batch.
- **Reto batch:** `reto:ask` gripes go out as one digest (question,
  recommendation) via the review queue (`.claude/fleet/review-protocol.md`)
  and one `scripts/board post` line.
- **Lease, not a lock.** No `FOR UPDATE SKIP LOCKED` (or any transaction)
  is held across the LLM call: migration 0045 (`30766194a`) replaced a
  transaction spanning per-chunk LLM calls, which starved autovacuum on hot
  tables, with a short-tx lease. Claim = one short tx setting
  `meta.pipeline.claimed_at = now()` on up to 40 gripes where it is absent
  or older than the TTL (30 min), commit; the LLM call runs with no open
  tx; the write tx replaces tags/PRIO, stamps `triaged_at`, clears
  `claimed_at`. Crashed claims expire by TTL; writes are replaces, so a
  doubled tick converges.
- **Disagreement.** On "not a bug" triage never closes: it keeps
  `STATUS:triaged`, adds tag `disagreement` and `triage: not a bug -
  <reason>`; the Reto batch shows both views. Reto rules `wontfix` (reason
  becomes `resolved:`) or overrules to `ready_for_fix`.
- Gripe text is untrusted: quoted in the prompt, no tools, output
  schema-validated before any write.

### Model (Reto's call)

Recommendation (CHAT and Fable review agree): **one hosted Sonnet-class
batch call per tick for triage and dedup together.** A false "same" hides
a real gripe and near-dups often share vocabulary, not cause (2 true
duplicates among 5 groom closes); one call avoids a local/hosted split, and
suggestion-only dedup means the choice cannot lose a gripe. Open question.

## Fix discipline

- **Batch by cluster:** builders are briefed per `cluster:<slug>` by top
  PRIO. `ready_for_fix` means size is set and, for `fix:spec`, a spec exists.
  3+ live gripes in a cluster with no backlog file get a spec todo (not
  code) and tag `spec:pending`.
- **Every fix answers "what stops this recurring".** Entering `verify`
  requires a `prevents:` comment (skill edit, check or test path, or
  `prevents: none - <reason>`, which goes to the Reto batch). The gate sits
  in `gripe.py` beside `_require_resolved_comment`; refusals name the format.

## Verify-to-close

Done means prod shows it gone, not that the fix merged.

1. **Claim:** the builder (or `issue-closer`) comments `fix-sha: <sha>`; the
   gripe is `in_review`. After a reopen only a `fix-sha:` newer than
   `meta.pipeline.reopened_at` counts.
2. **Trigger: runtime-CONFIRMED releases only.** `scripts/release` exits 3
   when the rollout is done but runtime is pending (the exit a deploy takes
   in practice; `scripts/gripe-note` already comments "runtime confirmation
   pending"). Exit 3 must NOT move a gripe: the build may still be bad. The
   step runs once `scripts/round deploy --confirm-runtime <sha>` records
   `runtime_confirmed_at`: `in_review` gripes whose newest eligible
   `fix-sha` is an ancestor of the confirmed sha (`git merge-base
   --is-ancestor`) move to `verify`, `deployed_at` is stamped, one board
   line posts. Replaces the `DEPLOY:r105` list.
3. **No deployable sha** (`fix:skill|docs|ops|reto`): these enter `verify`
   on the `prevents:` comment, not on a deploy.
4. **Probe author:** the fixer, in the fix commit: a read-only check at
   `ops/probes/gr<id>_*.py` (NOT under `tests/`: pytest would collect it),
   named in a `probe: <path>` comment. The gripe body is untrusted text,
   never executed: no "run the repro in the gripe".
5. **Probe execution is read-only by construction.** `scripts/prod-psql`
   defaults to `agent_rw`, so probes never call it bare. They run ONLY as
   `agent_ro` (`--ro`; the `write:none` role of `envelope.py::db_role`) in
   a read-only container: no shell, no network beyond that DB role (shape
   fixed in slice 7, no wider than that envelope). No hostnames or
   addresses (public repo).
6. **Verify tick:** a worker or `scripts/gripe-verify` runs each probe.
   Pass: comment `resolved: verified <probe> at <sha>`, `STATUS:done`.
   Fail: `STATUS:open`, tag `reopened`, `reopened_at`, probe output as
   comment, `PRIO` one step hotter; a second reopen adds `disagreement`
   and goes to the Reto batch.
7. **No probe:** one read-only check by a Sonnet-class agent from the
   `prevents:`/`fix-sha:` comments and changed files, never the gripe body.
   Inconclusive stays in `verify`, listed on the board; never closes.

## Metrics and flags

- **Flow:** `scripts/gripe-flow` prints in/out per UTC day and median time
  per stage (`meta.pipeline`); the coordinator sweep posts it to the board.
- **Cost:** ticks log `llm_call_log.source` `gripe_triage:*` /
  `gripe_verify:*`; `precis llm cost` gains `--per-closed`. Builders put
  `cost: <lane> <tokens>` in the fix comment.
- **Stuck builder:** `fleet-wave` posts `scripts/board post <window> --state
  wedged` once for a `size:S` gripe claimed over 2 hours ago.

## Explicitly NOT in scope

A model call in filing; age-based closing; changes to `file_gripe_readonly`
or ro-gripes caps; auto-fixing (`backlog_groom` stays OFF); a new MCP verb.

## Slices

Order: 1, 2, 3, then 5, 4, 6, 7, 8.

| # | Slice | Size | Opus review |
|---|---|---|---|
| 1 | Migration + `verify` across the enumerations below | M | yes (trigger SQL) |
| 2 | `gripe_parents` pass + title-card line | M | yes (ANN query, lease) |
| 3 | `gripe_triage.py` extended: LLM tick, PRIO, Reto batch, disagreement | L | no |
| 4 | Dedup auto-close, gated on the labelled-set eval | M | no |
| 5 | `x N` in listings (incl. theme-fold links), sort by score | S | no |
| 6 | `prevents:` gate, cluster-of-3 spec trigger | M | no |
| 7 | Confirmed-release trigger + verify tick + `ops/probes/` | L | yes (release path, probe sandbox) |
| 8 | `gripe-flow`, per-closed cost rollup, stuck flag, flood tag and alarm | M | no |

### Slice 1: every `verify` enumeration

From `rg 'ready_for_fix|in_review'`; re-run at build. Paths under `src/precis/`:

- DB and store: new migration (0176 triggers); `store/types.py`
  `_KIND_STATUS_VALUES["gripe"]` + tag-value list (~L1210);
  `handlers/gripe.py` error text (~L221).
- Live lists that must include `verify`: `quest/needs.py`
  `_LIVE_GRIPE_STATUSES`, `workers/call_errors.py` `_LIVE_STATUSES`,
  `workers/health_digest.py` (~L2550).
- Writers that must not downgrade a `verify` gripe: `handlers/job.py`
  (~L2032), `workers/job_types/fix_gripe.py`,
  `workers/executors/claude_inproc.py` (~L1479), `fixer/tick.py`,
  `fixer/writeback.py`.
- Web: `precis_web/routes/gripes.py` (~L64, L82, L99),
  `precis_web/__init__.py` (~L170), `templates/gripes/list.html.j2`.
- Text: `data/prompts/doctor-prompt.md`; skills `precis-tags.md` (L231, plus
  the `in_review` = in-fix reading), `precis-gripe-help.md` (L121, L556),
  `precis-fix-gripe-help.md`, eval `skill-evals/precis-gripe-help.yaml`;
  `.claude/agents/gripe-filer.md`, `issue-closer.md`;
  `docs/runbooks/tmux-worker-launch.md` (L75).

## Acceptance criteria

- Filing makes no model call and no embedder call; the ack is unchanged.
  Once the embedding exists (up to ~35 minutes in a quiet trickle), the next
  `gripe_parents` run writes `meta.dup_parents`: non-terminal gripes only,
  `gripe_body` chunks only, one entry per gripe, at or above the floor.
- `STATUS:verify` is accepted on gripes only. A tick over 40 seeded gripes
  writes `fix:`, `cluster:`, `size:`, PRIO and `STATUS:triaged`; a second
  run changes nothing. No transaction is held across the LLM call; an
  expired `claimed_at` is re-claimed.
- A seeded true-duplicate pair gets a `dup-suggest:` comment only (linked
  and closed after slice 4's eval); a near-miss gets neither. A "not a bug"
  ruling stays open as `disagreement`. Listings show `x N` = `supersedes`
  links onto the row.
- A runtime-confirmed release containing a `fix-sha` moves its gripe to
  `verify`; exit 3 alone moves nothing. A passing probe (run as `agent_ro`)
  closes it, a failing one reopens it with the output, and the next deploy
  does not re-verify it on the old sha.
- `gripe-flow` prints the medians; `precis llm cost --per-closed` works; a
  `size:S` builder past 2 hours yields one `wedged` line.
- No hostnames or IPs in new files; forward-only migration.

## Open questions / decisions log

Decided: `in_review` = in-fix (`precis-tags.md`); similarity 0.80 / k=8 as
config defaults (expect ~0.85); probes in `ops/probes/`; dedup suggests only
until a labelled-set eval gates auto-close.

- **Model (Reto):** one hosted Sonnet-class batch call per tick for triage
  and dedup together (recommended, CHAT and Fable), or a local classifier
  plus hosted dedup.
- **Dup candidate statuses (Reto):** all non-terminal (recommended) or just
  `open`+`triaged`.
- **Theme folds:** link with `supersedes` (recommended) or accept `x N`
  undercounting.
- **Calibration:** run 0.80 / k=8 on the 121 open gripes of the 2026-10-25
  groom plus known duplicate pairs before slice 2.
- **Flood caps** (100/h per filer, 400/h global) and the **verify hook
  point** (inside `round deploy --confirm-runtime` or a follow-up step):
  confirm in slice 7.
