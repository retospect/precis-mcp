"""``compile_pdf``'s success contract (gr549006).

latexmk exits non-zero when ANY orchestrated step fails — a
makeglossaries/biber custom-dep error, or the rerun cap — even though
the engine already shipped a complete ``main.pdf``. Judging failure by
``returncode != 0`` 500'd real PDFs (prod, draft 546649). The contract
now: ``ok`` tracks a *fresh* ``main.pdf`` (re)written by this run — a
missing or stale file still fails, and a non-zero latexmk verdict is
surfaced in ``log_tail`` instead of hiding behind a clean shipout.

No TeX toolchain needed: ``PRECIS_LATEXMK_BIN`` points at a stub script.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from precis.export import compile as compile_mod
from precis.export.compile import compile_pdf


def _stub(tmp_path: Path, body: str) -> Path:
    """A fake ``latexmk`` executable run in the project dir (cwd)."""
    stub = tmp_path / "latexmk-stub"
    stub.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _project(tmp_path: Path) -> Path:
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}x\\end{document}",
        encoding="utf-8",
    )
    return tmp_path


# The prod failure shape (gr549006): latexmk exit 12 — the makeglossaries
# custom-dep failed — yet the engine's last pass shipped a complete PDF.
_STUB_RC12_WITH_PDF = """\
printf 'fake log\\nOutput written on main.pdf (1 page).\\n' > main.log
printf '%%PDF-1.4 fake\\n%%%%EOF\\n' > main.pdf
echo "Latexmk: Errors, so I did not complete making targets"
echo "  cusdep glo gls main: Command for 'cusdep glo gls main' gave return code 256"
exit 12
"""

_STUB_RC12_NO_PDF = """\
printf 'fatal error fake log\\n' > main.log
echo "Latexmk: Errors, so I did not complete making targets"
exit 12
"""


def test_nonzero_latexmk_exit_with_fresh_pdf_is_served(
    monkeypatch, tmp_path: Path
) -> None:
    """Auxiliary-step failure after a complete shipout: the fresh PDF is
    the deliverable — ok, with the non-zero verdict still on record."""
    stub = _stub(tmp_path, _STUB_RC12_WITH_PDF)
    monkeypatch.setenv("PRECIS_LATEXMK_BIN", str(stub))
    res = compile_pdf(_project(tmp_path))
    assert res.ok
    assert res.pdf is not None and res.pdf.name == "main.pdf"
    assert res.returncode == 12
    # latexmk's own diagnostics name the failing step — the .log tail
    # alone would show a clean 'Output written on main.pdf' and hide it.
    assert "latexmk (exit 12)" in res.log_tail
    assert "cusdep glo gls main" in res.log_tail


def test_nonzero_latexmk_exit_without_pdf_fails(monkeypatch, tmp_path: Path) -> None:
    stub = _stub(tmp_path, _STUB_RC12_NO_PDF)
    monkeypatch.setenv("PRECIS_LATEXMK_BIN", str(stub))
    res = compile_pdf(_project(tmp_path))
    assert not res.ok
    assert res.pdf is None
    assert res.returncode == 12
    assert "latexmk" in res.log_tail


def test_stale_pdf_does_not_flip_failure_to_ok(monkeypatch, tmp_path: Path) -> None:
    """A main.pdf left over from an earlier attempt must NOT rescue a
    run that produced nothing — only this run's (re)write counts."""
    proj = _project(tmp_path)
    stale = proj / "main.pdf"
    stale.write_bytes(b"%PDF-1.4 stale\n%%EOF\n")
    old = 1_000_000_000  # long before this compile started
    os.utime(stale, (old, old))
    stub = _stub(tmp_path, _STUB_RC12_NO_PDF)
    monkeypatch.setenv("PRECIS_LATEXMK_BIN", str(stub))
    res = compile_pdf(proj)
    assert not res.ok
    assert res.pdf is None
    assert res.returncode == 12


def test_zero_byte_pdf_is_not_servable(monkeypatch, tmp_path: Path) -> None:
    stub = _stub(tmp_path, "printf '' > main.pdf\nexit 12\n")
    monkeypatch.setenv("PRECIS_LATEXMK_BIN", str(stub))
    res = compile_pdf(_project(tmp_path))
    assert not res.ok
    assert res.pdf is None


@pytest.mark.parametrize(
    ("environment_timeout", "explicit_timeout", "expected_timeout"),
    [(None, None, 300), ("420", None, 420), ("420", 60, 60)],
)
def test_compile_timeout_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    environment_timeout: str | None,
    explicit_timeout: int | None,
    expected_timeout: int,
) -> None:
    """gr684953: large drafts get five minutes; overrides still win."""
    monkeypatch.delenv("PRECIS_LATEXMK_TIMEOUT_S", raising=False)
    if environment_timeout is not None:
        monkeypatch.setenv("PRECIS_LATEXMK_TIMEOUT_S", environment_timeout)
    monkeypatch.setattr(compile_mod, "have_latexmk", lambda: True)

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == expected_timeout
        raise subprocess.TimeoutExpired(cmd="latexmk", timeout=expected_timeout)

    monkeypatch.setattr(compile_mod.subprocess, "run", run)
    result = compile_pdf(_project(tmp_path), timeout_s=explicit_timeout)
    assert not result.ok
    assert result.pdf is None
    assert result.returncode == -1
    assert result.log_tail == f"latexmk timed out after {expected_timeout}s"
