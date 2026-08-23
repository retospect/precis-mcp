---
status: draft
title: "agent tool deny-lists go inert under the command MCP profile — a config flip silently grants edit/delete/link"
---

# Agent tool deny-lists go inert under the `command` MCP profile

`workers/dream_agent.py::_DREAM_DISALLOWED_TOOLS` denies by per-verb tool
name:

```python
"mcp__precis__edit", "mcp__precis__delete", "mcp__precis__link"
```

Those names only exist under the **`typed`** profile. `PRECIS_MCP_PROFILE=
command` (`server.py`) collapses the whole surface into a single
`precis(command, text=None)` tool parsed by `tools/command_parser.py`, so
none of the three denied names is registered — the deny list matches
nothing and the agent can call `edit` / `delete` / `link` through
`precis("delete(kind='paper', id=…)")` with no gate.

**Not currently exploitable:** the deploy sets no `PRECIS_MCP_PROFILE`, so
workers run `typed` and the denies are in force. Verified against
`deploy/roles/precis_worker_agent/templates/precis-worker-agent.plist.j2`.

**Why it matters now.** The deny list is load-bearing for a stated safety
property, written in `dream_agent.py`'s own comment: the dream's fisheye
pulls recent paper/patent summaries into the prompt *unvetted*, and "a
crafted summary must not be able to steer it into `delete`/`edit`/`link` of
arbitrary refs". Since 2026-08-23 (b6e64acf) the dream also mints nanopub
hypothesis hubs; its proposal door deliberately writes the motivation and
provenance edges server-side precisely so `link` can stay denied. Flipping
one env var silently removes all of that — a config change with no code
change and no error.

## Shape of a fix

The deny must be expressed where the profile can't erase it — options,
roughly cheapest first:

1. **Verb-level deny in dispatch.** An env/envelope-carried set of denied
   *verbs* checked in `runtime/dispatch.py`, which both profiles funnel
   through. The tool-name deny then becomes belt-and-braces rather than the
   only gate.
2. **Refuse the combination.** `run_dream_pass` (and any pass with a
   non-empty `disallowed_tools`) asserts `PRECIS_MCP_PROFILE != 'command'`
   and gates itself off with a clear log line rather than running unguarded.
3. **Profile-aware translation.** Map denied verb names to the active
   profile's tool surface at dispatch-config time so one declaration covers
   both.

(1) is the real fix; (2) is a five-line stopgap that converts a silent
capability grant into a visible refusal, and is worth doing first if the
`command` profile is going anywhere near a worker.

## Scope note

This is not dream-specific. Any pass setting `disallowed_tools` with
`mcp__precis__*` names has the same hole — `workers/registry.py` shows
reviewer passes denying `WebFetch`/`WebSearch` (built-ins, unaffected), so
the dream is currently the only pass denying *precis verbs*. It would stop
being the only one the moment another pass copies the pattern.
