"""Postgres-backed store for precis V2 (psycopg3, sync).

Public surface:
    Store              — high-level store handle, owns the psycopg pool
    Migrator           — forward-only migration runner
    MigrationChecksumMismatch — the sealed-edit refusal (RuntimeError)
    MigrationLockContention — lock-retry budget exhausted (RuntimeError)

    Ref, ChunkRow, Link, Tag, CacheEntry, ChunkInsert  — frozen row types
    Density, CacheFreshness, Namespace, Relation, ActorSlug  — type aliases

The shared corpus uses server-side visibility predicates from
:mod:`precis.store._secret_scope`. Apply them to the owning ref before
sampling, ranking or aggregating a read. Link reads check both endpoints.
Tag usage counts are evaluated per request because a privacy tag can
change on another process between reads. Cache writes recheck visibility
while holding the ref lock so a lookup miss cannot overwrite private data.
Worker dispatch checks dependencies independently of board visibility to
preserve work ordering. Direct SQL bypasses the store and remains subject
to the standing restriction on reading private rows.

Superseded-ref restoration clears retirement, merge markers and incoming
supersedes edges. Garden merges retain reversal records for ``precis garden undo``.
Ambiguous identifier, content and link transfers stay put with audit history.
The raw-id store path locks the loser and sets transaction-local operator
trust. Only ``precis garden restore`` provides the operator entry point.

Concept retirement locks the concept then its cards in ID order. A
``represents`` writer rechecks both endpoints after waiting so it cannot
re-home a card behind the cascade.

Grouped ref counts deduplicate facet membership before numeric aggregation.
Missing or non-numeric JSON reduces the numeric sample size only.

Lowercase paper facet filters match both the classifier's uppercase axis and
literal tags with the same spelling, preserving existing manually tagged refs.
The alias list follows the documented paper facets independently of the
classifier registry, which also contains chunk and bookkeeping axes.

Narrow clients use the import-light role protocols in
:mod:`precis.store.protocols`. ``Hub.live_store`` narrows the optional store.

MCP and web share paper re-keying through :mod:`precis.store.paper_rekey`.
The key and reason commit together. Failed PDF moves retain the prior
storage path because filesystem operations sit outside database rollback.

Stub acquisition refuses identifier hits with substantial, disjoint title
tokens, preserving potentially corrupt rows for operator repair. Sparse
or placeholder titles provide too little evidence for refusal.

Human byline edits preserve OpenAlex linkage, source tier and verification
stamp when normalized name, ORCID and position still match. The form omits
OpenAlex IDs, and matching position keeps same-name coauthors distinct.

Lexical chunk ranks use logarithmic document-length normalization in both
standalone and fused search. This discounts large chunks with scattered
matches while retaining strict-AND eligibility and the existing RRF weights;
multi-query search inherits the same ranking through its lexical legs.
Taxon lexical legs fetch chunks by ref first so an OR retry over common
terms only tests taxon chunks.

Figure indexes use the printed table label to exclude table captions
because legacy ingest shares the standalone caption kind across figures
and tables. A standalone figure caption can survive without a figure block.

Drive date ordering uses existing ref timestamps: ``recent_refs(created=True)``
orders browse by creation; cross-kind chunk search accepts ``created`` and
``modified`` alongside legacy ``recency``. Explicit new names avoid changing
existing callers' modification-first browse or creation-first search order;
both new orders use ref id ties and require no derived clock or schema.

Unscoped paper verbatim search checks whole phrases within a capped pool of
the newest live TSV candidates. This recovers source phrases KeyBERT omitted
(gr681521) while bounding substring checks. Older or unindexed phrase hits
can be missed. Search and count share the pool.

Capped paper search opts into ``search_chunks_multi(prefer_body=True)``:
discard a synthetic card when the fused candidate pool contains a body hit
for that ref, before the diversity cap and page slice. Otherwise a stronger
card can consume the paper's only slot and hide quotable evidence. Card-only
matches survive; other store consumers retain their ranking by default.
``search(kind='paper')`` defaults to ``unique_per='paper'`` on the same
machinery (``per_paper=1`` over the fused pool, ``ref_counts_out=`` feeding
each row's ``+N`` "more" counter); ``unique_per='chunk'`` is the drill shape.

Decomposition (in progress, codereview-store-decomposition): the
stateful pool/tx lifecycle lives in :class:`precis.store.core.StoreCore`
(``tx()`` for ops that take ``conn=``; ``atomic()`` pins the connection so
ops that open their own join one transaction — the batch ``tag`` seam);
domain sub-stores hold a core and are reached as composed properties —
``store.drafts`` (:class:`precis.store._draft_ops.DraftStore`) is the
first, fully carved: draft ops exist only on the sub-store (no flat
delegations remain on ``Store``).

Draft list containers hold structure and their item children hold the prose
read by exporters. Explicit list puts parse Markdown into that shape, and
importers can pass empty text to create containers for separately parsed items.

PCB chat guards use READ COMMITTED capture after ordered FOR UPDATE parents,
retired rows and leaves: an earlier repeatable snapshot would miss a child that
commits while locking waits. Short parts/footprint SHARE NOWAIT locks protect
missing cache keys and catalogue replacement; board advisory locks cannot cover
existing writers. Cache locks never span model calls or GET; bounded conflicts
roll back without rebasing. Conn-aware ref/chunk reads avoid a second pooled
connection under these locks. Conversation appends share stored ordinal allocation.

Web, Wikipedia, YouTube and the Perplexity cache kinds keep a live ref per
result version. The current row owns the request key. Earlier rows keep their
slugs, chunks, and citation links so ordinal selectors still name the original
text. A refresh advances the head with a supersedes edge and copies ref tags
for discovery and watch scheduling. Identical web response hashes reuse the
head. YouTube's lazy legacy windowing also publishes a version and keeps the
original blob at its original address.

Nanopub supersession uses an explicit version relationship, separate from
approval's replaceable grounding envelope. A discarded candidate detaches the
successor; its predecessor obligation survives and reattaches atomically on
restaging. No signed bytes or timestamp proofs are rewritten.

The schema is defined in `src/precis/migrations/0001_initial.sql`.
Missing-rationale link scans use physical edge IDs rather than offsets, so
backfilling a page cannot shift the remaining queue. They are live reads:
higher IDs can join later pages; late lower-ID commits need a fresh scan.
Reason and legacy why payloads are inspected without refreshing fingerprints.
Part-selection records are memory refs linked by ``selects`` / ``rejects``
to parts or components. Link admission requires a nonblank rejection reason
so a rejected candidate remains reusable evidence. PCB instance reads resolve
the existing LCSC part ref without minting one and list its current decisions,
selects before rejects. Superseded records stay reachable through the new
record's links but are omitted from this current-selection view.

Generated baselines provision every extension prerequisite and load the full
seed vocabulary with a transaction-local public search path, so sealed seed
triggers resolve their helpers without changing function bodies or permissions.

Lexical counts and chunk retrieval optionally report the executed OR fallback
through call-local metadata, preserving integer count returns. Search notes
use this signal because any-word matches can cover most of a scope.

Body word counts derive from current chunks in a batched read, so existing
refs need no counter backfill. POSIX whitespace avoids double interpretation
of backslashes by Python, SQL escape strings and the regex engine.

Measure ranking reads the newest content-current ledger verdict in its
candidate query and excludes rejected rows. Historical or stale rejection
cannot veto a current approval; proposed and unreviewed values retain the
numeric ordering. The winning row exposes ``review_state`` so consumers can
distinguish eligible values from approved ones without another ledger read.

The write-time magnitude check requires three live values with a current
approval in the same measurand and effective currency/price year. A majority
of unchecked extraction errors can otherwise set the median and cause a
correct reading to be flagged and excluded from ranking.

Measure paper/quest searches match subjects and anchor-chunk owners so
promoting a subject does not hide its source evidence. Quest filtering
shares the recursive inbound ``serves`` closure with measure ranking.

A measure anchor's ``extraction_status`` check matches the literal on
number boundaries after the markup fold; a duplicate math rendering of the
same value glued to the literal (``3651±31$3651\\pm31$``) welds both copies
into one digit run. The retry blanks only math spans that render the entire
literal, preserving intervening math such as ``$\\times 2$``. Ordinary
literals allow optional whitespace around non-word tokens. Adjacent word
tokens require whitespace, and decimal points stay glued to their digits.

Hybrid automatic recall uses ``_search_cut.gap_cut_distance``: best semantic
distance plus one delta, capped at the 0.65 corpus floor. Absolute per-kind
floors cannot account for free-text queries' distance offsets. OR-term lexical
corroboration always survives; the numeric renderer hides the same weak tail
with an explicit count, while ids and explicit semantic/lexical modes bypass
the gap cut. ``PRECIS_SEARCH_GAP_CUT`` overrides delta;
``PRECIS_SEARCH_FIXED_FLOORS=1`` restores the old table for one release.
The delta is provisional: local hash embeddings do not calibrate prod bge-m3.

The magnitude check skips readings with an absent, blank or bare ``1``
reported unit because there is no unit prefix to lose. The data migration
removes the old magnitude flags from those live outputs without changing
their other flags or review hashes. It restores the frozen-row trigger
before commit so subsequent writes cannot remove review flags.
"""

from __future__ import annotations

from precis.store._salience import (
    as_background_actor,
    as_dream_actor,
    background_actor_active,
    current_background_actor,
)
from precis.store.migrate import (
    MigrationChecksumMismatch,
    MigrationLockContention,
    Migrator,
)
from precis.store.store import (
    SEMANTIC_DISTANCE_FLOOR,
    SEMANTIC_ONLY_DISTANCE_FLOOR,
    SEMANTIC_ONLY_FOLD_DISTANCE,
    Store,
)
from precis.store.types import (
    ActorSlug,
    BibEntry,
    CacheEntry,
    CacheFreshness,
    ChunkInsert,
    ChunkRow,
    Density,
    Link,
    Namespace,
    Ref,
    Relation,
    S2Direction,
    S2Neighbor,
    Tag,
)

__all__ = [
    "SEMANTIC_DISTANCE_FLOOR",
    "SEMANTIC_ONLY_DISTANCE_FLOOR",
    "SEMANTIC_ONLY_FOLD_DISTANCE",
    "ActorSlug",
    "BibEntry",
    "CacheEntry",
    "CacheFreshness",
    "ChunkInsert",
    "ChunkRow",
    "Density",
    "Link",
    "MigrationChecksumMismatch",
    "MigrationLockContention",
    "Migrator",
    "Namespace",
    "Ref",
    "Relation",
    "S2Direction",
    "S2Neighbor",
    "Store",
    "Tag",
    "as_background_actor",
    "as_dream_actor",
    "background_actor_active",
    "current_background_actor",
]
