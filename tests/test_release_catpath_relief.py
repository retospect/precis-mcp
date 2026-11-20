"""`scripts/release` catpath wheel preflight relief (gr584832).

The fix-lane host can never hold a catpath checkout (the repo is private),
so scripts/deploy's wheel preflight (gr263082) killed every factory
release_deploy even with the fleet already seeded at the pinned version.
The relief lives in scripts/release (the deploy is launched there, through
`round deploy`): with no controller-side wheel source and every
autocatpath_plugin host's wheelhouse already holding the pin-matching
wheel, the deploy runs with PRECIS_DEPLOY_SKIP_CATPATH_WHEEL=1. These
tests fake the ansible probe binaries; no cluster is touched.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests._script import load_script

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: probe runs shell stubs"
)

RELEASE = Path(__file__).resolve().parents[1] / "scripts" / "release"

LOCK = """\
version = 1
requires-python = ">=3.12"

[[package]]
name = "ase"
version = "3.24.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "autocatpath"
version = "0.50.0"
source = { git = "https://github.com/retospect/catpath?rev=769e29a832195c74038f1e34fd818500c9179838#769e29a832195c74038f1e34fd818500c9179838" }

[[package]]
name = "zope-interface"
version = "7.0"
source = { registry = "https://pypi.org/simple" }
"""

INVENTORY = {
    "_meta": {
        "hostvars": {
            "node-gateway": {
                "precis_capabilities": {"autocatpath_plugin": ["node-gateway"]}
            },
            "node-gpu": {
                "precis_capabilities": {
                    "autocatpath_plugin": ["node-gateway", "node-gpu"]
                },
                "autocatpath_wheelhouse": "/opt/precis/wheels",
            },
            "node-data": {"precis_capabilities": {"autocatpath_plugin": []}},
        }
    }
}


def _load_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    copy = tmp_path / "release_script.py"
    copy.write_text(RELEASE.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "lib").mkdir(exist_ok=True)
    for name in ("ci_run.py", "git_network.py", "soak_verdict.py", "round_state.py"):
        (tmp_path / "lib" / name).write_text(
            (RELEASE.parent / "lib" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    return load_script("release_catpath_relief", copy)


@pytest.fixture
def mod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    m = _load_release(tmp_path, monkeypatch)
    root = tmp_path / "repo"
    (root / "deploy" / "inventory").mkdir(parents=True)
    (root / "uv.lock").write_text(LOCK, encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\ndependencies = []\n"
        "[project.optional-dependencies]\n"
        'catalyst = ["autocatpath>=0.50.0"]\n'
        'gpu = ["autocatpath[mace]>=0.32.0"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(m, "ROOT", root)
    # Isolate the conventional probe paths: a real ~/catpath checkout on
    # the test host (the deploy controller has one) would suppress the
    # relief the positive tests assert, and a git repo anywhere above
    # tmp_path would move the <main checkout>/../catpath probe.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    for var in (
        "PRECIS_CATPATH_DIR",
        "PRECIS_DEPLOY_SKIP_CATPATH_WHEEL",
        "PRECIS_DEPLOY_EXTRA_VARS",
        "PRECIS_OVERLAY_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    # Fake the ansible probe binaries; FAKE_UNSEEDED_HOSTS makes named hosts
    # answer "no pin-matching wheel in my wheelhouse".
    bindir = tmp_path / "bin"
    bindir.mkdir()
    inv_json = tmp_path / "inventory.json"
    inv_json.write_text(json.dumps(INVENTORY), encoding="utf-8")
    (bindir / "ansible-inventory").write_text(
        f'#!/usr/bin/env bash\ncat "{inv_json}"\n', encoding="utf-8"
    )
    (bindir / "ansible").write_text(
        "#!/usr/bin/env bash\n"
        'case ",$FAKE_UNSEEDED_HOSTS," in *",$1,"*) exit 1 ;; esac\n'
        "echo /opt/precis/wheels/autocatpath-0.50.0-py3-none-any.whl\n",
        encoding="utf-8",
    )
    for name in ("ansible", "ansible-inventory"):
        (bindir / name).chmod(0o755)
    monkeypatch.setenv("PRECIS_ANSIBLE_BIN_DIR", str(bindir))
    monkeypatch.delenv("FAKE_UNSEEDED_HOSTS", raising=False)
    return m


def test_lock_pin_reads_sha_and_version(mod: ModuleType) -> None:
    sha, version = mod._autocatpath_lock_pin(LOCK)
    assert sha == "769e29a832195c74038f1e34fd818500c9179838"
    assert version == "0.50.0"


def test_lock_pin_ignores_other_packages(mod: ModuleType) -> None:
    sha, version = mod._autocatpath_lock_pin(
        '[[package]]\nname = "ase"\nversion = "3.24.0"\n'
    )
    assert (sha, version) == ("", "")


def test_floor_reads_the_lowest_declared(mod: ModuleType) -> None:
    # The real pyproject declares the floor twice (catalyst's pure engine,
    # catalyst-gpu's [mace]); a wheel clearing ONE extra resolves, so the
    # lowest is the gate — same rule as the shell autocatpath_floor.
    assert mod._autocatpath_floor(mod.ROOT / "pyproject.toml") == "0.32.0"
    assert mod._autocatpath_floor(mod.ROOT / "missing.toml") is None


def test_version_ge_orders_release_numbers(mod: ModuleType) -> None:
    assert mod._version_ge("0.50.0", "0.32.0")
    assert mod._version_ge("0.50.0", "0.50.0")
    assert not mod._version_ge("0.31.0", "0.32.0")
    # sort -V orders a shorter prefix before a longer one.
    assert not mod._version_ge("0.50", "0.50.0")
    assert mod._version_ge("0.50.0", "0.50")
    # Anything unparseable fails closed.
    assert not mod._version_ge("0.50.", "0.32.0")
    assert not mod._version_ge("", "0.32.0")


def test_relief_skips_when_the_pin_is_below_the_pyproject_floor(
    mod: ModuleType,
) -> None:
    # gr499104's guard the bypass would skip: a lock below the floor must
    # die in preflight, not sail past it into a mid-run install failure.
    (mod.ROOT / "uv.lock").write_text(
        LOCK.replace('version = "0.50.0"', 'version = "0.31.0"'),
        encoding="utf-8",
    )
    assert mod.catpath_preflight_relief() is None


def test_relief_skips_without_a_pyproject_floor(
    mod: ModuleType,
) -> None:
    # No floor: the deploy skips the catpath block itself — nothing to
    # relieve.
    (mod.ROOT / "pyproject.toml").write_text(
        "[project]\ndependencies = []\n", encoding="utf-8"
    )
    assert mod.catpath_preflight_relief() is None


def test_relief_fires_when_every_plugin_host_is_seeded(mod: ModuleType) -> None:
    line = mod.catpath_preflight_relief()
    assert line is not None
    assert "PRECIS_DEPLOY_SKIP_CATPATH_WHEEL=1" in line
    assert "0.50.0" in line
    assert "node-gateway" in line and "node-gpu" in line
    # node-data carries no autocatpath_plugin capability — never probed.
    assert "node-data" not in line


def test_relief_fails_closed_when_a_host_lacks_the_wheel(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_UNSEEDED_HOSTS", "node-gpu")
    assert mod.catpath_preflight_relief() is None


def test_relief_fails_closed_when_a_host_is_unreachable(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_UNSEEDED_HOSTS", "node-gateway")
    assert mod.catpath_preflight_relief() is None


def test_relief_skips_when_no_autocatpath_pin(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    (mod.ROOT / "uv.lock").write_text(
        '[[package]]\nname = "ase"\nversion = "3.24.0"\n', encoding="utf-8"
    )
    assert mod.catpath_preflight_relief() is None


def test_relief_skips_when_a_checkout_exists(
    mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "catpath"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text(
        '[project]\nversion = "0.50.0"\n', encoding="utf-8"
    )
    monkeypatch.setenv("PRECIS_CATPATH_DIR", str(checkout))
    assert mod.catpath_preflight_relief() is None


def test_relief_skips_when_already_overridden(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_DEPLOY_SKIP_CATPATH_WHEEL", "1")
    assert mod.catpath_preflight_relief() is None
    monkeypatch.delenv("PRECIS_DEPLOY_SKIP_CATPATH_WHEEL")
    monkeypatch.setenv("PRECIS_DEPLOY_EXTRA_VARS", "autocatpath_wheel=/w.whl")
    assert mod.catpath_preflight_relief() is None


def test_relief_covers_a_fleet_with_no_plugin_hosts(
    mod: ModuleType, tmp_path: Path
) -> None:
    inv: dict[str, object] = {
        "_meta": {
            "hostvars": {
                "node-data": {"precis_capabilities": {"autocatpath_plugin": []}}
            }
        }
    }
    (tmp_path / "inventory.json").write_text(json.dumps(inv), encoding="utf-8")
    line = mod.catpath_preflight_relief()
    assert line is not None
    assert "no autocatpath_plugin hosts" in line


def test_relief_fails_closed_when_the_inventory_is_unreadable(
    mod: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stub = tmp_path / "bin" / "ansible-inventory"
    stub.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    stub.chmod(0o755)
    assert mod.catpath_preflight_relief() is None
    assert "could not be read" in capsys.readouterr().out


def test_relief_fails_closed_without_an_overlay(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.rmtree(mod.ROOT / "deploy" / "inventory")
    assert mod.catpath_preflight_relief() is None


def _wire_deploy_and_close(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, object]]
) -> None:
    """deploy_and_close's happy path (rc 0) with the round/prod edges
    stubbed; rnd's kwargs are captured for the env assertion."""
    monkeypatch.setattr(mod, "round_state", lambda: {})
    monkeypatch.setattr(mod, "_regate_if_stale", lambda args: 0)
    monkeypatch.setattr(mod, "_release_deploying", lambda state: None)
    monkeypatch.setattr(mod, "_release_by_state", lambda state, kinds: None)
    monkeypatch.setattr(mod, "release_label", lambda state: "r243")
    monkeypatch.setattr(mod, "rev", lambda name: "e" * 40)
    monkeypatch.setattr(mod, "warn_post_migration", lambda: None)
    monkeypatch.setattr(mod, "pending_sha", lambda rel, sha: "c" * 40)
    monkeypatch.setattr(mod, "record_prod_head", lambda prev, head, release="": "ok")
    monkeypatch.setattr(mod, "validate_gripes", lambda: None)
    monkeypatch.setattr(mod, "owns", lambda rel: False)
    monkeypatch.setattr(mod, "clear_owner", lambda: None)
    monkeypatch.setattr(mod, "live_releases", lambda state: [])

    def fake_rnd(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(mod, "rnd", fake_rnd)


def test_deploy_and_close_hands_the_relief_env_to_round_deploy(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    _wire_deploy_and_close(mod, monkeypatch, calls)
    monkeypatch.setattr(
        mod, "catpath_preflight_relief", lambda: "relief: seeded (gr584832)"
    )
    args = mod.argparse.Namespace(deploy_only="r243")
    assert mod.deploy_and_close(args, "c" * 40) == 0
    deploy = calls[0]
    assert deploy["args"] == ("deploy",)
    env = deploy["env"]
    assert isinstance(env, dict)
    assert env["PRECIS_DEPLOY_SKIP_CATPATH_WHEEL"] == "1"


def test_deploy_and_close_passes_no_env_without_relief(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    _wire_deploy_and_close(mod, monkeypatch, calls)
    monkeypatch.setattr(mod, "catpath_preflight_relief", lambda: None)
    args = mod.argparse.Namespace(deploy_only="r243")
    assert mod.deploy_and_close(args, "c" * 40) == 0
    assert calls[0]["env"] is None
