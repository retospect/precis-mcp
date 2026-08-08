# structure_import isn't atomic end-to-end

`structure_save` commits its own tx, then a second tx writes
ref_identifiers + the external run. A crash between the two leaves a ref
with no identifier row; on retry structure_save finds the orphan by its
deterministic slug (created=False), so the `if created`-guarded identifier
insert never fires again → the (dataset, config_id) lookup permanently
misses. Fold create + identifier + run into one transaction, or make the
identifier insert unconditional/idempotent. Sibling hygiene: escape/allowlist
the f-string-interpolated GraphQL filter values in
`structure/importers/catalysis_hub.py::fetch_config`. Owner
`src/precis/store/_structure_ops.py::structure_import`. Mechanical.
