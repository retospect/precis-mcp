# codereview: Store decomposition — mixin facade → composed sub-stores

`store/store.py::Store` still composes 25 direct domain mixins in one flat
namespace. This already caused one shipped MRO-shadowing incident (documented
at `store/_refs_ops.py` — a runtime `add_tag` stub shadowed TagsMixin's real
one); `tests/test_store_mixin_guard.py` now catches duplicate names, but the
facade remains file-splitting-by-inheritance rather than domain namespacing.

Design (agreed): composition with a delegating facade, migrated incrementally —
`StoreCore` holds pool/tx lifecycle; domain sub-stores hold a core + a `host`
back-reference for their few cross-domain calls, exposed as cached properties.
Inheritance alone gives sharing, not namespacing, and the flat-namespace
collision class is the defect.

Steps 1–2 SHIPPED: `StoreCore` extracted and drafts carved as
`store.drafts`; all source/test call sites migrated and transitional flat
`Store` delegations deleted. `tests/test_store_drafts_facade.py` pins the
composed shape.

Step 3 SHIPPED under the then-current `blocks` name; the vocabulary has since
converged on chunks: `_chunks_ops.py::ChunkStore` is composed as
`store.chunks`, all call sites migrated, and flat delegations deleted.
`tests/test_store_chunks_facade.py` pins the final shape. The measurement that
picked chunks over refs still governs the next decision: refs is the hottest,
most cross-domain surface, so it needs a design pass before any carve.

REMAINING (one domain per ship):
- **DraftReviewStore delegation finish** — Transitional delegations documented
  in `store/_draft_ops.py` still expose each review op under two names:
  `store.drafts.X` and `store.drafts.review.X`. The flat `store.X` layer is
  already gone. Follow the finished drafts/chunks carve pattern: confirm source
  call sites use `store.drafts.review.*`, delete the `DraftStore` delegations,
  and flip `tests/test_store_drafts_facade.py` from parity checks to an inverse
  guard. (Related: `codereview-handler-size-cleanups.md` owns the review-surface
  call-site migration residual.)
- **Refs full pass — LESSER PRIORITY (Reto, 2026-08-15).** Design pass
  first (carve vs bless the flat names permanently — see measurement
  above; the design pass is cheap, ~150–400k tokens). If "carve": the
  full migration is the biggest carve yet, est. ~10–15M tokens across
  3–4 ship cycles — sed handles the bulk renames, but the cost tail is
  the hand-rolled fakes/monkeypatches around the hottest methods
  (`get_ref`/`insert_ref`/`add_tag`, 20 inbound packages). If "bless":
  near-zero — a paragraph here + a guard test. Optional ~50k scouting
  pass (count distinct fake classes + monkeypatch sites touching refs
  methods in tests) pins the tail before deciding. Do not start ahead
  of higher-priority work.
- Then tags/links/cache/… (withheld — not yet approved).
- Endgame (per carve, as done for drafts): delete the delegation
  block once call sites migrate; `Store` ends as core + sub-store
  properties + the small cross-cutting ops it already owns.
