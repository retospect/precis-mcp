"""``get(view='page_image')`` + the figure-pointer lines on chunk reads
(gr520326).

Real Postgres: the view resolves ``args={'ord': N}`` against
``chunk_blobs`` / Marker ``page-N`` spans / ``page_first`` and mints a
``file_links`` token the ``/files/blob`` + ``/files/pdf_page`` routes
verify (route half: ``tests/precis_web/test_files.py``). The mint key
rides the env layer (``PRECIS_FILE_LINK_KEY``), same fixture as
``test_paper_pdf_link.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis import file_links
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers._paper_text import _is_caption_only_block
from precis.handlers.paper import PaperHandler
from precis.store import Store
from precis.store.types import ChunkInsert

_SHA = "f" * 64
_BLOB_SHA = "a" * 64
_BLOB = b"\x89PNG\r\n\x1a\nfake-crop-bytes"


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


@pytest.fixture(autouse=True)
def _link_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("PRECIS_FILE_LINK_KEY", "page-image-test-key")
    monkeypatch.setenv("PRECIS_SECRETS_FILE_DIR", str(tmp_path))
    monkeypatch.delenv("PRECIS_WEB_BASE_URL", raising=False)


def _seed(store: Store, slug: str, *, with_pdf: bool = True) -> int:
    ref_id = store.insert_ref(
        kind="paper",
        slug=slug,
        title=f"Title of {slug}",
        provider="manual",
        meta={},
    ).id
    if with_pdf:
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
                "size_bytes, storage_path) VALUES (%s, %s, 12, 12345, "
                "'x.pdf') ON CONFLICT DO NOTHING",
                (_SHA, _SHA),
            )
            conn.execute(
                "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s",
                (_SHA, ref_id),
            )
    return ref_id


def _chunks(store: Store, ref_id: int, blocks: list[dict[str, Any]]) -> list[Any]:
    """Insert body chunks; ``meta={'chunk_kind': …}`` picks the kind."""
    return store.chunks.insert_chunks(
        ref_id,
        [
            ChunkInsert(ord=i, text=b["text"], meta=b.get("meta") or {})
            for i, b in enumerate(blocks)
        ],
    )


def _add_blob(store: Store, chunk_id: int, sha: str = _BLOB_SHA) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_blobs "
            "(chunk_id, bytes, mime, sha256, size_bytes) "
            "VALUES (%s, %s, %s, %s, %s)",
            (chunk_id, _BLOB, "image/png", sha, len(_BLOB)),
        )


def _set_page_first(store: Store, ref_id: int, ord_: int, page: int) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET page_first = %s, page_last = %s "
            "WHERE ref_id = %s AND ord = %s",
            (page, page, ref_id, ord_),
        )


def _url(body: str) -> str:
    return next(line[5:] for line in body.splitlines() if line.startswith("url: "))


def _token(body: str) -> str:
    return _url(body).split("?t=", 1)[1]


class TestPageImageView:
    def test_page_link_shape_round_trips(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref_id = _seed(store, "pimg2020")
        body = handler.get(id="pimg2020", view="page_image", args={"page": 3}).body
        assert _url(body).startswith(f"/files/pdf_page/{_SHA}/3?t=")
        claims = file_links.verify(_token(body), op="get", sha=_SHA, store=store)
        assert claims.ref_id == ref_id
        assert "page: 3 (0-based PDF index)" in body
        assert "expires: " in body
        assert "pimg2020-p3.png" in body

    def test_page_and_ord_are_mutually_exclusive(
        self, store: Store, handler: PaperHandler
    ) -> None:
        _seed(store, "pimgxor2020")
        with pytest.raises(BadInput, match="exactly one of"):
            handler.get(id="pimgxor2020", view="page_image")
        with pytest.raises(BadInput, match="exactly one of"):
            handler.get(
                id="pimgxor2020",
                view="page_image",
                args={"page": 1, "ord": 0},
            )

    def test_page_bounds(self, store: Store, handler: PaperHandler) -> None:
        _seed(store, "pimgbounds2020")
        with pytest.raises(BadInput, match=">= 0"):
            handler.get(id="pimgbounds2020", view="page_image", args={"page": -1})
        with pytest.raises(BadInput, match="past the end"):
            # The seeded pdfs row says 12 pages — page 12 is off the end.
            handler.get(id="pimgbounds2020", view="page_image", args={"page": 12})

    def test_bad_arg_types_refuse(self, store: Store, handler: PaperHandler) -> None:
        _seed(store, "pimgtypes2020")
        with pytest.raises(BadInput, match="0-based PDF page"):
            handler.get(id="pimgtypes2020", view="page_image", args={"page": "x"})
        with pytest.raises(BadInput, match="chunk ord"):
            handler.get(id="pimgtypes2020", view="page_image", args={"ord": "x"})
        with pytest.raises(BadInput, match="ttl_s"):
            handler.get(
                id="pimgtypes2020",
                view="page_image",
                args={"page": 1, "ttl_s": "soon"},
            )

    def test_no_pdf_refuses(self, store: Store, handler: PaperHandler) -> None:
        _seed(store, "pimgless2020", with_pdf=False)
        with pytest.raises(BadInput, match="holds no PDF"):
            handler.get(id="pimgless2020", view="page_image", args={"page": 0})

    def test_ord_with_stored_crop_mints_blob_link(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref_id = _seed(store, "pimgcrop2020")
        rows = _chunks(store, ref_id, [{"text": "*Figure 1: schematic*"}])
        _add_blob(store, rows[0].id)
        body = handler.get(id="pimgcrop2020", view="page_image", args={"ord": 0}).body
        assert _url(body).startswith(f"/files/blob/{_BLOB_SHA}?t=")
        claims = file_links.verify(_token(body), op="get", sha=_BLOB_SHA, store=store)
        assert claims.ref_id == ref_id
        assert f"sha256: {_BLOB_SHA}" in body

    def test_ord_resolves_its_own_page_span(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref_id = _seed(store, "pimgspan2020")
        _chunks(
            store,
            ref_id,
            [{"text": '<span id="page-7-0"></span>*Figure 9: pinout*'}],
        )
        body = handler.get(id="pimgspan2020", view="page_image", args={"ord": 0}).body
        assert _url(body).startswith(f"/files/pdf_page/{_SHA}/7?t=")

    def test_ord_borrows_the_nearest_neighbour_span(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """The caption-only datasheet failure (gr520326): the legend
        chunk carries no span and ``page_first`` is a drifted TOC
        interpolation — the nearest neighbour's Marker ``page-N`` span
        brackets tighter."""
        ref_id = _seed(store, "pimgnb2020")
        _chunks(
            store,
            ref_id,
            [
                {"text": "*Figure 211: Circuit configuration no. 6*"},
                {"text": "prose"},
                {"text": '<span id="page-9-0"></span>more prose'},
            ],
        )
        _set_page_first(store, ref_id, 0, 1)  # the wrong-but-present hint
        body = handler.get(id="pimgnb2020", view="page_image", args={"ord": 0}).body
        assert _url(body).startswith(f"/files/pdf_page/{_SHA}/9?t=")

    def test_preceding_neighbour_uses_its_last_span(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A neighbour block straddling a page turn carries two
        ``page-N`` spans; the pointer wants the LAST one — the span
        nearest the requesting chunk (gr520326 review)."""
        ref_id = _seed(store, "pimglast2020")
        _chunks(
            store,
            ref_id,
            [
                {
                    "text": (
                        '<span id="page-5-0"></span>tail of p5 '
                        '<span id="page-6-0"></span>top of p6'
                    )
                },
                {"text": "*Figure 4: block diagram*"},
            ],
        )
        body = handler.get(id="pimglast2020", view="page_image", args={"ord": 1}).body
        assert _url(body).startswith(f"/files/pdf_page/{_SHA}/6?t=")

    def test_ord_falls_back_to_page_first(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref_id = _seed(store, "pimgfirst2020")
        _chunks(store, ref_id, [{"text": "a caption-ish block"}])
        _set_page_first(store, ref_id, 0, 4)
        body = handler.get(id="pimgfirst2020", view="page_image", args={"ord": 0}).body
        assert _url(body).startswith(f"/files/pdf_page/{_SHA}/4?t=")

    def test_missing_ord_and_unresolvable_page(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref_id = _seed(store, "pimgnone2020")
        _chunks(store, ref_id, [{"text": "no span, no page_first"}])
        with pytest.raises(NotFound, match="no body chunk at ~9"):
            handler.get(id="pimgnone2020", view="page_image", args={"ord": 9})
        with pytest.raises(NotFound, match="no page can be derived"):
            handler.get(id="pimgnone2020", view="page_image", args={"ord": 0})


class TestFigurePointerLines:
    """The chunk read itself earns the door (gr520326): figure/caption
    kinds and caption-only paragraphs render a ``figure image:`` or
    ``page N of <pa>:`` line; plain prose pays nothing."""

    def test_caption_only_chunk_gets_page_pointer(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref_id = _seed(store, "figptr2020")
        _chunks(
            store,
            ref_id,
            [
                {"text": "Some prose block."},
                {
                    "text": (
                        '<span id="page-20-0"></span>'
                        "*Figure 211: Circuit configuration no. 6*\n"
                        "*Table 93: Absolute maximum ratings*"
                    )
                },
            ],
        )
        body = handler.get(id="figptr2020~0..1").body
        assert (
            f"page 20 of pa{ref_id}: get(id='pa{ref_id}', "
            "view='page_image', args={'page': 20})" in body
        )

    def test_figure_kind_with_blob_gets_image_pointer(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref_id = _seed(store, "figimg2020")
        rows = _chunks(
            store,
            ref_id,
            [{"text": "![](_page_3_Figure_1.png)", "meta": {"chunk_kind": "figure"}}],
        )
        _add_blob(store, rows[0].id)
        body = handler.get(id="figimg2020~0").body
        assert "figure image: pc" in body
        assert f"get(id='pa{ref_id}', view='page_image', args={{'ord': 0}})" in body

    def test_caption_next_to_blob_figure_links_the_crop(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """The crop sits on the *figure* block; its caption-only
        neighbour (ord ± 1) still earns the ``figure image:`` line
        naming that ord."""
        ref_id = _seed(store, "figadj2020")
        rows = _chunks(
            store,
            ref_id,
            [
                {"text": "![](f.png)", "meta": {"chunk_kind": "figure"}},
                {"text": "*Figure 2: land pattern*"},
            ],
        )
        _add_blob(store, rows[0].id)
        body = handler.get(id="figadj2020~1").body
        assert f"get(id='pa{ref_id}', view='page_image', args={{'ord': 0}})" in body

    def test_page_first_only_pointer_is_marked_approximate(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """``page_first`` is a TOC-anchor interpolation that drifts on
        image-heavy datasheets — the line must say ``≈`` (the WIP
        unpacked the exactness flag inverted, hiding the mark)."""
        ref_id = _seed(store, "figapprox2020")
        _chunks(store, ref_id, [{"text": "*Mechanical drawing*"}])
        _set_page_first(store, ref_id, 0, 33)
        body = handler.get(id="figapprox2020~0").body
        assert f"page ≈33 of pa{ref_id}:" in body

    def test_plain_paragraph_pays_nothing(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref_id = _seed(store, "figclean2020")
        _chunks(
            store,
            ref_id,
            [{"text": "The device integrates a 2.4 GHz transceiver."}],
        )
        body = handler.get(id="figclean2020~0").body
        assert "page_image" not in body
        assert "figure image:" not in body


class TestCaptionOnlyDetector:
    """``_is_caption_only_block`` — the pointer-lines gate. A lead line
    is a *numbered* legend (``Figure 3:`` — the ``[.:]`` after the
    number is the discriminator) or a fully emphasis-wrapped line;
    headings, ``Table of Contents`` and >200-char lines never count
    (gr520326 review: the wide keyword net fired on prose)."""

    @pytest.mark.parametrize(
        "text",
        [
            "Figure 3 shows the measured transfer curve across loads.",
            "Table 5 lists the register map; the reset values follow.",
            "Scheme 2 was used to synthesise the intermediate.",
            "Layout: keep the decoupling capacitor close to the pin.",
            "Picture this: a cross-section of the package.",
            "## Layout guidelines",
            "# Mechanical specifications",
            "## Table of Contents",
            "*Table of Contents*",
            # A >200-char lead line is a paragraph, not a legend.
            "*Figure 3: " + "x" * 220 + "*",
            # One legend in a paragraph stays prose (half-lines rule).
            "*Figure 3: x*\nThe curve saturates at high load.\nMore text.",
        ],
    )
    def test_prose_and_headings_are_not_caption_only(self, text: str) -> None:
        assert not _is_caption_only_block(text)

    @pytest.mark.parametrize(
        "text",
        [
            "*Figure 211: Circuit configuration no. 6*",
            "*Table 93: Absolute maximum ratings*",
            "**Mechanical drawing for the QFN-32 package**",
            "Figure 3: measured transfer curve.",
            "Fig. 16. (a) NOx reduction mechanism",
            "Scheme 2: synthesis of the intermediate",
            "*Figure 2: land pattern*\nNote: dimensions in mm",
        ],
    )
    def test_legends_are_caption_only(self, text: str) -> None:
        assert _is_caption_only_block(text)
