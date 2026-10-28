"""scripts/lib/check-dev-deps.py — preflight: every dev-group dist is installed.

`scripts/test` runs `uv run --no-sync` against the venv BAKED into the
precis-dev image, so a dependency added to `[dependency-groups] dev` after
the bake is invisible in the container (gr519535: ansible-core landed on
worktree-bugs-ab for gr519391 — its tests passed on the host but
quiet-skipped in the gate, because a dev-group gap surfaces as
`importorskip` skips, not red tests). check-core-deps.py guards
`[project] dependencies` only; this is the dev-group sibling.

The comparison reads uv.lock's root `[package.dev-dependencies].dev` —
the resolved list the image bakes (pyproject's `[dependency-groups]` is
the fallback when the lock has no dev table) — and asks
`importlib.metadata` whether each distribution is INSTALLED. Presence, not
import: half the dev group (types-*, *-stubs, ViennaRNA) ships no
importable module under its dist name, and a metadata lookup costs a
site-packages listing instead of a real import — the whole group checks in
well under a second.

Run under the SAME `uv run --no-sync ${UV_WITH}` prefix as the pytest
invocation (pytest-inner.sh chains it), so a UV_WITH/`--with` bridge is
honoured: uv's overlay env puts bridged dists on sys.path where
importlib.metadata sees them. `--with-flags` is the query mode behind
scripts/test's autobridge pre-pass (gr513760; default-on since gr525770,
`PRECIS_TEST_AUTOBRIDGE=0` opts out): it prints the UV_WITH flags that
would cover the gap, nothing else.
"""

from __future__ import annotations

import sys
import tomllib
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

LOCK_PATH = Path(__file__).resolve().parents[2] / "uv.lock"
PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"


def dev_group_specs(data: dict, lock_text: str | None) -> list[str]:
    """Requirement spec strings for the dev group.

    uv.lock's editable/virtual root carries the resolved
    `[package.dev-dependencies]` table — the set the image actually bakes.
    When the lock has no root or no table (a lock predating dev groups),
    fall back to pyproject's `[dependency-groups] dev` so the guard still
    applies; include-group dicts there are skipped (uv writes one `dev`
    group only today).
    """
    if lock_text is not None:
        packages = tomllib.loads(lock_text).get("package", [])
        roots = [
            p
            for p in packages
            if "editable" in p.get("source", {}) or "virtual" in p.get("source", {})
        ]
        # A workspace lock can carry more than one editable root — the dev
        # group belongs to THIS project, so pick the root by name.
        project = canonicalize_name(data.get("project", {}).get("name", ""))
        root = next(
            (p for p in roots if canonicalize_name(p.get("name", "")) == project),
            roots[0] if roots else None,
        )
        if root and "dev-dependencies" in root:
            specs = []
            for dep in root["dev-dependencies"].get("dev", []):
                spec = dep["name"]
                if dep.get("marker"):
                    spec = f"{spec}; {dep['marker']}"
                specs.append(spec)
            return specs
    return [
        d
        for d in data.get("dependency-groups", {}).get("dev", [])
        if isinstance(d, str)
    ]


def _installed(name: str) -> bool:
    try:
        metadata.version(name)
    except metadata.PackageNotFoundError:
        return False
    return True


def missing_dev_deps(
    data: dict,
    lock_text: str | None,
    installed: Callable[[str], bool] | None = None,
) -> list[str]:
    """Dev-group distribution names not installed here.

    Environment-marker-gated entries that don't apply in THIS container are
    meant to be absent — skipped, the same rule check-core-deps.py applies.
    """
    installed = installed or _installed
    missing = []
    for raw in dev_group_specs(data, lock_text):
        req = Requirement(raw)
        if req.marker is not None and not req.marker.evaluate():
            continue
        if not installed(canonicalize_name(req.name)):
            missing.append(req.name)
    return missing


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    lock_text = LOCK_PATH.read_text(encoding="utf-8") if LOCK_PATH.exists() else None
    missing = missing_dev_deps(data, lock_text)

    if "--with-flags" in argv:
        # Query mode for scripts/test's PRECIS_TEST_AUTOBRIDGE pre-pass:
        # stdout must be exactly the uv flags — capture-safe, nothing else.
        print(" ".join(f"--with {m}" for m in missing))
        return 0 if not missing else 1

    if missing:
        with_flags = " ".join(f"--with {m}" for m in missing)
        print(
            "stale precis-dev image — missing dev-group package(s): "
            + ", ".join(missing)
            + "\ntheir tests quiet-skip via importorskip instead of failing "
            "red — the gap is invisible until you look for it\n"
            f'bridge this run only:  UV_WITH="{with_flags}" scripts/test …\n'
            "bridge automatically:  PRECIS_TEST_AUTOBRIDGE=1 scripts/test …\n"
            "durable fix:           run scripts/build-image precis-dev",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
