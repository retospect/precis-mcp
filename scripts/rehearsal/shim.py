"""Scripted session: stands in for ``claude`` / ``devin`` / ``kimi`` / ``codex``.

The worker's lane adapters (``_slice_adapters.Adapter.argv``) exec the binary
named by ``PRECIS_CLAUDE_BIN`` / ``PRECIS_DEVIN_BIN`` / ``PRECIS_KIMI_BIN`` /
``PRECIS_CODEX_BIN`` (and ``world.py`` also puts same-named wrappers first on
the worker's ``PATH``). Each wrapper execs this file with ``--lane`` and the
adapter's own argv; nothing in the engine changes.

What the shim does, in order:

1. Probes (``--version``, ``auth status``, ``login status``, ``models
   list``) are answered and exit 0: the engine's capability probes and
   preflights run them.
2. A session (the adapter's headless argv) reads its brief where that lane
   puts it (stdin for claude/codex, ``--prompt-file`` for devin, the
   argument after ``-p`` for kimi), saves a copy under ``<world>/briefs/``
   and writes a session record ``<world>/sessions/td<N>-n<K>.json`` (pid,
   process group, folder, lane, every step as it runs) so the driver can see
   and kill it.
3. It loads ``<world>/scripts/td<N>.json``, picks the K-th attempt's step
   list (K counts this todo's sessions) and runs the steps in the session's
   folder (the process cwd). Steps are listed in :data:`STEPS`.
4. It prints a final message in the shape that lane's adapter parses:
   claude a JSON result line, kimi a stream-json assistant line, codex a
   ``-o`` last-message file plus a JSON event line, devin plain text.

The shim never sets a new session or process group: the process group the
worker's launcher made is the one a driver kills. Standard library only.

Script file::

    {"attempts": [{"steps": [ {"do": "write", ...}, ... ]}, ...],
     "repeat_last": true}

An attempt without steps, or no file at all, runs :func:`default_steps`
(a builder that makes one commit, pushes and verifies) or, when the brief is
a pre-land review, a LAND verdict.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

#: Exit code for a step that failed (distinct from a scripted exit).
EXIT_STEP_FAILED = 70

_REFUSED = "precis-hub: refused"

#: Environment values the session record keeps (the worker's launch env, as
#: the engine built it); every other name is recorded without its value, so
#: a driver can assert that nothing like a database DSN reached a session.
SESSION_ENV_KEYS = (
    "PRECIS_SLICE_ID",
    "PRECIS_SLICE_JOB",
    "PRECIS_SLICE_EFFORT",
    "PRECIS_GATE_SLOT_ROOT",
    "PRECIS_NO_AUTOREAP",
    "PRECIS_FACTORY",
    "HOME",
    "TMPDIR",
    "UV_CACHE_DIR",
)


class StepFailed(Exception):
    """A step did not do what it said; the session ends non-zero."""


class EarlyExit(Exception):
    """A scripted ``exit`` step."""

    def __init__(self, code: int, message: str | None) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


# ── probes ─────────────────────────────────────────────────────────


def answer_probe(lane: str, argv: list[str]) -> int | None:
    """Exit code when ``argv`` is a probe the engine runs, else ``None``."""
    words = [a for a in argv if not a.startswith("-")]
    if "--version" in argv or "-V" in argv:
        print(f"{lane} 0.0.0 (rehearsal shim)")
        return 0
    if lane == "devin" and words[:2] == ["auth", "status"]:
        print("Logged in as rehearsal@example.invalid")
        return 0
    if lane == "codex" and words[:2] == ["login", "status"]:
        print("Logged in using ChatGPT")
        return 0
    if lane == "devin" and words[:2] == ["models", "list"]:
        print(json.dumps({"families": []}))
        return 0
    if lane == "claude" and words[:1] == ["auth"]:
        print(json.dumps({"loggedIn": True}))
        return 0
    return None


def is_session(lane: str, argv: list[str]) -> bool:
    if lane == "claude":
        return "-p" in argv or "--print" in argv
    if lane == "devin":
        return "-p" in argv and "--prompt-file" in argv
    if lane == "kimi":
        return "-p" in argv
    if lane == "codex":
        return bool(argv) and argv[0] == "exec"
    return False


# ── brief ──────────────────────────────────────────────────────────


def read_brief(lane: str, argv: list[str]) -> str:
    if lane in ("claude", "codex"):
        return sys.stdin.read()
    if lane == "devin":
        i = argv.index("--prompt-file")
        return Path(argv[i + 1]).read_text(encoding="utf-8")
    if lane == "kimi":
        i = argv.index("-p")
        return argv[i + 1] if i + 1 < len(argv) else ""
    return ""


def codex_last_message_path(argv: list[str]) -> Path | None:
    if "-o" in argv:
        i = argv.index("-o")
        if i + 1 < len(argv):
            return Path(argv[i + 1])
    return None


# ── records ────────────────────────────────────────────────────────


class Record:
    """The session record the driver reads; rewritten atomically."""

    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        self.path = path
        self.data = data
        self.flush()

    def flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, self.path)

    def step(self, name: str, **info: Any) -> None:
        self.data.setdefault("steps", []).append(
            {"do": name, "at": time.time(), **info}
        )
        self.flush()

    def set(self, **kv: Any) -> None:
        self.data.update(kv)
        self.flush()


def next_attempt(world: Path, todo: int) -> int:
    """K for this todo's next session (1-based), claimed under a lock."""
    sessions = world / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    with open(sessions / ".lock", "a+", encoding="utf-8") as lock:
        deadline = time.monotonic() + 120
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        k = 1 + len(list(sessions.glob(f"td{todo}-n*.json")))
        (sessions / f"td{todo}-n{k}.json").write_text("{}", encoding="utf-8")
        return k


# ── git helpers ────────────────────────────────────────────────────


def git(
    args: list[str],
    cwd: Path,
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
    timeout: float = 300.0,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env={**os.environ, **(env or {})},
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise StepFailed(
            f"git {' '.join(args[:3])}: {(proc.stderr or proc.stdout).strip()[:300]}"
        )
    return proc


def attempt_ref(todo: int, job: str) -> str:
    return f"refs/factory/build/{todo}/{job}"


# ── steps ──────────────────────────────────────────────────────────


class Session:
    """Everything a step can see."""

    def __init__(
        self,
        *,
        lane: str,
        todo: int,
        job: str,
        cwd: Path,
        brief: str,
        record: Record,
        base: str = "main",
    ) -> None:
        self.lane = lane
        self.todo = todo
        self.job = job
        self.cwd = cwd
        self.brief = brief
        self.record = record
        self.base = base
        self.final: str | None = None
        self.last_push_refused = False
        self.last_push_output = ""

    # -- builder steps --

    def do_write(self, step: dict[str, Any]) -> None:
        target = self.cwd / step["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if step.get("append") else "w"
        with open(target, mode, encoding="utf-8") as fh:
            fh.write(step["text"])
        self.record.step("write", path=step["path"])

    def do_delete(self, step: dict[str, Any]) -> None:
        (self.cwd / step["path"]).unlink(missing_ok=True)
        self.record.step("delete", path=step["path"])

    def do_commit(self, step: dict[str, Any]) -> None:
        subject = (
            step.get("message") or f"feat(rehearsal): scripted change (td{self.todo})"
        )
        msg = self.cwd / "scratch" / "commit-msg.txt"
        msg.parent.mkdir(parents=True, exist_ok=True)
        msg.write_text(subject + "\n", encoding="utf-8")
        git(["add", "-A"], self.cwd)
        args = ["commit", "-q", "-F", "scratch/commit-msg.txt"]
        if step.get("allow_empty"):
            args.append("--allow-empty")
        git(args, self.cwd)
        self.record.step(
            "commit",
            subject=subject,
            head=git(["rev-parse", "HEAD"], self.cwd).stdout.strip(),
        )

    def do_amend(self, step: dict[str, Any]) -> None:
        git(["add", "-A"], self.cwd)
        git(["commit", "-q", "--amend", "--no-edit", "--allow-empty"], self.cwd)
        self.record.step(
            "amend", head=git(["rev-parse", "HEAD"], self.cwd).stdout.strip()
        )

    def do_rebase(self, step: dict[str, Any]) -> None:
        base = step.get("base") or self.base
        git(["fetch", "-q", "origin", base], self.cwd)
        proc = git(["rebase", f"origin/{base}"], self.cwd, check=False)
        if proc.returncode == 0:
            self.record.step("rebase", result="clean")
            return
        conflicts = git(
            ["diff", "--name-only", "--diff-filter=U"], self.cwd
        ).stdout.split()
        how = step.get("conflict", "fail")
        self.record.step("rebase", result="conflict", files=conflicts, how=how)
        if how == "fail":
            git(["rebase", "--abort"], self.cwd, check=False)
            raise StepFailed(f"rebase conflict in {conflicts}")
        if how == "leave":
            return
        if isinstance(how, dict):
            for path, text in how.items():
                (self.cwd / path).write_text(text, encoding="utf-8")
                git(["add", "--", path], self.cwd)
            git(["rebase", "--continue"], self.cwd, env={"GIT_EDITOR": "true"})
            self.record.step("rebase-continue", resolved=list(how))
            return
        raise StepFailed(f"unknown conflict handling {how!r}")

    def do_check(self, step: dict[str, Any]) -> None:
        cmd = step.get("cmd", "true")
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(self.cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=600,
        )
        self.record.step("check", cmd=cmd, rc=proc.returncode)
        if proc.returncode != 0 and not step.get("allow_fail"):
            raise StepFailed(f"check {cmd!r} failed")

    def do_push(self, step: dict[str, Any]) -> None:
        ref = attempt_ref(self.todo, self.job)
        proc = git(["push", "origin", f"+HEAD:{ref}"], self.cwd, check=False)
        out = (proc.stderr or "") + (proc.stdout or "")
        refused = _REFUSED in out
        self.last_push_refused = refused
        self.last_push_output = out
        self.record.step(
            "push", ref=ref, rc=proc.returncode, refused=refused, output=out[-4000:]
        )
        if proc.returncode == 0:
            return
        handler = step.get("on_refused") if refused else step.get("on_fail")
        if handler:
            self.run(handler)
            if step.get("retry", True):
                self.do_push({**step, "on_refused": None, "on_fail": None})
            return
        if refused and step.get("allow_refused"):
            return
        raise StepFailed(f"push failed: {out.strip()[-300:]}")

    def do_verify(self, step: dict[str, Any]) -> None:
        ref = attempt_ref(self.todo, self.job)
        head = git(["rev-parse", "HEAD"], self.cwd).stdout.strip()
        ls = git(["ls-remote", "origin", ref], self.cwd).stdout.split()
        held = ls[0] if ls else ""
        self.record.step("verify", ref=ref, head=head, held=held, ok=held == head)
        if held != head:
            raise StepFailed(
                f"ls-remote {ref} prints {held or 'nothing'}, HEAD is {head}"
            )

    def do_wip(self, step: dict[str, Any]) -> None:
        """Commit what exists as ``wip: snapshot — <why>``, push, verify."""
        why = step.get("why", "could not finish")
        self.do_commit(
            {"message": f"wip: snapshot — {why} (td{self.todo})", "allow_empty": True}
        )
        self.do_push({})
        self.do_verify({})

    def do_hang_in_commit(self, step: dict[str, Any]) -> None:
        """Hold ``index.lock`` inside ``git commit`` until killed: a
        pre-commit hook records whether the lock exists, then sleeps."""
        marker = Path(step["marker"])
        hook = self.cwd / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(
            "#!/bin/sh\n"
            'if [ -f "$(git rev-parse --git-dir)/index.lock" ]; then s=locked; else s=unlocked; fi\n'
            f"echo \"$s $$\" > '{marker}'\n"
            "exec sleep 3600\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)
        msg = self.cwd / "scratch" / "commit-msg.txt"
        msg.parent.mkdir(parents=True, exist_ok=True)
        msg.write_text(
            step.get("message", f"feat(rehearsal): interrupted (td{self.todo})") + "\n",
            encoding="utf-8",
        )
        git(["add", "-A"], self.cwd)
        self.record.step("hang_in_commit", marker=str(marker))
        # Blocks inside the hook until the group is killed.
        # ``-a``: only a commit that updates the index itself (``-a``, a
        # pathspec) holds ``index.lock`` while its pre-commit hook runs.
        git(
            ["commit", "-a", "-q", "-F", "scratch/commit-msg.txt"],
            self.cwd,
            check=False,
            # The hook sleeps an hour; the group is killed long before.
            timeout=3700,
        )
        self.record.step("hang_in_commit-returned")

    # -- control --

    def do_wait_for(self, step: dict[str, Any]) -> None:
        path = Path(step["path"])
        deadline = time.time() + float(step.get("timeout", 600))
        self.record.step("wait_for", path=str(path))
        while not path.exists():
            if time.time() > deadline:
                raise StepFailed(f"timed out waiting for {path}")
            time.sleep(0.2)

    def do_touch(self, step: dict[str, Any]) -> None:
        Path(step["path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(step["path"]).write_text(str(step.get("text", "")), encoding="utf-8")
        self.record.step("touch", path=step["path"])

    def do_sleep(self, step: dict[str, Any]) -> None:
        time.sleep(float(step.get("seconds", 1)))

    def do_say(self, step: dict[str, Any]) -> None:
        self.final = step["text"]
        self.record.step("say")

    def do_exit(self, step: dict[str, Any]) -> None:
        raise EarlyExit(int(step.get("code", 0)), step.get("say"))

    def do_fail(self, step: dict[str, Any]) -> None:
        raise StepFailed(step.get("why", "scripted failure"))

    # -- reviewer --

    def reviewed_head(self) -> str:
        m = re.search(r"origin/(slice/\d+)", self.brief)
        ref = f"origin/{m.group(1)}" if m else "HEAD"
        proc = git(
            ["rev-parse", "--verify", f"{ref}^{{commit}}"], self.cwd, check=False
        )
        return proc.stdout.strip()

    def do_verdict(self, step: dict[str, Any]) -> None:
        verdict = str(step.get("verdict", "LAND")).upper()
        head = self.reviewed_head()
        lines: list[str]
        if verdict == "LAND":
            lines = ["review: LAND (read-only, tests not run)"]
        else:
            findings = step.get("text", "scripted finding in docs/rehearsal")
            lines = [f"review: {verdict} — {findings} (read-only, tests not run)"]
            lines.append(f"anchors: {step.get('anchors', 'docs/rehearsal.md::probe')}")
        if head:
            lines.append(f"verified head {head}")
        self.final = "\n".join(lines)
        self.record.step("verdict", verdict=verdict, head=head)

    # -- driver --

    def run(self, steps: list[dict[str, Any]]) -> None:
        for step in steps:
            name = step["do"]
            fn = getattr(self, f"do_{name}", None)
            if fn is None:
                raise StepFailed(f"unknown step {name!r}")
            fn(step)


def default_steps(todo: int, job: str, brief: str) -> list[dict[str, Any]]:
    """What a session does with no script: a reviewer says LAND, a builder
    makes one docs commit, rebases, pushes and checks the ref."""
    if "origin/slice/" in brief and "verified head" in brief:
        return [{"do": "verdict", "verdict": "LAND"}]
    return builder_steps(todo, job)


def builder_steps(
    todo: int, job: str, *, path: str | None = None
) -> list[dict[str, Any]]:
    path = path or f"docs/rehearsal/td{todo}.md"
    return [
        {
            "do": "write",
            "path": path,
            "text": f"# Rehearsal probe td{todo}\n\nWritten by the scripted builder (job {job}).\n",
        },
        {"do": "commit", "message": f"docs(rehearsal): scripted probe (td{todo})"},
        {"do": "rebase"},
        {"do": "check", "cmd": "true"},
        {"do": "push"},
        {"do": "verify"},
    ]


def default_say(todo: int) -> str:
    return (
        f"Done: one docs commit on slice/{todo}, pushed and read back.\n\n"
        "tests: ran (rehearsal shim: trivial check only)"
    )


# ── final message per lane ─────────────────────────────────────────


def emit(lane: str, message: str, argv: list[str], *, error: bool) -> None:
    if lane == "claude":
        print(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "error_during_execution" if error else "success",
                    "is_error": error,
                    "result": message,
                    "num_turns": 1,
                    "duration_ms": 1,
                    "total_cost_usd": 0.0,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            )
        )
    elif lane == "kimi":
        print(json.dumps({"role": "assistant", "content": message}))
    elif lane == "codex":
        path = codex_last_message_path(argv)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(message, encoding="utf-8")
        print(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item_0", "type": "agent_message", "text": message},
                }
            )
        )
        print(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            )
        )
    else:  # devin: the adapter reads plain text
        print(message)
    sys.stdout.flush()


# ── main ───────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--world", required=True)
    parser.add_argument("--lane", required=True)
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    ns = parser.parse_args(argv)
    rest = ns.rest[1:] if ns.rest and ns.rest[0] == "--" else ns.rest
    lane = ns.lane
    world = Path(ns.world)

    probe = answer_probe(lane, rest)
    if probe is not None:
        return probe
    if not is_session(lane, rest):
        print(f"{lane}: rehearsal shim ignores {rest[:3]}", file=sys.stderr)
        return 0

    brief = read_brief(lane, rest)
    slice_id = os.environ.get("PRECIS_SLICE_ID", "td0")
    todo = int(slice_id.removeprefix("td") or 0)
    job = os.environ.get("PRECIS_SLICE_JOB", "0")
    cwd = Path.cwd()
    k = next_attempt(world, todo)
    briefs = world / "briefs"
    briefs.mkdir(parents=True, exist_ok=True)
    (briefs / f"td{todo}-n{k}.md").write_text(brief, encoding="utf-8")
    record = Record(
        world / "sessions" / f"td{todo}-n{k}.json",
        {
            "todo": todo,
            "job": job,
            "attempt": k,
            "lane": lane,
            "cwd": str(cwd),
            "pid": os.getpid(),
            "pgid": os.getpgrp(),
            "argv": rest[:12],
            "env": {k: os.environ[k] for k in SESSION_ENV_KEYS if k in os.environ},
            "env_names": sorted(os.environ),
            "head0": git(["rev-parse", "HEAD"], cwd, check=False).stdout.strip(),
            "started": time.time(),
            "state": "running",
            "steps": [],
        },
    )
    script_path = world / "scripts" / f"td{todo}.json"
    steps: list[dict[str, Any]] | None = None
    say: str | None = None
    if script_path.is_file():
        script = json.loads(script_path.read_text(encoding="utf-8"))
        attempts = script.get("attempts") or []
        pick = (
            attempts[k - 1]
            if k <= len(attempts)
            else (attempts[-1] if attempts and script.get("repeat_last") else None)
        )
        if pick is not None:
            steps = pick.get("steps")
            say = pick.get("say")
    if steps is None:
        steps = default_steps(todo, job, brief)
    sess = Session(lane=lane, todo=todo, job=job, cwd=cwd, brief=brief, record=record)
    code = 0
    error = False
    try:
        sess.run(steps)
    except EarlyExit as ex:
        code = ex.code
        error = ex.code != 0
        sess.final = ex.message or sess.final
    except StepFailed as ex:
        code = EXIT_STEP_FAILED
        error = True
        sess.final = f"The scripted session stopped: {ex}"
        record.set(failure=str(ex))
    message = sess.final or say or default_say(todo)
    record.set(state="exited", code=code, message=message, ended=time.time())
    emit(lane, message, rest, error=error)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
