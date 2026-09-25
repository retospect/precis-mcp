---
status: idea
title: Give PrecisRuntime explicit ownership and a managed lifecycle
model: opus
---

# Give PrecisRuntime explicit ownership and a managed lifecycle

`build_runtime` currently connects the store, mutates process globals, scrubs the
DSN from `os.environ`, can resurrect an adopted DSN into an explicitly storeless
config, performs boot-time DB writes, and starts daemon reconciliation threads.
`PrecisRuntime` itself has no `close()` or context-manager contract, so several
maintenance CLI paths construct one and never close its pool; shutdown code is
reimplemented in the server, web lifespan, worker, and `tools.core` atexit hook.
Daemon boot tasks are not owned or joined, so a one-shot CLI may exit before
they finish or close the store underneath them.

Make the runtime the idempotent owner of its store and startup tasks, with
`close()` plus sync context-manager support. Split pure dependency composition
from process startup policy: environment scrubbing, process-global resolver
binding, kind upserts, and packaged-data reconciliation should be explicit
server/worker lifecycle steps, not unavoidable effects of every
`build_runtime()` call. Migrate all direct callers and pin repeated build/close,
intentional stateless-after-stateful construction, and shutdown-during-sync.

Owner anchors: `src/precis/runtime/factory.py::build_runtime`,
`src/precis/runtime/core.py::PrecisRuntime`, `src/precis/dispatch.py::boot`,
`src/precis/server.py::_shutdown_runtime`, `src/precis/tools/core.py::_build_runtime_locked`.
Related: `cli-bind-store-audit.md`.
