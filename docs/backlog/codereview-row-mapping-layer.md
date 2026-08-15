# codereview: DB row mapping — positional-mapper residuals

The three worst flows are shipped: draft review surface returns
frozen dataclasses (`_draft_ops.py::ReviewableChunk`/`ChunkReviewEntry`/
`DraftReviewRow`, consumed via attributes through `quest/review_fanout`
→ `handlers/_review_view` → `precis_web/routes/drafts`); component +
structure ops read via psycopg `dict_row` with TypedDict rows
(`_component_ops.py::ComponentValueRow` union family,
`_structure_ops.py::StructRunRow`/`StructForcesRow`); pathway payloads
typed (`precis_pathway/types.py::PathwayArtifact`/`NetworkTopology`/
`SeedPartialResult`/…). Pattern for new code: named column access
(`dict_row` + `cast` to a TypedDict, or explicit named unpack) — never
positional indexing over a long SELECT list.

REMAINING (convert opportunistically, when the file is next touched):

- `store/_mappers.py` — the original refs/blocks/links mappers are
  still positional over up to 30 columns with defensive `len(row) > N`
  probing, and no pool-level `row_factory` exists (`store/pool.py`).
  Converting is a big, mechanical, test-heavy diff; do it per-mapper
  when a mapper's SELECT next changes, not as a big bang.
- `store/_material_ops.py` — identical `_row_to_property`/
  `_row_to_value` positional mappers + the same 5-way tagged-union
  values shape as component ops; same `dict_row` + TypedDict recipe
  applies directly.
- `store/_structure_ops.py::structure_load` — atom/bond/measure rows
  use positional multi-variable unpacking (self-documenting but
  drift-prone); larger Scene-construction diff, low urgency.
- `store/_pcb_ops.py` — dict-returning sigs never audited in detail.
