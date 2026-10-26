"""scripts/deploy must not run the project .venv's core-only ansible (r110).

``uv run --no-project`` prepends the cwd's ``.venv/bin`` to PATH; the dev
group's ansible-core has no collections, so ``community.general.homebrew``
failed to resolve. The PATH-sanitizing function is extracted from the script
and run in a ``bash -c`` harness.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: bash harness over scripts/deploy"
)

DEPLOY = Path(__file__).resolve().parent.parent / "scripts" / "deploy"


def _func() -> str:
    m = re.search(
        r"# BEGIN _deploy_sanitize_path.*?\n(.*?)# END _deploy_sanitize_path",
        DEPLOY.read_text(encoding="utf-8"),
        re.S,
    )
    assert m, "sanitize markers missing from scripts/deploy"
    return m.group(1)


def _run(path: str, extra_env: str = "") -> list[str]:
    script = (
        f"{_func()}\nPATH='{path}'\nVIRTUAL_ENV=/x/.venv\n{extra_env}\n"
        '_deploy_sanitize_path\necho "${VIRTUAL_ENV-unset}"\necho "$PATH"\n'
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    ).stdout.splitlines()


def test_venv_bin_removed_others_kept() -> None:
    venv, path = _run("/a/repo/.venv/bin:/usr/bin:/opt/homebrew/bin:/b/.venv/bin:/bin")
    assert venv == "unset"
    assert path == "/usr/bin:/opt/homebrew/bin:/bin"


def test_lookalikes_kept() -> None:
    _, path = _run("/x/.venv/bin2:/x/myvenv/bin:/usr/bin")
    assert path == "/x/.venv/bin2:/x/myvenv/bin:/usr/bin"


def test_ansible_bin_dir_prepended() -> None:
    _, path = _run("/r/.venv/bin:/usr/bin", "PRECIS_ANSIBLE_BIN_DIR=/opt/ans/bin")
    assert path == "/opt/ans/bin:/usr/bin"


def test_deploy_parses_and_checks_collection() -> None:
    subprocess.run(["bash", "-n", str(DEPLOY)], check=True)
    text = DEPLOY.read_text(encoding="utf-8")
    assert "ansible-doc -t module community.general.homebrew" in text
