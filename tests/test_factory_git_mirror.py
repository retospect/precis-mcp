"""workers/factory_git_mirror: the land's credential boundary (gr659929, review td660513).

The lander refreshes a per-launch bare mirror of the land refs from GitHub
under the factory token, points ship's ``origin`` at it, and after a green
ship pushes only the moved refs to the real origin — each leased on the tip
GitHub held at launch, each verified to be the sha ship reported. Every
credential-bearing call runs the trusted boundary (fresh ops gitdir, hooks
off, no system/global config) and every GitHub answer is compared against
the ``ls-remote`` oracle. Ship's tree, where branch code runs host-side,
never holds the token, a token fd, or a credential helper.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from precis.workers import factory_git_mirror as fgm

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40

TIPS = {"main": SHA_A, "prod": SHA_B}


def _proc(
    stdout: str = "", rc: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


def _sub(args: list[str]) -> str:
    """The git subcommand under ``git <TRUSTED_ARGS> [-C <dir>] <sub> ...``."""
    rest = args[1 + len(fgm.TRUSTED_ARGS) :]
    if rest[:1] == ["-C"]:
        rest = rest[2:]
    return rest[0]


def _ls_remote_text(tips: dict[str, str]) -> str:
    return "".join(f"{sha}\trefs/heads/{ref}\n" for ref, sha in tips.items())


def _real_run(
    args: list[str], cwd: Path, env: dict, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        env=dict(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )


# ── layout / trusted env ───────────────────────────────────────────


def test_mirror_dirs_are_per_launch_and_the_cache_is_shared(tmp_path: Path) -> None:
    """Review finding 3 (regression): each launch gets its own mirror so a
    sibling populate cannot refresh away an unsynced squash; the object
    cache is shared."""
    assert fgm.mirror_dir(tmp_path, "td1") != fgm.mirror_dir(tmp_path, "td2")
    assert fgm.mirror_dir(tmp_path, "td1").parent == fgm.cache_dir(tmp_path).parent
    assert fgm.cache_dir(tmp_path).name == "cache.git"


def test_trusted_env_blinds_the_file_config() -> None:
    env = fgm.trusted_env({"PATH": "/x"})
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert env["PATH"] == "/x"


def test_launch_tips_record_lives_beside_the_mirror_and_drops(
    tmp_path: Path,
) -> None:
    """The launch record sits under the main checkout's scratch — beside
    the launch mirror, never in a slice tree — round-trips the launch
    tips, and ``drop_launch`` forgets record and mirror together."""
    path = fgm.launch_tips_path(tmp_path, "td1")
    assert path.parent == fgm.mirror_root(tmp_path)
    assert path.name == "td1.tips.json"
    assert fgm.read_launch_tips(tmp_path, "td1") is None
    assert fgm.write_launch_tips(tmp_path, "td1", TIPS)
    assert fgm.read_launch_tips(tmp_path, "td1") == TIPS
    fgm.mirror_dir(tmp_path, "td1").mkdir(parents=True)
    fgm.drop_launch(tmp_path, "td1")
    assert fgm.read_launch_tips(tmp_path, "td1") is None
    assert not fgm.mirror_dir(tmp_path, "td1").exists()


def test_read_launch_tips_present_but_unreadable_is_not_absent(
    tmp_path: Path,
) -> None:
    """A record that exists but cannot be read as tips answers ``{}`` —
    mirror mode with no CAS basis (the caller fails closed) — while a
    missing record answers ``None`` ("no mirror")."""
    path = fgm.launch_tips_path(tmp_path, "td1")
    path.parent.mkdir(parents=True)
    path.write_text("not json\n", encoding="utf-8")
    assert fgm.read_launch_tips(tmp_path, "td1") == {}
    path.write_text(json.dumps(["main", SHA_A]) + "\n", encoding="utf-8")
    assert fgm.read_launch_tips(tmp_path, "td1") == {}
    path.write_text(
        json.dumps({"main": SHA_A, "prod": "not-a-sha", "x": None}) + "\n",
        encoding="utf-8",
    )
    assert fgm.read_launch_tips(tmp_path, "td1") == {"main": SHA_A}


# ── populate ───────────────────────────────────────────────────────


def _populate_run(
    calls: list,
    *,
    oracle: dict[str, str] | None = TIPS,
    fetched: dict[str, str] | None = None,
    fetch_rc: int = 0,
    real_init: bool = False,
) -> object:
    """A fake git seam for one populate: the oracle ls-remote, the ops-repo
    fetches, the tmp→mirror push, rev-parse tips. ``fetched`` is what the
    ops repo ends up holding — set it different from ``oracle`` to model a
    tampered fetch. ``real_init`` runs a real ``git init`` (needed only
    when a test asserts on the created gitdir — it needs a real PATH)."""

    def fake_run(
        args: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        calls.append((list(args), Path(cwd), dict(env)))
        sub = _sub(args)
        if sub == "init" and real_init:
            return _real_run(args, cwd, env, timeout)
        if sub == "config":
            return _proc()
        if sub == "ls-remote":
            if oracle is None:
                return _proc(rc=1)
            return _proc(_ls_remote_text(oracle))
        if sub == "fetch":
            return _proc(rc=fetch_rc)
        if sub == "push":
            return _proc()
        if sub == "rev-parse":
            tips = fetched if fetched is not None else oracle
            if tips is None:
                return _proc(rc=1)
            ref = args[-1].removeprefix("refs/heads/")
            sha = tips.get(ref)
            return _proc((sha + "\n") if sha else "", rc=0 if sha else 1)
        return _proc()

    return fake_run


def test_populate_creates_per_launch_mirror_and_returns_oracle_tips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mirror = tmp_path / "scratch" / "factory-mirror" / "td42.git"
    cache = tmp_path / "scratch" / "factory-mirror" / "cache.git"
    calls: list = []
    monkeypatch.setattr(fgm, "_run", _populate_run(calls, real_init=True))
    tips = fgm.populate(
        mirror,
        cache,
        "https://github.com/example/precis.git",
        None,
        env={"PATH": os.environ["PATH"]},
    )
    assert tips == TIPS
    assert (mirror / "HEAD").exists() and (cache / "HEAD").exists()
    # the oracle answered first; the github fetch ran in an ops repo, not
    # in the mirror or any slice tree; the mirror got the refs by a LOCAL
    # tmp→mirror push
    subs = [_sub(a) for a, _c, _e in calls]
    assert subs.index("ls-remote") < subs.index("fetch")
    github_fetch = next(
        (a, c)
        for a, c, _e in calls
        if _sub(a) == "fetch" and "github.com" in " ".join(a)
    )
    assert github_fetch[0][-2:] == [
        "+refs/heads/main:refs/heads/main",
        "+refs/heads/prod:refs/heads/prod",
    ]
    assert str(github_fetch[1]).startswith(str(mirror.parent))
    assert github_fetch[1] != mirror
    local_push = next(a for a, _c, _e in calls if _sub(a) == "push")
    assert str(mirror) in local_push
    assert "github.com" not in " ".join(local_push)
    # review finding 2 (regression): every call ran hooks-off with no
    # system/global config and no refs/replace substitution — the
    # trusted boundary, argv and env halves alike
    for args, _cwd, env in calls:
        assert args[: 1 + len(fgm.TRUSTED_ARGS)] == ["git", *fgm.TRUSTED_ARGS]
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_NO_REPLACE_OBJECTS"] == "1"


def test_populate_adds_the_release_target_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    oracle = {"main": SHA_A, "prod": SHA_B, "release/r7": SHA_C}
    calls: list = []
    monkeypatch.setattr(fgm, "_run", _populate_run(calls, oracle=oracle))
    tips = fgm.populate(
        tmp_path / "m.git",
        tmp_path / "c.git",
        "https://github.com/example/precis.git",
        "release/r7",
        env={"PATH": "/x"},
    )
    assert tips == oracle
    github_fetch = next(
        a for a, _c, _e in calls if _sub(a) == "fetch" and "github.com" in " ".join(a)
    )
    assert github_fetch[-1] == "+refs/heads/release/r7:refs/heads/release/r7"


def test_populate_refuses_when_github_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(fgm, "_run", _populate_run([], oracle=None))
    assert (
        fgm.populate(
            tmp_path / "m.git",
            tmp_path / "c.git",
            "https://x",
            None,
            env={"PATH": "/x"},
        )
        is None
    )


def test_populate_refuses_a_fetch_that_disagrees_with_the_oracle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A redirected or tampered fetch must not seed the mirror: the ops
    repo's tips are compared against the oracle's answer, not trusted."""
    monkeypatch.setattr(
        fgm,
        "_run",
        _populate_run([], oracle=TIPS, fetched={"main": SHA_C, "prod": SHA_B}),
    )
    assert (
        fgm.populate(
            tmp_path / "m.git",
            tmp_path / "c.git",
            "https://x",
            None,
            env={"PATH": "/x"},
        )
        is None
    )


def test_populate_refuses_when_the_fetch_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(fgm, "_run", _populate_run([], fetch_rc=1))
    assert (
        fgm.populate(
            tmp_path / "m.git",
            tmp_path / "c.git",
            "https://x",
            None,
            env={"PATH": "/x"},
        )
        is None
    )


def test_populate_drops_the_ops_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mirror = tmp_path / "scratch" / "factory-mirror" / "td1.git"
    monkeypatch.setattr(fgm, "_run", _populate_run([]))
    fgm.populate(mirror, tmp_path / "c.git", "https://x", None, env={"PATH": "/x"})
    assert not [p for p in mirror.parent.iterdir() if p.name.startswith("ops-")]


# ── mirror_ship_env ────────────────────────────────────────────────


def test_mirror_ship_env_rewrites_the_origin_url_to_the_mirror() -> None:
    env = fgm.mirror_ship_env(
        Path("/var/m.git"), {"PATH": "/usr/bin"}, "https://github.com/o/r.git"
    )
    # ship's reads of the mirror and the shared dir ignore planted
    # refs/replace (pre-land review pass)
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
    # both the configured URL and its bare (no-.git) variant rewrite
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "url./var/m.git.insteadOf"
    assert env["GIT_CONFIG_VALUE_0"] == "https://github.com/o/r.git"
    assert env["GIT_CONFIG_KEY_1"] == "url./var/m.git.insteadOf"
    assert env["GIT_CONFIG_VALUE_1"] == "https://github.com/o/r"
    assert env["PATH"] == "/usr/bin"


def test_mirror_ship_env_without_a_dot_git_url_adds_one_entry() -> None:
    env = fgm.mirror_ship_env(Path("/var/m.git"), {}, "https://github.com/o/r")
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_VALUE_0"] == "https://github.com/o/r"


def test_mirror_ship_env_appends_after_existing_git_config() -> None:
    env = fgm.mirror_ship_env(
        Path("/var/m.git"),
        {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "user.name",
            "GIT_CONFIG_VALUE_0": "x",
            "GIT_CONFIG_KEY_1": "core.autocrlf",
            "GIT_CONFIG_VALUE_1": "false",
        },
        "https://github.com/o/r.git",
    )
    assert env["GIT_CONFIG_COUNT"] == "4"
    assert env["GIT_CONFIG_KEY_2"] == "url./var/m.git.insteadOf"
    assert env["GIT_CONFIG_VALUE_2"] == "https://github.com/o/r.git"
    # never a credential key of any kind
    for i in range(4):
        assert not env[f"GIT_CONFIG_KEY_{i}"].startswith("credential.")


def test_ship_env_answers_no_credential_and_origin_resolves_to_the_mirror(
    tmp_path: Path,
) -> None:
    """Review finding 2 (regression), end to end with a real git: a
    credential fill for github.com under the ship env gets NO password,
    and a repo whose configured origin is the real GitHub pushes and
    ls-remotes ``origin`` into the local mirror — the ``url.<mirror>.
    insteadOf`` rewrite applies at the transport layer (a plain
    ``remote.origin.url`` override is ignored by fetch/push: git resolves
    a remote's URL without consulting command-scope config)."""
    mirror = tmp_path / "mirror.git"
    init = subprocess.run(
        ["git", "init", "--bare", str(mirror)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert init.returncode == 0, init.stderr
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["git", "init"],
        ["git", "remote", "add", "origin", "https://github.com/example/real.git"],
    ):
        cp = subprocess.run(
            args,
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )
        assert cp.returncode == 0, cp.stderr
    # a commit to push: the rewritten push must land in the mirror
    (repo / "f.txt").write_text("hi\n", encoding="utf-8")
    for args in (
        ["git", "add", "f.txt"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
    ):
        cp = subprocess.run(
            args,
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )
        assert cp.returncode == 0, cp.stderr
    env = fgm.mirror_ship_env(
        mirror,
        {"PATH": os.environ["PATH"], "GIT_TERMINAL_PROMPT": "0"},
        "https://github.com/example/real.git",
    )
    # no credential helper answers a github fill (prompting disabled, so
    # a fill that found nothing errors out instead of hanging — the
    # security property is that no credential is ever produced)
    fill = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=30,
    )
    assert "password=" not in fill.stdout
    assert "username=" not in fill.stdout
    # fetch and push against `origin` speak to the mirror, not GitHub
    pushed = subprocess.run(
        ["git", "-C", str(repo), "push", "origin", "HEAD:refs/heads/main"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=30,
    )
    assert pushed.returncode == 0, pushed.stderr
    remote_head = subprocess.run(
        ["git", "-C", str(mirror), "rev-parse", "refs/heads/main"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert remote_head.returncode == 0, remote_head.stderr
    assert remote_head.stdout.strip()
    ls = subprocess.run(
        ["git", "-C", str(repo), "ls-remote", "origin"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=30,
    )
    assert ls.returncode == 0, ls.stderr
    assert "refs/heads/main" in ls.stdout


# ── github_url / ls_remote ─────────────────────────────────────────


def test_github_url_prefers_the_operator_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(fgm.ORIGIN_URL_ENV, "https://github.com/pin/repo.git")
    monkeypatch.setattr(fgm, "_run", lambda *a, **k: _proc(rc=1))
    assert fgm.github_url(tmp_path, env={"PATH": "/x"}) == (
        "https://github.com/pin/repo.git"
    )


def test_github_url_reads_the_main_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(fgm.ORIGIN_URL_ENV, raising=False)
    seen: list[tuple[list[str], Path]] = []

    def fake_run(
        args: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        seen.append((list(args), Path(cwd)))
        return _proc("https://github.com/example/precis.git\n")

    monkeypatch.setattr(fgm, "_run", fake_run)
    url = fgm.github_url(tmp_path, env={"PATH": "/x"})
    assert url == "https://github.com/example/precis.git"
    [(args, cwd)] = seen
    assert args[-3:] == ["remote", "get-url", "origin"]
    assert cwd == tmp_path


def test_github_url_rejects_a_non_github_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The credential is https/github.com-scoped: an ``ssh://`` (or any
    other) configured URL is a redirect, never a fetch source."""
    monkeypatch.delenv(fgm.ORIGIN_URL_ENV, raising=False)
    monkeypatch.setattr(
        fgm, "_run", lambda *a, **k: _proc("ssh://git@evil.example/repo.git\n")
    )
    assert fgm.github_url(tmp_path, env={"PATH": "/x"}) is None
    monkeypatch.setenv(fgm.ORIGIN_URL_ENV, "https://evil.example/repo.git")
    assert fgm.github_url(tmp_path, env={"PATH": "/x"}) is None


def test_github_url_is_none_when_git_cannot_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(fgm.ORIGIN_URL_ENV, raising=False)
    monkeypatch.setattr(fgm, "_run", lambda *a, **k: _proc(rc=1))
    assert fgm.github_url(tmp_path, env={"PATH": "/x"}) is None


def test_ls_remote_parses_and_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []

    def fake_run(
        args: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        seen.append(list(args))
        assert args[: 1 + len(fgm.TRUSTED_ARGS)] == ["git", *fgm.TRUSTED_ARGS]
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
        return _proc(_ls_remote_text(TIPS))

    monkeypatch.setattr(fgm, "_run", fake_run)
    assert fgm.ls_remote("https://x", ("main", "prod"), env={"PATH": "/x"}) == TIPS
    assert seen[0][-2:] == ["refs/heads/main", "refs/heads/prod"]
    monkeypatch.setattr(fgm, "_run", lambda *a, **k: _proc(rc=1))
    assert fgm.ls_remote("https://x", ("main",), env={"PATH": "/x"}) is None


# ── changed_refs / sync ────────────────────────────────────────────


def _sync_run(
    mirror_tip: dict[str, str],
    *,
    push_rc: int = 0,
    remote_sha: str | None = SHA_A,
    ancestor_rc: int = 0,
    pushes: list[list[str]] | None = None,
) -> object:
    """A fake git seam for one sync: mirror ref reads, the merge-base
    ancestry check, the ops-repo init, one leased push, the ls-remote
    oracle read-back."""

    def fake_run(
        args: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        sub = _sub(args)
        if sub == "init":
            return _proc()
        if sub == "rev-parse":
            ref = args[-1].removeprefix("refs/heads/")
            sha = mirror_tip.get(ref)
            return _proc((sha + "\n") if sha else "", rc=0 if sha else 1)
        if sub == "merge-base":
            return _proc(rc=ancestor_rc)
        if sub == "push":
            if pushes is not None:
                pushes.append(list(args))
            return _proc(rc=push_rc)
        if sub == "ls-remote":
            if remote_sha is None:
                return _proc(rc=1)
            return _proc(f"{remote_sha}\trefs/heads/main\n")
        return _proc(rc=1)

    return fake_run


def test_sync_no_refs_moved_is_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(fgm, "_run", _sync_run(dict(TIPS)))
    outcome, detail = fgm.sync(
        tmp_path / "m.git", None, "https://x", dict(TIPS), expect={}, env={"PATH": "/x"}
    )
    assert outcome == "ok"
    assert "no land refs moved" in detail


def test_sync_refuses_a_claimed_land_the_mirror_did_not_move(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review finding 3 (regression): "no land refs moved" while ship
    reported a landed sha is a refusal, never a recorded land."""
    monkeypatch.setattr(fgm, "_run", _sync_run(dict(TIPS)))
    outcome, detail = fgm.sync(
        tmp_path / "m.git",
        None,
        "https://x",
        dict(TIPS),
        expect={"main": SHA_C},
        env={"PATH": "/x"},
    )
    assert outcome == "error"
    assert "no land refs moved" in detail


def test_sync_pushes_the_claimed_sha_with_the_launch_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pushes: list[list[str]] = []
    monkeypatch.setattr(
        fgm,
        "_run",
        _sync_run({"main": SHA_C, "prod": SHA_B}, remote_sha=SHA_C, pushes=pushes),
    )
    outcome, detail = fgm.sync(
        tmp_path / "m.git",
        None,
        "https://x",
        dict(TIPS),
        expect={"main": SHA_C},
        env={"PATH": "/x"},
    )
    assert outcome == "ok"
    assert detail == "main → origin"
    [(push)] = pushes
    assert _sub(push) == "push"
    assert f"{SHA_C}:refs/heads/main" in push
    assert f"--force-with-lease=main:{SHA_A}" in push


def test_sync_refuses_a_moved_ref_ship_never_claimed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """prod moved in the mirror while ship claimed only main — nothing
    ship did not report is pushed."""
    monkeypatch.setattr(
        fgm,
        "_run",
        _sync_run({"main": SHA_C, "prod": SHA_A}, remote_sha=SHA_C),
    )
    outcome, detail = fgm.sync(
        tmp_path / "m.git",
        None,
        "https://x",
        dict(TIPS),
        expect={"main": SHA_C},
        env={"PATH": "/x"},
    )
    assert outcome == "error"
    assert "prod" in detail


def test_sync_refuses_a_mirror_tip_that_is_not_the_claimed_sha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        fgm, "_run", _sync_run({"main": SHA_B, "prod": SHA_B}, remote_sha=SHA_B)
    )
    outcome, detail = fgm.sync(
        tmp_path / "m.git",
        None,
        "https://x",
        dict(TIPS),
        expect={"main": SHA_C},
        env={"PATH": "/x"},
    )
    assert outcome == "error"
    assert "not the sha ship reported" in detail


def test_sync_refuses_a_tip_that_does_not_descend_from_the_launch_tip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        fgm,
        "_run",
        _sync_run({"main": SHA_C, "prod": SHA_B}, remote_sha=SHA_C, ancestor_rc=1),
    )
    outcome, detail = fgm.sync(
        tmp_path / "m.git",
        None,
        "https://x",
        dict(TIPS),
        expect={"main": SHA_C},
        env={"PATH": "/x"},
    )
    assert outcome == "error"
    assert "does not descend" in detail


def test_sync_raced_when_origin_moved_under_the_land(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        fgm,
        "_run",
        _sync_run({"main": SHA_C, "prod": SHA_B}, push_rc=1, remote_sha="d" * 40),
    )
    outcome, detail = fgm.sync(
        tmp_path / "m.git",
        None,
        "https://x",
        dict(TIPS),
        expect={"main": SHA_C},
        env={"PATH": "/x"},
    )
    assert outcome == "raced"
    assert "main moved on origin" in detail


def test_sync_ok_when_the_sha_is_already_there(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # push refused (network blip) but a same-sha reland already placed it
    monkeypatch.setattr(
        fgm,
        "_run",
        _sync_run({"main": SHA_C, "prod": SHA_B}, push_rc=1, remote_sha=SHA_C),
    )
    outcome, _detail = fgm.sync(
        tmp_path / "m.git",
        None,
        "https://x",
        dict(TIPS),
        expect={"main": SHA_C},
        env={"PATH": "/x"},
    )
    assert outcome == "ok"


def test_sync_races_when_origin_moved_past_a_green_push(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The push succeeded but the oracle shows a different sha: the land
    record would name a sha origin does not hold — defer, do not write."""
    other = "d" * 40
    monkeypatch.setattr(
        fgm,
        "_run",
        _sync_run({"main": SHA_C, "prod": SHA_B}, remote_sha=other),
    )
    outcome, detail = fgm.sync(
        tmp_path / "m.git",
        None,
        "https://x",
        dict(TIPS),
        expect={"main": SHA_C},
        env={"PATH": "/x"},
    )
    assert outcome == "raced"
    assert "after the push" in detail


def test_sync_errors_when_github_is_unreadable_after_a_push_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        fgm,
        "_run",
        _sync_run({"main": SHA_C, "prod": SHA_B}, push_rc=1, remote_sha=None),
    )
    outcome, detail = fgm.sync(
        tmp_path / "m.git",
        None,
        "https://x",
        dict(TIPS),
        expect={"main": SHA_C},
        env={"PATH": "/x"},
    )
    assert outcome == "error"
    assert "unreadable" in detail


def test_sync_retries_while_the_lease_still_names_the_launch_tip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A refused push with origin still on the launch tip retries once
    (the git_network reconcile), then a green push lands it."""
    pushes = 0

    def fake_run(
        args: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        nonlocal pushes
        sub = _sub(args)
        if sub == "init":
            return _proc()
        if sub == "rev-parse":
            ref = args[-1].removeprefix("refs/heads/")
            sha = {"main": SHA_C, "prod": SHA_B}.get(ref)
            return _proc((sha + "\n") if sha else "", rc=0 if sha else 1)
        if sub == "merge-base":
            return _proc()
        if sub == "push":
            pushes += 1
            return _proc(rc=0 if pushes == 2 else 1)
        if sub == "ls-remote":
            return _proc(f"{SHA_A if pushes < 2 else SHA_C}\trefs/heads/main\n")
        return _proc(rc=1)

    monkeypatch.setattr(fgm, "_run", fake_run)
    outcome, _detail = fgm.sync(
        tmp_path / "m.git",
        None,
        "https://x",
        dict(TIPS),
        expect={"main": SHA_C},
        env={"PATH": "/x"},
    )
    assert outcome == "ok"
    assert pushes == 2


def test_sync_fails_closed_when_a_mirror_tip_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(fgm, "_run", lambda *a, **k: _proc(rc=1))
    outcome, detail = fgm.sync(
        tmp_path / "m.git",
        None,
        "https://x",
        dict(TIPS),
        expect={"main": SHA_C},
        env={"PATH": "/x"},
    )
    assert outcome == "error"
    assert "unreadable" in detail


# ── repair_origin ──────────────────────────────────────────────────


def test_repair_origin_resets_tracking_refs_and_rewinds_the_phantom(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review finding 4 (regression): after a sync that did not place the
    land, the shared dir's ``origin/*`` are pushed back to GitHub's real
    tips and a local main sitting on the unpushed squash is rewound."""
    common = tmp_path / "gitdir"
    pushes: list[list[str]] = []
    updated: list[list[str]] = []

    def fake_run(
        args: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        sub = _sub(args)
        if sub == "ls-remote":
            return _proc(_ls_remote_text(TIPS))
        if sub == "fetch":
            return _proc()
        if sub == "push":
            pushes.append(list(args))
            return _proc()
        if sub == "rev-parse":
            return _proc(SHA_C + "\n")
        if sub == "worktree":
            return _proc("")
        if sub == "update-ref":
            updated.append(list(args))
            return _proc()
        return _proc()

    monkeypatch.setattr(fgm, "_run", fake_run)
    notes = fgm.repair_origin(
        common, "https://x", ("main", "prod"), phantom_main=SHA_C, env={"PATH": "/x"}
    )
    ref_writes = [a for a in pushes if any("refs/remotes/origin/" in x for x in a)]
    assert {x for a in ref_writes for x in a if ":refs/remotes/" in x} == {
        f"{SHA_A}:refs/remotes/origin/main",
        f"{SHA_B}:refs/remotes/origin/prod",
    }
    assert any("refs/heads/main" in x for a in updated for x in a)
    assert any("rewound" in n for n in notes)


def test_repair_origin_leaves_a_main_that_is_not_the_phantom(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    updated: list[list[str]] = []

    def fake_run(
        args: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        sub = _sub(args)
        if sub == "ls-remote":
            return _proc(_ls_remote_text(TIPS))
        if sub in ("fetch", "push"):
            return _proc()
        if sub == "rev-parse":
            return _proc(SHA_B + "\n")  # local main is NOT the phantom
        if sub == "worktree":
            return _proc("")
        if sub == "update-ref":
            updated.append(list(args))
            return _proc()
        return _proc()

    monkeypatch.setattr(fgm, "_run", fake_run)
    notes = fgm.repair_origin(
        tmp_path / "gitdir",
        "https://x",
        ("main",),
        phantom_main=SHA_C,
        env={"PATH": "/x"},
    )
    assert updated == []
    assert any("left alone" in n for n in notes)


def test_repair_origin_reports_when_github_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(fgm, "_run", lambda *a, **k: _proc(rc=1))
    notes = fgm.repair_origin(
        tmp_path / "gitdir",
        "https://x",
        ("main",),
        phantom_main=None,
        env={"PATH": "/x"},
    )
    assert notes == ["origin repair: GitHub unreadable — refs left for the relaunch"]


# ── local_env / deferred local-main (review td660513, second pass) ──


def _authed() -> dict[str, str]:
    """An env carrying the credential wiring ``git_auth_env`` adds."""
    return {
        "PATH": "/x",
        "PRECIS_FACTORY_GIT_TOKEN": "tok",
        "GH_TOKEN": "ghp",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "!f() { :; }; f",
    }


_CRED_NAMES = (
    "PRECIS_FACTORY_GIT_TOKEN",
    "GH_TOKEN",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_VALUE_0",
)


def _github_facing(args: list[str], url: str) -> bool:
    """A call that can reach GitHub — the oracle or a fetch/push naming
    the remote url — as opposed to a local-path or in-repo op."""
    return _sub(args) == "ls-remote" or (
        _sub(args) in ("fetch", "push") and url in args
    )


def test_local_env_scrubs_the_credential_names() -> None:
    """Second-pass review (regression): a repair op's spawned children —
    an fsmonitor hook, a clean/smudge filter, a local-path fetch's
    upload-pack ``packObjectsHook`` — run whatever the adversary-writable
    local config names, so the env they inherit carries no token and no
    command-scope wiring."""
    env = fgm.local_env({**_authed(), "PRECIS_GATE_N": "3"})
    for name in _CRED_NAMES:
        assert name not in env, name
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert env["PRECIS_GATE_N"] == "3"
    assert "tok" not in env.values()


def test_repair_origin_keeps_the_token_off_the_local_ops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Second-pass review (regression): inside repair_origin only the
    oracle and the fetch from origin run under the credential env — the
    ops-repo init, the priming fetch (whose spawned upload-pack reads
    the common dir's writable config), the tracking-ref pushes, the
    primary worktree's status/reset and the ref moves run
    credential-free."""
    url = "https://github.com/example/precis.git"
    seen: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(
        args: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        seen.append((list(args), dict(env)))
        sub = _sub(args)
        if sub == "ls-remote":
            return _proc(_ls_remote_text(TIPS))
        if sub == "rev-parse":
            return _proc(SHA_C + "\n")  # local main IS the phantom
        if sub == "worktree":
            return _proc("worktree /primary\n")
        if sub == "symbolic-ref":
            return _proc("main\n")
        if sub == "status":
            return _proc("")  # clean → the reset --hard path
        return _proc()

    monkeypatch.setattr(fgm, "_run", fake_run)
    notes = fgm.repair_origin(
        tmp_path / "gitdir", url, ("main", "prod"), phantom_main=SHA_C, env=_authed()
    )
    assert any("worktree reset" in n for n in notes)
    assert seen
    github = [e for a, e in seen if _github_facing(a, url)]
    local = [e for a, e in seen if not _github_facing(a, url)]
    assert github and all(e["PRECIS_FACTORY_GIT_TOKEN"] == "tok" for e in github)
    assert local
    for env in local:
        for name in _CRED_NAMES:
            assert name not in env, name


def test_populate_keeps_the_token_off_the_local_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Second-pass review (regression): the populate's credential is
    confined to the oracle and the fetch from GitHub — the bare inits,
    the cache-priming fetch (whose spawned upload-pack reads the cache's
    writable config), the tmp→mirror push and the tip reads run
    credential-free."""
    url = "https://github.com/example/precis.git"
    calls: list = []
    monkeypatch.setattr(fgm, "_run", _populate_run(calls))
    tips = fgm.populate(
        tmp_path / "m.git", tmp_path / "c.git", url, None, env=_authed()
    )
    assert tips == TIPS
    github = [e for a, _c, e in calls if _github_facing(a, url)]
    local = [e for a, _c, e in calls if not _github_facing(a, url)]
    assert github and all(e["PRECIS_FACTORY_GIT_TOKEN"] == "tok" for e in github)
    assert local
    for env in local:
        for name in _CRED_NAMES:
            assert name not in env, name


def test_advance_local_main_merges_ff_only_in_a_main_primary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Finding 4, second pass: the deferred advance replays ship's move —
    a primary that checks ``main`` out gets ``merge --ff-only <sha>``
    (ref and worktree together), every call credential-free."""
    calls: list = []

    def fake_run(
        args: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        calls.append((list(args), dict(env)))
        sub = _sub(args)
        if sub == "worktree":
            return _proc("worktree /primary\n")
        if sub == "symbolic-ref":
            return _proc("main\n")
        return _proc()

    monkeypatch.setattr(fgm, "_run", fake_run)
    note = fgm.advance_local_main(tmp_path / "gitdir", SHA_C, env=_authed())
    merges = [a for a, _e in calls if _sub(a) == "merge"]
    assert merges and merges[0][-2:] == ["--ff-only", SHA_C]
    assert SHA_C[:9] in note
    for _a, env in calls:
        for name in _CRED_NAMES:
            assert name not in env, name


def test_advance_local_main_moves_the_ref_when_main_is_not_checked_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ship's fallback shape: a primary not on ``main`` gets the bare ref
    update in the shared dir."""
    calls: list = []

    def fake_run(
        args: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        sub = _sub(args)
        if sub == "worktree":
            return _proc("worktree /primary\n")
        if sub == "symbolic-ref":
            return _proc("slice/9\n")
        return _proc()

    monkeypatch.setattr(fgm, "_run", fake_run)
    note = fgm.advance_local_main(tmp_path / "gitdir", SHA_C, env={})
    update = [a for a in calls if _sub(a) == "update-ref"]
    assert update and update[0][-2:] == ["refs/heads/main", SHA_C]
    assert SHA_C[:9] in note


@pytest.mark.skipif(sys.platform == "win32", reason="the shim is a POSIX sh script")
def test_ship_git_shim_refuses_only_the_local_main_advance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Finding 4, second pass (regression): the shim on ship's PATH
    refuses exactly the calls that move the shared local main — the
    primary-checkout ``merge --ff-only`` and the ``update-ref
    refs/heads/main`` fallback — and execs real git for the rest."""
    real = tmp_path / "real-git"
    real.write_text("#!/bin/sh\nprintf 'real-git %s\\n' \"$1\"\n", encoding="utf-8")
    real.chmod(0o755)
    monkeypatch.setattr(fgm.shutil, "which", lambda name: str(real))
    bindir = fgm.ship_git_shim(tmp_path / "launch.git")
    assert bindir == tmp_path / "launch.git" / "ship-bin"
    shim = bindir / "git"
    assert shim.stat().st_mode & 0o111
    for argv in (
        ["-C", "/primary", "merge", "--ff-only", "origin/main"],
        ["update-ref", "refs/heads/main", "origin/main"],
    ):
        cp = subprocess.run(
            [str(shim), *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )
        assert cp.returncode == 1, argv
        assert "deferred to the lander" in cp.stderr
    for argv in (
        ["status"],
        # the in-worktree forward merge ship does mid-race: not --ff-only
        ["-C", "/tree", "merge", "--no-edit", "origin/main"],
        ["merge-base", "origin/main", "HEAD"],
        # the absorbed-agent cleanup's own update-ref deletes branches
        ["update-ref", "-d", "refs/heads/agent/x", SHA_A],
        ["push", "origin", "HEAD:refs/heads/main"],
    ):
        cp = subprocess.run(
            [str(shim), *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )
        assert cp.returncode == 0 and cp.stdout.startswith("real-git "), argv


# ── planted refs/replace (pre-land pass) ───────────────────────────


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    cp = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=env or {"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


def test_trusted_reads_ignore_a_planted_replace_ref(tmp_path: Path) -> None:
    """Pre-land review pass (regression), end to end with a real git:
    the mirror, the shared dir and the worktree gitdir are same-uid
    writable, so a ``refs/replace/*`` planted for an oracle-verified sha
    must not substitute the object a trusted read resolves — the drift
    helper's ``git show <tip>:…`` under the token, and the sync's
    ``merge-base --is-ancestor`` descent check, were the named holes.
    A plain read IS fooled (the attack is real); TRUSTED_ARGS and the
    trusted env are not."""
    repo = tmp_path / "repo"
    repo.mkdir()
    ident = ["-c", "user.email=t@t", "-c", "user.name=t"]
    _git(repo, "init")
    (repo / "helper.py").write_text("# real main helper\n", encoding="utf-8")
    _git(repo, "add", "helper.py")
    _git(repo, *ident, "commit", "-qm", "real")
    real_tip = _git(repo, "rev-parse", "HEAD")
    # the attacker's content, committed normally then grafted over the
    # verified tip via refs/replace/<tip>
    (repo / "helper.py").write_text(
        "# attacker helper — must never run\n", encoding="utf-8"
    )
    _git(repo, "add", "helper.py")
    _git(repo, *ident, "commit", "-qm", "attacker")
    attacker = _git(repo, "rev-parse", "HEAD")
    _git(repo, "replace", real_tip, attacker)
    # a plain `git show <tip>:…` serves the replacement's content — the
    # helper-substitution hole the review named
    assert "attacker" in _git(repo, "show", f"{real_tip}:helper.py")
    trusted = fgm.trusted_env({"PATH": os.environ["PATH"]})
    cp = subprocess.run(
        ["git", *fgm.TRUSTED_ARGS, "show", f"{real_tip}:helper.py"],
        cwd=str(repo),
        env=trusted,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "# real main helper"
    # the env half alone blinds a read without the argv flag too
    assert (
        _git(repo, "show", f"{real_tip}:helper.py", env=trusted) == "# real main helper"
    )
    # ancestry: a foreign commit with a replacement grafting real_tip as
    # its parent spoofs `--is-ancestor` under a plain read only
    _git(repo, "checkout", "--orphan", "other")
    _git(repo, "rm", "-rf", ".")
    (repo / "x").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "x")
    _git(repo, *ident, "commit", "-qm", "foreign")
    foreign = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", f"{foreign}^{{tree}}")
    grafted = _git(repo, *ident, "commit-tree", tree, "-p", real_tip, "-m", "grafted")
    _git(repo, "replace", foreign, grafted)
    plain = subprocess.run(
        ["git", "merge-base", "--is-ancestor", real_tip, foreign],
        cwd=str(repo),
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert plain.returncode == 0, "the planted replacement did not spoof ancestry"
    blind = subprocess.run(
        ["git", *fgm.TRUSTED_ARGS, "merge-base", "--is-ancestor", real_tip, foreign],
        cwd=str(repo),
        env=trusted,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert blind.returncode == 1, "a trusted read followed the planted replace"


def test_ship_git_shim_none_without_a_resolvable_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(fgm.shutil, "which", lambda name: None)
    assert fgm.ship_git_shim(tmp_path / "m.git") is None


# ── fetch_ref_into / push_cas (catpath lander) ─────────────────────


_IDENT = ["-c", "user.email=t@t", "-c", "user.name=t"]


def _real_env() -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "PRECIS_FACTORY_GIT_TOKEN": "tok",
        "GIT_CONFIG_COUNT": "0",
    }


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name + "\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, *_IDENT, "commit", "-qm", name)
    return _git(repo, "rev-parse", "HEAD")


def _lander_repos(tmp_path: Path) -> tuple[Path, Path, str]:
    """A bare 'GitHub' with one commit on main and a writable clone of it."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    tip = _commit(seed, "one")
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(remote))
    dest = tmp_path / "dest"
    _git(tmp_path, "clone", "-q", str(remote), str(dest))
    return remote, dest, tip


def _record_real(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[list[str], Path, dict[str, str]]]:
    calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def run(
        args: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        calls.append((list(args), cwd, dict(env)))
        return _real_run(args, cwd, env, timeout)

    monkeypatch.setattr(fgm, "_run", run)
    return calls


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def test_fetch_ref_into_writes_the_tracking_ref_and_drops_the_ops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    remote, dest, tip = _lander_repos(tmp_path)
    new = _commit(dest, "two")  # a second commit, pushed to the remote
    _git(dest, "push", "-q", str(remote), "HEAD:refs/heads/main")
    _git(dest, "update-ref", "refs/remotes/origin/main", tip)
    calls = _record_real(monkeypatch)
    ops_parent = tmp_path / "ops"
    got = fgm.fetch_ref_into(
        dest,
        str(remote),
        "main",
        "refs/remotes/origin/main",
        ops_parent=ops_parent,
        env=_real_env(),
    )
    assert got == new
    assert _git(dest, "rev-parse", "refs/remotes/origin/main") == new
    assert list(ops_parent.iterdir()) == []
    # the credential never ran git inside the writable clone
    cred = [(a, c) for a, c, e in calls if "PRECIS_FACTORY_GIT_TOKEN" in e]
    assert cred
    for args, cwd in cred:
        assert not _inside(cwd, dest), args
        if "-C" in args:
            assert not _inside(Path(args[args.index("-C") + 1]), dest), args


def test_fetch_ref_into_refuses_when_the_oracle_disagrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    remote, dest, tip = _lander_repos(tmp_path)
    before = _git(dest, "rev-parse", "refs/remotes/origin/main")
    monkeypatch.setattr(fgm, "ls_remote", lambda *a, **k: {"main": SHA_C})
    ops_parent = tmp_path / "ops"
    got = fgm.fetch_ref_into(
        dest,
        str(remote),
        "main",
        "refs/remotes/origin/main",
        ops_parent=ops_parent,
        env=_real_env(),
    )
    assert got is None
    assert _git(dest, "rev-parse", "refs/remotes/origin/main") == before == tip
    assert list(ops_parent.iterdir()) == []


def test_fetch_ref_into_is_none_when_the_remote_is_unreadable(
    tmp_path: Path,
) -> None:
    _remote, dest, _tip = _lander_repos(tmp_path)
    ops_parent = tmp_path / "ops"
    got = fgm.fetch_ref_into(
        dest,
        str(tmp_path / "missing.git"),
        "main",
        "refs/remotes/origin/main",
        ops_parent=ops_parent,
        env=_real_env(),
    )
    assert got is None
    assert list(ops_parent.iterdir()) == []


def _push_setup(tmp_path: Path) -> tuple[Path, Path, str, str]:
    remote, dest, tip = _lander_repos(tmp_path)
    sha = _commit(dest, "two")
    return remote, dest, tip, sha


def _cas(
    remote: Path, dest: Path, sha: str, tip: str, tmp_path: Path
) -> tuple[str, str]:
    return fgm.push_cas(
        str(remote),
        "main",
        sha,
        tip,
        ops_parent=tmp_path / "ops",
        env=_real_env(),
        object_dirs=(dest / ".git" / "objects",),
    )


def _pushes(calls: list[tuple[list[str], Path, dict[str, str]]]) -> list[list[str]]:
    return [a for a, _c, _e in calls if _sub(a) == "push"]


def test_push_cas_fast_forwards_without_any_force(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    remote, dest, tip, sha = _push_setup(tmp_path)
    calls = _record_real(monkeypatch)
    assert _cas(remote, dest, sha, tip, tmp_path) == ("ok", "main")
    assert _git(remote, "rev-parse", "refs/heads/main") == sha
    pushes = _pushes(calls)
    assert len(pushes) == 1
    for arg in pushes[0]:
        assert not arg.startswith("--force"), arg
        assert not arg.startswith("+"), arg
    assert f"{sha}:refs/heads/main" in pushes[0]
    assert list((tmp_path / "ops").iterdir()) == []


def test_push_cas_refuses_a_sha_that_does_not_descend_from_the_tip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    remote, dest, tip, sha = _push_setup(tmp_path)
    calls = _record_real(monkeypatch)
    outcome, detail = _cas(remote, dest, tip, sha, tmp_path)  # reversed
    assert outcome == "error" and "descend" in detail
    assert _pushes(calls) == []
    assert _git(remote, "rev-parse", "refs/heads/main") == tip


def test_push_cas_races_when_the_remote_moved_elsewhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    remote, dest, tip, sha = _push_setup(tmp_path)
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(remote), str(other))
    moved = _commit(other, "elsewhere")
    _git(other, "push", "-q", "origin", "HEAD:refs/heads/main")
    calls = _record_real(monkeypatch)
    outcome, detail = _cas(remote, dest, sha, tip, tmp_path)
    assert outcome == "raced", detail
    assert _git(remote, "rev-parse", "refs/heads/main") == moved
    assert all("--force" not in a for a in _pushes(calls))


def test_push_cas_is_ok_without_a_push_when_the_sha_is_already_there(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    remote, dest, tip, sha = _push_setup(tmp_path)
    _git(dest, "push", "-q", str(remote), "HEAD:refs/heads/main")
    calls = _record_real(monkeypatch)
    assert _cas(remote, dest, sha, tip, tmp_path) == ("ok", "main")
    assert _pushes(calls) == []
