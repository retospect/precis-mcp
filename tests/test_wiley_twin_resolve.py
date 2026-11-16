"""Wiley anie/ange twin DOIs route through the title-resolver owner gate."""

from __future__ import annotations

from typing import Any

from precis.ingest.metadata_resolve import _resolve_by_title
from precis.store import Store

_TITLE = "Accelerated Tandem Catalysis for Industrial-Level Nitric Oxide Reduction"


def test_title_resolve_flags_twin_doi_owner(store: Store) -> None:
    held = store.insert_ref(kind="paper", slug="held2024", title=_TITLE, year=2024)
    stub = store.insert_ref(kind="paper", slug="stub2024", title=_TITLE, year=2024)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('doi', '10.1002/anie.5660280', %s, 'test')",
            (held.id,),
        )
        conn.commit()

    def s2_fn(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"title": _TITLE, "year": 2024, "doi": "10.1002/ange.5660280"}

    res = _resolve_by_title(
        store,
        stub,
        candidates=[_TITLE],
        trusted_title=True,
        s2_fn=s2_fn,
        s2_api_key="",
    )
    assert res.verdict == "review"
    assert res.reason == f"recovered-doi-owned-by-#{held.id}"
