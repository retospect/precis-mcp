# codereview: handler/mixin size cleanups — residuals

Done (shipped): DraftStore review surface lifted to `DraftReviewStore`
(`store.drafts.review.*`, 12 methods, transitional delegations kept);
draft-lint hint generators extracted to `handlers/_draft_lint.py`;
finding.py's store-only state machines extracted to
`handlers/_finding_acquire.py`/`_finding_edit.py`/`_finding_evidence.py`
(FindingHandler stays on NumericRefHandler — it genuinely uses the
shared CRUD contract); perplexity tiers collapsed to a `_SonarTier`
config dataclass + `cost_per_call_usd` declared on `CacheBackedHandler`.

REMAINING:

- Migrate the review-surface call sites (`handlers/draft.py`,
  `quest/review_fanout.py`, `handlers/_review_view.py`,
  `precis_web/routes/drafts.py`) to `store.drafts.review.*` and delete
  the 12 transitional delegations on DraftStore
  (`tests/test_store_drafts_facade.py` pins their signature parity
  meanwhile).
- `precis_web/item_view.py::ItemPresenter` — 10-method hierarchy with
  one subclass overriding one method; watch-item only, keep an eye on
  whether it earns its ceremony (module docstring already tracks
  promotion honestly).
