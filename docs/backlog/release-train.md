---
status: draft
title: Release train — qland all, gate one release at 10 shards, fix in place, pipeline the next cut
pillar: platform
prio: high
---

# Release train

Reto 2026-10-23 ~21:30Z (verbatim): "We shard the tests and stuff over the
40 cores. We qland. (change of judgement). we qland all the features when
ready. then we cut the deploy branch. We run CI. completely. It has failures,
we fix, we run failures, then complete CI again (should be fast bc 40
cores!). when pass sqash merge fixes to main deploy that sha (or deploy
branch). (meanwhile more landed on main) when deploy starts, we cut another
deploy branch, if there are any updates, repeat."

Rulings that refine it (Reto 2026-10-23 ~21:50Z):

- A red release is fixed **in the release by the release owner** (`ship
  --release`, which merges forward to main). Failures are not routed back to
  authoring trees.
- **Harness** changes (`scripts/ship`, `scripts/test`, `scripts/lib/`,
  `.github/`) keep `--remote` pre-merge gates; everything else lands
  `--quick`. The `ci/**` trigger stays, for harness only.
- "give everyone 10 concurrent jobs. 10 is fine, we don't need 40. But no
  fewer." Every full CI run (release, harness gate, nightly leg) uses at
  least 10 test shards; a 10-shard gate is 12 jobs (plan + lint + 10), so 3
  fit the 40-job cap (GitHub Pro standard-runner cap,
  `.github/workflows/check.yml` "Job budget" comment).

Replaces memory me499045's "one release at a time" with a pipelined train.
Related: thread `docs/backlog/threads/ship-gate-ci.md` (gr495187),
`docs/runbooks/release-cycle.md`, `release-ci-diff-coverage.md`.

## Measurements (2026-10-23, `gh run view <id> --json jobs`)

| Run | Shards | Slowest shard | Mean pytest step | Non-pytest steps/shard |
|---|---|---|---|---|
| 40018273776 (`ci/`, green) | 10 | 28.5 min | 21.7 min | 1.4–2.0 min |
| 40020802837 (main, green, queued 8 min) | 10 | 34.9 min | 24.7 min | 1.3–1.9 min |
| 39978678657 (`release/r88`, green) | 12 | 28.1 min | 19.4 min | 1.4–2.0 min |
| 39926555648 (`release/r87`, red) | 10 | 35.9 min | 24.7 min | 0.8–1.7 min |

- **Setup is small.** Per shard: ~1.5 min of steps (checkout, node, uv,
  apt pg17 client, postgres service) plus ~1.2 min inside the pytest step
  (shard 9 of 40018273776: `uv sync` 2 s, collection and xdist start 74 s).
  ~2.7 min of a ~28 min shard. A cached image saves at most the ~1.5 min of
  steps; not worth a moving part.
- **The heavy cluster dominates.** One run is ~205–218 runner-min of pytest
  (≈ 410 xdist-worker-min on 2-vCPU runners). The 61 tests ≥ 60 s in
  `tests/shard_weights.json` `seconds` sum to 208 worker-min, half the
  suite; the top eight (6.7–11.3 min each, led by
  `test_pcb_nano_usb_pd_testboard_r2.py::test_nano_board_place_route_drc_baseline`
  at 678 s) are 63 worker-min. Slowest/mean shard is 1.25–1.4: pinning
  balance plus ~30% runner-speed spread.
- **Wall at 10 shards: ~28–36 min.** Fix, in order: (1) cut the heavy
  cluster (split the top pcb/SE/xtb tests by seed or stage, or move fixed
  seed sweeps to the nightly) so no test exceeds ~4 min; (2) re-run
  `scripts/shard-weights` after each such change. Estimated result: ~20 min
  at 10 shards. More shards is off the table per the ruling.

## 1. Landing

**Exists.** `scripts/ship` `--quick` path: dirty-tree refusal, sync,
`_refuse_stale_migration_mint`, `_refuse_stray_adds`,
`_squawk_new_migrations`, the qland drift guard (24 h warn / 48 h refuse via
`scripts/last-gated-main-sha --age-hours`), the pre-qland lint (ruff, mypy,
import contracts, DB-free hygiene tests; gate env `uv sync` catches a broken
lock), the in-lock migration collision guard, CAS squash push. `--remote`:
`remote_gate_wait`, `_remote_gate_admit` (`PRECIS_REMOTE_GATE_SLOTS`=2).
Main pushes run lint only since b92cb5e95 (`plan` output `lint_only`).

**Delta.**

- Classify the diff in `scripts/ship` before any gate: harness = any path
  under `scripts/ship`, `scripts/test`, `scripts/lib/`, `.github/`.
  Harness diff + `--quick`/`--checked` → refuse, naming `--remote`.
  Non-harness diff + `--remote` → refuse, naming `--quick`. `uv.lock`,
  `pyproject.toml`, migrations and schema are not harness: `--quick`.
- Keep every `--quick` refusal listed above. `scripts/qgo-guard` stays a
  deploy-time guard (`/qgo`), not a landing one.
- Docs: CLAUDE.md ship bullets, `docs/conventions/shipping.md` §/land and
  §/qland, `.claude/commands/land.md`, `qland.md`,
  `docs/conventions/testing.md` CI shapes. `.claude/fleet/authority.md` Tier W
  still limits `--quick` to docs/lint (me499045): Reto's file, flag it.

**Accepted risk.** Main can be red at test level for up to one release
cycle; only lint runs on main pushes, and `scripts/main-ci-status` no longer
sees test reds there. The release gate catches them; the drift guard bounds
how long main runs without a full verdict.

**Acceptance.** Tests in `tests/test_ship*.py`: harness diff refused by
`--quick` and `--checked`, non-harness diff refused by `--remote`, a
`uv.lock`-only diff lands under `--quick`.

## 2. Shard width: fixed 10

**Exists.** `scripts/ci-shard-plan` `choose()` picks 12 (≤1 other active
run) or 10 from the live run count; weights tables `10` and `12`;
`max-parallel` 12 (gate) / 20 (nightly); `scripts/last-gated-main-sha`
`GATE_SHARD_COUNTS = (6, 10, 12)`.

**Delta.** Every full run uses exactly 10 shards: `choose()` returns 10
always (keep the `FORCE_SHARDS` input for measurement, floor 10). Drop the
`12` table from `tests/shard_weights.json` and `scripts/shard-weights`
`NS`. Keep 6 and 12 in `GATE_SHARD_COUNTS` until no sha inside its 48 h
window carries them, then drop. Budget comment in check.yml and
`tests/test_ci_shards.py`: release 12 + two harness gates 24 + main lint 2
= 38; nightly (25) + one gate (12) = 37; a fourth gate queues.

Separate slice: the heavy-cluster diet from the Measurements section.

**Acceptance.** Three consecutive release runs show 10 `test-linux (3.13,
K)` jobs; `tests/test_ci_shards.py` pins the arithmetic. Heavy slice: no
test in a fresh `seconds` table above 240 s; release wall ≤ 22 min on three
consecutive runs.

## 3. Fix loop (release owner)

**Exists.** `scripts/release` `ci_red()` exits 2 on a completed red
`lint`/`test-linux*` check-run of the head. `scripts/ship --release`
(`_release_land`, `_release_after_landing`): applies a tree's change onto
`release/r<N>`, then merges forward into main as a `--no-ff` merge, both in
one narrow-lock section; `_release_migration_guard`; `--allow-migration`.
Every `release/**` push runs the full shape, one concurrency group per sha,
never cancelled.

**Delta.**

- The release owner fixes every failure itself, from a tree cut at the
  release head (`coder` sub-agents allowed). No per-failure routing.
  Before each `ship --release` it runs the failing ids locally
  (`scripts/test <nodeid>…`); that is its check, not the gate.
- Fixes reach main at fix time (today's forward merge), not after the
  release is green. Required by §5: the next cut is taken from main and must
  contain every fix of the release it follows.
- **Failures first, in CI, in the same run.** On a `release/**` push, `plan`
  finds the newest completed run on that branch with conclusion `failure`
  whose test-linux shards all uploaded `junit-10-K` artifacts, extracts
  failed and errored node ids (cap 200), and outputs `failed_ids`. A new job
  `failed-first` (pg17 service, same steps as test-linux, `pytest
  <ids>` without `--shard`) runs when `failed_ids` is set; `test-linux`
  `needs` it and runs only if it succeeded or was skipped. No prior red, a
  shard without junit (timeout), or over the cap → straight to the full
  matrix.
  - Why CI, not local: release failures are the ones local lanes missed,
    often runner-specific (2 vCPU, pg17 client, node). Why the same run: the
    exact certificate stays one run on one sha, and no dispatch input or
    driver step is needed. Cost: +~4–6 min on a green head; saves a full
    12-job run when a fix does not fix.
- Readers: `scripts/release` `ci_red()` and `scripts/round` `_red_above()`
  treat a red `failed-first` as red (today the skipped shards would read as
  "not red" until `--ci-timeout`). `last-gated-main-sha` needs no change:
  its exact shape check counts only `lint` and `test-linux*` names.
- Concurrency: `release/**` gets one group per branch with
  `cancel-in-progress: true`. Only the newest head deploys; fixes never
  arrive during a deploy (the journal refuses `ship --release`).

**Acceptance.** Workflow test (`tests/test_ci_shards.py` or a new
`tests/test_check_yml.py`) pins `failed-first` ordering and the
`needs`/`if`; a `plan` unit test extracts ids from fixture junit; a real
release red→fix shows `failed-first` then 10 shards in one run.

## 4. Deploy the green head pinned

**Exists, no delta.** `scripts/round deploy` (exact certificate, `--pinned`,
journal, ff `gated`/`prod`), the playbook's `/healthz` X-Precis-Boot-Sha
check, exit 3 then `--confirm-runtime`, the 03:00–04:20Z no-deploy window.
§5 adds one hook: deploy start triggers the next cut.

## 5. Pipelining

**Exists.** One release at a time: `scripts/round` `cmd_cut` refuses while
round.json records a release or any `release/*` branch exists;
`scripts/release` queues (step 3, `--queue-timeout`); `scripts/ship`
`_release_preflight` refuses unless exactly one `release/*` exists.

**States** (per release record in round state):

| State | Entered by | Accepts fixes | Next cut allowed |
|---|---|---|---|
| `cut` | `round cut` | yes | no |
| `gating` | check.yml run on head in flight | yes (cancels run) | no |
| `fixing` | head red | yes | no |
| `green` | exact certificate on head | yes (→ `gating`) | no |
| `deploying` | `round deploy` writes its journal | no | yes |
| `deployed` | rollout returned, exit 3 | no | yes |
| `confirmed` | `--confirm-runtime`: tag, branch deleted | — | yes |
| `superseded` | the next release deploys over it | — | yes |
| `abandoned` | `round cut --abandon` | — | yes |

**Invariants.** At most one release in `cut`…`green` (the *open* release)
and at most one in `deploying`/`deployed`. Release N+1 is cut only when N
is `deploying` or later **and** origin/main's tree differs from N's head
tree (`git diff --quiet <N head> origin/main` fails), at origin/main, which
must contain N's head (refuse otherwise). N+1 enters `deploying` only after
N is `confirmed`, `superseded` or `abandoned`; `prod` stays fast-forward.

**N's deploy fails while N+1 gates.**

- Rollout fault, code unchanged (host down, playbook error, health
  timeout): resume N at the same pinned sha (journal resume / `--catch-up`).
  N+1 keeps gating and waits.
- Code defect in N (deploy health or runtime check): N takes no fixes. The
  owner fixes it on N+1 via `ship --release`. N+1, once green, deploys with
  `round deploy --supersede r<N>`: allowed only when N+1's head contains N's
  head; N's journal and receipt are kept, N gets no `deployed/r<N>` tag and
  is marked `superseded`. No rollback (prod moves ff-only); prod runs the
  defect until N+1 lands. Today close/abandon refuse a partial deployment;
  `--supersede` is the one exception.

**`ship --release` target.** The open release from round state, not the
count of `release/*` branches. None open (N deploying, N+1 not cut yet):
refuse with "land on main with --quick; it rides r<N+1>". Optional
`--release-branch release/r<N>` must name the open release.

**Delta, code.** Round state holds a list of release records with `state`
(round.json today holds one `release`); `cmd_cut` gates on the invariants
above; `round deploy --supersede`; `_release_preflight` reads round state;
`scripts/release` step 3 stops queueing: S in the open release → ride; else
exit 0 "rides the next cut". Peer marks (`round in|none|eta`) attach to the
open release.

**Acceptance.** `tests/test_round_release.py` cases: cut refused while a
release is `gating`; cut allowed at `deploying` with main moved, refused
with main's tree equal to N's head; `--supersede` refused when N+1 lacks
N's head; `ship --release` targets the open release with two
`release/*` branches present.

## 6. Driver

**Exists.** `scripts/release` (cut → wait → deploy for one sha), `/round`,
`/go`; the owner today is the builder-ce session.

**Recommendation.** A loop script plus one owner session. New
`scripts/release-train` (`--once` | `--watch`): each tick reads round state
and GitHub, takes the one idempotent next step through `scripts/round`
(cut, deploy when green and outside 03:00–04:20Z, cut N+1 at deploy start),
and prints one event line (`red r<N> <ids>`, `green r<N>`,
`deploy-start r<N>`, `cut r<N+1> <sha>`, `runtime-pending r<N>`). The
owner session (builder-ce) runs it under Monitor and does the steps that
need judgment: fixes on `red`, runtime confirmation on `runtime-pending`,
supersede decisions. No unattended daemon: runtime confirmation and fixes
need a session, and an unwatched deploy is `/qgo`'s risk profile.

**Acceptance.** `--once` against fixture state takes exactly one step per
state in the §5 table; a real cycle runs cut → green → deploy → cut N+1
with only the session's fix and confirm steps by hand.

## 7. What goes away, what stays

| Item | Fate |
|---|---|
| `--remote` for non-harness diffs | refused (§1) |
| `ci/**` trigger, `PRECIS_REMOTE_GATE_SLOTS`, `_remote_gate_admit`, `sweep_ci_refs` | stay, harness only |
| `--remote` race retries / hybrid local fallback (`REMOTE_CI_RETRIES`, `REMOTE_FALLBACK`) | stay; revisit once harness gate volume is known |
| `ci-shard-plan` load-based 12 | removed, fixed 10 (§2) |
| `scripts/release` queueing | removed (§5) |
| per-sha never-cancel group for `release/**` | per-branch, cancel-in-progress (§3) |
| nightly full matrix, `coverage-db`, PR trigger, `workflow_dispatch` | stay |
| main lint-only lane (b92cb5e95) | stays |

## Slices

| # | Slice | Depends | Acceptance |
|---|---|---|---|
| 1 | done 72b4c3689 — harness classifier in `scripts/ship`; `--quick` for all non-harness; docs. Reto-only files still need the lane wording: `.claude/commands/land.md` step 4 → `--remote` is the harness lane (non-harness → `--quick`); `.claude/commands/qland.md` → `--quick` is the default lane, harness refused; `.claude/fleet/authority.md` Tier W "--quick is for docs or lint only" is stale under the train — flag to Reto | — | §1 tests; shipping.md/CLAUDE.md agree |
| 2 | done c7ba4cd9b — fixed 10 shards; dropped the `12` table (Reto-only files still saying "6 shards" → 10: `.claude/commands/land.md:69`, `.claude/commands/go.md:78`) | — | §2 first acceptance |
| 3 | Heavy-cluster diet + reweigh — rt-s3: seed/stage/candidate splits + duplicate-solve dedup landed; reweighed from the last green gate junit; the 22-min wall is checked on the next three release runs. needs ruling: nano per-seed sweep + both nano baselines (route realize alone ~250-380 s CI), cad BOM roll-up (~292 s, monolithic `use` expansion), xtb pillar neck (~276 s, single relax) — none fit 240 s without moving to nightly or shrinking a sweep | 2 | no test > 240 s; release wall ≤ 22 min ×3 |
| 4 | `failed-first` job, reader updates, per-branch release concurrency | — | §3 acceptance |
| 5 | Round state as release list; cut at `deploying`; `ship --release` targets open release; no queueing | — | §5 tests except supersede |
| 6 | `round deploy --supersede` | 5 | §5 supersede tests |
| 7 | `scripts/release-train` driver, owner runbook in `docs/runbooks/release-cycle.md` | 5 | §6 acceptance |

## Rulings on the open questions (Reto 2026-10-23 ~23:15Z)

- A code defect in release N's deploy while N+1 gates: N+1 supersedes N,
  no rollback (§5 as written). Reto: "yes."
- `pyproject.toml` and `uv.lock` are not harness: they land `--quick` (§1
  as written). Reto: "no harness".
