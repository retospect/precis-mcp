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
supersedes edges; garden merges retain reversal records for
``precis garden undo``, while ambiguous identifier, content and link
transfers stay put with audit history. The raw-id path locks the loser
and sets transaction-local operator trust — only
``precis garden restore`` is the operator entry point.

Concept retirement locks the concept then its cards in ID order; a
``represents`` writer rechecks both endpoints after waiting so it cannot
re-home a card behind the cascade.

Grouped ref counts deduplicate facet membership before numeric aggregation.
Missing or non-numeric JSON reduces the sample size only.

Lowercase paper facet filters match both the classifier's uppercase axis and
literal tags with the same spelling, preserving existing manually tagged refs.
The alias list follows the documented paper facets independently of the
classifier registry, which also contains chunk and bookkeeping axes.

Narrow clients use the import-light role protocols in
:mod:`precis.store.protocols`. ``Hub.live_store`` narrows the optional store.

MCP and web share paper re-keying through :mod:`precis.store.paper_rekey`;
key and reason commit together, and failed PDF moves keep the prior
storage path since filesystem operations sit outside database rollback.

Stub acquisition refuses identifier hits with substantial, disjoint title
tokens, preserving potentially corrupt rows for operator repair. Sparse
or placeholder titles give too little evidence.

Human byline edits preserve OpenAlex linkage, source tier and verification
stamp when normalized name, ORCID and position still match. The form omits
OpenAlex IDs, and matching position keeps same-name coauthors distinct.

Lexical chunk ranks use logarithmic document-length normalization in
standalone and fused search, discounting large chunks with scattered
matches while retaining strict-AND eligibility and the RRF weights;
multi-query search inherits this through its lexical legs. Unscoped
paper OR retries promote papers matching the rarest lexemes — taxon
legs fetch chunks by ref first so a retry only tests taxon chunks, and
unscoped memory search skips the retry.

Figure indexes use the printed table label to exclude table captions —
legacy ingest shares the standalone caption kind across figures and
tables, and a standalone figure caption can survive without a figure
block.

Drive date ordering uses existing ref timestamps:
``recent_refs(created=True)`` orders browse by creation; cross-kind
chunk search accepts ``created`` and ``modified`` alongside legacy
``recency`` — new names leave existing callers' order untouched, and
both tie-break on ref id.

Unscoped paper verbatim search checks whole phrases within a capped pool
of the newest live TSV candidates, recovering phrases KeyBERT omitted
(gr681521) while bounding substring checks; older or unindexed hits can
be missed. Search and count share the pool.

Capped paper search opts into ``search_chunks_multi(prefer_body=True)``:
discard a synthetic card when the fused pool holds a body hit for that
ref, before the diversity cap and page slice — otherwise a stronger card
consumes the paper's only slot and hides quotable evidence. Card-only
matches survive; other consumers keep their ranking.
``search(kind='paper')`` defaults to ``unique_per='paper'`` on the same
machinery (``per_paper=1`` over the fused pool, ``ref_counts_out=``
feeding each row's ``+N`` counter); ``unique_per='chunk'`` is the drill
shape.

Decomposition (in progress, codereview-store-decomposition): the
stateful pool/tx lifecycle lives in :class:`precis.store.core.StoreCore`
(``tx()`` for ``conn=`` ops; ``atomic()`` pins the connection so ops
opening their own join one transaction — the batch ``tag`` seam);
domain sub-stores hold a core, reached as composed properties —
``store.drafts`` (:class:`precis.store._draft_ops.DraftStore`) is the
first, fully carved: draft ops exist only on it (no flat ``Store``
delegations remain).

Draft list containers hold structure; their item children hold the prose
exporters read. Explicit list puts parse Markdown into that shape, and
importers may pass empty text for separately parsed items.

PCB chat guards use READ COMMITTED capture after ordered FOR UPDATE
parents, retired rows and leaves — an earlier snapshot would miss a
child committing during the lock wait. Short parts/footprint SHARE
NOWAIT locks protect missing cache keys and catalogue replacement;
board advisory locks cannot cover existing writers. Cache locks never
span model calls or GET; bounded conflicts roll back without rebasing.
Conn-aware ref/chunk reads avoid a second pooled connection under these
locks. Conversation appends share stored ordinal allocation.

Web, Wikipedia, YouTube and Perplexity cache kinds keep a live ref per
result version; the current row owns the request key while earlier rows
keep their slugs, chunks and citation links so ordinal selectors still
name the original text. A refresh advances the head with a supersedes
edge, copying ref tags for discovery and watch scheduling; identical web
response hashes reuse the head. YouTube's lazy legacy windowing publishes
a version too, keeping the original blob at its original address.

Nanopub supersession uses an explicit version relationship, separate from
approval's replaceable grounding envelope. A discarded candidate detaches the
successor; its predecessor obligation survives and reattaches atomically on
restaging. No signed bytes or timestamp proofs are rewritten.

The schema is defined in `src/precis/migrations/0001_initial.sql`.
Missing-rationale link scans use physical edge IDs rather than offsets, so
backfilling a page cannot shift the remaining queue; they are live reads —
higher IDs can join later pages, late lower-ID commits need a fresh scan.
Reason and legacy why payloads are inspected without refreshing
fingerprints. Part-selection records are memory refs linked by ``selects``
/ ``rejects`` to parts or components; a nonblank rejection reason keeps a
rejected candidate reusable evidence. PCB instance reads resolve the
existing LCSC part ref without minting one and list its decisions, selects
before rejects; superseded records stay reachable through the new record's
links but leave this current-selection view.

Generated baselines provision every extension prerequisite and load the full
seed vocabulary with a transaction-local public search path, so sealed seed
triggers resolve their helpers without changing function bodies or permissions.

Lexical counts and chunk retrieval optionally report the executed OR
fallback through call-local metadata, preserving integer count returns —
search notes use it because any-word matches can cover most of a scope.

Body word counts derive from current chunks in a batched read, so existing
refs need no counter backfill. POSIX whitespace avoids double interpretation
of backslashes by Python, SQL escape strings and the regex engine.

Measure ranking reads the newest content-current ledger verdict in its
candidate query and excludes rejected rows — historical or stale
rejection cannot veto a current approval, and proposed or unreviewed
values retain numeric ordering. The winning row exposes ``review_state``
so consumers distinguish eligible from approved without another read.

The write-time magnitude check requires three live values with a current
approval in the same measurand and effective currency/price year. A majority
of unchecked extraction errors can otherwise set the median and cause a
correct reading to be flagged and excluded from ranking.

Measure paper/quest searches match subjects and anchor-chunk owners so
promoting a subject does not hide its source evidence. Quest filtering
shares the recursive inbound ``serves`` closure with measure ranking.

A measure anchor's ``extraction_status`` check matches the literal on
number boundaries after the markup fold; a math rendering glued to it
(``3651±31$3651\\pm31$``) welds both copies into one digit run. The
retry blanks only math spans rendering the whole literal, preserving
intervening math (``$\\times 2$``); ordinary literals allow whitespace
around non-word tokens, adjacent word tokens require it, and decimal
points stay glued to digits.

Hybrid automatic recall uses ``_search_cut.gap_cut_distance``: best semantic
distance plus one delta, capped at the 0.65 corpus floor — absolute per-kind
floors cannot account for free-text distance offsets. OR-term lexical
corroboration always survives; the numeric renderer hides the same weak tail
with an explicit count, and ids and explicit semantic/lexical modes bypass
the gap cut. ``PRECIS_SEARCH_GAP_CUT`` overrides delta,
``PRECIS_SEARCH_FIXED_FLOORS=1`` restores the old table for one release; the
delta is provisional — local hash embeddings do not calibrate prod bge-m3.

The magnitude check skips readings with an absent, blank or bare ``1``
unit (no prefix to lose). The data migration removes the old magnitude
flags from those live outputs without changing other flags or review
hashes, restoring the frozen-row trigger before commit.

Body-chunk rewrites (``ChunkStore.replace_body_chunk`` /
``delete_body_chunk``, gr782951) serialize writers on the parent
``refs`` row before reading the doomed chunks, mint the replacement at
a scratch ord and re-home pinned link endpoints before the DELETE's
cascade; a clear de-grounds them to ref-level. Re-point and ord settle
run under the ``precis.trust_internal`` door (0369), so moved edges
keep provenance and the audit stays at one ``revise`` act; unlandable
moves drop with a report the acks and previews carry.
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
