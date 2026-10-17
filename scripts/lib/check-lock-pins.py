"""scripts/lib/check-lock-pins.py — preflight: installed packages match uv.lock.

`scripts/test` runs `uv run --no-sync` against the venv BAKED into the
precis-dev image. check-core-deps.py catches a dependency that is missing
outright; this guard catches the quieter sibling: a package that is installed
but at a different version (or git commit) than `uv.lock` pins. The image was
built before the lock moved, so tests whose results depend on the package
(autocatpath's engine-versioned coverage anchor key, gr494988) go red with no
hint that the environment, not the change, is at fault.

For every `[[package]]` in uv.lock that is installed here, compare the version
(local `+cpu`-style suffixes ignored) and, for git sources, the commit recorded
in the dist's direct_url.json. A name the lock pins at several versions (marker
forks) passes if the installed one matches any. Not installed -> skipped (extras
the image does not bake; check-core-deps covers the required ones). Editable and
virtual sources (this project itself) are skipped.

Warns loudly, before pytest starts, naming each stale package and the rebuild
command; the run continues (a rebuild can be held up by disk). Exits 1 only
under `PRECIS_STRICT_LOCK_CHECK=1`.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

LOCK_PATH = Path(__file__).resolve().parents[2] / "uv.lock"
REBUILD = "scripts/build-image precis-dev"

State = tuple[str, str | None]


def _public(version: str) -> str:
    try:
        return Version(version).public
    except InvalidVersion:
        return version


def _edges(pkg: dict) -> list[str]:
    deps = list(pkg.get("dependencies", []))
    for group in pkg.get("optional-dependencies", {}).values():
        deps.extend(group)
    return [canonicalize_name(d["name"]) for d in deps]


def _runtime_names(packages: list[dict]) -> set[str] | None:
    """Names reachable from the project's dependencies and extras.

    Dev-group tooling (ruff, mypy, pytest) is excluded: the image's copies
    drift for reasons that cannot change a test's result. None (check all)
    when the lock has no editable root.
    """
    by_name: dict[str, dict] = {canonicalize_name(p["name"]): p for p in packages}
    roots = [p for p in packages if "editable" in p.get("source", {})]
    if not roots:
        return None
    seen: set[str] = set()
    todo = [n for root in roots for n in _edges(root)]
    while todo:
        name = todo.pop()
        if name in seen or name not in by_name:
            continue
        seen.add(name)
        todo.extend(_edges(by_name[name]))
    return seen


def locked_pins(lock_text: str) -> dict[str, list[State]]:
    """name -> [(version, git commit or None)] for every checkable package."""
    pins: dict[str, list[State]] = {}
    packages = tomllib.loads(lock_text).get("package", [])
    runtime = _runtime_names(packages)
    for pkg in packages:
        source = pkg.get("source", {})
        if "version" not in pkg or "editable" in source or "virtual" in source:
            continue
        if runtime is not None and canonicalize_name(pkg["name"]) not in runtime:
            continue
        commit = None
        if "git" in source:
            commit = source["git"].rpartition("#")[2] or None
        pins.setdefault(canonicalize_name(pkg["name"]), []).append(
            (_public(pkg["version"]), commit)
        )
    return pins


def installed_state(name: str) -> State | None:
    """(public version, git commit or None) of an installed dist, else None."""
    try:
        distribution = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return None
    commit = None
    raw = distribution.read_text("direct_url.json")
    if raw:
        commit = json.loads(raw).get("vcs_info", {}).get("commit_id")
    return _public(distribution.version), commit


def stale_packages(
    lock_text: str, installed: Callable[[str], State | None] | None = None
) -> list[str]:
    installed = installed or installed_state
    stale = []
    for name, wanted in sorted(locked_pins(lock_text).items()):
        state = installed(name)
        if state is None:
            continue
        version, commit = _public(state[0]), state[1]
        if any(version == v and (c is None or commit == c) for v, c in wanted):
            continue
        want = " | ".join(v for v, _ in wanted)
        stale.append(f"{name} (image {version}, uv.lock {want})")
    return stale


def main() -> int:
    stale = stale_packages(LOCK_PATH.read_text(encoding="utf-8"))
    if not stale:
        return 0
    strict = os.environ.get("PRECIS_STRICT_LOCK_CHECK") == "1"
    print(
        ("ERROR" if strict else "WARNING")
        + ": stale precis-dev image — installed package(s) differ from uv.lock: "
        + ", ".join(stale)
        + f"\nfix: run {REBUILD}; tests that depend on these packages may fail"
        + ("" if strict else " (PRECIS_STRICT_LOCK_CHECK=1 makes this fatal)"),
        file=sys.stderr,
    )
    return 1 if strict else 0


if __name__ == "__main__":
    sys.exit(main())
