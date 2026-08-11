"""Load the precis-side sim registry.

Slice 1 of ``docs/backlog/sim-harness.md`` (In-scope item 2, AC #2). The
registry is how precis learns the *set* of sims it drives and the
sim↔quest link (AC #6) — a small YAML data file mapping::

    <slug>:
      path: /abs/or/~-path/to/checkout
      git_remote: git@github.com:you/lighterthanair.git   # optional
      manifest: precis.sim.yaml                            # optional, this default
      quest: lighterthanair-materials                      # optional

Per the proposal's decided repo topology, sims stay independent repos;
the registry only *points at* a local checkout — it does not own or
migrate the sim's code (rejected: submodules/monorepo).

**Registry path — DECIDED default: ``$PRECIS_ROOT/sims.yaml``.**
``PRECIS_ROOT`` is already the per-deploy prose workspace every sim's
ingested findings land under (``sim/<slug>/`` — see
:mod:`precis.sim.ingest`), so co-locating the registry there keeps one
root per deploy instead of introducing a second config-dir convention.
Resolution order: ``--registry`` CLI flag > ``PRECIS_SIMS_REGISTRY`` env
var > ``$PRECIS_ROOT/sims.yaml``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_MANIFEST_NAME = "precis.sim.yaml"


@dataclass(frozen=True, slots=True)
class SimEntry:
    """One registry entry — a sim's location + sim↔quest link."""

    slug: str
    path: Path
    git_remote: str | None
    manifest: Path
    """Manifest filename/path. Relative paths resolve against ``path``."""
    quest: str | None
    """Quest id/slug this sim's ``verify`` runs report a deed to."""

    def resolved_manifest_path(self) -> Path:
        """``manifest`` resolved against ``path`` if it isn't already absolute."""
        return (
            self.manifest if self.manifest.is_absolute() else self.path / self.manifest
        )


def registry_path(*, override: str | None = None, cfg: Any = None) -> Path:
    """Resolve the registry file location.

    Order: *override* (CLI ``--registry``) > ``PRECIS_SIMS_REGISTRY`` env
    var > ``$PRECIS_ROOT/sims.yaml``. Raises :class:`ValueError` if none
    of those resolve (no override/env, and ``PRECIS_ROOT`` unset).
    """
    if override:
        return Path(override).expanduser()
    env = os.environ.get("PRECIS_SIMS_REGISTRY")
    if env:
        return Path(env).expanduser()
    if cfg is None:
        from precis.config import load_config

        cfg = load_config()
    root = getattr(cfg, "root", None)
    if not root:
        raise ValueError(
            "no sim registry location: pass --registry, set "
            "PRECIS_SIMS_REGISTRY, or set PRECIS_ROOT (default is "
            "$PRECIS_ROOT/sims.yaml)"
        )
    return Path(root).expanduser().resolve() / "sims.yaml"


def load_registry(path: Path) -> dict[str, SimEntry]:
    """Load + validate the registry at *path*.

    Raises :class:`FileNotFoundError` if *path* doesn't exist, or
    :class:`ValueError` if the YAML isn't a mapping or an entry is
    missing its required ``path`` key. An entry's *filesystem* path
    being unreachable is **not** an error here — callers (``precis sim
    list``) report that per-entry instead of crashing (AC #2).
    """
    if not path.is_file():
        raise FileNotFoundError(f"sim registry not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{path}: registry must be a YAML mapping of slug -> entry, "
            f"got {type(raw).__name__}"
        )

    entries: dict[str, SimEntry] = {}
    errors: list[str] = []
    for slug, cfg_ in raw.items():
        if not isinstance(cfg_, dict):
            errors.append(f"{slug!r}: entry must be a mapping")
            continue
        sim_path = cfg_.get("path")
        if not isinstance(sim_path, str) or not sim_path.strip():
            errors.append(f"{slug!r}: 'path' must be a non-empty string")
            continue
        manifest = cfg_.get("manifest", _DEFAULT_MANIFEST_NAME)
        if not isinstance(manifest, str) or not manifest.strip():
            errors.append(f"{slug!r}: 'manifest' must be a non-empty string")
            continue
        git_remote = cfg_.get("git_remote")
        if git_remote is not None and not isinstance(git_remote, str):
            errors.append(f"{slug!r}: 'git_remote' must be a string")
            continue
        quest = cfg_.get("quest")
        # A numeric quest id is the natural unquoted form in YAML — coerce it
        # to str (SimEntry.quest is str | None; verify re-parses digit strings
        # back to an int for the numeric-kind lookup).
        if isinstance(quest, int) and not isinstance(quest, bool):
            quest = str(quest)
        elif quest is not None and not isinstance(quest, str):
            errors.append(f"{slug!r}: 'quest' must be a string or integer id")
            continue
        entries[slug] = SimEntry(
            slug=slug,
            path=Path(sim_path).expanduser(),
            git_remote=git_remote,
            manifest=Path(manifest),
            quest=quest,
        )

    if errors:
        bullets = "\n".join(f"  - {e}" for e in errors)
        raise ValueError(f"{path}: invalid sim registry:\n{bullets}")

    return entries


__all__ = ["SimEntry", "load_registry", "registry_path"]
