"""gr497175: ``get(kind='gripe', id='/open', args={'page': N})`` pages the list.

``get()`` has no ``page=`` wire param (tool-surface decision), so paging rides
``args=``; the footer names the next call verbatim plus the search fallback.
"""

from __future__ import annotations

from precis.runtime import PrecisRuntime
from precis.store import Store
from tests._gripe import insert_gripe


def _rows(out: str) -> set[str]:
    return {ln for ln in out.splitlines() if "paging probe" in ln}


def test_open_list_pages_through_args(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    for i in range(25):
        insert_gripe(store, f"paging probe {i:02d}")

    def get(**extras: int) -> str:
        payload: dict = {"kind": "gripe", "id": "/open"}
        if extras:
            payload["__extras__"] = extras
        return runtime_with_store.dispatch("get", payload)

    p1 = get()
    p2 = get(page=2)
    assert _rows(p1) and _rows(p2)
    assert not _rows(p1) & _rows(p2)
    assert len(_rows(p1)) + len(_rows(p2)) == 25
    assert "get(kind='gripe', id='/open', args={'page': 2})" in p1
    assert "search(kind='gripe', status='open', page_size=100)" in p1
    assert "page 2 of 2 · 25 total · last page" in p2
    assert "batch-read: get(kind='gripe', id=[<ids>], view='body'|'full')" in p1
    assert "batch-read: get(kind='gripe', id=[<ids>], view='body'|'full')" in p2
