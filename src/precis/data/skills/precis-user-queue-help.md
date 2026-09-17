---
id: precis-user-queue-help
title: precis — a person's action queue (waiting-for:<login> todos)
summary: park a decision or chore on a named person's queue with a waiting-for:<login> todo — the factory never dispatches it, the same login string lists it
answers:
  - how do I record something a specific person must decide or do?
  - how do I list what a person still needs to do?
  - how do I hand a decision back to a human without the factory picking it up?
  - how do I close an item on a person's queue once they answered?
applies-to: put / search / tag (kind='todo'; tag waiting-for:<login>)
status: active
tags: [workflow]
kinds: [todo]
---

# precis-user-queue-help — a person's action queue

A **user queue item** is a plain `todo` tagged `waiting-for:<login>`,
where `<login>` is the person's login name — the same string that
`refs.owner_login` carries on the rows they own. One tag value does both
jobs: you *emit* it when you park something on the person, and you
*search* for it when you list what they owe. Use the login exactly as
stored (lower-case, no display name, no guessing — if you do not know a
person's login, ask them; a mistyped login makes a queue nobody reads).

The `waiting-for:*` prefix is an existing park: a tagged leaf drops out
of the `doable` view and out of the dispatch worker's candidates, so the
factory never runs it. Nothing new is needed on the server side; this
skill fixes the *value* convention so every session files and reads the
same list.

## Put something on a person's queue
## Hand a decision back to a human

One item per decision or chore. Verb-first text, then what it unblocks
(cite the ids, not prose descriptions), then the default if they do
nothing.

```python
put(
    kind="todo",
    text="Decide: lift halt:cost-cap on the 28 idle planner parents, or leave "
         "the factory idle. Unblocks the planner tick; default = stays idle.",
    tags=["waiting-for:<user>", "planner"],
)
```

Hang it under the thread it belongs to with `parent_id=` when there is
one, so the tree view shows the wait in context. When the item should
come back on a date regardless (a credential renewal, a recheck), add
`meta.auto_check` with `on_resolve: "open"` so it wakes itself — every
`waiting-for:*` tag is dropped on wake, so re-park it if the person is
still the owner:

```python
put(
    kind="todo",
    text="Renew the AnkiWeb credentials on the account page",
    tags=["waiting-for:<user>", "anki"],
    meta={"auto_check": {"type": "time_past",
                         "at": "2026-10-01T09:00:00+00:00",
                         "on_resolve": "open"}},
)
```

## List what a person needs to do

```python
search(kind="todo", tags=["waiting-for:<user>"])
search(kind="todo", tags=["waiting-for:<user>"], q="credentials")  # narrow
search(kind="todo", view="waiting")  # every external wait, all people
```

The tagged set is the whole queue; `q=` filters inside it. A session
that starts a triage pass lists this first, before mining memory or
chat for "things the owner still has to do".

## Close an item once the person answered

Record the answer in the item's details body (`text=` would replace
the title), then release it:

```python
put(kind="todo", id=<N>, body="Answer: lift the tags on td1, td2; leave the rest.")
tag(kind="todo", id=<N>, remove=["waiting-for:<user>"])   # real work → re-enters doable
tag(kind="todo", id=<N>, add=["STATUS:done"])              # pure decision → done
```

Removing the tag is the resume edge for an item that is work the
factory or a session now carries on; `STATUS:done` is for an item that
was only a decision.

## What does not belong on a queue

- Work the factory can do unattended — file it as an ordinary leaf.
- A worker blocked mid-task on a reply — that is `ask-user` (see
  `precis-todo-tree-help`); the queue is a standing personal list, not
  a yield.
- Bugs — those are `gripe` rows. A queue item may *point at* a gripe
  ("decide whether gr343941 fix (a) or (b)").

The nursery flags any `waiting-for:*` held longer than a week as a
long wait. On a personal queue that is the intended nudge, not a fault:
do not re-file the item, and do not clear the tag to silence it.
