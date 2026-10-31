---
id: precis-grill-me-help
family: work
title: precis — grill-me: targeted questions before a flagged build
summary: opt-in pre-build interrogation — check the corpus before asking, ask only the gaps most likely to bite (security, deploy target, back-compat, scope boundary), cite what the corpus already answers, file the rest on the item
answers:
  - how do I run a grill-me pass on a flagged task before building?
  - what do I look up in the corpus before asking clarifying questions?
  - which questions should a pre-build question round ask?
  - what do I do with the questions when nobody is there to answer?
  - how do I flag a task for a grill-me pass?
applies-to: search(kind='memory'|'conv'|'paper'); put(kind='gripe'|'todo', id=N, text='grill: …')
tags: [workflow]
kinds: [memory, conv, paper, gripe, todo]
status: active
---

# precis-grill-me-help — the grill-me pass, before the build starts

A grill-me pass is a short round of targeted clarifying questions run
before a **flagged** task touches any files. It is opt-in per task —
an unflagged task runs exactly as today; nothing here is a gate.

The pass has two halves and the order matters: check the corpus first,
then ask only what the corpus could not answer. Unstated assumptions —
security model, deploy target, back-compat, where the task stops — are
what silently get filled in wrong. A question the corpus already answers
is never asked; the hit is cited instead.

## When does a task get a grill-me pass?
## Flag a task for a pre-build question round

A task is flagged when its ask, brief or item says so — "grill me" in
the request, or a `grill` line in the brief the builder was handed. Any
task-sized item qualifies: a todo, a gripe, a dispatched slice. Whoever
picks the flagged task up — coder, writer or science work alike — runs
the pass before edits begin.

Flag one on the item itself so the next agent sees it:

```python
put(kind="todo", id=540848, text="grill: run a question round before building")
put(kind="gripe", id=541348, text="grill: run a question round before building")
```

## Check the corpus before asking anything
## What do I look up before asking a question?

Each candidate question goes through the corpus first — the point is
never asking what a lookup would answer. Search the task's keywords
(its title terms, the subsystem it touches, the decision it implies)
across the three stores that hold prior decisions:

```python
search(kind="memory", q="md-index refresh", view="index")
search(kind="conv", q="md-index refresh")
search(kind="paper", q="incremental index invalidation")
```

- `memory` holds recorded decisions and gotchas — the first stop.
- `conv` holds the discussions that produced them.
- `paper` covers what a literature answer would already close.

Read the item itself the same way: a gripe or todo's body and comment
timeline routinely carry the ruling that answers the question.

A hit that *resolves* the question suppresses it: the pass reports
"decided: <answer> — <handle>" and moves on. The bar is a recorded
decision, constraint or prior attempt on the same point — a merely
related hit stays as context under the question, it is not an answer.

## Ask the questions that bite
## Which questions does a grill-me round ask?

A short round — typically three to six questions, never a survey —
aimed at the gaps most likely to bite on this task:

- **Security model** — who can reach this, what untrusted input it
  touches, what it may call.
- **Deploy target** — where it runs, and what else shares that target.
- **Back-compat** — which existing callers, data or behaviour must keep
  working.
- **Scope boundary** — what is explicitly out; where the task stops.

Ask what is genuinely uncertain *for this task*, not the checklist
verbatim — a question the corpus answered, or one the task text already
states, is filler and is not asked. Tag each question with the one
clause that says why it is being asked, so the answer lands on the gap.

## What if nobody is there to answer?
## Proceed on stated defaults when the run is unattended

A human in the loop gets the questions and the build waits for the
answers. Unattended — a fleet builder, an autonomous fixer — the pass
does not block: it files the open questions **and the default each one
is proceeding on** as a `grill:` comment on the item, then builds:

```python
put(
    kind="todo",
    id=540848,
    text="grill: unanswered — deploy target assumed main-only; "
    "back-compat assumed: existing callers unchanged. "
    "Answered by corpus: trust boundary per co73 (safe_fetch).",
)
```

The comment carries every asked question verbatim with its assumed
default, plus each corpus answer cited by handle — a wrong default then
surfaces on the item instead of hiding inside the diff.

## See also

- [[precis-work-help]] — the work tree; an ask-user todo for the owner
- [[precis-gripe-loop-help]] — briefs, handoffs and verdicts on a gripe
- [[precis-memory-help]] — the memory store the corpus pass searches
- [[precis-conv-help]] — searching past conversations
- [[precis-search-help]] — search modes, views and tag filters
