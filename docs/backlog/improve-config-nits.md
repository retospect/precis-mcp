# Config + one-line hygiene batch

- ruff `target-version = "py311"` vs `requires-python >= 3.12` (some UP
  rules never fire).
- Decide coverage-measurement posture (none exists; a deliberate "no"
  is fine — document it in docs/conventions/testing.md).
- Stale `handlers/_patent_ingest.py` docstring still sketches inline
  `fill_embeddings` (invites a bad "restore" — workers own embedding).
- `tools/core.py::set_runtime` missing its param annotation.
- Bare cursor in `store/_identifiers_ops.py::insert_ref_identifiers`;
  `SELECT *` in `pcb/catalog.py`; `routes/cad.py` export filename should
  key on `ref.id` not the raw slug fallback; trust-boundary comment on
  `cli/heartbeat.py`'s `shell=True`; comment near `store/pool.py` for
  the five deliberate bare `psycopg.connect()` lock-holder sites.
Mechanical, one tidy pass.
