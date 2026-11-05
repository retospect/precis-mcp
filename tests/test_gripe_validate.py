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
def test_held_gripe_is_never_in_the_close_set(
    tmp_path: Path, repo: dict[str, Any]
) -> None:
    """gr547015: a ``hold:no-autoclose`` row (``held`` on --verify-list)
    is reported, never closed — the recurring job posts its ``shipped
    in r<N>`` note instead."""
    rows = _rows(repo) + [{"id": 6, "sha": repo["c1"][:9], "held": True}]
    cli, log = _fake_cli(tmp_path, rows)
    line = _run(repo, cli)
    assert ": done gr1 · pending:" in line
    assert f"held (no auto-close): gr6@{repo['c1'][:9]}" in line
    calls = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    done = calls[1]
    assert done[:2] == ["gripes", "--verify-done"]
    assert done[3:] == ["--ids", "1,5"]  # gr6 is live but held — absent


@posix
def test_nothing_live_makes_no_write(tmp_path: Path, repo: dict[str, Any]) -> None:
    cli, log = _fake_cli(tmp_path, [{"id": 2, "sha": repo["c3"]}])
    line = _run(repo, cli)
    assert ": none on prod · pending: gr2@" in line
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


@posix
def test_check_gripe_is_autoclose_owned_not_closed(
    tmp_path: Path, repo: dict[str, Any]
) -> None:
    """td570413: a ``check`` row on --verify-list (the gripe's own
    ``meta.fix.check`` or its code todo's) is the gripe_autoclose pass's
    to close on the check's verdict — reported, never in the close set,
    and the report names the next step (r2)."""
    rows = _rows(repo) + [{"id": 6, "sha": repo["c1"][:9], "check": True}]
    cli, log = _fake_cli(tmp_path, rows)
    line = _run(repo, cli)
    assert ": done gr1 · pending:" in line
    assert f"autoclose-owned: gr6@{repo['c1'][:9]}" in line
    assert "closes when its prod check passes after deploy" in line
    assert "clear meta.fix.check" in line
    calls = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    done = calls[1]
    assert done[:2] == ["gripes", "--verify-done"]
    assert done[3:] == ["--ids", "1,5"]  # gr6 is live but check-owned — absent


@posix
def test_inevaluable_check_closes_on_sha_naming_why(
    tmp_path: Path, repo: dict[str, Any]
) -> None:
    """td570413 r2: a ``check_why`` row — a check line the pass cannot
    evaluate — is validate's to close on the sha like a check-less
    gripe, and the output names the reason."""
    rows = _rows(repo) + [
        {
            "id": 6,
            "sha": repo["c1"][:9],
            "check_why": "check does not parse (nope)",
        }
    ]
    cli, log = _fake_cli(tmp_path, rows)
    line = _run(repo, cli)
    assert "check not evaluable (check does not parse (nope))" in line
    assert "closing on sha" in line
    calls = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    done = calls[1]
    assert done[:2] == ["gripes", "--verify-done"]
    assert done[3:] == ["--ids", "1,5,6"]  # gr6 closes on the sha anyway


# ── the prod halves, in process on the test DB ──────────────────────


def _gripe(store: Store, status: str) -> int:
    with store.tx() as conn:
        ref = store.insert_ref(kind="gripe", slug=None, title="v", meta={}, conn=conn)
        store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="agent", conn=conn)
    return int(ref.id)


def _enable(store: Store) -> None:
    """Light the dark ``gripe_autoclose`` gate — the check-line
    mechanism is inert until service_config lights it (r3)."""
    from precis.workers.gripe_autoclose import SERVICE_NAME
    from precis.workers.service_config import set_service_prio

    set_service_prio(store, "*", SERVICE_NAME, 1, actor="test")


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


@pytest.mark.db
def test_verify_done_refuses_a_held_gripe(hub: Any, store: Store) -> None:
    """gr547015: the prod-side verb itself honours ``hold:no-autoclose``
    — a stale caller's id list cannot close a held gripe."""
    from precis.cli.gripe import hold_set, verify_done
    from precis.handlers.gripe import GripeHandler

    h = GripeHandler(hub=hub)
    a = _gripe(store, "verify")
    h.put(id=a, text="resolved: abcdef123 (main, release pending)")
    store.add_tag(a, Tag.open("hold:no-autoclose"), set_by="agent")
    assert hold_set(store, [a]) == {a}
    out = verify_done(store, "resolved: {sha} (on prod p) — test", [a])
    assert out == f"held: gr{a}"
    assert {str(t) for t in store.tags_for(a)} >= {"STATUS:verify"}


def _code_todo(
    store: Store,
    *,
    check: bool,
    status: str | None = "done",
    landed: str | list[str] | None = "b1b2b3b4b",
) -> int:
    """A ``fixes``-shaped code todo: STATUS tag, real ``todo_comment``
    ``landed:`` chunk(s) (the land record — a list appends in order,
    the last is the NEWEST one the pass reads) and ``meta.fix.check``."""
    from precis.handlers._comment_timeline import append_comment

    tref = store.insert_ref(kind="todo", slug=None, title="code item", meta={})
    tid = int(tref.id)
    if status is not None:
        store.add_tag(tid, Tag.closed("STATUS", status), set_by="agent")
    if landed:
        for text in [landed] if isinstance(landed, str) else landed:
            append_comment(
                store,
                tref,
                kind="todo",
                comment_kind="todo_comment",
                text=f"landed: {text} — feat(x): y",
            )
    if check:
        store.update_ref(tid, meta_patch={"fix": {"check": "count gripe >= 1"}})
    return tid


@pytest.mark.db
def test_check_map_flags_evaluable_and_names_why(store: Store) -> None:
    """td570413 r2: ``check_map`` shares ``gripe_autoclose._candidates``'
    evaluator — a check line the pass cannot evaluate (no landed, todo
    not ``done``, a newest ``landed:`` that is not a sha, an
    unparseable or own-ref line) is NOT autoclose-owned but IS named
    with its reason, so gripe-validate can close it and say why."""
    from precis.cli.gripe import check_map, check_set

    _enable(store)  # the whole mechanism is inert while dark (r3)
    # legacy gripe-side, evaluable: landed + parseable check (a count —
    # a bare ``read <kind> <gid>`` collides with the own-ref rule, which
    # matches ref_id regardless of kind).
    a = _gripe(store, "verify")
    store.update_ref(
        a, meta_patch={"fix": {"landed": "b1b2b3b4b", "check": "count gripe >= 1"}}
    )
    # todo-side, evaluable: done + landed comment + check.
    b = _gripe(store, "verify")
    store.add_link(
        src_ref_id=_code_todo(store, check=True), dst_ref_id=b, relation="fixes"
    )
    # gripe-side check with no landed.
    c = _gripe(store, "verify")
    store.update_ref(c, meta_patch={"fix": {"check": "count gripe >= 1"}})
    # done todo with check but no landed: record.
    d = _gripe(store, "verify")
    store.add_link(
        src_ref_id=_code_todo(store, check=True, landed=None),
        dst_ref_id=d,
        relation="fixes",
    )
    # a check-lined todo still in build.
    e = _gripe(store, "verify")
    store.add_link(
        src_ref_id=_code_todo(store, check=True, status="doing"),
        dst_ref_id=e,
        relation="fixes",
    )
    # a fixes todo carrying no check line.
    f = _gripe(store, "verify")
    store.add_link(
        src_ref_id=_code_todo(store, check=False), dst_ref_id=f, relation="fixes"
    )
    # a check-lined retired todo does not count.
    g = _gripe(store, "verify")
    dead = _code_todo(store, check=True)
    store.retire_ref(dead)
    store.add_link(src_ref_id=dead, dst_ref_id=g, relation="fixes")
    # an unparseable gripe-side check.
    h = _gripe(store, "verify")
    store.update_ref(
        h, meta_patch={"fix": {"landed": "b1b2b3b4b", "check": "not a check"}}
    )
    # a gripe-side check reading its own ref — tautological.
    i = _gripe(store, "verify")
    store.update_ref(
        i,
        meta_patch={
            "fix": {"landed": "b1b2b3b4b", "check": f'read gripe {i} contains "x"'}
        },
    )
    # the NEWEST landed: comment is no sha — the older valid one loses.
    j = _gripe(store, "verify")
    store.add_link(
        src_ref_id=_code_todo(store, check=True, landed=["b1b2b3b4b", "pending"]),
        dst_ref_id=j,
        relation="fixes",
    )
    # r3: an older INEVALUABLE todo does not shadow a newer evaluable
    # one — the first evaluable line owns the gripe.
    k = _gripe(store, "verify")
    store.add_link(
        src_ref_id=_code_todo(store, check=True, status="doing"),
        dst_ref_id=k,
        relation="fixes",
    )
    store.add_link(
        src_ref_id=_code_todo(store, check=True), dst_ref_id=k, relation="fixes"
    )
    # r3: no evaluable todo → the evaluable legacy gripe-side line
    # stands (the todo is still in build; the gripe's meta.fix is good).
    ll = _gripe(store, "verify")
    store.update_ref(
        ll,
        meta_patch={"fix": {"landed": "b1b2b3b4b", "check": "count gripe >= 1"}},
    )
    store.add_link(
        src_ref_id=_code_todo(store, check=True, status="doing"),
        dst_ref_id=ll,
        relation="fixes",
    )
    ids = [a, b, c, d, e, f, g, h, i, j, k, ll]
    assert check_set(store, ids) == {a, b, k, ll}
    cm = check_map(store, ids)
    assert {g for g, w in cm.items() if w is None} == {a, b, k, ll}
    assert f not in cm and g not in cm

    def _why(gid: int) -> str:
        w = cm[gid]
        assert w is not None
        return w

    assert "landed" in _why(c)
    assert "landed" in _why(d)
    assert "done" in _why(e)
    assert "does not parse" in _why(h)
    assert "own ref" in _why(i)
    assert "landed" in _why(j)


@pytest.mark.db
def test_verify_done_refuses_a_check_lined_gripe(hub: Any, store: Store) -> None:
    """td570413 r1: ``verify_done`` itself refuses a check-lined gripe —
    a stale caller's id list cannot close what ``gripe_autoclose`` owns,
    the same shape as the ``hold:no-autoclose`` refusal."""
    from precis.cli.gripe import verify_done
    from precis.handlers._comment_timeline import append_comment
    from precis.handlers.gripe import GripeHandler

    _enable(store)  # the refusal exists only while the pass is lit (r3)
    h = GripeHandler(hub=hub)
    a = _gripe(store, "verify")
    h.put(id=a, text="resolved: abcdef123 (main, release pending)")
    tref = store.insert_ref(
        kind="todo",
        slug=None,
        title="code item",
        meta={"fix": {"check": "count gripe >= 1"}},
    )
    tid = int(tref.id)
    store.add_link(src_ref_id=tid, dst_ref_id=a, relation="fixes")
    store.add_tag(tid, Tag.closed("STATUS", "done"), set_by="agent")
    append_comment(
        store,
        tref,
        kind="todo",
        comment_kind="todo_comment",
        text="landed: abcdef123 — feat(x): y",
    )
    out = verify_done(store, "resolved: {sha} (on prod p) — test", [a])
    assert out == (
        f"autoclose-owned: gr{a} — closes when its prod check passes "
        "after deploy; clear meta.fix.check to close on the sha alone"
    )
    assert {str(t) for t in store.tags_for(a)} >= {"STATUS:verify"}


@pytest.mark.db
def test_gate_off_closes_check_lined_gripes_on_the_sha(hub: Any, store: Store) -> None:
    """td570413 r3: while the pass is dark, behaviour is exactly as
    before this branch — ``check_map`` is empty and ``verify_done``
    closes a check-lined gripe on the sha. Lit, the same shape is
    autoclose-owned."""
    from precis.cli.gripe import check_map, check_set, verify_done
    from precis.handlers._comment_timeline import append_comment
    from precis.handlers.gripe import GripeHandler

    def _lined() -> int:
        g = _gripe(store, "verify")
        tref = store.insert_ref(
            kind="todo",
            slug=None,
            title="code item",
            meta={"fix": {"check": "count gripe >= 1"}},
        )
        tid = int(tref.id)
        store.add_link(src_ref_id=tid, dst_ref_id=g, relation="fixes")
        store.add_tag(tid, Tag.closed("STATUS", "done"), set_by="agent")
        append_comment(
            store,
            tref,
            kind="todo",
            comment_kind="todo_comment",
            text="landed: abcdef123 — feat(x): y",
        )
        return g

    h = GripeHandler(hub=hub)
    a = _lined()
    h.put(id=a, text="resolved: abcdef123 (main, release pending)")
    # Dark: nothing marks it — it closes on the sha like any other.
    assert check_map(store, [a]) == {}
    assert check_set(store, [a]) == set()
    out = verify_done(store, "resolved: {sha} (on prod p) — test", [a])
    assert out == f"done gr{a}"
    assert {str(t) for t in store.tags_for(a)} >= {"STATUS:done"}
    # Lit: the identical shape is autoclose-owned and refuses.
    _enable(store)
    b = _lined()
    h.put(id=b, text="resolved: abcdef123 (main, release pending)")
    assert check_set(store, [b]) == {b}
    out = verify_done(store, "resolved: {sha} (on prod p) — test", [b])
    assert out.startswith("autoclose-owned:")
    assert {str(t) for t in store.tags_for(b)} >= {"STATUS:verify"}
