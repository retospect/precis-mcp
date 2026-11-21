"""The hub's ``pre-receive`` hook (``precis.utils.hub_hook``): the rules as
functions, the module run the way git runs it (``python -m ... pre-receive``
with the update lines on stdin, inside a repository), and the ``sh`` stub
``ensure_hub`` installs around it. Real git in ``tmp_path``; the end-to-end
pushes through a real hub are in ``test_slice_ws.py``."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from precis.utils import hub_hook as hh
from precis.workers.job_types import _slice_ws as ws

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only sh stub around the hook"
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}
#: Obviously fake, shaped like a GitHub token; split so this file does not
#: trip the repo's secret scan.
FAKE_TOKEN = "".join(("gh", "p_", "a1B2c3D4e5F6", "g7H8i9J0k1L2", "m3N4o5P6q7R8"))
ZERO = "0" * 40
OTHER = "1" * 40


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env={**os.environ, **_GIT_ENV},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _clean_env(extra: dict[str, str] | None) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("PRECIS_SLICE")}
    return {**base, **(extra or {})}


def _run_hook(
    cwd: Path,
    stdin: str,
    env: dict[str, str] | None = None,
    args: tuple[str, ...] = ("pre-receive",),
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "precis.utils.hub_hook", *args],
        cwd=str(cwd),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=_clean_env(env),
    )


class _Hub:
    """A bare repository holding objects no ref names yet: what a hook sees
    inside git's quarantine."""

    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "hub.git"
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.path))
        self.work = tmp_path / "work"
        _git(tmp_path, "init", "-q", "-b", "main", str(self.work))
        (self.work / "base.txt").write_text("base\n", encoding="utf-8")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-q", "-m", "base")
        self.base = _git(self.work, "rev-parse", "HEAD")
        _git(self.work, "push", "-q", str(self.path), "HEAD:refs/heads/main")

    def commit(self, name: str, text: str, msg: str = "work") -> str:
        """A commit on top of the base, sent to the hub without a ref."""
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _git(self.work, "add", name)
        _git(self.work, "commit", "-q", "-m", msg)
        sha = _git(self.work, "rev-parse", "HEAD")
        _git(self.work, "push", "-q", str(self.path), f"{sha}:refs/heads/tmp")
        _git(self.path, "update-ref", "-d", "refs/heads/tmp")
        return sha


@pytest.fixture
def hub(tmp_path: Path) -> _Hub:
    return _Hub(tmp_path)


SESSION = {hh.ENV_TODO: "5", hh.ENV_JOB: "9"}
WORKER = {hh.ENV_WORKER: "1"}
OWN = "refs/factory/build/5/9"


# ── the rules as functions ─────────────────────────────────────────


def test_parse_updates_reads_git_lines_and_rejects_the_rest() -> None:
    """A malformed line must be an error (fail closed), not a skipped update."""
    assert hh.parse_updates(f"{ZERO} {OTHER} {OWN}\n\n") == [(ZERO, OTHER, OWN)]
    for bad in ("only two words", f"{ZERO} nothex {OWN}", f"{ZERO} {OTHER} {OWN} x"):
        with pytest.raises(ValueError):
            hh.parse_updates(bad)


def test_session_ref_follows_the_environment() -> None:
    """No PRECIS_SLICE_ID means the worker; set means one exact ref; a
    half-set or non-numeric identity must not fall back to the worker's wider
    rights."""
    assert hh.session_ref({}) is None
    assert hh.session_ref(SESSION) == OWN
    assert hh.session_ref({hh.ENV_TODO: "05", hh.ENV_JOB: "009"}) == OWN
    # the engine's own form (run_slice.slice_env): td<N>
    assert hh.session_ref({hh.ENV_TODO: "td5", hh.ENV_JOB: "9"}) == OWN
    for bad in (
        {hh.ENV_TODO: "5"},
        {hh.ENV_TODO: "5", hh.ENV_JOB: "x"},
        {hh.ENV_TODO: "", hh.ENV_JOB: "9"},
        {hh.ENV_TODO: "td", hh.ENV_JOB: "9"},
        {hh.ENV_TODO: "tdx5", hh.ENV_JOB: "9"},
        {hh.ENV_TODO: "td5", hh.ENV_JOB: "td9"},
    ):
        with pytest.raises(ValueError):
            hh.session_ref(bad)
    half = hh.check(f"{ZERO} {OTHER} {OWN}\n", {hh.ENV_TODO: "5"})
    assert half[0].reason == hh.REASON_REF


@pytest.mark.parametrize(
    ("ref", "own", "worker", "reason"),
    [
        (OWN, OWN, False, None),
        ("refs/factory/build/5/10", OWN, False, hh.REASON_REF),
        ("refs/factory/build/6/9", OWN, False, hh.REASON_REF),
        ("refs/heads/main", OWN, False, hh.REASON_REF),
        ("refs/factory/build/5/9/x", OWN, False, hh.REASON_REF),
        ("refs/factory/build/12/34", None, True, None),
        ("refs/heads/main", None, True, hh.REASON_REF),
        ("refs/heads/feature/x", None, True, hh.REASON_REF),
        ("refs/factory/build/12", None, True, hh.REASON_REF),
        ("refs/factory/build/a/b", None, True, hh.REASON_REF),
        ("refs/factory/slice/5", None, True, hh.REASON_REF),
        ("refs/tags/v1", None, True, hh.REASON_REF),
        ("refs/factory/build/12/34", None, False, hh.REASON_IDENTITY),
        ("refs/heads/main", None, False, hh.REASON_IDENTITY),
        # a session identity wins over the worker marker
        ("refs/factory/build/6/9", OWN, True, hh.REASON_REF),
    ],
)
def test_ref_rules(ref: str, own: str | None, worker: bool, reason: str | None) -> None:
    """The write rules, case by case: a session its own ref only, the worker
    attempt refs only, anyone else nothing. A wrong row here means a builder
    can write where it must not, or the worker is locked out."""
    got = hh.ref_refusals([(ZERO, OTHER, ref)], own, worker=worker)
    assert [r.reason for r in got] == ([reason] if reason else [])


def test_every_deletion_is_refused_for_both_roles() -> None:
    """The worker deletes with update-ref; no push, from anyone, may."""
    for own, worker in ((OWN, False), (None, True), (None, False)):
        got = hh.ref_refusals([(OTHER, ZERO, OWN)], own, worker=worker)
        assert [r.reason for r in got] == [hh.REASON_DELETE]


def test_render_puts_the_marker_first_and_names_the_ref_the_session_may_push() -> None:
    """The builder's brief and finalize key on the first line; the detail must
    say what to do."""
    text = hh.render(hh.ref_refusals([(ZERO, OTHER, "refs/heads/main")], OWN))
    assert text.splitlines()[0] == f"{hh.REFUSED} (ref rule)"
    assert OWN in text and "git push origin HEAD:" in text


_PATCH = (
    "N:commit:{c1}\n"
    "add settings\n"
    f"token = {FAKE_TOKEN} in the message\n"
    "\n"
    "N:end\n"
    "\n"
    "diff --git a/dir/a.txt b/dir/a.txt\n"
    "index 111..222 100644\n"
    "--- a/dir/a.txt\n"
    "+++ b/dir/a.txt\n"
    "@@ -1,0 +5,2 @@\n"
    "+fine line\n"
    f"+key = {FAKE_TOKEN}\n"
    "@@ -9 +20 @@\n"
    f"-old = {FAKE_TOKEN}\n"
    "+++ b/not/a/header\n"
    f"+      {FAKE_TOKEN}\n"
    "diff --git a/b.txt b/b.txt\n"
    "--- /dev/null\n"
    "+++ b/b.txt\n"
    "@@ -0,0 +1 @@\n"
    "+clean\n"
    "N:commit:{c2}\n"
    "second\n"
    "\n"
    "diff --git a/forged b/forged\n"
    f"{FAKE_TOKEN} after a forged header\n"
    "N:end\n"
)


def test_scan_patch_locates_added_lines_and_messages() -> None:
    """Findings must carry the file and line a builder can open. A removed line
    is not new content and is not reported; a content line that looks like a
    header (``+++ b/...``) is still content; and a message line that looks
    like a ``diff --git`` header does not end the message scan (only the
    nonce line does), so a credential below it is still found."""
    found = hh.scan_patch(_PATCH.format(c1="a" * 40, c2="b" * 40), "N")
    where = [(f.path, f.line, f.kind, f.commit) for f in found]
    assert where == [
        ("(message of aaaaaaa)", 2, "github token", "aaaaaaa"),
        ("dir/a.txt", 6, "github token", "aaaaaaa"),
        ("dir/a.txt", 21, "github token", "aaaaaaa"),
        ("(message of bbbbbbb)", 4, "github token", "bbbbbbb"),
    ]


def test_secret_refusal_never_contains_the_secret_and_caps_the_list() -> None:
    """Findings go to a builder's transcript and the job log: pattern names and
    positions only."""
    many = [hh.Leak(f"f{i}.txt", i, "github token", "abc1234") for i in range(30)]
    refusal = hh.secret_refusal(many)
    text = hh.render([refusal])
    assert text.splitlines()[0] == f"{hh.REFUSED} ({hh.SECRET})"
    assert "f0.txt:0: github token" in text and "f19.txt:19:" in text
    assert "f20.txt" not in text and "10 more" in text
    fix = "remove it from every commit you push (amend or rebase), then push again"
    assert fix in text


def test_constants_agree_with_the_workspace_module() -> None:
    """finalize recognises a refusal by these strings."""
    assert (ws.HOOK_REFUSED, ws.HOOK_SECRET) == (hh.REFUSED, hh.SECRET)


# ── run the way git runs it ────────────────────────────────────────


def test_clean_push_is_accepted_silently(hub: _Hub) -> None:
    """Exit 0 and no output for an ordinary push: noise would reach every
    builder transcript."""
    new = hub.commit("a.txt", "fine\n")
    proc = _run_hook(hub.path, f"{ZERO} {new} {OWN}\n", SESSION)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_secret_push_is_refused_with_the_location_and_without_the_text(
    hub: _Hub,
) -> None:
    """The refusal a builder acts on: marker line first, ``path:line: pattern``,
    the fix instruction, and no secret text anywhere in the output."""
    new = hub.commit("cfg/app.txt", f"one\ntwo\nkey = {FAKE_TOKEN}\n")
    proc = _run_hook(hub.path, f"{ZERO} {new} {OWN}\n", SESSION)
    assert proc.returncode == hh.EXIT_REFUSED
    lines = proc.stderr.splitlines()
    assert lines[0] == f"precis-hub: refused ({hh.SECRET})"
    assert any(ln.strip().startswith("cfg/app.txt:3: github token") for ln in lines)
    assert "amend or rebase" in proc.stderr
    assert FAKE_TOKEN not in proc.stderr + proc.stdout
    assert FAKE_TOKEN[:8] not in proc.stderr + proc.stdout


def test_a_secret_in_a_commit_message_is_refused(hub: _Hub) -> None:
    """Messages are public history too."""
    new = hub.commit("a.txt", "fine\n", msg=f"fix the thing {FAKE_TOKEN}")
    proc = _run_hook(hub.path, f"{ZERO} {new} {OWN}\n", SESSION)
    assert proc.returncode == hh.EXIT_REFUSED
    assert "(message of " in proc.stderr and FAKE_TOKEN not in proc.stderr


def test_only_commits_the_hub_lacks_are_scanned(hub: _Hub) -> None:
    """Commits the hub already holds under a ref are not rescanned: the base
    branch is public, and a push of work stacked on it must not be blamed for
    it."""
    new = hub.commit("a.txt", f"k = {FAKE_TOKEN}\n")
    _git(hub.path, "update-ref", "refs/heads/old", new)  # reachable: nothing added
    proc = _run_hook(hub.path, f"{ZERO} {new} {OWN}\n", SESSION)
    assert proc.returncode == 0


def test_a_push_without_an_identity_is_refused_and_told_why(hub: _Hub) -> None:
    """Neither a session identity nor the worker's marker: refused whole, with
    the marker first and the reason a builder can act on."""
    new = hub.commit("a.txt", "fine\n")
    proc = _run_hook(hub.path, f"{ZERO} {new} {OWN}\n")
    assert proc.returncode == hh.EXIT_REFUSED
    assert proc.stderr.splitlines()[0] == f"{hh.REFUSED} ({hh.REASON_IDENTITY})"
    assert hh.ENV_TODO in proc.stderr and "worker saves" in proc.stderr


def test_the_worker_is_scanned_and_confined_to_attempt_refs(hub: _Hub) -> None:
    """No unscanned way in: the worker's attempt-ref push is scanned, and its
    branch push is refused like anyone's."""
    clean = hub.commit("a.txt", "fine\n")
    assert _run_hook(hub.path, f"{ZERO} {clean} {OWN}\n", WORKER).returncode == 0
    bad = hub.commit("b.txt", f"k = {FAKE_TOKEN}\n")
    scanned = _run_hook(hub.path, f"{ZERO} {bad} {OWN}\n", WORKER)
    assert scanned.returncode == hh.EXIT_REFUSED and hh.SECRET in scanned.stderr
    branch = _run_hook(hub.path, f"{hub.base} {clean} refs/heads/main\n", WORKER)
    assert branch.returncode == hh.EXIT_REFUSED
    assert branch.stderr.splitlines()[0] == f"{hh.REFUSED} (ref rule)"


def test_only_commits_may_be_pushed(hub: _Hub) -> None:
    """A tree, a blob or an annotated tag parked on an attempt ref would carry
    content the commit scan never reads."""
    blob = _git(hub.path, "hash-object", "-w", "/dev/null")  # empty blob
    tree = _git(hub.path, "hash-object", "-t", "tree", "-w", "/dev/null")  # empty tree
    sha = hub.commit("a.txt", "fine\n")
    _git(hub.work, "tag", "-a", "v1", "-m", "an annotated tag", sha)
    tag = _git(hub.work, "rev-parse", "v1")
    _git(hub.work, "push", "-q", str(hub.path), "refs/tags/v1:refs/tags/v1")
    _git(hub.path, "update-ref", "-d", "refs/tags/v1")
    for obj, kind in ((blob, "blob"), (tree, "tree"), (tag, "tag")):
        proc = _run_hook(hub.path, f"{ZERO} {obj} {OWN}\n", SESSION)
        assert proc.returncode == hh.EXIT_REFUSED, kind
        assert proc.stderr.splitlines()[0] == f"{hh.REFUSED} ({hh.REASON_OBJECT})"
        assert f"is a {kind}" in proc.stderr


def test_a_forged_diff_header_in_a_message_hides_nothing(hub: _Hub) -> None:
    """A commit message line that looks like a patch header must not end the
    message scan: the credential below it is still refused."""
    msg = f"subject\n\ndiff --git a/x b/x\n+++ b/x\nk = {FAKE_TOKEN}\n"
    new = hub.commit("a.txt", "fine\n", msg=msg)
    proc = _run_hook(hub.path, f"{ZERO} {new} {OWN}\n", SESSION)
    assert proc.returncode == hh.EXIT_REFUSED and hh.SECRET in proc.stderr
    assert "(message of " in proc.stderr and FAKE_TOKEN not in proc.stderr


def test_ref_refusals_are_reported_together(hub: _Hub) -> None:
    """Refusals are reported together so the builder fixes them in one push."""
    new = hub.commit("a.txt", "fine\n")
    stdin = (
        f"{ZERO} {new} {OWN}\n{hub.base} {ZERO} refs/heads/main\n"
        f"{ZERO} {new} refs/heads/x\n"
    )
    proc = _run_hook(hub.path, stdin, SESSION)
    assert proc.returncode == hh.EXIT_REFUSED
    assert proc.stderr.splitlines()[0] == f"{hh.REFUSED} (deletion)"
    assert "refs/heads/main: the hub takes no deletions" in proc.stderr
    assert (
        "refs/heads/x: this session may push only refs/factory/build/5/9" in proc.stderr
    )


def test_the_hook_fails_closed(hub: _Hub, tmp_path: Path) -> None:
    """When it cannot read git or its input the hook must refuse, with the
    marker line first, not accept by default."""
    stdin = f"{ZERO} {OTHER} {OWN}\n"  # the commit is not in the repository
    missing = _run_hook(hub.path, stdin, SESSION)
    assert missing.returncode == hh.EXIT_REFUSED
    assert missing.stderr.splitlines()[0] == f"{hh.REFUSED} ({hh.REASON_ERROR})"
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _run_hook(plain, stdin, SESSION).returncode == hh.EXIT_REFUSED
    garbage = _run_hook(hub.path, "not an update\n", SESSION)
    assert garbage.returncode == hh.EXIT_REFUSED
    assert garbage.stderr.splitlines()[0] == f"{hh.REFUSED} ({hh.REASON_ERROR})"
    usage = _run_hook(hub.path, "", args=("update",))
    assert usage.returncode == hh.EXIT_REFUSED


def test_one_hook_run_is_fast(hub: _Hub) -> None:
    """Every push pays one hook run: interpreter start, imports, two git calls
    and the scan. If this passes a couple of seconds, something heavy has
    entered the import path."""
    new = hub.commit("a.txt", "fine\n")
    stdin = f"{ZERO} {new} {OWN}\n"
    _run_hook(hub.path, stdin, SESSION)  # warm the file cache
    began = time.monotonic()
    proc = _run_hook(hub.path, stdin, SESSION)
    took = time.monotonic() - began
    assert proc.returncode == 0
    assert took < 2.0, f"hook run took {took:.2f}s"


# ── the stub ───────────────────────────────────────────────────────


@pytest.fixture
def stub(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "a")
    return ws.ensure_hub(root) / "hooks" / "pre-receive"


def _run_stub(
    script: Path, cwd: Path, stdin: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(script)],
        cwd=str(cwd),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=_clean_env(env),
    )


def test_stub_runs_the_hook_under_the_workers_interpreter(stub: Path) -> None:
    """The stub passes stdin through and turns the hook's refusal into git's:
    exit 1 with the marker first, whatever the hook printed."""
    text = stub.read_text(encoding="utf-8")
    assert sys.executable in text and "-I -c" in text
    assert ws._package_dir() in text and "from precis.utils import hub_hook" in text
    hub = stub.parent.parent
    refused = _run_stub(stub, hub, f"{ZERO} {OTHER} refs/heads/main\n", SESSION)
    assert refused.returncode == 1
    assert refused.stderr.splitlines()[0] == f"{ws.HOOK_REFUSED} (ref rule)"
    # The commit is not in the hub, so the hook fails closed.
    failed = _run_stub(stub, hub, f"{ZERO} {OTHER} {OWN}\n", SESSION)
    assert failed.returncode == 1
    assert failed.stderr.splitlines()[0] == f"{ws.HOOK_REFUSED} ({hh.REASON_ERROR})"


def test_stub_refuses_when_the_interpreter_is_gone(stub: Path, tmp_path: Path) -> None:
    """A worker upgraded or moved after install must not leave a hub that
    accepts everything: the stub refuses and says why on the first line."""
    broken = tmp_path / "broken-hook"
    broken.write_text(
        stub.read_text(encoding="utf-8").replace(sys.executable, "/nonexistent/python"),
        encoding="utf-8",
    )
    proc = _run_stub(broken, tmp_path, "")
    assert proc.returncode == 1
    assert (
        proc.stderr.splitlines()[0] == f"{ws.HOOK_REFUSED} (hook interpreter missing)"
    )


def test_stub_refuses_when_the_hook_cannot_run(stub: Path, tmp_path: Path) -> None:
    """A hook that crashes (an import error, a traceback) is a refusal, not an
    accept, and the marker is still the first line even though the traceback
    follows."""
    fake = tmp_path / "fake-python"
    fake.write_text(
        "#!/bin/sh\necho 'Traceback: no module named precis' >&2\nexit 1\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    broken = tmp_path / "broken-hook"
    broken.write_text(
        stub.read_text(encoding="utf-8").replace(sys.executable, str(fake)),
        encoding="utf-8",
    )
    proc = _run_stub(broken, tmp_path, "")
    assert proc.returncode == 1
    lines = proc.stderr.splitlines()
    assert lines[0] == f"{ws.HOOK_REFUSED} (hook failed to run, status 1)"
    assert "Traceback" in proc.stderr


def test_stub_runs_the_deployed_code_whatever_the_pusher_sets(
    stub: Path, tmp_path: Path
) -> None:
    """A session's PYTHONPATH, or a ``precis`` package in the directory the
    push runs from, must not replace the hook: the stub runs the worker's own
    package isolated. The fake hook here accepts everything; the real one
    refuses this ref."""
    fake = tmp_path / "fake"
    (fake / "precis" / "utils").mkdir(parents=True)
    (fake / "precis" / "__init__.py").write_text("", encoding="utf-8")
    (fake / "precis" / "utils" / "__init__.py").write_text("", encoding="utf-8")
    (fake / "precis" / "utils" / "hub_hook.py").write_text(
        "def main(argv):\n    return 0\n", encoding="utf-8"
    )
    env = {**SESSION, "PYTHONPATH": str(fake), "PYTHONSAFEPATH": ""}
    proc = _run_stub(stub, fake, f"{ZERO} {OTHER} refs/heads/main\n", env)
    assert proc.returncode == 1
    assert proc.stderr.splitlines()[0] == f"{ws.HOOK_REFUSED} (ref rule)"
