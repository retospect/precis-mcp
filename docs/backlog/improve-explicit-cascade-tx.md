# Explicit transactions on DELETE+INSERT cascades

`workers/paper_glossary.py`, `store/_blocks_ops.py::
_replace_card_combined` / `upsert_card_combined` rely on pool
implicit-commit semantics for the append-only-chunks invariant — wrap
in `with conn.transaction():` for auditability; no behavior change.
Mechanical.
