# codereview: DB row mapping — positional, no row_factory, abandoned in newer ops

`store/_mappers.py` maps rows → frozen dataclasses for refs/blocks/links,
but every mapper is positional over up to 30 columns and **no
`row_factory` is configured anywhere** (`store/pool.py`) — a SELECT-list
drift silently mis-assigns fields (defensive `len(row) > N` probing in
`_mappers.py` shows the bug class is known). Newer ops modules skip the
layer entirely: `_draft_ops.py` (49 dict-returning sigs, 0 dataclasses),
`_pcb_ops.py`, `_component_ops.py` (hand-rolls three dict mappers),
`_structure_ops.py` (`dict(zip(cols, row))`).

Worst untyped flows to model first (TypedDict or dataclass, no runtime
change): draft review chunks
(`_draft_ops.py::reviewable_chunks`/`review_status_for_draft` →
`quest/review_fanout.py` → `handlers/_review_view.py` →
`precis_web/smartdraft.py`, three different dict shapes), component
spec/values (5-way tagged union as five nullable dict keys), pathway
payloads (`precis_pathway/runner.py` — 0 dataclasses in the package).

Fix: named-access row factory (psycopg `dict_row`/`class_row` or
namedtuple) on new/refactored mappers, dataclass returns for the draft
review surface, TypedDicts for the three flows.
