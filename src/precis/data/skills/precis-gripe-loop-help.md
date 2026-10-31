---
id: precis-gripe-loop-help
family: work
title: precis — run the gripe-fix loop on the gripe itself
summary: coordinator's loop — triage, batch into a brief, assign a builder, handoff, review verdict, land queue, close on deploy; every state lives on the gripe and every queue is a get/search filter
answers:
  - how do I run a builder wave over a set of gripes from scratch?
  - where does the brief for a batch of gripes live?
  - how do I record where a builder stopped (a handoff)?
  - how do I record a review verdict on a gripe?
  - which gripes are in build, in review, waiting to land, awaiting deploy?
  - who is working this gripe, on which host and session?
  - how do I resume a wave after a crash with no files left?
applies-to: edit(kind='gripe', mode='fix'); get(kind='gripe', id='/queue' | '/batch', view='fix')
tags: workflow
kinds: gripe
status: active
scope: write
---

# precis-gripe-loop-help — the gripe-fix loop, end to end

A wave is a set of gripes fixed by builders (one per tree), coordinated by
one session. Every fact the coordinator used to keep in files — the brief,
who builds it, where the builder stopped, the review verdict, the landed
sha — lives **on the gripe**, written through one edit mode and read back
as queues. A second coordinator (or the same one after a crash) resumes
from `get(kind='gripe', id='/queue')` alone.

## How does the loop run, step by step?

| step | call | the gripe's queue after it |
|---|---|---|
| 1 triage | existing `STATUS:triaged`, `fix:`/`size:`/`cluster:` tags | — |
| 2 batch + brief | `edit(id=[...], mode='fix', args={'batch', 'brief'})` | `queued` |
| 3 assign a builder | `edit(..., args={'host', 'session', 'tree'})` | `in-build` |
| 4 handoff | `edit(..., args={'handoff'})` | `in-review` |
| 5 review verdict | `edit(..., args={'review': 'land' \| 'fix', 'findings'})` | `landing` / `fix-round` |
| 5b fix round | `edit(..., args={'brief'})` again | `in-build` |
| 6 landed on main | `edit(..., args={'landed': '<sha>'})` | `awaiting-deploy` |
| 7 deployed | `put(id=N, text='resolved: …', tags=['STATUS:done'])` | out of every queue |

`id=[...]` applies one edit to a whole batch in one transaction. The
queue is never set by hand: it is derived from what the gripe carries.

## What does a gripe in the loop carry?

Stored — the smallest set:

| fact | where | set by |
|---|---|---|
| batch | tag `batch:<slug>` (one at a time) | `args={'batch': 'tables-s2'}` (`''` removes) |
| brief(s) | `gripe_brief` chunks, newest in force | `args={'brief': '<text>'}` |
| handoff(s) | `gripe_handoff` chunks | `args={'handoff': '<text>'}` |
| review(s) | `gripe_review` chunks, first line `LAND — …` / `FIX — …` | `args={'review': 'land', 'findings': '…'}` |
| who | `meta.fix.host`, `meta.fix.session` | `args={'host': 'mac-a', 'session': 'devin:tables-s2'}` |
| tree | `meta.fix.tree` | `args={'tree': 'tables-s2'}` |
| landed sha | `meta.fix.landed` | `args={'landed': '98aa301d6'}` |

`host` is the machine name the fleet kind reports; `session` is
`<vendor>:<window>` — the builder's tmux window name
(`claude:claude-tables-s2`, `devin:devin-tables-s2`,
`codex:codex-castor-s2`; the launcher records it), or
`claude:<session id>` for a session with no window — so the fleet row
`vendor:host:project:tree` resolves from its prefix. `''` clears any of
the four meta fields.

Derived, never stored: the queue (below), the census per batch, counts,
ages, the round number (briefs so far), the builder vendor.

Queue derivation: no brief → `unbriefed`; newest event a brief → `queued`
(no session) or `in-build` (session set); a handoff → `in-review`; a review
`LAND` → `landing`, `FIX` → `fix-round`; landed sha → `awaiting-deploy`;
`STATUS:done`/`wontfix` → none. The STATUS axis moves with the loop and
the ack says so: a brief or batch takes `open`/`triaged` to
`ready_for_fix`; a session, handoff, review or landed sha takes anything
below `in_review` to `in_review`. The verify states (`auto_verify`,
`user_verify`, legacy `verify`) and the terminal values are
never touched; a closed gripe refuses loop edits (reopen first).

## Step 1 — which gripes go in a batch?

The triage tick has already tagged most open gripes (`fix:<type>`,
`size:S|M|L`, `cluster:<slug>`, a PRIO) and moved them to
`STATUS:triaged`. Pick a batch of 2–6 that touch the same files or area:

```python
search(kind="gripe", tags=["STATUS:triaged", "cluster:draft-reads"], view="list", page_size=50)
search(kind="gripe", tags=["STATUS:open"], view="list", page_size=100)  # untriaged inbox
get(kind="gripe", id=[514335, 514404, 514405], view="full")  # read the candidates
```

A gripe that needs a design decision or a Reto ruling (`fix:spec`,
`fix:reto`) is not batched; comment the question on it instead.

## Step 2 — batch the gripes and write the brief

One edit puts every gripe of the batch under one tag and gives each the
same brief text:

```python
edit(
    kind="gripe",
    id=[514335, 514404, 514405],
    mode="fix",
    args={
        "batch": "draft-reads",
        "brief": "Task: draft read selectors, one commit per gripe. "
        "Files: handlers/draft.py and its tests. Verify first: ... "
        "Rulings: gr514404 comment 3 narrows scope to view='full'.",
    },
)
# → gr514335: batch=draft-reads · brief #1 appended (round 1) ·
#   STATUS:triaged → STATUS:ready_for_fix · queue none → queued  (one line per id)
```

The brief is the builder's whole task: numbered slices (one commit each),
the files and anchors you already know, what to verify before changing,
how to test, and each gripe's rulings folded in — a builder reads the
brief, not the comment timeline. Three rules that cost builders real
time:

- A length target and a per-item content rule can be unsatisfiable
  together ("a shorter summary" plus "one line per group with reason and
  fix"): give a character budget or say which wins, or the builder
  resolves it by measuring instead of building.
- When a brief constrains a surface, name the allowed functions —
  "`_TOP_KEYS` only" leaves open whether a related refusal may live in a
  neighbouring helper.
- State what a cap is measured on — a 120-char line cap on a board post
  did not say whether it counts the note text or the stored line with its
  timestamp and window prepended; the builder had to guess.

## Step 3 — assign the builder

When the builder session starts, record who and where:

```python
edit(
    kind="gripe",
    id=[514335, 514404, 514405],
    mode="fix",
    args={"host": "mac-a", "session": "devin:devin-draft-reads", "tree": "draft-reads"},
)
# → … session=devin:devin-draft-reads · host=mac-a · tree=draft-reads ·
#   STATUS:ready_for_fix → STATUS:in_review · queue queued → in-build
```

A builder without MCP access gets the batch dossier as a file — the brief
in force once, then every member's title, body and comments:

```python
get(kind="gripe", id="/batch", args={"batch": "draft-reads"})
```

A builder with MCP access reads the same thing, and comments
`in progress <tree> — covers all asks` on each gripe it picks up (or
names which asks the tree covers).

## Step 4 — record the handoff

When a builder finishes, hits its context ceiling or dies, its state goes
on the gripes — head sha, what is done, what is next, what is blocked:

```python
edit(
    kind="gripe",
    id=[514335, 514404],
    mode="fix",
    args={"handoff": "Head 8aa66f699 on worktree-draft-reads. Done: 514335 (sel. "
    "refusal), 514404. Next: 514405 needs the ord window. Blocked: none."},
)
# → … handoff #3 appended · queue in-build → in-review
```

A gripe the tree did not reach gets no handoff; it stays `in-build` until
you re-brief or re-assign it.

## Step 5 — record the review verdict

The reviewer's verdict is one word plus findings; `fix` sends the batch
back for a round, `land` puts it in the land queue:

```python
edit(kind="gripe", id=[514335, 514404], mode="fix",
     args={"review": "land", "findings": "selectors + tests ok; typecheck clean"})
# → … review LAND #4 appended · queue in-review → landing
edit(kind="gripe", id=[514405], mode="fix",
     args={"review": "fix", "findings": "mypy red in tests/test_draft.py:244"})
# → … review FIX #4 appended · queue in-review → fix-round
```

A fix round is the next brief: `args={'brief': 'round 2: …'}` moves the
gripe back to `in-build` (the session is still set) and bumps the round
count the card shows.

## Step 6 — the land queue

```python
get(kind="gripe", id="/queue", args={"queue": "landing"})
```

lists what is ready for the release owner, with tree and who. Once the
squash lands on main, record the main sha (not the branch head):

```python
edit(kind="gripe", id=[514335, 514404], mode="fix", args={"landed": "98aa301d6"})
# → … landed=98aa301d6 · queue landing → awaiting-deploy
```

## Step 7 — close on deploy

```python
get(kind="gripe", id="/queue", args={"queue": "awaiting-deploy"})
```

is the close list. When the release carrying the sha is live, close each
gripe the ordinary way — the `resolved:` comment and `STATUS:done` in one
call; the gripe leaves every queue by derivation (its `batch:` tag stays
as history):

```python
put(kind="gripe", id=514335, text="resolved: 98aa301d6 (r110) — deployed, probe ok",
    tags=["STATUS:done"])
```

The triage tick closes a gripe on its own only when a commit *citing* the
gripe is provably deployed; a batch gripe the commit does not name is
yours to close from this list.

## Which gripes are in build, in review, landing, awaiting deploy?
## Who is working this gripe, on which host?

```python
get(kind="gripe", id="/queue")                                   # counts per queue, then rows
get(kind="gripe", id="/queue", args={"queue": "in-build"})
get(kind="gripe", id="/queue", args={"host": "mac-a"})          # one machine's load
get(kind="gripe", id="/queue", args={"batch": "draft-reads"})
get(kind="gripe", id="/batch")                                   # census: one row per batch
get(kind="gripe", id=514335)                                     # card: fix: batch · queue · session@host · tree · brief #k · review LAND #k · landed
get(kind="gripe", id=514335, view="fix")                         # the texts: newest brief, handoff, review
search(kind="gripe", tags=["batch:draft-reads"], status="*")   # membership incl. closed
```

Queue rows read `gr<id> · batch · session@host · tree · landed · age ·
title`. The `/queue` and `/batch` views render the whole loop on one page
and take no `page=`.

## How do I resume a wave after a crash?

Nothing is in files. `get(kind='gripe', id='/queue')` is the state:
`in-build` rows name the builder sessions to check, `in-review` rows the
reviews to spawn, `fix-round` rows the fix briefs still to write,
`landing` rows the shas to hand to the release owner, `awaiting-deploy`
the close list. `view='fix'` on any row gives the last brief, handoff and
verdict in full.

## Refusals you will meet

- `a closed gripe takes no fix-loop state` — reopen with
  `tag(add=['STATUS:open'])` first, or leave it; history stays readable.
- `review verdict 'maybe' is not one of ['land', 'fix']`.
- `landed '…' is not a 7-40 char lowercase hex sha` — the main sha.
- `batch, brief are gripe-fix loop keys — they need mode='fix'`.
- `args['findings'] rides with args['review']`.
- `edit(mode='fix') needs at least one key` — the key list is in `options`.
- `nothing to change (state already as asked)` is an ack, not a refusal:
  re-sending the same patch is safe.

## See also

- [[precis-gripe-help]] — filing, comments, STATUS lifecycle, closing
- [[precis-fleet-help]] — live agent rows: host, tree, ctx%, attach line
- [[precis-fix-gripe-help]] — the `fix_gripe` job recipe (lane OFF)
