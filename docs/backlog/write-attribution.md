---
status: in-progress
title: Write attribution — revisions.writer records the model, process and human behind every write
---

# Write attribution

Reto, 2026-11-05 (session GARDEN): "Do we know what level model (or even, a
human) has written a memory? We should add this as meta whenever available.
Also the process (gardener, some research task, whatever) — a little log or
at least the first and last model who touched the memory?" Ruled the same
day: a labelled self-declared model is acceptable; "human" covers human
origin and agent-mediated direct human input ("opus-mediated human").

## Measured before the change (prod, 2026-11-05 UTC)

- 12,856 live memories: 11,570 `unknown`, 1,286 `generated`, 0 `human`.
- `meta.filed_by` on 460 memories, none naming a model or run.
- About 5,000 memory `revisions`, none with `model` set; `trust_acts.run`
  unset on all 3,585 acts.
- `tool_calls.client` declared a model on 0 of 200,468 calls in 30 days.
- 2,463 of 4,697 memory `put`/`edit` calls in 30 days had no client,
  session, source or agentlog.

## Design

One nullable jsonb, `revisions.writer`, in the shape of `meta.filed_by`
(actor, client, session_id, source, agentlog_id, model, model_source, human,
human_via). `revisions` already keeps one row per target per transaction, so
it is the log: first and last writer are its oldest and newest stamped rows.
No per-edit meta, which would be a second log and would have to stay out of
the content sha.

- **Transport.** `RevisionContext.writer` goes out as the GUC `precis.writer`.
  A `BEFORE INSERT` trigger on `revisions` (migration 0279) copies it, so
  every insert path is covered without replacing `precis_log_revision` or
  `precis_trust_append_act`, and a raw insert cannot name its own writer.
- **Never `ctx.model`.** A model on a `generated` context routes the write
  through the machine-edge gate (`_links_ops._machine_write_context`).
  `writer` is deliberately a separate field; `precis.model` keeps its meaning.
- **Verb writes** build the stamp in `dispatch._write_context`
  (`filing_origin.writer_stamp`). An outer context's writer wins, so a worker
  calling a verb keeps its own. `meta.filed_by` reads the same stamp at
  creation.
- **Model precedence:** agentlog (`model_source='agentlog'`), then the
  client's declaration (`declared`), then the caller's payload (`self`).
  Workers that know their model pass it directly (`caller`).
- **Agent door.** `args={'writer': {'model': '<id>', 'human': '<who>'}}` on
  `put`, `edit`, `delete`, popped before the handler gate like `trust`. It is
  a labelled claim, not proof. An MCP session with no known model gets a
  one-line ack nudge on memory writes, once per session (`hint_ledger`).
- **Human.** `human_via='direct'` when the write runs inside the web's human
  ingress; `human_via='mediated'` when an agent states the content is a
  person's direct input. `refs.trust_origin='human'` stays web-only: mediated
  input is `generated` with a `writer.human` label, because an MCP caller
  still cannot prove a person typed it. A session's acting user alone sets no
  `human`; an agent's own note in that session is not the person's input.
- **Level** is read, not stored: keep the exact model id and map to a tier at
  read time, since tier mappings change.
- **Trust.** Model level never feeds `considered`
  (`graph-node-trust-state.md`). Use it to order re-review.
- **Display.** A bare `get` of a converted kind adds a `written:` line above
  the trust line when any stamped revision exists.

Rejected: a `PreToolUse` hook injecting the model through `updatedInput`. With
no `permissionDecision` auto mode can deny the changed call, and `allow` would
auto-approve prod writes. Tool hooks get no `model` field either.

## Shipped by this item

Migration 0279, `RevisionContext.writer`, `filing_origin.writer_stamp`, the
`args.writer` door and nudge, the `written:` line, garden apply stamps,
`source` on `scripts/prod-precis`, skill text.

## Open

- [ ] Local rung: agentlog `meta.model='local'` has no `model_actual`, so a
      local run records the alias, not the served model.
- [ ] Scripted bulk edits get `source` but no model. A script run by an agent
      could pass one through an env var if the gap stays large.
- [ ] 835 memory revisions came from context-less writers (`agent_rw`,
      `(unrecorded)`, mostly retires). They get no stamp until wrapped; the
      `unrecorded_revisions` cadence names them.
- [ ] Search or gardener ordering on `writer` (re-review weak-model memories
      first) once a few weeks of data exist.

No backfill: pre-existing rows stay unstamped, never guessed.
