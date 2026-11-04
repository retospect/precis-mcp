"""Presentation PDF availability and shared signed download links (real PG)."""

from pathlib import Path

import pytest

from precis import file_links
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.presentation import PresentationHandler
from precis.runtime import PrecisRuntime
from precis.store import Store

_SHA = "e" * 64


def _seed(store: Store, *, with_pdf: bool) -> int:
    ref = store.insert_ref(kind="pres", slug="pdf-deck", title="Stored slides")
    if with_pdf:
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
                "size_bytes, storage_path) VALUES (%s, %s, 12, 12345, 'deck.pdf')",
                (_SHA, _SHA),
            )
            conn.execute(
                "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (_SHA, ref.id)
            )
    return ref.id


@pytest.mark.parametrize("with_pdf", [True, False])
def test_overview_reports_pdf_availability_and_web_reader(
    store: Store, with_pdf: bool
) -> None:
    ref_id = _seed(store, with_pdf=with_pdf)
    handler = PresentationHandler(hub=Hub(store=store))

    body = handler.get(id=f"pr{ref_id}").body

    assert f"/pres/{ref_id}" in body.split("Next:", 1)[1]
    if with_pdf:
        assert "pdf: present · pages: 12 · size: 12345 B" in body
        assert "view='pdf_link'" in body
    else:
        assert "pdf: missing" in body
        assert "view='pdf_link'" not in body


@pytest.mark.parametrize("path_view", [True, False])
def test_pdf_link_is_declared_and_download_token_verifies(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    path_view: bool,
) -> None:
    monkeypatch.setenv("PRECIS_FILE_LINK_KEY", "pres-pdf-link-test-key")
    monkeypatch.setenv("PRECIS_SECRETS_FILE_DIR", str(tmp_path))
    monkeypatch.delenv("PRECIS_WEB_BASE_URL", raising=False)
    ref_id = _seed(store, with_pdf=True)
    handler = PresentationHandler(hub=Hub(store=store))
    assert "pdf_link" in handler.accepted_views()

    body, is_error = runtime_with_store.dispatch_with_status(
        "get",
        {
            "kind": "pres",
            "id": f"pr{ref_id}/pdf_link" if path_view else f"pr{ref_id}",
            **({} if path_view else {"view": "pdf_link"}),
        },
    )

    assert not is_error, body
    url = next(line[5:] for line in body.splitlines() if line.startswith("url: "))
    assert url.startswith(f"/files/pdf/{_SHA}?t=")
    claims = file_links.verify(url.split("?t=", 1)[1], op="get", sha=_SHA, store=store)
    assert claims.ref_id == ref_id
    assert f"# pr{ref_id} — PDF download link" in body
    assert f"sha256: {_SHA}" in body
    assert "bytes: 12345 B · pages: 12" in body
    assert "Z (reusable until then)" in body
    assert f"curl -fo pdf-deck.pdf '{url}'" in body


def test_pdf_link_missing_attachment_names_pres_ingest(store: Store) -> None:
    ref_id = _seed(store, with_pdf=False)
    handler = PresentationHandler(hub=Hub(store=store))

    with pytest.raises(BadInput, match="holds no PDF") as exc:
        handler.get(id=f"pr{ref_id}", view="pdf_link")

    assert "precis-pres-help" in str(exc.value.next)
