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

REMAINING (one domain per ship):
- Next carves, same pattern: refs (`_refs_ops`, 108KB) and blocks
  (`_blocks_ops`, 110KB) are the big ones; then tags/links/cache/…
  Measure each mixin's outbound `self.*` cross-domain calls first (the
  drafts carve needed only insert_ref/resolve_handle/add_link on host).
- Endgame (per carve, as done for drafts): delete the delegation
  block once call sites migrate; `Store` ends as core + sub-store
  properties + the small cross-cutting ops it already owns.
