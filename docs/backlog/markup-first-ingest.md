# markup-first-ingest

## Residuals (from OPEN-ITEMS)

Decide the PDF-race before flipping PRECIS_FETCH_MARKUP (still default-off):
per-stub the markup pass runs first (best-effort, swallows its own errors),
then the PDF cascade runs unconditionally after — which body wins when both
succeed is undecided. Decide before enabling on any host. Owner
`src/precis/workers/fetch_oa.py::_run_markup_cascade` /
`_markup_fetch_enabled`.
