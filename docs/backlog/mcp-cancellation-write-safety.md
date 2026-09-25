---
status: idea
title: Make MCP cancellation safe for mutating tools and real concurrency
prio: high
model: opus
---

# Make MCP cancellation safe for mutating tools and real concurrency

`precis.server._offload_sync` applies `abandon_on_cancel=True` to every tool.
Cancellation therefore returns and releases the async semaphore while the OS
thread keeps running. For `put`/`edit`/`delete`/`tag`/`link`, a write may commit
after the caller has cancelled and retried; for every verb, abandoned bodies can
exceed `PRECIS_MCP_TOOL_CONCURRENCY` and contend at AnyIO's wider thread limit
against the ten-slot DB pool. The current test explicitly pins permit release
while the abandoned body is still blocked.

Separate read and mutation cancellation policy. A capacity token must remain
owned until the real sync body exits, not merely until its awaiting task is
cancelled. Mutations must not report cancellation as complete unless the work
was cooperatively stopped; genuinely interruptible long reads/CPU work belong
behind deadlines, a subprocess, or the job substrate. Add tests for cancelled
writes (no late commit or duplicate retry) and for the true executing-body cap
under a cancellation burst.

Owner anchors: `src/precis/server.py::_offload_sync`,
`src/precis/server.py::_register_tools_from_registry`,
`tests/test_mcp_tool_offload.py::test_offload_sync_cancel_returns_promptly_and_frees_semaphore`.
