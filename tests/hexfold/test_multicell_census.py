"""The multi-cell prototype census fixtures and their closed forms.

Slice 0 of docs/backlog/hexfold-multicell-prism.md: the chat-99 targets
(prism-hcol-forced, prism-hfold-out, prism-hcol, tri-tube, bundle3,
bundle6) at (R, P) = (2, 4) and (4, 6), counted from the prototype
``build.py`` at ``periodic=True`` (``scripts/hexfold-multicell-census``
regenerates the fixture).  Every entry satisfies the closed forms

    C = 2*R*P*walls + P*k3_seams
    H = 2*P*hydrogenated_folds

which slices 3 and 4's builders are scored against.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_CENSUS = Path(__file__).parent / "data" / "multicell" / "census.json"

#: the spec's recorded reference census (slice 0, §Slice 0 item 2).
_REFERENCE = {
    "prism-hcol-forced/R4P6": {"C": 288, "H": 72},
    "prism-hfold-out/R4P6": {"C": 288, "H": 72},
    "prism-hcol/R4P6": {"C": 576, "H": 36, "walls": 12, "hydrogenated_folds": 3},
    "tri-tube/R4P6": {"C": 744, "H": 0, "walls": 15, "k3_seams": 4},
    "bundle3/R4P6": {
        "C": 1188,
        "H": 36,
        "walls": 24,
        "k3_seams": 6,
        "folds": 15,
    },
    "bundle6/R4P6": {"C": 2088, "H": 72, "walls": 42, "k3_seams": 12},
    "prism-hcol-forced/R2P4": {"C": 96, "H": 48},
    "prism-hfold-out/R2P4": {"C": 96, "H": 48},
    "prism-hcol/R2P4": {"C": 192, "H": 24},
    "tri-tube/R2P4": {"C": 256},
    "bundle3/R2P4": {"C": 408, "H": 24},
    "bundle6/R2P4": {"C": 720, "H": 48},
}


def _entries() -> dict[str, dict]:
    data = json.loads(_CENSUS.read_text(encoding="utf-8"))
    return data["entries"]


@pytest.mark.parametrize("key", sorted(_REFERENCE))
def test_census_matches_spec_reference(key: str) -> None:
    """Each fixture carries the census the spec recorded at approval."""
    entry = _entries()[key]
    for field, want in _REFERENCE[key].items():
        assert entry[field] == want, f"{key}: {field} {entry[field]} != {want}"


def test_census_covers_targets_and_sizes() -> None:
    """Six targets at (2, 4) and (4, 6), with every census field."""
    entries = _entries()
    for name in (
        "prism-hcol-forced",
        "prism-hfold-out",
        "prism-hcol",
        "tri-tube",
        "bundle3",
        "bundle6",
    ):
        for r, p in ((2, 4), (4, 6)):
            entry = entries[f"{name}/R{r}P{p}"]
            for field in (
                "C",
                "H",
                "walls",
                "k3_seams",
                "folds",
                "hydrogenated_folds",
                "roles",
                "degree_hist",
                "rings",
            ):
                assert field in entry, f"{name}/R{r}P{p}: missing {field}"


@pytest.mark.parametrize("key", sorted(_entries()))
def test_census_closed_forms(key: str) -> None:
    """C = 2*R*P*walls + P*k3; H = 2*P*hydrogenated folds."""
    entry = _entries()[key]
    r, p = (int(x) for x in key.rsplit("/R", 1)[1].split("P"))
    assert entry["C"] == 2 * r * p * entry["walls"] + p * entry["k3_seams"]
    assert entry["H"] == 2 * p * entry["hydrogenated_folds"]
