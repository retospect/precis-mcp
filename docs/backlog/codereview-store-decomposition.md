# codereview: Store decomposition — 23 mixins → composed sub-stores

`store/store.py::Store` composes 23 mixins, 344 transitive methods, one
flat namespace. Already caused one shipped MRO-shadowing incident
(documented at `store/_refs_ops.py` — a runtime `add_tag` stub shadowed
TagsMixin's real one; guard test added, but two runtime stubs still rely on
MRO ordering). Mixins fake their contract with bare `pool: Any`-style
annotations. This is file-splitting-by-inheritance, and it's why nobody can
usefully annotate `store:` (see [codereview-store-typing-seam]).

Direction under discussion (Reto asked whether "clever inheritance and
helpers" can get us the full decomposition): composition with a delegating
facade, migrated incrementally —

- `_StoreCore`: pool/tx/connection lifecycle, the only stateful part.
- Domain sub-stores (`DraftStore`, `RefStore`, …) each holding `core`,
  exposed as cached properties: `store.drafts.add_chunks(...)`.
- The flat 344-method surface survives as thin explicit delegations (or a
  `__getattr__` forwarder during migration) so the ~650 call sites move
  one domain at a time; delete each delegation when its callers are gone.
- Inheritance alone can't do this: it gives sharing, not namespacing, and
  the flat-namespace collision class is the defect.

Status: design agreed in principle 2026-08-11, sequencing after the typing
seam; dedicated session (multi-day, touches every package).
