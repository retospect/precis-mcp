"""bib_retag — Layer-2 corpus remediation for mis-typed bibliography chunks
(gripe 196447).

Bibliography chunks ingested via the marker/PDF-OCR path frequently land as
``chunk_kind='paragraph'`` instead of ``'references'``. Because the embed
skip-list keys on ``chunk_kind='references'`` (see ``workers/embed.py``
``EmbedHandler.skip_chunk_kinds`` / ``unembedded_chunk_count``), such a
mis-typed chunk gets a vector and then pollutes semantic search — the taproot
ANN discovery passes (``hub_refine`` / ``chase_trigger``) treat it as a body
candidate.

**Layer 1** (``utils/boilerplate.py``, shipped separately) fixes the ingest
classifier so NEW ingests tag bibliography correctly. **This pass is Layer 2**:
it remediates the EXISTING corpus. Per claimed paper it finds ``ord >= 0``
``chunk_kind='paragraph'`` chunks that are *content-detected* as bibliography
(by ``bib_parse``'s shared, proven detector — imported here so the two passes
never disagree on what a bibliography chunk is), re-types them to
``'references'`` in place, and DELETEs their now-inappropriate
``chunk_embeddings`` / ``chunk_summaries`` so they drop out of search and never
get re-embedded (the embed worker skips ``chunk_kind='references'``).

**Why an in-place UPDATE, not DELETE + INSERT.** The repo's append-only body
invariant (``migrations/0068_chunks_forbid_body_text_update.sql``) is enforced
by a trigger that fires ONLY ``WHEN NEW.text IS DISTINCT FROM OLD.text`` — a
``chunk_kind``-only UPDATE leaves ``text`` untouched, so the trigger never
fires. The trigger exists because an in-place ``text`` edit would orphan the
derived ``chunk_embeddings`` / ``chunk_summaries`` (keyed by ``chunk_id``); we
sidestep that concern by DELETEing those derived rows explicitly in the same
transaction rather than leaving them stale. DELETE + INSERT was rejected
deliberately: it churns ``chunk_id``, which would CASCADE-orphan the citation
grounding in ``chunk_citations`` (FK ``chunk_id ON DELETE CASCADE``, migration
0109), ``links.src_chunk_id``, and ``chunk_tags``. An in-place UPDATE preserves
``chunk_id``, so every one of those rows stays intact and correctly attached.
(A bibliography chunk is a citation *target*, not a source of
``chunk_citations`` rows — ``bib_mark`` writes those keyed to the CITING body
chunk — so retyping it strands nothing regardless.)

**Trigger model — DEFAULT-OFF / manual.** This pass MUTATES existing corpus
data, so unlike a default-ON ``_SYS`` pass it must never auto-run on deploy. Its
``ServiceSpec`` (``workers/registry.py``) carries ``enable_env`` and NO
``default_profiles``: it registers structurally but is gated OFF every cycle
absent an explicit ``service_config`` row, and is invoked on demand via
``precis worker --only bib_retag``.

**Convergence.** Every processed paper is stamped ``refs.meta.bib_retag_version``
ALWAYS — even when zero chunks were retyped — so the claim predicate
(``meta.bib_retag_version`` absent or below :data:`BIB_RETAG_VERSION`) drains
and never rescans. Idempotent: a second run finds already-``references`` chunks
(no longer ``paragraph`` targets) and skips already-stamped papers; a version
bump re-sweeps the corpus.

**Dry-run.** ``PRECIS_BIB_RETAG_DRY_RUN=1`` (or ``dry_run=True``) detects and
logs what WOULD be retyped without mutating anything — no UPDATE, no derived-row
DELETE, no version stamp — so a corpus-wide count can precede the real sweep.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

# Reuse bib_parse's proven content-based detector so the remediation pass and
# the parse pass agree, byte-for-byte, on what a bibliography chunk is.
from precis.workers.bib_parse import _chunk_is_bibliography

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: Bumping this re-sweeps the corpus lazily (a stamped paper below this version
#: is re-claimed) — mirrors ``bib_parse.BIB_PARSE_VERSION``.
BIB_RETAG_VERSION = 1

#: Key stamped onto ``refs.meta`` (paper-level convergence marker).
_META_VERSION_KEY = "bib_retag_version"

#: Env flag turning the whole pass into a non-mutating count.
_DRY_RUN_ENV = "PRECIS_BIB_RETAG_DRY_RUN"

#: ``chunk_kind`` the mis-typed bibliography chunks currently carry (and the
#: only candidates this pass inspects — an ``ord >= 0`` body row already tagged
#: ``references`` is done, and other kinds are out of scope).
_TARGET_CHUNK_KIND = "paragraph"

#: What a retag target becomes (the embed skip-list key).
_REFERENCES_CHUNK_KIND = "references"


def _dry_run_enabled() -> bool:
    return (os.environ.get(_DRY_RUN_ENV) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# ── DB: claim + candidate read + mutation ──────────────────────────────


def _claim(
    conn: Any, *, limit: int, ref_ids: list[int] | None = None
) -> list[tuple[int, str]]:
    """Papers with body content whose ``meta.bib_retag_version`` is absent or
    below :data:`BIB_RETAG_VERSION`. ``ref_ids`` optionally restricts the sweep
    (targeted backfill / tests); ``None`` sweeps the corpus.

    ``FOR UPDATE OF r SKIP LOCKED`` (mirrors ``bib_parse._claim``): this pass
    is DB-mutating, so two nodes racing the same unstamped batch would double
    the work — a concurrent claim already holding one of these rows drops it
    from this call's returned set.

    Selects on body-chunk existence (not paragraph-bib existence) so a paper
    whose bibliography is ALREADY correctly ``references`` is still claimed and
    stamped — converging it out of every future sweep.
    """
    ref_filter = "AND r.ref_id = ANY(%(ref_ids)s)" if ref_ids else ""
    sql = f"""
        SELECT r.ref_id, r.title
        FROM refs r
        WHERE r.kind = 'paper' AND r.deleted_at IS NULL
          {ref_filter}
          AND EXISTS (
            SELECT 1 FROM chunks c
            WHERE c.ref_id = r.ref_id AND c.ord >= 0 AND c.retired_at IS NULL
          )
          AND (
            NOT (r.meta ? %(mk)s)
            OR COALESCE((r.meta->>%(mk)s)::int, 0) < %(ver)s
          )
        ORDER BY r.ref_id
        LIMIT %(limit)s
        FOR UPDATE OF r SKIP LOCKED
    """
    params: dict[str, Any] = {
        "mk": _META_VERSION_KEY,
        "ver": BIB_RETAG_VERSION,
        "limit": limit,
    }
    if ref_ids:
        params["ref_ids"] = list(ref_ids)
    rows = conn.execute(sql, params).fetchall()
    return [(int(r[0]), str(r[1] or "")) for r in rows]


def _paragraph_chunks(conn: Any, ref_id: int) -> list[tuple[int, str]]:
    """``(chunk_id, text)`` for one paper's ``ord >= 0`` ``paragraph`` body
    chunks — the only retag candidates."""
    rows = conn.execute(
        "SELECT chunk_id, text FROM chunks "
        "WHERE ref_id = %s AND ord >= 0 AND retired_at IS NULL "
        "AND chunk_kind = %s ORDER BY ord",
        (ref_id, _TARGET_CHUNK_KIND),
    ).fetchall()
    return [(int(r[0]), r[1] or "") for r in rows]


def _retag_targets(chunk_rows: list[tuple[int, str]]) -> list[int]:
    """``chunk_id``s among ``chunk_rows`` (all ``paragraph``) that content-detect
    as bibliography via ``bib_parse``'s shared, conservative detector.

    Passing ``chunk_kind=_TARGET_CHUNK_KIND`` means the detector's
    always-qualify shortcut (``chunk_kind='references'``) is never taken — the
    verdict is purely the "most non-empty lines look like ``- [N] ...``"
    content ratio, the same confidence bar ``bib_parse`` uses. Conservative by
    design: a false retype silently drops real body content from search, which
    is worse than leaving a bibliography chunk mis-typed.
    """
    return [
        chunk_id
        for chunk_id, text in chunk_rows
        if _chunk_is_bibliography(text, _TARGET_CHUNK_KIND)
    ]


def _retype_chunks(conn: Any, chunk_ids: list[int]) -> int:
    """Re-type ``chunk_ids`` to ``references`` in place and DELETE their stale
    derived rows. Returns the number of ``chunk_embeddings`` rows removed.

    In-place ``chunk_kind`` UPDATE (``text`` unchanged) does NOT trip the
    append-only body-text trigger (see module docstring). The derived-row
    DELETEs keep ``chunk_embeddings`` / ``chunk_summaries`` from describing a
    chunk that no longer participates in search; ``chunk_id`` is preserved, so
    ``chunk_citations`` / ``links`` / ``chunk_tags`` stay attached.
    """
    if not chunk_ids:
        return 0
    conn.execute(
        "UPDATE chunks SET chunk_kind = %s WHERE chunk_id = ANY(%s)",
        (_REFERENCES_CHUNK_KIND, chunk_ids),
    )
    deleted = conn.execute(
        "DELETE FROM chunk_embeddings WHERE chunk_id = ANY(%s)",
        (chunk_ids,),
    ).rowcount
    conn.execute(
        "DELETE FROM chunk_summaries WHERE chunk_id = ANY(%s)",
        (chunk_ids,),
    )
    return int(deleted or 0)


def _stamp_paper_version(conn: Any, ref_id: int) -> None:
    conn.execute(
        "UPDATE refs SET meta = meta || %s, updated_at = now() WHERE ref_id = %s",
        (Jsonb({_META_VERSION_KEY: BIB_RETAG_VERSION}), ref_id),
    )


# ── the pass ────────────────────────────────────────────────────────────


def run_bib_retag_pass(
    store: Store,
    *,
    batch_size: int = 8,
    ref_ids: list[int] | None = None,
    dry_run: bool | None = None,
) -> dict[str, int]:
    """One claim -> detect -> retype -> stamp cycle. Returns ``{claimed, ok,
    failed, papers_retagged, chunks_retyped, embeddings_deleted, dry_run}``.

    ``ref_ids`` optionally restricts the claim to specific papers (targeted
    backfill / tests); ``None`` sweeps the corpus. ``dry_run`` (default: the
    ``PRECIS_BIB_RETAG_DRY_RUN`` env) detects + logs but mutates nothing —
    neither the chunks nor their derived rows nor the version stamp — so a
    dry-run does NOT converge (it re-claims the same papers next call, which is
    the intended behaviour for a repeatable pre-sweep count).
    """
    if dry_run is None:
        dry_run = _dry_run_enabled()

    with store.pool.connection() as conn:
        rows = _claim(conn, limit=batch_size, ref_ids=ref_ids)
        conn.commit()
    if not rows:
        return {
            "claimed": 0,
            "ok": 0,
            "failed": 0,
            "papers_retagged": 0,
            "chunks_retyped": 0,
            "embeddings_deleted": 0,
            "dry_run": int(dry_run),
        }

    ok = failed = papers_retagged = chunks_retyped = embeddings_deleted = 0
    for ref_id, _title in rows:
        try:
            with store.pool.connection() as conn:
                targets = _retag_targets(_paragraph_chunks(conn, ref_id))
                if dry_run:
                    if targets:
                        log.info(
                            "bib_retag[dry-run]: ref_id=%s would retype %d chunk(s)",
                            ref_id,
                            len(targets),
                        )
                    conn.rollback()
                else:
                    n_emb = _retype_chunks(conn, targets)
                    _stamp_paper_version(conn, ref_id)
                    conn.commit()
                    if targets:
                        log.info(
                            "bib_retag: ref_id=%s retyped %d chunk(s), "
                            "deleted %d embedding(s)",
                            ref_id,
                            len(targets),
                            n_emb,
                        )
                    embeddings_deleted += n_emb
            if targets:
                papers_retagged += 1
                chunks_retyped += len(targets)
            ok += 1
        except Exception:
            log.exception("bib_retag: failed ref_id=%s", ref_id)
            failed += 1

    return {
        "claimed": len(rows),
        "ok": ok,
        "failed": failed,
        "papers_retagged": papers_retagged,
        "chunks_retyped": chunks_retyped,
        "embeddings_deleted": embeddings_deleted,
        "dry_run": int(dry_run),
    }


__all__ = [
    "BIB_RETAG_VERSION",
    "run_bib_retag_pass",
]
