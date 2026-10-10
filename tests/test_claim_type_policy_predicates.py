"""Claim-type policy predicates (``taproot.claim_type``) reach the workers.

A ``landscape`` hub is never widened, swept for conflicts, or offered as a
``disputes`` counterparty; ``measurement`` and untyped hubs pass all three.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.taproot.canon import CanonicalClaim
from precis.taproot.claim_type import (
    disputes_counterparty_predicate_sql,
    sweepable_predicate_sql,
    widenable_predicate_sql,
)
from precis.taproot.hub import mint_hub
from precis.workers import conflict_search
from precis.workers.hub_refine import _claim_hubs_due_for_refine

_SENTENCES = {
    "landscape": "Perovskite films generally degrade under humid air.",
    "measurement": "DFT shows the elastic modulus rises 12% under strain.",
    None: "Nanoindentation gives a modulus above 9 GPa in nanobud films.",
}


def _mint(store: Store, claim_type: str | None) -> int:
    return mint_hub(
        store,
        CanonicalClaim(
            sentence=_SENTENCES[claim_type], scope={}, claim_type=claim_type
        ),
    )


def _passing(store: Store, clause: str, ids: list[int]) -> set[int]:
    sql = f"SELECT r.ref_id FROM refs r WHERE r.ref_id = ANY(%s) AND {clause}"
    with store.pool.connection() as conn:
        return {int(r[0]) for r in conn.execute(sql, (ids,)).fetchall()}


@pytest.mark.parametrize(
    "predicate",
    [
        widenable_predicate_sql,
        sweepable_predicate_sql,
        disputes_counterparty_predicate_sql,
    ],
)
def test_predicate_excludes_landscape_only(store: Store, predicate: Any) -> None:
    land, meas, untyped = (_mint(store, t) for t in ("landscape", "measurement", None))
    assert _passing(store, predicate(), [land, meas, untyped]) == {meas, untyped}


def test_hub_refine_due_set_skips_landscape(store: Store) -> None:
    land, meas = _mint(store, "landscape"), _mint(store, "measurement")
    for hub in (land, meas):
        store.add_tag(hub, Tag.closed("TAPROOT_DUE", "1"), set_by="system")
    with store.pool.connection() as conn:
        due = _claim_hubs_due_for_refine(conn, store, limit=10, backstop_h=2160.0)
        conn.commit()
    assert meas in due
    assert land not in due


def test_conflict_finding_candidates_drop_landscape(store: Store) -> None:
    land, meas = _mint(store, "landscape"), _mint(store, "measurement")
    cands = [
        conflict_search._Candidate(
            chunk_id=i,
            chunk_ord=0,
            chunk_text="x",
            ref_id=ref,
            ref_kind="finding",
            distance=0.1,
        )
        for i, ref in enumerate((land, meas), start=1)
    ]
    kept = conflict_search._filter_finding_candidates(store, cands)
    assert [c.ref_id for c in kept] == [meas]
