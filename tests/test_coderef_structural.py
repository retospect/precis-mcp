"""Unit tests for scripts/coderef's structural retrieval verbs — deps, callers
(alias refs), imports, importers. The script has no ``.py`` extension (it's an
executable, not a package module), so it's loaded by path — same convention as
``tests/test_worktree_path_guard.py``.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_CODEREF = Path(__file__).resolve().parents[1] / "scripts" / "coderef"
# extension-less executable — spec_from_file_location can't infer a loader from
# the suffix, so hand it one explicitly. Load from a byte-identical `.py`-
# suffixed COPY, not the real dotless path: pytest-testmon fingerprints every
# executed file by extension (`filename.rsplit(".", 1)[1]`) and IndexErrors on
# one that has none, which would INTERNALERROR any `--impacted` run that
# touches this test. The module name stays "coderef" either way.
_coderef_copy = Path(tempfile.mkdtemp(prefix="coderef_py_")) / "coderef.py"
shutil.copyfile(_CODEREF, _coderef_copy)
_loader = importlib.machinery.SourceFileLoader("coderef", str(_coderef_copy))
_spec = importlib.util.spec_from_loader("coderef", _loader)
assert _spec and _spec.loader
coderef = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(coderef)


def _run(capsys: pytest.CaptureFixture[str], fn, root: Path, *args: str) -> str:
    rc = fn(root, list(args))
    out = capsys.readouterr().out
    assert rc == 0, out
    return out


class TestDeps:
    def test_direct_call_same_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "a.py").write_text(
            "def a():\n    return b()\n\n\ndef b():\n    return 1\n"
        )
        out = _run(capsys, coderef.cmd_deps, tmp_path, "a.py::a")
        assert "a.py::b" in out
        assert "1 dependenc" in out

    def test_imported_name_across_files(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "helper.py").write_text("def helper():\n    return 1\n")
        (tmp_path / "a.py").write_text(
            "from helper import helper\n\n\ndef a():\n    return helper()\n"
        )
        out = _run(capsys, coderef.cmd_deps, tmp_path, "a.py::a")
        assert "helper.py::helper" in out

    def test_stdlib_call_is_unresolved_not_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "a.py").write_text(
            "import os\n\n\ndef a():\n    return os.getcwd()\n"
        )
        out = _run(capsys, coderef.cmd_deps, tmp_path, "a.py::a")
        assert "external/unresolved" in out

    def test_local_variable_is_not_a_dependency(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # a local var read (Load ctx) must not be mistaken for an external dep.
        (tmp_path / "a.py").write_text("def a():\n    x = 1\n    return x\n")
        out = _run(capsys, coderef.cmd_deps, tmp_path, "a.py::a")
        assert "0 dependenc" in out
        assert "0 external/unresolved" in out

    def test_self_call_resolves_to_sibling_method(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "a.py").write_text(
            "class Widget:\n"
            "    def method_a(self):\n"
            "        return self.method_b()\n"
            "\n"
            "    def method_b(self):\n"
            "        return 1\n"
        )
        out = _run(capsys, coderef.cmd_deps, tmp_path, "a.py::Widget.method_a")
        assert "a.py::Widget.method_b" in out

    def test_self_call_to_inherited_attr_is_unresolved_not_dropped(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "a.py").write_text(
            "class Widget:\n"
            "    def method_a(self):\n"
            "        return self.inherited_method()\n"
        )
        out = _run(capsys, coderef.cmd_deps, tmp_path, "a.py::Widget.method_a")
        assert "self.inherited_method" in out
        assert "external/unresolved" in out

    def test_depth_two_recurses(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "a.py").write_text(
            "def a():\n"
            "    return b()\n"
            "\n\n"
            "def b():\n"
            "    return c()\n"
            "\n\n"
            "def c():\n"
            "    return 1\n"
        )
        out = _run(capsys, coderef.cmd_deps, tmp_path, "--depth", "2", "a.py::a")
        assert "a.py::b" in out
        assert "a.py::c" in out
        # default depth (1) does NOT recurse into c.
        out1 = _run(capsys, coderef.cmd_deps, tmp_path, "a.py::a")
        assert "a.py::b" in out1
        assert "a.py::c" not in out1


class TestCallers:
    def test_confirmed_caller_found(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "lib.py").write_text("def foo():\n    return 1\n")
        (tmp_path / "user.py").write_text(
            "from lib import foo\n\n\ndef use():\n    return foo()\n"
        )
        out = _run(capsys, coderef.cmd_callers, tmp_path, "lib.py::foo")
        assert "user.py:" in out

    def test_unrelated_same_named_symbol_not_reported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "lib.py").write_text("def foo():\n    return 1\n")
        (tmp_path / "user.py").write_text(
            "from lib import foo\n\n\ndef use():\n    return foo()\n"
        )
        # a same-named foo elsewhere that never imports lib's foo — not a caller.
        (tmp_path / "other.py").write_text(
            "def foo():\n    return 2\n\n\ndef use2():\n    return foo()\n"
        )
        out = _run(capsys, coderef.cmd_callers, tmp_path, "lib.py::foo")
        assert "user.py:" in out
        assert "other.py" not in out

    def test_git_grep_path_finds_tracked_and_untracked_callers(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # a real git repo (not the ast-fallback scan) exercises the actual
        # `git grep` path, including --untracked for a file never `git add`ed.
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True
        )
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
        (tmp_path / "lib.py").write_text("def foo():\n    return 1\n")
        (tmp_path / "tracked_user.py").write_text(
            "from lib import foo\n\n\ndef use():\n    return foo()\n"
        )
        subprocess.run(
            ["git", "add", "lib.py", "tracked_user.py"], cwd=tmp_path, check=True
        )
        # a second caller left UNTRACKED (never `git add`ed) — must still be found.
        (tmp_path / "untracked_user.py").write_text(
            "from lib import foo\n\n\ndef use2():\n    return foo()\n"
        )
        out = _run(capsys, coderef.cmd_callers, tmp_path, "lib.py::foo")
        assert "tracked_user.py:" in out
        assert "untracked_user.py:" in out

    def test_refs_alias_dispatches_through_main(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "lib.py").write_text("def foo():\n    return 1\n")
        (tmp_path / "user.py").write_text(
            "from lib import foo\n\n\ndef use():\n    return foo()\n"
        )
        monkeypatch.setattr(coderef, "repo_root", lambda: tmp_path)
        rc = coderef.main(["coderef", "refs", "lib.py::foo"])
        assert rc == 0
        assert "user.py:" in capsys.readouterr().out


class TestGrimpVerbs:
    def test_imports_over_a_real_package(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        pytest.importorskip("grimp")
        pkg = tmp_path / "src" / "pkgx"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "a.py").write_text("from pkgx import b\n")
        (pkg / "b.py").write_text("x = 1\n")
        monkeypatch.syspath_prepend(str(tmp_path / "src"))
        out = _run(capsys, coderef.cmd_imports, tmp_path, "pkgx.a")
        assert "pkgx.b" in out

    def test_importers_over_a_real_package(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        pytest.importorskip("grimp")
        pkg = tmp_path / "src" / "pkgy"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "a.py").write_text("from pkgy import b\n")
        (pkg / "b.py").write_text("x = 1\n")
        monkeypatch.syspath_prepend(str(tmp_path / "src"))
        out = _run(capsys, coderef.cmd_importers, tmp_path, "pkgy.b")
        assert "pkgy.a" in out

    def test_missing_grimp_reports_and_returns_1(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # simulate grimp being absent regardless of the real environment, so
        # this assertion holds whether or not the dev image has it installed.
        real_import = __import__

        def fake_import(
            name: str,
            globals: dict[str, object] | None = None,
            locals: dict[str, object] | None = None,
            fromlist: tuple[str, ...] = (),
            level: int = 0,
        ) -> object:
            if name == "grimp":
                raise ImportError("no grimp")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr("builtins.__import__", fake_import)
        rc = coderef.cmd_imports(tmp_path, ["precis.store"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "grimp" in out
