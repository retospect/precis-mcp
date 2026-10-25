"""scripts/lib/check-lock-pins.py refuses an image whose packages differ from uv.lock.

gr494988: the precis-dev image carried autocatpath 0.30.0 while uv.lock pinned
0.31.0, so engine-versioned tests went red with no pointer at the environment.
The guard is pure (lock text + an injected installed-state lookup); no docker.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "check_lock_pins", REPO / "scripts" / "lib" / "check-lock-pins.py"
)
assert _SPEC and _SPEC.loader
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

COMMIT = "239654c82a99834e732b0b436edcfeaecd150541"
LOCK = f"""
[[package]]
name = "autocatpath"
version = "0.31.0"
source = {{ git = "https://example.invalid/catpath?rev={COMMIT}#{COMMIT}" }}

[[package]]
name = "torch"
version = "2.5.0"
source = {{ registry = "https://pypi.org/simple" }}

[[package]]
name = "numpy"
version = "1.26.0"
source = {{ registry = "https://pypi.org/simple" }}

[[package]]
name = "numpy"
version = "2.1.0"
source = {{ registry = "https://pypi.org/simple" }}

[[package]]
name = "ruff"
version = "0.16.0"
source = {{ registry = "https://pypi.org/simple" }}

[[package]]
name = "precis-mcp"
version = "0.1.0"
source = {{ editable = "." }}
dependencies = [
    {{ name = "autocatpath" }},
    {{ name = "numpy" }},
    {{ name = "Not-Baked" }},
]

[package.optional-dependencies]
ml = [
    {{ name = "torch" }},
]

[package.dev-dependencies]
dev = [
    {{ name = "ruff" }},
]

[[package]]
name = "Not-Baked"
version = "9.9.9"
source = {{ registry = "https://pypi.org/simple" }}
"""


def _stale(installed: dict[str, tuple[str, str | None]]) -> list[str]:
    return mod.stale_packages(LOCK, installed.get)


def test_matching_image_is_clean():
    assert not _stale(
        {
            "autocatpath": ("0.31.0", COMMIT),
            "torch": ("2.5.0+cpu", None),
            "numpy": ("1.26.0", None),
        }
    )


def test_old_autocatpath_is_named():
    stale = _stale({"autocatpath": ("0.30.0", "deadbeef")})
    assert len(stale) == 1
    assert "autocatpath (image 0.30.0, uv.lock 0.31.0)" in stale[0]


def test_same_version_other_git_commit_is_stale():
    assert _stale({"autocatpath": ("0.31.0", "deadbeef")})


def test_marker_fork_passes_if_any_pin_matches():
    assert not _stale({"numpy": ("2.1.0", None)})
    assert _stale({"numpy": ("2.0.0", None)})


def test_uninstalled_and_editable_are_skipped():
    assert not _stale({})
    assert not _stale({"precis-mcp": ("0.0.1", None)})


def test_git_pin_without_recorded_commit_is_stale():
    assert _stale({"autocatpath": ("0.31.0", None)})


def test_lookup_uses_canonical_names():
    seen = []
    mod.stale_packages(LOCK, lambda n: seen.append(n))
    assert "not-baked" in seen


def test_dev_group_tooling_is_not_checked():
    assert not _stale({"ruff": ("0.15.0", None)})


def test_local_version_suffix_ignored():
    assert not _stale({"torch": ("2.5.0+cu121", None)})


@pytest.fixture
def stale_image(monkeypatch, tmp_path):
    lock = tmp_path / "uv.lock"
    lock.write_text(LOCK, encoding="utf-8")
    monkeypatch.setattr(mod, "LOCK_PATH", lock)
    monkeypatch.setattr(
        mod,
        "installed_state",
        lambda name: ("0.30.0", None) if name == "autocatpath" else None,
    )
    monkeypatch.delenv("PRECIS_STRICT_LOCK_CHECK", raising=False)


def test_main_warns_by_default_naming_package_and_rebuild(stale_image, capsys):
    assert mod.main() == 0
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "autocatpath" in err
    assert "scripts/build-image precis-dev" in err


def test_main_refuses_under_strict_opt_in(stale_image, monkeypatch, capsys):
    monkeypatch.setenv("PRECIS_STRICT_LOCK_CHECK", "1")
    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "ERROR" in err
    assert "autocatpath" in err


def test_main_silent_when_clean(monkeypatch, tmp_path, capsys):
    lock = tmp_path / "uv.lock"
    lock.write_text(LOCK, encoding="utf-8")
    monkeypatch.setattr(mod, "LOCK_PATH", lock)
    monkeypatch.setattr(mod, "installed_state", lambda name: None)
    assert mod.main() == 0
    assert capsys.readouterr().err == ""


def test_scripts_test_runs_the_guard_before_pytest():
    # The guard must run before pytest wherever the chain physically
    # lives — scripts/test today, a helper it delegates to tomorrow
    # (gr515703: a move of the literal && chain to pytest-inner.sh
    # turned this pin red on a harmless reformat). Check the content of
    # every candidate runner, not one file's exact string.
    runners = [REPO / "scripts" / "test", REPO / "scripts" / "pytest-inner.sh"]
    texts = [p.read_text(encoding="utf-8") for p in runners if p.exists()]
    carriers = [t for t in texts if "check-lock-pins.py" in t]
    assert carriers, (
        "check-lock-pins.py runs in neither scripts/test nor pytest-inner.sh"
    )
    for text in carriers:
        assert "check-core-deps.py" in text
        assert text.index("check-core-deps.py") < text.index("check-lock-pins.py")
        assert "pytest" in text[text.index("check-lock-pins.py") :]
    assert any("PRECIS_STRICT_LOCK_CHECK" in t for t in texts)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX script")
def test_real_lock_parses():
    assert "autocatpath" in mod.locked_pins(
        (REPO / "uv.lock").read_text(encoding="utf-8")
    )
