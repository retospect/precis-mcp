---
id: resume-note
family: memory
title: precis — write a resume-note memory a new session can resume from
summary: before compaction or a restart, write or update one memory tagged resume-note that links the notes, python-kind file refs, required repos, in-flight work and transient instructions, so a new session resumes from its handle alone
answers:
  - ready to compact — how do I leave a durable transfer note for the next session?
  - how do I write a resume-note memory?
  - how do I find all resume-notes?
  - what must a resume-note carry so a new session can continue?
  - what do I do with session state that lives only in the conversation?
applies-to: a session about to compact or restart, with state that exists only in the conversation
status: active
tags: [workflow]
kinds: [memory, python, todo, gripe]
governs: factory
---

# resume-note — one memory a new session resumes from

Run this when the user says "ready to compact", or before you hand off with
[[next]]. Conversation-only state (deploy state, blocked rounds, rulings not
yet filed) is lost at compaction; a resume-note makes it durable. The goal:
the user tells a new session "resume from me<N>" and that session can continue.

## Find or create

1. `search(kind='memory', tags=['resume-note'], q='<this session's topic>', view='index')`.
2. This session's note exists → **update it** with `edit(kind='memory',
   id='me…', mode='replace', text='<new body>')`; never mint a second one.
3. Otherwise `put(kind='memory', tags=['resume-note', 'SPACE:repo-dev'],
   title='Resume: <topic>, <UTC date>', text='<body>',
   args={'writer': {'model': '<your model id>'}})`.

The `resume-note` tag is the whole convention: `search(kind='memory',
tags=['resume-note'])` lists every note.

## Body sections

Write each section; say `none` for an empty one.

- **Goal and next step** — one line each.
- **In flight** — every open thing by handle: todos, gripes, branches and
  worktrees, rounds, deploys (sha and what blocks it), background jobs. Handle
  and state only; the ref holds the detail.
- **Notes to read** — `me…` handles of every memory needed to resume. Also
  `link(id='me<note>', target='memory:me…', rel='cites')` each one, so the
  graph carries them.
- **Files** — one python-kind ref per file the next session must open
  (`python:src/precis/path/file.py::Symbol`), not prose paths.
- **Repos in context** — the repos that must be mounted or checked out for those
  python refs to resolve, and the worktree or branch in each.
- **Transient instructions** — rulings, warnings, and steps told to you this
  session that live nowhere else. This is the one place restating content is right.

## Rules

- Anything already on a durable ref (todo, gripe, file) is a handle, not a copy.
- No secrets, cluster addresses or node hostnames in the body.
- Notes expire in 14 days; the next session deletes or retires the note once
  it has resumed and filed what is still true.
- Finish by giving the user the handle: `resume from me<N>`.
