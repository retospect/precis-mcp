# Drive presenter completeness + kind-taxonomy audit

Drive shipped; the presenter contract totality + kind cleanup remain.

- @abstractmethod promotion: ItemPresenter has a generic default for every
  method; the check-time-totality acceptance criterion needs a dedicated
  presenter per source/artifact kind (~40) — a separate, larger pass.
- Kind-taxonomy audit (coupled — do alongside, both touch every kind's
  declaration): reconcile role/corpus_role drift (datasheet, pres); collapse
  near-dup kinds (perplexity-*/websearch/web/wikipedia; calc/math/oracle);
  rewrite the precis-*-help skills. No-legacy-alias license granted.
- Slice 4: "write a document from this view" — a tailored filter is a
  serialized query → mint an authoring job scoped to exactly those refs.
Owner `src/precis_web/routes/drive.py`, `src/precis_web/item_view.py`.
