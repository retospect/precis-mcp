"""Send-to-reMarkable uploader (``precis.export.remarkable``).

Unit tests drive a ``#!/bin/sh`` stub through ``PRECIS_RMAPI_BIN`` +
``shutil.which`` + ``subprocess.run`` (the same POSIX stub-binary pattern
as the latexmk compile tests), so no real ``rmapi`` / device is needed.
The credential resolves from the ``REMARKABLE_TOKEN`` env var (``get_secret``
checks the environment first).
"""

from __future__ import annotations

import http.server
import logging
import stat
import sys
import textwrap
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from precis.export import remarkable as rm

if TYPE_CHECKING:
    from precis.store import Store

_needs_posix_stub = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX execute-shebang support required for the rmapi stub-binary pattern",
)


def _stub_rmapi(tmp_path: Path, *, succeed: bool = True) -> Path:
    """A stub rmapi that echoes its args (so tests can assert the call) and
    exits 0/1."""
    script = tmp_path / "rmapi"
    tail = "exit 0\n" if succeed else "echo 'upload failed' >&2\nexit 1\n"
    script.write_text('#!/bin/sh\necho "rmapi $@"\n' + tail, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _pdf(tmp_path: Path) -> Path:
    p = tmp_path / "main.pdf"
    p.write_bytes(b"%PDF-1.4\n%stub\n")
    return p


def test_remarkable_configured_reads_credential(monkeypatch) -> None:
    monkeypatch.delenv("REMARKABLE_RMAPI_CONFIG", raising=False)
    monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)
    assert rm.remarkable_configured(store=None) is False
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    assert rm.remarkable_configured(store=None) is True


# ── per-user pairing (vault-backed; env can't hold a colon name) ────


class _FakeVaultStore:
    """A minimal ``store`` stand-in so ``secrets.get/set/delete_secret``
    (which take ``store=``) have something to thread — the real reveal
    path is monkeypatched below, this object's identity is never used."""


def _fake_store() -> Store:
    return cast("Store", _FakeVaultStore())


@pytest.fixture
def vault_box(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """In-memory stand-in for :mod:`precis.secrets`, patched at the module
    the helpers import from — mirrors ``tests/precis_web/test_account.py``.
    A per-user secret name carries a colon, which no env var can hold, so
    this is the only way to exercise the per-user path in tests."""
    from precis import secrets as vault_mod

    box: dict[str, str] = {}
    monkeypatch.setattr(
        vault_mod,
        "get_secret",
        lambda n, *, store=None, default=None: box.get(n, default),
    )
    monkeypatch.setattr(
        vault_mod, "set_secret", lambda n, v, *, store: box.__setitem__(n, v)
    )
    monkeypatch.setattr(
        vault_mod, "delete_secret", lambda n, *, store: box.pop(n, None)
    )
    monkeypatch.setattr(
        vault_mod, "is_available", lambda n, *, store=None: box.get(n) is not None
    )
    return box


def test_user_config_secret_is_colon_scoped() -> None:
    assert rm.user_config_secret("reto") == "REMARKABLE_RMAPI_CONFIG:reto"


def test_per_user_config_beats_global(vault_box: dict[str, str]) -> None:
    store = _fake_store()
    vault_box["REMARKABLE_RMAPI_CONFIG"] = "devicetoken: global-token\n"
    vault_box["REMARKABLE_RMAPI_CONFIG:reto"] = "devicetoken: retos-own-token\n"
    assert rm._config_body(store, "reto") == "devicetoken: retos-own-token\n"
    # A different login with no paired device of its own falls through to
    # the deployment-wide config.
    assert rm._config_body(store, "someone-else") == "devicetoken: global-token\n"


def test_login_none_keeps_global_behaviour(vault_box: dict[str, str]) -> None:
    store = _fake_store()
    vault_box["REMARKABLE_TOKEN"] = "bare-token"
    assert rm._config_body(store, None) == "devicetoken: bare-token\n"
    assert rm.remarkable_configured(store) is True
    assert rm.remarkable_configured(store, login=None) is True


def test_remarkable_configured_per_user_and_global(vault_box: dict[str, str]) -> None:
    store = _fake_store()
    assert rm.remarkable_configured(store, login="reto") is False
    assert rm.user_remarkable_configured(store, "reto") is False
    vault_box["REMARKABLE_RMAPI_CONFIG:reto"] = "devicetoken: t\n"
    assert rm.remarkable_configured(store, login="reto") is True
    assert rm.user_remarkable_configured(store, "reto") is True
    # No global fallback bleeds into the per-user-only check.
    assert rm.user_remarkable_configured(store, "someone-else") is False
    assert rm.remarkable_configured(store, login="someone-else") is False


def test_set_and_clear_user_config(vault_box: dict[str, str]) -> None:
    store = _fake_store()
    rm.set_user_config(store, "reto", "bare-pasted-secret")
    assert (
        vault_box["REMARKABLE_RMAPI_CONFIG:reto"] == "devicetoken: bare-pasted-secret\n"
    )
    assert rm.clear_user_config(store, "reto") is True
    assert "REMARKABLE_RMAPI_CONFIG:reto" not in vault_box


def test_set_user_config_keeps_a_full_config_body_verbatim(
    vault_box: dict[str, str],
) -> None:
    store = _fake_store()
    body = "devicetoken: abc\nusertoken: def\n"
    rm.set_user_config(store, "reto", body)
    assert vault_box["REMARKABLE_RMAPI_CONFIG:reto"] == body


def test_clear_user_config_reports_vault_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _boom(name, *, store):
        raise RuntimeError("vault down")

    monkeypatch.setattr(rm.secrets, "delete_secret", _boom)
    with caplog.at_level(logging.WARNING, logger=rm.log.name):
        assert rm.clear_user_config(_fake_store(), "reto") is False
    # The warning must carry the traceback (exc_info=True) — this is the
    # ONLY signal an operator gets that the vault entry is still live
    # despite the "unpaired" outcome, so it must not be silently dropped.
    assert caplog.records
    assert caplog.records[-1].exc_info  # truthy tuple, not None/False


# ── send_pdf threading login ─────────────────────────────────────────


@_needs_posix_stub
def test_send_pdf_prefers_the_users_own_device(
    tmp_path, monkeypatch, vault_box: dict[str, str]
) -> None:
    monkeypatch.setenv("PRECIS_RMAPI_BIN", str(_stub_rmapi(tmp_path)))
    monkeypatch.setenv("REMARKABLE_TOKEN", "global-token")
    vault_box["REMARKABLE_RMAPI_CONFIG:reto"] = "devicetoken: retos-token\n"
    res = rm.send_pdf(_pdf(tmp_path), store=_fake_store(), login="reto")
    assert res.ok


# ── device pairing exchange ──────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


def test_register_device_rejects_a_malformed_code() -> None:
    with pytest.raises(rm.PairingError, match="8-character"):
        rm.register_device("short")


def test_register_device_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx as _httpx

    seen: dict[str, Any] = {}

    def _post(url, *, json, headers, timeout):
        seen["url"] = url
        seen["json"] = json
        seen["headers"] = headers
        return _FakeResponse(200, "the-device-token")

    monkeypatch.setattr(_httpx, "post", _post)
    monkeypatch.setenv("PRECIS_RMAPI_REGISTER_URL", "https://example.test/register")

    body = rm.register_device("AbC12dEf")
    assert body == "devicetoken: the-device-token\n"
    assert seen["url"] == "https://example.test/register"
    assert seen["json"]["code"] == "abc12def"  # lowercased
    # No trailing space — h11 rejects that outright (see the regression test below).
    assert seen["headers"]["Authorization"] == "Bearer"


def test_register_device_rejected_code_raises_pairing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx as _httpx

    monkeypatch.setattr(
        _httpx, "post", lambda *a, **kw: _FakeResponse(400, "invalid code")
    )
    with pytest.raises(rm.PairingError, match="single-use"):
        rm.register_device("aaaaaaaa")


def test_register_device_network_error_raises_pairing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx as _httpx

    def _boom(*a, **kw):
        raise _httpx.ConnectError("nope")

    monkeypatch.setattr(_httpx, "post", _boom)
    with pytest.raises(rm.PairingError, match="could not reach"):
        rm.register_device("aaaaaaaa")


def test_register_device_over_a_real_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pairing POST must survive httpx's *real* transport.

    Regression: the empty bearer header was sent as ``"Bearer "`` (what
    rmapi's Go client sends). h11 refuses a trailing-whitespace header
    value — ``LocalProtocolError: Illegal header value b'Bearer '`` — which
    httpx raises as an ``HTTPError``, so every pairing attempt surfaced as
    "could not reach the reMarkable pairing service". The other tests here
    stub out ``httpx.post`` and so never exercise h11; this one drives a
    loopback server through the whole stack.
    """
    seen: dict[str, Any] = {}

    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            seen["auth"] = self.headers.get("Authorization")
            seen["body"] = body
            payload = b"the-device-token"
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_a: Any) -> None:  # silence
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv(
            "PRECIS_RMAPI_REGISTER_URL",
            f"http://127.0.0.1:{srv.server_address[1]}/register",
        )
        assert rm.register_device("aaaaaaaa") == "devicetoken: the-device-token\n"
    finally:
        srv.shutdown()
    assert seen["auth"] == "Bearer"


def test_send_pdf_skips_without_binary(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    monkeypatch.setenv("PRECIS_RMAPI_BIN", str(tmp_path / "does-not-exist"))
    res = rm.send_pdf(_pdf(tmp_path), store=None)
    assert res.skipped and not res.ok and "not installed" in res.error


@_needs_posix_stub
def test_send_pdf_skips_without_credential(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("REMARKABLE_RMAPI_CONFIG", raising=False)
    monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)
    monkeypatch.setenv("PRECIS_RMAPI_BIN", str(_stub_rmapi(tmp_path)))
    res = rm.send_pdf(_pdf(tmp_path), store=None)
    assert res.skipped and not res.ok and "credential" in res.error


@_needs_posix_stub
def test_send_pdf_uploads_via_stub(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    monkeypatch.setenv("PRECIS_RMAPI_BIN", str(_stub_rmapi(tmp_path)))
    res = rm.send_pdf(
        _pdf(tmp_path), folder="/Precis", display_name="My Draft!", store=None
    )
    assert res.ok and res.returncode == 0
    assert res.name == "My Draft"  # sanitised (‘!’ dropped)
    assert "put" in res.output and "My Draft.pdf" in res.output  # staged name
    assert "/Precis" in res.output


def _stub_rmapi_logging(tmp_path: Path, log_path: Path) -> Path:
    """A stub rmapi like :func:`_stub_rmapi` that additionally appends every
    invocation's argv to ``log_path`` — needed because ``send_pdf`` only
    keeps the *last* subprocess's stdout/stderr (the ``put``), discarding the
    ``mkdir`` calls' own output."""
    script = tmp_path / "rmapi"
    script.write_text(
        '#!/bin/sh\necho "rmapi $@" >> "'
        + str(log_path)
        + '"\necho "rmapi $@"\nexit 0\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


@_needs_posix_stub
def test_send_pdf_creates_nested_folder_segment_by_segment(
    tmp_path, monkeypatch
) -> None:
    """rmapi's mkdir is not recursive — a nested destination like
    "/Precis/173020" must get one mkdir per ancestor, in order, before the
    put."""
    log_path = tmp_path / "argv.log"
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    monkeypatch.setenv("PRECIS_RMAPI_BIN", str(_stub_rmapi_logging(tmp_path, log_path)))
    res = rm.send_pdf(
        _pdf(tmp_path), folder="/Precis/173020", display_name="Source", store=None
    )
    assert res.ok
    lines = log_path.read_text(encoding="utf-8").splitlines()
    mkdir_lines = [ln for ln in lines if ln.startswith("rmapi mkdir")]
    assert "rmapi mkdir /Precis" in mkdir_lines
    assert "rmapi mkdir /Precis/173020" in mkdir_lines
    # "/Precis" is created before "/Precis/173020", and both before the put.
    assert lines.index("rmapi mkdir /Precis") < lines.index(
        "rmapi mkdir /Precis/173020"
    )
    assert any(ln.startswith("rmapi put") for ln in lines)
    assert lines.index("rmapi mkdir /Precis/173020") < next(
        i for i, ln in enumerate(lines) if ln.startswith("rmapi put")
    )


@_needs_posix_stub
def test_send_pdf_rejects_unsafe_folder(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    monkeypatch.setenv("PRECIS_RMAPI_BIN", str(_stub_rmapi(tmp_path)))
    res = rm.send_pdf(_pdf(tmp_path), folder="/a; rm -rf /", store=None)
    assert not res.ok and not res.skipped and "unsafe" in res.error


@_needs_posix_stub
def test_send_pdf_reports_upload_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    monkeypatch.setenv("PRECIS_RMAPI_BIN", str(_stub_rmapi(tmp_path, succeed=False)))
    res = rm.send_pdf(_pdf(tmp_path), store=None)
    assert not res.ok and res.returncode == 1 and "failed" in res.error


# ── Container path (PRECIS_REMARKABLE_IMAGE → docker/remarkable) ────


def _stub_container(
    tmp_path: Path, *, ok: bool = True, write_result: bool = True
) -> Path:
    """A stub container CLI: logs its argv to ``$RM_STUB_LOG`` and writes a
    ``result.json`` into the bind-mounted out dir (``…:/work/out``), so a
    ``send_via_container`` run completes with no real docker/rmapi."""
    script = tmp_path / "ctr"
    src = textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        import json, os, sys
        log = os.environ.get("RM_STUB_LOG")
        if log:
            with open(log, "a") as fh:
                fh.write(" ".join(sys.argv[1:]) + "\\n")
        out = None
        for a in sys.argv[1:]:
            if a.endswith(":/work/out"):
                out = a[: -len(":/work/out")]
        if out and {write_result!r}:
            with open(os.path.join(out, "result.json"), "w") as fh:
                json.dump(
                    {{
                        "ok": {ok!r},
                        "returncode": {0 if ok else 1},
                        "output": "rmapi put stub",
                        "name": "My Draft",
                        "folder": "/Precis",
                        "error": "" if {ok!r} else "rmapi upload failed",
                    }},
                    fh,
                )
        sys.exit(0)
        """
    )
    script.write_text(src, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_build_container_argv_invariants(tmp_path) -> None:
    ind, outd = tmp_path / "in", tmp_path / "out"
    argv = rm.build_container_argv(
        "docker", image="precis-remarkable:t", in_dir=ind, out_dir=outd
    )
    assert argv[:3] == ["docker", "run", "--rm"]
    assert f"{ind}:/work/in:ro" in argv  # in mount is read-only
    assert f"{outd}:/work/out" in argv  # out mount is writable
    # credential passed BY KEY only — the flag is present, no "=value" form.
    i = argv.index("--env")
    assert argv[i + 1] == "REMARKABLE_RMAPI_CONFIG"
    assert not any(a.startswith("REMARKABLE_RMAPI_CONFIG=") for a in argv)
    assert argv[-1] == "precis-remarkable:t"  # image is last (default CMD)


@_needs_posix_stub
def test_send_pdf_uploads_via_container(tmp_path, monkeypatch) -> None:
    log = tmp_path / "argv.log"
    monkeypatch.setenv("RM_STUB_LOG", str(log))
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token-SECRET123")
    monkeypatch.setenv("PRECIS_REMARKABLE_IMAGE", "precis-remarkable:t")
    monkeypatch.setenv("PRECIS_CONTAINER_BIN", str(_stub_container(tmp_path)))
    # No rmapi on the host at all — the container owns it.
    monkeypatch.setenv("PRECIS_RMAPI_BIN", str(tmp_path / "no-rmapi"))

    res = rm.send_pdf(
        _pdf(tmp_path), folder="/Precis", display_name="My Draft!", store=None
    )
    assert res.ok and res.returncode == 0
    assert res.name == "My Draft" and res.folder == "/Precis"

    argv = log.read_text(encoding="utf-8")
    assert "--env REMARKABLE_RMAPI_CONFIG" in argv  # secret passed by key
    assert "SECRET123" not in argv  # …never the value on the command line
    assert "precis-remarkable:t" in argv  # the configured image


@_needs_posix_stub
def test_send_pdf_container_reports_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    monkeypatch.setenv("PRECIS_REMARKABLE_IMAGE", "precis-remarkable:t")
    monkeypatch.setenv("PRECIS_CONTAINER_BIN", str(_stub_container(tmp_path, ok=False)))
    res = rm.send_pdf(_pdf(tmp_path), store=None)
    assert not res.ok and res.returncode == 1 and "failed" in res.error


@_needs_posix_stub
def test_send_pdf_container_no_result_is_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REMARKABLE_TOKEN", "dev-token")
    monkeypatch.setenv("PRECIS_REMARKABLE_IMAGE", "precis-remarkable:t")
    monkeypatch.setenv(
        "PRECIS_CONTAINER_BIN", str(_stub_container(tmp_path, write_result=False))
    )
    res = rm.send_pdf(_pdf(tmp_path), store=None)
    assert not res.ok and "result.json" in res.error


@_needs_posix_stub
def test_send_pdf_container_skips_without_credential(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("REMARKABLE_RMAPI_CONFIG", raising=False)
    monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)
    monkeypatch.setenv("PRECIS_REMARKABLE_IMAGE", "precis-remarkable:t")
    monkeypatch.setenv("PRECIS_CONTAINER_BIN", str(_stub_container(tmp_path)))
    res = rm.send_pdf(_pdf(tmp_path), store=None)
    # Credential gate fires before the container is ever run.
    assert res.skipped and not res.ok and "credential" in res.error
