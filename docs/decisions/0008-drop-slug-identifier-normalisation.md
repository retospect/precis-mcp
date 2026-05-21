# ADR 0008 — Drop `slug`; normalise all identifiers into `ref_identifiers`

- **Status**: accepted (2026-05-21)
- **Deciders**: Reto + agent
- **Supersedes**:
  - ADR 0006 §"Decision" — the tri-identifier scheme is reduced to two
    user-visible identifiers (`pub_id`, `cite_key`); the `slug`
    section and the "`refs` carries all three columns" statement no
    longer hold.

## Context

ADR 0006 specified three user-visible identifiers — `pub_id`,
`cite_key`, `slug` — each materialised as a UNIQUE column on
`refs` *and* duplicated as a row in `ref_identifiers`. During the
schema-v2 lock review the design was challenged on two fronts:

1. **Is a third human-readable handle pulling its weight?**
   `cite_key` (`miller23a`) is the LaTeX-friendly form; `slug`
   (`miller2023dopamine`) is the long descriptive form used for
   filenames and human display. The two overlap in audience: both
   are "human-readable handles you might type". The descriptive
   topic word in `slug` is a nice touch but not essential —
   `miller23a.pdf` is unambiguous once the corpus is consistent.

2. **Why do primary identifiers live in two tables?**
   `refs.pub_id` and `ref_identifiers(id_kind='pub_id', id_value=…)`
   say the same thing. The duplication serves "single uniform
   lookup across all handle kinds" but at the cost of
   write amplification (one INSERT into `refs` triggers two-to-three
   INSERTs into `ref_identifiers`) and a sync invariant that has to
   be enforced by code outside the schema.

A cleaner data model removes both redundancies. Lookups gain a
single index-probe through `ref_identifiers`; ergonomic access to
the "primary handles as columns" is restored via a view.

## Decision

### 1. Drop `slug`

`slug` is removed from the identifier set. `cite_key` becomes the
canonical human-readable handle, used wherever `slug` was previously
used:

| Old use of `slug` | New approach |
|---|---|
| File names in `~/work/corpus/` | `<cite_key>.pdf` |
| `precis show <handle>` resolves slug | `precis show <handle>` resolves `cite_key` (and `pub_id`, DOI, …) |
| URL paths `/ref/<slug>` | `/ref/<cite_key>` |
| Citations in agent text | Already `pub_id`; unchanged |

The `cite_key` algorithm (firstauthor + 2-digit year + lowercase
letter suffix) from ADR 0006 stands.

### 2. Identifier normalisation

`refs` carries **no** identifier columns. All identifiers
(`pub_id`, `cite_key`, `paper_id` legacy, plus external aliases like
`doi`, `arxiv`, `pdf_sha256`) live as rows in `ref_identifiers`.
The schema becomes:

```sql
CREATE TABLE refs (
  ref_id     BIGSERIAL PRIMARY KEY,
  kind       TEXT NOT NULL REFERENCES kinds(slug),
  -- ... operational columns: title, authors, year, provider, …
  -- NO pub_id, NO cite_key, NO paper_id, NO slug
);

CREATE TABLE ref_identifiers (
  id_kind    TEXT NOT NULL,    -- 'pub_id' | 'cite_key' | 'paper_id'
                              --  | 'doi'   | 'arxiv'  | 's2' | 'pubmed'
                              --  | 'openalex' | 'pdf_sha256' | 'content_hash'
  id_value   TEXT NOT NULL,
  ref_id     BIGINT NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
  source     TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (id_kind, id_value)
);
CREATE INDEX ref_identifiers_ref_id_idx ON ref_identifiers (ref_id);
```

`pub_id`, `cite_key`, and `paper_id` are not UNIQUE columns on
`refs` — uniqueness for each comes from the
`(id_kind, id_value)` primary key on `ref_identifiers`. Each ref
gets two `ref_identifiers` rows at insert time (`pub_id`,
`cite_key`); legacy refs (acatome migration) get a third
(`paper_id`).

### 3. Ergonomic view: `v_refs`

To avoid forcing every application query to JOIN through
`ref_identifiers`, expose the primary handles as virtual columns
via a view:

```sql
CREATE VIEW v_refs AS
SELECT r.*,
       (SELECT id_value FROM ref_identifiers
         WHERE ref_id = r.ref_id AND id_kind = 'pub_id')   AS pub_id,
       (SELECT id_value FROM ref_identifiers
         WHERE ref_id = r.ref_id AND id_kind = 'cite_key') AS cite_key,
       (SELECT id_value FROM ref_identifiers
         WHERE ref_id = r.ref_id AND id_kind = 'paper_id') AS paper_id
FROM refs r;
```

Application code SELECTs from `v_refs` rather than `refs` for
display and search. The three correlated subqueries are cheap
(each is an indexed lookup on `(id_kind, ref_id)`).

For "find ref by any handle":

```sql
SELECT r.* FROM refs r
  JOIN ref_identifiers ri USING (ref_id)
  WHERE ri.id_value = $1;
-- optional: AND ri.id_kind = 'cite_key'  (strict kind match)
```

## Consequences

### Positive

- **One canonical lookup path** for identifiers. No "is this query
  hitting the columns-on-refs path or the alias-table path?".
- **Write amplification reduced**: one INSERT into `refs` + two
  INSERTs into `ref_identifiers` (down from three INSERTs *plus*
  three column writes).
- **Cross-kind uniqueness is free**: the
  `(id_kind, id_value)` PK guarantees no two refs share a
  `cite_key`, no two share a `pub_id`, no two share a DOI, etc.
- **Fewer mutable surfaces**. The schema no longer admits a
  state where `refs.pub_id` and the `pub_id` row in
  `ref_identifiers` disagree.
- **One fewer human-handle to bikeshed**: dropping `slug` removes
  the entire "what topic word goes in the slug?" question.
- **Aesthetic / docs simplification**: papers are referred to by
  `cite_key` everywhere humans look (filenames, LaTeX cites,
  search aliases) and by `pub_id` everywhere machines look.

### Negative

- **Identifier columns require JOIN or view access.** Code that
  previously did `SELECT pub_id FROM refs` now reads `v_refs`
  or JOINs `ref_identifiers`. Trivial change; tiny per-query cost.
- **The `v_refs` view runs three correlated subqueries per row.**
  For hot bulk reads (e.g. `SELECT … FROM v_refs LIMIT 10000`),
  hand-rewrite as a LATERAL JOIN if measured to matter. Default
  view is fine for typical queries.
- **One-time inbox rename.** Existing PDFs named
  `<slug>.pdf` must be renamed to `<cite_key>.pdf`. Greenfield
  re-ingest produces the new names directly; no manual rename
  needed if we re-ingest from sources.
- **`paper_id` legacy column dropped from `refs`** too — it
  becomes an `id_kind` in `ref_identifiers`. Old acatome data is
  migrated at re-ingest by writing
  `(id_kind='paper_id', id_value='<legacy>', ref_id=…)`.

### Migration

Greenfield (per ADR 0005): no separate migration. The change is a
rewrite of `0001_initial.sql` before B1 lands. Behavioural surface:

- `refs.pub_id`, `refs.cite_key`, `refs.slug`, `refs.paper_id` columns:
  **removed**.
- `ref_identifiers` accepts `pub_id`, `cite_key`, `paper_id` as
  `id_kind` values: **added**.
- `v_refs` view: **added**.
- Application code paths using `SELECT … FROM refs`: switch to
  `v_refs` if they need identifier columns; keep using `refs`
  otherwise.

## Open questions

- **Should `slug` come back as an opt-in display alias?** A user
  could attach a descriptive slug as a `ref_identifiers` row with
  `id_kind='slug'` purely for display. We don't seed any slug rows;
  if a real need surfaces, the schema already supports it without
  changes.
- **`v_refs` as a MATERIALIZED view?** A materialised view would
  speed bulk reads at the cost of a REFRESH after every relevant
  INSERT/UPDATE. We start with a regular view; revisit only if
  hot-path measurements demand it.

## Workflow note

This ADR was authored during the schema-v2 PUML lock review on
2026-05-21. See `docs/design/schema-v2.puml` and
`docs/design/schema-v2.svg` for the visual.
