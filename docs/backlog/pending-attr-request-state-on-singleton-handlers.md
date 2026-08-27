# `_pending_*` request state on singleton handlers — cross-request leak/race exposure

Handlers are process-lifetime singletons (one `Hub` per server,
`runtime/factory.py::boot`), but at least two thread per-request arguments
through mutable instance attrs because `CacheBackedHandler.get()`'s
signature is fixed: `memory.py`'s `_pending_title` and (2026-08-27, same
convention followed deliberately) `semanticscholar.py`'s `_pending_exclude`.
Set in `get()`, consumed in `_render`, cleared in `finally`. If two calls
ever overlap on one instance, one request's list leaks into the other's
render or is nulled mid-render — a *silent wrong-result* (wrong papers
excluded / wrong held flags), no exception. Today the MCP stdio loop is
serial per process, so no observed defect — this is a latent trap for any
future threaded/web reuse of the same Hub.

Fix direction: give the cache-backed get path an explicit request-context
parameter (or a contextvar) instead of instance state, and migrate both
call sites; alternatively pin the serial-per-handler guarantee with an
assertion/comment where Hub is built. Sweep for other `_pending_` attrs
while there.

test: simulate two interleaved `get()` calls on one handler instance
(threads or manual re-entry) and assert neither request's exclude/title
bleeds into the other.
