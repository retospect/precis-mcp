"""AliasHandler tests — kind='alias', the bidirectional string map.

Pairs are looked up from either side, relations read inverted from the
b side, duplicates return the existing handle, expired pairs hide, and a
pair tied to a gripe surfaces in ``/stale`` once the gripe closes.
"""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.alias import AliasHandler
from precis.handlers.gripe import GripeHandler
from precis.store import Tag
from tests.conftest import id_of

URL = "https://api.crossref.org/works/10.1126/sciadv.aeg5791"


@pytest.fixture
def handler(store) -> AliasHandler:
    store.upsert_kinds([AliasHandler.spec, GripeHandler.spec])
    return AliasHandler(hub=Hub(store=store))


def _aid(body: str) -> int:
    m = re.search(r"\bas(\d+)\b", body)
    assert m, body
    return int(m.group(1))


def _mk(handler: AliasHandler, **kw) -> int:
    args = {"a": "sciadv-x", "b": URL, "rel": "short-for", **kw}
    return _aid(handler.put(**args).body)


def test_lookup_from_either_side(handler) -> None:
    aid = _mk(handler)
    by_a = handler.get(id="sciadv-x").body
    assert URL in by_a and "short-for" in by_a and f"as{aid}" in by_a
    by_b = handler.get(id=URL).body
    assert "sciadv-x" in by_b and "expands-to" in by_b


def test_inverse_rel_is_stored_forward(handler) -> None:
    aid = _aid(handler.put(a=URL, b="sciadv-x", rel="expands-to").body)
    ref = handler.store.get_ref(kind="alias", id=aid)
    p = ref.meta["alias"]
    assert (p["a"], p["b"], p["rel"]) == ("sciadv-x", URL, "short-for")


def test_duplicate_returns_existing(handler) -> None:
    aid = _mk(handler)
    again = handler.put(a="sciadv-x", b=URL, rel="short-for").body
    assert f"as{aid}" in again and "unchanged" in again


def test_case_insensitive_fallback(handler) -> None:
    _mk(handler)
    assert URL in handler.get(id="SCIADV-X").body


def test_scope_filters_lookup(handler) -> None:
    handler.put(a="C25804", b="cp7", rel="same-as", scope="lcsc")
    handler.put(a="C25804", b="cp9", rel="same-as", scope="other")
    body = handler.get(id="C25804", scope="lcsc").body
    assert "cp7" in body and "cp9" not in body


def test_miss_suggests_put(handler) -> None:
    body = handler.get(id="nothing-here").body
    assert "no live alias" in body and "put(kind='alias'" in body


def test_handle_read_and_bare_pk(handler) -> None:
    aid = _mk(handler)
    assert f"b: {URL}" in handler.get(id=f"as{aid}").body
    assert f"b: {URL}" in handler.get(id=str(aid)).body


def test_search_substring(handler) -> None:
    _mk(handler)
    assert "sciadv-x" in handler.search(q="crossref").body
    assert "no alias matching" in handler.search(q="zzz").body


def test_expired_pair_hidden_and_listed_stale(handler) -> None:
    aid = _mk(handler, expires="1h")
    handler.store.update_ref(
        aid,
        meta_patch={
            "alias": {
                **handler.store.get_ref(kind="alias", id=aid).meta["alias"],
                "expires_at": "2020-01-01T00:00:00Z",
            }
        },
    )
    assert "no live alias" in handler.get(id="sciadv-x").body
    assert f"as{aid}" in handler.get(id="/stale").body


def test_gripe_tie_links_and_goes_stale_on_close(handler) -> None:
    gid = id_of(GripeHandler(hub=handler.hub).put(text="doi dropped on ingest").body)
    aid = _mk(handler, gripe=f"gr{gid}")
    links = handler.store.links_for(aid, direction="out")
    assert any(lk.dst_ref_id == gid for lk in links)
    assert "no stale aliases" in handler.get(id="/stale").body
    handler.store.add_tag(gid, Tag.closed("STATUS", "done"), replace_prefix=True)
    assert f"as{aid}" in handler.get(id="/stale").body


def test_edit_and_delete(handler) -> None:
    aid = _mk(handler)
    assert "note" in handler.edit(id=f"as{aid}", note="crossref json").body
    assert "crossref json" in handler.get(id=f"as{aid}").body
    handler.delete(id=f"as{aid}")
    assert "no live alias" in handler.get(id="sciadv-x").body


@pytest.mark.parametrize(
    "kw",
    [
        {"a": "", "b": "x"},
        {"a": "x", "b": "x"},
        {"a": "x", "b": "y", "rel": "cites"},
        {"a": "as5", "b": "y"},
        {"a": "x", "b": "y", "expires": "yesterday"},
        {"a": "x" * 2001, "b": "y"},
    ],
)
def test_put_rejects_bad_input(handler, kw) -> None:
    with pytest.raises(BadInput):
        handler.put(**kw)


def test_put_unknown_gripe(handler) -> None:
    with pytest.raises(NotFound):
        handler.put(a="x", b="y", gripe="gr999999999")


def test_runtime_args_roundtrip(store) -> None:
    """The wire shape: put/get through the runtime with args= extras."""
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.runtime import PrecisRuntime

    rt = PrecisRuntime(config=PrecisConfig(), hub=boot(store=store))
    # The MCP tool layer forwards ``args=`` as ``__extras__``.
    out = rt.dispatch(
        "put",
        {"kind": "alias", "__extras__": {"a": "sx", "b": URL, "rel": "short-for"}},
    )
    aid = _aid(out)
    assert URL in rt.dispatch("get", {"kind": "alias", "id": "sx"})
    assert "sx" in rt.dispatch("get", {"kind": "alias", "id": URL})
    assert "cp7" not in rt.dispatch(
        "get", {"kind": "alias", "id": "sx", "__extras__": {"scope": "lcsc"}}
    )
    assert f"b: {URL}" in rt.dispatch("get", {"id": f"as{aid}"})
