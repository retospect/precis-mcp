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
  - how do I resume a wave after a crash with no files left?
applies-to: put/tag/link(kind='todo') under the 'Code work from gripes' root; put(kind='gripe') work: mirrors; edit(kind='gripe', mode='fix') state stays readable
tags: workflow
kinds: [gripe, todo]
status: active
scope: write
---

# precis-gripe-loop-help — the gripe-fix loop, end to end

A wave is a set of gripes fixed by builders (one per tree), coordinated
by one session. The gripe keeps what a todo must not have: open filing,
dedupe, flood control, triage verdicts, severity, the `resolved:` close
gate and the never-close mark. Everything about *doing* the work — the
brief, who builds it, where the builder stopped, the review verdict, the
landed sha — lives on one **code todo** per accepted gripe, minted under
the `Code work from gripes` root (`td559473`) and linked `fixes` to the
gripe. A second coordinator (or the same one after a crash) resumes from
the todo tree alone. This is phase 1 of the design:

```python
get(kind="md", id="docs/backlog/gripe-todo-unify.md")
```

Loop state the old wave kept on the gripe — `batch:` tags, the
`gripe_brief`/`gripe_handoff`/`gripe_review` chunks, `meta.fix.*` — is
still readable; see the legacy appendix at the end.

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

A red attempt is not a queue either: the todo goes back to `open` (the
claim drops, `attempt:<n>` bumps, the `handoff:` comment says why) and
one `work:` mirror line on the gripe says the attempt failed. Mirror
lines are the only loop prose the gripe takes — `work: parked behind
gr<M> (td<U>)`, `work: attempt N failed on <lane> — <first line>`,
`work: asks Reto: td<ask>` — and never a `work:` line that starts with
`resolved:`: the close gate owns that word, land-notify writes the real
one.

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
the plan shape; `templates.py` stamps it on generated nodes), so the
coordinator picks the vendor by hand; automatic routing on it is phase
2. The vocabulary: `code` (cheapest lane with fit), `design` (a spec
pass first), `review` (a different vendor than the builder), `verify`,
`docs`. Risk tags (`risk:spend`, `risk:migration`, `risk:prod-write`,
`risk:public-surface`, `risk:read-only-verb`) are informational too —
no code reads `risk:*` today and none blocks a claim — but they still
steer the brief: `risk:spend` carries the $25 cap and the paid-fetch
rule, `risk:prod-write` sends write-path testing to the dev DB,
`risk:read-only-verb` pins the verb read-only with a test in the slice.
In the plan shape `risk:migration` and `risk:public-surface` insert an
`answer` gate between build and land — that insertion is phase 2.

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

## Step 3 — assign the builder

When the builder session starts, record who and where on both refs:

```python
tag(kind="todo", id=T, add=["claimed-by:fleet-draft-reads"])   # CAS lease, 4 h
put(kind="todo", id=T, text="lane: devin:devin-draft-reads@<host>, tree draft-reads")
tag(kind="gripe", id=514335, add=["claimed-by:fleet-draft-reads"])  # → STATUS:fixing
put(kind="gripe", id=514335, text="in progress draft-reads — covers all asks; work item td<T>")
```

The `lane:` comment is one line — `<vendor>:<window>@<host>, tree
<tree>` — so the fleet row `vendor:host:project:tree` resolves from its
prefix. The claim is a lease, not a mark: `expires_at` is stamped 4 h
out and re-claiming refreshes it (`guards.CLAIM_TTL_HOURS`,
`guards.stamp_claim_lease`) — re-issue the same tag at each coordinator
poll while the build runs. A live lease held by someone else refuses
the claim and names the holder. Keep the gripe's lease alive too:
dropping it without a `resolved:` comment bounces the gripe back to
`triaged`. Under `waiting-for:fleet` the todo's own STATUS stays
`open`; the gripe's lease is what marks the work claimed.

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
put(kind="todo", id=T, text="review: LAND — selectors + tests ok; typecheck clean")
put(kind="todo", id=T, text="review: FIX — mypy red in tests/test_draft.py")
```

`review: LAND|FIX|REVERT — <findings>` is the line
`plan_dispatch.parse_verdict` accepts (`FIX` and `REVERT` need the
rest), so phase 2 reads phase 1 history. A fix round is the next
`brief:` comment on the same todo — no new todo, just the attempt bump
above.

## Step 6 — land: the subject moves both handles

Nothing is recorded by hand at land. The squash subject ends with the
handle list `(gr<N>, td<T>)`; `scripts/lib/land_notify.py` reads it and
posts `landed: <sha>` + `STATUS:done` on the todo and `resolved: <sha>
(main, release pending)` + `STATUS:auto_verify` (or `user_verify`) on
the gripe — that is why the builder's brief names the subject it must
end with. A commit that touches a gripe without finishing it names it
`refs gr<N>` inside the same tail: a `landed (partial)` comment, no
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

The grammar is the closed set in the legacy appendix below
(`read`/`count`). When a check line exists — on the todo or the legacy
gripe-side `meta.fix.check` — gripe-validate skips the gripe and the
`gripe_autoclose` tick owns the close: once the sha is an ancestor of
the recorded prod head and the head has stood 30 min, it runs the line
read-only; pass → `resolved:` + `STATUS:done`, a confirmed failure →
close + a `derived-from` triage refile. A fix that is NOT observable
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

## How do I resume a wave after a crash?

Nothing is in files — `get(kind='todo', id=559473, view='tree')` is the
state: `handoff:`-last items want a review spawned, `review: FIX` items
want a fix brief, `done` rows carrying `landed:` are awaiting deploy.
A `claimed-by` holder with no recent `lane:` or `handoff:` comment is a
candidate for a dead build — the stored lease expiry is not shown by
any view. From the gripe side, the `fixed-by` link on the card finds
its todo; from a tree name,
`search(kind='todo', q='<tree>')` finds the item (the `lane:` comment
carries it).

## Legacy loop state (read-only after phase 1)

A gripe the old wave ran still carries its
`edit(kind='gripe', mode='fix', args=…)` state — all readable, none
written for new work:

| fact | where | set by |
|---|---|---|
| batch | tag `batch:<slug>` (one at a time) | `args={'batch': 'tables-s2'}` (`''` removes) |
| brief(s) | `gripe_brief` chunks, newest in force | `args={'brief': '<text>'}` |
| handoff(s) | `gripe_handoff` chunks | `args={'handoff': '<text>'}` |
| review(s) | `gripe_review` chunks, first line `LAND — …` / `FIX — …` | `args={'review': 'land', 'findings': '…'}` |
| who | `meta.fix.host`, `meta.fix.session` | `args={'host': '<host>', 'session': 'devin:tables-s2'}` |
| tree | `meta.fix.tree` | `args={'tree': 'tables-s2'}` |
| landed sha | `meta.fix.landed` | `args={'landed': '98aa301d6'}` |
| prod check | `meta.fix.check` | `args={'check': 'count job with tag=STATUS:succeeded >= 1'}` |

New work puts the prod-check line on the code todo instead —
`meta.fix.check` there via `tag(kind='todo', id=T, meta={'fix': {'check':
'<line>'}})`; the landed sha comes from the todo's `landed:` comment.
A gripe-side line still evaluates, but the todo's wins when both exist.

`host` is the machine name the fleet kind reports; `session` is
`<vendor>:<window>` — the builder's tmux window
(`devin:devin-tables-s2`), or `claude:<session id>` for a session with
no window. `''` clears a meta field.

Queue derivation (how `/queue` reads it): no brief → `unbriefed`;
newest event a brief → `queued` (no session) or `in-build` (session
set); a handoff → `in-review`; a review `LAND` → `landing`, `FIX` →
`fix-round`; landed sha → `awaiting-deploy`; `STATUS:done`/`wontfix` →
none.

```python
get(kind="gripe", id="/queue")                                # counts per queue, then rows
get(kind="gripe", id="/queue", args={"queue": "in-build"})
get(kind="gripe", id="/batch")                                # census: one row per batch
get(kind="gripe", id=514335, view="fix")                      # newest brief, handoff, review texts
```

The `/queue` and `/batch` views go quiet for new work — they read the
gripe chunks — but still render pre-phase-1 gripes.

## Legacy — the prod check and the refusals

A `meta.fix.check` line is the auto-close prod check: one line, written
at land time, run read-only by the `gripe_autoclose` tick once the
landed sha is in prod. Grammar (closed set):
`read <kind> <id> contains "<substring>"` ·
`read <kind> <id> count "<substring>" <cmp> <n>` ·
`count <kind> [with tag=<tag>|meta.<path>=<value> [and …]] <cmp> <n>`
where `<cmp>` is `= != > < >= <=`. A `read` check matches the target's
raw title + chunk text, never the gripe's own id. The tick rejects
checks a typo would silently decide — an unknown `kind`, a comparison
true or false for every count, an unanchored `count` an empty result
satisfies. It runs only once the landed sha is an ancestor of the
CURRENT recorded prod head (a rolled-back sha still in the record's
`landed` reads not-deployed; on a clone-less worker the record's
membership is the fallback) and that head has stood 30 min. Pass →
`resolved:` + `STATUS:done`; a first failure stamps
`meta.fix.check_failed_at` (a pass between clears it), a repeat an hour
later closes the gripe FIRST and files a `derived-from` refile naming
the sha and the observed output. No check line → untouched;
`hold:no-autoclose` → untouched (both exclusions honour
`ref_tags.expires_at`; check queries run under a 5 s statement_timeout
and a cancel is skipped, never a verdict). The tick ships dark; to
light it, ask a human operator to arm the `gripe_autoclose` service
prio (recipe: `docs/runbooks/fix-gripe-ops.md`).

Refusals the legacy edit raises:

- `a closed gripe takes no fix-loop state` — reopen with
  `tag(add=['STATUS:open'])` first, or leave it; history stays readable.
- `review verdict 'maybe' is not one of ['land', 'fix']`.
- `landed '…' is not a 7-40 char lowercase hex sha` — the main sha.
- `batch, brief are gripe-fix loop keys — they need mode='fix'`.
- `args['findings'] rides with args['review']`.
- `edit(mode='fix') needs at least one key` — the key list is in `options`.
- `nothing to change (state already as asked)` is an ack, not a refusal.

## See also

- [[precis-gripe-help]] — filing, comments, STATUS lifecycle, closing
- [[precis-work-help]] — the todo tree, `meta.ask`, leases
- [[precis-fleet-help]] — live agent rows: host, tree, ctx%, attach line
- [[precis-fix-gripe-help]] — the `fix_gripe` job recipe (lane OFF)
