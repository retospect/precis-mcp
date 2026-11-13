---
id: mcp-down
family: memory
title: precis — what to do when the session MCP is down or a call aborts
summary: the outage contract — the MCP-down rule, verifying unconfirmed writes (an abort is outcome unknown, not failed), the get() ModuleNotFoundError signature, the post-restart peer ping
answers:
  - the session MCP is down or unreachable — what do I do?
  - a precis call aborted or reported failed — did the write land?
  - get() throws ModuleNotFoundError while search, put and tag still work — what is it?
  - should I restart the precis server myself?
  - how do I ping peers after the server restarts?
applies-to: a dead session MCP, an aborted or "failed" precis call, a get() ModuleNotFoundError
status: active
tags: [troubleshooting, workflow]
governs: factory
---

# mcp-down — when the session MCP is down or a call aborts

The outage contract: your moves while precis is unreachable, and how to
verify the writes that never confirmed once it is back.

## The session MCP is down — what do I do?
## Recover a dead or unreachable precis MCP

The MCP-down rule (gr657041): while precis is down your only moves are
restore the connection or wait about five minutes and retry — do not
carry on with the task without it. Subagents inherit the dead MCP, so
delegating buys nothing. Restarting the server is not yours: flag the
outage to the release owner or Reto.

`scripts/prod-precis tools get|search|put|edit` runs the same handlers
against prod for operators — write subcommands refuse unless the tree
descends from origin/prod, reads only warn on a stale tree, and search
returns a bounded top-N. Whether an agent may use it to work through an
outage is an open ruling against the rule above; until it settles, the
rule stands.

## A call aborted or reported "failed" — did the write land?
## Verify an unconfirmed write before retrying it

An aborted call or a `failed` notification is outcome unknown, not
failed — the write may have committed anyway. Check-then-refile:

- Keep a list of the unconfirmed writes (verb, kind, first line, key).
- Once the connection is back, `search` for each one — did it land?
  Re-file only the missing ones; a write you have not checked is never
  reissued blind.
- If the original `put` carried `idem_key=`, re-send it with the SAME
  key — the server returns the first write's ref instead of minting a
  duplicate. A fresh key cannot dedupe an earlier write; on an unkeyed
  original it only makes the re-file's own retry safe, which is why
  the search comes first.
- Prefer an anchored `edit(find=…)` over a blind append — it refuses on
  zero matches, so a wrong retry is a no-op, not a duplicate.

```python
search(kind="memory", q="<the write's first line>")   # did it land?
put(kind="todo", text="<the todo>", idem_key="outage-2026-11-13:2")  # the original key if it had one
edit(kind="memory", id="me123456", find="<anchored text>", text="<replacement>")
```

The check-then-refile list is [[precis-gripe-help]] "When precis stops
responding"; `idem_key=` mechanics are [[precis-put-help]] "Make a retry
safe with idem_key".

## get() throws ModuleNotFoundError while search, put and tag work

A `get()` that raises `ModuleNotFoundError` while `search`, `put` and
`tag` still work is a missing server-side dependency, not your call —
a retry loop changes nothing. Read the same refs through `search`
snippets and flag it to the release owner; one gripe naming the verb is
enough.

## The server restarted after real downtime

Send one batched message to interactive peers — a single batch, never a
second round.

## See also

- [[precis-gripe-help]] — check-then-refile for writes that never confirmed
- [[precis-put-help]] — `idem_key=` dedupes a retried create
- [[precis-edit-help]] — anchored `find=` rewrites
- [[precis-search-help]] — snippet reads while `get` is broken
- [[precis-overview]] — the seven verbs and address grammar
