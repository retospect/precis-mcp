---
status: draft
prio: high
---

# Runtime dispatch codereview sweep

Grouped 2026-09-26 from 6 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Give PrecisRuntime explicit ownership and a managed lifecycle

_Grouped 2026-09-26; was `managed-runtime-lifecycle`, status idea._

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

## Replace PrecisRuntime cross-mixin stubs with explicit collaborators

_Grouped 2026-09-26; was `runtime-mixin-decomposition`, status idea, prio low._

The runtime package split reduced one monolithic file but retained one implicit
object: five mixins call sibling-mixin methods through `RuntimeShape`, a plain
class of `NotImplementedError` stubs whose safety depends on C3 ordering. The
largest slices remain `runtime/dispatch.py` and `runtime/search.py`; adding a
cross-concern helper expands the shared fake shape and mypy can accept a method
whose real implementation was renamed or dropped.

When runtime behaviour next needs a substantive change, extract explicit
`DispatchService`, `SearchService`, request-context, and pagination
collaborators rather than adding another mixin or `RuntimeShape` stub. Preserve
`PrecisRuntime` as the public facade. Add a structural guard that every shared
shape declaration resolves to a concrete implementation before any incremental
carve ships.

Owner anchors: `src/precis/runtime/core.py::PrecisRuntime`,
`src/precis/runtime/_shared.py::RuntimeShape`,
`src/precis/runtime/dispatch.py::DispatchMixin`,
`src/precis/runtime/search.py::SearchMixin`.

## Make kind enablement one boundary for built-ins, plugins, and siblings

_Grouped 2026-09-26; was `plugin-kind-gate-parity`, status idea, prio high._

`PRECIS_KINDS_DISABLED` is not authoritative today. `precis.dispatch._load_plugins`
constructs and registers entry-point handlers without `kind_gate.gate`, without a
`Loadability`, and without the parsed prohibition set; reproduced with a fake
entry point, where `boot(kinds_disabled={'probe'})` still loaded `probe` and the
cold-start banner had no verdict. `Hub.sibling` has the same boundary problem:
on a booted hub, an absent/prohibited `job` or `todo` handler is lazily rebuilt
for direct internal use.

Make one gate-aware construction path cover built-ins and plugins while keeping
plugins' wider exception isolation. A booted hub must never lazily resurrect a
kind that boot rejected; test-only bare-hub convenience needs an explicit
separate path. Pin plugin `requires_env`/`requires_secret`/`requires_setting`,
prohibition reasons, banner loadability, and prohibited `job`/`todo` sibling
behaviour.

Owner anchors: `src/precis/dispatch.py::_try`,
`src/precis/dispatch.py::_load_plugins`, `src/precis/dispatch.py::Hub.sibling`,
`src/precis/kind_gate.py::gate`.

## Make MCP cancellation safe for mutating tools and real concurrency

_Grouped 2026-09-26; was `mcp-cancellation-write-safety`, status idea, prio high._

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

## Import only the selected CLI command

_Grouped 2026-09-26; was `lazy-cli-command-imports`, status idea, prio normal._

`precis.cli.main` imports the complete subcommand catalogue before parsing the
requested command. An unrelated command's module-level optional dependency or
side effect can therefore break every entry point; this already forced
`tenacity` into core after eager `fetch_openalex` import crash-looped the slim
`serve-embeddings` service. The same coupling inflates startup and turns future
optional-extra mistakes into package-wide outages.

Replace the eager module tuple with a dependency-light command registry and
import the owning module only after the first command token is known. Preserve
full top-level/subcommand `--help` discovery without importing heavy execution
modules. Test a slim environment where selected core commands still parse and
run while unrelated optional command dependencies are unavailable.

Owner anchors: `src/precis/cli/main.py::_build_parser`,
`src/precis/cli/main.py::main`, `pyproject.toml` core-dependency rationale.

## Repair architecture-record drift at load-bearing seams

_Grouped 2026-09-26; was `architecture-doc-contract-drift`, status idea, prio normal._

The architecture record contradicts code at several load-bearing seams:
`precis.workers` says `run_handler_once` processes a batch in one transaction,
while the runner deliberately uses claim/process/write phases with two short
transactions; `precis_chem` and `precis_bio` package docstrings still describe
retired private enable flags while their handlers/tests say the plugins are
always on; `docs/codebase.md` calls the same public surface both seven and eight
verbs; `AGENTS.md` still claims Python 3.11 support while `pyproject.toml`
requires 3.12. These are the exact docs the project tells agents to trust.

Audit `docs/codebase.md` plus owning package docstrings against composition
roots and invariant tests, correct present-tense claims, and add cheap parity
assertions where code can express the contract (verb count, plugin gate state,
transaction phase shape). Do not turn this into a broad prose rewrite.

Owner anchors: `src/precis/workers/__init__.py`,
`src/precis/workers/runner.py::run_handler_once`, `src/precis_chem/__init__.py`,
`src/precis_bio/__init__.py`, `docs/codebase.md`.
