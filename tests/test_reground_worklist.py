"""The mined-unheld-DOI worklist (`src/precis/taproot/reground_worklist.py`).

DB-backed (real ``refs``/``chunks`` via the ``store`` fixture); no LLM.
Covers the merge across hubs' ``reground_external`` reports, the
citer-count demand signal, demand ordering, the row cap, and the
read-only contract (no report -> no rows; nothing writes).
"""

from __future__ import annotations

from typing import Any

from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub
from precis.taproot.reground_worklist import unheld_doi_worklist


def _seed_hub_with_report(
    store: Any,
    *,
    sentence: str,
    unheld: list[dict[str, Any]],
    at: str = "2026-11-20T00:00:00+00:00",
) -> int:
    hub = mint_hub(store, CanonicalClaim(sentence=sentence, scope={}))
    store.update_ref(
        hub,
        meta_patch={
            "reground_external": {
                "at": at,
                "unheld_dois": unheld,
                "probed": [],
            }
        },
    )
    return hub


def _draft_citing(store: Any, hub: int, n: int = 1) -> None:
    for i in range(n):
        ref, _title = store.drafts.create_draft(
            name=f"worklist-draft-{hub}-{i}", title="Worklist draft"
        )
        store.drafts.add_chunks(
            ref_id=ref.id,
            chunk_kind="paragraph",
            text=f"A paragraph citing the hub [fi{hub}].",
            at={"last": True},
        )


def test_worklist_merges_dois_across_hubs_and_counts_citers(store: Any) -> None:
    h1 = _seed_hub_with_report(
        store,
        sentence="Claim one is unsupported.",
        unheld=[{"doi": "10.1/alpha", "via": "reference-list"}],
    )
    h2 = _seed_hub_with_report(
        store,
        sentence="Claim two is unsupported.",
        unheld=[
            {"doi": "10.1/ALPHA", "via": "s2"},  # case-folds onto the same DOI
            {"doi": "10.1/beta", "via": "reference-list"},
        ],
    )
    _draft_citing(store, h1, n=2)
    _draft_citing(store, h2, n=1)

    worklist = unheld_doi_worklist(store)

    assert worklist.scanned_hubs == 2
    by_doi = {r.doi: r for r in worklist.rows}
    assert set(by_doi) == {"10.1/alpha", "10.1/beta"}
    alpha = by_doi["10.1/alpha"]
    assert alpha.hubs == (h1, h2)
    assert sorted(alpha.vias) == ["reference-list", "s2"]
    assert alpha.draft_citers == 3  # 2 citers of h1 + 1 of h2
    assert alpha.mined_at == "2026-11-20T00:00:00+00:00"
    assert by_doi["10.1/beta"].hubs == (h2,)
    assert by_doi["10.1/beta"].draft_citers == 1


def test_worklist_is_sorted_by_demand(store: Any) -> None:
    quiet = _seed_hub_with_report(
        store,
        sentence="A quiet unsupported claim.",
        unheld=[{"doi": "10.1/quiet", "via": "s2"}],
    )
    loud = _seed_hub_with_report(
        store,
        sentence="A loud unsupported claim.",
        unheld=[{"doi": "10.1/loud", "via": "s2"}],
    )
    _draft_citing(store, loud, n=3)

    worklist = unheld_doi_worklist(store)
    assert [r.doi for r in worklist.rows] == ["10.1/loud", "10.1/quiet"]
    assert worklist.rows[0].hubs == (loud,)
    assert quiet in worklist.rows[1].hubs


def test_worklist_honours_the_row_limit(store: Any) -> None:
    for i in range(5):
        _seed_hub_with_report(
            store,
            sentence=f"Unsupported claim number {i}.",
            unheld=[{"doi": f"10.1/d{i}", "via": "s2"}],
        )
    assert len(unheld_doi_worklist(store, limit=2).rows) == 2
    assert len(unheld_doi_worklist(store).rows) == 5


def test_worklist_with_no_reports_is_empty(store: Any) -> None:
    worklist = unheld_doi_worklist(store)
    assert worklist.rows == ()
    assert worklist.scanned_hubs == 0
    assert "0 unheld DOI(s)" in worklist.format()


def test_worklist_is_read_only(store: Any) -> None:
    """The record-only contract: running the view writes nothing — the
    report is byte-identical afterwards and no ref for the DOI exists."""
    hub = _seed_hub_with_report(
        store,
        sentence="An unsupported claim.",
        unheld=[{"doi": "10.1/held-back", "via": "reference-list"}],
    )

    def _report() -> Any:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta->'reground_external' FROM refs WHERE ref_id = %s",
                (hub,),
            ).fetchone()
        return row[0]

    before = _report()
    worklist = unheld_doi_worklist(store)
    assert [r.doi for r in worklist.rows] == ["10.1/held-back"]
    assert _report() == before
    with store.pool.connection() as conn:
        found = conn.execute(
            "SELECT 1 FROM ref_identifiers WHERE id_kind = 'doi' "
            "AND id_value = '10.1/held-back'"
        ).fetchone()
    assert found is None


def test_format_names_every_row(store: Any) -> None:
    hub = _seed_hub_with_report(
        store,
        sentence="An unsupported claim.",
        unheld=[{"doi": "10.1/alpha", "via": "s2"}],
    )
    text = unheld_doi_worklist(store).format()
    assert "10.1/alpha" in text
    assert f"fi{hub}" in text
    assert "citers=0" in text
