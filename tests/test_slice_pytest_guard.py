"""``tests/conftest.py``'s slice-lane bare-suite refusal (gr626270).

A builder session (``PRECIS_SLICE_ID`` in its env) may run ``uv run
pytest`` only on named test files — the land lint's steps; a directory
or no-arg selection is refused before collection. Inside
``scripts/test``'s container ``PRECIS_SLICE_ID`` is absent and
``PRECIS_TEST_PG_URL`` vetoes the check, so the gate's own full-suite
runs never trip it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _pytest.outcomes import Exit

import tests.conftest as conftest


def _config(args: list[str]) -> Any:
    return SimpleNamespace(args=list(args))


@pytest.fixture(autouse=True)
def _slice_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_SLICE_ID", "td1")
    monkeypatch.delenv("PRECIS_TEST_PG_URL", raising=False)


def test_named_test_files_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real = tmp_path / "test_x.py"
    real.write_text("", encoding="utf-8")
    for args in (
        ["tests/test_a.py"],
        [str(real)],
        ["tests/test_a.py::TestX::test_y"],
        ["tests/test_a.py", "tests/test_b.py"],
    ):
        conftest._refuse_bare_suite_on_a_slice(_config(args))


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["tests/"],
        ["tests"],
        ["."],
        ["tests/workers"],
        ["tests/test_a.py", "tests"],
        ["bench"],
    ],
)
def test_directory_or_default_selection_is_refused(args: list[str]) -> None:
    with pytest.raises(Exit):
        conftest._refuse_bare_suite_on_a_slice(_config(args))


def test_no_slice_env_no_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_SLICE_ID", raising=False)
    conftest._refuse_bare_suite_on_a_slice(_config(["tests"]))


def test_container_pg_url_vetoes_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """``PRECIS_TEST_PG_URL`` is set unconditionally in scripts/test's
    container — a forwarded ``PRECIS_SLICE_ID`` there must not refuse
    the gate's own full-suite run."""
    monkeypatch.setenv(
        "PRECIS_TEST_PG_URL",
        "postgresql://postgres@precis-test-db:5432/precis_test",
    )
    conftest._refuse_bare_suite_on_a_slice(_config(["tests"]))
