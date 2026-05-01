"""Tests for ``view='entries'`` — pyproject scripts + ``__main__`` guards."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers import _python_entries as entries_mod
from precis.handlers.python import PythonHandler
from precis.python_index import index_repo

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write(repo: Path, rel: str, content: str) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return p


@pytest.fixture
def repo_with_pyproject(tmp_path: Path) -> Path:
    """Repo with pyproject.toml (scripts + entry-points), one __main__ guard."""
    _write(
        tmp_path,
        "pyproject.toml",
        """
        [project]
        name = "demo"
        version = "0.0.0"

        [project.scripts]
        demo-cli = "demo.cli:main"
        demo-helper = "demo.cli:helper_main"

        [project.entry-points.demo_plugins]
        plugin-a = "demo.plugins.a:setup"
        """,
    )
    _write(tmp_path, "demo/__init__.py", "")
    _write(
        tmp_path,
        "demo/cli.py",
        """
        def main() -> None:
            print("hi")


        def helper_main() -> None:
            pass


        if __name__ == "__main__":
            main()
        """,
    )
    _write(tmp_path, "demo/plugins/__init__.py", "")
    _write(
        tmp_path,
        "demo/plugins/a.py",
        """
        def setup() -> None:
            pass
        """,
    )
    return tmp_path


@pytest.fixture
def handler(repo_with_pyproject: Path) -> PythonHandler:
    return PythonHandler(hub=Hub(), roots={"demo": repo_with_pyproject})


# ---------------------------------------------------------------------------
# pyproject discovery
# ---------------------------------------------------------------------------


def test_find_pyproject_at_root(repo_with_pyproject: Path) -> None:
    found = entries_mod._find_pyproject(repo_with_pyproject)
    assert found == repo_with_pyproject / "pyproject.toml"


def test_find_pyproject_walks_up(repo_with_pyproject: Path) -> None:
    """A root pointing at `src/<pkg>/` finds the project file by walking up."""
    found = entries_mod._find_pyproject(repo_with_pyproject / "demo" / "plugins")
    assert found == repo_with_pyproject / "pyproject.toml"


def test_find_pyproject_returns_none_when_missing(tmp_path: Path) -> None:
    found = entries_mod._find_pyproject(tmp_path)
    assert found is None


# ---------------------------------------------------------------------------
# Console scripts
# ---------------------------------------------------------------------------


def test_load_console_scripts_resolves_to_files(repo_with_pyproject: Path) -> None:
    idx = index_repo(repo_with_pyproject)
    report = entries_mod.find_entries(idx)
    by_name = {s.name: s for s in report.console_scripts}

    assert "demo-cli" in by_name
    assert by_name["demo-cli"].entry == "demo.cli:main"
    assert by_name["demo-cli"].group == "scripts"
    assert by_name["demo-cli"].file == "demo/cli.py"
    assert by_name["demo-cli"].line is not None  # resolved to a line

    assert "demo-helper" in by_name
    assert by_name["demo-helper"].entry == "demo.cli:helper_main"


def test_load_entry_point_groups(repo_with_pyproject: Path) -> None:
    """`[project.entry-points.<group>]` entries appear with group label."""
    idx = index_repo(repo_with_pyproject)
    report = entries_mod.find_entries(idx)

    plugin = next((s for s in report.console_scripts if s.name == "plugin-a"), None)
    assert plugin is not None
    assert plugin.entry == "demo.plugins.a:setup"
    assert plugin.group == "entry-points.demo_plugins"
    assert plugin.file == "demo/plugins/a.py"


def test_unresolved_entry_has_none_file(tmp_path: Path) -> None:
    """An entry whose target isn't in the indexed repo (e.g. installed
    third-party) has file=None instead of crashing."""
    _write(
        tmp_path,
        "pyproject.toml",
        """
        [project]
        name = "demo"
        version = "0.0.0"

        [project.scripts]
        external = "third_party.module:main"
        """,
    )
    _write(tmp_path, "demo/__init__.py", "")

    idx = index_repo(tmp_path)
    report = entries_mod.find_entries(idx)
    by_name = {s.name: s for s in report.console_scripts}
    assert by_name["external"].file is None
    assert by_name["external"].line is None


def test_no_pyproject_yields_empty_scripts(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/m.py", "x = 1\n")
    idx = index_repo(tmp_path)
    report = entries_mod.find_entries(idx)
    assert report.pyproject_path is None
    assert report.console_scripts == ()


def test_invalid_pyproject_does_not_crash(tmp_path: Path) -> None:
    """Malformed TOML logs a warning, returns empty scripts."""
    _write(tmp_path, "pyproject.toml", "this is not = valid = toml [")
    _write(tmp_path, "pkg/__init__.py", "")
    idx = index_repo(tmp_path)
    report = entries_mod.find_entries(idx)
    assert report.console_scripts == ()


# ---------------------------------------------------------------------------
# __main__ guard detection
# ---------------------------------------------------------------------------


def test_main_guard_detected(repo_with_pyproject: Path) -> None:
    idx = index_repo(repo_with_pyproject)
    report = entries_mod.find_entries(idx)
    files = {g.file for g in report.main_guards}
    assert "demo/cli.py" in files
    guard = next(g for g in report.main_guards if g.file == "demo/cli.py")
    assert "main()" in guard.body_summary


def test_main_guard_symmetric_form(tmp_path: Path) -> None:
    """`if "__main__" == __name__:` (operands swapped) also detected."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        def go() -> None: pass

        if "__main__" == __name__:
            go()
        """,
    )
    idx = index_repo(tmp_path)
    report = entries_mod.find_entries(idx)
    assert len(report.main_guards) == 1


def test_main_guard_ignored_when_not_top_level(tmp_path: Path) -> None:
    """A `__main__` check inside a function body is NOT a module-level
    runnable. We only detect top-level guards (mirrors how Python
    actually treats them)."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        def helper():
            if __name__ == "__main__":
                pass
        """,
    )
    idx = index_repo(tmp_path)
    report = entries_mod.find_entries(idx)
    assert report.main_guards == ()


def test_no_main_guards_in_repo(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/m.py", "x = 1\n")
    idx = index_repo(tmp_path)
    report = entries_mod.find_entries(idx)
    assert report.main_guards == ()


def test_summarise_body_truncates() -> None:
    """Long body lines get truncated to keep the rendered table tidy."""
    long_call = "x" * 100 + "()"
    summary = entries_mod._summarise_body([])
    assert summary == "pass"

    import ast

    expr = ast.parse(f"{long_call}", mode="exec").body[0]
    summary = entries_mod._summarise_body([expr])
    assert "…" in summary
    assert len(summary) < 80


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def test_render_includes_scripts_and_guards(handler: PythonHandler) -> None:
    out = handler.get(id="demo", view="entries")
    body = out.body
    assert "Console scripts" in body
    assert "demo-cli" in body
    assert "demo.cli:main" in body
    assert "__main__ guards" in body
    assert "demo/cli.py:" in body
    assert "Next:" in body


def test_render_no_pyproject(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/m.py", "x = 1\n")
    handler = PythonHandler(hub=Hub(), roots={"r": tmp_path})
    out = handler.get(id="r", view="entries")
    assert "no pyproject.toml found" in out.body


def test_render_groups_entry_points(handler: PythonHandler) -> None:
    """Entry-point groups appear with their group label, scripts don't."""
    out = handler.get(id="demo", view="entries")
    body = out.body
    assert "[entry-points.demo_plugins]" in body
    # The 'scripts' group label is implicit (header is "Console scripts:").
    assert "[scripts]" not in body


# ---------------------------------------------------------------------------
# Handler integration
# ---------------------------------------------------------------------------


def test_handler_rejects_file_id_with_entries_view(
    handler: PythonHandler,
) -> None:
    with pytest.raises(BadInput, match="bare alias"):
        handler.get(id="demo/demo/cli.py", view="entries")


def test_handler_rejects_qualname_with_entries_view(
    handler: PythonHandler,
) -> None:
    with pytest.raises(BadInput, match="bare alias"):
        handler.get(id="demo::demo.cli.main", view="entries")
