"""``diagnose_scan`` — the minter half of ``diagnose_gripe``; the job
itself lives in :mod:`precis.workers.job_types.diagnose_gripe`.

Per pass: select open gripes with no diagnosis yet, mint up to
:data:`_CAP` ``diagnose_gripe`` jobs, dedup by ``idem_key =
f"diagnose:{gripe_id}"`` so a gripe is diagnosed at most once in v1 (no
staleness window / re-diagnosis — see the backlog's open-questions log).

Minting reuses the direct-``insert_ref`` pattern
:func:`precis.workers.materialize._mint_jobs` established (parentless —
no owning todo/build-subject exists for a scanner-minted diagnosis) and
:func:`precis.workers.draft_refresh_scan._mint`'s idem-key-guarded
existence check under the same connection as the insert (dedup on
existence, "any status" blocks a re-mint).

A gripe is skipped when it already carries a ``DIAGNOSIS (auto`` comment
(the write-back diagnose_gripe leaves — see
:data:`precis.workers.job_types.diagnose_gripe._DIAGNOSIS_PREFIX`), or the
``backlog_groom`` opt-out tag (``no-groom`` — a human who doesn't want the
autonomous-fixer substrate touching a gripe presumably doesn't want it
auto-diagnosed either; reused rather than inventing a second opt-out tag).

``PRECIS_DIAGNOSE_SCAN_ENABLED`` only registers this pass in ``cli/worker.py``
(structural, like ``backlog_groom``'s flag) — whether it actually *fires*
each cycle is the DB-backed ``ServiceConfigResolver`` gate, which defaults a
pass with no ``default_profiles`` to OFF absent an explicit ``service_config``
row (seeded from the flag at deploy). Once armed, every fire spends up to
:data:`_CAP` BIG-tier ``claude -p`` calls, so it is enabled deliberately,
like the classifier and the groomer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from precis.store.types import Tag
from precis.workers.backlog_groom import _OPT_OUT_TAG
from precis.workers.job_types.diagnose_gripe import _DIAGNOSIS_PREFIX
from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

_HANDLER_NAME = "diagnose_scan"

#: Hard cap on mints per pass — bounds the BIG-tier spend per fire (each
#: mint becomes one clone + claude -p call once dispatched).
_CAP = 3

#: Background priority — unattended maintenance, never ahead of
#: operator-facing work (mirrors materialize.py / draft_refresh_scan.py's
#: shared background prio).
_MINT_PRIO = 8

#: The comment-text marker a live diagnosis leaves (see
#: diagnose_gripe._DIAGNOSIS_PREFIX, format()-templated with a job id —
#: the literal prefix up to the first ``{`` is what's stable to match on).
_DIAGNOSIS_MARKER = _DIAGNOSIS_PREFIX.split("{", 1)[0].strip()


def _already_diagnosed(store: Store, gripe_id: int) -> bool:
    """True when ``gripe_id`` already carries a ``DIAGNOSIS (auto`` comment."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM chunks "
            "WHERE ref_id = %s AND chunk_kind = 'gripe_comment' "
            "AND retired_at IS NULL AND text LIKE %s LIMIT 1",
            (gripe_id, f"{_DIAGNOSIS_MARKER}%"),
        ).fetchone()
    return row is not None


def _mint(store: Store, *, gripe_id: int, prio: int | None) -> bool:
    """Mint ONE ``diagnose_gripe`` job for ``gripe_id``. Returns ``True``
    iff a new job was inserted, ``False`` when its ``idem_key`` already
    exists (any status — the dedup working as intended, not an error)."""
    idem_key = f"diagnose:{gripe_id}"
    with store.pool.connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM refs WHERE kind = 'job' AND deleted_at IS NULL "
            "AND meta->>'idem_key' = %s LIMIT 1",
            (idem_key,),
        ).fetchone()
        if existing is not None:
            conn.commit()
            return False
        ref = store.insert_ref(
            kind="job",
            slug=None,
            title=f"diagnose_gripe (gripe:{gripe_id})",
            meta={
                "job_type": "diagnose_gripe",
                "executor": "claude_inproc",
                "params": {"gripe_id": gripe_id},
                "idem_key": idem_key,
            },
            prio=prio if prio is not None else _MINT_PRIO,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "queued"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        conn.commit()
    log.info(
        "diagnose_scan: minted diagnose_gripe job id=%d for gripe:%d",
        ref.id,
        gripe_id,
    )
    return True


def run_diagnose_scan_pass(store: Store, batch_size: int = _CAP) -> BatchResult:
    """One scan tick: mint at most ``min(batch_size, _CAP)`` ``diagnose_gripe``
    jobs for open, undiagnosed, non-opted-out gripes.

    Counters mirror the other folded cadences (``backlog_groom`` /
    ``draft_refresh_scan``): ``claimed`` = candidates selected this pass,
    ``ok`` = jobs actually minted (a candidate whose idem_key already
    exists is claimed-but-not-ok, the dedup working as intended),
    ``failed`` = mints that raised (logged, skipped).
    """
    cap = min(batch_size, _CAP) if batch_size and batch_size > 0 else _CAP
    open_gripes = store.list_refs(
        kind="gripe",
        tags=["STATUS:open"],
        order_by="updated_desc",
        limit=200,
    )

    selected: list[tuple[int, int | None]] = []
    for g in open_gripes:
        if len(selected) >= cap:
            break
        if store.has_tag(int(g.id), "OPEN", _OPT_OUT_TAG):
            continue
        if _already_diagnosed(store, int(g.id)):
            continue
        selected.append((int(g.id), g.prio))

    if not selected:
        return BatchResult(handler=_HANDLER_NAME, claimed=0, ok=0, failed=0)

    minted = 0
    failed = 0
    for gripe_id, prio in selected:
        try:
            if _mint(store, gripe_id=gripe_id, prio=prio):
                minted += 1
        except Exception:  # pragma: no cover - defensive
            log.exception("diagnose_scan: failed to mint job for gripe id=%d", gripe_id)
            failed += 1

    return BatchResult(
        handler=_HANDLER_NAME,
        claimed=len(selected),
        ok=minted,
        failed=failed,
    )


__all__ = ["run_diagnose_scan_pass"]
