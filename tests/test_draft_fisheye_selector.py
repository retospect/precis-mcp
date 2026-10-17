"""gr495026: ``get(kind='draft', id=…, extent='fisheye')`` takes every chunk
selector form the plain get takes — one parser (``Draft._chunk_of_selector``)."""

from __future__ import annotations

from typing import Any

import pytest

from precis.utils import handle_registry

pytestmark = pytest.mark.db

_MARK = "Zircon paragraph body"


def _get(runtime: Any, **args: Any) -> tuple[str, bool]:
    return runtime.dispatch_with_status("get", {"kind": "draft", **args})


def _draft(store: Any, name: str) -> tuple[Any, str]:
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    ref, _ = store.drafts.create_draft(name=name, title=name, project_ref_id=proj)
    created = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text=_MARK, at={"last": True}
    )
    dc = handle_registry.format_handle("draft", created[0].chunk_id, chunk=True)
    return ref, dc


def _forms(ref: Any, dc: str) -> dict[str, str]:
    return {
        "bare-dc": dc,
        "dr-id~dc": f"{handle_registry.format_handle('draft', ref.id)}~{dc}",
        "slug~dc": f"{ref.slug}~{dc}",
    }


@pytest.mark.parametrize("form", ["bare-dc", "dr-id~dc", "slug~dc"])
class TestSelectorForms:
    def test_plain_get_reads_the_chunk(
        self, runtime_with_store: Any, store: Any, form: str
    ) -> None:
        ref, dc = _draft(store, "selplain")
        out, err = _get(runtime_with_store, id=_forms(ref, dc)[form])
        assert not err and _MARK in out

    @pytest.mark.parametrize("how", ["extent", "view"])
    def test_fisheye_reads_the_same_chunk(
        self, runtime_with_store: Any, store: Any, form: str, how: str
    ) -> None:
        ref, dc = _draft(store, "seleye")
        plain, _ = _get(runtime_with_store, id=dc)
        out, err = _get(
            runtime_with_store, id=_forms(ref, dc)[form], **{how: "fisheye"}
        )
        assert not err and _MARK in out
        assert out != plain  # the eye rendered, not the plain read


def test_fisheye_keeps_the_ordinal_selector(
    runtime_with_store: Any, store: Any
) -> None:
    ref, _dc = _draft(store, "selord")
    with store.pool.connection() as conn:
        ord_ = conn.execute(
            "SELECT ord FROM chunks WHERE ref_id = %s", (ref.id,)
        ).fetchone()[0]
    out, err = _get(runtime_with_store, id=f"{ref.slug}~{ord_}", extent="fisheye")
    assert not err and _MARK in out


@pytest.mark.parametrize("extent", [None, "fisheye"])
def test_foreign_chunk_selector_is_not_found_either_way(
    runtime_with_store: Any, store: Any, extent: str | None
) -> None:
    a, _ = _draft(store, "sela")
    _b, dc_b = _draft(store, "selb")
    args: dict[str, Any] = {"id": f"{a.slug}~{dc_b}"}
    if extent:
        args["extent"] = extent
    out, err = _get(runtime_with_store, **args)
    assert err and "not found in draft" in out


def test_window_selector_keeps_the_eye_wording(
    runtime_with_store: Any, store: Any
) -> None:
    ref, _dc = _draft(store, "selwin")
    out, err = _get(runtime_with_store, id=f"{ref.slug}~0..5", extent="fisheye")
    assert err and "not a window" in out
