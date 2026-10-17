"""gr495292 / gr495400: the design kinds (``pcb``, ``se``) take the ``tag``
verb through the one shared default — ``Handler.tag`` in
:mod:`precis.protocol` — gated on ``KindSpec.supports_tag``, the same
way every other refs-backed slug kind gets tagging. Pin the whole path:
dispatch → ref resolution → ``apply_tag_ops`` → visible on ``get`` —
plus the ``put(tags=/untags=)`` D3 shortcut each kind's put accepts.
"""

from __future__ import annotations

import pytest

from precis.errors import BadInput
from precis.runtime import PrecisRuntime
from precis.store import Store


def _seed_pcb(store: Store, slug: str) -> None:
    store.pcb_apply(
        slug=slug,
        title=slug,
        components=[
            {
                "refdes": "U1",
                "label": "test",
                "pins": [{"name": "1"}, {"name": "2"}],
            }
        ],
        nets=[{"name": "N1"}],
        connections=[],
    )


def _seed_se(runtime: PrecisRuntime, slug: str) -> str:
    return runtime.dispatch(
        "put",
        {
            "kind": "se",
            "id": slug,
            "text": '{"ops":[{"op":"add_block","name":"b1",'
            '"envelope":"box:w0.01d0.01h0.01"}]}',
        },
    )


def test_pcb_tag_verb(runtime_with_store: PrecisRuntime, store: Store) -> None:
    _seed_pcb(store, "tag-board")
    out = runtime_with_store.dispatch(
        "tag", {"kind": "pcb", "id": "tag-board", "add": ["dogfood-1"]}
    )
    assert "error" not in out.lower(), out
    ref = store.get_ref(kind="pcb", id="tag-board")
    assert ref is not None
    tags = [str(t) for t in store.tags_for(ref.id)]
    assert "dogfood-1" in tags
    # Visible on get — gr495292's "nothing visible reads it".
    body = runtime_with_store.dispatch("get", {"kind": "pcb", "id": "tag-board"})
    assert "dogfood-1" in body
    out = runtime_with_store.dispatch(
        "tag", {"kind": "pcb", "id": "tag-board", "remove": ["dogfood-1"]}
    )
    assert "error" not in out.lower(), out
    assert [str(t) for t in store.tags_for(ref.id)] == []


def test_se_tag_verb(runtime_with_store: PrecisRuntime, store: Store) -> None:
    _seed_se(runtime_with_store, "tag-se")
    out = runtime_with_store.dispatch(
        "tag", {"kind": "se", "id": "tag-se", "add": ["dogfood-1"]}
    )
    assert "error" not in out.lower(), out
    ref = store.get_ref(kind="se", id="tag-se")
    assert ref is not None
    assert "dogfood-1" in [str(t) for t in store.tags_for(ref.id)]
    body = runtime_with_store.dispatch("get", {"kind": "se", "id": "tag-se"})
    assert "dogfood-1" in body


def test_tag_untagged_kind_still_refuses(runtime: PrecisRuntime) -> None:
    """The default is inert behind the flag — a kind that never declared
    ``supports_tag`` still gets the Unsupported envelope at dispatch."""
    out = runtime.dispatch("tag", {"kind": "calc", "id": "x", "add": ["y"]})
    assert "does not support tag" in out


def test_pcb_put_tags_shortcut(runtime_with_store: PrecisRuntime, store: Store) -> None:
    out = runtime_with_store.dispatch(
        "put",
        {
            "kind": "pcb",
            "id": "put-tags-board",
            "args": {
                "components": [{"refdes": "U1", "label": "t", "pins": [{"name": "1"}]}],
                "nets": [{"name": "N1"}],
            },
            "tags": ["dogfood-2"],
        },
    )
    assert "error" not in out.lower(), out
    ref = store.get_ref(kind="pcb", id="put-tags-board")
    assert ref is not None
    assert "dogfood-2" in [str(t) for t in store.tags_for(ref.id)]


def test_se_put_tags_shortcut(runtime_with_store: PrecisRuntime, store: Store) -> None:
    runtime_with_store.dispatch(
        "put",
        {
            "kind": "se",
            "id": "put-tags-se",
            "text": '{"ops":[{"op":"add_block","name":"b1",'
            '"envelope":"box:w0.01d0.01h0.01"}]}',
            "tags": ["dogfood-3"],
        },
    )
    ref = store.get_ref(kind="se", id="put-tags-se")
    assert ref is not None
    assert "dogfood-3" in [str(t) for t in store.tags_for(ref.id)]


def test_pcb_tag_requires_add_or_remove(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    _seed_pcb(store, "tag-empty")
    out = runtime_with_store.dispatch("tag", {"kind": "pcb", "id": "tag-empty"})
    assert "requires add= or remove=" in out


def test_pcb_tag_missing_ref_is_notfound(
    runtime_with_store: PrecisRuntime,
) -> None:
    out = runtime_with_store.dispatch(
        "tag", {"kind": "pcb", "id": "no-such-board", "add": ["x"]}
    )
    assert "not found" in out.lower()


def test_base_tag_needs_an_id() -> None:
    """Direct unit check on the shared default — no store needed to see
    the input guard fire."""
    from precis.protocol import Handler, KindSpec

    class _Bare(Handler):
        spec = KindSpec(kind="bare", title="t", description="d", supports_tag=True)

    bare = _Bare()
    with pytest.raises(BadInput):
        bare.tag(add=["x"])
    with pytest.raises(BadInput):
        bare.tag(id=["a", "b"], add=["x"])
