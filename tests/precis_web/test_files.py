"""``GET /files/pdf/{sha}?t=<token>`` — the precis_web half of the
signed-PDF-link slice (gr517423). The FakeStore-backed app answers
``get_ref`` / ``pdf_storage_path`` / ``ref_cite_keys`` from the seeded
fixtures; the signing key comes from the env layer so no vault is
needed. Verify-before-anything is the security contract: a caller with
no valid token can only ever see 403/410."""

from __future__ import annotations

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


def test_files_prefix_is_segment_scoped() -> None:
    """``/files`` exempts itself and its subtree only — ``/filesx`` must
    never ride the exemption (same segment-match rule the podcast
    prefix already enforces)."""
    from precis_web.auth import _is_self_auth

    assert _is_self_auth("/files")
    assert _is_self_auth("/files/pdf/x")
    assert not _is_self_auth("/filesx")
    assert not _is_self_auth("/files-extra")
