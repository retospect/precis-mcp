"""Git transport failures retain evidence and retry only safe transient pushes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests._script import load_script

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the git network helper's shell side is POSIX-only"
)

HELPER = Path(__file__).resolve().parents[1] / "scripts/lib/git_network.py"
TARGET = "a" * 40
OLD = "b" * 40
REF = "refs/heads/prod"


@pytest.mark.parametrize("userinfo", ["user:password", "user:p@ss", "user:p@ss@word"])
def test_redact_removes_entire_url_userinfo(network: ModuleType, userinfo: str) -> None:
    assert network.redact(f"https://{userinfo}@host.invalid/repo") == (
        "https://[redacted]@host.invalid/repo"
    )


def test_shell_calls_keep_distinct_default_and_explicit_timeouts() -> None:
    shell_helper = HELPER.with_name("git-network.sh")
    cp = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; python3() { printf "%s\\n" "$@"; }; '
            "git_network fetch origin main; git_network_bounded 60 fetch origin main",
            "bash",
            str(shell_helper),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    arguments = cp.stdout.splitlines()
    assert arguments == [
        str(HELPER),
        "--timeout",
        "300",
        "--",
        "fetch",
        "origin",
        "main",
        str(HELPER),
        "--timeout",
        "60",
        "--",
        "fetch",
        "origin",
        "main",
    ]


def test_fetch_allows_ninety_seconds_with_shell_timeout(
    network: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == 300
        assert kwargs["timeout"] > 90
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(network.subprocess, "run", run)
    assert network.run_git(["fetch", "origin", "main"], timeout=300).returncode == 0


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    mod = load_script("git_network_test", HELPER)
    monkeypatch.setattr(mod.time, "sleep", lambda _seconds: None)
    return mod


def _stub(
    monkeypatch: pytest.MonkeyPatch,
    network: ModuleType,
    outcomes: list[str | subprocess.TimeoutExpired],
    *,
    remote: str | list[str] = OLD,
    push_urls: list[str] | None = None,
    remote_shas: dict[str, str] | None = None,
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if "push" in argv:
            outcome = outcomes.pop(0)
            if isinstance(outcome, subprocess.TimeoutExpired):
                raise outcome
            return subprocess.CompletedProcess(argv, 1 if outcome else 0, "", outcome)
        if "ls-remote" in argv:
            current = remote.pop(0) if isinstance(remote, list) else remote
            if remote_shas is not None:
                current = remote_shas[argv[-2]]
            return subprocess.CompletedProcess(
                argv, 0, f"{current}\t{REF}\n" if current else "", ""
            )
        if "remote" in argv and "get-url" in argv:
            urls = push_urls if push_urls is not None else [argv[-1]]
            return subprocess.CompletedProcess(argv, 0, "\n".join(urls) + "\n", "")
        if "rev-parse" in argv:
            sha = OLD if argv[-1].startswith("refs/remotes/") else TARGET
            return subprocess.CompletedProcess(argv, 0, sha + "\n", "")
        if "config" in argv:
            return subprocess.CompletedProcess(
                argv, 0, "+refs/heads/*:refs/remotes/origin/*\n", ""
            )
        raise AssertionError(f"unexpected git call: {argv}")

    monkeypatch.setattr(network.subprocess, "run", run)
    return calls


def test_failed_push_exposes_original_error_and_redacts_url(
    network: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url = "https://" + "alice:secret" + "@example.invalid/repository"
    _stub(monkeypatch, network, [f"remote: protected branch at {url}\n"])
    cp = network.run_git(["push", url, f"{TARGET}:{REF}"])
    assert cp.returncode == 1
    err = capsys.readouterr().err
    assert "protected branch" in err
    assert "git push" in err
    assert "exit 1" in err
    assert "alice" not in err
    assert "secret" not in err


def test_timeout_names_duration_and_preserves_partial_stderr(
    network: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    timeout = subprocess.TimeoutExpired(
        "git push", 7, stderr=b"remote: waiting for receive-pack\n"
    )
    _stub(monkeypatch, network, [timeout, timeout, timeout])
    cp = network.run_git(["push", "origin", f"{TARGET}:{REF}"], timeout=7)
    assert cp.returncode == 124
    err = capsys.readouterr().err
    assert "timed out" in err
    assert "7" in err
    assert "waiting for receive-pack" in err


@pytest.mark.parametrize(
    ("failure", "attempts"),
    [
        ("fatal: HTTP 503 Service Unavailable", 2),
        ("fatal: unable to access remote: Connection reset by peer", 2),
        ("remote: error: GH006: Protected branch update failed", 1),
        ("! [rejected] prod -> prod (non-fast-forward)", 1),
        ("! [rejected] prod -> prod (stale info)", 1),
    ],
)
def test_only_transient_failures_retry_with_original_lease(
    network: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    attempts: int,
) -> None:
    calls = _stub(monkeypatch, network, [failure, ""])
    args = ["push", f"--force-with-lease={REF}:{OLD}", "origin", f"{TARGET}:{REF}"]
    cp = network.run_git(args)
    pushes = [argv for argv in calls if "push" in argv]
    assert len(pushes) == attempts
    assert all(argv == ["git", *args] for argv in pushes)
    assert cp.returncode == (0 if attempts == 2 else 1)
    if attempts == 2:
        first_push = calls.index(pushes[0])
        second_push = next(
            i for i in range(first_push + 1, len(calls)) if "push" in calls[i]
        )
        assert any("ls-remote" in argv for argv in calls[first_push + 1 : second_push])


def test_timeout_after_landed_push_is_success_without_another_push(
    network: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub(
        monkeypatch,
        network,
        [subprocess.TimeoutExpired("git push", 60)],
        remote=TARGET,
    )
    cp = network.run_git(
        ["push", f"--force-with-lease={REF}:{OLD}", "origin", f"{TARGET}:{REF}"]
    )
    assert cp.returncode == 0
    assert sum("push" in argv for argv in calls) == 1
    assert any("ls-remote" in argv for argv in calls)


def test_retry_budget_is_bounded(
    network: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub(monkeypatch, network, ["fatal: HTTP 503"] * 3)
    delays: list[float] = []
    monkeypatch.setattr(network.time, "sleep", delays.append)
    cp = network.run_git(["push", "origin", f"{TARGET}:{REF}"])
    assert cp.returncode != 0
    assert sum("push" in argv for argv in calls) == 3
    assert delays == [1, 2]


def test_delete_never_retries_against_a_recreated_ref(
    network: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _stub(
        monkeypatch,
        network,
        [subprocess.TimeoutExpired("git push", 60)],
        remote=[OLD, "c" * 40],
    )
    cp = network.run_git(["push", "origin", "--delete", REF])
    assert cp.returncode != 0
    assert sum("push" in argv for argv in calls) == 1
    assert "remote ref changed" in capsys.readouterr().err


@pytest.mark.parametrize("force", ["--force", "--force-with-lease"])
def test_retry_freezes_symbolic_source_and_original_remote_expectation(
    network: ModuleType, monkeypatch: pytest.MonkeyPatch, force: str
) -> None:
    calls = _stub(monkeypatch, network, ["fatal: HTTP 503", ""])
    cp = network.run_git(["push", force, "origin", f"HEAD:{REF}"])
    assert cp.returncode == 0
    pushes = [argv for argv in calls if "push" in argv]
    assert len(pushes) == 2
    assert f"{TARGET}:{REF}" in pushes[0]
    assert f"--force-with-lease={REF}:{OLD}" in pushes[1]
    assert f"{TARGET}:{REF}" in pushes[1]
    assert "--force" not in pushes[1]
    assert "--force-with-lease" not in pushes[1]


def test_success_does_not_add_diagnostics(
    network: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub(monkeypatch, network, [""])
    assert network.run_git(["push", "origin", f"{TARGET}:{REF}"]).returncode == 0
    assert capsys.readouterr().err == ""


def test_retry_reconciles_push_url_instead_of_fetch_remote(
    network: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub(
        monkeypatch,
        network,
        [subprocess.TimeoutExpired("git push", 60), ""],
        push_urls=["push-destination"],
        remote_shas={"origin": TARGET, "push-destination": OLD},
    )
    cp = network.run_git(
        ["push", f"--force-with-lease={REF}:{OLD}", "origin", f"{TARGET}:{REF}"]
    )
    assert cp.returncode == 0
    assert sum("push" in argv for argv in calls) == 2
    reads = [argv for argv in calls if "ls-remote" in argv]
    assert reads
    assert all(argv[-2] == "push-destination" for argv in reads)


def test_multiple_push_urls_are_not_blindly_retried(
    network: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub(
        monkeypatch,
        network,
        ["fatal: HTTP 503"],
        push_urls=["first-destination", "second-destination"],
    )
    cp = network.run_git(["push", "origin", f"{TARGET}:{REF}"])
    assert cp.returncode != 0
    assert sum("push" in argv for argv in calls) == 1
    assert not any("ls-remote" in argv for argv in calls)


def test_short_destination_is_not_retried_as_a_branch(
    network: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub(monkeypatch, network, ["fatal: HTTP 503"])
    cp = network.run_git(["push", "origin", "v1:v1"])
    assert cp.returncode != 0
    assert sum("push" in argv for argv in calls) == 1
    assert not any("ls-remote" in argv for argv in calls)


def test_literal_push_rewrite_is_not_reconciled_against_fetch_url(
    network: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub(monkeypatch, network, ["fatal: HTTP 503"], remote=TARGET)
    original = network.subprocess.run

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "get-url" in argv:
            return subprocess.CompletedProcess(argv, 2, "", "not a named remote")
        if "--get-regexp" in argv:
            return subprocess.CompletedProcess(
                argv, 0, "url.other.pushinsteadof literal\n", ""
            )
        return original(argv, **kwargs)

    monkeypatch.setattr(network.subprocess, "run", run)
    cp = network.run_git(["push", "literal", f"{TARGET}:{REF}"])
    assert cp.returncode != 0
    assert sum("push" in argv for argv in calls) == 1
    assert not any("ls-remote" in argv for argv in calls)


def test_main_passes_success_stderr_through_redacted(
    network: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    userinfo = "u:p"
    done = subprocess.CompletedProcess(
        ["git"],
        0,
        "",
        f"To https://{userinfo}@host.invalid/r\n   aaa..bbb  HEAD -> x\n",
    )
    monkeypatch.setattr(network, "run_git", lambda *_a, **_k: done)
    monkeypatch.setattr(sys, "argv", ["git_network.py", "--", "push", "origin", "x"])

    assert network.main() == 0
    err = capsys.readouterr().err
    assert "aaa..bbb" in err
    assert userinfo not in err
