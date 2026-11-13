"""``PRECIS_PYTHON_READONLY=1`` makes every python write verb refuse (gr648136)."""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import Unsupported
from precis.handlers.python import PythonHandler

_SRC = '"""Module."""\n\n\ndef helper(x: int) -> int:\n    """A helper."""\n    return x + 1\n'


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "m.py").write_text(_SRC, encoding="utf-8")
    return tmp_path


@pytest.fixture
def h(repo: Path) -> PythonHandler:
    return PythonHandler(hub=Hub(), roots={"r": repo})


def _snapshot(repo: Path) -> dict[str, str]:
    return {
        str(p.relative_to(repo)): p.read_text(encoding="utf-8")
        for p in repo.rglob("*.py")
    }


@pytest.mark.parametrize("flag", ["1", "true", "YES"])
def test_every_write_verb_refuses(
    h: PythonHandler, repo: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    monkeypatch.setenv("PRECIS_PYTHON_READONLY", flag)
    before = _snapshot(repo)
    calls = [
        lambda: h.put(id="r/pkg/new.py", text="x = 1\n", mode="create"),
        lambda: h.edit(
            id="r/pkg/m.py", mode="find-replace", find="x + 1", text="x + 2"
        ),
        lambda: h.edit(id="r/pkg/m.py", mode="append", text="\nY = 1\n"),
        lambda: h.edit(id="r/pkg/m.py", mode="insert", text="Z = 1\n", where="top"),
        lambda: h.edit(
            id="r::pkg.m.helper", mode="replace", text="def helper(): ...\n"
        ),
        lambda: h.delete(id="r::pkg.m.helper"),
        lambda: h.delete(id="r/pkg/m.py"),
    ]
    for call in calls:
        with pytest.raises(Unsupported, match="PRECIS_PYTHON_READONLY"):
            call()
    assert _snapshot(repo) == before


def test_reads_unaffected_under_switch(
    h: PythonHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_PYTHON_READONLY", "1")
    assert "helper" in h.get(id="r::pkg.m.helper").body


@pytest.mark.parametrize("flag", [None, "", "0"])
def test_unset_or_off_still_writes(
    h: PythonHandler, repo: Path, monkeypatch: pytest.MonkeyPatch, flag: str | None
) -> None:
    if flag is None:
        monkeypatch.delenv("PRECIS_PYTHON_READONLY", raising=False)
    else:
        monkeypatch.setenv("PRECIS_PYTHON_READONLY", flag)
    h.put(id="r/pkg/new.py", text="x = 1\n", mode="create")
    assert (repo / "pkg" / "new.py").read_text(encoding="utf-8") == "x = 1\n"
