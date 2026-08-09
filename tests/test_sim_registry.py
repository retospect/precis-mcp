"""Tests for ``precis.sim.registry`` + the ``precis sim list`` CLI verb.

Covers AC #2 of ``docs/backlog/sim-harness.md``: ``precis sim list``
reads the registry and prints registered sims with resolved paths and
their linked quest id; an unreachable path is reported, not a crash.
No DB needed — the registry + ``list`` are pure filesystem/YAML.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from precis.cli.main import _build_parser
from precis.cli.sim import run as sim_run
from precis.sim.registry import SimEntry, load_registry, registry_path


def _write_registry(tmp_path: Path, data: dict[str, object]) -> Path:
    p = tmp_path / "sims.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


# ── registry_path ────────────────────────────────────────────────────


def test_registry_path_override_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_SIMS_REGISTRY", str(tmp_path / "env.yaml"))
    resolved = registry_path(override=str(tmp_path / "override.yaml"))
    assert resolved == tmp_path / "override.yaml"


def test_registry_path_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_SIMS_REGISTRY", str(tmp_path / "env.yaml"))
    resolved = registry_path()
    assert resolved == tmp_path / "env.yaml"


def test_registry_path_default_under_precis_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRECIS_SIMS_REGISTRY", raising=False)
    monkeypatch.setenv("PRECIS_ROOT", str(tmp_path))
    resolved = registry_path()
    assert resolved == tmp_path.resolve() / "sims.yaml"


# ── load_registry ────────────────────────────────────────────────────


def test_load_registry_valid(tmp_path: Path) -> None:
    sim_dir = tmp_path / "lighterthanair"
    sim_dir.mkdir()
    path = _write_registry(
        tmp_path,
        {
            "lighterthanair": {
                "path": str(sim_dir),
                "git_remote": "git@github.com:example/lighterthanair.git",
                "manifest": "precis.sim.yaml",
                "quest": "lighterthanair-materials",
            }
        },
    )
    entries = load_registry(path)
    assert entries == {
        "lighterthanair": SimEntry(
            slug="lighterthanair",
            path=sim_dir,
            git_remote="git@github.com:example/lighterthanair.git",
            manifest=Path("precis.sim.yaml"),
            quest="lighterthanair-materials",
        )
    }


def test_load_registry_coerces_numeric_quest_id(tmp_path: Path) -> None:
    """A numeric quest id (the natural unquoted YAML form) coerces to str."""
    sim_dir = tmp_path / "lighterthanair"
    sim_dir.mkdir()
    path = _write_registry(
        tmp_path, {"lighterthanair": {"path": str(sim_dir), "quest": 175733}}
    )
    entries = load_registry(path)
    assert entries["lighterthanair"].quest == "175733"


def test_load_registry_defaults_manifest_name(tmp_path: Path) -> None:
    sim_dir = tmp_path / "flowsim"
    sim_dir.mkdir()
    path = _write_registry(tmp_path, {"flowsim": {"path": str(sim_dir)}})
    entries = load_registry(path)
    assert entries["flowsim"].manifest == Path("precis.sim.yaml")
    assert entries["flowsim"].quest is None
    assert entries["flowsim"].git_remote is None


def test_load_registry_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        load_registry(tmp_path / "nonexistent.yaml")


def test_load_registry_non_mapping_raises(tmp_path: Path) -> None:
    p = tmp_path / "sims.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        load_registry(p)


def test_load_registry_missing_path_key_raises(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, {"broken": {"quest": "q"}})
    with pytest.raises(ValueError, match="'path'"):
        load_registry(path)


def test_load_registry_unreachable_path_does_not_raise(tmp_path: Path) -> None:
    path = _write_registry(
        tmp_path, {"ghost": {"path": str(tmp_path / "no-such-checkout")}}
    )
    entries = load_registry(path)
    assert entries["ghost"].path.is_dir() is False


def test_resolved_manifest_path_relative(tmp_path: Path) -> None:
    entry = SimEntry(
        slug="s",
        path=tmp_path,
        git_remote=None,
        manifest=Path("precis.sim.yaml"),
        quest=None,
    )
    assert entry.resolved_manifest_path() == tmp_path / "precis.sim.yaml"


def test_resolved_manifest_path_absolute(tmp_path: Path) -> None:
    abs_manifest = tmp_path / "elsewhere" / "manifest.yaml"
    entry = SimEntry(
        slug="s", path=tmp_path, git_remote=None, manifest=abs_manifest, quest=None
    )
    assert entry.resolved_manifest_path() == abs_manifest


# ── CLI parser wiring ────────────────────────────────────────────────


def test_sim_parser_registered() -> None:
    parser = _build_parser()
    args = parser.parse_args(["sim", "list"])
    assert args.cmd == "sim"
    assert args.sim_cmd == "list"


def test_sim_ingest_parser_registered() -> None:
    parser = _build_parser()
    args = parser.parse_args(["sim", "ingest", "lighterthanair", "--force"])
    assert args.sim_cmd == "ingest"
    assert args.slug == "lighterthanair"
    assert args.force is True


def test_sim_verify_parser_registered() -> None:
    parser = _build_parser()
    args = parser.parse_args(["sim", "verify", "lighterthanair", "--dry-run"])
    assert args.sim_cmd == "verify"
    assert args.dry_run is True


# ── `precis sim list` output ─────────────────────────────────────────


def test_sim_list_prints_reachable_and_unreachable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reachable = tmp_path / "lighterthanair"
    reachable.mkdir()
    registry = _write_registry(
        tmp_path,
        {
            "lighterthanair": {"path": str(reachable), "quest": "lta-quest"},
            "flowsim": {"path": str(tmp_path / "missing-checkout")},
        },
    )
    parser = _build_parser()
    args = parser.parse_args(["sim", "list", "--registry", str(registry)])
    sim_run(args)
    out = capsys.readouterr().out
    assert "lighterthanair" in out
    assert "lta-quest" in out
    assert str(reachable) in out
    assert "flowsim" in out
    assert "UNREACHABLE" in out


def test_sim_list_empty_registry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = _write_registry(tmp_path, {})
    parser = _build_parser()
    args = parser.parse_args(["sim", "list", "--registry", str(registry)])
    sim_run(args)
    out = capsys.readouterr().out
    assert "no sims registered" in out


def test_sim_list_missing_registry_exits(tmp_path: Path) -> None:
    parser = _build_parser()
    args = parser.parse_args(
        ["sim", "list", "--registry", str(tmp_path / "nonexistent.yaml")]
    )
    with pytest.raises(SystemExit) as excinfo:
        sim_run(args)
    assert excinfo.value.code == 2
