"""scripts/lib/check-core-deps.py refuses a stale image with a way through.

gr513760: a stale precis-dev image hard-refused ``scripts/test`` and the only
escape (``UV_WITH="--with <dep>"``) was undocumented outside the refusal line.
The refusal now names the exact bridge command, the ``PRECIS_TEST_AUTOBRIDGE=1``
opt-in and the durable fix (``scripts/build-image precis-dev``, the release
owner's job). ``--with-flags`` is the query mode the autobridge pre-pass
consumes. Pure tests — a fake pyproject, no docker.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "check_core_deps", REPO / "scripts" / "lib" / "check-core-deps.py"
)
assert _SPEC and _SPEC.loader
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

MISSING_DEP = "definitely-not-a-real-dep-xyz123"


def _pyproject(tmp_path: Path, deps: list[str]) -> Path:
    p = tmp_path / "pyproject.toml"
    deps_toml = ", ".join(f'"{d}"' for d in deps)
    p.write_text(
        f'[project]\nname = "precis-mcp"\nversion = "0"\n'
        f"dependencies = [{deps_toml}]\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def stale_image(monkeypatch, tmp_path):
    """A pyproject declaring one dep the baked venv cannot have."""
    monkeypatch.setattr(mod, "PYPROJECT_PATH", _pyproject(tmp_path, [MISSING_DEP]))


def test_missing_dep_refusal_names_bridge_and_durable_fix(stale_image, capsys):
    """The gripe's core: the refusal must carry BOTH exits — the UV_WITH
    bridge command for this run and the durable image rebuild."""
    assert mod.main([]) == 1
    err = capsys.readouterr().err
    assert MISSING_DEP in err
    assert f'UV_WITH="--with {MISSING_DEP}"' in err
    assert "PRECIS_TEST_AUTOBRIDGE=1" in err
    assert "scripts/build-image precis-dev" in err


def test_with_flags_prints_only_the_bridge_flags(stale_image, capsys):
    """Query mode feeds scripts/test's PRECIS_TEST_AUTOBRIDGE pre-pass, so
    stdout must be exactly the uv flags — capture-safe."""
    assert mod.main(["--with-flags"]) == 1
    out = capsys.readouterr()
    assert out.out.strip() == f"--with {MISSING_DEP}"
    assert out.err == ""


def test_with_flags_clean_is_empty_success(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mod, "PYPROJECT_PATH", _pyproject(tmp_path, []))
    assert mod.main(["--with-flags"]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_marker_gated_dep_is_not_missing(monkeypatch, tmp_path, capsys):
    """A dep whose environment marker doesn't apply here is meant to be
    absent — never a refusal, never a bridge flag."""
    monkeypatch.setattr(
        mod,
        "PYPROJECT_PATH",
        _pyproject(tmp_path, [f"{MISSING_DEP}; sys_platform == 'win32'"]),
    )
    import sys

    if sys.platform == "win32":
        pytest.skip("the marker applies on this host")
    assert mod.main([]) == 0
    assert capsys.readouterr().err == ""


def test_scripts_test_wires_autobridge():
    """The opt-in must reach the container and consume --with-flags: pass the
    env through `compose run`, compute the flags in a pre-pass, and expand the
    result into the real `uv run` invocation."""
    text = (REPO / "scripts" / "test").read_text(encoding="utf-8")
    assert 'PRECIS_TEST_AUTOBRIDGE="$AUTOBRIDGE"' in text
    assert "check-core-deps.py --with-flags" in text
    assert "uv run --no-sync ${UV_WITH} ${AUTO_WITH}" in text
