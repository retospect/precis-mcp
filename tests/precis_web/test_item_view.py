"""Unit tests for the ``ItemPresenter`` contract (``item_view.py``).

Exercises the presenter methods directly (no FastAPI client needed) —
``title_meta`` / ``chunk_full`` / ``thumbnail`` / ``actions`` are new
surface on the per-kind presenter contract; the per-kind registry and
the ``artifact_kinds`` facet helper back the ``/drive`` route tests in
``test_routes.py``.
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
    base = {
        "id": 1,
        "kind": "paper",
        "slug": None,
        "title": "t",
        "meta": {},
        "year": None,
        "authors": None,
        "pdf_sha256": None,
    }
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


def test_chunk_full_is_chunk_only_no_abstract() -> None:
    """``chunk_full`` is the matching chunk alone — no abstract splice,
    unlike the retired ``hover_preview``."""
    p = ItemPresenter("paper")
    assert p.chunk_full(_block("matching chunk text")) == "matching chunk text"


def test_chunk_full_caps_at_hover_chars() -> None:
    from precis_web.item_view import _HOVER_CHARS

    p = ItemPresenter("paper")
    cf = p.chunk_full(_block("y" * 900))
    assert len(cf) <= _HOVER_CHARS
    assert cf.endswith("…")


def test_title_meta_carries_full_title_journal_authors_year() -> None:
    ref = _ref(
        title="A   very\nlong   title",
        meta={"journal": "Nature"},
        authors=[{"name": "Doe, Jane"}],
        year=2020,
    )
    p = ItemPresenter("paper")
    tm = p.title_meta(ref)
    assert tm["title"] == "A very long title"
    assert tm["journal"] == "Nature"
    assert tm["authors"] == ["Doe, Jane"]
    assert tm["year"] == 2020


def test_title_meta_defaults_when_no_meta() -> None:
    ref = _ref(title="Plain", meta={})
    p = ItemPresenter("web")
    tm = p.title_meta(ref)
    assert tm["title"] == "Plain"
    assert tm["journal"] is None
    assert tm["authors"] == []
    assert tm["year"] is None


def test_preview_prefers_gloss_over_chunk_text() -> None:
    p = ItemPresenter("paper")
    assert p.preview(_block("chunk text"), "a gloss") == "a gloss"


def test_preview_falls_back_to_truncated_chunk_text_without_gloss() -> None:
    p = ItemPresenter("paper")
    assert p.preview(_block("chunk text"), None) == "chunk text"


def test_preview_caps_at_140_for_both_paths() -> None:
    from precis_web.item_view import _PREVIEW_CHARS

    assert _PREVIEW_CHARS == 140
    p = ItemPresenter("paper")
    gloss_preview = p.preview(_block("x"), "g" * 200)
    chunk_preview = p.preview(_block("c" * 200), None)
    assert len(gloss_preview) == 140
    assert gloss_preview.endswith("…")
    assert len(chunk_preview) == 140
    assert chunk_preview.endswith("…")


def test_state_adds_pdf_badge_for_pipeline_kind_with_pdf() -> None:
    ref = _ref(kind="paper", pdf_sha256="deadbeef")
    p = ItemPresenter("paper")
    badges = p.state(ref, has_chunks=True)
    assert any(b["label"] == "pdf" for b in badges)


def test_state_no_pdf_badge_for_non_pipeline_kind() -> None:
    ref = _ref(kind="web", pdf_sha256="deadbeef")
    p = ItemPresenter("web")
    badges = p.state(ref, has_chunks=True)
    assert not any(b["label"] == "pdf" for b in badges)


def test_state_adds_llm_label_badge_when_meta_carries_one() -> None:
    ref = _ref(
        kind="paper",
        pdf_sha256=None,
        meta={"llm_label": "core", "llm_reason": "fits the quest"},
    )
    p = ItemPresenter("paper")
    badges = p.state(ref, has_chunks=False)
    label_badges = [b for b in badges if b["label"] == "core"]
    assert len(label_badges) == 1
    assert label_badges[0]["cls"] == "bg-emerald-100 text-emerald-700"
    assert label_badges[0]["title"] == "fits the quest"


def test_state_llm_label_badge_falls_back_to_label_without_reason() -> None:
    ref = _ref(kind="paper", pdf_sha256=None, meta={"llm_label": "off"})
    p = ItemPresenter("paper")
    badges = p.state(ref, has_chunks=False)
    label_badges = [b for b in badges if b["label"] == "off"]
    assert len(label_badges) == 1
    assert label_badges[0]["cls"] == "bg-slate-200 text-slate-500"
    assert label_badges[0]["title"] == "off"


def test_state_ignores_unknown_llm_label_value() -> None:
    ref = _ref(kind="paper", pdf_sha256=None, meta={"llm_label": "bogus"})
    p = ItemPresenter("paper")
    badges = p.state(ref, has_chunks=False)
    assert not any(b["label"] == "bogus" for b in badges)


def test_state_no_llm_label_badge_for_non_pipeline_kind() -> None:
    ref = _ref(kind="web", meta={"llm_label": "core"})
    p = ItemPresenter("web")
    badges = p.state(ref, has_chunks=False)
    assert badges == []


def test_default_thumbnail_is_empty_actions_are_universal() -> None:
    """No default thumbnail, but the universal move/delete/tag quick
    actions (WS1a) are always present, keyed to the ref's own kind + id
    (falling back to the numeric ref_id when there's no slug)."""
    p = ItemPresenter("paper")
    assert p.thumbnail(_ref()) is None
    actions = p.actions(_ref())
    assert [a["type"] for a in actions] == ["move", "delete", "tag"]
    assert all(a["kind"] == "paper" and a["id"] == "1" for a in actions)


def test_links_doi_row_carries_libkey_download_plus_search_tier() -> None:
    """A DOI row's off-site links: DOI abstract, the direct LibKey
    full-text PDF (marked ``download`` so the "Open all downloads" button
    walks it), then the UoL/Scholar search tier. Regression for the
    LibKey "to-pdf" link dropped when papers_needed folded into Drive."""
    links = ItemPresenter("paper").links("10.1016/j.enbuild.2024.114668")
    labels = [link["label"] for link in links]
    assert labels == ["DOI", "⧉", "LibKey ↓", "UoL", "Scholar"]
    # The ⧉ entry is copy-to-clipboard (bare DOI, no href) — for pasting
    # into a library/ILL search.
    copy = next(link for link in links if link.get("clip"))
    assert copy["clip"] == "10.1016/j.enbuild.2024.114668"
    assert "href" not in copy
    libkey = next(link for link in links if link["label"] == "LibKey ↓")
    assert libkey["download"] is True
    assert libkey["href"] == (
        "https://libkey.io/libraries/2545/10.1016/j.enbuild.2024.114668"
    )
    # Only the LibKey entry is a download; the search links are not walked.
    assert [link for link in links if link.get("download")] == [libkey]


def test_links_arxiv_row_gets_pdf_download_not_libkey() -> None:
    """An arXiv preprint has its own free PDF (``download``) but no LibKey
    key, so the download tier is the arXiv PDF alone."""
    links = ItemPresenter("paper").links("arxiv:2401.01234")
    downloads = [link for link in links if link.get("download")]
    assert [link["label"] for link in downloads] == ["arXiv ↓"]
    assert downloads[0]["href"] == "https://arxiv.org/pdf/2401.01234"
    assert not any(link["label"] == "LibKey ↓" for link in links)
    # Copy entry carries the bare arXiv id (scheme prefix stripped).
    copy = next(link for link in links if link.get("clip"))
    assert copy["clip"] == "2401.01234"


def test_links_empty_without_identifier() -> None:
    assert ItemPresenter("paper").links(None) == []
    assert ItemPresenter("paper").links("") == []


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
        ("draft", "/smartdraft/7"),
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
    assert "a caption line" in row["chunk_full"]
    assert row["title_meta"]["title"] == "A video"


def test_artifact_kinds_falls_back_when_hub_is_none() -> None:
    assert artifact_kinds(None) == [
        "cad",
        "draft",
        "figure",
        "mermaid",
        "plan",
        "structure",
        "todo",
    ]


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
    assert artifact_kinds(hub) == [
        "cad",
        "draft",
        "figure",
        "mermaid",
        "plan",
        "structure",
        "todo",
    ]
