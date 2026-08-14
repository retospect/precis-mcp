"""Store ops for the chunk-review ledger — the ``chunk_review`` table
(migration 0086) backing the draft-chunk approval workflow: a per-
``(chunk, checker)`` approval watermark keyed on ``content_sha``.
"Requires review" is a derived query — current content_sha != the
approved_sha the checker last recorded (or no row at all).

Carved out of :class:`precis.store._draft_ops.DraftStore` — the first
cut of that module's decomposition (see
``docs/backlog/codereview-handler-size-cleanups.md``) — as a further
composed sub-store, reached as ``store.drafts.review``. Holds its own
:class:`~precis.store.core.StoreCore` reference (like
:class:`~precis.store._draft_ops.DraftStore` itself) plus a
back-reference to the host :class:`~precis.store._draft_ops.DraftStore`
for the handful of ops that reuse its structural helpers
(``reading_order``, ``_descendant_ids``) rather than duplicating them.

:class:`~precis.store._draft_ops.DraftStore` still exposes every
method here under its own flat name too (``store.drafts.record_review(
...)`` etc.) via a transitional delegation block — those delegations
are deleted one by one as call sites migrate to
``store.drafts.review.*``.
"""

from __future__ import annotations

import difflib
import hashlib
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg_pool import ConnectionPool

from precis.errors import BadInput, NotFound
from precis.store.core import StoreCore
from precis.utils.wordcount import PROSE_CHUNK_KINDS

if TYPE_CHECKING:
    from precis.store._draft_ops import DraftStore


@dataclass(frozen=True, slots=True)
class ReviewableChunk:
    """One live, reviewable chunk stub — the ``(chunk_id, handle,
    chunk_kind)`` triple :meth:`DraftReviewStore.reviewable_chunks`
    returns. Walked by the review fanout (``quest/review_fanout.py::
    mint_review_fanout``) to decide which ``(chunk, lens)`` pairs to mint
    review-todos for, independent of any checker's ledger state."""

    chunk_id: int
    handle: str
    chunk_kind: str


@dataclass(frozen=True, slots=True)
class ChunkReviewEntry:
    """One checker's ledger row — a ``chunk_review`` row (migration 0086)
    joined against the chunk's current ``content_sha`` to derive
    ``dirty``. Returned by :meth:`DraftReviewStore.review_status_for_chunk`
    (one chunk, every checker that has ever reviewed it — a checker with
    no row simply doesn't appear) and reused, per-row, inside
    :meth:`DraftReviewStore.review_status_for_draft`'s
    :class:`DraftReviewRow` (where ``checker is None`` instead marks a
    chunk with no ledger rows at all)."""

    checker: str | None
    approved_sha: str | None
    verdict: str | None
    at: datetime | None
    dirty: bool


@dataclass(frozen=True, slots=True)
class DraftReviewRow:
    """One ``(chunk, checker)`` row of the whole-draft review ledger —
    :meth:`DraftReviewStore.review_status_for_draft`'s return shape.
    Carries the chunk's own fields (denormalized onto every checker row
    of that chunk) plus one checker's :class:`ChunkReviewEntry` fields
    flattened in, so a flat-list walk never needs a second lookup. A
    chunk with no ledger rows at all still appears once, with
    ``checker=None`` (LEFT JOIN) — ``dirty`` is then ``True`` (never
    reviewed)."""

    chunk_id: int
    handle: str
    chunk_kind: str
    #: Nearest enclosing HEADING chunk id (ancestor walk, self excluded;
    #: ``None`` for a chunk with no heading ancestor) — the id a
    #: paragraph's rollup uses to pull in its section's
    #: ``structure``/``adversarial`` state ("via section").
    section_chunk_id: int | None
    checker: str | None
    approved_sha: str | None
    verdict: str | None
    at: datetime | None
    dirty: bool


class DraftReviewStore:
    """Composed sub-store for the chunk-review ledger — reached as
    ``store.drafts.review``. Holds a reference to the shared
    :class:`~precis.store.core.StoreCore` (pool/tx lifecycle) rather
    than its own pool, and a back-reference to the host
    :class:`~precis.store._draft_ops.DraftStore` for the ops that reuse
    its structural helpers (``reading_order``, ``_descendant_ids``)."""

    def __init__(self, core: StoreCore, *, host: DraftStore) -> None:
        self._core = core
        self._host = host

    @property
    def pool(self) -> ConnectionPool:
        return self._core.pool

    def tx(self) -> AbstractContextManager[psycopg.Connection]:
        return self._core.tx()

    def record_review(
        self, chunk_id: int, checker: str, *, verdict: str = "approved"
    ) -> str:
        """Record that ``checker`` evaluated ``chunk_id`` at its *current*
        content_sha, with the given ``verdict`` (free text). Upserts on
        ``(chunk_id, checker)`` — a re-review overwrites the prior
        approved_sha/verdict/at. Returns the recorded sha.

        Only draft-family chunks (non-NULL ``content_sha``) are reviewable
        — a body/paper chunk invalidates by row identity, not by sha, so
        this ledger doesn't apply to it."""
        with self.tx() as conn:
            row = conn.execute(
                "SELECT content_sha FROM chunks WHERE chunk_id = %s", (chunk_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"no chunk {chunk_id}")
            sha = row[0]
            if sha is None:
                raise BadInput(
                    f"chunk {chunk_id} has no content_sha — the review ledger "
                    "only tracks draft-family chunks (a body/paper chunk "
                    "invalidates by row identity, not content_sha)",
                    next="review a draft chunk: edit(kind='draft', "
                    "id='dc<id>', review='human')",
                )
            conn.execute(
                """INSERT INTO chunk_review (chunk_id, checker, approved_sha, verdict)
                        VALUES (%s, %s, %s, %s)
                   ON CONFLICT (chunk_id, checker) DO UPDATE
                          SET approved_sha = EXCLUDED.approved_sha,
                              verdict = EXCLUDED.verdict,
                              at = now()""",
                (chunk_id, checker, sha, verdict),
            )
        return str(sha)

    def retract_review(self, chunk_id: int, checker: str) -> bool:
        """Delete the ``chunk_review`` row for ``(chunk_id, checker)`` — the
        un-review op (spec item 7): retracting a human ✓ (or, generically,
        any checker's approval) reverts the chunk to "requires review".
        Returns whether a row existed to delete."""
        with self.tx() as conn:
            rc = conn.execute(
                "DELETE FROM chunk_review WHERE chunk_id = %s AND checker = %s",
                (chunk_id, checker),
            ).rowcount
        return rc > 0

    def approved_pairs_at_current_sha(self, ref_id: int) -> set[tuple[int, str]]:
        """``{(chunk_id, checker)}`` for every ledger row of ``ref_id``'s
        live chunks that is approved at the chunk's *current* content_sha —
        the incremental fanout's (item 1) ``only_dirty`` skip set: a pair
        already here passed at this exact text, so re-minting it would just
        re-run the same check against the same words."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT cr.chunk_id, cr.checker
                  FROM chunk_review cr
                  JOIN chunks c ON c.chunk_id = cr.chunk_id
                 WHERE c.ref_id = %s
                   AND c.retired_at IS NULL
                   AND cr.approved_sha = c.content_sha
                   AND cr.verdict = 'approved'
                """,
                (ref_id,),
            ).fetchall()
        return {(int(r[0]), str(r[1])) for r in rows}

    def review_subtree_chunk_ids(self, ref_id: int, heading_chunk_id: int) -> list[int]:
        """The heading chunk itself + all live descendant chunks, in
        document order — the incremental fanout's (item 1) subtree scope.

        Reuses :meth:`DraftStore._descendant_ids` (the recursive family
        walk backing :meth:`DraftStore.draft_subtree_chunk_ids`) for the
        descendant set and :meth:`DraftStore.reading_order` (the DFS
        backing the fisheye/section render, ``utils/fisheye.py``) for the
        document ordering, rather than a new tree walk. Empty when
        ``heading_chunk_id`` doesn't resolve to a live chunk of ``ref_id``
        (matching :meth:`DraftStore.draft_subtree_chunk_ids`'s "empty when
        the handle is unknown")."""
        with self.pool.connection() as conn:
            descendant_ids = set(self._host._descendant_ids(conn, heading_chunk_id))
        descendant_ids.add(heading_chunk_id)
        return [
            c.chunk_id
            for c in self._host.reading_order(ref_id)
            if c.chunk_id in descendant_ids
        ]

    def toc_digest(self, ref_id: int) -> str:
        """Hex sha256 over the ordered ``(chunk_id, content_sha)`` list of
        ``ref_id``'s live HEADING chunks, in document order (item 10 — the
        ``toc`` lens's approval pins to this instead of any single chunk's
        sha). Adding/removing/renaming/reordering a section changes the
        digest; editing a paragraph's body does not — deliberately excludes
        body text and word counts (balance drift is the deterministic
        wordcount stats' job, not this digest's)."""
        headings = [
            c.chunk_id
            for c in self._host.reading_order(ref_id)
            if c.chunk_kind == "heading"
        ]
        sha_by_id: dict[int, str | None] = {}
        if headings:
            with self.pool.connection() as conn:
                rows = conn.execute(
                    "SELECT chunk_id, content_sha FROM chunks WHERE chunk_id = ANY(%s)",
                    (headings,),
                ).fetchall()
            sha_by_id = {int(r[0]): r[1] for r in rows}
        h = hashlib.sha256()
        for chunk_id in headings:
            h.update(f"{chunk_id}:{sha_by_id.get(chunk_id) or ''}\n".encode())
        return h.hexdigest()

    def chunks_requiring_review(
        self, ref_id: int, checker: str
    ) -> list[dict[str, Any]]:
        """Live draft chunks of ``ref_id`` that are dirty for ``checker`` —
        never reviewed, or reviewed at a sha other than the chunk's current
        one. Same NOT-EXISTS shape as the workers' fresh-claim predicate
        (``workers/base.py:_claim_fresh``), keyed on `chunk_review` instead
        of a typed artifact table."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT c.chunk_id, c.handle, c.chunk_kind, c.text
                  FROM chunks c
                 WHERE c.ref_id = %(ref_id)s
                   AND c.content_sha IS NOT NULL
                   AND c.retired_at IS NULL
                   AND NOT EXISTS (
                           SELECT 1 FROM chunk_review r
                            WHERE r.chunk_id = c.chunk_id
                              AND r.checker = %(checker)s
                              AND r.approved_sha IS NOT DISTINCT FROM c.content_sha
                       )
                 ORDER BY c.chunk_id
                """,
                {"ref_id": ref_id, "checker": checker},
            ).fetchall()
        return [
            {
                "chunk_id": int(r[0]),
                "handle": r[1],
                "chunk_kind": r[2],
                "text": r[3] or "",
            }
            for r in rows
        ]

    def reviewable_chunks(self, ref_id: int) -> list[ReviewableChunk]:
        """Every live, reviewable chunk of ``ref_id`` — draft-family chunks
        with a non-NULL ``content_sha`` (the same population
        :meth:`chunks_requiring_review` / :meth:`review_status_for_draft`
        scope to), regardless of any checker's ledger state.

        Unlike :meth:`chunks_requiring_review`, this is not filtered by
        ``checker`` — it's the whole-draft chunk list a fanout (rung 3a
        ``mint_review_fanout``) walks to mint one review-todo per
        ``(chunk, lens)``, independent of dirty/clean status."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT c.chunk_id, c.handle, c.chunk_kind
                  FROM chunks c
                 WHERE c.ref_id = %s
                   AND c.content_sha IS NOT NULL
                   AND c.retired_at IS NULL
                 ORDER BY c.chunk_id
                """,
                (ref_id,),
            ).fetchall()
        return [
            ReviewableChunk(
                chunk_id=int(chunk_id), handle=handle, chunk_kind=chunk_kind
            )
            for chunk_id, handle, chunk_kind in rows
        ]

    def review_status_for_chunk(self, chunk_id: int) -> list[ChunkReviewEntry]:
        """Every checker's ledger row for ``chunk_id`` — ``checker``,
        ``approved_sha``, ``verdict``, ``at``, and a derived ``dirty`` bit
        (``approved_sha`` no longer matches the chunk's current
        content_sha). A checker with no row simply doesn't appear — the
        caller treats "no row" as dirty too (never reviewed)."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT content_sha FROM chunks WHERE chunk_id = %s", (chunk_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"no chunk {chunk_id}")
            current_sha = row[0]
            rows = conn.execute(
                """SELECT checker, approved_sha, verdict, at
                     FROM chunk_review
                    WHERE chunk_id = %s
                    ORDER BY checker""",
                (chunk_id,),
            ).fetchall()
        return [
            ChunkReviewEntry(
                checker=checker,
                approved_sha=approved_sha,
                verdict=verdict,
                at=at,
                dirty=approved_sha != current_sha,
            )
            for checker, approved_sha, verdict, at in rows
        ]

    def review_root_chunk_id(self, ref_id: int) -> int | None:
        """The chunk the document-level ``toc`` lens rides on (item 10) —
        the first ROOT-level chunk (``parent_chunk_id IS NULL``) in
        document order (lowest ``pos``) that satisfies the SAME
        reviewability filters :meth:`review_status_for_draft` scopes its
        ledger to: ``content_sha IS NOT NULL``, ``retired_at IS NULL``,
        ``pos IS NOT NULL``. Deliberately NOT :meth:`DraftStore.reading_order`'s
        first chunk — that has no ``content_sha`` filter, so if the
        draft's very first root chunk has a NULL ``content_sha`` (not yet
        reviewable), anchoring there would mint the ``toc`` review-todo on
        a chunk :meth:`review_status_for_draft`'s ledger never returns a
        row for — the toc indicator would then read permanently
        unapproved no matter how many times it's actually reviewed.
        SINGLE selection rule, shared by :func:`precis.quest.review_fanout.
        _mint_doc_lenses` (the toc anchor mint) and this method's own
        toc-row patch below, so the two can't drift apart. ``None`` for a
        draft with no reviewable root chunk."""
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT chunk_id FROM chunks
                 WHERE ref_id = %s AND parent_chunk_id IS NULL
                   AND retired_at IS NULL AND pos IS NOT NULL
                   AND content_sha IS NOT NULL
                 ORDER BY pos COLLATE "C" ASC
                 LIMIT 1
                """,
                (ref_id,),
            ).fetchone()
        return int(row[0]) if row else None

    def review_status_for_draft(self, ref_id: int) -> list[DraftReviewRow]:
        """Every checker's ledger row for every live, reviewable chunk of
        ``ref_id`` — the whole-draft counterpart to
        :meth:`review_status_for_chunk`, in **one** query instead of one
        per chunk (a large dossier has thousands of chunks; see
        ``render_review_view``, which used to call ``review_status_for_chunk``
        in a loop).

        A single flat fetch (``chunks`` LEFT JOIN ``chunk_review``), with
        the reading-order DFS done in Python — same technique as
        :meth:`DraftStore.reading_order` and for the same reason: a
        recursive SQL CTE re-scans the whole ref at every depth
        (≈O(N·depth), ~5.5s on a 9,700-chunk draft; see that method's
        docstring). Rows are ordered by chunk reading position (DFS
        pre-order, siblings by ``pos``) then ``checker``. A chunk with no
        review rows still appears once, with ``checker=None`` (LEFT JOIN)
        — ``dirty`` is then ``True`` (never reviewed). Chunks reachable
        only through a retired/absent parent are excluded, matching
        ``reading_order``.

        Each row also carries ``section_chunk_id`` — the nearest enclosing
        HEADING chunk id (ancestor walk, self excluded; ``None`` for a
        chunk with no heading ancestor), the id a paragraph's rollup uses to
        pull in its section's ``structure``/``adversarial`` state (item 2 —
        "via section"). The draft's first chunk in document order (there is
        no single dedicated root — a fresh draft's title heading and any
        scaffolded top-level sections are all ``parent_chunk_id IS NULL``
        siblings, see :meth:`DraftStore.create_draft`/
        :meth:`DraftStore.scaffold_sections`) also carries the
        document-level ``toc`` lens entry (item 10): its ``dirty`` is
        patched to compare the stored digest (``approved_sha``) against
        :meth:`toc_digest` — NOT the chunk's own ``content_sha`` — and a
        synthetic never-reviewed ``toc`` row is added when no
        ``chunk_review`` row exists yet, so a caller can always render a
        ``toc`` column."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT c.chunk_id, c.handle, c.chunk_kind,
                       c.parent_chunk_id, c.pos,
                       cr.checker, cr.approved_sha, cr.verdict, cr.at,
                       (cr.approved_sha IS DISTINCT FROM c.content_sha) AS dirty
                  FROM chunks c
             LEFT JOIN chunk_review cr ON cr.chunk_id = c.chunk_id
                 WHERE c.ref_id = %s
                   AND c.content_sha IS NOT NULL
                   AND c.retired_at IS NULL
                   AND c.pos IS NOT NULL
                """,
                (ref_id,),
            ).fetchall()
        # group ledger rows by chunk_id, keep each chunk's own fields once
        by_chunk: dict[int, dict[str, Any]] = {}
        for (
            chunk_id,
            handle,
            chunk_kind,
            parent_chunk_id,
            pos,
            checker,
            approved_sha,
            verdict,
            at,
            dirty,
        ) in rows:
            chunk_id = int(chunk_id)
            entry = by_chunk.setdefault(
                chunk_id,
                {
                    "handle": handle,
                    "chunk_kind": chunk_kind,
                    "parent_chunk_id": parent_chunk_id,
                    "pos": pos,
                    "reviews": [],
                },
            )
            entry["reviews"].append(
                ChunkReviewEntry(
                    checker=checker,
                    approved_sha=approved_sha,
                    verdict=verdict,
                    at=at,
                    dirty=bool(dirty),
                )
            )
        # DFS pre-order over the chunks (not a per-checker row), same shape
        # as `reading_order`: children keyed by parent_chunk_id, siblings by
        # pos (byte order), roots-first stack walk.
        children: dict[Any, list[int]] = {}
        for chunk_id, entry in by_chunk.items():
            children.setdefault(entry["parent_chunk_id"], []).append(chunk_id)
        for lst in children.values():
            lst.sort(key=lambda cid: by_chunk[cid]["pos"])

        # Nearest enclosing HEADING ancestor per chunk (self excluded),
        # memoized — same ancestor walk as ``utils/fisheye.py``'s
        # ``_ancestors``, just stopping at the first heading instead of
        # collecting the whole branch.
        section_of: dict[int, int | None] = {}

        def _section_chunk_id(cid: int) -> int | None:
            if cid in section_of:
                return section_of[cid]
            section_of[cid] = None  # cycle guard
            pid = by_chunk[cid]["parent_chunk_id"]
            seen: set[int] = set()
            result: int | None = None
            while pid is not None and pid in by_chunk and pid not in seen:
                seen.add(pid)
                if by_chunk[pid]["chunk_kind"] == "heading":
                    result = pid
                    break
                pid = by_chunk[pid]["parent_chunk_id"]
            section_of[cid] = result
            return result

        # Document-level ``toc`` entry (item 10): the first chunk in
        # document order stands in for the draft's (nonexistent) single
        # root. Patch/synthesize its ``toc`` row against the recomputed
        # digest rather than the generic content_sha comparison. Routed
        # through ``review_root_chunk_id`` (not just ``roots[0]``) so this
        # picks the SAME chunk the fanout's toc-lens mint anchors on —
        # both already apply the identical content_sha filter here (the
        # rows feeding ``roots`` were fetched with it above), but sharing
        # the helper keeps the two selections from ever drifting apart.
        roots = children.get(None, [])
        root_id = self.review_root_chunk_id(ref_id)
        if root_id is not None:
            digest = self.toc_digest(ref_id)
            root_reviews: list[ChunkReviewEntry] = by_chunk[root_id]["reviews"]
            toc_idx = next(
                (i for i, rv in enumerate(root_reviews) if rv.checker == "toc"), None
            )
            if toc_idx is not None:
                toc_row = root_reviews[toc_idx]
                root_reviews[toc_idx] = replace(
                    toc_row, dirty=toc_row.approved_sha != digest
                )
            else:
                root_reviews.append(
                    ChunkReviewEntry(
                        checker="toc",
                        approved_sha=None,
                        verdict=None,
                        at=None,
                        dirty=True,
                    )
                )

        out: list[DraftReviewRow] = []
        stack = list(reversed(roots))
        while stack:
            chunk_id = stack.pop()
            entry = by_chunk[chunk_id]
            section_chunk_id = _section_chunk_id(chunk_id)
            for review in sorted(entry["reviews"], key=lambda rv: rv.checker or ""):
                out.append(
                    DraftReviewRow(
                        chunk_id=chunk_id,
                        handle=entry["handle"],
                        chunk_kind=entry["chunk_kind"],
                        section_chunk_id=section_chunk_id,
                        checker=review.checker,
                        approved_sha=review.approved_sha,
                        verdict=review.verdict,
                        at=review.at,
                        dirty=review.dirty,
                    )
                )
            kids = children.get(chunk_id, [])
            stack.extend(reversed(kids))
        return out

    def review_rollup_for_draft(self, ref_id: int) -> dict[str, int]:
        """``{"done": N, "total": M}`` — the toolbar's ``N/M`` rollup badge
        (item 8). ``total`` counts PROSE chunks only (denominator excludes
        headings/equations/tables/terms — human sign-off isn't collected on
        them); ``done`` counts those PROSE chunks approved by
        ``checker='human'`` at their current content_sha. Built from
        :meth:`review_status_for_draft`'s rows so the two counts can't
        drift from what the per-chunk indicator renders."""
        prose: dict[int, bool] = {}
        for row in self.review_status_for_draft(ref_id):
            if row.chunk_kind not in PROSE_CHUNK_KINDS:
                continue
            prose.setdefault(row.chunk_id, False)
            if row.checker == "human" and not row.dirty:
                prose[row.chunk_id] = True
        return {"done": sum(1 for v in prose.values() if v), "total": len(prose)}

    def review_diff_since(self, chunk_id: int, since_sha: str) -> str:
        """Unified diff of ``chunk_id``'s text from ``since_sha`` to its
        current text, reconstructed by walking the `chunk_events` version
        chain (`created`/`edited` rows retain `prev_text` per edit — schema
        0031). Empty string when ``since_sha`` already matches the current
        content_sha (no change)."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT text, content_sha FROM chunks WHERE chunk_id = %s",
                (chunk_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"no chunk {chunk_id}")
            current_text, current_sha = row[0], row[1]
            if since_sha == current_sha:
                return ""
            events = conn.execute(
                """SELECT content_sha, prev_text FROM chunk_events
                    WHERE chunk_id = %s AND event_kind IN ('created', 'edited')
                    ORDER BY ts ASC, event_id ASC""",
                (chunk_id,),
            ).fetchall()
        # text_at[sha] = the chunk's text while it carried that sha. Each
        # event's own text isn't in the row — it's the *next* event's
        # prev_text (the snapshot taken just before that later edit). The
        # last (current) sha's text comes from `chunks.text` instead, since
        # there is no later edit to have snapshotted it.
        text_at: dict[str, str] = {}
        for i in range(len(events) - 1):
            sha_i, _ = events[i]
            _, next_prev_text = events[i + 1]
            if sha_i is not None and next_prev_text is not None:
                text_at[sha_i] = next_prev_text
        if events and events[-1][0] is not None:
            text_at[events[-1][0]] = current_text or ""
        since_text = text_at.get(since_sha)
        if since_text is None:
            raise NotFound(
                f"no recorded text at content_sha {since_sha[:12]}… for chunk "
                f"{chunk_id} — the version chain doesn't reach that far back"
            )
        diff = difflib.unified_diff(
            since_text.splitlines(keepends=True),
            (current_text or "").splitlines(keepends=True),
            fromfile=f"approved@{since_sha[:8]}",
            tofile="current",
        )
        return "".join(diff)
