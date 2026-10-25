"""``scripts/lib/*.sh`` — the sourced shell helpers behind scripts/*.

gr514484: ``scripts/host-ops/*`` and the hardening role's files were already
shellcheck-gated (``tests/test_host_ops.py``,
``tests/test_deploy_hardening_role.py``) but nothing linted the shared lib
they all source — which is where the lock/queue/overlay logic lives. This
walks the glob rather than a name list so a new lib joins the gate
automatically.

``shellcheck`` is a runner/dev-host binary, not a uv dep: absent locally the
test skips, absent under ``CI=true`` it FAILS (check.yml installs
``shellcheck-py`` for the pytest jobs — a missing one is a broken runner,
and a silent skip would let the lib rot unlinted again, the
``tests/_node.py`` rule).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only shell scripts"
)

_REPO = Path(__file__).resolve().parent.parent
_LIBS = sorted((_REPO / "scripts" / "lib").glob("*.sh"))


def _shellcheck() -> str:
    path = shutil.which("shellcheck")
    if path is not None:
        return path
    if os.environ.get("CI", "").lower() == "true":
        pytest.fail(
            "shellcheck is not on PATH under CI=true: check.yml must "
            "install it before pytest (gr514484)"
        )
    pytest.skip("shellcheck not installed")


def test_lib_glob_is_not_empty() -> None:
    """The glob must keep finding libs — an empty set would vacuously pass."""
    assert _LIBS


def test_shellcheck() -> None:
    shellcheck = _shellcheck()
    for lib in _LIBS:
        result = subprocess.run(
            # -x follows each lib's `# shellcheck source=scripts/lib/...`
            # (paths are repo-relative, hence cwd); without it SC1091 fails.
            [shellcheck, "-x", str(lib)],
            cwd=_REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, f"{lib.name}:\n{result.stdout}{result.stderr}"
