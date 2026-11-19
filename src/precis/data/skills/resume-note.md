---
id: resume-note
family: memory
title: precis — write or update a resume-note memory for a new session
summary: before compaction or a restart, write one memory tagged resume-note that links every memory, python file and repo a new session needs, plus work in flight and transient instructions, so "resume from me…" is the whole handoff
answers:
  - how do I leave a transfer memory a new session can resume from?
  - what do I write when the user says ready to compact?
  - how do I find earlier resume-notes?
  - where do deploy state and other conversation-only state go before compaction?
applies-to: a session about to be compacted or replaced, whose state lives only in the conversation
status: active
tags: [workflow]
kinds: [memory, python]
governs: factory
---

# resume-note — one memory a new session resumes from

Goal: the user says "resume from me123…" and the new session has the
context to continue. State that lives only in the conversation (deploy
state, a blocked round, a half-made decision) is lost at compaction;
this skill makes it durable. Run it on "ready to compact", and alongside
[[next]], whose recovery prompt can then be just the one line
`Resume from me<id>`.

## Find, then update or create

1. `search(kind='memory', tags=['SPACE:repo-dev', 'resume-note'],
   q='<this session's task>', view='index')`. A note for this same work
   exists → update it with anchored `edit(...)`, never a second note.
   All resume-notes are the nodes carrying the `resume-note` tag.
2. None → `put(kind='memory', tags=['SPACE:repo-dev', 'resume-note',
   'section:threads'], title='Resume: <task in at most 10 words>',
   meta={'hook': '<status + first step>'}, text=…)`. Link it
   `part-of` the threads hub the `Next:` line suggests ([[remember]]).
3. Name yourself on the write: `args={'writer': {'model': '<model id>'}}`.

## Body — fixed headings, in this order

- `Left:` first line, as for any thread: what remains (`Left: none` when done).
- **Goal** — one sentence.
- **In flight** — each thing half-done or waiting: sha or branch on main,
  worktree path, todo/gripe/round handles, what blocks it, who must act.
  Handles, not restated state: their current state is on their refs.
- **Memories** — one `me…` handle per line with a few words on why, for
  every note the new session must read. Also `link(kind='memory',
  id=<note>, target='me…', rel='cites')` each one, so the graph carries it.
- **Files** — python-kind refs, one per line, in the form
  `get(kind='python', id='<alias>::pkg.mod.Symbol')` (or the `py` path
  form [[precis-python-help]] documents), for every file the next step touches.
- **Repos** — the checkouts or worktree aliases that must be in context
  (`PRECIS_PYTHON_WORKTREES`) for the Files refs to resolve; name the alias
  and branch. A missing alias means the refs fail, so say so.
- **Transient** — instructions that only matter for this resume: do-not-do
  lists, the order of next steps, a ruling Reto gave in chat (mark it
  `args={'writer': {'model': '<model id>', 'human': 'reto'}}`), state to
  re-verify before trusting it.

## Rules

- Persist found issues first (todo/gripe), as [[next]] step 2 says; the
  note points at them, it does not replace them.
- Transient notes carry a UTC date. Resume-notes expire in 14 days
  ([[remember]]); retire one once its `Left:` is `none`.
- Keep the body under 4000 characters; split detail into linked nodes.
- Public repo: no cluster addresses, hostnames or secrets in the note.
- Finish by telling the user: `Resume from me<id>`.
