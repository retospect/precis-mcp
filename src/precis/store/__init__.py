"""Postgres-backed store for precis V2 (psycopg3, sync).

Public surface:
    Store              — high-level store handle, owns the psycopg pool
    Migrator           — forward-only migration runner
    MigrationChecksumMismatch — the sealed-edit refusal (RuntimeError)
    MigrationLockContention — lock-retry budget exhausted (RuntimeError)

    Ref, ChunkRow, Link, Tag, CacheEntry, ChunkInsert  — frozen row types
    Density, CacheFreshness, Namespace, Relation, ActorSlug  — type aliases

Superseded-ref restoration removes only retirement, the merge pointer/dedup
markers and incoming supersedes edges. Garden merges are refused so their
reversal records survive for ``precis garden undo`` to restore original links.
Identifier, content and ordinary-link
transfers cannot be inferred backwards safely, so they and audit history stay
put. The raw-id store door locks the loser and sets transaction-local operator
trust itself; ``precis garden restore`` supplies the operator context and
resolves the raw loser. No MCP/web restore door is exposed, and the store
and worker layers never import handlers.

Grouped ref counts deduplicate facet membership per ref before aggregating
numeric JSON values. Repeated join rows from tag facets therefore cannot
weight a ref's contribution, and absent or non-numeric JSON values leave
the member count intact while reducing the numeric sample size.

Lowercase paper facet filters match both the classifier's uppercase axis and
literal tags with the same spelling, preserving existing manually tagged refs.
The alias list follows the documented paper facets independently of the
classifier registry, which also contains chunk and bookkeeping axes.

Typing the seam: functions that need only a sliver of the Store take a
role protocol from :mod:`precis.store.protocols` (import-light, no
cycles) instead of ``Store`` or ``Any``. ``Hub.store`` is typed
``Store | None``; ``Hub.live_store`` narrows it for store-backed paths.

Paper re-keying lives in :mod:`precis.store.paper_rekey` so MCP and web
edits share collision checks, audit history and PDF relocation. The key
and reason commit together; filesystem moves remain best-effort, keeping
the old storage path on failure because a database transaction cannot
roll back a corpus filesystem operation.

Stub acquisition refuses an identifier hit with substantial, disjoint title
tokens. An identifier on a corrupt row must not silently claim another work
is held; sparse or placeholder titles provide too little evidence to refuse.
This guard preserves the row for operator repair rather than guessing which
of its metadata, body and links belongs to the requested paper.

Human byline edits keep rows with the same normalized name and ORCID at
the same position. The edit form omits OpenAlex IDs, so a missing ID on
an otherwise unchanged row preserves its index link alongside its
source tier and verification stamp. Retention by position avoids moving
identity evidence between co-authors with the same name.

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

PCB chat guards use READ COMMITTED capture after ordered FOR UPDATE parents,
retired rows and leaves: an earlier repeatable snapshot would miss a child that
commits while locking waits. Short parts/footprint SHARE NOWAIT locks protect
missing cache keys and catalogue replacement; board advisory locks cannot cover
existing writers. Cache locks never span model calls or GET; bounded conflicts
roll back without rebasing. Conn-aware ref/chunk reads avoid a second pooled
connection under these locks. Conversation appends share stored ordinal allocation.

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
