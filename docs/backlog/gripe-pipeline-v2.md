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

From `/tmp/gripe-fix/queue.txt` (census line, 22:45Z 2026-10-24): 376 open
(327 dogfood), **521 filed in 24h** (417 dogfood), **170 closed in 24h**. The
groom pass of 2026-10-25 (`/tmp/gripe-fix/groom-summary.md`) started at 126
open and found: 31 UNBRIEFED, 30 HELD-SPEC, 27 DEPLOY:r105, 10 HELD-OPS, 6
HELD-RETO, 6 BUILDER, 11 QUEUED. Only 2 of 5 closes were duplicates, and the
earlier census had already merged the rest by hand.

Intake outruns closure about 3:1 at peak and the only throttle is a
coordinator's attention. "Deployed, close when r105 is live" is a hand-kept
27-row list. Nothing asks "did the fix work": 515633 sat at "deployed r101,
confirm at runtime".

## What exists vs what is new

| Piece | Exists | New in v2 |
|---|---|---|
| Filing | `GripeHandler.put` + `_insert_gripe` (`handlers/gripe.py`), read-only filing via `file_gripe_readonly` (0079) | `gripe_parents` pass after embedding (open+triaged only, no model); flood tag |
| Dedup, observation | `args={'observation': True}` files closed; theme registry `_gripe_themes.py` (e49d4484a): 10 slugs, folds a match into the `theme:<slug>` keeper as `+1` | embedding-nearest potential duplicate parents on every gripe; themes stay the exact path |
| Statuses | `open triaged ready_for_fix in_review done wontfix`, DB-checked by 0176 triggers; `_KIND_STATUS_VALUES` | one value: `verify` (forward migration) |
| Dup links | `link(rel='supersedes')`, `precis-gripe-help` "soft-delete a duplicate" | `x N` derived from those links in listings |
| Semantic search | `search(kind='gripe', q=...)`, HNSW per kind, `chunk_embeddings` (0226, 0243) | reused by the parents pass; no new index |
| Read-only filing | ro-gripes spec (`ro-gripes.md`, branch `worktree-ro-gripes`): cap 3/run, 20/h/role, `source:agent_ro` | shares the flood tag; v2 does not touch its SQL |
| Auto gripes | `workers/auto_gripe.py` (AUTO_PRIO 7, auto-close on clear), call_errors cadence | triaged like any other; `source` tag read as a signal |
| Promotion | `workers/backlog_groom.py` (OFF), `job_types/diagnose_gripe.py`, `fix_gripe.py` | tick reuses the job-type pattern; groomer stays OFF |
| Manual flow, closing | `/tmp/gripe-fix` census/triage/groom, `issue-closer` agent (closes only what a commit visibly fixes) | the tick emits the same classes as tags; verify tick takes the "deployed" half |
| Fleet, cost | `scripts/board`, `fleet-wave ctx`, `precis llm cost` (`cli/llm.py`) | flow metrics, stuck flag, per-closed-gripe rollup |

## State machine

```
open --triage--> triaged --batch+size--> ready_for_fix --claim--> in_review
  ^                 |                                               | deploy
  |                 +-- not a bug: triaged + disagreement (Reto)    v
  +---- reopened (STATUS:open + evidence) <--- probe fails ------ verify
                                          done <-- probe ok -------+
```

- `in_review` is read as "a fix is in flight" (claimed through landing);
  no second new value. `verify` is the only new STATUS: entered when the fix
  sha is an ancestor of `origin/prod`, left only by a probe result.
- `reopened` is `STATUS:open` + tag `reopened` + an evidence comment; the
  next tick takes it first. `disagreement` is a flag tag on a `triaged`
  gripe, never a STATUS or a close.

## Data model

No new table. Tags, links and `refs.meta` (confirm the gripe ref carries
`meta` at build time; else a `pipeline:` comment kind is the fallback).

| Fact | Stored as |
|---|---|
| fix type | tag `fix:skill\|code\|mcp\|web\|docs\|infra\|ops\|spec\|reto` |
| cluster / thread | tag `cluster:<slug>` (free-form `ns:value` already works on gripes), plus `thread:<slug>` when it maps |
| size | tag `size:S\|M\|L` |
| priority | existing `PRIO` column, 1..10, lower is hotter (written by the tick) |
| dup candidates | `refs.meta.dup_parents`: `[{id, sim, status}]`, written once by the `gripe_parents` pass |
| dup count | derived: count of `supersedes` links onto the keeper; shown as `x N` |
| timestamps | `refs.meta.pipeline`: `triaged_at, fix_claimed_at, deployed_at, verified_at` (UTC `Z`) |
| fix sha | comment `fix-sha: <sha>` by the builder, parsed at deploy |
| recurrence answer | comment `prevents: skill\|check\|test <path>` or `prevents: none - <reason>` |
| verify probe | comment `probe: <path>` (see Verify) |
| Reto question | tag `reto:ask`, recommendation in a `recommend:` comment |

Migration: STATUS is DB-checked (0176 deferred triggers hold the six-value
vocabulary). `verify` needs one forward migration replacing that value list,
plus `_KIND_STATUS_VALUES` and the `precis-tags` table; the number is checked
at build time (latest today 0243). Opus review: yes, it edits a commit-time
trigger.

The tick writes `STATUS` through `tag()` so existing guards (terminal needs
a `resolved:` comment) apply.

## Intake: potential duplicate parents, no model call

Reto 2026-10-25: no model is called during filing. Filing does an embedding
search only, against gripes still in `open` or `triaged`, and attaches the
result as **potential duplicate parents**. The LLM triage pass, which runs
soon after, sorts through them. The filer gets success back regardless.

Embedding timing: embeddings come from the worker, not the filing path
(repo invariant), so at filing time the new gripe has `embedding IS NULL`
and a synchronous search is not possible without embedding inside `put`
(adds a model/embedder round trip to every filing; rejected). So the search
is a worker step that runs as soon as the embedding exists:

1. `_insert_gripe` is unchanged: no search, no model call, no new latency.
2. A worker pass `gripe_parents` runs right after the embedding fill (same
   cadence family as `auto_gripe`, target a minute or two). For each gripe
   that has an embedding and no `meta.dup_parents`, it runs one HNSW query
   over gripe body chunks and writes the list. Idempotent: key presence is
   the marker; no hits is written as `[]`.
3. **k = 8**, cosine **floor 0.80** as a first guess (calibrate, see Open
   questions). Best chunk per gripe; exclude self and gripes already linked
   by `supersedes`.
4. **Statuses searched: `open` and `triaged` only.** Closed gripes are not
   candidates. A regression of a closed gripe is caught by verify-to-close
   and by the next filing becoming a fresh gripe that triage can link.
5. Cost: one indexed vector query per gripe, zero LLM tokens (about 1 per
   3 minutes at peak).
6. Triage never waits on it: a gripe reaching the tick without `dup_parents`
   is classified now and `dedup-pending` in meta; parents resolve next tick.
7. A theme match at filing still folds as today and skips this pass.

Readers (tick, session, or an agent sent over a batch) use `meta.dup_parents`
via `get(kind='gripe', id=N)`; the title card adds one line,
`potential duplicate parents: gr1(0.91) gr2(0.84)`.

## Flood control

Cap is "rather large": 100 gripes per filer per hour (filer = `source:`/
`agent:` tag, else DB role). Past it filing still succeeds; the row gets tag
`flood` and skips the parents pass. The tick folds `flood` rows into one
`cluster:flood-<filer>` keeper and posts one board line. Global alarm: 400
per hour posts `blocked`. ro-gripes caps (3/run, 20/h/role) stay tighter and
authoritative. Nothing is dropped, expired or refused.

## Triage tick

A worker job type `triage_gripes` (pattern of `diagnose_gripe`), not a
coordinator session: coordinator wake-ups at full context are the expensive
part of today's flow.

- **Cadence:** every 15 minutes; also runs on demand. Batch: the 40 oldest
  `STATUS:open` gripes (parents present or pending), `reopened` first. Under
  flood the backlog drains 160/hour, above the 520/day intake peak.
- **Per gripe it writes:** `fix:<type>`, `cluster:<slug>` (reusing an
  existing slug when one fits, from the live cluster list in the prompt),
  `size:`, `PRIO`, and `STATUS:triaged`. Gripes with `fix:reto` also get
  `reto:ask` and a `recommend:` comment.
- **Priority = severity x dup count x age.** Severity 1..5 comes from the
  model with a fixed rubric (data loss or wrong result 5, blocked workflow 4,
  papercut 2, wish 1). Score = severity x (1 + dups) x (1 + age_days / 7);
  PRIO is the score bucketed to 1..10 (thresholds in config, not code).
- **Dedup resolution, batched:** for every gripe whose potential-parents list is
  non-empty, the tick judges "same root cause?" per pair. Same: it links
  `supersedes`, comments the distinct facts copied across (the groom's
  rule: copy facts, then link), and closes the duplicate with
  `resolved: duplicate of gr<N>`. Different: records `dup-checked` in meta so
  the pair is not re-judged. Unsure: leaves both, adds the pair to the Reto
  batch only when severity is 4+.
- **Reto batch:** all `reto:ask` gripes since the last batch go out as one
  digest (question, recommendation) via the review queue
  (`.claude/fleet/review-protocol.md`) and one `scripts/board post` line.
- **Idempotent:** selects `STATUS:open` with no `meta.pipeline.triaged_at`
  (or `reopened`), claims with `FOR UPDATE SKIP LOCKED`, and every write is
  a replace, so a crashed or doubled tick converges.
- Gripe text is untrusted data: quoted block in the prompt, no tools beyond
  the tick's own writes.

### Model flag: classification vs dedup resolution

Reto approved a local model for classification; on 2026-10-25 he described
the triage pass as a normal model running soon after filing. **Flag: dedup resolution is
a harder judgment and probably needs a bigger model.** Classification
(type, size, cluster slug from a closed list, severity) is a lookup with a
rubric; an error is a wrong tag, fixed on the next pass. Dedup resolution
compares two bodies for the same root cause, and a false "same" closes a
real gripe. Many of today's near-dups share vocabulary but not cause (the
groom found 2 true duplicates among 5 closes after the census had merged the
obvious ones).

Recommendation: one tick, two passes. Pass A (local): all classification.
Pass B (hosted mid-tier, Sonnet or Haiku class): only pairs above the floor,
about 40 per tick. Pass B applies the `supersedes` link at once but defers
closing the duplicate by one tick, when the pair must agree again. Decide on
a labelled set (the 2026-10-25 groom pairs and the closes in
`/tmp/gripe-fix/triage/A-summary.md`): if local matches the hosted model on
95%+ of pairs with no false "same", drop pass B.

### Disagreement

Filer and triage can disagree ("not a bug": by design, already fixed, wrong
tool). Triage writes its ruling and reason but never closes on it: it keeps
`STATUS:triaged`, adds tag `disagreement` and a comment `triage: not a bug -
<reason>`, and the next Reto batch shows both views. Reto rules `wontfix`
(reason becomes the `resolved:` text) or overrules to `ready_for_fix`.

## Fix discipline

- **Batch by cluster.** Builders are briefed per `cluster:<slug>` (today's
  `batch-*.md` briefs), ordered by the cluster's top PRIO. `ready_for_fix`
  means size is set and, for `fix:spec`, a spec exists.
- **Cluster of 3 or more triggers a spec:** a cluster with 3+ live gripes
  and no linked backlog file gets a todo for the spec agent (not code) and
  tag `spec:pending` on its gripes.
- **Every fix answers "what stops this recurring".** Moving to `verify`
  requires a `prevents:` comment naming a skill edit, check or test path, or
  `prevents: none - <reason>` (goes to the Reto batch). The gate lives in
  `gripe.py` beside `_require_resolved_comment`, refusal names the format.

## Verify-to-close

Done means prod shows it gone, not that the fix merged.

1. **Claim:** the builder (or `issue-closer`) comments `fix-sha: <sha>` and
   the gripe is `in_review`.
2. **Deploy trigger:** `scripts/release` already knows the deployed sha. A
   post-deploy step lists `in_review` gripes with a `fix-sha` that is an
   ancestor of the deployed sha (`git merge-base --is-ancestor`), moves them
   to `verify`, stamps `meta.pipeline.deployed_at`, and posts one board line
   (`done <sha> verify: gr.. gr..`). This replaces the hand-kept
   `DEPLOY:r105` list.
3. **Probe author:** the fixer, in the fix commit. A probe is a read-only
   check at `tests/prod_probes/gr<id>_*.py` (or the fix's own test, when it
   is runnable against prod read-only), named in a `probe: <path>` comment.
   Probes use the `precis` MCP read path or `scripts/prod-psql`
   (read-only) and carry no hostnames or addresses (public repo).
4. **Verify tick:** a worker or `scripts/gripe-verify` runs each `verify`
   gripe's probe on prod. Pass: comment `resolved: verified <probe> at
   <sha>`, `STATUS:done`. Fail: `STATUS:open`, tag `reopened`, comment with
   the probe output, `PRIO` one step hotter; a second reopen adds
   `disagreement` and goes to the Reto batch.
5. **No probe** (docs, skill, many ops fixes): one attempt by a Sonnet-class
   agent from the repro in the gripe body. Inconclusive keeps the gripe in
   `verify` and lists it on the board; it never closes on inconclusive and
   never times out.

## Metrics and flags

- **Flow on the board:** `scripts/gripe-flow` prints in/out per UTC day and
  median time open to triaged, triaged to `verify`, `verify` to done (from
  `meta.pipeline`); the coordinator sweep posts the summary to `scripts/board`.
- **Tokens per closed gripe per lane:** ticks log `llm_call_log.source` as
  `gripe_triage:*` / `gripe_verify:*` with the gripe id as ref; `precis llm
  cost --days N` already splits by lane/pass/ref, plus a `--per-closed`
  rollup. Builder lanes are not in `llm_call_log`; they put `cost: <lane>
  <tokens>` in the fix comment.
- **Stuck builder:** the `fleet-wave` sweep (which already reads pane context
  via `ctx`; ctx-guard untouched) posts `scripts/board post <window> --state
  wedged` once for a window whose `.claude/purpose` names a `size:S` gripe
  claimed (`fix_claimed_at`) more than 2 hours ago.

## Explicitly NOT in scope

Any model call in the filing path; expiry or age-based closing (gripes get closed, not aged out); changes to
`file_gripe_readonly` or the ro-gripes caps; auto-fixing (`backlog_groom`
stays OFF); a new MCP tool or verb; web UI beyond `x N`.

## Slices

| # | Slice | Size | Opus review |
|---|---|---|---|
| 1 | Migration + `_KIND_STATUS_VALUES` + skill table for `verify` | S | yes (trigger SQL) |
| 2 | `gripe_parents` pass + title-card line | M | yes (HNSW query, claim) |
| 3 | `triage_gripes` job: classification, PRIO, Reto batch, disagreement | L | no |
| 4 | Dedup resolution in the tick (pass B, labelled-set eval) | M | no |
| 5 | `x N` in listings, sort by score | S | no |
| 6 | `prevents:` gate, cluster-of-3 spec trigger | M | no |
| 7 | Deploy trigger + verify tick + probe layout | L | yes (release path) |
| 8 | `gripe-flow`, per-closed cost rollup, stuck flag | M | no |
| 9 | Flood tag and global alarm | S | no |

Order: 1, 2, 3, then 5, 4, 6, 7, 8, 9.

## Acceptance criteria

- Filing makes no model call and no embedder call; the ack is unchanged.
  Once the embedding exists, `meta.dup_parents` appears (target 5 minutes)
  listing only `open`/`triaged` gripes at or above the floor.
- `STATUS:verify` is accepted on gripes only.
- A tick over 40 seeded gripes writes `fix:`, `cluster:`, `size:`, PRIO and
  `STATUS:triaged`; a second run changes nothing.
- A seeded true-duplicate pair ends linked and closed; a near-miss pair
  stays open. A "not a bug" ruling stays open as `disagreement` in the Reto
  batch. Listings show `x N` = `supersedes` links onto the row.
- A deploy containing a `fix-sha` moves its gripe to `verify`; a passing
  probe closes it, a failing one reopens it with the output.
- `scripts/gripe-flow` prints the medians; `precis llm cost` reports tokens
  per closed gripe per lane; a `size:S` builder past 2 hours yields one
  `wedged` board line.
- No hostnames or IPs in new files; forward-only migration.
## Open questions / decisions log
- **Similarity floor and k:** 0.80 and 8 are guesses. Calibrate on the 121
  open gripes of the 2026-10-25 groom plus the known duplicate pairs before
  slice 2 lands.
- **Triage model:** is local enough for dedup resolution (see flag), or does
  pass B run on a hosted mid-tier? Measure on the labelled set. (Reto)
- **Dup link direction:** the groom linked the duplicate to the keeper with
  `supersedes`. Pin it and count inbound links; reuse beats a new rel
  (migration).
- **`in_review` as "in fix":** accept this reading (recommended, one new
  STATUS) or add a distinct `in_fix` (second migration value)?
- **Does the gripe ref carry `meta`?** If not, `meta.pipeline` and
  `dup_parents` need another home (machine comment excluded from embeddings).
- **Flood cap numbers:** 100/h per filer, 400/h global; confirm "rather large".
- **Probe location:** `tests/prod_probes/` vs beside the fix test; which lane runs them.
