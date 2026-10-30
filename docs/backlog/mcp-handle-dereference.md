---
status: ready
title: Dereference universal handles in MCP write arguments
pillar: platform
prio: normal
---

# Dereference universal handles in MCP write arguments

## Motivation / why

An agent that has read a useful chunk cannot pass it to another tool without
copying the full text through its next tool call. That wastes context and makes
JSON escaping part of the task, especially for LaTeX and source code. Precis
already has persistent, typed addresses for this content; the missing door is
using one as an argument value.

```python
get(id="dc2445852")
put(kind="memory", text={"$ref": "dc2445852"}, tags=["SPACE:repo-dev"])
# ack leads: dereferenced text <- dc2445852 (312 chars, sha256:…)
```

This is value substitution, not a named buffer: the server resolves the handle
when the write executes. The earlier `get` permits the substitution; it does not
pin the bytes. A source edited between the calls yields the new value and a new
digest in the ack.

Related: `context-navigation.md` §7 (session identity and the difference between
a shared server and a shared context), `workspace-db-references.md` (lazy handle
expansion at an export boundary), gr524256 (cursor lifetime/reuse defects).

## Contract

### Reference envelope

The entire `text=` value is exactly `{"$ref": "<universal-handle>"}`. Unknown
keys, nested references, lists of references, and references embedded in strings
are `BadInput`; there is no template language. The exact object shape is
reserved for this feature, including when a client bridge auto-parses a JSON
string into a dict.

v1 accepts refs-backed record handles and chunk handles that both resolve
through `Store.resolve_handle` and have an exact-handle `get` read. Chunk kinds
without that read (for example `gc…` and `jc…`) are refused with a next step;
their owning ref's record handle remains available. File-backed/other-table
codes and cursors are not accepted. A chunk handle expands to that live body
chunk's exact `chunks.text`. A record handle expands to its live body chunks
(`ord >= 0`) in `ord` order, joined with `\n\n`; synthetic cards are excluded. A
record with no body chunks is not an empty string: refuse with a next step
naming a concrete chunk or an ordinary text argument.

Expansion uses `Store.resolve_handle`, `Store.chunk_text_by_id`, and a focused
live-body chunk query (generic `list_chunks_for_ref` does not filter
`retired_at`); it does not call a handler's agent-facing `get` renderer.
Headers, link neighbourhoods, and hints therefore never become payload text.
Merged-record redirects keep their existing hint behavior; the ack names both
the requested and resolved handles.

### Where substitution happens

v1 covers only `put` and `edit` `text=`. The runtime's existing `text`/`body`
alias then applies normally, so a finding can receive the value through its
canonical `body` parameter without widening every string-typed wire field.
Kinds that declare both fields keep their existing meaning: only `text` accepts
a reference.

Resolve in `DispatchMixin.dispatch_with_status` before
`_dispatch_with_retry`, after the wrapper has recognized and preserved the
reference envelope instead of passing it through `_coerce_text_body` or the
pre-dispatch string-size check. Enforce `_TEXT_PAYLOAD_MAX_BYTES` on the
resolved UTF-8 value. Unknown, retired, wrong-shaped, and unreadable references
fail before handler invocation; a failed resolution changes nothing.

Prepend one ack line to a successful write: destination field, requested
handle, resolved handle if redirected, character count, and sha256 of the
resolved UTF-8 text. Do not echo the text. `dry_run` resolves and reports the
same line without writing. Preserve the original reference envelope for logs;
the bounded tool-call sample must not mistake the dereferenced body for text
the caller sent.

### Read-before-write receipt

A mutating call may dereference only a handle that the same MCP session
successfully read with `get` earlier. Search results, a handle mentioned in
prose, and another session's read do not count. A missing receipt is `Denied`
with `next="get(id='<handle>')"`; the write is not attempted.

Use `serve_ledger.current_session()` plus the public session-key seam in
`tool_ledger` to key a bounded, TTL-expiry in-memory receipt set. Record a
receipt only after a successful exact-handle `get`; batch reads and
kind/id-mismatched reads do not qualify. Key by resolved identity so a merged
handle's read and write agree. CLI/in-process calls without an MCP session do
not use `$ref` in v1 and get a refusal naming the literal-text alternative.

This is a laundering guard, not authorization: ordinary read/write permissions
still apply, and a session includes subagents sharing that connection. A server
restart clears receipts and fails closed with a fresh-read instruction. The
single-worker constraint in `session-mcp-http-server.md` therefore also covers
this map; multi-worker support must externalize it first. Oversized reads that
refuse with a targeted next step do not qualify because they are not successful
reads. A receipt attests to the successful read, not that every source byte
remained in context, and must not be described as a prompt-injection defense.

## In scope

- The exact `$ref` envelope on `put`/`edit` `text=`.
- Current-value expansion of refs-backed record handles and exact-readable chunk
  handles.
- Same-session read receipts, size checks, typed errors, and resolution acks.
- MCP integration tests plus `precis-addressing-help`, `precis-put-help`, and
  `precis-edit-help` updates with one copy-pasteable example and the failure
  rules.
- One end-to-end dogfood: read a LaTeX-bearing draft chunk, create a memory from
  it without retransmission, edit it through another handle, and verify the
  stored bytes and ack digest.

## Explicitly NOT in scope

- `body=`, `title=`, `source_quote=`, nested `args=`, `ops=`, or references in
  list elements; each needs its own wire-shape decision.
- `view=`/`extent=` selection, computed views, search results, arbitrary tool
  outputs, or a general BUF9/ring-buffer abstraction.
- Pagination cursors as references. Cursors are single-use, process-local page
  capabilities; persistent handles have the required value semantics.
- String interpolation, concatenation, transforms, fan-out, and batch writes.
  Scripts remain the path for those jobs.
- Cross-session receipts, durable receipt storage, per-agent separation within
  one MCP session, or changes to authorization.
- File-backed handles, bodyless metadata records, chunk kinds without an
  exact-handle read, and a literal-JSON escape for the reserved exact `$ref`
  object shape.

## Acceptance criteria

1. After `get(id="dc…")`, `put(kind="memory", text={"$ref":"dc…"})` stores
   exactly that chunk's text, including backslashes and newlines; the ack names
   the handle, length, and digest.
2. A record handle joins only live body chunks in order. A bodyless record,
   unknown handle, retired or exact-unreadable chunk, malformed envelope,
   cursor, or oversized expansion refuses before mutation with a concrete next
   step.
3. With no read, after a different session's read, and after a server restart,
   the same write is `Denied` and the target is unchanged. A successful
   same-session read permits it.
4. Editing the source after the read changes what a later write stores; the ack
   digest reflects the executed value, not the earlier read.
5. `edit` replacement and `dry_run` obey the same rules. Ordinary dict/list
   `text=` payloads other than the reserved exact envelope retain their current
   JSON coercion behavior.
6. Logs retain the caller's reference without logging the expanded body, and
   stored payload never contains pagination or fold markup.
7. The advertised schema and shipped skills document the door; the end-to-end
   dogfood passes through the real MCP transport.

## Target + blast radius

- `src/precis/tools/core.py::_coerce_text_body`, `put`, `edit`, and resolved
  payload-size validation.
- New focused resolver/receipt module under `src/precis/runtime/`; invocation
  and ack decoration in `runtime/dispatch.py::DispatchMixin.dispatch_with_status`.
- Existing resolution/text helpers in `store/_refs_ops.py::RefsOps.resolve_handle`
  and `store/_chunks_ops.py`; no schema migration.
- Public session identity from `serve_ledger`/`tool_ledger`; bounded process
  state joins the single-worker inventory in `session-mcp-http-server.md`.
- `tests/test_mcp_handle_deref.py` for value/error/receipt behavior, MCP schema
  and transport tests, and a no-secret/no-body assertion for tool logging.

## Implementation slices

1. Pure envelope parser and store-backed expansion, with chunk/record, redirect,
   empty-body, and size-limit tests.
2. Runtime substitution, receipts, acks, dry-run, and audit sampling; test two
   concurrent sessions and restart loss.
3. Preserve the envelope through `put`/`edit`, advertise the shape, update the
   three skills, and run the real-transport dogfood.

## Open questions / decisions log

Decided in plan review: structured whole-value envelope; execution-time values;
persistent handles rather than cursors; `text=` only; same-session reads;
fail-closed receipt loss. Oversized agent responses refuse with targeted reads;
there is no pagination continuation.
