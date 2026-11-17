"""gr702986: dispatch-level inbound Links section and ``view='links'``
fallback for refs-backed kinds whose handler prints neither."""

from __future__ import annotations

import pytest

from precis.runtime import PrecisRuntime
from precis.store import Store

KINDS = ["pathway", "rxn", "material", "route"]


def _seed(store: Store, kind: str) -> tuple[int, int]:
    target = store.insert_ref(kind=kind, slug=f"{kind}-tgt", title=f"{kind} target").id
    citer = store.insert_ref(kind="memory", slug=None, title="memory citing it").id
    store.add_link(src_ref_id=citer, dst_ref_id=target, relation="cites")
    return target, citer


@pytest.mark.parametrize("kind", KINDS)
def test_default_get_shows_inbound_links(
    runtime_with_store: PrecisRuntime, store: Store, kind: str
) -> None:
    target, _ = _seed(store, kind)
    out = str(runtime_with_store.dispatch("get", {"kind": kind, "id": f"{kind}-tgt"}))
    assert "Links" in out
    assert "memory citing it" in out


# route ignores an unknown view instead of refusing it, so the refusal-driven
# fallback never sees it.
@pytest.mark.parametrize("kind", ["pathway", "rxn", "material"])
def test_view_links_falls_back(
    runtime_with_store: PrecisRuntime, store: Store, kind: str
) -> None:
    target, _ = _seed(store, kind)
    out = str(
        runtime_with_store.dispatch(
            "get", {"kind": kind, "id": f"{kind}-tgt", "view": "links"}
        )
    )
    assert "links" in out
    assert "memory citing it" in out
