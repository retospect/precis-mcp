"""scripts/gripe-validate and ``precis gripes --verify-list/--verify-done``.

gr541800 / td541978: a STATUS:verify gripe waits on the sha in its newest
``resolved:`` / ``fix-sha:`` comment; once that sha is an ancestor of
origin/prod the validator comments ``resolved: <sha> (on prod …)`` and
tags STATUS:done. Pinned here: the classification against a fixture git
range (dry-run lists, writes nothing), the one ``--verify-done`` call a
real run makes, and the two in-process prod halves on the test DB.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "gripe-validate"

posix = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: execs scripts in a git repo"
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, Any]:
    """c1 ← c2 ← c3 on one line; prod sits at c2."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    shas = []
    for i in range(1, 4):
        (root / "f.txt").write_text(f"{i}\n", encoding="utf-8")
        _git(root, "add", "f.txt")
        _git(root, "commit", "-q", "-m", f"c{i}")
        shas.append(_git(root, "rev-parse", "HEAD"))
    return {"root": root, "c1": shas[0], "c2": shas[1], "c3": shas[2]}


def _fake_cli(tmp_path: Path, rows: list[dict[str, Any]]) -> tuple[str, Path]:
    """A prod-precis stand-in: ``--verify-list`` prints ``rows``; every
    call's argv is appended to the log."""
    log = tmp_path / "calls.jsonl"
    fake = tmp_path / "fake_cli.py"
    listing = "\n".join(json.dumps(r) for r in rows)
    fake.write_text(
        "import json, sys\n"
        f"with open({str(log)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if '--verify-list' in sys.argv:\n"
        "    print('precis gripes: a warning line, not JSON')\n"
        f"    print({listing!r})\n"
        "else:\n"
        "    print('done gr1')\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {fake}", log


def _run(repo: dict[str, Any], cli: str, *args: str) -> str:
    env = {**os.environ, "PRECIS_GRIPE_VALIDATE_CLI": cli}
    env.pop("PRECIS_GRIPE_VALIDATE", None)
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--prod-ref", repo["c2"], *args],
        cwd=repo["root"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _rows(repo: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"id": 1, "sha": repo["c1"][:9]},  # ancestor of prod → live
        {"id": 2, "sha": repo["c3"][:9]},  # on main, not on prod yet
        {"id": 3, "sha": None},  # no resolved sha
        {"id": 4, "sha": "deadbeef0"},  # not in this clone
        {"id": 5, "sha": repo["c2"]},  # prod itself → live
    ]


@posix
def test_dry_run_lists_live_and_writes_nothing(
    tmp_path: Path, repo: dict[str, Any]
) -> None:
    cli, log = _fake_cli(tmp_path, _rows(repo))
    line = _run(repo, cli, "--dry-run")
    c2 = repo["c2"][:9]
    assert line.startswith(f"gripe-validate ({repo['c2']} {c2}): would flip")
    assert f"verify → done: gr1@{repo['c1'][:9]} gr5@{c2}" in line
    assert f"pending: gr2@{repo['c3'][:9]}" in line
    assert "no sha: gr3" in line
    assert "unknown sha: gr4@deadbeef0" in line
    calls = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert calls == [["gripes", "--verify-list"], ["gripes", "--spec-list"]]


@posix
def test_run_closes_only_live_ids(tmp_path: Path, repo: dict[str, Any]) -> None:
    cli, log = _fake_cli(tmp_path, _rows(repo))
    line = _run(repo, cli)
    assert ": done gr1 · pending:" in line
    calls = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 3
    done = calls[1]
    assert done[:2] == ["gripes", "--verify-done"]
    assert done[2].startswith("resolved: {sha} (on prod ")
    assert done[3:] == ["--ids", "1,5"]


@posix
def test_nothing_live_makes_no_write(tmp_path: Path, repo: dict[str, Any]) -> None:
    cli, log = _fake_cli(tmp_path, [{"id": 2, "sha": repo["c3"]}])
    line = _run(repo, cli)
    assert ": none on prod · pending: gr2@" in line
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


# ── the prod halves, in process on the test DB ──────────────────────


def _gripe(store: Store, status: str) -> int:
    with store.tx() as conn:
        ref = store.insert_ref(kind="gripe", slug=None, title="v", meta={}, conn=conn)
        store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="agent", conn=conn)
    return int(ref.id)


@pytest.mark.db
def test_verify_list_and_done(hub: Any, store: Store) -> None:
    from precis.cli.gripe import verify_done, verify_list
    from precis.handlers.gripe import GripeHandler

    h = GripeHandler(hub=hub)
    a = _gripe(store, "verify")
    h.put(id=a, text="fix-sha: 1111111aa")
    h.put(id=a, text="resolved: abcdef123 (main, release pending) — landed")
    h.put(id=a, text="a later chat comment")
    b = _gripe(store, "verify")
    c = _gripe(store, "triaged")
    u = _gripe(store, "user_verify")

    rows = {g: (st, s) for g, st, s in verify_list(store)}
    assert rows[a] == ("verify", "abcdef123")
    assert rows[b] == ("verify", None)
    assert rows[u] == ("user_verify", None)
    assert c not in rows

    out = verify_done(store, "resolved: {sha} (on prod p) — test", [a, c, u, 999999999])
    assert out == (f"done gr{a} · not verify: gr{c} gr{u} · not found: gr999999999")
    tags = {str(t) for t in store.tags_for(a)}
    assert "STATUS:done" in tags
    comments = [
        x
        for x in store.chunks.list_chunks_for_ref(a)
        if x.chunk_kind == "gripe_comment"
    ]
    newest = max(comments, key=lambda x: x.ord)
    assert newest.text == "resolved: abcdef123 (on prod p) — test"
    assert a not in {g for g, _st, _s in verify_list(store)}
