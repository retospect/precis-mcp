"""Tests for the ``precis.sim.manifest`` loader/validator.

Covers AC #1 of ``docs/proposals/sim-harness.md``: the loader parses a
valid ``precis.sim.yaml`` and raises a clear error for each
missing/ill-typed required key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from precis.sim.manifest import SimManifest, load_manifest


def _write_manifest(tmp_path: Path, data: object) -> Path:
    p = tmp_path / "precis.sim.yaml"
    if isinstance(data, str):
        p.write_text(data, encoding="utf-8")
    else:
        p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_load_valid_manifest(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        {
            "run": "python run_pareto.py",
            "outputs": ["docs/findings.md", "out/pareto.csv"],
            "verify": ["materials.yaml"],
            "writeup": "lighterthanair-writeup",
        },
    )
    manifest = load_manifest(path)
    assert manifest == SimManifest(
        run="python run_pareto.py",
        outputs=("docs/findings.md", "out/pareto.csv"),
        verify=("materials.yaml",),
        writeup="lighterthanair-writeup",
    )


def test_load_valid_manifest_allows_empty_verify(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        {
            "run": "python run.py",
            "outputs": ["findings.md"],
            "verify": [],
            "writeup": "writeup-slug",
        },
    )
    manifest = load_manifest(path)
    assert manifest.verify == ()


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        load_manifest(tmp_path / "nonexistent.sim.yaml")


def test_non_mapping_yaml_raises(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        load_manifest(path)


def test_empty_file_raises(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, "")
    with pytest.raises(ValueError, match="'run'"):
        load_manifest(path)


@pytest.mark.parametrize(
    ("data", "expected_key"),
    [
        (
            {
                "outputs": ["findings.md"],
                "verify": [],
                "writeup": "w",
            },
            "run",
        ),
        (
            {
                "run": "x",
                "verify": [],
                "writeup": "w",
            },
            "outputs",
        ),
        (
            {
                "run": "x",
                "outputs": ["findings.md"],
                "writeup": "w",
            },
            "verify",
        ),
        (
            {
                "run": "x",
                "outputs": ["findings.md"],
                "verify": [],
            },
            "writeup",
        ),
    ],
)
def test_missing_required_key_raises(
    tmp_path: Path, data: dict[str, object], expected_key: str
) -> None:
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ValueError, match=expected_key):
        load_manifest(path)


def test_run_wrong_type_raises(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        {"run": 123, "outputs": ["findings.md"], "verify": [], "writeup": "w"},
    )
    with pytest.raises(ValueError, match="'run'"):
        load_manifest(path)


def test_run_blank_string_raises(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        {"run": "   ", "outputs": ["findings.md"], "verify": [], "writeup": "w"},
    )
    with pytest.raises(ValueError, match="'run'"):
        load_manifest(path)


def test_outputs_wrong_type_raises(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        {"run": "x", "outputs": "findings.md", "verify": [], "writeup": "w"},
    )
    with pytest.raises(ValueError, match="'outputs'"):
        load_manifest(path)


def test_outputs_empty_list_raises(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        {"run": "x", "outputs": [], "verify": [], "writeup": "w"},
    )
    with pytest.raises(ValueError, match="'outputs'"):
        load_manifest(path)


def test_outputs_non_string_elements_raises(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        {"run": "x", "outputs": ["findings.md", 42], "verify": [], "writeup": "w"},
    )
    with pytest.raises(ValueError, match="'outputs'"):
        load_manifest(path)


def test_verify_wrong_type_raises(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        {
            "run": "x",
            "outputs": ["findings.md"],
            "verify": "materials.yaml",
            "writeup": "w",
        },
    )
    with pytest.raises(ValueError, match="'verify'"):
        load_manifest(path)


def test_writeup_wrong_type_raises(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        {"run": "x", "outputs": ["findings.md"], "verify": [], "writeup": 7},
    )
    with pytest.raises(ValueError, match="'writeup'"):
        load_manifest(path)


def test_multiple_errors_all_reported(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, {"outputs": []})
    with pytest.raises(ValueError) as excinfo:
        load_manifest(path)
    msg = str(excinfo.value)
    assert "'run'" in msg
    assert "'outputs'" in msg
    assert "'verify'" in msg
    assert "'writeup'" in msg
