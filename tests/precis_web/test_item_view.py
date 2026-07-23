"""Unit tests for the ``ItemPresenter`` contract (``item_view.py``).

Exercises the presenter methods directly (no FastAPI client needed) —
``hover_preview`` / ``thumbnail`` / ``actions`` are new surface on the
Slice-3 contract (``docs/proposals/unified-item-view.md``); the
per-kind registry and the ``artifact_kinds`` facet helper back the
``/items`` route tests in ``test_routes.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

from precis_web.item_view import (
    ItemPresenter,
    YoutubePresenter,
    artifact_kinds,
    item_row,
    presenter_for,
)


def _ref(**kw):
    base = {"id": 1, "kind": "paper", "slug": None, "title": "t", "meta": {}}
    base.update(kw)
    return SimpleNamespace(**base)


def _block(text: str):
    return SimpleNamespace(id=1, pos=0, text=text)


def test_hover_preview_leads_with_abstract() -> None:
    """A kind carrying ``meta['abstract']`` gets a richer hover peek than
    the row preview — abstract first, then the matching chunk."""
    ref = _ref(meta={"abstract": "<p>The <b>abstract</b>.</p>"})
    p = ItemPresenter("paper")
    hv = p.hover_preview(ref, _block("matching chunk text"))
    assert "The abstract" in hv
    assert "matching chunk text" in hv
    # Tags stripped, whitespace collapsed.
    assert "<p>" not in hv and "<b>" not in hv


def test_hover_preview_falls_back_to_row_preview_with_no_abstract() -> None:
    ref = _ref(meta={})
    p = ItemPresenter("web")
    assert p.hover_preview(ref, _block("just a chunk")) == p.preview(
        _block("just a chunk")
    )


def test_hover_preview_truncates_long_combined_text() -> None:
    ref = _ref(meta={"abstract": "x" * 500})
    p = ItemPresenter("paper")
    hv = p.hover_preview(ref, _block("y" * 500))
    assert len(hv) <= 600
    assert hv.endswith("…")


def test_default_thumbnail_and_actions_are_empty() -> None:
    p = ItemPresenter("paper")
    assert p.thumbnail(_ref()) is None
    assert p.actions(_ref()) == []


def test_youtube_presenter_thumbnail_from_slug() -> None:
    ref = _ref(kind="youtube", slug="dQw4w9WgXcQ")
    p = presenter_for("youtube")
    assert isinstance(p, YoutubePresenter)
    assert p.thumbnail(ref) == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"


def test_youtube_presenter_no_thumbnail_without_slug() -> None:
    ref = _ref(kind="youtube", slug=None)
    assert presenter_for("youtube").thumbnail(ref) is None


def test_presenter_for_unregistered_kind_is_generic() -> None:
    p = presenter_for("web")
    assert type(p) is ItemPresenter


def test_item_row_carries_hover_thumbnail_actions() -> None:
    ref = _ref(kind="youtube", slug="abc123", title="A video")
    row = item_row(ref, _block("a caption line"), 0.5, set())
    assert row["thumbnail"] == "https://i.ytimg.com/vi/abc123/hqdefault.jpg"
    assert row["actions"] == []
    assert "a caption line" in row["hover_preview"]


def test_artifact_kinds_falls_back_when_hub_is_none() -> None:
    assert artifact_kinds(None) == ["draft", "structure", "cad", "todo"]


def test_artifact_kinds_reads_role_from_hub() -> None:
    def handler_for(kind):
        specs = {
            "draft": SimpleNamespace(role="artifact"),
            "paper": SimpleNamespace(role="corpus"),
            "folder": SimpleNamespace(role="artifact"),
        }
        return SimpleNamespace(spec=specs[kind])

    hub = SimpleNamespace(kinds=["draft", "paper", "folder"], handler_for=handler_for)
    assert artifact_kinds(hub) == ["draft"]  # folder excluded, paper not artifact-role


def test_artifact_kinds_falls_back_on_hub_error() -> None:
    hub = SimpleNamespace(
        kinds=["draft"], handler_for=lambda k: (_ for _ in ()).throw(RuntimeError())
    )
    assert artifact_kinds(hub) == ["draft", "structure", "cad", "todo"]
