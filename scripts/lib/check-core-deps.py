"""scripts/lib/check-core-deps.py — preflight: every core dep is importable.

`scripts/test` runs `uv run --no-sync` against the venv BAKED into the
precis-dev image — fast (no per-run resolve), but stale the moment a commit
promotes a new `[project] dependencies` entry (e.g. shapely, 2e8940b9) until
the image is rebuilt. Without a guard, that surfaces as ModuleNotFoundError
red tests in whatever file pytest collects first — unrelated to the change
under test, and expensive to root-cause from (gr286507: three agents burned
time reasoning from that broken baseline before recognising a stale image).

This script is the guard: parse `pyproject.toml`'s `[project] dependencies`,
try to import each one (skipping entries whose environment marker doesn't
apply here), and fail loud with the missing module(s) + the fix, before
pytest ever starts. Run under the SAME `uv run --no-sync ${UV_WITH}` prefix
as the pytest invocation (see scripts/test) so a UV_WITH bridge that covers
a gap is honoured here too — a dep added via `--with` is present at run time
even though the baked venv lacks it.

Distribution name -> import name only needs an explicit entry here when it
differs from the `name.lower().replace("-", "_")` default guess; keep this
mapping to exactly what this project's `dependencies` list actually needs
(checked against pyproject.toml — do not pre-populate speculative entries).
"""

from __future__ import annotations

import importlib
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

# Only the distributions whose import name differs from the
# name.lower().replace("-", "_") default guess.
IMPORT_NAME_OVERRIDES = {
    "pyyaml": "yaml",
    "python-docx": "docx",
    "python-epo-ops-client": "epo_ops",
}

PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _import_name(requirement_name: str) -> str:
    key = requirement_name.lower()
    return IMPORT_NAME_OVERRIDES.get(key, key.replace("-", "_"))


def main() -> int:
    data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]

    missing: list[str] = []
    for raw in deps:
        req = Requirement(raw)
        # Environment-marker-gated deps (e.g. `sys_platform != 'win32'`) that
        # don't apply in THIS container are meant to be absent — skip them.
        if req.marker is not None and not req.marker.evaluate():
            continue
        mod = _import_name(req.name)
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(f"{req.name} (import {mod})")

    if missing:
        with_flags = " ".join(f"--with {m.split()[0]}" for m in missing)
        print(
            "stale precis-dev image — missing core dependency import(s): "
            + ", ".join(missing)
            + "\n"
            "fix: run scripts/build-image precis-dev "
            f'(or bridge with UV_WITH="{with_flags}")',
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
