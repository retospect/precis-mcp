"""Send-to-reMarkable uploader (``precis.export.remarkable``).

Unit tests drive a ``#!/bin/sh`` stub through ``PRECIS_RMAPI_BIN`` +
``shutil.which`` + ``subprocess.run`` (the same POSIX stub-binary pattern
as the latexmk compile tests), so no real ``rmapi`` / device is needed.
The credential resolves from the ``REMARKABLE_TOKEN`` env var (``get_secret``
checks the environment first).
"""

from __future__ import annotations

import stat
import sys
import textwrap
from pathlib import Path

import pytest

from precis.export import remarkable as rm

_needs_posix_stub = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX execute-shebang support required for the rmapi stub-binary pattern",
)


def _stub_rmapi(tmp_path: Path, *, succeed: bool = True) -> Path:
    """A stub rmapi that echoes its args (so tests can assert the call) and
    exits 0/1."""
    script = tmp_path / "rmapi"
    tail = "exit 0\n" if succeed else "echo 'upload failed' >&2\nexit 1\n"
    script.write_text('#!/bin/sh\necho "rmapi $@"\n' + tail)
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
    script.write_text(src)
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

    argv = log.read_text()
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
