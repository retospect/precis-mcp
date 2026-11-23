"""Explicit Python observations share a namespace with provisional gripe aliases."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from precis.config import PrecisConfig
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers import _gripe_summary
from precis.handlers._python_anchors import relocate
from precis.handlers.alias import AliasHandler
from precis.handlers.gripe import GripeHandler
from precis.handlers.python import PythonHandler
from precis.runtime.core import PrecisRuntime
from precis.store import Store
from tests._refs import insert_gripe

SOURCE = """\
def work():
    first = 1
    second = 2
    third = 3
    return first + second + third
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "example.py").write_text(SOURCE, encoding="utf-8")
    _git(tmp_path, "init")
    _git(tmp_path, "add", "example.py")
    _git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "source",
    )
    return tmp_path


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep unavailable")
def test_relocate_matches_whole_lines(repo: Path) -> None:
    (repo / "substring.py").write_text(
        SOURCE.replace(" = 2", " = 23"), encoding="utf-8"
    )
    assert relocate(repo, ["    first = 1", "    second = 2"]) == [("example.py", 2)]


@pytest.fixture
def handler(hub: Hub, repo: Path, store: Store) -> PythonHandler:
    store.upsert_kinds([AliasHandler.spec, GripeHandler.spec])
    py = PythonHandler(hub=hub, roots={"example": repo})
    py._register_with(hub)
    return py


def _put(handler: PythonHandler, selector: str, *, note: str | None = None) -> str:
    response = handler.put(
        mode="anchor", id="example/example.py~" + selector, args={"note": note}
    )
    match = re.search(r"\ban[0-9]+\b", response.body)
    assert match is not None
    return match.group()


def test_explicit_anchor_reuses_overlap_and_shows_gripe_comments(
    handler: PythonHandler, store: Store, hub: Hub, repo: Path
) -> None:
    legacy = _gripe_summary.mint_anchors(store, ["example.py"])["example.py"]
    handle = _put(handler, "L2-L3", note="why this range matters")
    runtime = PrecisRuntime(config=PrecisConfig(), hub=hub)
    assert runtime.dispatch("get", {"kind": "python", "id": handle}).strip() == (
        f"{handle} example/example.py:2"
    )
    with pytest.raises(BadInput, match="already allocated"):
        AliasHandler(hub=hub).put(a=handle, b="somewhere.py")
    assert handle != legacy
    assert _put(handler, "L3-L4") == handle
    assert _put(handler, "L4-L5") != handle

    assert handler.get(id=handle).body == f"{handle} example/example.py:2"
    assert "legacy location" in handler.get(id=legacy).body
    assert handle in handler.get(id="example/example.py", view="outline").body
    assert handle in handler.get(id="example::example.work", view="callers").body
    source = handler.get(id="example/example.py~L2-L3").body
    assert "mode='anchor', id='example/example.py~L2-L3'" in source
    assert "first = 1" in handler.get(id=handle, view="context").body
    assert "eye on example::example.work" in handler.get(id=handle, view="fisheye").body

    gripe = insert_gripe(store, "Observations")
    GripeHandler(hub=hub).put(id=gripe.id, text=f"See {handle}.")
    assert f"gr{gripe.id} comment" in handler.get(id=handle, view="meta").body
    assert "why this range matters" in handler.get(id=handle, view="meta").body

    (repo / "example.py").write_text("\n" + SOURCE, encoding="utf-8")
    _git(repo, "add", "example.py")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "move lines",
    )
    assert "example/example.py:3 — moved — re-verify" in handler.get(id=handle).body
    assert _put(handler, "L3-L4") == handle
    assert handler.get(id=handle).body == f"{handle} example/example.py:3"


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep unavailable")
def test_anchor_ladder_and_repo_boundary(
    handler: PythonHandler, repo: Path, store: Store
) -> None:
    handle = _put(handler, "L2-L3")
    other_hub = Hub(store=store)
    missing = PythonHandler(hub=other_hub, roots={"other": repo})
    missing._register_with(other_hub)
    assert "stale: repo not indexed" in missing.get(id=handle).body
    with pytest.raises(BadInput, match="checkout"):
        handler.get(id=handle, args={"checkout": "other"})
    (repo / "example.py").write_text(
        SOURCE.replace("first = 1", "first = 9"), encoding="utf-8"
    )
    assert "stale: snippet changed in symbol" in handler.get(id=handle).body
    (repo / "example.py").unlink()
    (repo / "relocated.py").write_text(SOURCE, encoding="utf-8")
    assert "example/relocated.py:2 — moved" in handler.get(id=handle).body
    (repo / "relocated.py").unlink()
    assert "stale: snippet not found" in handler.get(id=handle).body
