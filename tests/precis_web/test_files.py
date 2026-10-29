"""``GET /files/pdf/{sha}?t=<token>`` — the precis_web half of the
signed-PDF-link slice (gr517423). The FakeStore-backed app answers
``get_ref`` / ``pdf_storage_path`` / ``ref_cite_keys`` from the seeded
fixtures; the signing key comes from the env layer so no vault is
needed. Verify-before-anything is the security contract: a caller with
no valid token can only ever see 403/410."""

from __future__ import annotations

import io
import struct
import tarfile

import pytest
from fastapi.testclient import TestClient

from precis import file_links
from precis_web.app import create_app
from precis_web.config import WebConfig
from tests.precis_web.conftest import FakeRuntime, make_ref

_SHA = "ab" * 32
_OTHER_SHA = "cd" * 32
_PDF_BYTES = b"%PDF-1.4 fixture-bytes"


@pytest.fixture(autouse=True)
def _link_key(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("PRECIS_FILE_LINK_KEY", "web-test-key")
    monkeypatch.setenv("PRECIS_SECRETS_FILE_DIR", str(tmp_path))


@pytest.fixture
def paper_pdf(runtime: FakeRuntime, tmp_path):
    """Paper 50 (slug ``doe2020``) owning ``_SHA``, with the bytes on
    disk where ``pdfs.storage_path`` says they are."""
    pdf = tmp_path / "d" / "doe2020.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(_PDF_BYTES)
    runtime.store.papers.append(
        make_ref(
            id=50,
            kind="paper",
            slug="doe2020",
            title="Doe paper",
            pdf_sha256=_SHA,
        )
    )
    runtime.store.storage_paths[_SHA] = str(pdf)
    return pdf


def _client(runtime: FakeRuntime, tmp_path) -> TestClient:
    return TestClient(
        create_app(runtime=runtime, web_config=WebConfig(corpus_dir=tmp_path))
    )


def test_valid_token_streams_the_pdf(runtime: FakeRuntime, paper_pdf, tmp_path) -> None:
    token = file_links.mint("get", _SHA, 50)
    r = _client(runtime, tmp_path).get(f"/files/pdf/{_SHA}?t={token}")
    assert r.status_code == 200
    assert r.content == _PDF_BYTES
    assert r.headers["content-disposition"].startswith("attachment")
    assert "doe2020.pdf" in r.headers["content-disposition"]
    assert "no-store" in r.headers["cache-control"]


def test_link_is_reusable_until_expiry(
    runtime: FakeRuntime, paper_pdf, tmp_path
) -> None:
    """Reto 2026-10-25: a download link may be fetched more than once
    inside its TTL — only the upload link (slice 6) is single-use."""
    token = file_links.mint("get", _SHA, 50)
    client = _client(runtime, tmp_path)
    assert client.get(f"/files/pdf/{_SHA}?t={token}").status_code == 200
    assert client.get(f"/files/pdf/{_SHA}?t={token}").status_code == 200


def test_no_token_403(runtime: FakeRuntime, tmp_path) -> None:
    assert _client(runtime, tmp_path).get(f"/files/pdf/{_SHA}").status_code == 403


def test_bad_token_403(runtime: FakeRuntime, tmp_path) -> None:
    assert (
        _client(runtime, tmp_path).get(f"/files/pdf/{_SHA}?t=garbage").status_code
        == 403
    )


def test_token_for_another_sha_403(runtime: FakeRuntime, paper_pdf, tmp_path) -> None:
    """A well-signed token for sha B does not open the path for sha A."""
    token = file_links.mint("get", _OTHER_SHA, 50)
    assert (
        _client(runtime, tmp_path).get(f"/files/pdf/{_SHA}?t={token}").status_code
        == 403
    )


def test_non_ascii_token_403(runtime: FakeRuntime, paper_pdf, tmp_path) -> None:
    """``?t=…%C3%A9`` decodes to a non-ASCII token — ``compare_digest``
    would TypeError on it, so it must be a 403, never a 500."""
    r = _client(runtime, tmp_path).get(f"/files/pdf/{_SHA}?t=v1.get.{_SHA}.1.2.é")
    assert r.status_code == 403


def test_expired_token_410(runtime: FakeRuntime, paper_pdf, tmp_path) -> None:
    token = file_links.mint("get", _SHA, 50, now=1_000_000)
    r = _client(runtime, tmp_path).get(f"/files/pdf/{_SHA}?t={token}")
    assert r.status_code == 410


def test_ownership_moved_404(runtime: FakeRuntime, paper_pdf, tmp_path) -> None:
    """The token verifies, but the named ref no longer owns the sha
    (paper 10's ``pdf_sha256`` is ``abc``, not ``_SHA``)."""
    token = file_links.mint("get", _SHA, 10)
    assert (
        _client(runtime, tmp_path).get(f"/files/pdf/{_SHA}?t={token}").status_code
        == 404
    )


def test_missing_file_404(runtime: FakeRuntime, tmp_path) -> None:
    """Valid token, valid owner — but the bytes aren't on this node."""
    runtime.store.papers.append(
        make_ref(
            id=51,
            kind="paper",
            slug="ghost2020",
            title="Ghost paper",
            pdf_sha256=_OTHER_SHA,
        )
    )
    token = file_links.mint("get", _OTHER_SHA, 51)
    assert (
        _client(runtime, tmp_path).get(f"/files/pdf/{_OTHER_SHA}?t={token}").status_code
        == 404
    )


def test_cite_key_fallback_checks_recorded_size(runtime: FakeRuntime, tmp_path) -> None:
    """No ``storage_path`` → the cite_key convention resolves
    ``<root>/d/doe2020.pdf``, which may be a stale earlier PDF of the
    same ref: it serves only when its size matches ``pdfs.size_bytes``."""
    runtime.store.papers.append(
        make_ref(id=52, kind="paper", slug="doe2020b", title="B", pdf_sha256=_SHA)
    )
    conv = tmp_path / "d" / "doe2020b.pdf"
    conv.parent.mkdir(parents=True)
    conv.write_bytes(_PDF_BYTES)
    token = file_links.mint("get", _SHA, 52)
    client = _client(runtime, tmp_path)

    # No recorded size → can't corroborate → 404.
    assert client.get(f"/files/pdf/{_SHA}?t={token}").status_code == 404
    # Stale file under the convention name → size mismatch → 404.
    runtime.store.pdf_sizes[_SHA] = len(_PDF_BYTES) + 1
    assert client.get(f"/files/pdf/{_SHA}?t={token}").status_code == 404
    # Size matches the record → the convention file serves.
    runtime.store.pdf_sizes[_SHA] = len(_PDF_BYTES)
    r = client.get(f"/files/pdf/{_SHA}?t={token}")
    assert r.status_code == 200
    assert r.content == _PDF_BYTES


def test_recorded_leg_needs_no_size_check(
    runtime: FakeRuntime, paper_pdf, tmp_path
) -> None:
    """The ingest-recorded ``storage_path`` is authoritative — the size
    corroboration is for the convention fallback only."""
    runtime.store.pdf_sizes[_SHA] = 1  # deliberately wrong
    token = file_links.mint("get", _SHA, 50)
    assert (
        _client(runtime, tmp_path).get(f"/files/pdf/{_SHA}?t={token}").status_code
        == 200
    )


# -- POST /files/upload/{ref_id} (slice 6) --------------------------------
# The route reads the RAW body (``request.stream()``) — no multipart
# framing, so ``curl --data-binary`` / ``content=`` is the contract; a
# ``files=`` post would spool the boundary bytes into the staged file.
# Staging lives in ``PRECIS_UPLOAD_STAGING`` — deliberately OUTSIDE the
# watched inbox (feat-a2 MF1) — so the fixture sets it to a sibling dir.


@pytest.fixture
def staging_dir(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """``PRECIS_UPLOAD_STAGING`` → a sibling of the watch inbox; returns
    the staging dir."""
    monkeypatch.setenv("PRECIS_WATCH_INBOX", str(tmp_path / "inbox"))
    staging = tmp_path / "staging"
    monkeypatch.setenv("PRECIS_UPLOAD_STAGING", str(staging))
    return staging


def _put_token(ref_id: int = 50, **kw) -> str:
    return file_links.mint("put", file_links.UPLOAD_SHA, ref_id, **kw)


def _post(client: TestClient, ref_id: int, token: str, body: bytes = _PDF_BYTES):
    return client.post(
        f"/files/upload/{ref_id}?t={token}",
        content=body,
        headers={"content-type": "application/pdf"},
    )


def test_upload_stages_pdf_and_answers_sha(
    runtime: FakeRuntime, staging_dir, tmp_path
) -> None:
    r = _post(_client(runtime, tmp_path), 50, _put_token(50))
    assert r.status_code == 200
    sha = r.json()["sha256"]
    # The staged name binds the upload to its token's ref.
    assert (staging_dir / f"50-{sha}.pdf").read_bytes() == _PDF_BYTES
    assert r.json()["bytes"] == len(_PDF_BYTES)
    assert "pdf_upload" in r.json()["next"]
    # The nonce tombstone lands — the link is now spent.
    assert list(staging_dir.glob("*.done"))


def test_upload_single_use_409(runtime: FakeRuntime, staging_dir, tmp_path) -> None:
    token = _put_token(50)
    client = _client(runtime, tmp_path)
    assert _post(client, 50, token).status_code == 200
    r = _post(client, 50, token)
    assert r.status_code == 409


def test_upload_no_token_403(runtime: FakeRuntime, staging_dir, tmp_path) -> None:
    r = _client(runtime, tmp_path).post(
        "/files/upload/50",
        content=_PDF_BYTES,
        headers={"content-type": "application/pdf"},
    )
    assert r.status_code == 403


def test_upload_get_token_403(runtime: FakeRuntime, staging_dir, tmp_path) -> None:
    """A download token can't be replayed as an upload — op is signed."""
    token = file_links.mint("get", _SHA, 50)
    r = _post(_client(runtime, tmp_path), 50, token)
    assert r.status_code == 403


def test_upload_wrong_ref_403(runtime: FakeRuntime, staging_dir, tmp_path) -> None:
    r = _post(_client(runtime, tmp_path), 99, _put_token(50))
    assert r.status_code == 403


def test_upload_expired_410(runtime: FakeRuntime, staging_dir, tmp_path) -> None:
    r = _post(_client(runtime, tmp_path), 50, _put_token(50, now=1_000_000))
    assert r.status_code == 410


def test_upload_non_pdf_415(runtime: FakeRuntime, staging_dir, tmp_path) -> None:
    r = _post(_client(runtime, tmp_path), 50, _put_token(50), body=b"<html>x")
    assert r.status_code == 415
    assert list(staging_dir.iterdir()) == []


def test_upload_oversize_413(
    runtime: FakeRuntime, staging_dir, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.ingest import pdf_upload

    monkeypatch.setattr(pdf_upload, "MAX_UPLOAD_BYTES", 16)
    r = _post(_client(runtime, tmp_path), 50, _put_token(50), body=_PDF_BYTES * 4)
    assert r.status_code == 413
    # An over-cap upload never reaches disk — the declared-length check
    # may refuse before ``.uploads/`` even exists.
    assert not staging_dir.exists() or list(staging_dir.iterdir()) == []


def test_upload_failed_attempt_frees_the_nonce(
    runtime: FakeRuntime, staging_dir, tmp_path
) -> None:
    """A refused upload (415) drops the tombstone again — the token
    stays reusable until expiry; only a SUCCESS spends it."""
    client = _client(runtime, tmp_path)
    token = _put_token(50)
    assert _post(client, 50, token, body=b"<html>x").status_code == 415
    assert _post(client, 50, token).status_code == 200


def test_upload_no_staging_503(
    runtime: FakeRuntime, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PRECIS_UPLOAD_STAGING`` unset — the route refuses, and never
    falls back into the watch inbox even though it IS configured."""
    monkeypatch.setenv("PRECIS_WATCH_INBOX", str(tmp_path / "inbox"))
    monkeypatch.delenv("PRECIS_UPLOAD_STAGING", raising=False)
    r = _post(_client(runtime, tmp_path), 50, _put_token(50))
    assert r.status_code == 503
    assert "PRECIS_UPLOAD_STAGING" in r.json()["detail"]


# -- POST /files/upload_tree/{ref_id} (gr517643 — the se workspace door)
#
# Same contract as the PDF door — token before a body byte, single-use
# nonce, staged name ref-bound — but its own token sentinel
# (``upload_tree``) and an uncompressed-tar gate (``ustar`` magic at
# offset 257). ``import_workspace``'s ``staged`` key consumes what it
# leaves.


def _tree_token(ref_id: int = 50, **kw) -> str:
    return file_links.mint("put", file_links.UPLOAD_TREE_SHA, ref_id, **kw)


def _post_tree(client: TestClient, ref_id: int, token: str, body: bytes):
    return client.post(
        f"/files/upload_tree/{ref_id}?t={token}",
        content=body,
        headers={"content-type": "application/x-tar"},
    )


def _tar_bytes(files: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in (files or {"manifest.json": b"{}"}).items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_upload_tree_stages_tar_and_answers_sha(
    runtime: FakeRuntime, staging_dir, tmp_path
) -> None:
    body = _tar_bytes()
    r = _post_tree(_client(runtime, tmp_path), 50, _tree_token(50), body)
    assert r.status_code == 200
    sha = r.json()["sha256"]
    assert (staging_dir / f"50-{sha}.tar").read_bytes() == body
    assert r.json()["bytes"] == len(body)
    assert "'staged'" in r.json()["next"]


def test_upload_tree_pdf_token_403(runtime: FakeRuntime, staging_dir, tmp_path) -> None:
    """A paper-upload token can't feed the tree door — each door's
    sentinel is signed into its token."""
    r = _post_tree(_client(runtime, tmp_path), 50, _put_token(50), _tar_bytes())
    assert r.status_code == 403


def test_upload_pdf_tree_token_403(runtime: FakeRuntime, staging_dir, tmp_path) -> None:
    """…and a tree token can't stage a PDF."""
    r = _post(_client(runtime, tmp_path), 50, _tree_token(50))
    assert r.status_code == 403


def test_upload_tree_non_tar_415(runtime: FakeRuntime, staging_dir, tmp_path) -> None:
    r = _post_tree(
        _client(runtime, tmp_path), 50, _tree_token(50), _PDF_BYTES + b"0" * 300
    )
    assert r.status_code == 415
    assert list(staging_dir.iterdir()) == []


def test_upload_tree_gzip_tar_415(runtime: FakeRuntime, staging_dir, tmp_path) -> None:
    """A gzipped tar has gzip magic, not ustar at 257 — the door takes
    uncompressed tars only."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("manifest.json")
        data = b"{}"
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    r = _post_tree(_client(runtime, tmp_path), 50, _tree_token(50), buf.getvalue())
    assert r.status_code == 415


def test_upload_tree_short_body_415(
    runtime: FakeRuntime, staging_dir, tmp_path
) -> None:
    """Fewer than 262 bytes can never show ustar magic — a 415, not a
    staged file."""
    r = _post_tree(_client(runtime, tmp_path), 50, _tree_token(50), b"x" * 100)
    assert r.status_code == 415
    assert list(staging_dir.iterdir()) == []


def test_upload_tree_single_use_409(
    runtime: FakeRuntime, staging_dir, tmp_path
) -> None:
    token = _tree_token(50)
    client = _client(runtime, tmp_path)
    assert _post_tree(client, 50, token, _tar_bytes()).status_code == 200
    assert _post_tree(client, 50, token, _tar_bytes()).status_code == 409


def test_upload_tree_wrong_ref_403(runtime: FakeRuntime, staging_dir, tmp_path) -> None:
    r = _post_tree(_client(runtime, tmp_path), 99, _tree_token(50), _tar_bytes())
    assert r.status_code == 403


def test_upload_tree_expired_410(runtime: FakeRuntime, staging_dir, tmp_path) -> None:
    r = _post_tree(
        _client(runtime, tmp_path), 50, _tree_token(50, now=1_000_000), _tar_bytes()
    )
    assert r.status_code == 410


def test_upload_tree_no_staging_503(
    runtime: FakeRuntime, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_WATCH_INBOX", str(tmp_path / "inbox"))
    monkeypatch.delenv("PRECIS_UPLOAD_STAGING", raising=False)
    r = _post_tree(_client(runtime, tmp_path), 50, _tree_token(50), _tar_bytes())
    assert r.status_code == 503
    assert "PRECIS_UPLOAD_STAGING" in r.json()["detail"]


# -- GET /files/blob/{sha} (gr520326 — the page_image crop door) -------
#
# The token signs ``(op='get', sha, ref_id)``; the fetch is ref-bound —
# ``chunk_blob_for_ref`` resolves the sha only under the ref the token
# names, so a well-signed token for *another* ref gets a 404, not the
# bytes.

_IMG_BYTES = b"\x89PNG\r\n\x1a\nfake-crop"
_BLOB_SHA = "ef" * 32


def test_blob_serves_the_seeded_crop(runtime: FakeRuntime, tmp_path) -> None:
    runtime.store.chunk_blob_seed[(_BLOB_SHA, 50)] = (_IMG_BYTES, "image/png")
    token = file_links.mint("get", _BLOB_SHA, 50)
    r = _client(runtime, tmp_path).get(f"/files/blob/{_BLOB_SHA}?t={token}")
    assert r.status_code == 200
    assert r.content == _IMG_BYTES
    assert r.headers["content-type"].startswith("image/png")
    assert "no-store" in r.headers["cache-control"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_blob_refuses_non_raster_mime(runtime: FakeRuntime, tmp_path) -> None:
    """The DB ``mime`` is a claim, not a truth — an ``image/svg+xml`` blob
    served verbatim would be a same-origin script context, so only the
    raster allowlist renders."""
    runtime.store.chunk_blob_seed[(_BLOB_SHA, 50)] = (
        b"<svg onload=alert(1)>",
        "image/svg+xml",
    )
    token = file_links.mint("get", _BLOB_SHA, 50)
    assert (
        _client(runtime, tmp_path).get(f"/files/blob/{_BLOB_SHA}?t={token}").status_code
        == 404
    )


def test_blob_no_token_403(runtime: FakeRuntime, tmp_path) -> None:
    assert _client(runtime, tmp_path).get(f"/files/blob/{_BLOB_SHA}").status_code == 403


def test_blob_token_for_another_sha_403(runtime: FakeRuntime, tmp_path) -> None:
    runtime.store.chunk_blob_seed[(_BLOB_SHA, 50)] = (_IMG_BYTES, "image/png")
    token = file_links.mint("get", _OTHER_SHA, 50)
    assert (
        _client(runtime, tmp_path).get(f"/files/blob/{_BLOB_SHA}?t={token}").status_code
        == 403
    )


def test_blob_is_ref_bound(runtime: FakeRuntime, tmp_path) -> None:
    """A token honestly minted for ref 51 opens no door on ref 50's
    blob — the sha only resolves under the ref that owns the chunk."""
    runtime.store.chunk_blob_seed[(_BLOB_SHA, 50)] = (_IMG_BYTES, "image/png")
    token = file_links.mint("get", _BLOB_SHA, 51)
    assert (
        _client(runtime, tmp_path).get(f"/files/blob/{_BLOB_SHA}?t={token}").status_code
        == 404
    )


# -- GET /files/pdf_page/{sha}/{page} (gr520326 — the page door) -------


def _write_pdf(path, pages: int = 3) -> None:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    try:
        for _ in range(pages):
            doc.new_page()
        path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(path)
    finally:
        doc.close()


@pytest.fixture
def page_pdf(runtime: FakeRuntime, tmp_path):
    """Datasheet 55 (slug ``nrf52``) owning ``_SHA``, 3 pages on disk —
    a datasheet on purpose: the kind-agnostic ref lookup is the point."""
    pdf = tmp_path / "d" / "nrf52.pdf"
    _write_pdf(pdf)
    runtime.store.datasheets.append(
        make_ref(
            id=55,
            kind="datasheet",
            slug="nrf52",
            title="nRF52833 PS",
            pdf_sha256=_SHA,
        )
    )
    runtime.store.storage_paths[_SHA] = str(pdf)
    return pdf


def test_pdf_page_renders_png(runtime: FakeRuntime, page_pdf, tmp_path) -> None:
    token = file_links.mint("get", _SHA, 55)
    r = _client(runtime, tmp_path).get(f"/files/pdf_page/{_SHA}/1?t={token}")
    assert r.status_code == 200
    assert r.content.startswith(b"\x89PNG")
    assert r.headers["content-type"].startswith("image/png")
    assert "no-store" in r.headers["cache-control"]


def test_pdf_page_is_ref_bound(runtime: FakeRuntime, page_pdf, tmp_path) -> None:
    """Same moved-owner rule as ``/files/pdf``: the token verifies but
    the named ref doesn't own ``_SHA`` (datasheet 95's pdf is
    ``'dsheet'``), so it's a 404 — not a render of another ref's page."""
    token = file_links.mint("get", _SHA, 95)
    assert (
        _client(runtime, tmp_path)
        .get(f"/files/pdf_page/{_SHA}/0?t={token}")
        .status_code
        == 404
    )


def test_pdf_page_negative_and_out_of_range_404(
    runtime: FakeRuntime, page_pdf, tmp_path
) -> None:
    """The page index rides the path unsigned — ``doc[-1]`` would
    silently render the last page, so both directions refuse."""
    client = _client(runtime, tmp_path)
    token = file_links.mint("get", _SHA, 55)
    assert client.get(f"/files/pdf_page/{_SHA}/-1?t={token}").status_code == 404
    assert client.get(f"/files/pdf_page/{_SHA}/9?t={token}").status_code == 404


def test_pdf_page_no_token_403(runtime: FakeRuntime, page_pdf, tmp_path) -> None:
    assert (
        _client(runtime, tmp_path).get(f"/files/pdf_page/{_SHA}/0").status_code == 403
    )


def test_pdf_page_cite_key_leg_checks_size(runtime: FakeRuntime, tmp_path) -> None:
    """The page door shares ``/files/pdf``'s stale-bytes guard
    (``_resolve_ref_pdf``): a cite_key-convention hit may be an older
    PDF under the same name, so it renders only when the size matches
    the ``pdfs`` record."""
    pytest.importorskip("fitz")
    runtime.store.papers.append(
        make_ref(id=57, kind="paper", slug="nrf52b", title="B", pdf_sha256=_SHA)
    )
    conv = tmp_path / "n" / "nrf52b.pdf"
    _write_pdf(conv)
    size = conv.stat().st_size
    token = file_links.mint("get", _SHA, 57)
    client = _client(runtime, tmp_path)
    # No recorded size → can't corroborate → 404; stale file → mismatch → 404.
    assert client.get(f"/files/pdf_page/{_SHA}/0?t={token}").status_code == 404
    runtime.store.pdf_sizes[_SHA] = size + 1
    assert client.get(f"/files/pdf_page/{_SHA}/0?t={token}").status_code == 404
    runtime.store.pdf_sizes[_SHA] = size
    assert client.get(f"/files/pdf_page/{_SHA}/0?t={token}").status_code == 200


def test_pdf_page_clamps_oversize_page(runtime: FakeRuntime, tmp_path) -> None:
    """A hostile 14400 pt MediaBox must not render at full dpi — the
    zoom clamps so the longest side stays at 4000 px. The token is
    reusable until expiry, so every hit re-renders: uncapped, one page
    is a multi-GB pixmap (gr520326 review)."""
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "h" / "huge.pdf"
    doc = fitz.open()
    try:
        doc.new_page(width=14400, height=14400)
        pdf.parent.mkdir(parents=True, exist_ok=True)
        doc.save(pdf)
    finally:
        doc.close()
    runtime.store.datasheets.append(
        make_ref(
            id=58,
            kind="datasheet",
            slug="hugepage",
            title="Huge",
            pdf_sha256=_SHA,
        )
    )
    runtime.store.storage_paths[_SHA] = str(pdf)
    token = file_links.mint("get", _SHA, 58)
    r = _client(runtime, tmp_path).get(f"/files/pdf_page/{_SHA}/0?t={token}")
    assert r.status_code == 200
    # PNG IHDR: width/height are the two big-endian u32s at offset 16.
    w, h = struct.unpack(">II", r.content[16:24])
    assert max(w, h) <= 4000


def test_files_prefix_is_segment_scoped() -> None:
    """``/files`` exempts itself and its subtree only — ``/filesx`` must
    never ride the exemption (same segment-match rule the podcast
    prefix already enforces)."""
    from precis_web.auth import _is_self_auth

    assert _is_self_auth("/files")
    assert _is_self_auth("/files/pdf/x")
    assert not _is_self_auth("/filesx")
    assert not _is_self_auth("/files-extra")
