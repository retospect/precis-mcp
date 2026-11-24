---
id: release-red
family: work
title: precis — read a red release or red main and pick the remedy
summary: the release red cause classes (a missing registry row tops the list), the read-every-shard-then-batch fix loop, and the supersede / adopt / stale-record decisions around round cut and scripts/release
answers:
  - the release gate or main went red after quick lands — what broke?
  - a red check names something my diff never touched — what is it?
  - fix the open release in place or land on main and recut — which?
  - scripts/release exited 0 but nothing was cut or deployed — why?
  - when do I use round cut --abandon, cut --supersede, or release --adopt?
applies-to: a red release gate or red main after quick lands; also read before a quick land
status: active
tags: [workflow, troubleshooting]
scope: write
governs: factory
---

# release-red — recognise the cause, pick the remedy

Read this when release CI or main goes red after quick lands — and before
landing with `--quick`, because the lane's slim gate is exactly why these
reds surface late. The quick lane runs ruff, the secret scan, the fast
DB-free guards and mypy on the changed files only
(`docs/conventions/shipping.md` §/qland); full mypy, import contracts,
the rest of the hygiene batch and pytest wait for a checked lane or the
release gate. Most release reds are a check nobody ran, not a bug
somebody wrote.

## Recognise the release red cause classes

By frequency:

1. **A new thing without its registry row** — the diff added an entity a
   closed list doesn't know, and no selected test pins the list: ruff
   lint excludes for a new script, the seed-table lists
   (`SEED_TABLES`/`NOT_VOCAB`, `_PRESERVE_TABLES`), schema-design
   registrations (a `jsonb` column; covering indexes on new FK columns),
   the kind/args ratchets and the dispatch passthrough roster, a
   skill-vocabulary row, the render-dry allowlist, and the real-Postgres
   companion test raw SQL in the web package needs
   (`docs/conventions/testing.md` §Raw SQL). A brief that adds an alert
   id, cadence, CLI verb, list view or handler join must name the skill
   line or allowlist entry to add — that is also the fix's shape.
2. **Time-dependent tests on slower CI runners** — the gate's wall clock
   runs ~1.6x local; a timing budget that fit on the dev box does not.
3. **Environment differences** — engine-gated tests, sudoers syntax, the
   macOS ansible package: what the Linux gate sees that the dev box
   doesn't, and vice versa.
4. **Golden or fake drift after a refactor that never ran pytest** —
   fakes missing a new kwarg, next-literal ratchet baselines, goldens
   that drifted behind the code. Two neighbours share one remedy — move,
   don't re-pin: never edit a reviewed-blob file (new helpers go to a new
   module), and an import-contract break means a shared helper crossed a
   layer — move it to a neutral module.
5. **A migration overloaded a function name** — fresh_db teardown trips
   on the duplicate; fix the drop in test infra
   (`tests/conftest.py::_drop_all_public_objects`), not the migration.

The same "nobody ran the check" shape without a registry row to add:
docstring word-ceilings and CLI-output-text assertions stay silent on
quick lands until the checked lane runs them.

## Read every shard, then ship one batched fix

`scripts/ci-failed <run-id>` digests the failed jobs; [[ci-only-red]] owns
the read when it shows nothing and the fresh-database repro. Wait for
EVERY shard before fixing — a red gate usually carries several, and one
fix per shard re-runs the whole gate each round. Ship one batched release
fix with `scripts/ship --release` from a tree cut on the release head —
[[round]] §Release fixes owns the mechanics. A fix carrying a migration
needs the owner's typed go and goes through `scripts/ship --release
--allow-migration` (squawk and the number-collision check run first).

A checked land failing on a test outside its diff means main went red on
an earlier quick land: fix main forward, then retry.

## Choose the remedy: fix in place, recut, or adopt

- **Small isolated red:** fix in place on the open release
  (`scripts/ship --release`).
- **Big red, or the fix is already on main:** land it on main, then cut
  again — only round cut supersedes; `scripts/release` only resumes the
  in-flight release, never replaces it. `round cut --sha S` takes the
  main-side first-parent sha — never the release head.
- **After a hand cut:** `scripts/release --adopt`. A hand-run `round cut`
  writes no `.release-owner` marker, so a later `scripts/release` rides a
  release it doesn't own and can exit 0 having deployed nothing. Read any
  exit-0 "nothing to deploy" with that caveat — it also describes an
  un-adopted hand cut nobody is driving.
- **Long runs:** launch detached with a log —
  `scripts/release > .claude/release.log 2>&1`. The `!` prefix and
  background tasks have time caps a release outlives; re-running from the
  owner tree resumes.

## Recover a stale record or a failed prod push

A silent exit 0 with nothing cut is a stale round record — check
`scripts/round status` for a record this tree doesn't own. The remedy is
`scripts/round cut --abandon` — a denied `--abandon` goes to a human;
don't improvise around a denial. `cut --supersede` marks the open record
superseded and cuts the next at `origin/main`, deleting the origin
branch when its head is on main — untested for the stale-record case, so
not a substitute for `--abandon` there. Two senses of "supersede" live
here: the policy above is a new cut replacing the in-flight release; the
flag retires one stale record.

`round deploy` moves `gated` before the rollout and `prod` only after
convergence; a failed prod push finishes with `scripts/round deploy
--finish-pointer` — `docs/conventions/shipping.md` owns the
receipt-recovery contract.

## See also

- [[go]] — the single-session release pass; exit table and flake-vs-real triage.
- [[round]] — the coordinator view; §Release fixes owns `scripts/ship --release` mechanics.
- [[ci-only-red]] — read and reproduce a shard red that passes locally.
- [[qland]] / [[land]] — the landing lanes whose slim gate these reds escape.
- `docs/conventions/shipping.md` — ship paths and the recovery mechanics.
- `docs/conventions/testing.md` — the registry rows' own rules.
