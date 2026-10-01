"""A generator's output cannot change without its version changing.

The store decides no-op vs. re-expand on ``(generator, version, params)``
and never compares the emitted rows, so an output change shipped without a
:data:`precis.pcb.generators.VERSIONS` bump leaves every board authored at
the old version serving stale copper (pb345846 kept 54 removed B.Cu tracks,
docs/backlog/pcb-generator-version-is-a-manual-bump-with-no-tripwire.md).
This file pins a digest of each registered generator's expansion to its
version. When it fails: bump the generator's entry in ``VERSIONS`` and
replace the pinned ``(version, digest)`` with the pair the failure prints.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from precis.pcb import generators

#: Fixed inputs per generator: a bare field, and one that emits a sink part
#: (version 3 changed only the sink instances, so a sink-free case would not
#: have seen it).
_CASES: dict[str, list[dict[str, Any]]] = {
    "ewod_pad_array": [
        {"grid": [3, 3]},
        {
            "grid": [4, 4],
            "sink_grid": {
                "part": "C0000",
                "channels_per_sink": 16,
                "channel_pins": [f"HVOUT{i}" for i in range(1, 17)],
                "power": {"VDD": "VDD_LOGIC", "GND": "GND"},
            },
        },
    ],
}

#: generator -> (version, digest over every case's expansion). Re-pin both
#: together, never the digest alone.
_PINNED: dict[str, tuple[int, str]] = {
    "ewod_pad_array": (3, "889788bc99ef8d1b"),
}


def _rounded(value: Any) -> Any:
    # Floats rounded so a last-bit libm difference between macOS and the
    # Linux gate is not an "output change"; 1 nm is far below any fab grid.
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {str(k): _rounded(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_rounded(v) for v in value]
    return value


def _digest(generator: str) -> str:
    h = hashlib.sha256()
    for params in _CASES[generator]:
        exp = generators.expand(generator, "G1", params)
        emitted = {
            "components": exp.components,
            "nets": exp.nets,
            "connections": exp.connections,
            "footprints": exp.footprints,
            "features": exp.features,
            "copper": exp.copper,
            "net_classes": exp.net_classes,
            "canonical_params": exp.canonical_params,
        }
        h.update(json.dumps(_rounded(emitted), sort_keys=True).encode("utf-8"))
    return h.hexdigest()[:16]


def test_every_registered_generator_is_pinned():
    assert set(generators._REGISTRY) == set(_PINNED) == set(_CASES)
    assert set(generators.VERSIONS) == set(generators._REGISTRY)


@pytest.mark.parametrize("generator", sorted(_PINNED))
def test_output_changes_only_with_a_version_bump(generator):
    version, digest = _PINNED[generator]
    actual = _digest(generator)
    current = generators.VERSIONS[generator]
    assert (current, actual) == (version, digest), (
        f"{generator}: expansion output or version changed — pinned "
        f"({version}, {digest!r}), now ({current}, {actual!r}). If the output "
        f"changed, bump VERSIONS[{generator!r}] and re-pin both values."
    )


def test_expansion_carries_the_versions_table_entry():
    for generator, cases in _CASES.items():
        exp = generators.expand(generator, "G1", cases[0])
        assert exp.version == generators.VERSIONS[generator]


def test_stale_generators_lists_only_rows_behind_the_code():
    current = generators.VERSIONS["ewod_pad_array"]
    rows = {
        "OLD": {"generator": "ewod_pad_array", "version": current - 1},
        "NOW": {"generator": "ewod_pad_array", "version": current},
        "GONE": {"generator": "retired_generator", "version": 1},
    }
    assert generators.stale_generators(rows) == [
        ("OLD", "ewod_pad_array", current - 1, current)
    ]
    note = generators.stale_generator_note(generators.stale_generators(rows))
    assert "OLD (ewod_pad_array)" in note
    assert "put the same generators entry again" in note
    assert generators.stale_generator_note([]) == ""
