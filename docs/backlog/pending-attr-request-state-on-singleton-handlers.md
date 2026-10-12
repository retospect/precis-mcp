---
status: draft
pillar: platform
---

# `_pending_*` request state on singleton handlers — cross-request leak/race exposure

## Scope and corrected premise

This item now covers **todo and semanticscholar only**. Memory's request
state is independently addressed by graph stack `ed4af4c8a` + `b320514d5`;
the original memory claim is historical, not work to duplicate here.

The old serial-stdio premise is false in the current source.
`src/precis/server.py::_offload_sync` runs tools through
`anyio.to_thread.run_sync`; `_DEFAULT_TOOL_CONCURRENCY` is four. The
semaphore limits total concurrency, not access to each handler.
`src/precis/tools/core.py::_get_runtime` shares a process runtime;
`src/precis/dispatch.py::Hub.handler_for` returns the registered instance;
`src/precis/runtime/dispatch.py::DispatchMixin._invoke_handler` invokes
the bound method without a handler mutex. Web additionally shares its
app runtime via `src/precis_web/deps.py::get_runtime` and offloads
`dispatch_with_status` via `await_dispatch`. Independent CLI processes
do not share handler instances. No production race incident is claimed.

## Confirmed consumers

| Handler | Request slots | Consumers / impact |
|---|---|---|
| `TodoHandler.put` | `_pending_parent_id`, `_pending_meta`, `_pending_prio`, `_pending_body` | `_create` writes another request's fields to the DB; `_render_create_ack` can misreport parent/priority |
| `TodoHandler.put` | `_stamped_default_tier` | Set earlier than the pending slots; create receipt announces billed auto-run state. Shared-state exposure confirmed statically; not exercised by the current repro |
| `SemanticScholarHandler.get` | `_pending_exclude` | `_render_topic_search` resolves the wrong container/slug exclusions; request completion clears another call's state |
| `SemanticScholarHandler.get` | `_pending_complete` | `_canonical_key` selects the wrong `author:<id>[:complete]` cache key; on misses this also selects the wrong fetch/pagination policy |

Held/stub flags themselves come from `_corpus_flags_bulk`, not from the
pending exclusion list; the original "wrong held flags" claim conflated
filtering with classification. No mutation of cached paper data is
needed for the exclusion defect.

## Reproduction dossier

At local base `af08acb9a`, `tests/test_singleton_request_state_repro.py`
pauses A at a real base-class handoff after staging request fields,
allows B to stage its fields, completes A (including its `finally`),
then resumes B. Events and bounded futures enforce order without sleeps.
Test DB only; external fetches mocked; no prod writes or external calls.

`scripts/test tests/test_singleton_request_state_repro.py -n0 -q -rx -s`
exited 0 with **three strict XFAILs** against isolation assertions:

- Todo: A requested parent A / prio 2 / sonnet / details-a; stored
  parent B / prio 8 / haiku / details-b and acknowledged parent B.
  B requested parent B / prio 8 / haiku / details-b; stored no parent,
  NULL prio, empty meta and no body; receipt omitted parent/priority.
- S2 cached topic: A excluded Hit A but showed Hit A and dropped Hit B.
  B excluded Hit B but showed both. Real exclusion resolver and topic
  renderer ran; corpus flags supplied deterministically. Both calls
  were cache hits (fetch count unchanged).
- S2 cached author: A requested `complete=False` but read
  `author:111:complete`; B requested `complete=True` but read
  `author:222`. Both variants prewarmed; fetch count unchanged.

These are failing correctness assertions with expected-failure markers,
not a passing regression gate. The schedule can occur on the current
MCP/web paths; it is not a measured production frequency.

## Proposed minimal fix (review required)

Keep the shared handler/store APIs. Replace subclass-only instance slots
with one immutable request-context record per handler kind, held in a
`ContextVar`, with `set`/token-`reset` around the whole request. Todo's
record includes parent, meta, priority, body **and** default-tier receipt
flag; calculate that flag locally before setting context. S2's record
includes exclusions and completeness; key calculation and render must
read the same context. Copy mutable request containers on capture.

Explicit local parameters are preferable where the private call chain
already accepts them (the memory fix chose this); the todo numeric-ref
and S2 cache hooks currently lack these parameters. A per-kind context
avoids expanding every base-hook signature or serializing slow fetches.
Token reset also restores outer state under re-entry and exceptions;
clearing to `None` does not. No global handler/store redesign or memory
changes belong in this item.

## Meaningful regression plan

Remove XFAIL markers only with the fix; pin both requests' stored todo
parent/meta/priority/body, receipts and S2 survivor lists/cache identities.
Add the reversed completion order, default-tier receipt arming, exception
cleanup, and nested re-entry restoration. For S2, also cover author
cache misses: mocked API pagination must correspond to each call's
completeness flag and populate distinct intended cache rows. Exercise a
pair through the actual `_offload_sync` wrapper with a shared runtime so
the concurrency seam is pinned as well as the handler boundary.

Focused tests through `scripts/test`; coordinator schedules the full
gate. This assignment stops at reproduction/spec, pending review.
