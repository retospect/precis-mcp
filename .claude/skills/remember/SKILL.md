---
name: remember
description: "Use when you learned something durable for repo-dev memory (a gotcha, ruling, runbook step, thread state): file it into the graph in the right hub, type and links so memory-lint stays quiet."
---

# remember — keep one fact in the repo-dev graph

The graph shape (root me474312 → subject hubs → nodes, `part-of` edges, one
type tag, gotchas `qualifies` threads) is described in the root node and in
`precis-memory-help` §"Hubs, part-of and qualifies". This skill is the write
recipe; the `put`/`edit` response's `Next:` lines do most of it for you.

1. **Search first.** `search(kind='memory', tags=['SPACE:repo-dev'],
   q='<the fact>', view='index')`. Narrow to one hub with
   `args={'under': 'me<hub>'}`, to one type with `tags=[…, 'section:gotchas']`.
2. **Same subject exists → edit it.** Anchored
   `edit(kind='memory', id='me…', find='<old>', text='<new>')`; a wrong fact
   takes `reason=`. No second node.
3. **New → put it.**
   `put(kind='memory', tags=['SPACE:repo-dev', 'section:<type>'],
   meta={'hook': '<one line: status + what you need to resume>'}, text=…)`.
   Type: `threads` (in-flight, has a NEXT), `gotchas` (trap), `runbooks`
   (how to do X), `workflow` (how agents/Reto work), `reference` (stable fact).
   First body line is the title — keep it short.
4. **Run the `Next:` lines the response prints**: the `part-of` link to the
   suggested hub (check it fits; the root lists all hubs), the `qualifies`
   links from relevant gotchas to a new thread, the type tag if missing.
   No `Next:` lines = the node is in place.
5. **Size.** The fisheye shows 4000 chars of a body. Over that, the response
   says so: keep the node as a short summary and move detail into children
   `put(…, link='memory:<summary id>', rel='part-of')` with the same type tag.
6. **Cite durably.** Code as `path/file.py::Qual.name`
   (`docs/conventions/code-anchors.md`), commits as bare short shas, dates
   in UTC. A thread's landed history is not a fact — git has it.
7. **Thread landed with nothing next → retire it**:
   `delete(kind='memory', id='me…')` after moving any still-true gotcha or
   ruling into a live node.

8. **A recalled node misled you → fix it now**, not later: wrong →
   anchored `edit(…, reason='misled: <what, how found>')`; right only in some
   context → add a caveat line or a `qualifies` gotcha; obsolete → retire
   (step 7). Only if you cannot establish the truth: file the review todo
   (exact `put(kind='todo', …, tags=['memory-review'], meta={'llm_tier':
   'opus'}, link='memory:N', rel='raises-concern-about')` call in
   `precis-memory-help`, "A memory I recalled turned out wrong").

Policy (what belongs in memory at all, authority of old nodes): AGENTS.md
§"Repo-dev graph memory". Periodic cleanup: the `reconsolidate` skill.
