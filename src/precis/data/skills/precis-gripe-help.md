---
id: precis-gripe-help
family: work
title: precis — the project's bug tracker
summary: bug tracking — file complaints, comment timeline, status workflow, resolution handoff
answers:
  - how do I file a bug without breaking my flow?
  - how do I check if someone already griped about this?
  - how do I find gripes that are open but untriaged?
  - how do I comment on or correct a gripe thread?
  - how do I mark a gripe ready for someone to fix?
  - how do I say a tree is working a gripe?
applies-to: get/search/put/delete/tag/link (kind='gripe')
tags: workflow, troubleshooting
kinds: gripe
status: active
---

# precis-gripe-help — file, find, and resolve bugs

Gripe is **the project's bug tracker**. File a complaint, find
existing ones, add context as comments, hand off to an agent to
prepare a candidate fix, close as `STATUS:done` when resolved
(`delete` only junk/test artifacts). Same surface for humans and LLMs.

The canonical address is the **handle** `gr<id>` (e.g. `gr42`) — copy
it from search/get output.

## File a bug I just noticed
## File friction without breaking flow
## Log something annoying I noticed in passing
## Drop a complaint about precis behaviour

```python
put(kind="gripe", text="paper slug NotFound does not surface near-match options")
# → created gripe id=42 (STATUS:open)
```

Half-sentence is fine — articulation can wait. Don't pre-classify,
don't articulate the fix. A title is optional: `title=` is accepted and
becomes the first line of the body (the title is just the first line). The system tags it
`STATUS:open` automatically.

A longer filing can split the way `todo` does: `text=` is the title /
first line and `body=` the details — they join as `text` + blank line +
`body`. `body=` alone is the whole prose, same as `text=` alone.

<!-- write -->
If you know which thread owns it, add `tags=['thread:<slug>']` with the
thread's slug. An unknown slug is refused and the refusal lists every
valid one. The thread's open gripes are listed from that tag, so tagging
is how a gripe gets onto its thread.
<!-- /write -->

Make the first line `Area: what's wrong`, under 60 chars. A longer first
line is shortened for the title (the ack shows it; the body keeps the
full text).
<!-- write -->
`edit(kind='gripe', id=N, args={'title': '…'})` changes it.
<!-- /write -->

The cost of a wrong gripe is one `delete` call. File freely. (Quests file
their own too — a tick's `needs` lands as a `quest:<id>` +
`needs-tool:<tool>`-tagged gripe linked `blocks` → the quest; a repeat
sighting becomes a comment, so comment on the existing one rather than
re-filing.)

<!-- write -->
Misled by a skill? Every skill ends with a ready-made line: add
`link='skill:<id>', rel='raises-concern-about'` to the gripe and say what
misled you, what is actually true, and where you checked. The skill then shows
an open-concern banner at the top until the gripe closes, and the fixer
is briefed with the skill file (`src/precis/data/skills/<id>.md`) and your notes.
<!-- /write -->

**Recurring themes merge automatically** — filing stays exactly as
above; the tool does the matching. The text is checked against a
registry of recurring-friction themes. Exactly one match with a live
`theme:<slug>`-tagged gripe (the *keeper*) folds your filing into it as
a `+1 (<first line>): <text>` comment; the ack says so.
<!-- write -->
It names `args={'new': True}` as the bypass if you meant a standalone
gripe. One match with no live keeper tags the new gripe `theme:<slug>`,
making it the keeper. Several matches file normally — the ack lists
them (add the one you meant as `theme:<slug>` yourself if you want it
on a keeper). Comments (`put(id=N, …)`) never match.
<!-- /write -->

```python
put(kind="gripe", text="scripts/test <file> -q waits ~25s on container bring-up")
<!-- write -->
# → merged into gr514478 (theme test-speed); to file separately anyway
#   pass args={'new': True}
<!-- /write -->
```

<!-- write -->
**Observations** — friction worth recording with nothing to fix (a
one-off, already resolved): `args={'observation': True}` files it
already closed — `STATUS:done` + an `observation` tag, with its own
`resolved: observation, auto-closed at filing` comment. Browse them
with `search(kind='gripe', tags=['observation'], status='*')`; reopen
with `tag(kind='gripe', id=N, add=['STATUS:open'], remove=['STATUS:done'])`.
<!-- /write -->

## Has anyone griped about this before?
## Find an existing complaint about X
## Search the bug tracker

```python
search(kind="gripe", q="near-match suggestions")
search(kind="gripe", q="HNSW duplicates")
```

Search before you file. Duplicates are OK but the existing thread
often has the context you'd otherwise rediscover. Body text and
every comment chunk are searchable — and `get(kind='gripe', id=N,
q='…')` finds the matching passages inside one gripe (find-in-ref,
`precis-get-help`).

**Queryless browse defaults to live statuses** (hiding done/wontfix), with
status per row and an exact hidden count. Query-based search retains its
`STATUS:open` default.
The response header names the default so it's never silent. Override it:

```python
search(kind="gripe", q="X", status="wontfix")  # one other cohort
search(kind="gripe", q="X", status="*")  # every status, no filter
```

`status=` takes ONE value — a gripe carries exactly one `STATUS:` tag, so
`status='open|triaged'` (or a comma list) is an error naming the valid
values, not a union filter. Run one search per value, or `status='*'`.
Valid: `open`, `triaged`, `ready_for_fix`, `in_review`, `verify`, `done`, `wontfix`.

**Enumerating vs. searching — a trap.** Adding `q=` to a status/tag
filter turns a *complete enumeration* into a *ranked filter*: a gripe
must also lexically match `q` to appear, so the count can fall far below
the tagged population. To **count / list** the open backlog, use the
**no-`q`** form; a `q=` there answers "open gripes that mention X", not
"how many are open". When `q=` drops matches from a tagged set, the
header flags it (`⚠ N of M … also match q`) so the truncation is visible.

## Find gripes that are open but untriaged
## Show me the inbox of new gripes

```python
search(kind="gripe", tags=["STATUS:open"])  # complete enumeration (no q)
get(kind="gripe", id="/open")  # same set, 20 per page
```

Queryless gripe browse sorts by PRIO, oldest first within a PRIO
(unset PRIO counts as 5), so the longest-waiting gripe is on page 1.
Under the header one line reads `N open · M older than 3d · oldest grX
(D days) · K more past this page (page=N)`. `sort='recency'` gives the
newest-touched order instead.

Need every id in a filtered set — a triage pass, a batch `get`?
`view='ids'` renders one handle per line and nothing else, so a full
page never mid-elides behind a `more()` cursor:

```python
search(kind="gripe", tags=["STATUS:open"], view="ids")  # gr… one per line
search(kind="gripe", q="chase", view="ids")  # ranked search too
```

`view='list'` is the same one-line-per-hit shape with the summary
first line beside the handle — `gr514477 enumerate open gripes` — for
triage reads that need the title, not just the id:

```python
search(kind="gripe", tags=["STATUS:open"], view="list", page_size=100)
```

`args={'id_min': N}` / `args={'id_max': M}` bound any search to a
ref-id range — a lone bound is legal — so "open gripes filed since
gr514000" is `args={'id_min': 514000}`. The range applies on the
listing views, `view='count'`, and ranked `q=` searches; a range
covering more than 20k live ids refuses on the ranked path (enumerate
it with `view='ids'` instead). It does not narrow a `link=` listing —
that combination is refused.

## Find gripes that are ready for a fix attempt

```python
search(kind="gripe", tags=["STATUS:ready_for_fix"])
```

## Find gripes with a fix in flight (claimed through landing and deploy)

```python
search(kind="gripe", tags=["STATUS:in_review"])
```

## Find gripes deployed and awaiting the probe verdict

```python
search(kind="gripe", tags=["STATUS:verify"])
```

## Find gripes marked won't-fix

```python
search(kind="gripe", tags=["STATUS:wontfix"])
```

## Show me a specific gripe
## Read a gripe with its comment thread
## What's the full thread on this bug?

```python
get(kind="gripe", id=42)
# → title card + body: header + tags + title + comment count + a one-line
#   gist of the newest comment, then the body (a long one is head+tail-
#   clipped; the cut marker names view='body')
get(kind="gripe", id=42, view="body")  # the body, uncapped
get(kind="gripe", id=42, view="comments")  # every comment, oldest first,
                                         # paged whole (never cut mid-text)
get(kind="gripe", id=42, view="full")  # body + comments on one bounded page
```

A bare `get` shows the body (clipped at ~1500 chars when long) plus a
one-line gist of the newest comment; the full
comment timeline stays behind `view='comments'` (gr514351, gr515189). The
selectors work inside `view='comments'`
or `view='full'`: `args={'comments': K}` is the newest K,
`args={'comments': 'A-B'}` is comments A through B (also `'A-'`, A on),
`args={'comment': K}` is one comment uncapped. Both views page *whole*
comments — a page that can't hold the rest names the
`args={'comments': 'A-B'}` call that reads the remainder in one fetch.
Plain `view='full'` is the bounded complete read: a long body clips
naming `view='body'`, and comments the page can't hold are elided by
ord with the windowed call named. `view='log'` remains the audit log.
Holding several ids, pass them at once: `get(kind='gripe', id=[42, 43, 44])`
returns one summary block per id (tags, prio, link count, first line; up to
50); add a single-id view to map it over the batch —
`get(kind='gripe', id=[42, 43], view='comments')` (or `'body'`, `'full'`,
`'links'`, `'log'`). Batch `view='body'` is one title+body section per
id — the comment timeline is not included. A batch whose joined view
would overflow the page returns the whole blocks that fit and names the
rest verbatim — `next: get(kind='gripe', id=[<unread ids>], view='full')`
— so re-issuing that call continues without a `more()` cursor.
`view='index'` is accepted and equals the default listing; use `view='count'` /
`'meta'` for aggregates.
<!-- write -->
`tag(kind='gripe', id=[...], add=[...])` applies one edit to all of them in
a single transaction.
<!-- /write -->

A gripe's text routinely quotes the rows it is about by their
universal handles — `pa509706` a paper, `pc5155615` one paper *chunk*,
`jo512227` a job, `mx1118` a measure, `tn511707` a taxon. The title
card lists them on a `refs in text:` line. Each resolves with a bare
`get(id='<handle>')`, no `kind=` needed: a chunk handle returns that
chunk's text; a record handle routes to its kind — `jo<N>` opens the
job card (status, `wall_s`, and the `job_summary` chunk holding the
run's counters, e.g. `proposals=`/`dropped=`). A `kind=` that
disagrees with the handle's code is refused with `get(id='<handle>')`
named as the fix. Chunk handles of the numeric kinds (`gc<N>`,
`jc<N>`, …) have no direct read — their owning ref's views list them.

## Add context to an existing gripe
## Comment on a gripe
## Reply to a gripe thread {write}

```python
put(kind="gripe", id=42, text="only triggers when the slug has a hyphen")
```

Putting to an *existing* gripe appends a `gripe_comment` chunk —
same verb as create, the `id` presence routes it to append.
There is no separate `comment=` field. To hide a stale comment from the
default read (it stays searchable, and `view='comments'` shows it marked
`[hidden]`): `delete(kind='gripe', id=42, args={'comment': 3})`; undo with
`args={'comment': 3, 'restore': True}`. This never deletes the gripe.

A comment can carry an edge and tag edits in the same call, in one
transaction (a bad target, `rel=` or tag writes nothing): closing a
duplicate is one call,
`put(kind='gripe', id=42, text='dup of gr7', link='gripe:7', rel='superseded-by', tags=['STATUS:wontfix'])`.
Tag edits on an append run the same `tag()` validation —
`STATUS:` is closed-prefix, so removing a `STATUS:` tag without a
replacement is refused. `prio=` still belongs on `tag()`.
With no `text=`, `put(kind='gripe', id=42, link='gripe:7')` (or `tags=`)
runs as the `link()`/`tag()` call alone, and the reply's first line says
so (`interpreted: … read as link(…)`). `view='full'` is the complete
read (body + every comment), and `PRIO:medium` is read as `PRIO:normal`,
also announced.
A numeric `PRIO:3` is refused (no single scale); use `prio=3` on `tag()`.

## Correct or refine an earlier comment
## Take back a comment I made {write}

Comments are append-only. Don't try to edit; append a follow-up:

```python
put(
    kind="gripe",
    id=42,
    text="retracting my earlier note — trigger is the hyphen, not the underscore",
)
```

## Triage a gripe — I've reviewed it and it's real {write}

```python
tag(kind="gripe", id=42, add=["STATUS:triaged"])
```

Use when the gripe is confirmed but not yet ready to act on
(needs more discussion, blocked on something, etc.). `STATUS:`
is closed-prefix and replaces atomically.

## Mark a gripe ready for someone to fix {write}

```python
tag(kind="gripe", id=42, add=["STATUS:ready_for_fix"])
```

The signal that flips it from "thinking about it" to "ready for the
owning thread's session to fix". Nothing auto-mints a `fix_gripe` job
from it (lane OFF since 2026-10-02).

## Say a tree is working the gripe {write}

When a session starts work on a gripe it comments the tree doing the
work, and the comment states the tree's coverage:

```python
put(kind="gripe", id=42, text="in progress fix-gripe-42 — covers all asks")
# or partial: "in progress small-feat — covers the enrich_ref ask only"
```

`covers all asks` when the tree takes the whole gripe; otherwise name
which asks it covers. A bare `in progress <tree>` makes the next reader
diff the branch to learn the coverage (gr514899).

## Who fixes a gripe?
## Can I hand a gripe off to an LLM to fix? {write}

The repo session that owns the gripe's work thread fixes it in
its own worktree;
`/pillar-review` relinks unthreaded gripes to a thread. The
auto-fix lane is OFF by ruling: the groomer mints no `fix_gripe`
todos, and a hand-submitted job skips at the push preflight unless
the worker node holds a credential. A hand-submit, for reference:

```python
put(kind="job", job_type="fix_gripe", link="gripe:42", rel="fixes")
# → created job id=101
# gripe auto-tagged STATUS:ready_for_fix as a side effect.
```

One call — no need to set `STATUS:ready_for_fix` first. If a
credential is present the worker clones the repo, runs claude on a `gripe_42` branch,
publishes the branch to the repo's upstream — confirmed there
with `git ls-remote` before it says so — and posts a comment on
the gripe when it's ready for review (or explains why it
couldn't).

See `precis-fix-gripe-help` for the full review / iterate loop.

## Show me which agent fixes are running for this gripe

```python
search(kind="job", link="gripe:42")
```

## Mark a gripe as won't-fix
## Decided not to fix this bug {write}

```python
put(
    kind="gripe",
    id=42,
    text="not fixing — root cause is upstream in marker; tracking there instead",
    tags=["STATUS:wontfix"],
)
```

One call — the comment and the status flip land in the same
transaction. `wontfix` is a kept final
state — it doesn't imply deletion. Use `delete` separately when
you want the gripe gone from search.

## Defer a gripe without fixing or closing it
## Park a gripe {write}

Leave it at `STATUS:open` (or `STATUS:triaged`) and append a
comment with the reason:

```python
put(kind="gripe", id=42, text="parking until after the F20 freeze")
```

## Close a gripe after the fix is merged
## Retire a gripe that's been resolved {write}

```python
put(
    kind="gripe",
    id=42,
    text="resolved: <sha> (r<N>) — <how verified>",
    tags=["STATUS:done"],
)
```

One call — comment and close land together (two calls,
`tag(kind='gripe', id=42, add=['STATUS:done'])` after the put, work
too). `STATUS:done` and `STATUS:wontfix` are refused unless the gripe's
newest comment starts `resolved:`. Won't-do is
`resolved: wontfix — <reason>` plus `STATUS:wontfix`. `delete` is only for
duplicates, spam and gripes filed in error; a soft-deleted gripe still
`get`s read-only (title, last STATUS, last comment).

Near-miss status spellings with one reading run as canonical and say so
(`'STATUS:closed'`/`'resolved'`/`'fixed'`/`'landed'` read as
`STATUS:done`); anything ambiguous still refuses with the valid list.

## Bundled into a planned-work spec

A gripe folded into a planned-work spec stays `STATUS:triaged`; record
the home in a comment ("home is <spec name>"). Separate homed from
not-yet-homed:
`search(kind='gripe', status='triaged', q='home is')`.

## Soft-delete a duplicate I filed by mistake {write}

```python
put(kind="gripe", id=42, text="duplicate of gripe:38")
delete(kind="gripe", id=42)
```

## Who filed this gripe?
## Was this filed by a human or an agent?

Not tracked on this surface. `put(kind='gripe', ...)` — and the
read-only carve-out it always routes through
(`file_gripe_readonly`, migration 0079) — insert the ref with no
`set_by`. `get(kind='gripe', id=N)`'s header
shows `filed by: ...` only on the rare pre-migration row that has
it; in practice, assume it's absent. If you need provenance, say
who you are in the body text or a follow-up comment.

(The `/gripes` **web form** is a separate surface with its own
attribution: it appends a `— filed by <login> via the /gripes web
form` line to the *body text* itself. That's a web-only convention,
invisible here — it doesn't populate the header either, and MCP
`put` has no equivalent.)

Two automated filers exist. The daily `call_errors` pass files gripes
for clusters of MCP calls the server rejected before real work, tagged
`origin:call-errors` + `callerr:<mcp|skill|hint|needs-triage>`; repeats
bump the open gripe with a count comment instead of re-filing. The
health digest router files `origin:health-digest-router` gripes for
watchdog alerts past their self-heal budget. Both file at prio 7 (a
critical alert at 4), below an unset PRIO, and close their own gripe
when the condition clears (call_errors: no matching error for three
daily passes) with a `resolved: auto-closed: …` comment and
`STATUS:done`.

A `gripe_triage` tick fires every 15 minutes, off on a host unless its
resolved `gripe_triage` service_config prio is > 0 (an exact-host row
beats `*`, so a `prio 0` host skips even under a fleet-wide light). It leases up to 40 oldest
`STATUS:open` gripes (`reopened` first) once the parents pass has run
(or the row is an hour old), makes one Sonnet-class batch call, and
writes `fix:<type>`, `cluster:<slug>`, `size:S|M|L`, a computed PRIO
and `STATUS:triaged`. It can also CLOSE: a `fixed` verdict backed by a
commit citing the gripe writes `STATUS:done` when the commit is
provably deployed (ancestor of `origin/prod`, or listed in the
deploy-time `deployed_history.txt` the redeploy lands in the worker
venv's `share/precis/` — trusted only while its `# sha` header
matches the installed commit;
cite fixes as `type(scope): subject (gr<id>)` or a `fixes gr<id>`
body line; a `Refs gr<id>` mention does not count, and a `docs(`
subject never counts) and `STATUS:verify` when it is
on main only. `noise`, `not-a-bug` and `working-as-designed` verdicts
write `STATUS:wontfix` with the reason. Holds: a closing verdict
needs a reason; a reopened gripe is never closed (no `fixed`, `noise`
or dispute close);
an owner-filed gripe's dispute stays `triaged` + `disagreement` for
the owner's ruling; dispute closes cap at 5 per tick, the overflow
held the same way. Duplicates are suggestion-only (`dup-suggest:`
comment + `meta.dup_suggested`), and `fix:reto` gripes land in a
review-queue digest.
It never links `supersedes` or edits the gripe body.

## Link a gripe to a related paper or chunk {write}

```python
link(kind="gripe", id=42, target="pa5", rel="related-to")
link(kind="gripe", id=42, target="gripe:38", rel="supersedes")
```

The supersession/related graph shows up in the `Links:` section of
`get(kind='gripe', id=N)`, so a reader sees the relationship without
prose spelunking — but only if you actually make the link. A prose
pointer ("supersedes gr38") in a comment is invisible to the graph;
planner and search can't traverse it.

## Split a big gripe into focused children
## Break one broad complaint into several actionable ones
## Supersede a vague gripe with sharper ones {write}

A sprawling gripe ("embedders keep dying") is hard to fix as one
unit. Split it into focused children and record the relationship as a
**link**, not prose — do it at creation so the step never gets skipped:

```python
# child carries the supersedes link from birth
put(
    kind="gripe",
    text="embedder health signals lie — nothing can auto-heal a wedged embedder",
    link="gripe:50906",
    rel="supersedes",
)
put(
    kind="gripe",
    text="decouple spark embedder from the compute GPU",
    link="gripe:50906",
    rel="supersedes",
)

# then park (or wontfix / delete) the broad parent
put(
    kind="gripe",
    id=50906,
    text="split into gr51394 (health signals) + gr51395 (GPU contention)",
)
tag(kind="gripe", id=50906, add=["STATUS:wontfix"])  # kept on record, superseded
```

Use `rel='supersedes'` when the child *replaces* the parent's scope,
`rel='related-to'` when it's merely adjacent. Retire the parent
(`delete`) only once every live thread it carried is linked out —
otherwise you lose the trail.

## Tag the repo a gripe is about
## Which project is this bug in? {write}

If you've got more than one project under the same precis
instance, tag the gripe with the repo name so `fix_gripe` knows
which tree to clone:

```python
put(
    kind="gripe",
    text="auth handler chokes on empty cookies",
    tags=["repo:my-other-project"],
)
# or after the fact:
tag(kind="gripe", id=42, add=["repo:my-other-project"])
```

Filter by repo when triaging:

```python
search(kind="gripe", tags=["repo:my-other-project", "STATUS:open"])
```

Gripes with no `repo:` tag fall back to the deployment's
`PRECIS_FIX_REPO_DIR` (the single-repo workflow). The set of
allowed repo names is deployment-side
(`PRECIS_FIX_REPOS` JSON map); submitting a fix_gripe job for an
unknown repo is rejected at the `put` call.

## Gripe vs todo vs memory

| Capture                                  | Use      |
|------------------------------------------|----------|
| "This annoyed me, don't know why yet"    | `gripe`  |
| "I will do this"                         | `todo`   |
| "Here's a thought I want to keep"        | `memory` |

Gripe is pre-articulation friction. If you already know the fix,
skip straight to `todo`. If you understand why something matters
and want it findable, use `memory`.

## A gripe is a bug in *precis*, not a defect in your content

A gripe reports something wrong with the **precis tool / MCP surface /
repo** — a verb that errors, a misleading message, a missing affordance,
a handler bug. It is fixed by the session owning the relevant thread, in *this codebase*.

A defect in **content you are authoring or auditing** — a draft chunk
with a missing `\citep{}`, an empty section stub, an unsupported claim, a
table with no backing data — is **not** a gripe. Filing it as one dumps
manuscript work into the code bug-tracker, where no one will
act on it. Route it to the content substrate instead:

| You found…                                              | File as |
|---------------------------------------------------------|---------|
| a precis verb/handler/message bug                       | `gripe` |
| a draft chunk that needs work (add cite, fill stub)     | `todo`, linked to the draft chunk |
| evidence that a claim *is* supported by a source chunk  | `finding` (`cited_in=` the source) |

Rule of thumb: if the body leads with a draft/paper chunk handle
(`dc…` / `pc…`) and describes a manuscript problem, it belongs on a
`todo` (or `finding`) anchored to that chunk — never a `gripe`. A
citation audit emits findings and todos, not gripes.

## Lifecycle reference

| Tag                    | Meaning                                 |
|------------------------|-----------------------------------------|
| `STATUS:open`          | Just filed, untriaged                   |
| `STATUS:triaged`       | Human reviewed; real; not yet ready     |
| `STATUS:ready_for_fix` | Ready for its thread's session to fix   |
| `STATUS:in_review`     | A fix is in flight (claimed through landing and deploy) |
| `STATUS:verify`        | Fix deployed; awaiting the probe verdict — non-terminal |
| `STATUS:done`          | Fixed/resolved — terminal; hidden from the live queue |
| `STATUS:wontfix`       | Decided not to act (kept on record)     |
| (deleted)              | `delete` — duplicates, spam and gripes filed in error only. A *fixed* gripe is a `resolved: <sha> (r<N>) — <how verified>` comment + `STATUS:done` |

## See also

- [[precis-fix-gripe-help]] — the fix_gripe job recipe (lane OFF)
- [[precis-job-help]] — monitor/cancel fix attempts
- [[precis-search-help]] — search across kinds
- [[precis-todo-help]] — promote a gripe to a todo
