---
status: draft
title: Seen-state dedup — the MCP answers a repeat with a ^^ back-reference
pillar: platform
prio: normal
---

# Seen-state dedup — the MCP answers a repeat with a `^^` back-reference

Reto 2026-10-22: "it keeps the MCP from re-issuing info we already sent,
but required agent cooperation. Do we know when the agent compacts? We
could say (^^ for see above) for things sent since start or last
compaction." This spec removes the cooperation part: the server, not the
agent, remembers what it sent.

Depends on: `multi-user-overhaul.md` §S10, steps S10.0 (experiment) and
S10b (subagent stamp: `session_id` + `agent_id` on every call). Launched
agents with their own connection need neither (D-launched below).
Related: gr499044 (the same idea for the harness `Read` tool, shipped as
`scripts/hooks/read-dedup.py`), gr499039 (write-time result fold, parked on
`worktree-d-toolfold`), `server-side-session-context.md` (why dedup must
never cross sessions), `context-memory-hierarchy.md`. Thread:
`threads/agent-surface.md`.

## Problem

Tool results stay verbatim in an agent's context for the rest of its run
(gr499039: Claude Code and the Devin loop never drop them; ~57% of local
cache-read carry is tool results, 88% of sampled `doctor_tick` result bytes
are `mcp__precis__get`). An agent that re-asks for a ref it already has
pays for the body twice, and then carries both copies on every later turn.

The existing fix is client-held: skills accept
`args={'have': '<sha8>'}` and return a stub when the sha matches
(gr477798). It only works when the agent remembers to pass `have`, and only
for skills. A server-side ledger was tried first and removed, because a
subagent on its parent's connection got stubs for skills it had never
seen (`serve_ledger.py` docstring). The missing piece was a per-agent key
and a compaction signal; S10b supplies the first, Claude Code hooks the
second.

## Design

### What the server remembers

Per seen-key, a bounded in-memory map of `body_sha → (handle, call_seq,
sent_at)` for every dedup-eligible result it returned:

- seen-key = `(caller_session_id, agent_id)` from the S10b stamp
  (`agent_id = main` for the main loop); for a launched agent with its own
  credential and no stamp, `(tool_ledger.session_id, 'conn')`.
- `body_sha` = sha256 of the rendered page-1 body with the pagination
  footer stripped (cursors are minted fresh per call and must not defeat
  the match).
- `handle` = the request in short form (`get paper:smith2024 view=abstract`).

No stamp and no own credential → no seen-key → no dedup (fail open: the
call is served in full). A server restart empties the map, which also
fails open. Per-process state is acceptable under the single-uvicorn-worker
constraint (`session-mcp-http-server.md`); it never touches the DB.

### The marker

A repeat whose `body_sha` is in the agent's map, inside the window, gets
instead of the body:

```
^^ get paper:smith2024 view=abstract — unchanged since call #12 (sha 1a2b3c4d).
   Not in your context any more? args={'fresh': True} resends it.
```

If the earlier response had a `more(cursor=...)` footer, the marker carries
a freshly minted cursor so the tail stays reachable. Changed content
(different sha) is sent in full and replaces the entry. A diff instead of
the full body is not built (D6, later).

### Eligibility

- Eligible: `get` and `search` result bodies of 512 bytes or more, and
  skills (D5 applies the existing `have` stub automatically from the map).
- Never: write acks (`put`, `edit`, `delete`, `tag`, `link`), errors and
  refusals, `more` pages, results under 512 bytes (the marker saves
  little), calls with `fresh=True`, and kinds whose body is per-call
  (`time`, `random`; they change sha anyway, listed so the check is cheap).

### When the map resets

- **Compaction, clear, resume.** A new `scripts/hooks/mcp-seen-reset.py`
  (stdlib, same pattern as `read-dedup.py --session-start`) runs on
  SessionStart with `source` in `compact | clear | resume`, and on
  PreCompact. It POSTs `{session_id}` to `POST /seen/reset` on the HTTP
  server (bearer-authenticated, same token as the MCP connection), which
  drops every seen-key with that `caller_session_id`. Any hook failure is
  silent; the window bound below still applies.
- **Subagents** start empty automatically: new `agent_id`, new seen-key.
  A fork subagent inherits its parent's context but starts with an empty
  map, so it gets full bodies (wasteful, never wrong).
- **New session** = new `session_id` = empty map.
- **Window.** An entry expires after 30 minutes or after the agent's next
  40 calls, whichever comes first (open question 2). This bounds the case
  the hooks cannot see.

### Risks designed for

- **Results cleared without a compaction.** Claude Code may drop old tool
  results (context editing, microcompact) with no hook firing; a marker
  then points at nothing. Mitigations: the window; every marker names the
  `fresh` escape; the fresh-call rate is measured (below) and is the
  kill signal.
- **Subagent compaction.** S10.0(d) records whether a subagent's
  compaction fires a hook with its `agent_id`. If not, subagent entries
  rely on the window alone.
- **Mapping the hook's session to the MCP connection.** Solved by the
  stamp: the reset hook and the PreToolUse stamp carry the same Claude
  `session_id`, so the server keys on it, not on `Mcp-Session-Id` (which
  also changes when the connection reconnects after a server bounce).
- **Forged stamp.** A model-written `_caller` can only point the dedup at a
  key whose map it then misses (full bodies served). It cannot read another
  agent's content: markers carry no body.
- **Prompt cache.** The marker is fixed at write time, like the d-toolfold
  fold, so earlier turns stay byte-identical. Order at the seam: dedup check
  first, then fold (`_pagination.py::PaginationCache.split`).

### Measurement

`tool_ledger` gains `dedup` (`hit | shadow-hit | fresh`) and
`dedup_bytes` (body bytes not sent). Per session: bytes saved, and carry
saved ≈ bytes ÷ 4 × the agent's remaining turns. Miss signal: a `fresh=True`
call for a `body_sha` that got a marker in the same seen-key.

Acceptance thresholds, read after seven days of D4:

- fresh-after-marker rate ≤ 5% of markers (above 15%: shorten the window or
  switch off);
- markers save ≥ 10% of `get`/`search` result bytes on stamped sessions;
- zero markers served to a seen-key that had not received that sha (test
  invariant, also checked on the ledger).

## Slices

| Slice | Delivers | Acceptance |
|---|---|---|
| **D0** | `multi-user-overhaul.md` S10.0 experiment and S10b stamp | as specced there |
| **D1** shadow | seen-state map + eligibility in `tools/core.py::_dispatch`; computes would-be hits and logs `dedup=shadow-hit` + `dedup_bytes`; always serves the full body; migration adds the two ledger columns (ships via `/go`); `PRECIS_SEEN_DEDUP=off|shadow|on`, default `shadow` | a repeat `get` within one seen-key logs `shadow-hit`; the same `get` from a second `agent_id` of the same session logs nothing; a write ack never logs |
| **D2** reset | `POST /seen/reset`, `scripts/hooks/mcp-seen-reset.py` wired SessionStart + PreCompact in `.claude/settings.json` and the global copy | after a `/compact` the first repeat logs no `shadow-hit`; a reset for session A leaves session B's map intact |
| **D3** launched agents | seen-key `(tool_ledger.session_id, 'conn')` for S10a-credentialed connections without a stamp; the claude_agent loops (`dream`, `doctor_tick`, `plan_tick`) report reset on their own compaction if their loop exposes it, else window only | a launched loop's repeat `get` logs `shadow-hit` |
| **D4** markers on | flip default to `on` after one shadow week shows ≥ 10% of `get`/`search` bytes as would-be hits; `fresh` escape; marker text as above, with a fresh cursor when paginated | thresholds above; `fresh=True` always returns the full body |
| **D5** skills | skills consult the same map, so a repeat skill fetch gets the existing `have` stub without the agent passing `have`; explicit `have` keeps working | a second `get(kind='skill', id=X)` in one seen-key returns the stub; a subagent's first fetch returns the full skill |
| **D6** (later) diffs | changed `md`/`plaintext`/`draft` bodies under 50% churn sent as a unified diff against the sha in the map | only after D4's thresholds hold for a month |

## Not built

- **In-call cookies or agent-declared state.** The agent never states what
  it has; the server infers it from what it sent (`multi-user-overhaul.md`
  §S10, Not built).
- **Cross-session or cross-agent dedup** (`server-side-session-context.md`:
  a sibling does not hold the text).
- **Persisting seen-state** across server restarts or in Postgres.
- **Dedup of non-MCP tools** (`Read` has its own hook, gr499044; Bash
  output is gr499039's fold).

## Reto 2026-10-22 review (his ideas; the last bullet is a recommendation awaiting his call)

- **A hash is enough.** The map stays `body_sha → entry` plus a second
  `handle → last body_sha`; the repeat check is one set lookup.
- **Say "supersedes".** When a handle comes back with a different sha, the
  full body is prefixed with one line: `supersedes the copy from call #12
  (sha 1a2b3c4d)`, so the agent does not merge two versions.
- **Invalidate by timestamp.** Any reset signal (compaction hook, a detected
  or suspected tool-result clear, `args={'fresh': True}` on any call, or an
  explicit `seen reset`) drops every entry sent before that moment, not just
  one entry. The rolling window is the same rule applied continuously.
- **Recommended: self-describing marker, no mnemonic.** The marker always states what it
  is in plain words (as above), and names the skill once per window
  (`skill:precis-seen-help`). Reto floated: a verbose first
  explanation then a short tag, and a rare-token mnemonic such as `緩存` found
  via the skills. Recommendation against: both fail exactly when the agent has forgotten the first explanation, while
  the plain marker costs ~15 tokens against bodies of hundreds.

## Open questions for Reto

1. **Fallback if S10.0 shows `updatedInput` does not reach MCP tools.**
   Recommendation: ship D3 (launched agents only — where the measured
   carry is: `dream` 164.7M, `doctor_tick` 34.8M cache-read tokens in 14
   days) and leave interactive Claude Code sessions without dedup, rather
   than guess subagent boundaries from timing.
2. **Window.** 30 minutes or 40 calls per agent, whichever first?
   Recommendation: yes for D4; tune from the fresh-call rate, not before.
