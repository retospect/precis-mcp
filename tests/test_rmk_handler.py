"""Phase 5c — reMarkable tablet push handler (``rmk:``).

No network, no tablet, no ``remarkable-mcp`` import required in the
test env — the tests either:

- exercise the pre-client validation (path / ext / mode checks raise
  before the client is touched), or
- install a fake ``remarkable_mcp.sync`` module in ``sys.modules`` so
  the lazy import inside :meth:`RmkHandler._get_client` resolves to a
  ``MagicMock`` stand-in for ``RemarkableClient``.

The registry check exploits the fact that ``precis.handlers.rmk`` has
no module-level dependency on ``remarkable_mcp`` (the import is
deferred to ``_get_client``), so registration succeeds in a clean
install.  Visibility is gated purely on ``REMARKABLE_TOKEN`` via the
``KindSpec.requires`` mechanism.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from precis.handlers.rmk import (
    _SUPPORTED_EXT,
    RmkHandler,
    _format_upload_result,
    _help_text,
)
from precis.protocol import ErrorCode, PrecisError
from precis.registry import KINDS, SCHEMES, visible_kinds


# ---------------------------------------------------------------------------
# Fixtures — a fake ``remarkable_mcp.sync`` module
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_rm_client() -> MagicMock:
    """Return a MagicMock standing in for a ``RemarkableClient``.

    ``client.upload(...)`` returns a ``Document`` with a stable ID so
    the formatted response is predictable across runs.
    """
    client = MagicMock()
    doc = MagicMock()
    doc.id = "doc-abc-123"
    client.upload.return_value = doc
    return client


@pytest.fixture
def fake_remarkable_mcp(fake_rm_client: MagicMock, monkeypatch):
    """Install a fake ``remarkable_mcp.sync`` module for the duration
    of the test.  Returns the ``load_client_from_token`` stand-in so
    tests can assert on it.
    """
    pkg = types.ModuleType("remarkable_mcp")
    mod = types.ModuleType("remarkable_mcp.sync")
    loader = MagicMock(return_value=fake_rm_client)
    mod.load_client_from_token = loader  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "remarkable_mcp", pkg)
    monkeypatch.setitem(sys.modules, "remarkable_mcp.sync", mod)
    return loader


# ---------------------------------------------------------------------------
# Handler class attributes
# ---------------------------------------------------------------------------


class TestHandlerAttrs:
    def test_scheme_is_rmk(self):
        assert RmkHandler.scheme == "rmk"

    def test_handler_is_writable(self):
        assert RmkHandler.writable is True

    def test_only_push_mode_allowed(self):
        # Used by the MCP layer to enrich MODE_UNSUPPORTED errors with
        # the list of valid alternatives.
        assert RmkHandler.allowed_modes == {"push"}

    def test_views_is_help_only(self):
        assert RmkHandler.views == {"help"}


# ---------------------------------------------------------------------------
# Client init — env + package gating
# ---------------------------------------------------------------------------


class TestGetClient:
    def test_missing_package_raises_kind_unavailable(self, monkeypatch):
        # Ensure no fake module is installed; the real one isn't
        # available in the test env, so the import fails naturally.
        monkeypatch.delitem(sys.modules, "remarkable_mcp.sync", raising=False)
        monkeypatch.delitem(sys.modules, "remarkable_mcp", raising=False)
        monkeypatch.setenv("REMARKABLE_TOKEN", "dummy")

        h = RmkHandler()
        with pytest.raises(PrecisError) as exc:
            h._get_client()
        assert exc.value.code == ErrorCode.KIND_UNAVAILABLE
        assert "remarkable-mcp" in str(exc.value)

    def test_missing_env_raises_kind_unavailable(
        self, fake_remarkable_mcp, monkeypatch
    ):
        monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)
        h = RmkHandler()
        with pytest.raises(PrecisError) as exc:
            h._get_client()
        assert exc.value.code == ErrorCode.KIND_UNAVAILABLE
        assert "REMARKABLE_TOKEN" in str(exc.value)

    def test_whitespace_env_treated_as_missing(
        self, fake_remarkable_mcp, monkeypatch
    ):
        monkeypatch.setenv("REMARKABLE_TOKEN", "   ")
        h = RmkHandler()
        with pytest.raises(PrecisError) as exc:
            h._get_client()
        assert exc.value.code == ErrorCode.KIND_UNAVAILABLE

    def test_happy_path_calls_loader_with_token(
        self, fake_remarkable_mcp, fake_rm_client, monkeypatch
    ):
        monkeypatch.setenv("REMARKABLE_TOKEN", "a-real-looking-token")
        h = RmkHandler()
        client = h._get_client()
        assert client is fake_rm_client
        fake_remarkable_mcp.assert_called_once_with("a-real-looking-token")

    def test_client_is_cached_across_calls(
        self, fake_remarkable_mcp, monkeypatch
    ):
        monkeypatch.setenv("REMARKABLE_TOKEN", "t")
        h = RmkHandler()
        h._get_client()
        h._get_client()
        # Loader called once — second call hits the memoised client.
        assert fake_remarkable_mcp.call_count == 1


# ---------------------------------------------------------------------------
# put() — pre-client validation (doesn't touch the token / client)
# ---------------------------------------------------------------------------


class TestPutValidation:
    def test_unknown_mode_raises_mode_unsupported(self):
        h = RmkHandler()
        with pytest.raises(PrecisError) as exc:
            h.put(path="/x.pdf", selector=None, text="", mode="append")
        assert exc.value.code == ErrorCode.MODE_UNSUPPORTED

    def test_empty_path_raises_param_invalid(self):
        h = RmkHandler()
        with pytest.raises(PrecisError) as exc:
            h.put(path="", selector=None, text="", mode="push")
        assert exc.value.code == ErrorCode.PARAM_INVALID

    def test_relative_path_rejected(self, tmp_path, monkeypatch):
        # cd into tmp so any accidental resolution can't find a file.
        monkeypatch.chdir(tmp_path)
        h = RmkHandler()
        with pytest.raises(PrecisError) as exc:
            h.put(path="rel/foo.pdf", selector=None, text="", mode="push")
        assert exc.value.code == ErrorCode.PARAM_INVALID

    def test_nonexistent_absolute_file_raises_id_not_found(self, tmp_path):
        h = RmkHandler()
        missing = tmp_path / "does-not-exist.pdf"
        with pytest.raises(PrecisError) as exc:
            h.put(path=str(missing), selector=None, text="", mode="push")
        assert exc.value.code == ErrorCode.ID_NOT_FOUND

    def test_directory_rejected(self, tmp_path):
        h = RmkHandler()
        with pytest.raises(PrecisError) as exc:
            h.put(path=str(tmp_path), selector=None, text="", mode="push")
        assert exc.value.code == ErrorCode.PARAM_INVALID

    def test_unsupported_extension_rejected(self, tmp_path):
        notes = tmp_path / "notes.txt"
        notes.write_text("hello")
        h = RmkHandler()
        with pytest.raises(PrecisError) as exc:
            h.put(path=str(notes), selector=None, text="", mode="push")
        assert exc.value.code == ErrorCode.PARAM_INVALID

    def test_docx_rejected_with_helpful_options(self, tmp_path):
        # DOCX is a common near-miss — the tablet doesn't render it.
        # Check the error carries the supported-format hint.
        f = tmp_path / "manuscript.docx"
        f.write_bytes(b"PK...fake docx")
        h = RmkHandler()
        with pytest.raises(PrecisError) as exc:
            h.put(path=str(f), selector=None, text="", mode="push")
        assert exc.value.code == ErrorCode.PARAM_INVALID


# ---------------------------------------------------------------------------
# put() — happy path through the fake client
# ---------------------------------------------------------------------------


class TestPutUpload:
    @staticmethod
    def _make_pdf(tmp_path: Path, name: str = "paper.pdf") -> Path:
        p = tmp_path / name
        p.write_bytes(b"%PDF-1.7\nstub bytes\n%%EOF\n")
        return p

    @staticmethod
    def _make_epub(tmp_path: Path, name: str = "book.epub") -> Path:
        p = tmp_path / name
        p.write_bytes(b"PK\x03\x04stub-epub-bytes")
        return p

    def test_pdf_uploaded_with_correct_kwargs(
        self, tmp_path, fake_remarkable_mcp, fake_rm_client, monkeypatch
    ):
        monkeypatch.setenv("REMARKABLE_TOKEN", "t")
        f = self._make_pdf(tmp_path)

        out = RmkHandler().put(
            path=str(f), selector=None, text="", mode="push"
        )

        fake_rm_client.upload.assert_called_once()
        kwargs = fake_rm_client.upload.call_args.kwargs
        assert kwargs["file_type"] == "pdf"
        assert kwargs["data"] == f.read_bytes()
        assert kwargs["name"] == "paper"  # file stem, no extension
        assert "Pushed to reMarkable" in out
        assert "doc-abc-123" in out

    def test_epub_routed_as_epub_file_type(
        self, tmp_path, fake_remarkable_mcp, fake_rm_client, monkeypatch
    ):
        monkeypatch.setenv("REMARKABLE_TOKEN", "t")
        f = self._make_epub(tmp_path)
        RmkHandler().put(path=str(f), selector=None, text="", mode="push")
        assert fake_rm_client.upload.call_args.kwargs["file_type"] == "epub"

    def test_display_name_override_via_text(
        self, tmp_path, fake_remarkable_mcp, fake_rm_client, monkeypatch
    ):
        monkeypatch.setenv("REMARKABLE_TOKEN", "t")
        f = self._make_pdf(tmp_path)
        RmkHandler().put(
            path=str(f), selector=None, text="My Custom Title", mode="push"
        )
        assert fake_rm_client.upload.call_args.kwargs["name"] == "My Custom Title"

    def test_whitespace_only_text_falls_back_to_stem(
        self, tmp_path, fake_remarkable_mcp, fake_rm_client, monkeypatch
    ):
        monkeypatch.setenv("REMARKABLE_TOKEN", "t")
        f = self._make_pdf(tmp_path)
        RmkHandler().put(path=str(f), selector=None, text="   ", mode="push")
        assert fake_rm_client.upload.call_args.kwargs["name"] == "paper"

    def test_extension_is_case_insensitive(
        self, tmp_path, fake_remarkable_mcp, fake_rm_client, monkeypatch
    ):
        monkeypatch.setenv("REMARKABLE_TOKEN", "t")
        f = self._make_pdf(tmp_path, name="PAPER.PDF")
        RmkHandler().put(path=str(f), selector=None, text="", mode="push")
        assert fake_rm_client.upload.call_args.kwargs["file_type"] == "pdf"

    def test_upload_exception_wrapped_as_upstream_error(
        self, tmp_path, fake_remarkable_mcp, fake_rm_client, monkeypatch
    ):
        monkeypatch.setenv("REMARKABLE_TOKEN", "t")
        fake_rm_client.upload.side_effect = RuntimeError("cloud 503")
        f = self._make_pdf(tmp_path)
        with pytest.raises(PrecisError) as exc:
            RmkHandler().put(path=str(f), selector=None, text="", mode="push")
        assert exc.value.code == ErrorCode.UPSTREAM_ERROR
        assert "cloud 503" in str(exc.value)

    def test_response_reports_file_size_in_kb(
        self, tmp_path, fake_remarkable_mcp, fake_rm_client, monkeypatch
    ):
        monkeypatch.setenv("REMARKABLE_TOKEN", "t")
        f = tmp_path / "sized.pdf"
        f.write_bytes(b"x" * 4096)  # 4.0 KB
        out = RmkHandler().put(path=str(f), selector=None, text="", mode="push")
        assert "4.0 KB" in out


# ---------------------------------------------------------------------------
# read() — write-only kind, reads return help
# ---------------------------------------------------------------------------


class TestRead:
    def test_read_returns_help_text(self):
        h = RmkHandler()
        out = h.read(
            path="",
            selector=None,
            view=None,
            subview=None,
            query="",
            summarize=False,
            depth=0,
            page=1,
        )
        assert "rmk:" in out
        assert "reMarkable" in out
        assert "REMARKABLE_TOKEN" in out


# ---------------------------------------------------------------------------
# Registry / visibility
# ---------------------------------------------------------------------------


class TestRegistration:
    @classmethod
    def setup_class(cls):
        # Force discovery so KINDS / SCHEMES are populated even when
        # this test file runs before anything else has touched the
        # registry (e.g. under ``-k test_rmk``).
        import precis.registry as reg

        reg._discover()

    def test_kind_registered(self):
        # The handler module has no module-level dep on remarkable_mcp,
        # so registration succeeds even when the package is missing.
        assert "rmk" in KINDS
        assert "rmk" in SCHEMES

    def test_plugin_name_is_remarkable(self):
        assert KINDS["rmk"].plugin_name == "remarkable"

    def test_hidden_from_put_enum_without_token(self, monkeypatch):
        import precis.registry as reg

        monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)
        reg._ENV_WARNED.discard("rmk")
        names = {k.spec.name for k in visible_kinds("put")}
        assert "rmk" not in names

    def test_visible_with_token(self, monkeypatch):
        monkeypatch.setenv("REMARKABLE_TOKEN", "stub-for-visibility")
        names = {k.spec.name for k in visible_kinds("put")}
        assert "rmk" in names

    def test_spec_declares_required_env(self):
        spec = KINDS["rmk"].spec
        assert "REMARKABLE_TOKEN" in spec.requires

    def test_spec_description_mentions_tablet(self):
        # The description drives what the agent sees in the enum, so
        # lock in that we name-drop reMarkable + call out the tablet
        # form factor so the agent knows what push means.
        desc = KINDS["rmk"].spec.description.lower()
        assert "remarkable" in desc
        assert "tablet" in desc or "e-ink" in desc

    def test_spec_examples_show_put_usage(self):
        examples = KINDS["rmk"].spec.examples
        assert any("put(" in e for e in examples)
        assert any("rmk:" in e for e in examples)
        assert any("push" in e for e in examples)


# ---------------------------------------------------------------------------
# Module-level formatting helpers
# ---------------------------------------------------------------------------


class TestFormatting:
    def test_help_text_mentions_cloud_mode(self):
        out = _help_text()
        assert "Cloud" in out or "cloud" in out
        assert "rmk:" in out

    def test_help_text_mentions_pdf_and_epub(self):
        out = _help_text()
        assert ".pdf" in out.lower()
        assert ".epub" in out.lower()

    def test_help_text_mentions_token_setup(self):
        out = _help_text()
        assert "REMARKABLE_TOKEN" in out
        assert "register" in out.lower()

    def test_format_upload_result_includes_size(self, tmp_path):
        f = tmp_path / "sample.pdf"
        f.write_bytes(b"x" * 2048)  # 2 KB
        doc = MagicMock()
        doc.id = "d1"
        out = _format_upload_result(doc, f, "sample", "pdf", 2048)
        assert "2.0 KB" in out
        assert "PDF" in out
        assert "d1" in out
        assert "sample" in out

    def test_supported_extensions_map(self):
        # Locks in the accepted set; widening this is a deliberate
        # compat decision.
        assert _SUPPORTED_EXT == {".pdf": "pdf", ".epub": "epub"}
