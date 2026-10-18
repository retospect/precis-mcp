"""gr495382: ``sort=`` source search honours ``status=`` on the parent gripe
and returns gripes (gr ids), not the comment chunks (gc ids) that matched."""

from __future__ import annotations

import re

from precis.runtime.core import PrecisRuntime
from tests.conftest import id_of


def test_status_filter_and_gripe_ids(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    open_id = id_of(
        rt.dispatch("put", {"kind": "gripe", "text": "zanzibar open gripe"})
    )
    done_id = id_of(
        rt.dispatch("put", {"kind": "gripe", "text": "zanzibar closing gripe"})
    )
    rt.dispatch("put", {"kind": "gripe", "id": open_id, "text": "zanzibar note"})
    rt.dispatch(
        "put", {"kind": "gripe", "id": done_id, "text": "resolved: zanzibar fixed"}
    )
    out = rt.dispatch("tag", {"kind": "gripe", "id": done_id, "add": ["STATUS:done"]})
    assert "[error:" not in out, out

    res = rt.dispatch(
        "search",
        {"kind": "gripe", "status": "open", "q": "zanzibar", "sort": "recency"},
    )
    assert "[error:" not in res, res
    # Body and comment both match "zanzibar": still one row for the gripe.
    assert len(re.findall(rf"\bgr{open_id}\b", res)) == 1, res
    assert not re.search(rf"\bgr{done_id}\b", res), res
    assert not re.search(r"\bgc\d+", res), res


def test_status_exclusion_on_sort_path(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    keep_id = id_of(rt.dispatch("put", {"kind": "gripe", "text": "quokka kept"}))
    gone_id = id_of(rt.dispatch("put", {"kind": "gripe", "text": "quokka dropped"}))
    rt.dispatch(
        "put", {"kind": "gripe", "id": gone_id, "text": "resolved: wontfix, not a bug"}
    )
    tagged = rt.dispatch(
        "tag", {"kind": "gripe", "id": gone_id, "add": ["STATUS:wontfix"]}
    )
    assert "[error:" not in tagged, tagged

    res = rt.dispatch(
        "search",
        {"kind": "gripe", "status": "!wontfix", "q": "quokka", "sort": "recency"},
    )
    assert "[error:" not in res, res
    assert re.search(rf"\bgr{keep_id}\b", res), res
    assert not re.search(rf"\bgr{gone_id}\b", res), res
