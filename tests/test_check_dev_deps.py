"""scripts/lib/check-dev-deps.py refuses a stale image's dev group — gr519535.

A dep added to ``[dependency-groups] dev`` after the precis-dev image bake
is invisible under ``uv run --no-sync``, and its tests quiet-skip via
importorskip instead of going red (found on worktree-bugs-ab, gr519391:
ansible-core passed on the host, skipped in the gate). The guard diffs
uv.lock's root ``[package.dev-dependencies].dev`` against installed
distributions — presence, not import, since half the group (types-*,
*-stubs) ships no importable module — and refuses naming the package and
the rebuild. Pure tests — a fake uv.lock, no docker — plus the
fake-docker rig assertion that the autobridge pre-pass queries it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from tests.test_scripts_test_env import _run, _run_line

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "check_dev_deps", REPO / "scripts" / "lib" / "check-dev-deps.py"
)
assert _SPEC and _SPEC.loader
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

MISSING_DEP = "definitely-not-a-real-dep-xyz123"


def _lock(tmp_path: Path, entries: list[str]) -> Path:
    """A minimal uv.lock: one editable root carrying the dev group. Each
    entry is the inside of the dep's inline table, e.g.
    'name = "pytest"' or 'name = "x", marker = "sys_platform == \'win32\'"'."""
    body = "\n".join(f"    {{ {e} }}," for e in entries)
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "version = 1\n\n[[package]]\n"
        'name = "precis-mcp"\nversion = "0.0.0"\n'
        'source = { editable = "." }\n\n'
        "[package.dev-dependencies]\ndev = [\n" + body + "\n]\n",
        encoding="utf-8",
    )
    return lock


def _pyproject(tmp_path: Path, dev: list[str] | None = None) -> Path:
    deps_toml = ", ".join(f'"{d}"' for d in dev or [])
    p = tmp_path / "pyproject.toml"
    p.write_text(
        '[project]\nname = "precis-mcp"\nversion = "0"\ndependencies = []\n\n'
        f"[dependency-groups]\ndev = [{deps_toml}]\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def stale_image(monkeypatch, tmp_path):
    """A lock declaring one dev-group dep the baked venv cannot have."""
    monkeypatch.setattr(mod, "LOCK_PATH", _lock(tmp_path, [f'name = "{MISSING_DEP}"']))
    monkeypatch.setattr(mod, "PYPROJECT_PATH", _pyproject(tmp_path))


def test_missing_dev_dep_refusal_names_package_and_fix(stale_image, capsys):
    """The gripe's core: not a quiet importorskip skip — a refusal that
    names the package, the UV_WITH bridge and the durable rebuild."""
    assert mod.main([]) == 1
    err = capsys.readouterr().err
    assert MISSING_DEP in err
    assert "dev-group" in err
    assert "importorskip" in err
    assert f'UV_WITH="--with {MISSING_DEP}"' in err
    assert "PRECIS_TEST_AUTOBRIDGE=1" in err
    assert "run scripts/build-image precis-dev" in err


def test_installed_dev_dep_is_clean(monkeypatch, tmp_path, capsys):
    """pytest is a real dev-group entry and installed wherever the suite
    runs — the happy path stays silent."""
    monkeypatch.setattr(mod, "LOCK_PATH", _lock(tmp_path, ['name = "pytest"']))
    monkeypatch.setattr(mod, "PYPROJECT_PATH", _pyproject(tmp_path))
    assert mod.main([]) == 0
    assert capsys.readouterr().err == ""


def test_with_flags_prints_only_the_bridge_flags(stale_image, capsys):
    """Query mode feeds scripts/test's PRECIS_TEST_AUTOBRIDGE pre-pass, so
    stdout must be exactly the uv flags — capture-safe."""
    assert mod.main(["--with-flags"]) == 1
    out = capsys.readouterr()
    assert out.out.strip() == f"--with {MISSING_DEP}"
    assert out.err == ""


def test_marker_gated_dev_dep_is_not_missing(monkeypatch, tmp_path, capsys):
    """A dev dep whose environment marker doesn't apply here is meant to be
    absent — never a refusal, never a bridge flag."""
    monkeypatch.setattr(
        mod,
        "LOCK_PATH",
        _lock(
            tmp_path,
            [f'name = "{MISSING_DEP}", marker = "sys_platform == \'win32\'"'],
        ),
    )
    monkeypatch.setattr(mod, "PYPROJECT_PATH", _pyproject(tmp_path))
    if sys.platform == "win32":
        pytest.skip("the marker applies on this host")
    assert mod.main([]) == 0
    assert capsys.readouterr().err == ""


def test_presence_is_dist_metadata_not_import(tmp_path):
    """types-*/stubs packages install no importable module — the check asks
    importlib.metadata for the DIST (canonicalised), it does not import."""
    seen: list[str] = []

    def installed(name: str) -> bool:
        seen.append(name)
        return True

    missing = mod.missing_dev_deps(
        {},
        _lock(tmp_path, ['name = "types-pyyaml"']).read_text(encoding="utf-8"),
        installed=installed,
    )
    assert missing == []
    assert seen == ["types-pyyaml"]


def test_pyproject_dev_group_is_the_fallback(monkeypatch, tmp_path, capsys):
    """A lock with no root dev-dependencies table (it predates dev groups)
    falls back to pyproject's [dependency-groups] dev."""
    lock = tmp_path / "uv.lock"
    lock.write_text(
        'version = 1\n\n[[package]]\nname = "precis-mcp"\n'
        'version = "0.0.0"\nsource = { editable = "." }\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "LOCK_PATH", lock)
    monkeypatch.setattr(mod, "PYPROJECT_PATH", _pyproject(tmp_path, dev=[MISSING_DEP]))
    assert mod.main([]) == 1
    assert MISSING_DEP in capsys.readouterr().err


def test_lock_root_picked_by_project_name(tmp_path):
    """A workspace lock can carry several editable roots — the dev group
    belongs to THIS project, not the first editable package."""
    lock = tmp_path / "uv.lock"
    lock.write_text(
        'version = 1\n\n[[package]]\nname = "other-pkg"\nversion = "0"\n'
        'source = { editable = "../other" }\n\n'
        '[package.dev-dependencies]\ndev = [{ name = "other-dev-dep" }]\n\n'
        '[[package]]\nname = "precis-mcp"\nversion = "0"\n'
        'source = { editable = "." }\n\n'
        '[package.dev-dependencies]\ndev = [{ name = "my-dev-dep" }]\n',
        encoding="utf-8",
    )
    data = {"project": {"name": "precis-mcp"}}
    assert mod.dev_group_specs(data, lock.read_text(encoding="utf-8")) == ["my-dev-dep"]


def test_real_lock_parses_dev_group():
    specs = mod.dev_group_specs({}, (REPO / "uv.lock").read_text(encoding="utf-8"))
    assert "pytest" in specs
    assert "types-pyyaml" in specs


def test_pytest_inner_runs_the_guard_before_pytest():
    """Wired into the stamp-cached preflight chain, after the core-deps
    import check and before pytest ever starts."""
    inner = (REPO / "scripts" / "lib" / "pytest-inner.sh").read_text(encoding="utf-8")
    assert "scripts/lib/check-dev-deps.py" in inner
    assert inner.index("check-core-deps.py") < inner.index("check-dev-deps.py")
    assert inner.index("check-dev-deps.py") < inner.index("-m pytest")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the shipped bash script + fake docker are POSIX-only",
)
def test_autobridge_prepass_queries_the_dev_group(tmp_path):
    """gr519535: the opt-in bridge must cover dev deps too — a dev-group
    gap (ansible-core) is bridgeable, so the refusal's own escape hatch
    has to ask check-dev-deps for its flags. And AUTO_WITH must be
    exported so pytest-inner.sh knows the pass ran under the overlay —
    a bridged pass must never write the preflight stamp."""
    rc, log, stderr = _run(tmp_path)
    assert rc == 0, stderr
    line = _run_line(log)
    assert "check-dev-deps.py --with-flags" in line
    assert "export AUTO_WITH" in line
