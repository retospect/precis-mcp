# ADR 0017 — Derived-queue family (`*_artifacts` substrate + `artifact_kinds` registry)

- **Status**: accepted (2026-05-30), **§4 (WorkerHandler refactor)
  superseded by [ADR 0018](./0018-persistent-discovery-layer.md)
  §"Worker"** (2026-05-31)
- **Deciders**: Reto + agent
- **Extends**: [ADR 0007 — Derived queue, no `block_jobs`
  table](./0007-derived-queue-no-block-jobs.md). 0007 established
  the pattern for chunk-level *typed* derived state
  (`chunk_embeddings`, `chunk_summaries`). This ADR generalises
  the pattern into a *family* covering untyped per-ref / per-link /
  per-pdf derived state and formalises a registry table.
- **Superseded portions**: §4 originally proposed parameterising
  `WorkerHandler` on five descriptors so chunk-level and ref-level
  workers shared one base class. ADR 0018 ships the first ref-level
  worker (`run_paper_segments_pass`) as a *sibling* function
  instead, and explicitly defers extracting a shared
  `RefWorkerHandler` base until a third ref-level user exists. The
  substrate tables (`ref_artifacts`, `artifact_kinds`) and the
  storage decisions in §§1–3 are unaffected.
- **Triggered by**:
  [`docs/design/finding-chase.md`](../design/finding-chase.md) —
  the citation-chase work needs a per-ref output relation for
  `chase_citation` / `resolve_citation:s2` artifacts and surfaced
  that the pattern was implicit, not documented. (The
  [provenance kind](../provenance-kind-plan.md) — shipped
  Phases 1–6 ahead of this ADR — owns retraction state through
  a synchronous tool, not a queue artifact; see
  §"Consequences > Positive" for the cross-reference.)

## Context

ADR 0007 specified the **derived-queue pattern** for chunks:

1. Target relation = `chunks`.
2. Output relation = `chunk_embeddings` (or `chunk_summaries`),
   keyed by `(chunk_id, model_name)`.
3. Pending work is *derived*:
   `chunks LEFT JOIN output WHERE output.chunk_id IS NULL`.
4. Failure marker rows (`status='failed'`) prevent poison-pill
   re-claims.

The pattern works. But three things were left implicit:

- **It applies to more than chunks.** Anything you can compute
  *for* a row — a per-ref retraction check, a per-link severity
  score, a per-pdf OCR pass — fits the same shape.
- **There's no registry.** Today the set of registered artifacts
  is the union of `embedders.name` and `summarizers.name` — two
  registries, model-typed, not handler-typed. Observability
  (`precis worker --status`) walks both. A third source of
  artifacts (the finding-chase work) would need a third
  registry under the current scheme.
- **`WorkerHandler` is target-locked.** Today's base class
  (`src/precis/workers/base.py:88-113`) hardcodes
  `FROM chunks c LEFT JOIN <output_table>`. Extending to refs
  means a sibling class with a copy-paste claim query. Untracked
  duplication.

The finding-chase design (`docs/design/finding-chase.md`) wants
all three of: (a) per-ref artifacts, (b) a registry it can hang
new artifacts on, (c) one WorkerHandler base that handles both
chunks and refs without forking the claim SQL.

## Decision

Formalise the pattern as a **family** with three orthogonal
choices: target kind, output shape (typed vs. untyped), and a
shared registry.

### 1. Per-target output tables, same shape

One table per target *kind*. Same shape per table, varying only
in PK type and FK reference:

```sql
CREATE TABLE <target>_artifacts (
    <target>_pk  <pk_type>   NOT NULL
        REFERENCES <target_table>(<pk>) ON DELETE CASCADE,
    artifact     TEXT        NOT NULL
        REFERENCES artifact_kinds(slug) ON UPDATE CASCADE,
    payload      JSONB,
    status       TEXT        NOT NULL DEFAULT 'ok'
        CHECK (status IN ('ok', 'failed')),
    attempts     INT         NOT NULL DEFAULT 1,
    last_error   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (<target>_pk, artifact)
);
CREATE INDEX <target>_artifacts_failed_idx
    ON <target>_artifacts (<target>_pk, artifact)
    WHERE status = 'failed';
```

Instantiations:

| Table | Target | PK column | Status |
| --- | --- | --- | --- |
| `chunk_artifacts` | `chunks` | `chunk_id` BIGINT | deferred — created when first untyped per-chunk artifact lands |
| `ref_artifacts` | `refs` | `ref_id` BIGINT | **created now** (with the finding-chase migration) |
| `link_artifacts` | `links` | `link_id` BIGINT | deferred |
| `pdf_artifacts` | `pdfs` | `pdf_sha256` CHAR(64) | deferred |

A new target-kind is one CREATE TABLE; never a column-add on an
existing table. This keeps each target's FK + cascade semantics
correct (which polymorphism would lose — see §"Rejected
alternatives" below).

### 2. Typed outputs stay where they are

The existing `chunk_embeddings` and `chunk_summaries` are
**special cases of the family** with typed result columns. They
do *not* migrate into `chunk_artifacts`. Reasons:

- `chunk_embeddings.vector` is `vector(1024)`, HNSW-indexable.
  Burying it in JSONB loses native ANN.
- `chunk_summaries.text` carries its own `prompt_hash`,
  `token_count` columns and participates in lexical surfaces.

Typed and untyped tables coexist under the same conceptual
substrate; the worker base class doesn't care which it's looking
at (see §3 below).

### 3. `artifact_kinds` — single registry for handler-typed artifacts

```sql
CREATE TABLE artifact_kinds (
    slug          TEXT PRIMARY KEY,
    target        TEXT NOT NULL
        CHECK (target IN ('chunk', 'ref', 'link', 'pdf')),
    storage       TEXT NOT NULL
        CHECK (storage IN ('typed', 'untyped')),
        -- 'typed'   → output table is dedicated (chunk_embeddings, chunk_summaries)
        -- 'untyped' → output table is <target>_artifacts, payload in JSONB
    output_table  TEXT NOT NULL,
    description   TEXT,
    deprecated_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO artifact_kinds (slug, target, storage, output_table, description) VALUES
    ('embed:bge-m3',              'chunk', 'typed',
        'chunk_embeddings', 'BGE-M3 1024-dim dense vector'),
    ('summarize:rake-lemma',      'chunk', 'typed',
        'chunk_summaries',  'RAKE keyword summary'),
    ('chase_citation',            'ref',   'untyped',
        'ref_artifacts',    'Citation-chase pass result'),
    ('resolve_citation:s2',       'ref',   'untyped',
        'ref_artifacts',    'S2 metadata fill for stub ref');
```

> Retraction tracking is **not** on this list. The provenance
> kind — shipped in `0002_provenance.sql` +
> `0003_provenance_rw_cache.sql` + the
> `src/precis/handlers/provenance.py` and
> `src/precis/ingest/provenance.py` modules — handles
> retraction / EoC / correction state through a synchronous
> user-triggered tool that writes through to
> `refs.retraction_*` columns and `links` directly. An earlier
> draft of this ADR listed a `check_retraction:crossmark`
> artifact; that has been retracted per DRY. A future
> periodic-backfill scanner can register here if corpus-wide
> retraction sweeps become a real workload.

`artifact_kinds` is the **handler registry** — it indexes the
worker's view of the world, not the model's. It coexists with
`embedders` and `summarizers`, which carry *model-specific*
metadata (`embedders.dim`, `summarizers.prompt_template`) the
worker needs at load time. Two registries, two purposes:

| Concern | Table | Carries |
| --- | --- | --- |
| Handler / queue / observability | `artifact_kinds` | slug, target, output_table, storage |
| Model loading | `embedders`, `summarizers` | dim, prompt, deprecated_at |

`chunk_embeddings.embedder` keeps its FK to `embedders.name`.
`chunk_summaries.summarizer` keeps its FK to `summarizers.name`.
`<target>_artifacts.artifact` FKs to `artifact_kinds.slug`. Three
FKs, three slug spaces — but the slugs are aligned by convention
(`embed:bge-m3` in `artifact_kinds` corresponds to `bge-m3` in
`embedders`).

### 4. `WorkerHandler` parameterised on five descriptors — SUPERSEDED

> **Superseded by [ADR 0018](./0018-persistent-discovery-layer.md)
> §"Worker":** ref-level workers ship as sibling functions
> (`precis.workers.segment_toc.run_paper_segments_pass` is the
> first example) rather than subclasses of a parameterised base
> class. The rationale: folding ref-level into the chunk-keyed
> base would force polymorphism over the claim shape, and the
> chunk-level base is already wide. A shared `RefWorkerHandler`
> base is worth extracting once a third ref-level worker shows up;
> until then the duplication is small enough to leave inline.
>
> The original proposal below is preserved for context only.

```python
class WorkerHandler(ABC):
    # registry — set in subclass ClassVar
    target_table:     str    # 'chunks' | 'refs' | …
    target_pk_column: str    # 'chunk_id' | 'ref_id' | …
    output_table:     str    # 'chunk_embeddings' | 'ref_artifacts' | …
    artifact_column:  str    # 'embedder' | 'summarizer' | 'artifact'
    artifact_value:   str    # 'bge-m3' | 'rake-lemma' | 'chase_citation'

    def claim_batch(self, conn, *, limit):
        """Generated from descriptors — same shape across the family."""
        sql = f"""
            SELECT t.{self.target_pk_column}, ...
              FROM {self.target_table} t
              LEFT JOIN {self.output_table} o
                ON o.{self.target_pk_column} = t.{self.target_pk_column}
               AND o.{self.artifact_column} = %s
             WHERE o.{self.target_pk_column} IS NULL
             ORDER BY t.{self.target_pk_column}
             LIMIT %s
               FOR UPDATE OF t SKIP LOCKED
        """
        ...

    def write_failed(self, conn, target_pk, error):
        """Generated — INSERT ... ON CONFLICT DO UPDATE."""
        ...

    @abstractmethod
    def process(self, row) -> object: ...

    @abstractmethod
    def write_ok(self, conn, target_pk, payload) -> None: ...
```

Today's handlers (`EmbedHandler`, `RakeLemmaHandler`) set the
typed descriptors and keep their bespoke `write_ok` (vector +
text are not JSONB). New untyped handlers
(`ChaseCitationHandler`, etc.) set the untyped descriptors and
get the default `write_ok` that stuffs JSONB into `payload`.

Net effect: adding a new artifact is

1. INSERT into `artifact_kinds`.
2. If first artifact for that target and `storage='untyped'`,
   CREATE the `<target>_artifacts` table (one-time per target
   kind).
3. Subclass `WorkerHandler`, set five descriptors, implement
   `process` + (for typed) `write_ok`.
4. Register in `precis.workers.runner`.

Step 3-4 is ~30 LoC. Step 1 is one row. Steps 2 only happens once
per target kind in the system's lifetime.

### 5. Observability

`precis worker --status` (per ADR 0007) walks every registered
handler and prints `(total | ok | failed | pending)`. After this
ADR, the iteration source is `artifact_kinds` — one row per
artifact, with `target` + `output_table` telling the status query
where to LEFT JOIN. The status SQL template:

```sql
SELECT
    (SELECT count(*) FROM <target_table>) AS total,
    count(o.<target_pk>) FILTER (WHERE o.status = 'ok')     AS ok,
    count(o.<target_pk>) FILTER (WHERE o.status = 'failed') AS failed
  FROM <output_table> o
 WHERE o.<artifact_column> = %s
```

The four placeholders come from `artifact_kinds`. One template,
five handlers today, room to grow.

## Consequences

### Positive

- **One mental model for derived state.** Whether you're embedding
  a chunk, chasing a citation, or scoring a misattribution link,
  the substrate is the same shape: LEFT-JOIN-IS-NULL claim,
  failure-marker rows, status query.
- **Adding new artifacts is cheap.** No copy-paste of base-class
  SQL; no third registry to wire into `precis worker --status`.
- **Clear "where does this go?" decision.** Typed indexable
  output → dedicated table. Otherwise → `<target>_artifacts`.
  Document on `artifact_kinds.storage`.
- **Schema stays narrow at first.** Only `ref_artifacts` ships
  with this ADR (per finding-chase scope); `link_artifacts` /
  `pdf_artifacts` / `chunk_artifacts` land lazily when a real
  artifact needs them. Greenfield principle preserved.
- **Future-proof for periodic-backfill artifacts.** The substrate
  accommodates artifacts the
  [provenance kind](../provenance-kind-plan.md) explicitly punts
  on — bulk corpus sweeps for retraction state, link-level
  severity scoring, per-PDF OCR retries. None ship with this ADR;
  they have a home for when demand surfaces.

### Negative

- **Four near-identical tables eventually.** `chunk_artifacts`,
  `ref_artifacts`, `link_artifacts`, `pdf_artifacts` all carry
  `(<pk>, artifact, payload, status, attempts, last_error,
  created_at)`. Schema-level DRY would unify them — but
  polymorphism loses typed FK + ON DELETE CASCADE, which matter
  more than the table count.
- **Two registries (`artifact_kinds` + `embedders` / `summarizers`)**
  with slugs that have to stay aligned by convention
  (`embed:bge-m3` ↔ `bge-m3`). Risk of drift if an embedder is
  renamed without updating `artifact_kinds`. Mitigation: a CI
  check in `tests/test_migrations.py` that asserts the conventional
  mapping holds.
- **`WorkerHandler` becomes more abstract.** The current concrete
  base is ~200 LoC, mostly SQL strings; after the refactor it's
  ~250 LoC, with the SQL generated from descriptors. Slightly
  harder to grep ("where is the claim query?") in exchange for
  the family generalisation.

### Rejected alternatives

**Single polymorphic `artifacts` table.**

```sql
CREATE TABLE artifacts (
    target_kind TEXT,
    target_id   TEXT,   -- BIGINT cast or CHAR(64) for pdf_sha256
    artifact    TEXT,
    payload     JSONB,
    ...
);
```

Rejected because:

- `target_id` cannot FK to a single table (polymorphic FK is not
  a Postgres primitive).
- `ON DELETE CASCADE` from the actual target row no longer fires
  — orphaned artifact rows accumulate.
- PK type mismatch (BIGINT vs CHAR(64)) requires either casting
  on every query or accepting `TEXT`-typed PKs everywhere, both
  with perf and correctness costs.
- Loss of per-target indexing (today `chunk_embeddings_failed_idx`
  is partial on `(chunk_id, embedder) WHERE status='failed'` —
  per-target tables make these straightforward, polymorphic
  tables make them awkward).

**Folding typed outputs (`chunk_embeddings`, `chunk_summaries`)
into `chunk_artifacts` with NULLable typed columns.**

Rejected because the HNSW index would cover mostly-NULL rows
(every non-embed artifact would have NULL vector), and the FTS
index over `chunk_artifacts.text` would mix summaries with
LLM-annotation text and other future content.

**Putting handler config into `artifact_kinds`.** The handler
needs `dim` (for embedders) or `prompt_template` (for
summarizers) at *model load* time, not at *queue claim* time.
Keeping that data in `embedders` / `summarizers` keeps the load
path narrow; `artifact_kinds` stays purely about queue +
observability.

## Migration

Lands in **the same migration as the finding-chase work** —
`0004_finding_and_queue_family.sql`. (`0002` shipped as
`0002_provenance.sql`; `0003` is taken twice already by
`0003_app_state.sql` and `0003_provenance_rw_cache.sql`.) Sequence:

1. CREATE TABLE `artifact_kinds`.
2. INSERT the two existing artifacts (`embed:bge-m3`,
   `summarize:rake-lemma`) into `artifact_kinds` with their
   typed `output_table`.
3. CREATE TABLE `ref_artifacts` (FK to `refs`, FK to
   `artifact_kinds`).
4. INSERT the new untyped artifacts (`chase_citation`,
   `resolve_citation:s2`) into
   `artifact_kinds`.
5. (Same migration also adds `kinds.finding`, `chunk_kinds.finding_body`
   etc. — see finding-chase plan.)

No ALTER of existing tables. `chunk_embeddings` and
`chunk_summaries` are untouched; their `embedder` /
`summarizer` columns keep their existing FKs to the model
registries. `artifact_kinds` is a *separate* layer above.

## Open follow-ups

- **`chunk_artifacts` shape preview.** When the first untyped
  per-chunk artifact lands (likely an LLM annotation), confirm
  the table can be created additively without affecting
  `chunk_embeddings` / `chunk_summaries`. The shape above
  asserts it can; we'll know for sure the day it's needed.
- **Backoff policy.** ADR 0007 deferred this; carry forward.
  When backoff lands it lands on `<target>_artifacts.attempts +
  next_attempt_at` and on the typed tables' `attempts +
  next_attempt_at` symmetrically. One pattern, all instances.
- **Worker shard-key.** For multi-worker deployments processing
  millions of items, `ORDER BY <target>_pk` may concentrate
  contention. A future shard-key column on `artifact_kinds`
  (`order_by` SQL fragment) could steer different artifacts to
  different ordering. Defer until measured.
