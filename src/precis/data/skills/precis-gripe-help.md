---
id: precis-gripe-help
title: precis — the project's bug tracker
summary: bug tracking — file complaints, comment timeline, status workflow, resolution handoff
applies-to: get/search/put/delete/tag/link (kind='gripe')
status: active
---

# precis-gripe-help — file, find, and resolve bugs

Gripe is **the project's bug tracker**. File a complaint, find
existing ones, add context as comments, hand off to an agent to
prepare a candidate fix, retire when resolved. Same surface for
humans and LLMs.

The canonical address is the **handle** `gr<id>` (e.g. `gr42`) — copy
it from search/get output. The legacy forms `id=42` and `id='gripe:42'`
still resolve on input.

## File a bug I just noticed
## File friction without breaking flow
## Log something annoying I noticed in passing
## Drop a complaint about precis behaviour

```python
put(kind='gripe', text='paper slug NotFound does not surface near-match options')
# → created gripe id=42 (STATUS:open)
```

Half-sentence is fine — articulation can wait. Don't pre-classify,
don't write a title, don't articulate the fix. The system tags it
`STATUS:open` automatically.

The cost of a wrong gripe is one `delete` call. File freely.

## Has anyone griped about this before?
## Find an existing complaint about X
## Search the bug tracker

```python
search(kind='gripe', q='near-match suggestions')
search(kind='gripe', q='HNSW duplicates')
```

Search before you file. Duplicates are OK but the existing thread
often has the context you'd otherwise rediscover. Body text and
every comment chunk are searchable.

## Find gripes that are open but untriaged
## Show me the inbox of new gripes

```python
search(kind='gripe', tags=['STATUS:open'])
```

## Find gripes that are ready for a fix attempt

```python
search(kind='gripe', tags=['STATUS:ready_for_fix'])
```

## Find gripes that have a candidate fix in review

```python
search(kind='gripe', tags=['STATUS:in_review'])
```

## Find gripes marked won't-fix

```python
search(kind='gripe', tags=['STATUS:wontfix'])
```

## Show me a specific gripe
## Read a gripe with its comment thread
## What's the full thread on this bug?

```python
get(kind='gripe', id=42)
# → header + body + comment timeline (human + worker) + STATUS
#   tags + linked jobs
```

`get` composes the body chunk and every `gripe_comment` chunk in
creation order, so the whole conversation is one read.

## Add context to an existing gripe
## Comment on a gripe
## Reply to a gripe thread

```python
put(kind='gripe', id=42, text='only triggers when the slug has a hyphen')
```

Putting to an *existing* gripe appends a `gripe_comment` chunk —
same verb as create, the `id` presence routes it to append.
There is no separate `comment=` field.

## Correct or refine an earlier comment
## Take back a comment I made

Comments are append-only. Don't try to edit; append a follow-up:

```python
put(kind='gripe', id=42, text='retracting my earlier note — trigger is the hyphen, not the underscore')
```

## Triage a gripe — I've reviewed it and it's real

```python
tag(kind='gripe', id=42, add=['STATUS:triaged'])
```

Use when the gripe is confirmed but not yet ready to act on
(needs more discussion, blocked on something, etc.). `STATUS:`
is closed-prefix and replaces atomically.

## Mark a gripe ready for someone to fix

```python
tag(kind='gripe', id=42, add=['STATUS:ready_for_fix'])
```

The signal that flips it from "thinking about it" to "available
for a fix_gripe job". You can submit a job directly instead —
it auto-tags ready_for_fix; see below.

## Ask an agent to prepare a fix
## Hand a gripe off to an LLM to fix
## Auto-fix this bug

```python
put(kind='job', job_type='fix_gripe', link='gripe:42', rel='fixes')
# → created job id=101
# gripe auto-tagged STATUS:ready_for_fix as a side effect.
```

One call — no need to set `STATUS:ready_for_fix` first. The
worker clones the repo, runs claude on a `gripe_42` branch,
pushes the branch to origin, and posts a comment on the gripe
when it's ready for review (or explains why it couldn't).

See `precis-fix-gripe-help` for the full review / iterate loop.

## Show me which agent fixes are running for this gripe

```python
search(kind='job', link='gripe:42')
```

## Mark a gripe as won't-fix
## Decided not to fix this bug

```python
tag(kind='gripe', id=42, add=['STATUS:wontfix'])
put(kind='gripe', id=42, text='not fixing — root cause is upstream in marker; tracking there instead')
```

Leave a final comment with the reason. `wontfix` is a kept final
state — it doesn't imply deletion. Use `delete` separately when
you want the gripe gone from search.

## Defer a gripe without fixing or closing it
## Park a gripe

Leave it at `STATUS:open` (or `STATUS:triaged`) and append a
comment with the reason:

```python
put(kind='gripe', id=42, text='parking until after the F20 freeze')
```

## Retire a gripe that's been resolved
## Delete a gripe after the fix is merged

```python
delete(kind='gripe', id=42)
# soft-delete: history preserved, excluded from default search
```

Use after a fix is merged, or when the gripe is no longer
relevant. Leaving a final comment first is good manners.

## Soft-delete a duplicate I filed by mistake

```python
put(kind='gripe', id=42, text='duplicate of gripe:38')
delete(kind='gripe', id=42)
```

## Who filed this gripe?
## Was this filed by a human or an agent?

`get(kind='gripe', id=N)` surfaces the filer identity in the
header — humans show up by their CLI identity, agents by their
session label. No separate verb needed.

## Link a gripe to a related paper or chunk

```python
link(kind='gripe', id=42, target='paper:abazari2024design', rel='related-to')
link(kind='gripe', id=42, target='gripe:38', rel='supersedes')
```

## Tag the repo a gripe is about
## Which project is this bug in?

If you've got more than one project under the same precis
instance, tag the gripe with the repo name so `fix_gripe` knows
which tree to clone:

```python
put(kind='gripe', text='auth handler chokes on empty cookies',
    tags=['repo:my-other-project'])
# or after the fact:
tag(kind='gripe', id=42, add=['repo:my-other-project'])
```

Filter by repo when triaging:

```python
search(kind='gripe', tags=['repo:my-other-project', 'STATUS:open'])
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
a handler bug. It routes to a `fix_gripe` job that edits *this codebase*.

A defect in **content you are authoring or auditing** — a draft chunk
with a missing `\citep{}`, an empty section stub, an unsupported claim, a
table with no backing data — is **not** a gripe. Filing it as one dumps
manuscript work into the code bug-tracker, where no `fix_gripe` job can
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
| `STATUS:ready_for_fix` | Ready for a `fix_gripe` job to claim    |
| `STATUS:in_review`     | A fix landed on a branch; awaits merge  |
| `STATUS:wontfix`       | Decided not to act (kept on record)     |
| (deleted)              | Retired via `delete`; history preserved |

## See also

```python
get(kind='skill', id='precis-fix-gripe-help')   # the agent-fix recipe
get(kind='skill', id='precis-job-help')         # monitor/cancel fix attempts
get(kind='skill', id='precis-search-help')      # search across kinds
get(kind='skill', id='precis-todo-help')        # promote a gripe to a todo
```
