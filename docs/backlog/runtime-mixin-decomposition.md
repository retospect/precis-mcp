---
status: idea
title: Replace PrecisRuntime cross-mixin stubs with explicit collaborators
prio: low
model: opus
---

# Replace PrecisRuntime cross-mixin stubs with explicit collaborators

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
