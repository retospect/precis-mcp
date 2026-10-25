---
status: draft
title: Read-only agents file gripes (agent_ro friction channel)
pillar: platform
---

# Read-only agents file gripes

gr516603: agent_ro runs (write:none envelope: tier-R builders, read-only
cluster agents, the review container) cannot report friction, so it is lost.

## Motivation / why

Finding: the DB half already exists. Migration 0079 ships
`public.file_gripe_readonly(text)` (SECURITY DEFINER, one gripe ref + body
chunk + `STATUS:open`), and `GripeHandler._insert_gripe` routes every gripe
through it. What still blocks a ro agent, in order:

1. **Tool layer.** `envelope.denied_verbs` returns every mutate verb for
   `write:none`; `runtime/dispatch.py::_refuse_envelope_denied_verb` raises
   `Denied` on `put` before the handler runs. Reto's 2026-10-21 decision in
   `review-container-readonly-role.md` (kind-level carve-out for
   `put(kind='gripe')`, no new MCP tool) is unbuilt.
2. **Tags and links fail.** `_insert_gripe` applies caller `tags=`/`link=` with
   `add_tag`/`add_link` in the same transaction; agent_ro has no INSERT on
   `ref_tags`/`links`, so a tagged ro gripe rolls back whole. Today only a bare
   gripe could succeed, and nothing marks it as ro-sourced.
3. **No policy, cap or size limit.** The function is `GRANT ... TO PUBLIC`,
   takes unbounded text, and any caller can loop it.
4. **No nudge.** `utils/friction_reflect.py` (end-of-run footer, default OFF,
   `friction_eligible` needs MCP) tells agents to file with
   `tags=['friction', ...]`, which would hit (2) from a ro run.

## Policy

Nudge every ro run to end with a friction check; hard cap of 3 gripes per run;
no quota or sampled percentage (a sample drops the rare, important complaint).
"friction: none" stays the honoured default (the existing footer wording);
never invent friction. Reuse `friction_reflect.py`'s footer for ro runs (its
eligibility rule gains "ro envelope" as a reason to emit it), instead of a
second prompt. Runs with `max_turns` under `FRICTION_MIN_TURNS` skip it.

## Mechanism options

**(a) Narrow DB carve-out (recommended; mostly built).**
Extend 0079 rather than add a table: new `file_gripe_readonly(text, p_source
text)` that also stamps the tags itself (inside the definer function, where
the INSERT grants exist), so the handler never needs ref_tags INSERT.
- Security: the function can only create `kind='gripe'`, `STATUS:open`, plus
  the fixed tag `source:agent_ro` and a validated `agent:<name>` tag. No
  UPDATE/DELETE, no other kinds, no links. Rate limit inside the function:
  count gripes carrying the caller's run tag in the last hour; refuse past 3
  per run and (backstop) 20 per hour per `session_user`. Text capped at 4000
  chars, secrets masked as the handler already does.
- Complexity: one forward migration + a dispatch carve-out (below). Lowest of
  the three; one code path with rw filing.
- Failure modes: the definer function is a prod write path reachable from any
  prompt-injected ro agent (see Abuse); a cap keyed on a caller-supplied run
  name is spoofable, so the hourly per-role cap is the real bound; if the
  carve-out is wrong, `put(kind='finding')` leaks through under write:none
  (test pins it refused).

**(b) Structured block in final output, ingested by rw parent/coordinator.**
Format, one block per gripe, at the end of the final message:
```
FRICTION
title: <one line>
body: <what blocked you, what you expected, up to 1200 chars>
END
```
At most 3 blocks. A rw consumer (worker executor or coordinator session) parses
the block and files with `source:agent_ro` + `agent:<name>`.
- Security: no DB grant at all; the rw side validates and caps. Best fit for
  agents with no DB (Devin builders already emit FRICTION blocks).
- Complexity: needs a parser per executor; every ro executor must wire it.
- Failure modes: lost when the parent crashes, truncates output, or never
  parses; delayed until the parent runs; output injection by task content.

**(c) Spool file ingested by a rw sweeper.** Rejected: needs a writable path
the write:none box deliberately lacks, a sweeper tick, and a path-trust model;
(b) gives the same decoupling with no new filesystem surface.

**Recommendation:** (a) for any agent with a precis MCP connection; (b) as the
fallback for DB-less agents. Both end in the same tags, so triage sees one
population.

## Dispatch carve-out (the part (a) still needs)

In `_refuse_envelope_denied_verb`, allow `put` when `args['kind'] == 'gripe'`
and `id` is unset (create only: no comments, edits, tags, links on existing
gripes). The handler, when the connection is agent_ro (`_probe_read_only`
already detects it), calls the function with its source arg and skips
`add_tag`/`add_link`/`set_prio`; caller `tags=`/`link=` are ignored with a
reading, not an error. Verbs stay seven (no `file_gripe` tool).

## Tagging

`source:agent_ro` and `agent:<name>` (name from the envelope/run, e.g. the job
type or builder name). Check at build time: `Tag.parse_strict(..., kind='gripe')`
and `docs/reference` tag matrix decide whether `source` is an open namespace;
`cluster:agent-ro` on gr516603 shows free-form `ns:value` tags are accepted on
gripes, so expect no registration, but pin it with a test. Existing
`friction` / `friction-model:<model>` tags are added by the function too.

## Abuse and safety

- Prompt-injected ro agent spamming: per-run cap 3, per-role hourly cap 20,
  4000-char limit, all enforced in SQL so a client bug cannot bypass them.
- Gripe text is untrusted: it reaches GRIPE_FIX triage, which reads it as data;
  triage prompts must not follow instructions in a `source:agent_ro` body.
- No links, no edits, no tag choice: the definer function accepts text and a
  source/agent label only; the label is validated against `[a-z0-9_.:-]{1,48}`.
- Secrets: same masking as the handler path (`mask_secrets`).

## Fit with GRIPE_FIX triage

Rows land `STATUS:open`, untriaged, no filer-side dedup (filers just file;
triage dedups). Triage may batch by `source:agent_ro` and `agent:<name>`, e.g.
one pass over the week's ro gripes. The `friction-reflection-enable.md` gate
(a downstream grouping/dedup lane must exist before enabling the footer)
applies here too: enable the ro nudge only when that lane exists.

## In scope

Migration extending 0079; dispatch carve-out; handler ro path; footer wiring
for ro envelopes; (b) FRICTION-block parser for one DB-less executor as slice 3.

## Explicitly NOT in scope

Comments, edits or status changes by ro agents; ro writes of any other kind;
a new MCP tool; flipping the review container to agent_ro (ops step in
`review-container-readonly-role.md`); triage prompt changes; aggregation of
friction across runs (`friction-reflection-enable.md`).

## Acceptance criteria

- Under write:none + agent_ro, `put(kind='gripe', text=...)` succeeds and the
  row has `STATUS:open`, `source:agent_ro`, `agent:<name>`.
- Same connection: `put(kind='finding')`, `put(kind='gripe', id=N)`, `edit`,
  `tag`, `link`, `delete` are all refused; a gripe with `link=` files without
  the link and says so.
- The 4th gripe of a run and the 21st of an hour are refused with a next-step;
  text over 4000 chars is refused.
- Migration test: function creates exactly one ref + chunk + tag rows; no
  other table changes (extend `tests/test_gripe_carveout.py`).
- Forward-only migration; no hostnames or IPs in any new file.

## Slices

1. Migration (cap, size limit, source/agent tags) + SQL tests.
2. Dispatch carve-out + handler ro path + envelope tests.
3. Footer wiring for ro runs behind the existing friction switch; (b) block
   parser for DB-less executors (Devin FRICTION).

## Open questions / decisions log

- **Which runs get the nudge on day one?** Every ro run (recommended), or only
  tier-R builders first to gauge junk rate before the cluster agents? (Reto)
- Hourly per-role cap of 20: right order of magnitude, or tighter?
- Who owns the `agent:<name>` label for tier-R builders (envelope, job type,
  or the builder's own name)?
- Is a triage lane ready to absorb these, or does the nudge wait for it
  (`friction-reflection-enable.md`)?
