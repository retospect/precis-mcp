# codereview: `store: Any` epidemic — type the seam

`store: Any` at 651 call sites (vs 687 typed) — origin is
`dispatch.py::Hub.store` declared `Any`, plus the 23-mixin Store being hard
to name. Downstream cost: 560 defensive `getattr` calls against fully-typed
frozen dataclasses (e.g. `precis_web/routes/refs.py::_row` null-guarding
non-optional `datetime` fields). Four modules independently hand-rolled
narrow `_StoreLike` Protocols (`diagram/turn.py`, `diagram/context.py`,
`diagram/doc_context.py`, `handlers/_patent_cql.py::_StoreProto`).

Fix: central `precis/store/protocols.py` with narrow named role-interfaces;
consolidate the four ad-hoc Protocols; type `Hub.store` behind
`TYPE_CHECKING`; convert `store: Any` sites to `Store` or the narrowest
role Protocol, deleting dead `getattr` fallbacks as they go. Also retire
the two runtime stubs in `store/_refs_ops.py` / `_cache_ops.py` that
survive on MRO ordering luck. Related: [codereview-store-decomposition].
