---
id: precis-factory-submit-help
family: work
title: precis — submit code work to the dark factory
summary: the one recipe — a plan root, build + reviewer nodes with a self-contained brief, lane pick; the reviewer gates the land; wait on the node for the outcome; how to read a failed slice
answers:
  - how do I submit a coding task to the dark factory?
  - what todo nodes does a factory plan need and what meta do they carry?
  - how do I pick the builder lane and the review lane?
  - how do I learn whether my factory submission landed or failed?
  - how do I read a failed slice and hand it back to the builder?
  - how does a gripe fix or a spec section become a factory plan?
applies-to: put/link/tag/get (kind='todo') plan nodes; put(kind='todo', mode='plan', args={'template': …})
tags: [workflow]
kinds: [todo]
governs: [factory]
status: active
scope: write
---

# precis-factory-submit-help — submit code work to the dark factory

The dark factory turns a code task into builder, reviewer and lander
jobs on the worker fleet. A plan is a todo subtree the `plan_dispatch`
tick drives: it dispatches jobs for free nodes, evaluates gates and
reads verdicts — **pull-only**: nothing pushes a result back to you.
You learn the outcome by re-reading the tree, or by the one-shot wait
`get(kind='todo', id=<node>, args={'wait': <seconds>})`, which returns
when the node's STATUS turns terminal (`done` / `won't-do` /
`auto-timeout`, 1 h cap per call — re-issue to keep waiting).

Factory inputs are exactly two (Reto 2026-11-12): a **gripe** (use
`gripe-fix`/`gripe-fix-direct`) and a **spec section** (use
`spec-build`). Prefer the templates — they mint the right node shapes
in one call. The hand recipe below is for work that fits neither.

```python
put(kind="todo", mode="plan", args={"template": "gripe-fix", "gripe_id": 570506})
put(kind="todo", mode="plan",
    args={"template": "spec-build", "section": "<spec path>.md~<Heading>"})
```

## The plan shape — the hand recipe

Mint a **root** (`meta.auto_close` closes it when every child is
terminal), a **build node** (`meta.op` — `run_slice` is the default
job), and a **reviewer** (`meta.generate` with `role: 'reviewer'`,
`from: 'td<build>'`, `preland_review: true`), the reviewer
`blocked-by` the build. A `task_class` picks the lane class (`code` is
the default; `review` on the reviewer); pin a lane in
`meta.op.params.lane` when you must.

```python
# 1. the plan root (auto_closes when its children settle)
put(kind="todo", text="plan: add --json to the board export",
    prio=3, meta={"auto_close": True, "llm_tier": None})
#    → td<root>; tag it so the tick sees the subtree:
tag(kind="todo", id=<root>, add=["plan:<root>"])

# 2. the build — the body IS the brief, self-contained: the lane may
#    have no precis MCP, so inline every fact it needs
put(kind="todo", parent_id=<root>,
    text="Add --json to the board export",
    body="Implement … Acceptance: scripts/test tests/test_board.py. "
         "Commit on this branch and stop — the lander lands it.",
    tags=["plan:<root>"],
    meta={"llm_tier": None, "op": {"type": "run_slice", "params": {"lane": "devin"}},
          "task_class": "code"})

# 3. the pre-land reviewer — gates the land on its verdict
put(kind="todo", parent_id=<root>,
    text="Review the board-export build before land",
    tags=["plan:<root>"],
    meta={"llm_tier": None,
          "generate": {"role": "reviewer", "from": "td<build>"},
          "task_class": "review", "preland_review": True})
link(kind="todo", id=<reviewer>, target="todo:<build>", rel="blocked-by")
```

Every `put` inside a live plan lands held by `waiting-for:mint` — the
mint barrier. Settle each node's links and body, then drop the hold
(`tag(..., remove=["waiting-for:mint"])`); the tick dispatches only
unheld, unblocked nodes. Always pass `meta={"llm_tier": None}`: a
parented put otherwise defaults `llm_tier='opus'` and double-dispatches
a `plan_tick` ([[precis-todo-tree-help]]). Never give a builder node
`meta.executor` or an `executor:*` tag — the tick owns the jobs.

## What the pipeline does with it

1. **build** — `run_slice` launches the lane's vendor in a worktree on
   `slice/<todo id>` with the body as its brief. A clean committed
   exit pushes the branch to origin and, when a `preland_review`
   reviewer gates it, stamps `meta.pending_land` and closes the todo —
   the land is held, not minted.
2. **review** — the released reviewer (another vendor than the
   builder) reads `git diff origin/main...origin/slice/<id>` and posts
   one verdict comment: `review: LAND` · `review: FIX — <findings>` ·
   `review: REVERT — <why>`. LAND mints the held `land_slice`;
   FIX/REVERT send the branch back to the builder with the findings as
   its handoff — nothing is on main to fix or revert.
3. **land** — `land_slice` runs `scripts/ship --quick`: rebase, lint,
   squash-merge onto main, `landed: <sha>` on the todo, then deletes
   branch and worktree. **No pull requests, ever** — `gh pr` is denied
   in the lane settings; the squash merge is the only path to main.
4. Downstream nodes — a `sha-in-prod` gate fills from the `landed:`
   sha, a validate task re-proves the fix. The root closes when they
   settle. A `blocked-by` edge to the build (or to the reviewer) holds
   its dependant through the whole hold — the review wait and the
   in-flight land — since `done` is "built", not "on main" (gr659981).

## Wait for the outcome

```python
get(kind="todo", id=<build>, args={"wait": 900})   # blocks ≤15 min
```

The wait returns a `wait:` reading the moment STATUS turns terminal —
but read the tree before you call it a result: a build `done` means
"built", not "landed". The `landed: <sha>` comment is the land receipt;
`review:` is the verdict; `handoff:` is the last word on a failure.
`get(kind='todo', id=<root>, view='tree')` shows the whole plan.

## Reading a failed slice

`search(kind='job', q='run_slice')` or the todo card lists the job;
`get(kind='job', id=<job>, view='log'|'job_summary')` is the record —
`meta.error`/`job_summary` carries the failure class (`cli-missing`,
budget, deny-path violation, exit code). The todo's `handoff:` comment
is the worker's last word, `attempt:<n>` the round. A red build goes
back `open` and re-dispatches on the next rung of the lane ladder;
past the cap it parks on a coordinator ask. To retry by hand: reopen
(`STATUS:open`) with a `handoff:` saying what to fix. A `work:`
mirror line on the linked gripe is the place to say you did.

## A gripe fix on this shape

The coordinator's recipe ([[precis-gripe-loop-help]]): mint the plan
with `gripe-fix`, link the gripe `fixes` to the build todo, and brief
the squash subject — the land's subject ends `(gr<N>, td<T>)`, which
moves both handles (`resolved:` on the gripe, `landed:` on the todo).
The build commits repro-first — the failing test before the fix — and
the brief names a prod-check line (`meta.fix.check`) when the fix is
observable through the MCP, so the deploy close is mechanical. A spec
section submits through `spec-build`: the same commit that implements
the section retires it — a `~<Heading>` section becomes a redirect to
`<module>::<symbol>` and its acceptance test, a whole-file spec is
rewritten to the same one-line redirect, never deleted — so spec and implementation cannot drift.

## What gets recorded about the run itself

Every `run_slice` job ends with a `meta.outcome` record — the cheap,
every-run layer: the mechanical gates (the changed `.py` files
compile, the tests the run owed ran — a code run that left commits
owes the `tests: ran` receipt whether or not its brief named test
paths, no exceptions, within the step cap), the outcome word
(`completed`, `completed_empty`, `halted_step_cap`,
`halted_wall_clock`, `error`, `abandoned`), the lane, model, cost and
wall seconds. The gates decide, not just record: a clean exit that
fails one mints no land AND closes no verdict/no-commit node —
validate runs included — the run is a failed attempt on the ladder.
The expensive layer is sampled: the `run_quality` worker pass (30
min, dark until `service_config` lights it) mints one big-model judge
plan per tick for runs the policy flagged — every run of a task
category until about fifty, then a hash-stable ten percent, plus
every run whose test session disagreed with its code session (an
opposite gate record, or a validate session that had to patch the
fix) and any gate pass suspiciously cheap or fast for its category.
The judge runs the router's FRONTIER model, pinned in the plan's
`params.model` and validated against the lane's `accepts_model` — a
pinned lane that cannot run it falls back to an MCP lane that can.
The judge's `verdict: pass|fail` lands on the judged job's
`meta.verdict`; how often the gates lied (gate pass, review fail)
rolls up per category as the suitability evidence. One routing rule
rides with it: a run's validate session must use a different model
than its code session — re-picked at submit (another model on the
same lane, then another lane), the launch refused when nothing
satisfies it (a same-model test is no test), parked while a prior
pick read cannot answer, and refused when a prior code session's
model was never recorded: a difference that cannot be proven is none.

## See also

- [[precis-job-help]] — job kinds, executors, job views and cancel
- [[precis-gripe-loop-help]] — the coordinator's loop on gripe fixes
- [[precis-todo-tree-help]] — the todo tree, leases, `waiting-for:*`
- [[precis-job-observe-help]] — reading job logs and job meta
- [[precis-get-help]] — `args=`, views, the `wait` argument
