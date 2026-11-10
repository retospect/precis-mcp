"""Per-chunk KeyBERT keywords with paper-scope abbreviation handling.

F20 (2026-06-05). Replaces the old ``segment_toc`` worker. Where the
old worker computed one cluster-of-chunks segmentation per paper and
stashed per-segment keybert in ``ref_segments``, this worker computes
per-CHUNK keybert keywords and stores them directly on ``chunks``:

* ``chunks.keywords TEXT[]``  — canonical lower-cased short / display
  form for GIN-indexed lexical filter + Jaccard-distance clustering
  at query time.
* ``chunks.keywords_meta JSONB`` — versioned rich envelope::

      {
        "version": "1.0",
        "embedder": "bge-m3",
        "keywords": [
          {"short": "MOF", "long": "metal-organic framework", "score": 0.82},
          {"short": null,  "long": "activation procedures",   "score": 0.61}
        ]
      }

Algorithm (per chunk):

1. Skip chunks below ``MIN_CHUNK_CHARS`` (too short for stable
   KeyBERT — they fold into neighbours at TOC time via the
   empty-keyword Jaccard defence).
2. Skip chunks whose ``chunk_kind`` is on the non-content list
   (cards, tables, figures, equations, references). These would
   pollute the keyword set if included.
3. Load (or compute + lazy-stash) the paper's abbreviation dict from
   ``refs.meta['abbrevs']`` (Schwartz-Hearst).
4. Generate candidate phrases via RAKE.
5. For each candidate that matches a known short, embed its **long
   form** for scoring — gives bge-m3 the richer context (the
   embedding knows ``MOF ≈ metal-organic framework``).
6. Score by cosine vs. the chunk's pre-computed embedding.
7. Take top-K, dedupe abbrev pairs, store both forms in
   ``keywords_meta`` and the canonical short in ``keywords TEXT[]``.

Lazy upgrade: bump :data:`KEYWORDS_VERSION` to invalidate every
existing row's ``keywords_meta.version`` mismatch; the claim query
re-claims those chunks for re-extraction.

**Claim shape (gr520236 / gr550973 / gr563702).** Three tiered ``FOR
UPDATE OF c SKIP LOCKED`` queries, newest-first within each: (1)
``conv``/``draft``/``memory`` chunks — the interactive lane, jumping the
whole queue regardless of freshness and capped at ``limit -
bulk_reserve(limit)`` (gr563702) so an interactive burst can never
consume the bulk tiers' whole batch; (2) the
fresh ``keywords IS NULL`` backlog, ``chunk_id DESC`` so the newest
ingests get labels first and the ``chunks_keywords_pending_idx``
partial index (:data:`PENDING_INDEX_DDL` — pre-built on prod by ops,
not yet a shipped migration) yields only pending rows — the old
single ascending scan re-walked every already-done row between the
index's id-1 end and the live backlog on every claim; (3) stale
re-derives (version / ``content_sha`` mismatch with keywords already
set), also newest-first, and paced — it seq-scans
``chunk_embeddings``, so it runs every pass only while a live stale
backlog is draining, else every :data:`_STALE_CLAIM_EVERY` passes.

**Dedicated drain lane (gr520236 / gr550973).** The shared-tick pass
above drains at most ~7k chunks/day against a ~2.8k/h inflow, so the
pass also runs as a dedicated single-purpose worker:
``precis worker --only chunk_keywords --dedicated`` claims its own
batch (``PRECIS_CHUNK_KEYWORDS_DEDICATED_BATCH_SIZE``, default 500),
loops immediately while work remains, and sleeps
``PRECIS_CHUNK_KEYWORDS_DEDICATED_IDLE_SECONDS`` (default 60 s) when
the queue is empty. ``PRECIS_CHUNK_KEYWORDS_DEDICATED=1`` on the
shared units keeps the rotation's ref-pass from double-running the
same queue (``--only`` still forces it for a manual run).

**Parallel-safe, no long transactions.** The dedicated lane takes the
pass WITHOUT the advisory lock (``single_runner=False``): N lanes shard
the claim by ``chunk_id % N = i`` (``--shard-count`` /
``--shard-index``, env ``PRECIS_CHUNK_KEYWORDS_SHARDS`` /
``PRECIS_CHUNK_KEYWORDS_SHARD_INDEX``, default 1/0; N is GLOBAL —
hosts × lanes per host, see playbook 20g). The claim's ``FOR UPDATE OF
c SKIP LOCKED`` arbitrates the claim INSTANT only — its row locks
release at the claim's commit, before any embedder call, so no
transaction is ever open across the slow HTTP work — but it is not the
partition: two lanes in disjoint residue classes could still claim the
same row in the post-commit window (crash, same-shard restart, a
shared-tick pass racing the lane). That duplicate is harmless: the
write keeps the stale guard (``keywords IS NULL`` or
version/``content_sha`` drift), so a second writer of the same chunk
is a no-op.

**Single-runner advisory lock (shared tick only).** On the worker
rotation ``chunk_keywords`` still runs on every system-profile host on
the same ~2s tick — without a fleet-wide lock, every host would
re-claim and re-derive the *same* batch every cycle (fleet-normal, not
a rare edge case). Guarded by a **transaction**-scoped
``pg_try_advisory_xact_lock`` (:func:`precis.store.advisory.try_xact_advisory_lock`)
held on one dedicated, non-pooled connection spanning claim + every
chunk's processing (gr463966). The earlier session-scoped
``pg_try_advisory_lock`` was broken behind prod's pgbouncer
``pool_mode = transaction``: lock and unlock landed on different server
backends, so there was no exclusion and the lock leaked. An open
transaction pins one pgbouncer server connection for the whole pass, so
the lock lives and dies on one backend, and it ends with the transaction
(rollback in a ``finally``), so a ``DISCARD ALL`` reset or backend reuse
cannot leak it. The pass's own wall-clock cap
(:data:`_MAX_SECONDS_DEFAULT`, overridable) bounds the routine hold,
but a configured longer window — or one slow claim/embed call — can
still pass prod's ``300s`` ``idle_in_transaction_session_timeout``
kill (same ``ALTER ROLE`` block that sets ``lock_timeout=5s``), which
would end the idle lock-holding transaction mid-run, silently dropping
the lock; so the helper issues ``SET LOCAL
idle_in_transaction_session_timeout = 0``.
``SET LOCAL`` is transaction-scoped; a session ``SET`` through pgbouncer
would poison other clients. A lock-acquire miss (another host
owns this tick) is a graceful skip — same ``claimed=0`` convention as
the ``EmbedderUnavailable`` branch, not a failure.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np
from psycopg import Connection
from psycopg.errors import DeadlockDetected, LockNotAvailable
from psycopg.types.json import Jsonb

from precis.embedder import Embedder, EmbedderUnavailable
from precis.store.advisory import try_xact_advisory_lock
from precis.utils.abbreviations import find as find_abbreviations
from precis.utils.db_retry import retry_locked
from precis.utils.env import env_flag, env_float, env_int
from precis.utils.rake import extract_keywords as _rake_phrases
from precis.workers._claim import bulk_reserve

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: Bump on algorithm changes so existing rows get re-extracted.
KEYWORDS_VERSION = "1.0"

#: Fixed signed-bigint key for the single-runner advisory lock (same idiom
#: as ``paper_reconcile``/``openalex_enrich``/``paper_meta_enrich`` — see
#: ``paper_reconcile``'s module docstring). Arbitrary constant, namespaced
#: away from their keys.
_LOCK_KEY = 0x63_68_6E_6B_5F_6B_77_00 - 2**63  # "chnk_kw\x00", mapped signed

#: Below this length, KeyBERT doesn't have enough text for stable
#: scoring. We leave ``keywords`` NULL — the chunk folds into its
#: neighbour at TOC-clustering time (Jaccard defence: empty-keyword
#: chunks contribute zero distance to either side).
MIN_CHUNK_CHARS = 150

#: Chunk kinds whose text is non-prose and would produce noise if
#: fed to KeyBERT. Cards (front matter), tables (cell salad),
#: equations (LaTeX), figures (terse captions), references (citation
#: lists). The set is small and stable; promote to ``chunk_kinds``
#: metadata if it ever grows.
SKIP_KINDS: frozenset[str] = frozenset(
    {
        "card_authors",
        "card_combined",
        "card_description",
        "card_title",
        "table",
        "equation",
        "figure",
        "references",
        # Paper notes (gr521094) sit at ord<0 so the claim queries
        # never reach them anyway — listed so a kind scan reads as
        # classified, not missed.
        "paper_note",
        # Coverage scan geometry (gr540872): a summary line over an
        # extxyz blob, meta.no_index -- never prose.
        "scan_geometry",
    }
)

#: Top-K KeyBERT keywords kept per chunk.
_TOP_K = 8

#: RAKE candidate pool size. Wider gives KeyBERT more to score and
#: rank from. Unique candidates across the claimed chunks share each
#: embed call.
_RAKE_POOL = 40

#: ``PRECIS_CHUNK_KEYWORDS_BATCH_SIZE`` — chunks claimed per pass; the
#: pass's own knob. The ``_chunk_keywords_pass`` closure deliberately
#: does NOT forward the worker loop's ``--batch-size`` (default 32) —
#: the generic flag made this knob dead code, capping each claim at 32
#: and draining a 1.6M-chunk backlog at ~43 chunks/h (gr549751); same
#: argparse-default-overrides-env trap as
#: ``PRECIS_TAPROOT_CHASE_TRIGGER_BATCH_SIZE`` (gr454865).
_BATCH_SIZE_ENV = "PRECIS_CHUNK_KEYWORDS_BATCH_SIZE"
_BATCH_SIZE_DEFAULT = 256

#: ``PRECIS_CHUNK_KEYWORDS_MAX_SECONDS`` — wall-clock cap on one pass:
#: the pass stops preparing/embedding new chunks once the deadline
#: passes, so a big claim can't hold the single-runner advisory lock
#: for minutes. ``0`` claims nothing — a parked drain that doesn't
#: touch ``service prio``.
_MAX_SECONDS_ENV = "PRECIS_CHUNK_KEYWORDS_MAX_SECONDS"
_MAX_SECONDS_DEFAULT = 60.0

#: Unique candidate texts per embedder call inside a pass. There is no
#: client-side max batch size to honour — ``RemoteEmbedder.embed``
#: ships every text in one ``EmbedRequest`` and ``serve-embeddings``
#: re-chunks internally under its own ``pass_token_budget`` — so this
#: bound is ours: at the prod-observed ~0.1 s/candidate-text it keeps
#: one call under ~30 s, so the deadline check between calls stays
#: meaningful and the response payload (~30 MB of vectors) bounded.
_EMBED_CALL_MAX_TEXTS = 256

#: ``PRECIS_CHUNK_KEYWORDS_DEDICATED`` — set on the shared worker units
#: when the dedicated drain lane owns the queue: the rotation's
#: ``chunk_keywords`` ref-pass then doesn't register (the dedicated
#: lane itself runs ``--dedicated``, which never reaches ref-pass
#: registration; an explicit ``--only chunk_keywords`` still registers
#: for a manual run — ``--only`` is the operator escape hatch).
_DEDICATED_ENV = "PRECIS_CHUNK_KEYWORDS_DEDICATED"

#: The dedicated lane's own knobs (gr520236): a bigger claim than the
#: shared tick's — the lane holds no advisory lock, so a big batch
#: can't starve a sibling — a once-a-minute re-check when the queue is
#: empty ("rest when done, check once a minute"), and a generous
#: wall-clock cap per pass (the cap bounds lock hold on the shared
#: tick; here it only bounds one pass's work).
_DEDICATED_BATCH_SIZE_ENV = "PRECIS_CHUNK_KEYWORDS_DEDICATED_BATCH_SIZE"
_DEDICATED_BATCH_SIZE_DEFAULT = 500
_DEDICATED_IDLE_ENV = "PRECIS_CHUNK_KEYWORDS_DEDICATED_IDLE_SECONDS"
_DEDICATED_IDLE_DEFAULT = 60.0
_DEDICATED_MAX_SECONDS_ENV = "PRECIS_CHUNK_KEYWORDS_DEDICATED_MAX_SECONDS"
_DEDICATED_MAX_SECONDS_DEFAULT = 900.0

#: Dedicated-lane sharding (env form of ``--shard-count`` /
#: ``--shard-index``): lane ``i`` of ``N`` claims ``chunk_id % N = i``
#: so N lanes never work the same chunk. Default 1/0 = the whole queue.
_SHARDS_ENV = "PRECIS_CHUNK_KEYWORDS_SHARDS"
_SHARD_INDEX_ENV = "PRECIS_CHUNK_KEYWORDS_SHARD_INDEX"

#: Rows per ``UPDATE ... FROM (VALUES ...)`` bulk write — one
#: multi-row statement replaces the per-chunk write transactions
#: (gr550973); bounded so a single statement's lock footprint and
#: payload stay small.
_BULK_WRITE_MAX_ROWS = 250

#: Candidate multiplier on the claim's LIMIT: every tier's ``kw_cand``
#: candidate subquery materializes at most
#: ``remaining * _CANDIDATE_SCAN_MULT`` of the newest claimable rows
#: before the outer query keeps ``remaining`` — headroom that costs
#: nothing while keeping the window generous. ``kw_cand`` applies the
#: embedding gate itself (an ``EXISTS`` on ``chunk_embeddings``), so
#: the cap counts only CLAIMABLE rows: a not-yet-embedded head block
#: (ingest landed, ``embed`` still behind — or permanently failed
#: embeddings) is walked past, never counted, and the claim returns
#: exactly the rows and order the unbounded query would.
_CANDIDATE_SCAN_MULT = 4

#: Run the tier-3 stale re-derive claim only every Nth pass — but only
#: while no stale backlog is draining. The claim seq-scans
#: ``chunk_embeddings`` (~4.7M rows) on every underfilled pass, and a
#: stale re-derive delayed N×tick costs nothing user-visible. The
#: exception: a ``KEYWORDS_VERSION`` bump re-derives the whole corpus
#: and that backlog drains ONLY through tier 3, so pacing it to every
#: Nth pass stretches a full re-derive N× — while the previous tier-3
#: claim returned rows it runs on every pass (:data:`_stale_draining`),
#: falling back to the paced cadence after a claim comes back empty.
_STALE_CLAIM_EVERY = 10

#: Passes this process has run (:func:`run_chunk_keywords_pass` bumps
#: it); the stale claim fires when it hits a multiple of
#: :data:`_STALE_CLAIM_EVERY`, or on every pass while
#: :data:`_stale_draining` holds.
_pass_count = 0

#: True while the previous tier-3 claim returned rows — a live stale
#: backlog (a ``KEYWORDS_VERSION`` re-derive) drains on every pass.
#: Per-process like :data:`_pass_count`.
_stale_draining = False


def _eligibility_sql(prefix: str) -> str:
    """The claim's static eligibility rendered with a column ``prefix``
    (``"c."`` for the claim's aliased chunks table, ``""`` for the
    ``chunks_keywords_pending_idx`` partial-index predicate in
    :data:`PENDING_INDEX_DDL`) — the ONE source for the literals both
    carry. The claim inlines this text rather than binding params: the
    planner only picks the partial index when it can prove the query's
    WHERE implies the index predicate, which takes the same literal
    constants, not $n."""
    kinds = ", ".join(f"'{k}'" for k in sorted(SKIP_KINDS))
    return (
        f"{prefix}chunk_kind NOT IN ({kinds}) "
        f"AND length({prefix}text) >= {MIN_CHUNK_CHARS} "
        f"AND ({prefix}meta->>'no_index') IS DISTINCT FROM 'true'"
    )


#: The eligibility fragment as the claim's SELECT uses it (aliased ``c.``).
_ELIGIBILITY_SQL = _eligibility_sql("c.")

#: The partial index the tier-2 (fresh-backlog, ``chunk_id DESC``)
#: claim is shaped for — NOT a shipped migration. A plain
#: ``CREATE INDEX`` on prod's ``chunks`` (~4.7M rows) takes a SHARE
#: lock for a full heap scan and stalls ingest, so ops pre-builds it
#: CONCURRENTLY out of band before the migration ships (see
#: ``docs/backlog/chunk-keywords-pending-index.md``); the migration's
#: guard then verifies the pre-built index instead of building inline.
#: The claim works without it — just slower — and the predicate
#: carries the static eligibility too because ~1.4M of the
#: ``keywords IS NULL`` rows can never be keyworded (skip-kinds and
#: short text stay that way forever) and would otherwise sit in the
#: index forever, walked by every claim.
PENDING_INDEX_DDL = (
    "CREATE INDEX CONCURRENTLY chunks_keywords_pending_idx "
    "ON chunks (chunk_id DESC) "
    f"WHERE keywords IS NULL AND {_eligibility_sql('')}"
)


def _batch_size_default() -> int:
    return env_int(_BATCH_SIZE_ENV, _BATCH_SIZE_DEFAULT, lo=1)


def _max_seconds_default() -> float:
    return env_float(_MAX_SECONDS_ENV, _MAX_SECONDS_DEFAULT, lo=0.0)


def dedicated_lane_enabled() -> bool:
    """True when ``PRECIS_CHUNK_KEYWORDS_DEDICATED`` says a dedicated
    lane owns the queue — the shared-tick ref-pass stays dark."""
    return env_flag(_DEDICATED_ENV)


def is_keyword_eligible(chunk: Any) -> bool:
    """The structural half of the claim predicate, in Python: a
    non-:data:`SKIP_KINDS` kind, ``len(text) >= MIN_CHUNK_CHARS``, and
    not ``meta.no_index`` — the same filters
    :func:`claim_chunks_without_keywords` and
    :func:`keyword_eligible_chunk_count` express in SQL, for callers
    that already hold chunk objects (``toc_db``'s pending-labels
    notice). ``getattr``-safe: a stub without ``text``/``chunk_kind``
    fields is ineligible by the length floor."""
    if getattr(chunk, "chunk_kind", "paragraph") in SKIP_KINDS:
        return False
    if len(getattr(chunk, "text", "") or "") < MIN_CHUNK_CHARS:
        return False
    meta = getattr(chunk, "meta", None) or {}
    return str(meta.get("no_index", "")).lower() != "true"


def claim_chunks_without_keywords(
    conn: Connection,
    *,
    limit: int,
    include_stale: bool = True,
    stale_claimed: list[int] | None = None,
    shard_count: int = 1,
    shard_index: int = 0,
) -> list[tuple[int, int, str, str | None]]:
    """Return up to ``limit`` chunks needing keyword extraction.

    A chunk is claimed when either its ``keywords`` array is NULL
    (never processed) or its ``keywords_meta->>'version'`` doesn't
    match :data:`KEYWORDS_VERSION` (algorithm changed). Filters out
    skip-kinds and too-short chunks at the SQL boundary so the
    worker doesn't pay for rows it would immediately discard.

    Three tiered claims, each ``ORDER BY c.chunk_id DESC`` (gr520236),
    sharing one two-level shape — an inner ``kw_cand`` candidate scan
    capped at ``remaining * _CANDIDATE_SCAN_MULT`` rows and an outer
    query that joins ``chunk_embeddings`` for the vector payload and
    takes the row locks. The embedding gate is an ``EXISTS`` inside
    ``kw_cand``, so the cap counts only claimable rows: a
    not-yet-embedded head block is walked past, never counted, and the
    rows returned and their order equal the unbounded query's:

    1. ``conv``/``draft``/``memory`` chunks, fresh or stale — the
       interactive lane. Conv/draft jump the queue because asa_bot's
       mid-range digest tier renders ``chunks.keywords`` for each turn
       and the draft reader's view-slider (keywords / summary) needs
       them promptly on the actively-edited write-up; ``memory`` refs
       joined the lane under gr563702 — a just-written or edited memory
       is what an agent needs to recall next, ahead of the paper
       backlog. The lane is capped at ``limit - bulk_reserve(limit)``
       so it can never consume a whole batch. The tier
       drives from ``refs``: a materialized ``kw_refs`` CTE (index
       scan on ``refs_kind_idx``) joined to ``chunks_ref_id_idx``, so
       an idle poll costs a handful of index probes — not a
       ``chunks_pkey`` backward walk over 4.7M rows hunting rare
       conv/draft/memory pending rows.
    2. Fresh backlog (``keywords IS NULL``) — newest-first so fresh
       ingests get labels promptly, and cheap at any backlog depth:
       the ``chunks_keywords_pending_idx`` partial index
       (:data:`PENDING_INDEX_DDL`, pre-built by ops — on
       ``keywords IS NULL`` plus the same eligibility literals the
       claim inlines via :func:`_eligibility_sql`) lets the DESC walk
       skip every already-done row AND the never-eligible bulk, where
       the previous single ascending claim re-scanned the done prefix
       from id 1 on every batch. The small conv/draft ref set is a
       once-per-claim ``NOT IN`` hash, not a per-row ``refs`` probe.
    3. Stale re-derives — keywords exist but the version or
       ``content_sha`` drifted (edited ``draft`` chunks; papers leave
       ``content_sha`` NULL so NULL-vs-NULL never fires). Skipped when
       ``include_stale`` is false — the claim seq-scans
       ``chunk_embeddings``, so :func:`run_chunk_keywords_pass` runs
       it every pass only while the previous tier-3 claim returned
       rows, else every :data:`_STALE_CLAIM_EVERY` passes.

    ``shard_count`` / ``shard_index`` scope every tier to
    ``c.chunk_id % shard_count = shard_index`` — dedicated lane ``i``
    of ``N`` only ever claims its own residue class, so N lanes work
    disjoint sets without a fleet-wide lock (``shard_count=1`` drops
    the clause entirely, keeping the unsharded claim text unchanged).

    Returns rows of ``(chunk_id, ref_id, text, embedding)`` — the
    embedding is pulled from ``chunk_embeddings`` via JOIN so the
    worker can score candidates without re-embedding the chunk.
    ``embedding`` is ``None`` when no embedding exists yet (worker
    pass ordering: embed runs before keybert).

    ``stale_claimed``: when a list is passed, the number of rows the
    tier-3 claim added is appended to it (nothing is appended when
    tier 3 did not run — ``include_stale`` false or the earlier tiers
    already filled ``limit``). :func:`run_chunk_keywords_pass` reads
    it to pace the seq-scan off the backlog it actually saw.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if shard_count < 1:
        raise ValueError("shard_count must be >= 1")
    if not 0 <= shard_index < shard_count:
        raise ValueError("shard_index must satisfy 0 <= shard_index < shard_count")
    # ``meta->>'no_index' IS DISTINCT FROM 'true'`` filters out
    # ephemeral chunks (e.g. ``structure_draft`` annotation views
    # written by precis-dft's view_worker). NULL-safe: the vast
    # majority of chunks have no ``no_index`` key and still match.
    # Documented in ``docs/decisions/`` alongside PR 2 of the
    # plugin-substrate work; see the design doc at
    # ``dft-phase-0-pr-2-substrate-hardening`` (git-only) §2.3.
    # The two-level shape: ``kw_refs`` materializes the small
    # conv/draft ref set once per claim (``refs_kind_idx`` scan), and
    # ``kw_cand``'s own ``LIMIT remaining * _CANDIDATE_SCAN_MULT`` caps
    # how many CLAIMABLE rows materialize per claim — the embedding
    # gate is an ``EXISTS`` inside ``kw_cand``, so a not-yet-embedded
    # head block is walked past, never counted: it cannot starve the
    # claim to zero the way an outer-join-only filter did (0266 review:
    # >cap unembedded rows at the head evicted every claimable row from
    # the window and the lane slept while embedded backlog waited).
    # Tier 1 additionally drives from ``kw_refs`` (the join inside its
    # ``kw_cand``): conv/draft refs are few, so ``chunks_ref_id_idx``
    # probes beat a ``chunks_pkey`` DESC walk by orders of magnitude
    # when their pending rows are rare.
    # The embedding gate — only once the embedding is current for this
    # text — keeps KeyBERT scoring against ``ce.vector`` that matches
    # the text (else keywords from new text rank against a stale
    # vector). The outer query re-joins it for the vector payload and
    # re-checks the same predicates before locking.
    select = """
        WITH kw_refs AS MATERIALIZED (
            SELECT ref_id FROM refs WHERE kind IN ('conv', 'draft', 'memory')
        ),
        kw_cand AS MATERIALIZED (
            SELECT c2.chunk_id
              FROM chunks c2
              {join}
             WHERE {inner_eligibility}
               AND EXISTS (
                     SELECT 1
                       FROM chunk_embeddings ce2
                      WHERE ce2.chunk_id = c2.chunk_id
                        AND ce2.embedder = %s
                        AND ce2.status = 'ok'
                        AND ce2.vector IS NOT NULL
                        AND ce2.content_sha IS NOT DISTINCT FROM c2.content_sha{embedding_fence}
                   )
               AND {where}
             ORDER BY c2.chunk_id DESC
             LIMIT %s
        )
        SELECT c.chunk_id, c.ref_id, c.text, ce.vector::text
          FROM kw_cand q
          JOIN chunks c ON c.chunk_id = q.chunk_id
          LEFT JOIN chunk_embeddings ce
            ON ce.chunk_id = c.chunk_id
           AND ce.embedder = %s
           AND ce.status = 'ok'
         WHERE {eligibility}
           AND ce.vector IS NOT NULL
           AND ce.content_sha IS NOT DISTINCT FROM c.content_sha
         ORDER BY c.chunk_id DESC
         LIMIT %s
           FOR UPDATE OF c SKIP LOCKED
    """
    # re-derive when the chunk's text changed since the keywords were
    # built (edited `draft` chunks), or the algorithm version moved.
    pending = """(
                c2.keywords IS NULL
             OR (c2.keywords_meta->>'version') IS DISTINCT FROM %s
             OR (c2.keywords_meta->>'content_sha') IS DISTINCT FROM c2.content_sha
           )"""
    shard_clause = " AND c2.chunk_id %% %s = %s" if shard_count > 1 else ""
    out: list[tuple[int, int, str, str | None]] = []

    def _claim(
        join: str,
        where: str,
        params: tuple[Any, ...],
        *,
        embedding_fence: str = "",
        cap: int | None = None,
    ) -> None:
        remaining = (cap if cap is not None else limit) - len(out)
        if remaining <= 0:
            return
        shard_params: tuple[Any, ...] = (
            (shard_count, shard_index) if shard_count > 1 else ()
        )
        rows = conn.execute(
            select.format(
                join=join,
                where=where + shard_clause,
                eligibility=_ELIGIBILITY_SQL,
                inner_eligibility=_eligibility_sql("c2."),
                embedding_fence=f"\n                     {embedding_fence}"
                if embedding_fence
                else "",
            ),
            (
                "bge-m3",
                *params,
                *shard_params,
                remaining * _CANDIDATE_SCAN_MULT,
                "bge-m3",
                remaining,
            ),
        ).fetchall()
        for r in rows:
            out.append((int(r[0]), int(r[1]), str(r[2]), r[3]))

    # Tier 1 is the interactive lane (conv/draft/memory refs — gr563702
    # added ``memory`` so a just-written memory's labels land before the
    # paper backlog). Its claim is capped at ``limit - bulk_reserve``:
    # the interactive set is structurally small, and the bound keeps an
    # interactive burst from ever consuming the bulk tiers' whole batch.
    _claim(
        "JOIN kw_refs kr ON kr.ref_id = c2.ref_id",
        pending,
        (KEYWORDS_VERSION,),
        cap=limit - bulk_reserve(limit),
    )
    if len(out) < limit:
        _claim(
            "",
            "c2.ref_id NOT IN (SELECT ref_id FROM kw_refs) AND c2.keywords IS NULL",
            (),
            # Keep OFFSET 0: EXISTS flattening would scan all chunk_embeddings.
            embedding_fence="OFFSET 0",
        )
    if include_stale and len(out) < limit:
        before = len(out)
        _claim(
            "",
            "c2.ref_id NOT IN (SELECT ref_id FROM kw_refs) "
            f"AND c2.keywords IS NOT NULL AND {pending}",
            (KEYWORDS_VERSION,),
        )
        if stale_claimed is not None:
            stale_claimed.append(len(out) - before)
    return out


def keyword_eligible_chunk_count(conn: Connection, *, ref_id: int) -> int:
    """Body chunks (``ord >= 0``) of ``ref_id`` eligible for keyword
    extraction — the denominator the toc readiness line pairs with
    :func:`unkeyworded_chunk_count` (``keyworded N/<eligible>``).

    The structural half of :func:`claim_chunks_without_keywords`'s
    predicate: not a :data:`SKIP_KINDS` kind, ``length(text) >=
    MIN_CHUNK_CHARS``, not ``meta.no_index``. The claim's other
    clauses (a current embedding to score against, keywords missing or
    stale) are *processing* preconditions, not eligibility, so they are
    not part of this denominator — same split as
    :func:`~precis.workers.llm_summarize.eligible_chunk_count`.
    """
    row = conn.execute(
        """
        SELECT count(*)
          FROM chunks c
         WHERE c.ref_id = %(ref_id)s
           AND c.ord >= 0
           AND c.chunk_kind <> ALL(%(skip_kinds)s)
           AND length(c.text) >= %(min_chars)s
           AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
        """,
        {
            "ref_id": ref_id,
            "skip_kinds": list(SKIP_KINDS),
            "min_chars": MIN_CHUNK_CHARS,
        },
    ).fetchone()
    return int(row[0]) if row else 0


def unkeyworded_chunk_count(conn: Connection, *, ref_id: int) -> int:
    """Eligible body chunks of ``ref_id`` still carrying NULL
    ``keywords`` — the pending backlog the toc readiness line's
    ``— keywords pending for N chunks`` tail names.

    ``keywords IS NULL`` only: a version- or ``content_sha``-stale row
    re-queues in :func:`claim_chunks_without_keywords` but keeps its old
    labels meanwhile, so it is not "pending" the way a never-labelled
    chunk is."""
    row = conn.execute(
        """
        SELECT count(*)
          FROM chunks c
         WHERE c.ref_id = %(ref_id)s
           AND c.ord >= 0
           AND c.chunk_kind <> ALL(%(skip_kinds)s)
           AND length(c.text) >= %(min_chars)s
           AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
           AND c.keywords IS NULL
        """,
        {
            "ref_id": ref_id,
            "skip_kinds": list(SKIP_KINDS),
            "min_chars": MIN_CHUNK_CHARS,
        },
    ).fetchone()
    return int(row[0]) if row else 0


def ensure_paper_abbrevs(conn: Connection, ref_id: int) -> dict[str, str]:
    """Return ``{SHORT: long}`` for the paper, populating it on first ask.

    First call for a ref runs Schwartz-Hearst over the ref's full
    body text and stashes the result on ``refs.meta['abbrevs']``.
    Subsequent calls read from JSONB directly.

    Empty dict when the ref has no detectable abbreviations.
    """
    row = conn.execute("SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)).fetchone()
    if row is None:
        return {}
    meta = row[0] or {}
    cached = meta.get("abbrevs")
    if cached is not None:
        # ``cached`` is the JSONB dict. Values may be plain strings
        # (legacy) or `{long, first_at}` envelopes; normalize to
        # ``{short: long}`` for the caller.
        out: dict[str, str] = {}
        for short, val in cached.items():
            if isinstance(val, str):
                out[short] = val
            elif isinstance(val, dict) and "long" in val:
                out[short] = str(val["long"])
        return out

    # Build the dict from the body text. We concatenate every body
    # chunk (ord >= 0, non-skip-kind) to give Schwartz-Hearst the
    # widest context — the parenthetical definition often lives in
    # the intro and the short form appears later.
    body_rows = conn.execute(
        """
        SELECT c.text
          FROM chunks c
         WHERE c.ref_id = %s
           AND c.ord >= 0
           AND c.chunk_kind <> ALL(%s)
         ORDER BY c.ord
        """,
        (ref_id, list(SKIP_KINDS)),
    ).fetchall()
    full_text = "\n\n".join(r[0] or "" for r in body_rows)
    detected = find_abbreviations(full_text) if full_text else {}

    # Stash on meta as a jsonb MERGE (`||`), not a full-``meta``
    # overwrite: this write can race a concurrent meta writer for the
    # same ref (another chunk_keywords worker on a sibling host, or an
    # unrelated pass), and a bare `SET meta = %s` built from a
    # possibly-stale `meta` read above would silently clobber whatever
    # that writer just landed. The payload carries ONLY the ``abbrevs``
    # key, so the merge can only ever touch that one key.
    conn.execute(
        "UPDATE refs SET meta = coalesce(meta, '{}'::jsonb) || %s::jsonb "
        "WHERE ref_id = %s",
        (Jsonb({"abbrevs": dict(detected)}), ref_id),
    )
    return detected


def extract_chunk_keywords(
    *,
    chunk_text: str,
    chunk_embedding: list[float],
    abbrevs: dict[str, str],
    embedder: Embedder,
) -> list[dict[str, Any]]:
    """Return the top-K KeyBERT keywords for one chunk.

    Pure compute (modulo embedder calls). Returns the rich form
    ``[{"short": str|None, "long": str, "score": float}, ...]``,
    descending by score.

    Algorithm:
      1. RAKE candidate generation (up to ``_RAKE_POOL`` phrases).
      2. For each candidate: if it matches a known short, embed its
         long form (richer context for bge-m3). Otherwise embed as-is.
      3. Cosine vs. chunk embedding; descending sort.
      4. Take top-K; merge abbrev pairs (``MOF`` + ``metal-organic
         framework`` collapse to one entry).
    """
    candidate_meta = _prepare_keyword_candidates(chunk_text, abbrevs)
    if not candidate_meta:
        return []
    cand_vecs = embedder.embed([long_form for long_form, _ in candidate_meta])
    return _score_keyword_candidates(chunk_embedding, candidate_meta, cand_vecs)


def _prepare_keyword_candidates(
    chunk_text: str, abbrevs: dict[str, str]
) -> list[tuple[str, str | None]]:
    """Keep each candidate's embedding text and paper-local short form."""
    candidates = _rake_phrases(chunk_text, max_keywords=_RAKE_POOL)

    # Build the candidate→embedding_text mapping (long-form if known
    # short). We preserve the candidate-as-it-appeared for short-
    # form annotation downstream.
    short_to_long: dict[str, str] = {}
    long_to_short: dict[str, str] = {}
    for short, long_form in abbrevs.items():
        short_to_long[short.lower()] = long_form
        long_to_short[long_form.lower()] = short

    candidate_meta: list[tuple[str, str | None]] = []  # (long-form, short or None)
    for cand in candidates:
        cand_l = cand.lower()
        long_form = short_to_long.get(cand_l, cand)
        # If candidate is the long form of a known abbrev, attach the
        # short. If candidate IS a short, long is short_to_long[c].
        cand_short: str | None
        if cand_l in short_to_long:
            cand_short = next(s for s in abbrevs if s.lower() == cand_l)
        elif cand_l in long_to_short:
            cand_short = long_to_short[cand_l]
        else:
            cand_short = None
        candidate_meta.append((long_form, cand_short))
    return candidate_meta


def _score_keyword_candidates(
    chunk_embedding: list[float],
    candidate_meta: list[tuple[str, str | None]],
    cand_vecs: list[list[float]],
) -> list[dict[str, Any]]:
    """Score one chunk's slice without changing candidate order or dedup.

    The cosine ranking runs as numpy matrix ops (one ``mat @ chunk``
    dot-product row + one norm row for the whole candidate matrix)
    instead of a pure-Python per-candidate loop — identical scores to
    within float64 accumulation-order tolerance, and the descending
    sort is Python-side and stable, so equal scores keep their
    original candidate order exactly as before (the METHOD is
    unchanged: same candidates, same cosine, same top-K).
    """
    if not cand_vecs:
        return []
    # A ragged or wrong-dim candidate vector raises here (np.asarray
    # / matmul) — same per-chunk failure boundary the old strict zip
    # enforced.
    chunk = np.asarray(chunk_embedding, dtype=np.float64)
    mat = np.asarray(cand_vecs, dtype=np.float64)
    denom = float(np.linalg.norm(chunk)) * np.linalg.norm(mat, axis=1)
    dots = mat @ chunk
    scores = np.divide(dots, denom, out=np.zeros(dots.shape), where=denom != 0.0)
    scored: list[tuple[float, str, str | None]] = [
        (float(s), long_form, cand_short)
        for s, (long_form, cand_short) in zip(scores, candidate_meta, strict=True)
    ]

    scored.sort(key=lambda t: t[0], reverse=True)

    # Dedupe by abbrev pair: if the top-K contains both the short
    # ("MOF") and the long ("metal-organic framework") of the same
    # abbrev, keep just one entry (the higher-scoring). Walk top-down,
    # track seen abbrev-keys.
    seen_keys: set[str] = set()
    kept: list[dict[str, Any]] = []
    for score, long_form, cand_short in scored:
        key = (cand_short or long_form).lower()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        kept.append({"short": cand_short, "long": long_form, "score": score})
        if len(kept) >= _TOP_K:
            break
    return kept


def write_chunk_keywords(
    conn: Connection,
    chunk_id: int,
    *,
    keywords: list[dict[str, Any]],
    embedder_name: str,
) -> None:
    """Persist the keywords payload onto the chunk row.

    Writes both ``chunks.keywords`` (the canonical TEXT[] for GIN
    lookup + Jaccard) and ``chunks.keywords_meta`` (the rich JSONB
    envelope including version + embedder for the lazy-update path).

    Guarded by the same predicate :func:`claim_chunks_without_keywords`
    claims on (``keywords IS NULL OR`` version/``content_sha`` stale): the
    claim's row lock is released well before this write runs (see
    :func:`run_chunk_keywords_pass`'s claim-commit-first phase). With the
    pass's single-runner advisory lock (also documented there), two hosts
    processing the same chunk is a genuinely rare edge case now (a
    lock-handoff window right at pass boundary, not the fleet-normal
    collision it would be without that lock) — belt-and-braces, not the
    primary defense. Without the guard, whichever write lands second would
    clobber the first with equivalent-but-not-identical data; with it, a
    redundant write is a cheap no-op (0 rows affected, no error) instead.
    """
    canonical = [
        (k.get("short") or k.get("long") or "").lower().strip() for k in keywords
    ]
    canonical = [c for c in canonical if c]
    meta = {
        "version": KEYWORDS_VERSION,
        "embedder": embedder_name,
        "keywords": keywords,
    }
    # Stamp the chunk's current content_sha into the envelope (column ref,
    # so it's the locked row's value — race-free) for the re-derive claim.
    # NB: use ``|| jsonb_build_object(...)`` rather than ``jsonb_set(...,
    # to_jsonb(content_sha), ...)``. When ``content_sha`` is SQL NULL (a
    # chunk that never had its hash stamped), ``to_jsonb(NULL)`` is SQL
    # NULL and ``jsonb_set`` then returns NULL — nulling the *entire*
    # envelope (version included), which makes the claim query
    # (``keywords_meta->>'version' IS DISTINCT FROM …``) re-claim the row
    # forever (a spin loop). ``jsonb_build_object`` maps a NULL value to a
    # JSON ``null`` and the ``||`` merge preserves the rest of the envelope.
    conn.execute(
        """
        UPDATE chunks
           SET keywords = %s,
               keywords_meta =
                   %s::jsonb || jsonb_build_object('content_sha', content_sha)
         WHERE chunk_id = %s
           AND (
                keywords IS NULL
             OR (keywords_meta->>'version') IS DISTINCT FROM %s
             OR (keywords_meta->>'content_sha') IS DISTINCT FROM content_sha
           )
        """,
        (canonical, Jsonb(meta), chunk_id, KEYWORDS_VERSION),
    )


def write_chunk_keywords_bulk(
    conn: Connection,
    items: list[tuple[int, list[dict[str, Any]]]],
    *,
    embedder_name: str,
) -> int:
    """One ``UPDATE ... FROM (VALUES ...)`` for a slice of scored chunks.

    The dedicated lane's write path (gr550973): a multi-row statement
    replaces ``len(items)`` per-chunk write transactions — same
    columns, same payload shape as :func:`write_chunk_keywords`
    (canonical ``keywords`` TEXT[] + versioned ``keywords_meta``
    envelope), and the same stale guard
    (``keywords IS NULL OR`` version/``content_sha`` drift), so a
    duplicate claim landing in the post-commit window stays a no-op.
    ``items`` is ``[(chunk_id, keywords), ...]``; callers bound each
    call to :data:`_BULK_WRITE_MAX_ROWS` rows. The ``content_sha``
    stamp is the row's own column value at write time, race-free —
    same ``|| jsonb_build_object`` merge the single-row writer uses
    (NULL ``content_sha`` maps to JSON ``null``, not a NULLed
    envelope).

    ``keywords``/``keywords_meta`` are UPDATE-safe on ``chunks``: the
    body-chunk append-only trigger (migration 0068, see
    ``docs/conventions/invariants.md``) forbids mutating ``text`` and
    the identity columns only. Returns the UPDATE's rowcount — rows
    skipped by the stale guard simply don't count.
    """
    if not items:
        return 0
    values: list[str] = []
    params: list[Any] = []
    for chunk_id, keywords in items:
        canonical = [
            (k.get("short") or k.get("long") or "").lower().strip() for k in keywords
        ]
        canonical = [c for c in canonical if c]
        meta = {
            "version": KEYWORDS_VERSION,
            "embedder": embedder_name,
            "keywords": keywords,
        }
        values.append("(%s::bigint, %s::text[], %s::jsonb)")
        params.extend([chunk_id, canonical, Jsonb(meta)])
    cur = conn.execute(
        f"""
        UPDATE chunks c
           SET keywords = v.keywords,
               keywords_meta =
                   v.keywords_meta || jsonb_build_object('content_sha', c.content_sha)
          FROM (VALUES {", ".join(values)}) AS v(chunk_id, keywords, keywords_meta)
         WHERE c.chunk_id = v.chunk_id
           AND (
                c.keywords IS NULL
             OR (c.keywords_meta->>'version') IS DISTINCT FROM %s
             OR (c.keywords_meta->>'content_sha') IS DISTINCT FROM c.content_sha
           )
        """,
        (*params, KEYWORDS_VERSION),
    )
    return cur.rowcount


def _ensure_paper_abbrevs_retrying(store: Store, ref_id: int) -> dict[str, str]:
    """:func:`ensure_paper_abbrevs`, in its own short transaction wrapped
    in :func:`retry_locked` (see :func:`run_chunk_keywords_pass`'s
    docstring for why: its ``UPDATE refs`` write can contend with a
    sibling host's own meta write for the same ref)."""

    def _do() -> dict[str, str]:
        with store.pool.connection() as conn:
            result = ensure_paper_abbrevs(conn, ref_id)
            conn.commit()
            return result

    return retry_locked(_do, label=f"chunk_keywords abbrevs ref_id={ref_id}")


def _run_chunk_keywords_batch(
    store: Store,
    embedder: Embedder,
    *,
    batch_size: int,
    max_seconds: float,
    include_stale: bool = True,
    stale_claimed: list[int] | None = None,
    shard_count: int = 1,
    shard_index: int = 0,
    should_stop: Callable[[], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, int]:
    """The claim + per-chunk work. On the shared tick this runs while
    the caller (:func:`run_chunk_keywords_pass`) holds the single-runner
    advisory lock for the whole pass; the dedicated lane runs it
    lock-free. Either way every DB access in here goes through
    ``store``'s own pool (short, independent transactions), never a
    lock-holding connection, and no transaction stays open across an
    embedder call.

    Wall-clock cap: ``max_seconds`` bounds the whole body. The deadline
    is checked per claimed row and before the last embed call, so a big
    ``batch_size`` cannot hold the advisory lock for minutes. Chunks
    claimed but not reached before the deadline — or queued behind an
    unrun embed call — simply stay unwritten and re-claim next pass;
    the claim's row lock already released at commit and the claim's
    newest-first ordering keeps them near the head of the queue.

    ``should_stop`` is the dedicated lane's SIGINT/SIGTERM flag —
    checked alongside the deadline per claimed row, between write
    slices, and before the drain flush, so a stop lands within seconds
    even mid-batch (``None`` on the shared tick: the rotation's own
    loop owns stop cadence there).

    Three phases (mirrors ``runner.run_handler_once``'s claim-commit-
    first discipline, and gripe: prod's ``lock_timeout=5s`` — see
    ``precis.utils.db_retry``'s module docstring):

    1. **Claim** (short txn): :func:`claim_chunks_without_keywords`
       leases the batch's ``FOR UPDATE OF c SKIP LOCKED`` rows, then
       commits immediately — releasing the lock well before the slow
       per-chunk work below runs, not held for the whole batch as the
       pre-fix single-transaction version did. ``include_stale`` gates
       the seq-scanning tier-3 claim (see ``_STALE_CLAIM_EVERY``);
       ``stale_claimed`` is forwarded to the claim so the caller can
       see how much tier 3 contributed. ``shard_count``/``shard_index``
       scope the claim to this lane's ``chunk_id`` residue class.
    2. **Abbrevs + batched embed** (no DB connection held across an
       embedder call): per row, :func:`_ensure_paper_abbrevs_retrying`
       (per ref, cached — its own short retried transaction) and RAKE
       candidates. Candidate *texts* dedupe across the whole pass into
       ``vec_by_text`` — same text ⇒ same vector ⇒ same score, so each
       unique phrase embeds once and is shared by every chunk that
       produced it (the abbrev short→long mapping alone duplicates
       texts within one chunk's pool). Whenever the queue of unseen
       texts reaches :data:`_EMBED_CALL_MAX_TEXTS` it flushes in ONE
       ``embedder.embed`` call — a bounded few calls per pass, never
       one per chunk or per phrase — then every queued chunk scores +
       writes; see the constant's comment for why a few calls beat one
       giant call.
    3. **Write** (one short txn per :data:`_BULK_WRITE_MAX_ROWS` slice,
       wrapped in :func:`retry_locked`):
       :func:`write_chunk_keywords_bulk`'s ``UPDATE ... FROM
       (VALUES ...)`` carries the same stale guard as
       :func:`write_chunk_keywords` (see its docstring), so a
       double-claim landing in the post-commit window — a crash, a
       same-shard restart, a shared-tick pass racing the lane — is a
       cheap no-op instead of two writers racing the same row. A
       non-lock bulk failure falls back to per-chunk
       :func:`write_chunk_keywords` so one bad row can't sink the
       slice.

    Returns ``{"claimed": N, "ok": K, "failed": F}``. ``claimed`` is the
    number of chunks the claim query returned; ``ok``/``failed`` do NOT
    always sum to ``claimed`` — a chunk that hits residual lock
    contention after :func:`retry_locked`'s own retry budget is counted
    as neither (see the ``LockNotAvailable``/``DeadlockDetected`` branch
    below): it's still unwritten, so it re-claims cleanly next pass.
    Chunks past the wall-clock deadline report the same way.
    """
    idle = {"claimed": 0, "ok": 0, "failed": 0}
    deadline = clock() + max_seconds
    if clock() >= deadline:
        return idle
    with store.pool.connection() as conn:
        rows = claim_chunks_without_keywords(
            conn,
            limit=batch_size,
            include_stale=include_stale,
            stale_claimed=stale_claimed,
            shard_count=shard_count,
            shard_index=shard_index,
        )
        conn.commit()
    claimed = len(rows)
    if not rows:
        return idle

    ok = failed = 0
    abbrev_cache: dict[int, dict[str, str]] = {}
    # text -> vector, shared across the pass; a candidate already
    # embedded earlier scores from cache, never re-embeds.
    vec_by_text: dict[str, list[float]] = {}
    pending_texts: list[str] = []
    pending_set: set[str] = set()
    pending_items: list[tuple[int, list[float], list[tuple[str, str | None]]]] = []

    def _stopped() -> bool:
        return should_stop is not None and should_stop()

    def _write_scored(scored: list[tuple[int, list[dict[str, Any]]]]) -> None:
        """Bulk-write scored chunks in :data:`_BULK_WRITE_MAX_ROWS`
        slices; a non-lock slice failure falls back to per-chunk
        writes so one bad row can't sink the slice."""
        nonlocal ok, failed
        for i in range(0, len(scored), _BULK_WRITE_MAX_ROWS):
            if _stopped():
                return
            part = scored[i : i + _BULK_WRITE_MAX_ROWS]

            def _bulk(
                part: list[tuple[int, list[dict[str, Any]]]] = part,
            ) -> None:
                with store.pool.connection() as wconn:
                    write_chunk_keywords_bulk(wconn, part, embedder_name=embedder.model)
                    wconn.commit()

            try:
                retry_locked(_bulk, label="chunk_keywords bulk write")
                ok += len(part)
                continue
            except (LockNotAvailable, DeadlockDetected) as exc:
                # Residual lock contention that outlasted retry_locked's
                # own budget. Expected fleet contention under prod's
                # lock_timeout=5s, not a crash: log at warning (not
                # log.exception), and don't count the chunks ``failed``
                # — they never got written, so they stay claimable and
                # pick back up next pass instead of being permanently
                # marked bad.
                log.warning(
                    "chunk_keywords: %d chunks deferred after residual "
                    "lock contention: %s",
                    len(part),
                    exc,
                )
                continue
            except Exception:
                log.exception(
                    "chunk_keywords: bulk write failed — per-chunk "
                    "fallback for %d chunks",
                    len(part),
                )
            for chunk_id, keywords in part:
                try:

                    def _write(
                        chunk_id: int = chunk_id,
                        keywords: list[dict[str, Any]] = keywords,
                    ) -> None:
                        with store.pool.connection() as wconn:
                            write_chunk_keywords(
                                wconn,
                                chunk_id,
                                keywords=keywords,
                                embedder_name=embedder.model,
                            )
                            wconn.commit()

                    retry_locked(_write, label=f"chunk_keywords chunk_id={chunk_id}")
                    ok += 1
                except (LockNotAvailable, DeadlockDetected) as exc:
                    log.warning(
                        "chunk_keywords: chunk_id=%s deferred after "
                        "residual lock contention: %s",
                        chunk_id,
                        exc,
                    )
                except Exception:
                    log.exception("chunk_keywords: chunk_id=%s failed", chunk_id)
                    failed += 1

    def _flush() -> None:
        """One embed call for the queued unique texts, then score +
        write every chunk they unblock."""
        nonlocal pending_texts, pending_set, pending_items, failed
        if pending_texts:
            vecs = embedder.embed(pending_texts)
            if len(vecs) != len(pending_texts):
                raise ValueError(
                    f"embedder returned {len(vecs)} vectors for "
                    f"{len(pending_texts)} keyword candidates"
                )
            vec_by_text.update(zip(pending_texts, vecs, strict=True))
            pending_texts = []
            pending_set = set()
        items, pending_items = pending_items, []
        scored: list[tuple[int, list[dict[str, Any]]]] = []
        for chunk_id, chunk_vec, candidates in items:
            try:
                keywords = _score_keyword_candidates(
                    chunk_vec,
                    candidates,
                    [vec_by_text[long_form] for long_form, _ in candidates],
                )
            except Exception:
                log.exception("chunk_keywords: chunk_id=%s failed", chunk_id)
                failed += 1
                continue
            scored.append((chunk_id, keywords))
        _write_scored(scored)

    def _flush_or_defer() -> dict[str, int] | None:
        """Run :func:`_flush`; on embedder-level failure return the pass
        result to propagate, ``None`` on success."""
        try:
            _flush()
        except EmbedderUnavailable as exc:
            # The embedder is transiently down/busy — defer: queued
            # chunks stay unwritten and re-claim next pass. Same
            # ``claimed=0`` shape as a lock miss, so the run-loop backs
            # off rather than hot re-polling into a down embedder.
            # Slices already written stay written — the write guard
            # makes a redundant write a no-op anyway.
            log.warning(
                "chunk_keywords: embedder unavailable (%s); deferring "
                "batch of %d chunks",
                exc,
                claimed,
            )
            return idle
        except Exception:
            log.exception("chunk_keywords: candidate embedding batch failed")
            return {
                "claimed": claimed,
                "ok": ok,
                "failed": failed + len(pending_items),
            }
        return None

    for chunk_id, ref_id, text, vec_text in rows:
        if clock() >= deadline or _stopped():
            break
        try:
            if vec_text is None:
                raise RuntimeError(
                    "chunk has no bge-m3 embedding yet — run `precis worker --only embed` first"
                )
            # pgvector returns the vector as a string of the form
            # "[0.1, 0.2, ...]" when cast to text; parse it.
            chunk_vec = _parse_pgvector_text(vec_text)

            if ref_id not in abbrev_cache:
                abbrev_cache[ref_id] = _ensure_paper_abbrevs_retrying(store, ref_id)

            candidates = _prepare_keyword_candidates(text, abbrev_cache[ref_id])
        except (LockNotAvailable, DeadlockDetected) as exc:
            log.warning(
                "chunk_keywords: chunk_id=%s deferred after residual lock "
                "contention: %s",
                chunk_id,
                exc,
            )
            continue
        except Exception:
            log.exception("chunk_keywords: chunk_id=%s failed", chunk_id)
            failed += 1
            continue
        pending_items.append((chunk_id, chunk_vec, candidates))
        for long_form, _ in candidates:
            if long_form not in vec_by_text and long_form not in pending_set:
                pending_set.add(long_form)
                pending_texts.append(long_form)
        if len(pending_texts) >= _EMBED_CALL_MAX_TEXTS:
            out = _flush_or_defer()
            if out is not None:
                return out

    # Drain the last partial slice — unless the clock already ran out
    # or a stop landed; chunks left unflushed stay unwritten and
    # re-claim next pass.
    if pending_items and clock() < deadline and not _stopped():
        out = _flush_or_defer()
        if out is not None:
            return out
    return {"claimed": claimed, "ok": ok, "failed": failed}


def run_chunk_keywords_pass(
    store: Store,
    embedder: Embedder,
    *,
    batch_size: int | None = None,
    max_seconds: float | None = None,
    single_runner: bool = True,
    shard_count: int = 1,
    shard_index: int = 0,
    should_stop: Callable[[], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, int]:
    """One pass over the chunk_keywords queue.

    ``batch_size`` — chunks claimed this pass. ``None`` resolves
    :data:`_BATCH_SIZE_ENV` (default :data:`_BATCH_SIZE_DEFAULT`); the
    worker closure passes ``None`` deliberately so this knob — not the
    generic ``--batch-size`` — sizes the claim (gr549751).
    ``max_seconds`` — wall-clock cap on the pass body. ``None``
    resolves :data:`_MAX_SECONDS_ENV` (default
    :data:`_MAX_SECONDS_DEFAULT`); ``0`` claims nothing. ``clock`` is
    the deadline's time source — tests inject a fake one.
    ``single_runner`` — the shared tick's default: hold the fleet-wide
    advisory lock for the whole pass. The dedicated lane passes
    ``False`` — its N lanes shard the claim instead (``shard_count`` /
    ``shard_index``, lane ``i`` claims ``chunk_id % N = i``) so a
    lock would only serialize what the sharding already disjoints.
    ``should_stop`` — the lane's SIGINT/SIGTERM flag, forwarded to the
    batch so a stop lands mid-batch, not just between passes.

    Returns ``{"claimed": N, "ok": K, "failed": F}`` — see
    :func:`_run_chunk_keywords_batch`'s docstring for the shape and the
    claim/extract/write phases it runs. Pure pass — the caller decides
    whether to loop.

    Single-runner advisory lock (see the module docstring): ``chunk_keywords``
    runs on every system-profile host on the same ~2s tick, all ordering
    candidates by ``chunk_id`` — without exclusivity, every host would
    re-claim and re-derive the same batch every cycle. A dedicated
    connection (:func:`precis.store.advisory.try_xact_advisory_lock`) holds
    a **transaction-scoped** ``pg_try_advisory_xact_lock`` for the WHOLE
    pass (this function's entire body, not just the claim step) and rolls
    it back on exit (gr463966: session-scoped locks are broken behind
    pgbouncer transaction pooling). The helper lifts
    ``idle_in_transaction_session_timeout`` for that transaction only, so
    prod's ``300s`` cap cannot kill a slow pass's lock; if the process dies
    outright, pgbouncer closes the orphaned server connection and the lock
    goes with it. That dedicated connection ONLY ever holds the lock;
    :func:`_run_chunk_keywords_batch` does every real read/write through
    ``store``'s own pool, on separate connections. A lock-acquire miss
    (another host owns this tick) is a graceful skip — same ``claimed=0``
    shape as the ``EmbedderUnavailable`` branch, not a failure.

    The tier-3 stale re-derive claim is paced (see
    :data:`_STALE_CLAIM_EVERY` and :data:`_stale_draining`): it
    seq-scans ``chunk_embeddings``, so it only runs every pass while a
    live stale backlog is draining.
    """
    idle: dict[str, int] = {"claimed": 0, "ok": 0, "failed": 0}
    if not store.dsn:
        return idle

    resolved_batch_size = (
        batch_size if batch_size is not None else _batch_size_default()
    )
    resolved_max_seconds = (
        max_seconds if max_seconds is not None else _max_seconds_default()
    )

    def _pass_once() -> dict[str, int]:
        global _pass_count, _stale_draining
        _pass_count += 1
        stale: list[int] = []
        result = _run_chunk_keywords_batch(
            store,
            embedder,
            batch_size=resolved_batch_size,
            max_seconds=resolved_max_seconds,
            include_stale=_stale_draining or (_pass_count % _STALE_CLAIM_EVERY) == 0,
            stale_claimed=stale,
            shard_count=shard_count,
            shard_index=shard_index,
            should_stop=should_stop,
            clock=clock,
        )
        # The tier-3 claim only reports when it actually ran; a pass
        # filled by the earlier tiers leaves _stale_draining untouched.
        if stale:
            _stale_draining = stale[0] > 0
        return result

    if not single_runner:
        return _pass_once()
    with try_xact_advisory_lock(store.dsn, _LOCK_KEY) as got:
        if not got:
            return idle  # another node owns this pass this cycle
        return _pass_once()


def run_dedicated_loop(
    store: Store,
    embedder: Embedder,
    *,
    batch_size: int | None = None,
    idle_seconds: float | None = None,
    max_seconds: float | None = None,
    shard_count: int | None = None,
    shard_index: int | None = None,
    should_stop: Callable[[], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """The dedicated chunk_keywords drain lane (gr520236 / gr550973):
    one queue, one process, no advisory lock.

    Loop semantics — claim a batch; if it wrote at least half of what
    it claimed, loop immediately; otherwise sleep ``idle_seconds``
    and check again ("rest when done, check once a minute"). The
    ``ok < claimed/2`` back-off keeps a poison batch — a mostly-
    failing or mostly-deferred claim, or one cut short by the stop
    flag — from hot-looping the same stuck head of queue. Each
    iteration is :func:`run_chunk_keywords_pass` with
    ``single_runner=False`` — the lane's shard clause (lane ``i`` of
    ``N`` claims ``chunk_id % N = i``; N is GLOBAL — hosts × lanes,
    see playbook 20g) is the partition, ``FOR UPDATE OF c SKIP
    LOCKED`` arbitrates the claim instant, and the write's stale
    guard makes a post-commit-window duplicate a no-op; the claim's
    short transaction commits before any embedder call, so no
    transaction is ever open across the slow work.

    ``None`` arguments resolve env: ``batch_size`` ←
    ``PRECIS_CHUNK_KEYWORDS_DEDICATED_BATCH_SIZE`` (default 500),
    ``idle_seconds`` ← ``PRECIS_CHUNK_KEYWORDS_DEDICATED_IDLE_SECONDS``
    (default 60), ``max_seconds`` ←
    ``PRECIS_CHUNK_KEYWORDS_DEDICATED_MAX_SECONDS`` (default 900),
    ``shard_count``/``shard_index`` ← ``PRECIS_CHUNK_KEYWORDS_SHARDS``
    / ``PRECIS_CHUNK_KEYWORDS_SHARD_INDEX`` (default 1/0).

    Each pass's result is logged through the ``precis.workers.runner``
    logger with the same ``worker: chunk_keywords claimed=N ok=K
    failed=F`` ``extra={'payload': ...}`` shape ``run_loop`` emits, so
    ``worker_logs`` keeps the identical drain ledger the health
    probes read (``pass='runner'`` rows keyed on
    ``payload->>'handler'``); ``PRECIS_PROCESS`` is what distinguishes
    this lane's rows from a shared worker's.

    ``should_stop`` is the SIGINT/SIGTERM flag — checked between
    passes, during the idle sleep, and inside the pass's own batch
    row loop (forwarded to :func:`run_chunk_keywords_pass`), so a
    stop lands within seconds even mid-batch.
    ``sleep``/``clock`` are injectable for tests.
    """
    resolved_batch = (
        batch_size
        if batch_size is not None
        else env_int(_DEDICATED_BATCH_SIZE_ENV, _DEDICATED_BATCH_SIZE_DEFAULT, lo=1)
    )
    resolved_idle = (
        idle_seconds
        if idle_seconds is not None
        else env_float(_DEDICATED_IDLE_ENV, _DEDICATED_IDLE_DEFAULT, lo=0.0)
    )
    resolved_max = (
        max_seconds
        if max_seconds is not None
        else env_float(
            _DEDICATED_MAX_SECONDS_ENV, _DEDICATED_MAX_SECONDS_DEFAULT, lo=0.0
        )
    )
    resolved_shards = (
        shard_count if shard_count is not None else env_int(_SHARDS_ENV, 1, lo=1)
    )
    resolved_index = (
        shard_index if shard_index is not None else env_int(_SHARD_INDEX_ENV, 0, lo=0)
    )
    if not 0 <= resolved_index < resolved_shards:
        raise ValueError(
            f"shard_index ({resolved_index}) must satisfy "
            f"0 <= shard_index < shard_count ({resolved_shards})"
        )

    runner_log = logging.getLogger("precis.workers.runner")
    log.info(
        "chunk_keywords: dedicated lane starting (batch=%d idle=%.0fs "
        "max=%.0fs shard %d/%d)",
        resolved_batch,
        resolved_idle,
        resolved_max,
        resolved_index,
        resolved_shards,
    )
    while True:
        if should_stop is not None and should_stop():
            return
        t0 = clock()
        result = run_chunk_keywords_pass(
            store,
            embedder,
            batch_size=resolved_batch,
            max_seconds=resolved_max,
            single_runner=False,
            shard_count=resolved_shards,
            shard_index=resolved_index,
            should_stop=should_stop,
            clock=clock,
        )
        runner_log.info(
            "worker: chunk_keywords claimed=%d ok=%d failed=%d",
            result["claimed"],
            result["ok"],
            result["failed"],
            extra={
                "payload": {
                    "handler": "chunk_keywords",
                    "claimed": result["claimed"],
                    "ok": result["ok"],
                    "failed": result["failed"],
                    "report_only": False,
                    "duration_ms": int((clock() - t0) * 1000),
                }
            },
        )
        if result["claimed"] > 0 and result["ok"] * 2 >= result["claimed"]:
            continue
        # Empty queue — OR a mostly-poison batch that claimed rows but
        # wrote fewer than half of them (residual lock contention,
        # failures, or the deadline/stop flag cutting the pass short):
        # rest until the next check either way — looping on a
        # low-yield batch would hot-poll the same stuck head of
        # queue. In short slices so a stop signal lands inside the
        # sleep.
        slept = 0.0
        while slept < resolved_idle:
            if should_stop is not None and should_stop():
                return
            tick = min(0.25, resolved_idle - slept)
            if tick <= 0:
                break
            sleep(tick)
            slept += tick


# ── helpers ─────────────────────────────────────────────────────────


def _parse_pgvector_text(s: str) -> list[float]:
    """Parse a pgvector text representation: ``"[0.1, 0.2, ...]"``."""
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [float(x) for x in s.split(",") if x.strip()]


__all__ = [
    "KEYWORDS_VERSION",
    "MIN_CHUNK_CHARS",
    "PENDING_INDEX_DDL",
    "SKIP_KINDS",
    "claim_chunks_without_keywords",
    "dedicated_lane_enabled",
    "ensure_paper_abbrevs",
    "extract_chunk_keywords",
    "is_keyword_eligible",
    "keyword_eligible_chunk_count",
    "run_chunk_keywords_pass",
    "run_dedicated_loop",
    "unkeyworded_chunk_count",
    "write_chunk_keywords",
    "write_chunk_keywords_bulk",
]
