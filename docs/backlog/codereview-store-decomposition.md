# codereview: Store decomposition — 23 mixins → composed sub-stores

`store/store.py::Store` composes 23 mixins, 344 transitive methods, one
flat namespace. Already caused one shipped MRO-shadowing incident
(documented at `store/_refs_ops.py` — a runtime `add_tag` stub shadowed
TagsMixin's real one; guard test added, but two runtime stubs still rely on
MRO ordering). Mixins fake their contract with bare `pool: Any`-style
annotations. This is file-splitting-by-inheritance, and it's why nobody can
usefully annotate `store:` (see [codereview-store-typing-seam]).

Design (agreed): composition with a delegating facade, migrated
incrementally — `StoreCore` holds pool/tx lifecycle (the only stateful
part); domain sub-stores hold a core + a `host` back-ref for their few
cross-domain calls, exposed as cached properties; the flat method
surface survives as thin typed delegations, deleted per-domain once
call sites migrate. Inheritance alone can't do this: it gives sharing,
not namespacing, and the flat-namespace collision class is the defect.

Step 1 SHIPPED: `store/core.py::StoreCore` extracted; drafts carved —
`_draft_ops.py::DraftStore(_AbbrevMixin)` composed as `store.drafts`
(cached property), mixin guard rescoped to the remaining 21 mixins.
Step 2 SHIPPED (all batches): every src + test call site now goes
through `store.drafts.*`; the 76 transitional flat delegations are
DELETED from `Store`, and `tests/test_store_drafts_facade.py` now pins
the inverse (no flat draft name may reappear on `Store`). Test-double
recipe that worked: hand-rolled fakes get a one-line
`drafts = property(lambda self: self)`; `SimpleNamespace` fakes get
`store.drafts = store`; tests monkeypatching a flat facade name on a
REAL store must patch `store.drafts` instead (the flat patch silently
stops intercepting).

Step 3a SHIPPED: blocks carved — `_blocks_ops.py::BlockStore`
composed as `store.blocks`, transitional flat delegations on `Store`,
all src call sites migrated (244 sites, 87 files; store-internal
consumers too: `_cache_ops` insert_blocks, and
`_replace_card_combined` in cad/pcb/structure now via
`self.blocks.*`). Measurement that picked blocks over refs: blocks =
41 methods, 243 src + 429 test sites, 0 outbound cross-domain, 2
inbound; refs = 48 methods, 761 src + 2,477 test sites, 20 inbound —
and `get_ref`/`insert_ref`/`add_tag` are the most-called methods in
the codebase, so refs is core-adjacent and needs its own design pass
(maybe its flat names *stay* on the facade permanently).

REMAINING (one domain per ship):
- Step 3b: migrate the 429 test call sites to `store.blocks.*`, delete
  the delegation block, extend the facade test (same recipe as
  `tests/test_store_drafts_facade.py`; test-double recipe above).
- Then: refs design pass (see measurement), then tags/links/cache/…
- Endgame (per carve, as done for drafts): delete the delegation
  block once call sites migrate; `Store` ends as core + sub-store
  properties + the small cross-cutting ops it already owns.
