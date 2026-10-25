"""``get(kind='paper', view='pdf_link')`` — the MCP half of the signed
PDF download link (gr517423, ``paper-backup-pdf-transfer.md`` slice 3).

Real Postgres: the view reads ``pdfs`` for the byte-size/page line and
the token must verify against the same key ``precis_web`` would use.
The key rides the env layer (``PRECIS_FILE_LINK_KEY``); the route half
lives in ``tests/precis_web/test_files.py``."""

from __future__ import annotations

import time
from typing import Any

import pytest

from precis import file_links
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.store import Store

_SHA = "f" * 64


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


@pytest.fixture(autouse=True)
def _link_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("PRECIS_FILE_LINK_KEY", "pdf-link-test-key")
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


def _url(body: str) -> str:
    return next(line[5:] for line in body.splitlines() if line.startswith("url: "))


def _token(body: str) -> str:
    return _url(body).split("?t=", 1)[1]


class TestPdfLinkView:
    def test_link_shape_round_trips(self, store: Store, handler: PaperHandler) -> None:
        ref_id = _seed(store, "linkshape2020")
        body = handler.get(id="linkshape2020", view="pdf_link").body
        url = _url(body)
        assert url.startswith(f"/files/pdf/{_SHA}?t=v1.get.{_SHA}.{ref_id}.")
        claims = file_links.verify(_token(body), op="get", sha=_SHA, store=store)
        assert claims.ref_id == ref_id
        assert f"sha256: {_SHA}" in body
        assert "bytes: 12345 B" in body
        assert "pages: 12" in body
        assert f"curl -fo linkshape2020.pdf '{url}'" in body

    def test_unset_base_url_says_so_and_returns_bare_path(
        self, store: Store, handler: PaperHandler
    ) -> None:
        _seed(store, "linkbase2020")
        body = handler.get(id="linkbase2020", view="pdf_link").body
        assert "PRECIS_WEB_BASE_URL is not set" in body.splitlines()[0]
        assert _url(body).startswith("/files/pdf/")

    def test_base_url_joins_onto_path(
        self, store: Store, handler: PaperHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(store, "linkjoin2020")
        monkeypatch.setenv("PRECIS_WEB_BASE_URL", "https://web.example/")
        body = handler.get(id="linkjoin2020", view="pdf_link").body
        assert _url(body).startswith(f"https://web.example/files/pdf/{_SHA}")
        assert "PRECIS_WEB_BASE_URL is not set" not in body

    def test_ttl_s_clamps_at_3600(self, store: Store, handler: PaperHandler) -> None:
        _seed(store, "linkttl2020")
        before = int(time.time())
        body = handler.get(
            id="linkttl2020", view="pdf_link", args={"ttl_s": 99_999}
        ).body
        exp = file_links.parse(_token(body)).exp
        assert before + 3600 <= exp <= int(time.time()) + 3600
        assert "expires: " in body and body.split("expires: ")[1][19] == "Z"

    def test_default_ttl_600(self, store: Store, handler: PaperHandler) -> None:
        _seed(store, "linkdef2020")
        before = int(time.time())
        body = handler.get(id="linkdef2020", view="pdf_link").body
        exp = file_links.parse(_token(body)).exp
        assert before + 600 <= exp <= int(time.time()) + 600

    def test_no_pdf_refuses_with_pdf_url_next(
        self, store: Store, handler: PaperHandler
    ) -> None:
        _seed(store, "linkless2020", with_pdf=False)
        with pytest.raises(BadInput) as exc:
            handler.get(id="linkless2020", view="pdf_link")
        assert "pdf_url" in str(exc.value.next)
        assert "linkless2020" in str(exc.value.next) or "pa" in str(exc.value.next)

    @pytest.mark.parametrize(
        "slug,bad_ttl",
        [
            ("linkbad2020", "soon"),
            ("linkinf2020", float("inf")),
            ("linknone2020", None),
        ],
    )
    def test_bad_ttl_refuses(
        self, store: Store, handler: PaperHandler, slug: str, bad_ttl: Any
    ) -> None:
        """``int(inf)`` raises OverflowError — JSON ``Infinity`` must
        land as BadInput, not an unmapped error."""
        _seed(store, slug)
        with pytest.raises(BadInput, match="ttl_s"):
            handler.get(id=slug, view="pdf_link", args={"ttl_s": bad_ttl})
