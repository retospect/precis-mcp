---
status: idea
prio: low
---

# Review the 60 allowlisted jsonb columns

`tests/test_schema_design.py::JSONB_COLUMNS` froze the inventory
(2026-08-24) and gates new columns, but the existing 60 were allowlisted
wholesale, never judged. One low-effort review pass, column by column,
with three possible verdicts — this is a judgment sweep, not a
conversion campaign:

- **keep** — genuinely open-ended payload (`meta`, `payload`,
  transcripts, geometry blobs). Expected majority.
- **promote** — code reads fixed keys (`->>` / `#>>` in SQL, `.get()`
  on a known key in Python) or filters on a jsonb path: those keys
  deserve real columns. Each promotion is its own forward migration +
  backfill; file separately, don't batch.
- **index/constrain** — stays jsonb but is queried: add a GIN index or
  a CHECK on required keys.

Mechanical prep an agent can do: for each column, grep SQL + handlers
for accessors on it and count distinct keys in prod (`scripts/prod-psql`
with `jsonb_object_keys`, read-only). Columns with exactly one accessor
pattern and a stable key set are the promote candidates; start with
`refs.authors`, `struct_runs.params`, `llm_call_log.features`.

Test: review notes land per column (keep/promote/index) in this file or
successor items; JSONB_COLUMNS entries only shrink via shipped
promotions.
