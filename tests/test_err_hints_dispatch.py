"""Error-message hints reached through the dispatcher (U2 ERR-HINTS).

gr534656 (paper
NotFound names the nearest live handles), gr530422 (rendered ``sk``
handle is a valid link target).
"""

from __future__ import annotations

import importlib.util

import pytest

from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.utils import handle_registry

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("pysbd") is None,
    reason="full-hub boot needs the [paper] extra",
)


def _paper(store: Store, cite_key: str) -> int:
    return store.insert_ref(kind="paper", slug=cite_key, title=cite_key, meta={}).id


def test_missing_paper_handle_lists_the_nearest_live_handles(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    lo = _paper(store, "nearlo")
    hi = _paper(store, "nearhi")
    gone = f"pa{hi + 50}"
    out, err = runtime_with_store.dispatch_with_status(
        "get", {"kind": "paper", "id": gone}
    )
    assert err
    assert "not found" in out
    assert f"pa{hi}" in out and "options" in out
    out2, err2 = runtime_with_store.dispatch_with_status(
        "get", {"kind": "paper", "id": f"pa{lo}0000"}
    )
    assert err2 and f"pa{hi}" in out2


def test_rendered_skill_handle_is_a_gripe_link_target(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref_id = store.ensure_skill_ref("precis-search-help")
    handle = handle_registry.format_handle("skill", ref_id)
    out, err = runtime_with_store.dispatch_with_status(
        "put",
        {
            "kind": "gripe",
            "text": "search: the help skill is hard to find",
            "link": handle,
            "rel": "raises-concern-about",
        },
    )
    assert not err, out
    assert "created gripe" in out
