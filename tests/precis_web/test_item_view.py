"""Unit tests for the ``ItemPresenter`` contract (``item_view.py``).

Exercises the presenter methods directly (no FastAPI client needed) —
``hover_preview`` / ``thumbnail`` / ``actions`` are new surface on the
Slice-3 contract (``docs/proposals/unified-item-view.md``); the
per-kind registry and the ``artifact_kinds`` facet helper back the
``/drive`` route tests in ``test_routes.py``.
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


def test_name_caps_long_title_for_drive_row() -> None:
    """A ref whose title *is* its body (long websearch query / citation
    claim) is capped to one line for the /drive row — storage stays full,
    display truncates."""
    from precis_web.item_view import DISPLAY_TITLE_LIMIT

    long_claim = "Across Cu Ni Pt and Pd catalysts the C2+ Faradaic efficiency " * 6
    p = ItemPresenter("citation")
    out = p.name(_ref(kind="citation", title=long_claim))
    assert out.endswith("…")
    assert "\n" not in out
    assert len(out) <= DISPLAY_TITLE_LIMIT


def test_name_falls_back_to_kind_and_id_when_title_empty() -> None:
    p = ItemPresenter("web")
    assert p.name(_ref(kind="web", id=7, title="")) == "web #7"


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


def test_default_thumbnail_is_empty_actions_are_universal() -> None:
    """No default thumbnail, but the universal move/delete/tag quick
    actions (WS1a) are always present, keyed to the ref's own kind + id
    (falling back to the numeric ref_id when there's no slug)."""
    p = ItemPresenter("paper")
    assert p.thumbnail(_ref()) is None
    actions = p.actions(_ref())
    assert [a["type"] for a in actions] == ["move", "delete", "tag"]
    assert all(a["kind"] == "paper" and a["id"] == "1" for a in actions)


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


def test_open_url_routes_artifact_kinds_to_rich_editor() -> None:
    """Drive click-through for the slug-addressed artifact kinds lands in
    their dedicated editor, not the generic /refs reader (gripe 171150)."""
    for kind, expected in (
        ("cad", "/cad/hex-block"),
        ("structure", "/structure/hex-block"),
        ("figure", "/figure/hex-block"),
        ("mermaid", "/mermaid/hex-block"),
    ):
        ref = _ref(kind=kind, slug="hex-block", id=7)
        assert ItemPresenter(kind).open_url(ref) == expected
    # A kind with no override still falls back to the generic browser.
    assert ItemPresenter("web").open_url(_ref(kind="web", id=7)) == "/refs/web/7"


def test_open_url_routes_id_addressed_readers() -> None:
    """The id-addressed rich readers (paper/draft/datasheet) land in their
    dedicated page, not the generic /refs reader — which 400s for `draft`
    since it isn't a browsable-tab kind (the Drive click-through bug)."""
    for kind, expected in (
        ("paper", "/papers/7"),
        ("draft", "/drafts/7"),
        ("datasheet", "/datasheets/7"),
    ):
        assert ItemPresenter(kind).open_url(_ref(kind=kind, id=7)) == expected


def test_item_row_carries_hover_thumbnail_actions() -> None:
    ref = _ref(kind="youtube", slug="abc123", title="A video")
    row = item_row(ref, _block("a caption line"), 0.5, set())
    assert row["thumbnail"] == "https://i.ytimg.com/vi/abc123/hqdefault.jpg"
    # Universal per-row actions (WS1a) address by slug when the ref has one.
    assert {a["type"]: a["id"] for a in row["actions"]} == {
        "move": "abc123",
        "delete": "abc123",
        "tag": "abc123",
    }
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
