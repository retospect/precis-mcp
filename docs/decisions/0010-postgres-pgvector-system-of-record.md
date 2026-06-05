# ADR 0010 — Postgres + pgvector as the system of record

- **Status**: accepted (2026-05-21)
- **Deciders**: Reto + agent
- **Supersedes**: nothing (codifies an implicit choice)

## Context

Before writing `0001_initial.sql` the storage layer was challenged:
*should this be Postgres or a NoSQL store?* On the surface the
dataset (papers, notes, patents, conversations) looks
document-shaped; the question is fair.

The argument *for* NoSQL was concrete: **bidirectional
laptop ↔ cluster sync**. Document stores like CouchDB / PouchDB
ship multi-master replication as a built-in feature; a Postgres
pair needs a read-replica or a periodic dump/restore for the
same effect.

The argument *against* is the locked schema. Every clause in
`storage-v2.md` (and visualised in `schema-v2.puml`) leans on
something RDBMS-native:

| Schema element                                                       | RDBMS feature             |
| -------------------------------------------------------------------- | ------------------------- |
| `ref_identifiers.ref_id → refs ON DELETE CASCADE`                    | Foreign keys              |
| `PRIMARY KEY (id_kind, id_value)`                                    | Composite uniqueness      |
| `UNIQUE (is_default) WHERE is_default = TRUE`                        | Partial unique indexes    |
| `tsv tsvector GENERATED ALWAYS AS …`                                 | Generated columns + FTS   |
| `chunk_embeddings.vector vector(1024)` + HNSW                        | `pgvector` extension      |
| `pdf_pages int4range`                                                | Range types               |
| `v_refs`, `v_ref_tags_all`, `v_chunk_tags_all`                       | Views                     |
| Derived queue: `LEFT JOIN … WHERE x IS NULL FOR UPDATE SKIP LOCKED`  | MVCC + row locking        |
| Atomic `INSERT refs; INSERT ref_identifiers; INSERT chunks; …`       | Single-DB transactions    |

Re-expressing the schema in a document store would push every
one of these into application code: integrity checks, joins,
two-system writes (Postgres + Qdrant for vectors, Postgres +
Elasticsearch for FTS, …). The savings on the sync front are
smaller than the operational cost of running three databases
instead of one.

Write concurrency is also low and bounded — single watcher,
single worker, small fleet of MCP clients. There is no throughput
case that would push us past one Postgres node.

The sync requirement, examined honestly, is *not* "both sides
mutate independently and converge". It is "I want to query prod
data from my laptop". That is solved by Tailscale + libpq
without inventing CRDTs.

## Decision

**Postgres 16 + pgvector is the system of record for all
precis-mcp data**, including:

- Relational hub (`refs`, `ref_identifiers`, `pdfs`, `chunks`, `links`)
- Vectors (`chunk_embeddings.vector` via `pgvector`)
- Full-text search (`chunks.tsv` via generated `tsvector` + GIN)
- Controlled vocabularies and model registries
- The derived job queue (no separate queue service)

Concretely, in `0001_initial.sql`:

- `DO $$ … RAISE EXCEPTION` guard that aborts on Postgres < 16.
- `CREATE EXTENSION IF NOT EXISTS vector;`
- `CREATE EXTENSION IF NOT EXISTS pg_trgm;`
- HNSW indexes are built per active embedder; default is
  `(m=16, ef_construction=64)` with `vector_cosine_ops`.
- `pg_stat_statements` is *recommended* in `postgresql.conf` for
  observability but not required by the schema — the migration
  does not enable it.

The deployment topology is:

```
┌─────────────────────────────────────────┐
│  Cluster — prod, always-on               │
│  Postgres 16 + pgvector                  │
│  Backups: pg_basebackup + WAL → S3/B2    │
└─────────────────────────────────────────┘
                 ↑
                 │  Tailscale (libpq / TLS)
                 ↓
┌─────────────────────────────────────────┐
│  Laptop — dev                            │
│  Postgres 16 + pgvector (containerized)  │
│  precis_dev   ← local, fast iteration    │
│  precis_prod  ← read-mostly query target │
└─────────────────────────────────────────┘
```

No multi-master. No conflict resolution. Offline laptop work
uses the local `precis_dev`. "Working copy of prod" is
`pg_dump prod | pg_restore laptop_dev` over Tailscale —
acceptable at our data volume (<10 GB for the foreseeable
future).

## Consequences

### Positive

- **One transactional system.** `precis add` is one
  `BEGIN … COMMIT`; no outbox patterns or two-system drift.
- **Joins are first-class.** `refs JOIN chunks JOIN
  chunk_embeddings ORDER BY vector <-> $1` in a single round
  trip; the alternative is two services and a network hop per
  query.
- **Operational surface stays small.** One DB to back up,
  monitor, upgrade. Container image already exists
  (`pgvector/pgvector:pg16`).
- **Schema validity is enforced at the storage layer.** FK
  cascades, CHECK constraints, partial uniques catch bugs that
  document stores defer to runtime.

### Negative

- **No native bidirectional sync.** We accept Tailscale as the
  ergonomic substitute. Re-open this decision if multi-master
  becomes a real need (it has not, to date).
- **Vector scale ceiling is pgvector's ceiling.** Comfortable to
  ~100 M vectors at 1024-dim on a single node with HNSW. Beyond
  that, the escape hatch is adding Qdrant *as a side index*
  while keeping the relational truth in Postgres.
- **pgvector HNSW indexes are large.** Plan ~4–8 GB per million
  vectors at 1024-dim. SSD with headroom on the cluster node.

### Neutral

- **Backups are physical-replica-friendly.** `pg_basebackup` +
  WAL archiving to object storage is the canonical pattern; we
  adopt it on first prod deployment, not now.
- **Read replicas are cheap.** If laptop performance over
  Tailscale becomes annoying for ad-hoc queries, a streaming
  replica on the laptop is a one-command addition.

## Open questions

- **First prod deployment**: cluster machine, OS image, init
  scripts. Not part of this ADR; lands when prod is provisioned.
- **WAL archiving target**: B2 vs S3 vs self-hosted MinIO. Defer
  until prod exists.
- **`vector` partitioning** for cross-dim embedders: deferred to
  storage-v2.md "Open questions"; the schema permits it but the
  initial migration ships single-dim only.

## Alternatives considered

- **CouchDB / PouchDB** — built-in multi-master replication.
  Rejected: would force denormalising the schema, lose joins,
  and still need Qdrant/ES sidecars for vectors and FTS.
- **MongoDB** — flexible documents, mature ecosystem. Rejected:
  same denormalisation tax, no native vector search, no native
  FTS at the level of `to_tsvector`.
- **FoundationDB / Couchbase** — strongly-consistent NoSQL with
  RDBMS-ish features. Rejected: operational complexity not
  justified at our scale.
- **Postgres + Qdrant from day one** — separate the vector
  index. Rejected for v2: pgvector handles our volume; we can
  add Qdrant later if needed without re-architecting writes.
- **SQLite (per-host) + sync** — appealing for laptop offline
  work. Rejected: no pgvector equivalent (sqlite-vss is young),
  no GENERATED columns of the same expressiveness, and the
  single-writer constraint makes the worker pattern awkward.
