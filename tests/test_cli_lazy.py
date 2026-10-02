"""Lazy CLI subcommand loading — ``precis.cli.registry`` + ``main``.

Only the invoked subcommand's module is imported. These tests pin the
three properties that make that invisible to users and safe for
``serve``: help output identical to the eager tree, no stray imports,
and a subcommand whose dependency is missing failing alone, by name.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from precis.cli import registry
from precis.cli.main import _build_parser
from precis.cli.main import main as cli_main
from precis.cli.registry import Command


def _subparsers(parser: argparse.ArgumentParser) -> Any:
    return next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))


def _real_helps(cmds: tuple[Command, ...], jobs: bool) -> dict[str, str | None]:
    full = _subparsers(_build_parser(None))
    if jobs:
        full = _subparsers(full.choices["jobs"])
    helps = {a.dest: a.help for a in full._choices_actions}
    # A SUPPRESSed command has no choices_action; it is still registered.
    return {c.name: helps.get(c.name, argparse.SUPPRESS) for c in cmds}


@pytest.mark.parametrize(
    ("cmds", "jobs"),
    [(registry.COMMANDS, False), (registry.JOB_COMMANDS, True)],
    ids=["top-level", "jobs"],
)
def test_registry_help_matches_owning_module(
    cmds: tuple[Command, ...], jobs: bool
) -> None:
    """Each registry row's help equals the ``help=`` its module registers."""
    real = _real_helps(cmds, jobs)
    assert {c.name: c.help for c in cmds} == real


def test_top_level_help_identical_to_eager_tree() -> None:
    assert _build_parser(()).format_help() == _build_parser(None).format_help()


def test_jobs_help_identical_to_eager_tree() -> None:
    lazy = _subparsers(_build_parser(("jobs",))).choices["jobs"]
    eager = _subparsers(_build_parser(None)).choices["jobs"]
    assert lazy.format_help() == eager.format_help()


_ALWAYS = {"precis.cli.main", "precis.cli.registry", "precis.cli._common"}


@pytest.mark.parametrize(
    ("argv", "owner"),
    [
        (["--help"], None),
        (["serve", "--help"], None),
        (["jobs", "--help"], None),
        (["taproot", "--help"], "precis.cli.taproot"),
        (["jobs", "kill", "--help"], "precis.cli.jobs_admin"),
    ],
)
def test_help_imports_only_the_owning_module(
    argv: list[str], owner: str | None
) -> None:
    """Asserted on ``sys.modules`` in a fresh interpreter, not by timing."""
    code = textwrap.dedent(
        f"""
        import sys
        sys.argv = ["precis", *{argv!r}]
        from precis.cli import main
        try:
            main()
        except SystemExit:
            pass
        print(",".join(m for m in sys.modules if m.startswith("precis.cli.")),
              file=sys.stderr)
        """
    )
    run = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )
    loaded = set(run.stderr.strip().splitlines()[-1].split(","))
    expected = _ALWAYS | ({owner} if owner else set())
    assert loaded <= expected, sorted(loaded - expected)


@pytest.fixture
def broken_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Command:
    """A registered subcommand whose module imports a dependency that is absent."""
    (tmp_path / "precis_broken_cli_cmd.py").write_text(
        "import precis_absent_dep_xyz  # noqa: F401\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    cmd = Command("broken", "precis_broken_cli_cmd", "A command with a missing dep.")
    monkeypatch.setattr(registry, "COMMANDS", (*registry.COMMANDS, cmd))
    return cmd


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["precis", *argv])
    try:
        cli_main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


@pytest.mark.usefixtures("broken_command")
def test_missing_dependency_leaves_help_working(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(monkeypatch, "--help") == 0
    assert "A command with a missing dep." in capsys.readouterr().out
    assert _run(monkeypatch, "migrate", "--help") == 0


@pytest.mark.usefixtures("broken_command")
def test_missing_dependency_leaves_serve_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import precis.server

    hit: list[str] = []
    monkeypatch.setattr(precis.server, "main", lambda **kw: hit.append(kw["transport"]))
    assert _run(monkeypatch, "serve") == 0
    assert hit == ["stdio"]


@pytest.mark.usefixtures("broken_command")
def test_missing_dependency_named_when_invoked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(monkeypatch, "broken") == 1
    err = capsys.readouterr().err
    assert "precis broken: unavailable" in err
    assert "missing dependency 'precis_absent_dep_xyz'" in err
    assert "Traceback" not in err


class _FakeEntryPoint:
    def __init__(self, name: str, value: object) -> None:
        self.name = name
        self._value = value

    def load(self) -> object:
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


def test_plugin_commands_join_after_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin adds commands; a broken, malformed or colliding one is skipped."""
    good = Command("plugin-cmd", "precis_plugin_cli", "From a plugin.")
    eps = [
        _FakeEntryPoint("b-good", [good]),
        _FakeEntryPoint("a-broken", ImportError("no")),
        _FakeEntryPoint("c-malformed", "not commands"),
        _FakeEntryPoint("d-collides", [Command("migrate", "x", "dup")]),
        _FakeEntryPoint("e-reserved", [Command("serve", "x", "dup")]),
    ]
    monkeypatch.setattr(registry, "entry_points", lambda group: eps)
    cmds = registry.commands()
    assert cmds[: len(registry.COMMANDS)] == registry.COMMANDS
    assert cmds[len(registry.COMMANDS) :] == (good,)
