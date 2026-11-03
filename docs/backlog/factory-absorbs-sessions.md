---
status: draft
title: The factory absorbs GRIPE_FIX and BUILDER — the integration list
pillar: platform
prio: high
---

# The factory absorbs GRIPE_FIX and BUILDER — the integration list

Source: Reto 2026-11-03, "talk to GRIPE_FIX and BUILDER and assemble the
things we need to integrate all their work into our autonomous work
system". Inputs: BUILDER's inventory (16 recurring hand actions, 6 with a
factory op today) and GRIPE_FIX's (14 hand actions, 11 gaps this week),
both as messages to WORK_MANAGEMENT on 2026-11-03; the unification design
[gripe-todo-unify](gripe-todo-unify.md) (gr554555, landed b89f06a2f); the
factory spec [dark-factory](dark-factory.md). Each item below is a child
todo of td540836 (`thread:factory`), inert (`llm_tier: None`), so the list
is tracked where the work is.

Bottom line. Three things carry most of both sessions' hand work and none
of them exists yet: (1) a **runtime-confirm op** that closes a release
without a session (BUILDER 7–10), (2) **gripe → code todo** per the
unification design, phase 1 starting today with no code (GRIPE_FIX a, b,
e, f, k, l, m), (3) **vendor lanes as executors** so the free Devin and
Codex capacity dispatches from the tick instead of a tmux pane (GRIPE_FIX
c, n). Everything else is a bounded op or a Reto-only ask that stays one.

## A. Status quo, by owner

| hand action | who | factory op today | gap |
|---|---|---|---|
| cut release | BUILDER | `release_cut` | armed in prod? no (`/work`: "release: not reported by any host") |
| CI wait | BUILDER | `ci_verdict_poll` | stale verdict (>6 h) re-run lives only in `scripts/release` |
| red triage: flake vs real, route | BUILDER | release-fix loop body | the classification is judgement; no first mechanical pass (`gh run rerun --failed` once) |
| release fix on `release/rN` | BUILDER | release-fix + second `release_cut` | no op does the fix; resume marker must carry the main sha |
| supersede / abandon | BUILDER + Reto | none | Reto's typed go each time → ask-user |
| deploy | BUILDER | `release_deploy` behind the `deploy rN` approval ask | — |
| rollout check | BUILDER | exit code only | PLAY RECAP failed=0 and the two asserts are not read |
| runtime confirm (~32 min after rollout) | BUILDER | none | `served_sha` + daemon boot shas → `round deploy --confirm-runtime`, `round close` |
| post-deploy pings | BUILDER | `sha-in-prod` gate exists | peers' follow-ups are messages, not gated todos |
| gripe closes after deploy | BUILDER | `gripe_validate` | "shipped dark ≠ done" (gr520236) not checked |
| lock/pin bump on a catpath tag | BUILDER | `dep_check` (detect only) | pyproject + group_vars + `uv lock` + test key re-derive + ship: no op |
| gate image after an engine bump | BUILDER / Reto | none | landing nodes need `precis-dev` + `precis-mcp:premodels`; deploy role does not own them (gr552214 ops half) |
| main-red fix-forward | BUILDER, GRIPE_FIX | none | judgement; no "main red" sentinel mints a fixer |
| harness/CI fixes | BUILDER | none | stays human (`--remote`, Reto/BUILDER) |
| stale worktree cleanup | BUILDER | `reap-worktrees` reports only | apply mode under a capability |
| triage census | GRIPE_FIX | `gripe_triage` tick | human census duplicates it; retire with phase 1 |
| batch + brief in /tmp | GRIPE_FIX | `edit(kind='gripe', mode='fix')` unused | brief becomes the code todo body (unify §6) |
| lane pick + dispatch to Devin/Codex tmux | GRIPE_FIX | `run_slice` for `claude -p` only | no vendor-lane executor; no pick by cost/context |
| SHA watch | GRIPE_FIX | clean `run_slice` exit mints `land_slice` | slices only |
| review, warm reviewer agent | GRIPE_FIX | reviewer generator node, `review: LAND|FIX|REVERT` | verdicts not recorded on the ref; no warm-context reuse |
| fix round | GRIPE_FIX | tick mints a fix task on FIX | — |
| land, mode by hand | GRIPE_FIX | `land_slice --quick` (`--checked` for pr-intake) | mode per diff class; harness diffs must become a `/go` ask |
| post-deploy dry run → Reto go → apply | GRIPE_FIX | none | a `prod-change` plan shape: dry-run task → approval gate → apply task |
| close with `resolved:` | GRIPE_FIX | `gripe_autoclose`, `gripe_validate` | folds into the todo close (unify §5) |
| relay Reto rulings | GRIPE_FIX | ask-user todo | peer read-backs stay messages |
| queue.txt ledger | GRIPE_FIX | `/queue` derivation | goes away with phase 1 |

## B. Build items (todos under td540836)

Ordered by what it unlocks. "Exists" names code to extend, not rebuild.

1. **Runtime-confirm op** (`release_confirm`): after `release_deploy`
   returns, wait for the session MCP respawn, read `precis-status`
   `served_sha` and every required daemon's boot sha, then run
   `scripts/round deploy --confirm-runtime` and `scripts/round close`;
   also read the ansible PLAY RECAP (`failed=0`) and the two asserts
   instead of the exit code alone. Exists: `release_deploy`,
   `src/precis/plans/release.py`, the release-pass template. Unblocks
   BUILDER 7, 8, 9; needs the authority ruling (C1).
2. **Gripe → code todo, phase 1** (unify §6, no code): Reto or an owner
   creates the `Code work from gripes` root; GRIPE_FIX mints the code
   todo at dispatch, records lane/handoff/review/landed on it, converts
   its live queue (8 Reto questions → ask-user todos; held items → todos
   with `not-before` gates; the land queue → todos). Skills:
   `precis-gripe-loop-help`, `gripe-fleet`. Retires GRIPE_FIX a, b, e,
   f, k, l, m.
3. **Gripe → code todo, phase 2** (unify §7 changes 1–6, one slice each):
   triage mints with `route` + `risk`; `land_slice.with_handle` adds the
   gripe handle so land-notify posts `resolved:`; `_stale_gripe_claims`
   honours a live `fixes` todo; risk gates; `design`/`verify` task
   classes; reverse close cascade.
4. **Vendor lanes as executors**: a `run_slice` launcher per lane kind
   (`claude -p` today; Devin SWE-2 and Codex via their CLIs or the tmux
   pane protocol GRIPE_FIX drives), `pick_lane` by cost then fit, a
   model drift guard (a free lane that lands on a paid model is parked,
   GRIPE_FIX gap 7). Exists: `LANES` with `devin`/`codex` declared,
   `enabled=False`. Needs C2.
5. **Land mode per diff class** in `land_slice`: docs → `--quick`; code,
   security, migrations → `--checked`; harness paths (`HARNESS_DIFF`) →
   no land, an ask-user `/go` todo for BUILDER/Reto. Exists:
   `scripts/impacted-tests`, `FACTORY_DENY_WRITE_PATHS`.
6. **Main-red sentinel**: a tick that reads the nightly and the last
   ship verdict on main; red → one `gh run rerun --failed` for the flake
   class, then a fixer slice briefed with the failing tests and the
   commit range (the release-fix body, pointed at main); three reds in a
   row → showstopper ask. Retires BUILDER 3, 14 and GRIPE_FIX h for the
   mechanical half.
7. **Stale CI verdict re-run** in `ci_verdict_poll` (BUILDER 2) and the
   release-fix resume marker carrying the main sha (BUILDER 4).
8. **Supersede/abandon as an ask**: when the release-fix loop's third
   round goes red, or the fix is already on main, mint an approval ask
   whose answer runs `round cut --supersede|--abandon` (BUILDER 5).
9. **Dependency bump op** (`dep_bump`): on a catpath tag, a slice that
   edits pyproject rev + floor, `group_vars` `autocatpath_min_version`,
   `uv lock --upgrade-package`, re-derives the engine-versioned test key,
   ships `--quick`; then the gate image item (10). Exists: `dep_check`.
10. **Gate image on landing nodes**: the deploy role builds or refreshes
    `precis-dev` + `precis-mcp:premodels` for the deploy user, the prune
    spares them, an engine bump re-triggers the build (gr552214 ops half,
    gr544137, BUILDER 13). Until then every bump is an ask-user todo.
11. **"Shipped dark ≠ done"** in `gripe_validate`: a gripe whose fix
    needs a worker/service that is off stays `verify` with a
    `hold:no-autoclose`-style note (gr520236, BUILDER 11).
12. **`prod-change` plan shape** for GRIPE_FIX j: dry-run task (locked
    prod worktree, `scripts/prod-precis`) → approval ask carrying the
    dry-run summary → apply task; Reto's typed go stays the gate.
13. **Peer follow-ups as gated todos**: anything a peer holds "until rN
    is deployed" is a todo blocked by a `sha-in-prod` gate, not a
    message to remember (BUILDER 10).
14. **Worktree cleanup op**: `reap-worktrees --apply` under the
    `repo_checkout` capability, refusing live, dirty or unmerged trees
    (BUILDER 16; the reaper's prod-CLI deletion is already fixed with a
    worktree lock).
15. **Measure the review ruling**: review after land is non-blocking
    (Reto). GRIPE_FIX reports reviewers caught real bugs in about half of
    first rounds. Count `review: FIX|REVERT` verdicts per landed slice for
    two weeks before anyone proposes moving review before land.

## C. Rulings only Reto can give (ask-user todos)

1. **Authority**: a daemon as release owner. `authority.md` makes release
   and deploy Reto-only except the release owner; `release_confirm` and
   an unattended `release_deploy` need that file changed (dark-factory
   step 4). Until then BUILDER deploys ("almost invisible", Reto
   2026-11-03).
2. **Lanes and fallback**: enable `devin` (and `codex`) for `code`, and
   let the tick escalate lane/model on retry instead of asking. Open
   since 2026-11-03; not built without it.
3. **GRIPE_FIX stand-down test day**: do new triaged gripes wait, or go
   to `run_slice` on `claude-sub` (spend)? Who owns the land queue that
   day (BUILDER or the lander)? Needs Reto's OK: the lanes and tmux are
   his spend and attention.
4. **Deploy-user provisioning on melchior** (td551398): `scripts/build-image
   precis-dev` as deploy with `GH_TOKEN`; then the first factory land.

## D. Stays human, by design

From BUILDER's list, never automated, always an ask-user todo: typed go
for supersede/abandon, migrations needing ops, dark-feature enables,
deploy holds; prod SQL writes or DDL (the INVALID
`chunks_keywords_pending_idx` rebuild is pending now and needs an
interactive shell, GRIPE_FIX gap 9); `prod-precis` service writes;
secrets; deploy-user images and CLI plugins; colima VMs; service restarts
outside a deploy; remote branch deletes; spend caps; NAS host-key pins.
Harness and CI fixes (`--remote`) stay with BUILDER.

## Acceptance

- GRIPE_FIX can stand down for a day with nothing in `/tmp`: every open
  item is a todo or an ask in prod (B2).
- A release goes from cut to `deployed/rN` with no session once C1 is
  ruled (B1).
- A triaged gripe reaches main through the tick with the builder on the
  cheapest lane that fits (B3, B4).
- Every item in D that recurs appears as an ask-user todo with the exact
  command, never as a chat question.
