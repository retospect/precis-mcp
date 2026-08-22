"""LaTeX compile hardening: engine shell-escape is pinned off for the
agent-authored workspaces both compile paths run over (compile_guard +
export.compile). ``\\write18`` in agent tex must not reach a shell."""

from __future__ import annotations

import subprocess
from pathlib import Path

from precis.utils.tex_hardening import (
    hardened_latex_env,
    latexmk_argv,
    trusted_latexmkrc,
)


def test_hardened_env_disables_shell_escape() -> None:
    env = hardened_latex_env()
    assert env["shell_escape"] == "f"


# ── trusted-rc injection (gr178973: the .latexmkrc Perl RCE channel) ──


def test_trusted_latexmkrc_is_the_packaged_template() -> None:
    """The injected rc is the packaged SSOT (with the makeglossaries
    cus-dep the pipeline relies on), NOT the agent's workspace copy."""
    with trusted_latexmkrc() as rc:
        p = Path(rc)
        assert p.is_file()
        txt = p.read_text(encoding="utf-8")
        assert "run_makeglossaries" in txt  # the relied-upon cus-dep
        assert "$pdf_mode = 4" in txt


def test_latexmk_argv_injects_norc_and_trusted_rc() -> None:
    argv = latexmk_argv("latexmk", "-pdf", "main.tex", "/trusted/latexmkrc")
    # -norc disables reading the workspace ./.latexmkrc; -r reads only ours.
    assert "-norc" in argv
    assert argv[argv.index("-r") + 1] == "/trusted/latexmkrc"
    # Both come before the engine flag so a command-line engine still wins.
    assert argv.index("-norc") < argv.index("-pdf")
    assert argv.index("-r") < argv.index("-pdf")
    # The tex entrypoint is the final positional.
    assert argv[-1] == "main.tex"


def test_latexmk_argv_engine_flag_varies() -> None:
    # export uses lualatex, the guard uses pdflatex — same injection, both
    # keep the engine after the rc.
    lua = latexmk_argv("lmk", "-lualatex", "m.tex", "/rc")
    assert lua.index("-r") < lua.index("-lualatex")


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
        r"\documentclass{article}\begin{document}x\end{document}", encoding="utf-8"
    )
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        captured["cmd"] = cmd
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
    # gr178973: the actual compile argv must ignore the workspace .latexmkrc
    # (-norc) and read only the packaged trusted rc (-r <path>).
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert "-norc" in cmd
    rc = cmd[cmd.index("-r") + 1]
    assert Path(rc).is_file() and "run_makeglossaries" in Path(rc).read_text(
        encoding="utf-8"
    )


def test_compile_guard_injects_trusted_rc(monkeypatch, tmp_path: Path) -> None:
    """The STATUS:done compile guard must also ignore the agent's
    workspace .latexmkrc (gr178973) — it runs latexmk with -norc -r."""
    from precis.utils import compile_guard as cg

    class _WS:
        format = "tex"
        entrypoint = "main.tex"

        def absolute_root(self, _root: Path) -> Path:
            return tmp_path

    (tmp_path / "main.tex").write_text(r"\documentclass{article}", encoding="utf-8")
    monkeypatch.setattr(cg, "_load_workspace", lambda store, ref_id: _WS())
    monkeypatch.setattr(cg, "_has_live_child_todos", lambda store, ref_id: False)
    monkeypatch.setattr(cg, "_have_latexmk", lambda: True)

    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cg.subprocess, "run", fake_run)
    # `store` is only ever forwarded to `_load_workspace`/`_has_live_child_todos`,
    # both monkeypatched above, so None never actually hits real store code.
    cg.check_workspace_compiles(None, 1, ["STATUS:done"], precis_root=tmp_path)  # type: ignore[arg-type]
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert "-norc" in cmd
    rc = cmd[cmd.index("-r") + 1]
    assert Path(rc).is_file() and "run_makeglossaries" in Path(rc).read_text(
        encoding="utf-8"
    )
    assert cmd[-1] == "main.tex"
