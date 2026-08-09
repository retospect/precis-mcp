# Query-shape cleanups

N+1 enrichment in `workers/classify.py::_enrich` +
`workers/axis_pass.py` (3 SELECTs/row → one LAG/LEAD window query);
per-project recursive CTE loop in `handlers/_todo_views.py` (one CTE +
GROUP BY); unbounded `/gripes` listing (`routes/gripes.py::_rows` — the
repo already learned this lesson in tags paging). Sonnet-shaped, each
independently shippable.
