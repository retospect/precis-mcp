"""gr702986: dispatch-level inbound Links section and ``view='links'``
fallback for refs-backed kinds whose handler prints neither."""

from __future__ import annotations

import pytest

from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.utils.handle_registry import format_handle

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
    _seed(store, kind)
    out = runtime_with_store.dispatch("get", {"kind": kind, "id": f"{kind}-tgt"})
    assert "\nLinks:" in out
    assert "memory citing it" in out


@pytest.mark.parametrize("kind", KINDS)
def test_view_links_falls_back(
    runtime_with_store: PrecisRuntime, store: Store, kind: str
) -> None:
    _seed(store, kind)
    out = runtime_with_store.dispatch(
        "get", {"kind": kind, "id": f"{kind}-tgt", "view": "links"}
    )
    assert out.startswith(f"# {kind} ")
    assert " - links\n" in out
    assert "memory citing it" in out


def test_shared_links_view_accepts_filters_and_paging(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    target, _ = _seed(store, "route")
    first = store.links_for(target, direction="in")[0]
    second = store.insert_ref(kind="memory", slug=None, title="second citation").id
    store.add_link(src_ref_id=second, dst_ref_id=target, relation="cites")
    payload = {"kind": "route", "id": "route-tgt", "view": "links"}
    options = {"rel": "cites", "of_kind": "memory", "limit": 1}
    out = runtime_with_store.dispatch("get", {**payload, "args": options})
    assert "memory citing it" in out
    assert "second citation" not in out
    assert "shown 1 of 2" in out
    cursor = f"in:{first.id}"
    assert cursor in out
    out = runtime_with_store.dispatch(
        "get", {**payload, "args": {**options, "cursor": cursor}}
    )
    assert "second citation" in out
    assert "shown 2 of 2" in out


@pytest.mark.parametrize("use_handle", [False, True])
def test_skill_anchor_links(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    use_handle: bool,
) -> None:
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "200000")
    slug = "precis-gripe-help"
    target = store.ensure_skill_ref(slug)
    citer = store.insert_ref(kind="memory", slug=None, title="memory citing skill").id
    store.add_link(src_ref_id=citer, dst_ref_id=target, relation="cites")
    ident = format_handle("skill", target) if use_handle else slug
    payload = {"kind": "skill", "id": ident}
    default = runtime_with_store.dispatch("get", payload)
    assert "\nLinks:" in default
    assert "memory citing skill" in default
    links = runtime_with_store.dispatch("get", {**payload, "view": "links"})
    assert links.startswith(f"# skill {target} - links")
    assert "memory citing skill" in links
