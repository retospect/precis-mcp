---
id: precis-gripe-loop-help
family: work
title: precis — run the gripe-fix loop: work state on the gripe's code todo
summary: coordinator's loop — triage, mint one code todo per gripe, assign a builder, handoff, review verdict, land moves both handles, close on deploy; intake, triage and closure live on the gripe, work state lives on its fixed-by todo
answers:
  - how do I run a builder wave over a set of gripes from scratch?
  - where does the brief for a gripe live?
  - how do I record where a builder stopped (a handoff)?
  - how do I record a review verdict on a fix?
  - which work items are in build, in review, waiting to land, awaiting deploy?
  - who is working this gripe, on which host and session?
  - how do I hold a work item behind another fix or until a date?
  - how do I resume a wave after a crash?
  - where does a builder's task scratch live so other tasks can't recall it?
  - what happens to a builder's scratch when the todo closes?
applies-to: put/tag/link(kind='todo') under the 'Code work from gripes' root; put(kind='gripe') work: mirrors; edit(kind='gripe', mode='fix') state stays readable
tags: workflow
kinds: [gripe, todo]
governs: [factory]
status: active
scope: write
---

# precis-gripe-loop-help — the gripe-fix loop, end to end

**For one fix, prefer the factory** — `put(mode='plan', args={'template':
'gripe-fix', 'gripe_id': <N>})`; [[precis-factory-submit-help]] is the
recipe. The tmux wave below is the legacy manual form (Reto 2026-11-05):
the coordinator's state model still holds — the factory reads the same
tags, comments and links.

A wave is a set of gripes fixed by builders (one per tree), coordinated
by one session. The gripe keeps what a todo must not have: open filing,
dedupe, flood control, triage verdicts, severity, the `resolved:` close
gate and the never-close mark. Everything about *doing* the work — the
brief, who builds it, where the builder stopped, the review verdict, the
landed sha — lives on one **code todo** per accepted gripe under the
`Code work from gripes` root (`td559473`), linked `fixes` to the gripe.
A second coordinator (or the same one after a crash) resumes from the
todo tree alone — phase 1 of the gripe-todo-unify backlog spec.

Loop state the old wave kept on the gripe (`batch:` tags, the
`gripe_brief`/`gripe_handoff`/`gripe_review` chunks, `meta.fix.*`) is
still readable, nothing writes it for new work —
`src/precis/handlers/_gripe_fix.py` has the field map and the queue
derivation (`/queue`, `/batch` still render pre-phase-1 gripes).

## How does the loop run, step by step?

| step | call | what moves |
|---|---|---|
| 1 triage | existing `STATUS:triaged`, `fix:`/`size:`/`cluster:` tags | — |
| 2 mint + brief | `put(kind='todo', parent_id=559473, body=…, meta={'llm_tier': None, 'task_class': …}, link='gripe:N', rel='fixes')` | todo `open`, off `doable` |
| 3 assign a builder | `claimed-by:fleet-<tree>` + a `lane:` comment on the todo; `claimed-by:` + `in progress` on the gripe | gripe `fixing` |
| 4 handoff | `put(id=T, text='handoff: …')` | — |
| 5 review verdict | `put(id=T, text='review: LAND\|FIX\|REVERT — …')` | — |
| 5b fix round | `put(id=T, text='brief: round <n>: …')` | `attempt:<n>` bump |
| 6 landed on main | squash subject ends `(gr<N>, td<T>)`; land-notify moves both | todo `done` + `landed:`; gripe `auto_verify` + `resolved:` |
| 7 deployed | nothing by hand — gripe-validate closes a check-less gripe; a `meta.fix.check` line on the todo makes the `gripe_autoclose` pass run it in prod and own the close | gripe `done` |
| 8 close: harvest | `get(kind='todo', id=T, view='scratch')` → `put(id=T, text='harvest: none\|me<N> — …')` | — |

A red attempt is no queue either: the todo goes back to `open` (claim
drops, `attempt:<n>` bumps, the `handoff:` says why) plus one `work:`
line on the gripe. `work:` mirrors are the only loop prose the gripe
takes — `work: parked behind gr<M> (td<U>)`, `work: attempt N failed on
<lane> — <first line>`, `work: asks Reto: td<ask>` — and never
`work: resolved:…`: the close gate owns that word.

## Triage: is it a memory gripe?

The triage tick decides; the filer needs no tag or relation. A gripe
is a memory gripe when its first line starts with a handle (`me123: …`)
or it complains about a memory's content (stale, thin, ungrounded,
contradicted, wrong hub, too big). A gripe that only cites a memory as
context (`related-to`, `cites`, `derived-from`) stays ordinary. The
tick tags a memory gripe `project:memory`, adds `link='memory:<id>',
rel='raises-concern-about'` and keeps it out of the coder wave: the
gardener takes it, newest first ([[precis-fix-memory-gripe-help]]).

## Step 1 — which gripes go into a wave?

The triage tick has already tagged most open gripes (`fix:<type>`,
`size:S|M|L`, `cluster:<slug>`, a PRIO) and moved them to
`STATUS:triaged`. Pick a wave of gripes that touch the same files or
area — one builder tree can cover several:

```python
search(kind="gripe", tags=["STATUS:triaged", "cluster:draft-reads"], view="list", page_size=50)
search(kind="gripe", tags=["STATUS:open"], view="list", page_size=100)  # untriaged inbox
get(kind="gripe", id=[514335, 514404, 514405], view="full")  # read the candidates
```

A gripe that needs a design decision or a Reto ruling (`fix:spec`,
`fix:reto`) is not waved; comment the question on it instead.

## Step 2 — mint the work item: the brief is the todo's body

One code todo per accepted gripe, minted once under the root. Read the
gripe first — a `fixing` gripe with a live inbound `fixed-by` todo (the
card lists inbound links) already has its work item: post the brief on
that todo as a `brief:` comment, mint nothing.

```python
put(kind="todo", parent_id=559473,
    text="Fix gr514335: <gripe title, imperative>",
    body="<the brief: slices, files, verify-first, how to test, rulings>",
    prio=3,                            # the gripe's prio
    tags=["waiting-for:fleet", "thread:draft-reads", "risk:spend"],
    meta={"llm_tier": None, "task_class": "code", "retries": 2},
    link="gripe:514335", rel="fixes")
```

The inert shape is the no-auto-spend guarantee, written on every code
todo: `meta={'llm_tier': None}`, never `meta.executor`, never an
`executor:*` tag. The dispatch worker's candidate query only sees a
todo through one of those signals (`src/precis/todo/guards.py::
has_auto_run_signal`), so nothing mints a billed `plan_tick` under it;
`waiting-for:fleet` keeps it out of `view='doable'` too.

`meta.task_class` is informational in phase 1 — nothing reads it on a
plain code todo yet (`src/precis/plans/lanes.py::pick_lane` reads it in
the plan shape), so the coordinator picks the vendor by hand; automatic
routing is phase 2. The vocabulary: `code` (cheapest lane with fit),
`design` (a spec pass first), `review` (a different vendor), `verify`,
`docs`. Risk tags (`risk:spend`, `risk:migration`, `risk:prod-write`,
`risk:public-surface`, `risk:read-only-verb`) are informational too, but
steer the brief: `risk:spend` carries the $25 cap and the paid-fetch
rule, `risk:prod-write` sends write-path testing to the dev DB,
`risk:read-only-verb` pins the verb read-only with a test. In the plan
shape `risk:migration` and `risk:public-surface` insert an `answer` gate
between build and land.

A two-pass fix — design, then build — mints two children of T the same
way, the build `blocked-by` the design, and T carries no brief of its
own:

```python
link(kind="todo", id=<build>, target="todo:<design>", rel="blocked-by")
```

An item that must wait on another code todo is parked the same way —
`blocked-by` the blocker — plus one mirror line on the gripe:

```python
link(kind="todo", id=T, target="todo:<U>", rel="blocked-by")
put(kind="gripe", id=514335, text="work: parked behind gr<M> (td<U>)")
```

The brief itself is unchanged: numbered slices (one commit each), the
files and anchors you already know, what to verify before changing, how
to test, and each gripe's rulings folded in — a builder reads the body,
not the comment timeline. Three rules that cost builders real time:

- A length target and a per-item content rule can be unsatisfiable
  together ("a shorter summary" plus "one line per group with reason
  and fix"): give a character budget or say which wins, or the builder
  resolves it by measuring instead of building.
- When a brief constrains a surface, name the allowed functions —
  "`_TOP_KEYS` only" leaves open whether a related refusal may live in
  a neighbouring helper.
- State what a cap is measured on — a 120-char line cap on a board post
  did not say whether it counts the note text or the stored line with
  its timestamp and window prepended; the builder had to guess.

Every brief to an agent session also carries the standing context rule
(gr515153): bulky reads — queue files, triage outputs, review texts —
go to a subagent that returns a digest, not into the main loop.

## Migration DDL in a brief

A slice whose diff adds a file under `src/<pkg>/migrations/` gets the
squawk rules in its brief verbatim — a builder lane cannot read
`scripts/ship` to learn them (gr570072: a factory land went red
on `CREATE TABLE` without `IF NOT EXISTS` and a `smallint` column):

- `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE … ADD COLUMN IF NOT
  EXISTS`, `CREATE INDEX IF NOT EXISTS`; `bigint`/`integer`, never
  `smallint`; seeds `INSERT … ON CONFLICT DO NOTHING`.
- `src/precis/migrations/0276_learning_events.sql` is the exemplar —
  copy its shape.

The same brief carries the builder-run acceptance lines — the land's
checked selection runs them, so skipping lands red on tests the diff
never touched:

```
uvx -q --from 'squawk-cli>=2' squawk <new migration file>
scripts/test tests/test_schema_design.py tests/test_docs_docstring_lint.py
```

A new jsonb column joins `JSONB_COLUMNS` in test_schema_design.py with
its rationale; a package `__init__.py` docstring stays under the
1500-word ceiling. `run_slice.render_brief` appends the paragraph to a
session brief whose body names `src/<pkg>/migrations/`; the `gripe-fix`
brief-writer and `slice-tree` slicer prompts carry it too.

## Step 3 — assign the builder

When the builder session starts, record who and where on both refs:

```python
tag(kind="todo", id=T, add=["claimed-by:fleet-draft-reads"])   # CAS lease, 4 h
put(kind="todo", id=T, text="lane: devin:devin-draft-reads@<host>, tree draft-reads, model swe-2")
tag(kind="gripe", id=514335, add=["claimed-by:fleet-draft-reads"])  # → STATUS:fixing
put(kind="gripe", id=514335, text="in progress draft-reads — covers all asks; work item td<T>")
```

The `lane:` comment is one line — `<vendor>:<window>@<host>, tree
<tree>[, model <id>]` — so the fleet row `vendor:host:project:tree`
resolves from its prefix. A devin launch prints the line verbatim,
with the `, model <id>` tail when set (`--devin-model`, else
`--model`/`FLEET_MODEL`); event-watch reads the recorded id to keep
its quota reflex off non-SWE models (td573330).
The claim is a lease, not a mark: `expires_at` is stamped 4 h out and
re-claiming refreshes it (`guards.CLAIM_TTL_HOURS`,
`guards.stamp_claim_lease`) — re-issue at each coordinator poll while
the build runs. A live lease held by someone else refuses
the claim and names the holder. Keep the gripe's lease alive too:
dropping it without a `resolved:` bounces the gripe to `triaged`.
Under `waiting-for:fleet` the todo's own STATUS stays `open`; the
gripe's lease is what marks the work claimed.

## Step 4 — record the handoff

When a builder finishes, hits its context ceiling or dies, its state
goes on the todo — head sha, what is done, what is next, what is
blocked:

```python
put(kind="todo", id=T, text="handoff: Head 8aa66f699 on worktree-draft-reads. "
    "Done: selectors + tests. Next: the ord window. Blocked: none.")
```

A red round returns the item: drop the todo's lease, bump `attempt:`,
post the handoff, and mirror one line on the gripe:

```python
tag(kind="todo", id=T, add=["attempt:2"], remove=["attempt:1", "claimed-by:fleet-draft-reads"])
put(kind="gripe", id=514335, text="work: attempt 2 failed on devin — typecheck red in tests/test_draft.py")
```

The `attempt:N` tag shape is the one `run_slice.release_todo` writes,
so a later automated pass reads the same history — but `release_todo`
only knows the `claimed-by:lane-<lane>` prefix; a `claimed-by:fleet-…`
lease is dropped by hand in the same `tag` call, or kept when the same
tree re-takes the fix round.

## Step 5 — record the review verdict

The reviewer's verdict is one word plus findings, a comment on the
todo:

```python
put(kind="todo", id=T, text="review: LAND (read-only, tests not run) — selectors read against the brief, no defect found")
put(kind="todo", id=T, text="review: FIX — mypy red in tests/test_draft.py")
```

`review: LAND|FIX|REVERT — <findings>` is the line
`plan_dispatch.parse_verdict` accepts (`FIX` and `REVERT` need the
rest), so phase 2 reads phase 1 history — and a `handoff:` carrying
the token at a line start (markdown `- `/`* `/`**`/`#`/`> ` markers
allowed before it) or after sentence-final punctuation counts too
(never REVERT; a reasoned FIX beats a stray LAND, and a LAND glossed
`(clean` or `|` — or sharing the comment with a placeholder
`review: FIX|REVERT — <…>` template line — is the grammar echo, not a
verdict). A `(read-only, tests not run)` marker ending a line is
accepted — `parse_verdict` strips it before matching; it is never
required. When one comment carries verdict lines that disagree, the
verdict is malformed — post one line. A fix round is the next
`brief:` comment on the same todo — no new todo, just the attempt bump
above.

## Step 6 — land: the subject moves both handles

Nothing is recorded by hand at land. The squash subject ends
`(gr<N>, td<T>)`; `scripts/lib/land_notify.py` reads it and posts
`landed: <sha>` + `STATUS:done` on the todo and `resolved: <sha> (main,
release pending)` + `STATUS:auto_verify` (or `user_verify`) on the gripe.
`refs gr<N>` in the same tail writes a `landed (partial)` comment, no
status move. Never a second `resolved:` and never a hand `STATUS:done`
on a gripe land-notify already moved (gr542762).

## Step 7 — close on deploy

After the rollout the `gripe_validate` pass sees the `resolved:` sha in
prod and flips the gripe to `done`; `gripe_close_cascade` posts the
close note on the todo. A gripe the commit subject did not name is
yours to close the ordinary way — the `resolved:` comment and
`STATUS:done` in one call:

**A brief names a prod-check line whenever the fix is observable through
the MCP** (td570413, Reto 2026-11-05). The line lives on the code todo's
`meta.fix.check`, written — and grammar-checked at write time — with:

```python
tag(kind="todo", id=T, meta={"fix": {"check": 'read skill 123 contains "new flag"'}})
# or at mint: meta={..., "fix": {"check": "<line>"}}; {'check': None} clears
```

The write replaces the whole `fix` dict, so re-tagging the line clears
the tick's `check_failed_at` marker — a retagged check starts its
failure clock over.

The grammar is the closed set `parse_check` accepts
(`src/precis/workers/gripe_autoclose.py`): `read <kind> <id> contains
"<substring>"` · `read <kind> <id> count "<substring>" <cmp> <n>` ·
`count <kind> [with tag=<tag>|meta.<path>=<value> [and …]] <cmp> <n>`
(`<cmp>`: `= != > < >= <=`). A `read` check matches the target's raw
title + chunk text, and the tick itself ships dark — lighting it is an
operator act (`docs/runbooks/fix-gripe-ops.md`). When a check line
exists — on the todo or the legacy gripe-side `meta.fix.check` —
gripe-validate skips the gripe and the `gripe_autoclose` tick owns the
close: once the sha is an ancestor of the recorded prod head and the
head has stood 30 min, it runs the line read-only; pass →
`resolved:` + `STATUS:done`, a confirmed failure → close + a
`derived-from` triage refile. A fix that is NOT observable
through the MCP (CI workflow, harness, docs-only) gets no check line —
the brief says so — and closes on the sha-in-prod flip as today.

```python
put(kind="gripe", id=514335, text="resolved: 98aa301d6 (r110) — deployed, probe ok",
    tags=["STATUS:done"])
```

A never-close gripe (`hold:no-autoclose`) keeps its `done` todo and
gets a `shipped in r<N>` note instead of `STATUS:done`. A gripe a human
closes mid-work (dup, not-a-bug) also notes the todo via the cascade —
close the todo `STATUS:won't-do` and say why.

## Step 8 — harvest the scratch at close

A task's working notes live in `SPACE:scratch` owned by its todo (see
"Builder scratch" below). On close that residue is promoted into one
clean memory or left to dissolve on the owner-close expiry. The closer — the agent finishing
the task, never a worker (the worker cannot judge what is durable) —
makes that call in one read:

```python
get(kind="todo", id=T, view="scratch")   # every scratch-for:td<T> node, first line each
```

- Nothing durable → post `harvest: none` on the todo and close; the
  nodes dissolve on the owner-close expiry (gr605668).
- A durable residue → write ONE clean node where it belongs — a
  `SPACE:repo-dev` gotcha/ruling child of its hub, or the thread
  node's next `Left:` — link `supersedes` from the clean node to every
  scratch node it condenses, then post `harvest: me<N>` naming it:

```python
put(kind="memory", text="<the one clean claim, shape-card form>",
    tags=["SPACE:repo-dev", "section:gotchas"],
    link="memory:<hub>", rel="part-of")
link(kind="memory", id=<new>, target="memory:<scratch-id>", rel="supersedes")  # per condensed node
put(kind="todo", id=T, text="harvest: me<N> — <one line: what it condenses>")
```

The `supersedes` edge keeps the tangle readable after the scratch
dissolves. Do NOT hand-delete scratch: the owner-close expiry retires it.

Required on every gripe-fix todo, advisory elsewhere.

## Builder scratch — `scratch-for:td<T>`, not the shared bucket

A builder's working notes — probes, dead ends, draft text — go to
`SPACE:scratch` **owned by its todo** (gr605669). A bare `SPACE:scratch`
node sits in one shared bucket: every concurrent build recalls every
other build's probes. Own the scratch with the owner tag and read only
your own task's by default:

```python
# write — one call stamps SPACE:scratch + the owner tag
put(kind="memory", text="probe: <what you tried and saw>",
    args={"scratch_for": "td<T>"})

# read — this task's scratch only (the builder default)
search(kind="memory", tags=["SPACE:scratch", "scratch-for:td<T>"])
get(kind="memory", id="me<N>", extent="fisheye+1hop+recall",
    args={"ring": {"space": "scratch", "tags": ["scratch-for:td<T>"]}})
```

The owner tag is the key the task-close expiry (gr605668) reads — one
tag serves the recall partition and the close-out: while the owner is
open the only limit is the 30-day floor from the last touch; once it
closes, the node dissolves at the close + 3-day grace. Durable
findings still land where they always did: a `part-of` child on the
repo-dev hub, `work:`/`handoff:` lines on the refs — scratch is for
what nobody must remember to delete; the close step above decides what
survives the dissolve.

## Ask Reto mid-build, or hold the item

A question that blocks the item is an ask-user todo under T — never a
comment on the gripe:

```python
put(kind="todo", parent_id=T, text="<the question, one sentence>",
    tags=["ask-user", "thread:draft-reads"],
    meta={"ask": {"kind": "ruling", "options": ["yes", "no"],
                  "blocks": "td<T>", "context": "<one line>"}})
tag(kind="gripe", id=514335, add=["needs:ruling"])
put(kind="gripe", id=514335, text="work: asks Reto: td<ask>")
```

`meta.ask.blocks` naming the item files the ask as a showstopper and
writes the `blocked-by` link from T — [[precis-work-help]] has the full
`meta.ask` grammar (`design-verdict`, `ruling`, `approval`). The answer
(`answer:` comment + `STATUS:done` on the ask) re-arms the item; drop
`needs:ruling` from the gripe then.

Held work — an item that may not start before a date — is a gate child
todo, not a tag: mint a child under T carrying
`meta.gate={'kind': 'not-before', 'args': {'ts': 'YYYY-MM-DDTHH:MM:SSZ'}}`
(plus the `llm_tier: None` opt-out) and link the held item `blocked-by`
the gate:

```python
put(kind="todo", parent_id=T, text="gate: not before the r112 rollout",
    meta={"llm_tier": None,
          "gate": {"kind": "not-before", "args": {"ts": "2026-11-10T09:00:00Z"}}})
link(kind="todo", id=T, target="todo:<gate>", rel="blocked-by")
```

## Which items are in build, in review, landing, awaiting deploy?

```python
get(kind="todo", id=559473, view="tree")    # the whole wave, every state
search(kind="todo", view="attention")       # what needs the coordinator now
get(kind="todo", id=543210)                 # one item: lease, lane:, handoff:, review: lines
get(kind="gripe", id=514335)                # the gripe: fixed-by td<T>, needs:ruling, work: mirrors
```

The todo's own comments are its state: the newest `lane:` says who and
where, `handoff:` where it stopped, `review:` the verdict, `attempt:<n>`
the round. `open` under a live lease is in-build, a `review: FIX` after
it is a fix round, `done` with `landed:` is awaiting deploy.

Wave aggregates stay on the MCP, not raw SQL:
`view='count'`+`group_by`+`since=` is the snapshot, `view='flow'` the
burn-down (`precis-search-help`).

## How do I resume a wave after a crash?

Nothing is in files — `get(kind='todo', id=559473, view='tree')` is the
state: `handoff:`-last items want a review spawned, `review: FIX` items
want a fix brief, `done` rows carrying `landed:` are awaiting deploy.
A `claimed-by` holder with no recent `lane:` or `handoff:` is a
candidate for a dead build — the stored lease expiry is not shown by
any view. From the gripe side, the `fixed-by` link on the card finds
its todo; from a tree name,
`search(kind='todo', q='<tree>')` finds the item (the `lane:` carries
it).

## See also

- [[precis-factory-submit-help]] — submit a gripe fix to the dark factory
- [[precis-gripe-help]] — filing, comments, STATUS lifecycle, closing
- [[precis-work-help]] — the todo tree, `meta.ask`, leases
- [[precis-fleet-help]] — live agent rows: host, tree, ctx%, attach line
- [[precis-fix-gripe-help]] — the `fix_gripe` job recipe (lane OFF)
