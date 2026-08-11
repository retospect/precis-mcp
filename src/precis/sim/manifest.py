"""Load + validate a sim's ``precis.sim.yaml`` manifest.

Slice 1 of ``docs/backlog/sim-harness.md`` (In-scope item 1, AC #1). The
manifest lives **in each sim repo** (not precis) — it is the sim's own
declaration of how it wants to be driven; the :mod:`precis.sim.registry`
only points at it. Schema, four required keys:

- ``run`` (str) — how to reproduce the sim (informational this slice;
  the drive path lands with the ``sandbox_run`` container slice).
- ``outputs`` (list[str]) — prose/CSV file paths or globs, relative to
  the sim repo root, that ``precis sim ingest`` projects into
  ``PRECIS_ROOT`` and chunks+embeds as ``markdown``/``plaintext`` refs.
  Binary plots (``.png``/``.vti``/``.vtu``) may be listed here too —
  ``precis sim ingest`` skips them (deferred to the ``folder``-harvest
  slice) rather than failing.
- ``verify`` (list[str]) — YAML file paths, relative to the sim repo
  root, that ``precis sim verify`` will scan for low-confidence entries.
  May be empty (a sim with no verifiable YAML yet).
- ``writeup`` (str) — a slug naming the ``draft`` the writeup slice
  (slice 2) will compose.

The loader fails closed: every missing or ill-typed required key is
collected into one clear :class:`ValueError` rather than raising on the
first problem found.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class SimManifest:
    """A validated ``precis.sim.yaml``."""

    run: str
    outputs: tuple[str, ...]
    verify: tuple[str, ...]
    writeup: str


def load_manifest(path: Path) -> SimManifest:
    """Load and validate the manifest at *path*.

    Raises :class:`FileNotFoundError` if *path* doesn't exist, or
    :class:`ValueError` — listing every problem found, not just the
    first — if the YAML isn't a mapping or any required key is missing
    or the wrong type.
    """
    if not path.is_file():
        raise FileNotFoundError(f"sim manifest not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{path}: manifest must be a YAML mapping, got {type(raw).__name__}"
        )

    errors: list[str] = []

    run = raw.get("run")
    if not isinstance(run, str) or not run.strip():
        errors.append("'run' must be a non-empty string")

    outputs = _validate_str_list(raw, "outputs", errors, allow_empty=False)
    verify = _validate_str_list(raw, "verify", errors, allow_empty=True)

    writeup = raw.get("writeup")
    if not isinstance(writeup, str) or not writeup.strip():
        errors.append("'writeup' must be a non-empty string")

    if errors:
        bullets = "\n".join(f"  - {e}" for e in errors)
        raise ValueError(f"{path}: invalid sim manifest:\n{bullets}")

    assert isinstance(run, str)
    assert isinstance(writeup, str)
    return SimManifest(
        run=run,
        outputs=tuple(outputs or ()),
        verify=tuple(verify or ()),
        writeup=writeup,
    )


def _validate_str_list(
    raw: dict[str, Any], key: str, errors: list[str], *, allow_empty: bool
) -> list[str] | None:
    """Validate ``raw[key]`` is a list of strings; record a message on failure.

    Returns the list on success, ``None`` on failure (caller only reads
    the return value once ``errors`` is empty).
    """
    if key not in raw:
        errors.append(f"{key!r} is required")
        return None
    value = raw[key]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f"{key!r} must be a list of strings")
        return None
    if not allow_empty and not value:
        errors.append(f"{key!r} must be a non-empty list")
        return None
    return value


__all__ = ["SimManifest", "load_manifest"]
