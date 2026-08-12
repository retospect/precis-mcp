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
(cached property), 76 transitional delegations on `Store`,
`tests/test_store_drafts_facade.py` pins facade↔sub-store signature
parity, mixin guard rescoped to the remaining 21 mixins.

REMAINING (one domain per ship):
- Migrate draft call sites to `store.drafts.*`, deleting each
  delegation when its callers are gone (interleave with the
  [codereview-store-typing-seam] long tail — same files).
- Next carves, same pattern: refs (`_refs_ops`, 108KB) and blocks
  (`_blocks_ops`, 110KB) are the big ones; then tags/links/cache/…
  Measure each mixin's outbound `self.*` cross-domain calls first (the
  drafts carve needed only insert_ref/resolve_handle/add_link on host).
- Endgame: delete the delegation blocks, shrink `Store` to core +
  sub-store properties + the small cross-cutting ops it already owns.
