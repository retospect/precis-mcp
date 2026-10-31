"""Inbound pull requests (td543749): the class rules (``precis.plans.pr_class``)
on hand-built GitHub REST fixtures, the ``pr-intake`` template shapes, the
``pr_poll`` pass (idempotent), ``dep_check`` with a faked git/uv, ``pr_action``
with a faked ``gh``, and the plan tick closing the read-only analysis.

GitHub, git, uv and gh are all faked: no test calls the network or writes
to GitHub."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from precis import settings as psettings
from precis.errors import BadInput
from precis.plans import pr_class as P
from precis.plans import templates as T
from precis.plans.nodes import node_kind, validate_plan_meta
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.workers.job_types import dep_check as dc
from precis.workers.job_types import get_job_type, known_job_types
from precis.workers.job_types import plan_dispatch as pd
from precis.workers.job_types import pr_action as pa
from precis.workers.job_types import pr_poll as pp

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "github_pr"
REPO = "acme/widgets"
HEAD = "c4b3f7068776baa190e7b05bd8758bf19d786c3b"

#: The ten Dependabot titles open on 2026-10-31 (all pyproject floor bumps).
OPEN_TITLES = {
    73: "deps: Update lxml requirement from >=5.0 to >=6.1.3",
    68: "deps: Update types-pyyaml requirement from >=6.0.12.20260724 to >=6.0.12.20260906",
    66: "deps: Update opencv-python-headless requirement from >=4.10 to >=5.0.0.93",
    63: "deps: Update psycopg requirement from >=3.2 to >=3.3.6",
    62: "deps: Update slack-sdk requirement from >=3.27 to >=3.45.0",
    60: "deps: Update ase requirement from >=3.22 to >=3.29.0",
    58: "deps: Update pymupdf requirement from >=1.24.10 to >=2.0",
    56: "deps: Update jinja2 requirement from >=3.1 to >=3.1.6",
    54: "deps: Update trafilatura requirement from >=1.6 to >=2.3.1",
    53: "deps: Update spglib requirement from >=2.1 to >=2.8.0",
}


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _raw(**pull_over: Any) -> tuple[dict[str, Any], list[Any], list[Any]]:
    pull = _load("pull_dependabot.json")
    pull.update(pull_over)
    return pull, _load("files_dependabot.json"), _load("commits_dependabot.json")


def _facts(
    *,
    files: list[str] | None = None,
    commits: list[dict[str, Any]] | None = None,
    **pull_over: Any,
) -> P.PrFacts:
    pull, raw_files, raw_commits = _raw(**pull_over)
    if files is not None:
        raw_files = [{"filename": f, "status": "modified"} for f in files]
    return P.facts_from_api(
        REPO, pull, raw_files, raw_commits if commits is None else commits
    )


def _user(login: str, kind: str = "User") -> dict[str, Any]:
    return {"login": login, "type": kind}


def _fork_head() -> dict[str, Any]:
    head = copy.deepcopy(_load("pull_dependabot.json")["head"])
    head["repo"] = {"full_name": "mallory/widgets", "fork": True}
    return head


def _commit(sha: str, author: str, verified: bool) -> dict[str, Any]:
    return {
        "sha": sha,
        "commit": {"verification": {"verified": verified, "reason": "valid"}},
        "author": {"login": author},
    }


# ── class rules ────────────────────────────────────────────────────


def test_the_fixture_is_a_certified_dependabot_pr() -> None:
    f = _facts()
    assert f.files == ["pyproject.toml"] and not f.fork
    assert f.commits == [{"sha": HEAD, "author": "dependabot[bot]", "verified": True}]
    cls, why = P.classify(f)
    assert cls == P.DEPENDABOT, why


@pytest.mark.parametrize(
    ("change", "want", "why_has"),
    [
        ({"head": "fork"}, P.EXTERNAL, "head repo"),
        (
            {"commits": [_commit(HEAD, "dependabot[bot]", False)]},
            P.EXTERNAL,
            "unverified",
        ),
        ({"commits": [_commit(HEAD, "mallory", True)]}, P.EXTERNAL, "not authored"),
        ({"commits": []}, P.EXTERNAL, "no commits"),
        ({"user": _user("dependabot[bot]", "User")}, P.EXTERNAL, "user type"),
        ({"files": ["pyproject.toml", "src/precis/x.py"]}, P.EXTERNAL, "outside"),
        ({"files": ["pyproject.toml", "uv.lock"]}, P.DEPENDABOT, "verified"),
        ({"files": [".github/workflows/check.yml"]}, P.HARNESS, ".github/"),
        (
            {"files": ["uv.lock", "scripts/lib/ship-subject.sh"]},
            P.HARNESS,
            "scripts/lib",
        ),
        (
            {"files": ["scripts/ship"], "user": _user("mallory")},
            P.HARNESS,
            "scripts/ship",
        ),
        ({"user": _user("mallory")}, P.EXTERNAL, "author mallory"),
    ],
)
def test_class_rules_and_the_spoof_guard(
    change: dict[str, Any], want: str, why_has: str
) -> None:
    kw = dict(change)
    if kw.pop("head", None) == "fork":
        kw["head"] = _fork_head()
    cls, why = P.classify(_facts(**kw))
    assert cls == want, why
    assert why_has in why


def test_factory_needs_a_builder_a_td_handle_and_no_fork() -> None:
    devin = _user("devin-ai-integration[bot]", "Bot")
    title = "feat(plans): PR intake (td543749)"
    assert P.classify(_facts(user=devin, title=title))[0] == P.FACTORY
    assert P.classify(_facts(user=devin, title="feat: no handle"))[0] == P.EXTERNAL
    assert P.classify(_facts(user=devin, title=title, head=_fork_head()))[0] == (
        P.EXTERNAL
    )
    assert P.classify(_facts(user=_user("bob"), title=title))[0] == P.EXTERNAL
    # the setting replaces the default builder list
    assert (
        P.classify(_facts(user=_user("bob"), title=title), frozenset({"bob"}))[0]
        == P.FACTORY
    )
    assert P.factory_todo("fix(x): y (gr1, td42)") == 42
    assert P.factory_todo("fix(x): td42 in the middle") is None


def test_harness_patterns_match_ship_harness_diff() -> None:
    text = (REPO_ROOT / "scripts" / "ship").read_text(encoding="utf-8")
    m = re.search(r"^\s*(scripts/ship .*?)\) HARNESS_DIFF=1", text, re.MULTILINE)
    assert m is not None, "scripts/ship's HARNESS_DIFF case moved"
    globs = tuple(g.strip() for g in m.group(1).split("|"))
    assert globs == P.HARNESS_PATTERNS
    assert P.is_harness(".github/dependabot.yml")
    assert P.is_harness(".github/workflows/deep/x.yml")
    assert not P.is_harness("scripts/release")


def test_majors_among_the_open_dependabot_titles() -> None:
    majors = {
        n
        for n, title in OPEN_TITLES.items()
        if P.is_major(_facts(number=n, title=title))
    }
    # lxml 5->6, opencv 4->5, pymupdf 1->2, and trafilatura 1->2 too.
    assert majors == {73, 66, 58, 54}
    bump = P.bump_of(OPEN_TITLES[58])
    assert bump == P.Bump("pymupdf", ">=1.24.10", ">=2.0", True)
    assert P.bump_of("Bump the pip group with 3 updates") is None
    assert P.is_major(_facts(title="Bump the pip group with 3 updates"))
    zero = P.bump_of("Bump foo from 0.3.1 to 0.4.0")
    assert zero is not None and zero.major  # below 1.0 a minor breaks


def test_priority_security_and_ask_kind() -> None:
    f = _facts()
    assert not P.is_security(f) and P.prio_of(P.DEPENDABOT, f) == 6
    sec = _facts(labels=[{"name": "security"}])
    assert P.prio_of(P.DEPENDABOT, sec) == 2
    cve = _facts(body="Fixes CVE-2026-12345 in lxml")
    assert P.is_security(cve)
    assert P.prio_of(P.EXTERNAL, f) == 8 and P.prio_of(P.FACTORY, f) == 4
    assert P.ask_kind(_facts(title="feat: a new kind")) == "design-verdict"
    assert P.ask_kind(_facts(title="Fix typo", labels=[{"name": "enhancement"}])) == (
        "design-verdict"
    )
    assert P.ask_kind(_facts(title="Fix a typo")) == "approval"


def test_land_subject_passes_the_ship_lint() -> None:
    assert P.land_subject(OPEN_TITLES[73], P.DEPENDABOT) == (
        "build(deps): update lxml requirement from >=5.0 to >=6.1.3"
    )
    assert P.land_subject("fix(x): keep", P.EXTERNAL) == "fix(x): keep"
    assert P.land_subject("Add a widget", P.EXTERNAL) == "chore(pr): add a widget"


# ── registry ───────────────────────────────────────────────────────


def test_registry() -> None:
    for name, executor in (
        ("pr_poll", "job_inproc"),
        ("dep_check", "ssh_node"),
        ("pr_action", "ssh_node"),
    ):
        spec = get_job_type(name)
        assert spec is not None and name in known_job_types(), name
        assert spec.compatible_executors == frozenset({executor})
    poll = get_job_type("pr_poll")
    assert poll is not None and poll.dispatch is not None and poll.dedupe_pending
    for name in ("dep_check", "pr_action"):
        spec = get_job_type(name)
        assert spec is not None and spec.submit is not None and spec.poll is not None
    assert "pr-intake" in T.TEMPLATES and T.TEMPLATES["pr-intake"].required == ("pr",)


# ── template shapes (DB) ───────────────────────────────────────────

pytest_db = pytest.mark.db
pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX-only: reads scripts/ship, git paths"
    ),
]


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("PRECIS_NODE", raising=False)
    psettings.invalidate()


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    ref = store.get_ref(kind="todo", id=ref_id)
    assert ref is not None
    return dict(ref.meta or {})


def _status(store: Store, ref_id: int) -> str | None:
    for t in store.tags_for(ref_id):
        if t.prefix == "STATUS":
            return str(t.value)
    return None


def _tags(store: Store, ref_id: int) -> set[str]:
    return {str(t.value) for t in store.tags_for(ref_id)}


def _comment(store: Store, todo_id: int, text: str) -> None:
    existing = list(store.chunks.list_chunks_for_ref(todo_id))
    ord_ = max((int(c.ord) for c in existing), default=0) + 1
    store.chunks.insert_chunks(
        todo_id,
        [ChunkInsert(ord=ord_, text=text, meta={"chunk_kind": "todo_comment"})],
    )


def _comments(store: Store, todo_id: int) -> list[str]:
    return [
        str(c.text)
        for c in store.chunks.list_chunks_for_ref(todo_id)
        if c.chunk_kind == "todo_comment"
    ]


def _blocked_by(store: Store, mint: T.PlanMint) -> set[tuple[str, str]]:
    by_id = {v: k for k, v in mint.ids.items()}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_ref_id, dst_ref_id FROM links "
            "WHERE relation = 'blocked-by' AND src_ref_id = ANY(%s)",
            (list(mint.ids.values()),),
        ).fetchall()
    return {(by_id[s], by_id[d]) for s, d in rows}


def _prio(store: Store, ref_id: int) -> int | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT prio FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return row[0]


def _jobs_under(store: Store, todo_id: int) -> list[tuple[int, dict[str, Any]]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = 'job' AND parent_id = %s "
            "AND retired_at IS NULL ORDER BY ref_id",
            (todo_id,),
        ).fetchall()
    return [(int(r[0]), dict(r[1])) for r in rows]


def _succeed(store: Store, job_id: int) -> None:
    store.add_tag(
        job_id, Tag.closed("STATUS", "succeeded"), set_by="system", replace_prefix=True
    )


def _hygiene(store: Store, mint: T.PlanMint) -> None:
    for key, todo_id in mint.ids.items():
        meta = _meta(store, todo_id)
        assert validate_plan_meta(meta) == [], key
        assert "llm_tier" not in meta, key
        assert f"plan:{mint.root_id}" in _tags(store, todo_id), key


@pytest_db
def test_dependabot_major_shape(store: Store) -> None:
    mint = T.mint_pr_intake(store, pr=73, facts=_facts())
    ids = mint.ids
    _hygiene(store, mint)
    assert set(ids) == {"root", "check", "decision", "act", "land", "review"}
    assert {k: node_kind(_meta(store, i)) for k, i in ids.items()} == {
        "root": "phase",
        "check": "task",
        "decision": "gate",
        "act": "task",
        "land": "task",
        "review": "generator",
    }
    assert _blocked_by(store, mint) == {
        ("decision", "check"),
        ("act", "decision"),
        ("land", "act"),
        ("review", "land"),
    }
    root = _meta(store, ids["root"])
    assert root["pr"]["class"] == P.DEPENDABOT and root["pr"]["bump"]["major"]
    assert "pr:73" in _tags(store, ids["root"]) and _prio(store, ids["root"]) == 6
    assert _meta(store, ids["check"])["op"] == "dep_check"
    assert _meta(store, ids["act"])["op"] == "pr_action"
    ask = _meta(store, ids["decision"])["ask"]
    assert ask["options"] == T.DEPENDABOT_OPTIONS and ask["kind"] == "approval"
    assert ask["blocks"] == f"td{ids['act']}"
    assert "ask-user" in _tags(store, ids["decision"])
    assert _meta(store, ids["land"])["op"] == {
        "type": "land_slice",
        "params": {"todo_id": ids["land"], "pr": 73, "checked": True},
    }
    assert _meta(store, ids["review"])["generate"] == {
        "role": "reviewer",
        "from": f"td{ids['land']}",
    }
    with pytest.raises(BadInput, match="already has an intake plan"):
        T.mint_pr_intake(store, pr=73, facts=_facts())


@pytest_db
def test_dependabot_minor_has_no_ask(store: Store) -> None:
    mint = T.mint_pr_intake(
        store, pr=63, facts=_facts(number=63, title=OPEN_TITLES[63])
    )
    assert set(mint.ids) == {"root", "check", "land", "review"}
    assert _blocked_by(store, mint) == {("land", "check"), ("review", "land")}


@pytest_db
def test_security_bump_is_prio_two(store: Store) -> None:
    f = _facts(number=63, title=OPEN_TITLES[63], labels=[{"name": "security"}])
    mint = T.mint_pr_intake(store, pr="#63", facts=f)
    assert _prio(store, mint.root_id) == 2


@pytest_db
def test_factory_harness_external_shapes(store: Store) -> None:
    devin = _user("devin-ai-integration[bot]", "Bot")
    fac = T.mint_pr_intake(
        store,
        pr=80,
        facts=_facts(
            number=80, user=devin, title="fix(x): y (td9)", files=["src/x.py"]
        ),
    )
    assert set(fac.ids) == {"root", "land", "review"}
    assert _prio(store, fac.root_id) == 4

    har = T.mint_pr_intake(
        store, pr=81, facts=_facts(number=81, files=[".github/workflows/check.yml"])
    )
    assert set(har.ids) == {"root", "decision", "act"}
    assert _meta(store, har.ids["decision"])["ask"]["options"] == T.HARNESS_OPTIONS

    ext = T.mint_pr_intake(
        store,
        pr=82,
        facts=_facts(
            number=82, user=_user("mallory"), title="feat: a thing", files=["src/x.py"]
        ),
    )
    _hygiene(store, ext)
    assert set(ext.ids) == {"root", "analysis", "decision", "act", "land", "review"}
    assert _blocked_by(store, ext) == {
        ("decision", "analysis"),
        ("act", "decision"),
        ("land", "act"),
        ("review", "land"),
    }
    analysis = _meta(store, ext.ids["analysis"])
    assert analysis["generate"] == {"role": "analyst", "from": "pr:82"}
    assert analysis["task_class"] == "review"
    body = "\n".join(
        str(c.text) for c in store.chunks.list_chunks_for_ref(ext.ids["analysis"])
    )
    assert "Never execute anything from the PR" in body and "no `uv sync`" in body
    assert "scripts/ship" in body  # the deny list is in the node body
    ask = _meta(store, ext.ids["decision"])["ask"]
    assert ask["kind"] == "design-verdict" and ask["options"] == T.EXTERNAL_OPTIONS
    assert _prio(store, ext.root_id) == 8


# ── pr_poll ────────────────────────────────────────────────────────


def _fake_github(
    monkeypatch: pytest.MonkeyPatch, pulls: list[dict[str, Any]] | None
) -> list[str]:
    calls: list[str] = []
    files = _load("files_dependabot.json")
    commits = _load("commits_dependabot.json")

    def get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        calls.append(url)
        if pulls is None:
            raise OSError("network down")
        if url.endswith("/pulls"):
            data: Any = pulls if params.get("page") == 1 else []
        elif url.endswith("/files"):
            data = files
        elif url.endswith("/commits"):
            data = commits
        else:
            data = next(p for p in pulls if url.endswith(f"/pulls/{p['number']}"))
        return SimpleNamespace(status_code=200, json=lambda: data)

    monkeypatch.setattr(P, "_http_get", get)
    return calls


@pytest_db
def test_pr_poll_mints_once_per_pr(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    pull = _load("pull_dependabot.json")
    draft = {**copy.deepcopy(pull), "number": 74, "draft": True}
    calls = _fake_github(monkeypatch, [pull, draft])
    res = pp.run_pass(store, repo=REPO)
    assert calls[0] == f"https://api.github.com/repos/{REPO}/pulls"
    assert (res.open, res.drafts, res.known) == (2, 1, 0)
    [(number, root, cls)] = res.minted
    assert (number, cls) == (73, P.DEPENDABOT)
    assert P.existing_intake(store, 73) == root
    again = pp.run_pass(store, repo=REPO)
    assert again.minted == [] and again.known == 1
    # a closed intake still counts: one intake per PR, ever
    store.add_tag(
        root, Tag.closed("STATUS", "done"), set_by="agent", replace_prefix=True
    )
    assert pp.run_pass(store, repo=REPO).minted == []


@pytest_db
def test_pr_poll_dry_run_and_unreadable(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_github(monkeypatch, [_load("pull_dependabot.json")])
    dry = pp.run_pass(store, repo=REPO, dry_run=True)
    assert (
        dry.minted == [(73, 0, P.DEPENDABOT)] and P.existing_intake(store, 73) is None
    )
    _fake_github(monkeypatch, None)
    res = pp.run_pass(store, repo=REPO)
    assert res.minted == [] and "unreadable" in res.note


# ── dep_check ──────────────────────────────────────────────────────


class FakeRun:
    """git/uv seam: records argv, answers by the command."""

    def __init__(
        self, *, lock_ok: bool = True, fail_py: str = "", head: str = HEAD
    ) -> None:
        self.calls: list[tuple[list[str], Path, dict[str, str] | None]] = []
        self.lock_ok = lock_ok
        self.fail_py = fail_py
        self.head = head

    def __call__(
        self, argv: list[str], cwd: Path, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, Path(cwd), env))
        rc, out = 0, ""
        if argv[:2] == ["git", "worktree"] and argv[2] == "add":
            Path(argv[3]).mkdir(parents=True, exist_ok=True)
        if argv[:2] == ["git", "rev-parse"]:
            out = self.head + "\n"
        if argv[1:3] == ["lock", "--check"] and not self.lock_ok:
            rc = 1
        if argv[1:2] == ["sync"] and argv[-1] == self.fail_py:
            rc = 2
        return subprocess.CompletedProcess(
            argv, rc, stdout=out, stderr="uv said no" if rc else ""
        )

    def uv(self) -> list[list[str]]:
        return [a for a, _c, _e in self.calls if a[0] == "uv"]


def test_dep_check_resolves_both_pythons_without_building(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_DB_DSN", "postgres://secret")
    run = FakeRun()
    res = dc.run_check(tmp_path, 73, head=HEAD, run=run)
    assert res["ok"] and res["lock"] == "current", res
    assert res["pythons"] == {"3.12": "ok", "3.13": "ok"}
    assert res["branch"] == "slice/pr-73"
    assert res["worktree"] == str(tmp_path / ".claude" / "worktrees" / "slice-pr-73")
    git = [a for a, _c, _e in run.calls if a[0] == "git"]
    assert ["git", "fetch", "origin", "+pull/73/head:refs/heads/slice/pr-73"] in git
    assert run.uv() == [
        ["uv", "lock", "--check", "--no-build"],
        ["uv", "sync", "--dry-run", "--locked", "--no-build", "--python", "3.12"],
        ["uv", "sync", "--dry-run", "--locked", "--no-build", "--python", "3.13"],
    ]
    # resolve only: no install, no run, no pip; the env carries no DSN
    for _a, _c, env in run.calls:
        if env is not None:
            assert "PRECIS_DB_DSN" not in env and env["UV_NO_BUILD"] == "1"


def test_dep_check_relocks_a_stale_lock(tmp_path: Path) -> None:
    run = FakeRun(lock_ok=False)
    res = dc.run_check(tmp_path, 73, run=run)
    assert res["ok"] and res["lock"] == "relocked"
    assert ["uv", "lock", "--no-build"] in run.uv()
    commits = [a for a, _c, _e in run.calls if "commit" in a]
    assert commits and commits[0][-1] == "build(deps): relock uv.lock for PR #73"


@pytest.mark.parametrize(
    ("run", "step"),
    [
        (FakeRun(fail_py="3.13"), "uv sync --dry-run --python 3.13"),
        (FakeRun(head="f" * 40), "head moved since intake"),
    ],
)
def test_dep_check_red_cleans_up(tmp_path: Path, run: FakeRun, step: str) -> None:
    res = dc.run_check(tmp_path, 73, head=HEAD, run=run)
    assert not res["ok"] and res["failed_step"] == step
    git = [a for a, _c, _e in run.calls if a[0] == "git"]
    assert git[-2][:4] == ["git", "worktree", "remove", "--force"]
    assert git[-1] == ["git", "branch", "-D", "slice/pr-73"]


class Ctx:
    def __init__(self, store: Store, params: dict[str, Any]) -> None:
        self.store = store
        self.ref_id = 4243
        self.meta: dict[str, Any] = {"params": params}
        self.chunks: list[tuple[str, str]] = []
        self.failures: list[str] = []
        self.status: str | None = None
        self.stamped: dict[str, Any] = {}

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def set_meta(self, **kw: Any) -> None:
        self.stamped.update(kw)

    def record_failure(self, msg: str, **_kw: Any) -> None:
        self.failures.append(msg)

    def set_status(self, value: str) -> None:
        self.status = value


@pytest_db
def test_dep_check_job_pins_the_land(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.workers.job_types import run_slice as rs

    mint = T.mint_pr_intake(
        store, pr=63, facts=_facts(number=63, title=OPEN_TITLES[63])
    )
    seen: dict[str, Any] = {}

    def launch(argv: list[str], *, cwd: Path, log_path: Path, exit_file: Path) -> int:
        seen.update(argv=argv, log=log_path, exit=exit_file)
        return 999_999

    monkeypatch.setattr(rs, "slice_root", lambda: tmp_path)
    monkeypatch.setattr(rs, "_process_identity", lambda pid: None)
    monkeypatch.setattr(rs, "_process_alive", lambda pid: True)
    monkeypatch.setattr(dc, "_run_dir", lambda: tmp_path)
    monkeypatch.setattr(dc, "_launch", launch)
    monkeypatch.setenv("PRECIS_NODE", "slice-host")
    ctx = Ctx(store, {"todo_id": mint.ids["check"]})
    handle = dc.submit(ctx, None)
    assert ctx.failures == [] and handle["pr"] == 63
    assert seen["argv"][1:3] == ["-m", "precis.workers.job_types.dep_check"]
    assert seen["argv"][-2:] == ["--head", HEAD]
    assert dc.poll(ctx, handle) is False
    res = dc.run_check(tmp_path, 63, head=HEAD, run=FakeRun())
    Path(seen["log"]).write_text(
        dc.RESULT_PREFIX + json.dumps(res) + "\n", encoding="utf-8"
    )
    Path(seen["exit"]).write_text("0\n", encoding="utf-8")
    assert dc.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    params = _meta(store, mint.ids["land"])["op"]["params"]
    assert params == {
        "todo_id": mint.ids["land"],
        "pr": 63,
        "checked": True,
        "worktree": res["worktree"],
        "branch": "slice/pr-63",
        "target_node": "slice-host",
    }
    assert _meta(store, mint.root_id)["pr"]["dep_check"]["lock"] == "current"


@pytest_db
def test_dep_check_refused_outside_a_pr_plan_and_factory_off(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = T.mint_release_pass(store)
    why = dc.validate_submit(store, params={"todo_id": mint.ids["cut"]})
    assert why is not None and "no pr-intake plan" in why
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    off = dc.validate_submit(store, params={"todo_id": mint.ids["cut"]})
    assert off is not None and "PRECIS_FACTORY=off" in off


# ── pr_action ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("answer", "want"),
    [
        ("land", ("land", "")),
        ("Land as is", ("land", "")),
        ("land after fixes: rename x; drop y", ("fix", "rename x; drop y")),
        ("ignore this major", ("ignore-major", "")),
        (
            "request changes on the PR: please add a test",
            ("request-changes", "please add a test"),
        ),
        ("request changes on the PR", ("request-changes", "")),
        ("close: duplicate of #12", ("close", "duplicate of #12")),
        ("hold", ("hold", "")),
        ("land via /go", ("manual", "")),
        ("'land'", ("land", "")),
        ("maybe later", None),
    ],
)
def test_parse_choice(answer: str, want: tuple[str, str] | None) -> None:
    assert pa.parse_choice(answer) == want


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(pa, "_gh", fake)
    return calls


def _external(store: Store) -> T.PlanMint:
    return T.mint_pr_intake(
        store,
        pr=82,
        facts=_facts(
            number=82, user=_user("mallory"), title="Fix a typo", files=["README.md"]
        ),
    )


@pytest_db
def test_pr_action_waits_for_the_answer(store: Store, gh: list[list[str]]) -> None:
    mint = _external(store)
    why = pa.validate_submit(store, params={"todo_id": mint.ids["act"]})
    assert why is not None and "not answered" in why
    ok, summary = pa.act(store, mint.ids["act"])
    assert not ok and gh == []


@pytest_db
def test_pr_action_land_posts_nothing(store: Store, gh: list[list[str]]) -> None:
    mint = _external(store)
    _comment(store, mint.ids["decision"], "answer: land as is")
    assert pa.validate_submit(store, params={"todo_id": mint.ids["act"]}) is None
    ok, summary = pa.act(store, mint.ids["act"])
    assert ok and "the land runs next" in summary and gh == []
    assert _status(store, mint.ids["land"]) == "open"


@pytest_db
def test_pr_action_close_posts_then_skips_the_rest(
    store: Store, gh: list[list[str]]
) -> None:
    mint = _external(store)
    _comment(store, mint.ids["decision"], "answer: close: duplicate of #12")
    ok, summary = pa.act(store, mint.ids["act"])
    assert ok, summary
    assert gh == [
        ["pr", "close", "82", "--repo", REPO, "--comment", "duplicate of #12"]
    ]
    for key in ("land", "review"):
        assert _status(store, mint.ids[key]) == "won't-do"
        assert _comments(store, mint.ids[key])[-1].startswith("skipped: answer")
    assert _status(store, mint.ids["act"]) == "open"  # the tick closes it


@pytest_db
def test_pr_action_ignore_major_and_hold(store: Store, gh: list[list[str]]) -> None:
    mint = T.mint_pr_intake(store, pr=73, facts=_facts())
    _comment(store, mint.ids["decision"], "answer: ignore this major")
    ok, _ = pa.act(store, mint.ids["act"])
    assert ok and gh == [
        ["pr", "comment", "73", "--repo", REPO, "--body", pa.IGNORE_MAJOR_COMMENT]
    ]
    assert _status(store, mint.ids["land"]) == "won't-do"

    held = T.mint_pr_intake(
        store, pr=58, facts=_facts(number=58, title=OPEN_TITLES[58])
    )
    _comment(store, held.ids["decision"], "answer: hold")
    ok, summary = pa.act(store, held.ids["act"])
    assert ok and "nothing posted" in summary and len(gh) == 1


@pytest_db
def test_pr_action_land_after_fixes_mints_the_fix_slice(
    store: Store, gh: list[list[str]]
) -> None:
    mint = _external(store)
    _comment(store, mint.ids["decision"], "answer: land after fixes: add a test")
    ok, summary = pa.act(store, mint.ids["act"])
    assert ok, summary
    assert gh == []
    assert _status(store, mint.ids["land"]) == "won't-do"
    assert _status(store, mint.ids["review"]) == "won't-do"
    m = re.search(r"fix slice td(\d+) and review td(\d+)", summary)
    assert m is not None
    fix, review = int(m.group(1)), int(m.group(2))
    assert (
        _meta(store, fix)["pr_close"] == 82 and _meta(store, fix)["op"] == "run_slice"
    )
    assert _meta(store, review)["generate"] == {"role": "reviewer", "from": f"td{fix}"}
    assert f"plan:{mint.root_id}" in _tags(store, fix)
    body = "\n".join(str(c.text) for c in store.chunks.list_chunks_for_ref(fix))
    assert "git fetch origin pull/82/head" in body and "add a test" in body


@pytest_db
def test_pr_action_refuses_an_unknown_answer(store: Store, gh: list[list[str]]) -> None:
    mint = _external(store)
    _comment(store, mint.ids["decision"], "answer: maybe later")
    ok, summary = pa.act(store, mint.ids["act"])
    assert not ok and "matches no option" in summary and gh == []
    assert _status(store, mint.ids["land"]) == "open"


# ── the tick ───────────────────────────────────────────────────────


@pytest_db
def test_tick_closes_the_analysis_and_waits_for_the_answer(store: Store) -> None:
    mint = _external(store)
    ids = mint.ids
    res = pd.run_pass(store)
    [(job, meta)] = _jobs_under(store, ids["analysis"])
    assert meta["job_type"] == "run_slice" and res.dispatched == 1
    _succeed(store, job)
    _comment(store, ids["analysis"], "handoff: analysis: (a) no threats (b) typo fix")
    res = pd.run_pass(store)
    assert _status(store, ids["analysis"]) == "done"
    assert any("analysis: comment posted" in n for n in res.notes), res.notes
    assert _status(store, ids["decision"]) == "open"
    assert _jobs_under(store, ids["act"]) == []

    _comment(store, ids["decision"], "answer: land as is")
    pd.run_pass(store)
    pd.run_pass(store)
    assert _status(store, ids["decision"]) == "done"
    [(_job, meta)] = _jobs_under(store, ids["act"])
    assert (meta["job_type"], meta["params"]) == ("pr_action", {"todo_id": ids["act"]})
