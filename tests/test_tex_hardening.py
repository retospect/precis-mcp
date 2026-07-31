"""LaTeX compile hardening: engine shell-escape is pinned off for the
agent-authored workspaces both compile paths run over (compile_guard +
export.compile). ``\\write18`` in agent tex must not reach a shell."""

from __future__ import annotations

import subprocess
from pathlib import Path

from precis.utils.tex_hardening import hardened_latex_env


def test_hardened_env_disables_shell_escape() -> None:
    env = hardened_latex_env()
    assert env["shell_escape"] == "f"


def test_hardened_env_preserves_base_and_overrides() -> None:
    base = {"PATH": "/usr/bin", "shell_escape": "t"}
    env = hardened_latex_env(base)
    # Inherits the base env (PATH must survive so latexmk/lualatex resolve)...
    assert env["PATH"] == "/usr/bin"
    # ...but forces shell_escape off even when the base had it enabled (a host
    # texmf.cnf / stray env may ship it on).
    assert env["shell_escape"] == "f"
    assert base["shell_escape"] == "t"  # caller's dict untouched


def test_export_compile_passes_hardened_env(monkeypatch, tmp_path: Path) -> None:
    """compile_pdf must invoke latexmk with shell_escape=f in the env."""
    from precis.export import compile as export_compile

    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\begin{document}x\end{document}"
    )
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        (tmp_path / "main.pdf").write_bytes(b"%PDF-1.5\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(export_compile, "have_latexmk", lambda: True)
    monkeypatch.setattr(export_compile, "_latexmk_bin", lambda: "latexmk")
    monkeypatch.setattr(export_compile.subprocess, "run", fake_run)

    res = export_compile.compile_pdf(tmp_path)
    assert res.ok
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["shell_escape"] == "f"
