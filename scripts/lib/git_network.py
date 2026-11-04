"""Original failures, passed-through success output, bounded push retries.

Retries freeze the pushed object and retain explicit leases. A transport error
does not prove rejection: read the destination first, and count an already
installed object (or an already deleted ref) as success. Unsupported/multiple
refspecs are reported but never blindly retried. Shell callers use this same
implementation so classification and retry limits cannot drift.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

PUSH_RETRY_DELAYS = (1, 2)
PUSH_MAX_ATTEMPTS = len(PUSH_RETRY_DELAYS) + 1
DEFAULT_TIMEOUT = 60
_PERMANENT = re.compile(
    r"non.fast.forward|fetch first|stale info|protected branch|protection rule|"
    r"repository rule|pre.receive hook declined|permission denied|authentication failed|"
    r"repository not found|cannot lock ref|\[rejected\]",
    re.IGNORECASE,
)
_TRANSIENT = re.compile(
    r"(?:http|error|returned|response|status)[^\n]*\b5\d\d\b|"
    r"internal server error|bad gateway|service unavailable|gateway timeout|"
    r"connection (?:reset|refused|closed|timed out)|could not resolve|"
    r"couldn't connect|failed to connect|remote end hung up|unexpected disconnect|"
    r"broken pipe|network is unreachable|connection aborted|tls connection|"
    r"ssl connection|curl (?:18|28|35|52|55|56)\b|timed? out|timeout",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """Remove URL userinfo and credential query values from command/error text."""
    text = re.sub(r"(\w+://)[^\s/]+@", r"\1[redacted]@", text)
    return re.sub(
        r"([?&](?:access_token|token|password|key|signature)=)[^\s&#]+",
        r"\1[redacted]",
        text,
        flags=re.IGNORECASE,
    )


def _text(value: str | bytes | None) -> str:
    return (
        value.decode("utf-8", errors="replace")
        if isinstance(value, bytes)
        else value or ""
    )


def report_failure(cp: subprocess.CompletedProcess[str]) -> None:
    command = shlex.join(cp.args)
    print(f"git-network: {redact(command)}: exit {cp.returncode}", file=sys.stderr)
    for line in (
        cp.stderr or cp.stdout or "command returned no error text"
    ).splitlines():
        print(f"  {redact(line)}", file=sys.stderr)


def run_command(
    args: list[str],
    *,
    cwd: str | Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    allowed: tuple[int, ...] = (0,),
    report: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Capture a command, retaining timeout stderr and an actionable exit code."""
    try:
        cp = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        cp = subprocess.CompletedProcess(
            args,
            124,
            _text(exc.stdout),
            f"timed out after {timeout:g}s\n{_text(exc.stderr)}",
        )
    except OSError as exc:
        cp = subprocess.CompletedProcess(args, 127, "", str(exc))
    if report and cp.returncode not in allowed:
        report_failure(cp)
    return cp


def transient(cp: subprocess.CompletedProcess[str]) -> bool:
    error = cp.stderr + "\n" + cp.stdout
    return not _PERMANENT.search(error) and bool(
        cp.returncode == 124 or _TRANSIENT.search(error)
    )


def _push_target(
    args: list[str],
    index: int,
    cwd: str | Path | None,
    timeout: float,
) -> tuple[list[str], str, str, str, str | None, str] | None:
    """Only one explicit destination can be reconciled without guessing."""
    options = args[index + 1 :]
    positional = [arg for arg in options if not arg.startswith("-")]
    known = (
        "-q",
        "--quiet",
        "-f",
        "--force",
        "--porcelain",
        "-d",
        "--delete",
        "--force-with-lease",
        "--no-verify",
        "--atomic",
    )
    if any(
        arg.startswith("-")
        and arg not in known
        and not arg.startswith("--force-with-lease=")
        for arg in options
    ):
        return None
    if len(positional) != 2:
        return None
    remote, spec = positional
    deleting = "--delete" in options or "-d" in options
    if deleting:
        source, destination = "", spec
    elif ":" in spec:
        source, destination = spec.lstrip("+").split(":", 1)
    else:
        return None
    if not destination or "*" in spec:
        return None
    if not destination.startswith("refs/"):
        return None  # A short name may be a tag; never guess a retry namespace.
    prefix = args[:index]
    urls = run_command(
        ["git", *prefix, "remote", "get-url", "--push", "--all", remote],
        cwd=cwd,
        timeout=timeout,
        report=False,
    )
    if urls.returncode:
        rewrites = run_command(
            ["git", *prefix, "config", "--get-regexp", r"^url\..*\.pushinsteadof$"],
            cwd=cwd,
            timeout=timeout,
            report=False,
        )
        if rewrites.returncode != 1:
            return None  # Literal destinations may be rewritten only for push.
    push_urls = urls.stdout.splitlines() if urls.returncode == 0 else [remote]
    if len(push_urls) != 1:
        return None  # A partial push to multiple URLs cannot be reconciled here.
    push_url = push_urls[0]
    sha = ""
    if source:
        if re.fullmatch(r"[0-9a-f]{40,64}", source):
            sha = source
        else:
            resolved = run_command(
                ["git", *prefix, "rev-parse", "--verify", source],
                cwd=cwd,
                timeout=timeout,
            )
            if resolved.returncode:
                return None
            sha = resolved.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            return None
    lease: str | None = None
    forced = spec.startswith("+") or any(arg in options for arg in ("-f", "--force"))
    for option in options:
        if option.startswith("--force-with-lease="):
            ref, sep, expected = option.partition("=")[2].partition(":")
            full_ref = ref if ref.startswith("refs/") else "refs/heads/" + ref
            if full_ref != destination or not sep:
                return None
            lease = expected
        elif option == "--force-with-lease":
            if not destination.startswith("refs/heads/"):
                return None
            mapping = run_command(
                ["git", *prefix, "config", "--get-all", f"remote.{remote}.fetch"],
                cwd=cwd,
                timeout=timeout,
                report=False,
            )
            if mapping.returncode or mapping.stdout.splitlines() != [
                f"+refs/heads/*:refs/remotes/{remote}/*"
            ]:
                return None  # Custom tracking maps need an explicit lease.
            tracking = (
                f"refs/remotes/{remote}/{destination.removeprefix('refs/heads/')}"
            )
            expected = run_command(
                ["git", *prefix, "rev-parse", "--verify", tracking],
                cwd=cwd,
                timeout=timeout,
                report=False,
            )
            lease = expected.stdout.strip() if expected.returncode == 0 else ""
    if lease is None and (forced or deleting or not source):
        before = run_command(
            ["git", *prefix, "ls-remote", "--refs", push_url, destination],
            cwd=cwd,
            timeout=timeout,
        )
        if before.returncode:
            return None
        rows = [row.split() for row in before.stdout.splitlines()]
        lease = next(
            (row[0] for row in rows if len(row) == 2 and row[1] == destination), ""
        )
    return prefix, remote, destination, sha, lease, push_url


def run_git(
    args: list[str],
    *,
    cwd: str | Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Run git, printing failures only; reconcile before transient push retries."""
    index = next(
        (i for i, arg in enumerate(args) if arg in ("push", "fetch", "ls-remote")), -1
    )
    is_push = index >= 0 and args[index] == "push"
    target = _push_target(args, index, cwd, timeout) if is_push else None
    command = ["git", *args]
    if target and target[3]:
        # Freeze the first attempt too: another process can move HEAD while
        # the network is unavailable. Every attempt must mean the same object.
        spec_index = [
            i for i in range(index + 1, len(args)) if not args[i].startswith("-")
        ][1]
        force = "+" if args[spec_index].startswith("+") else ""
        command[spec_index + 1] = f"{force}{target[3]}:{target[2]}"
    for attempt in range(PUSH_MAX_ATTEMPTS):
        cp = run_command(command, cwd=cwd, timeout=timeout, report=index >= 0)
        if cp.returncode == 0 or not target or not transient(cp):
            return cp
        prefix, remote, destination, sha, lease, push_url = target
        observed = run_command(
            ["git", *prefix, "ls-remote", "--refs", push_url, destination],
            cwd=cwd,
            timeout=timeout,
        )
        if observed.returncode:
            return cp
        rows = [row.split() for row in observed.stdout.splitlines()]
        current = next(
            (row[0] for row in rows if len(row) == 2 and row[1] == destination), ""
        )
        if current == sha:
            print(
                "git-network: remote already holds the requested target; push succeeded",
                file=sys.stderr,
            )
            return subprocess.CompletedProcess(command, 0, "", "")
        if lease is not None and current != lease:
            print(
                "git-network: remote ref changed; refusing to retry the original lease",
                file=sys.stderr,
            )
            return cp
        if attempt == len(PUSH_RETRY_DELAYS):
            return cp
        delay = PUSH_RETRY_DELAYS[attempt]
        print(
            f"git-network: transient push failure; retry {attempt + 1}/{len(PUSH_RETRY_DELAYS)} after {delay}s",
            file=sys.stderr,
        )
        time.sleep(delay)
        # Freeze symbolic sources and the original delete/force expectation.
        options = args[index + 1 :]
        flags = [
            arg
            for arg in options
            if arg.startswith("-")
            and arg not in ("-f", "--force", "-d", "--delete", "--force-with-lease")
            and not arg.startswith("--force-with-lease=")
        ]
        if lease is not None:
            flags.append(f"--force-with-lease={destination}:{lease}")
        command = ["git", *prefix, "push", *flags, remote, f"{sha}:{destination}"]
    return cp


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    args = options.args[1:] if options.args[:1] == ["--"] else options.args
    if not args:
        parser.error("git arguments required")
    cp = run_git(args, timeout=options.timeout)
    sys.stdout.write(redact(cp.stdout) if cp.returncode else cp.stdout)
    if cp.returncode == 0:
        # git reports a push's ref update on stderr; callers show that line.
        sys.stderr.write(redact(cp.stderr))
    return cp.returncode


if __name__ == "__main__":
    sys.exit(main())
