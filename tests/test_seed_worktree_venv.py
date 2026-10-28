"""gr514738: ``scripts/seed-worktree-venv`` seeds a fresh worktree's
``.venv`` as a copy of a sibling checkout's instead of letting the first
``uv run`` build ~160 packages mid-task.

The interesting part is the repoint: a copied venv carries absolute paths
— console-script shebangs, ``VIRTUAL_ENV=`` in the activate scripts,
editable ``.pth``/``direct_url.json`` entries — and those paths can name a
THIRD checkout when the source venv was itself seeded. The fixture below
plants a stale checkout path on purpose.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests._script import load_script

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: execs scripts/devin-tree"
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
mod = load_script("seed_worktree_venv", _REPO_ROOT / "scripts" / "seed-worktree-venv")


def _fake_venv(checkout: Path, stale: Path) -> Path:
    """A minimal .venv under ``checkout`` whose paths point at ``stale``."""
    venv = checkout / ".venv"
    bin_dir = venv / "bin"
    sp = venv / "lib" / "python3.13" / "site-packages"
    bin_dir.mkdir(parents=True)
    sp.mkdir(parents=True)
    (bin_dir / "pytest").write_text(
        f"#!{stale}/.venv/bin/python\n# -*- coding: utf-8 -*-\nimport sys\n",
        encoding="utf-8",
    )
    (bin_dir / "activate").write_text(
        f"VIRTUAL_ENV='{stale}/.venv'\nexport VIRTUAL_ENV\n"
        'PATH="$VIRTUAL_ENV/bin:$PATH"\n',
        encoding="utf-8",
    )
    (bin_dir / "python").symlink_to("/usr/bin/python3")
    (bin_dir / "ruff").write_bytes(b"\x7fELF\x00\x01\x02" + b"\x00" * 64)
    (sp / "_editable_impl_precis_mcp.pth").write_text(
        f"{stale}/src\n{stale}/src\n", encoding="utf-8"
    )
    dist = sp / "precis_mcp-8.37.0.dist-info"
    dist.mkdir()
    (dist / "direct_url.json").write_text(
        json.dumps({"url": f"file://{stale}", "dir_info": {"editable": True}}),
        encoding="utf-8",
    )
    wheel = sp / "requests-2.0.dist-info"
    wheel.mkdir()
    (wheel / "direct_url.json").write_text(
        json.dumps({"url": "https://pypi.org/simple/requests/", "dir_info": {}}),
        encoding="utf-8",
    )
    (venv / "pyvenv.cfg").write_text("home = /x/y/bin\nuv = 0.12.5\n", encoding="utf-8")
    return venv


def test_seed_copies_and_repoints(tmp_path: Path) -> None:
    src, tree, stale = (tmp_path / n for n in ("main", "wt", "third"))
    stale.mkdir()
    _fake_venv(src, stale)
    tree.mkdir()

    assert mod.seed_tree(tree, src).startswith("seeded")

    venv = tree / ".venv"
    assert (
        (venv / "bin" / "pytest")
        .read_text(encoding="utf-8")
        .startswith(f"#!{tree}/.venv/bin/python\n")
    )
    activate = (venv / "bin" / "activate").read_text(encoding="utf-8")
    assert f"VIRTUAL_ENV='{tree}/.venv'" in activate
    assert 'PATH="$VIRTUAL_ENV/bin:$PATH"' in activate
    pth = (
        venv / "lib" / "python3.13" / "site-packages" / "_editable_impl_precis_mcp.pth"
    )
    assert pth.read_text(encoding="utf-8") == f"{tree}/src\n{tree}/src\n"
    du = json.loads(
        (
            venv
            / "lib"
            / "python3.13"
            / "site-packages"
            / "precis_mcp-8.37.0.dist-info"
            / "direct_url.json"
        ).read_text(encoding="utf-8")
    )
    assert du["url"] == f"file://{tree}"
    # Stale checkout leaked nowhere.
    assert stale.as_posix() not in pth.read_text(encoding="utf-8")
    # Symlinks and binary shims pass through untouched.
    assert (venv / "bin" / "python").is_symlink()
    assert (venv / "bin" / "ruff").read_bytes().startswith(b"\x7fELF")
    # The pyvenv.cfg shared-base `home` is not a venv path — untouched.
    assert "home = /x/y/bin" in (venv / "pyvenv.cfg").read_text(encoding="utf-8")
    # No staging dir left behind.
    assert not list(tree.glob(".venv-seed-*"))


def test_seed_idempotent_on_existing_venv(tmp_path: Path) -> None:
    src, tree, stale = (tmp_path / n for n in ("main", "wt", "third"))
    _fake_venv(src, stale)
    (tree / ".venv").mkdir(parents=True)
    sentinel = tree / ".venv" / "sentinel"
    sentinel.write_text("mine", encoding="utf-8")
    assert mod.seed_tree(tree, src) == "present"
    assert sentinel.read_text(encoding="utf-8") == "mine"


def test_seed_no_src_venv_is_noop(tmp_path: Path) -> None:
    src, tree = tmp_path / "main", tmp_path / "wt"
    src.mkdir()
    tree.mkdir()
    assert mod.seed_tree(tree, src) == "no-src"
    assert not (tree / ".venv").exists()


def test_seed_missing_tree_refuses(tmp_path: Path) -> None:
    src, stale = tmp_path / "main", tmp_path / "third"
    _fake_venv(src, stale)
    with pytest.raises(SystemExit):
        mod.seed_tree(tmp_path / "absent", src)


def test_devin_tree_wires_the_seed() -> None:
    src = (_REPO_ROOT / "scripts" / "lib" / "agent-tree.sh").read_text(encoding="utf-8")
    assert "seed-worktree-venv" in src


def test_fleet_wave_wires_the_seed() -> None:
    src = (_REPO_ROOT / "scripts" / "fleet-wave").read_text(encoding="utf-8")
    assert "seed-worktree-venv" in src


def test_main_checkout_resolves_via_git_common_dir() -> None:
    """In a real worktree, ``_main_checkout`` resolves the shared git dir's
    parent — the same path scripts/devin-tree computes."""
    import subprocess

    out = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if out.returncode:
        pytest.skip(f"git rev-parse unavailable here: {out.stderr.strip()}")
    assert mod._main_checkout(Path.cwd()) == Path(out.stdout.strip()).parent
