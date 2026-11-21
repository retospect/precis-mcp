"""The attempt hub's ``pre-receive`` hook (gr741695): the fence around ``refs/factory/build/*``.

``ensure_hub`` (``precis.workers.job_types._slice_ws``) installs a small
``sh`` stub in the hub that runs this module under the worker's own
interpreter, isolated (``python -I``) with the directory of the worker's own
``precis`` package first on ``sys.path``, so a session's ``PYTHONPATH`` or
working directory cannot substitute other code. The rules live here, in
deployed code, so a branch cannot change them.

The module sits in :mod:`precis.utils` and imports nothing outside the
standard library and :mod:`precis.utils.secret_scan`: ``precis.workers``
imports the embedding stack, which would put a second of start-up on every
push. One hook run costs a Python start, two ``git`` calls and the scan.

What the hook enforces, on every push, from the ``<old> <new> <ref>``
lines git writes to its stdin:

* **Who may write what.** A push is accepted from one of two identities.
  A session has ``PRECIS_SLICE_ID`` (the engine writes it as ``td<todo>``)
  and ``PRECIS_SLICE_JOB``, and may write one ref,
  ``refs/factory/build/<todo>/<job>``. The worker sets
  ``PRECIS_HUB_WORKER=1`` on its own pushes and may write attempt refs
  ``refs/factory/build/<digits>/<digits>``. A push with neither is refused
  whole: a builder shell that lost its environment is told the worker saves
  its work at the job's end. The worker never writes a branch through the
  hub's hook (its base sync and legacy import are fetches).
* **No deletions.** The worker deletes attempt refs with ``update-ref``,
  never by push.
* **Commits only.** The new object of a ref must be a commit: a tree, blob
  or annotated tag could carry content the scan below does not read.
* **No credential in the commits a push adds.** The commits
  ``git rev-list <new> --not --all`` names are scanned with
  :func:`precis.utils.secret_scan.find_secrets`: every added line and every
  commit message. Every accepted push is scanned.

A refusal prints :data:`REFUSED` ``(<reason>)`` as the first line on
stderr, then what to do about it; a secret is named by ``path:line`` and
pattern, never echoed. The hook fails closed: when it cannot read git or
hits any error it refuses with a line saying so. The stub does the same
when this module cannot be imported. The hook runs inside git's
quarantine, so it leaves the ``GIT_*`` environment as it found it.

The builder and the worker share one OS account: the hook guards against a
mistake, not against a session that writes the hub's files directly.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

#: First line on stderr of every refusal; ``_slice_ws.HOOK_REFUSED`` is the
#: same text (a test holds the two equal).
REFUSED = "precis-hub: refused"
#: Reason of a refusal for a credential-shaped string.
SECRET = "secret-scan"

#: Reasons of the other refusals.
REASON_REF = "ref rule"
REASON_DELETE = "deletion"
REASON_ERROR = "hook error"
REASON_IDENTITY = "no identity"
REASON_OBJECT = "not a commit"

#: Exit status of a refusal. The stub turns it into git's refusal status
#: and treats any other non-zero status as the hook itself failing.
EXIT_REFUSED = 3

ENV_TODO = "PRECIS_SLICE_ID"
ENV_JOB = "PRECIS_SLICE_JOB"
#: Set to ``1`` by the worker on its own pushes (``_slice_ws.ENV_WORKER``).
ENV_WORKER = "PRECIS_HUB_WORKER"

_ZERO = "0" * 40
_SHA = re.compile(r"[0-9a-f]{40}")
_BUILD_REF = re.compile(r"refs/factory/build/\d+/\d+")
_TODO_ID = re.compile(r"(?:td)?([0-9]+)")
_JOB_ID = re.compile(r"[0-9]+")
_HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

#: Most findings listed in one refusal; the rest are counted.
MAX_LISTED = 20
#: Seconds one git call may take before the hook refuses.
GIT_TIMEOUT_S = 120


def log_flags(nonce: str) -> tuple[str, ...]:
    """The ``git log`` flags the scan reads. The commit-message block is
    bracketed by lines holding a one-time ``nonce``, so no message can
    forge the end of itself and pass its own text off as a patch header."""
    return (
        "--no-walk=unsorted",
        "--stdin",
        "--full-history",
        "--root",
        "-m",
        "-p",
        "--text",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "-U0",
        f"--format={nonce}:commit:%H%n%B%n{nonce}:end",
    )


@dataclass(frozen=True)
class Refusal:
    """One reason the push is refused and what the pusher does about it."""

    reason: str
    detail: tuple[str, ...]


@dataclass(frozen=True)
class Leak:
    """One credential-shaped string: where it is and which pattern it matched.
    The text itself is never kept."""

    path: str
    line: int
    kind: str
    commit: str


class HookError(RuntimeError):
    """Git could not be read; the push is refused."""


def parse_updates(text: str) -> list[tuple[str, str, str]]:
    """``(old, new, ref)`` for each line git sent. Raises ``ValueError`` on a
    line that is not three words with two commit ids."""
    out: list[tuple[str, str, str]] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parts = raw.split()
        if len(parts) != 3 or not all(_SHA.fullmatch(p) for p in parts[:2]):
            raise ValueError(f"unreadable update line {raw[:80]!r}")
        out.append((parts[0], parts[1], parts[2]))
    return out


def session_ref(env: Mapping[str, str]) -> str | None:
    """The one ref the pushing session may write, or ``None`` when the push is
    not a session's (``PRECIS_SLICE_ID`` unset). The engine writes the todo
    as ``td<N>`` (``run_slice.slice_env``); a bare number reads the same.
    Raises ``ValueError`` when the session's identity is incomplete or not
    numeric."""
    todo = env.get(ENV_TODO)
    if todo is None:
        return None
    todo_m = _TODO_ID.fullmatch(todo)
    job_m = _JOB_ID.fullmatch(env.get(ENV_JOB) or "")
    if todo_m is None or job_m is None:
        raise ValueError(
            f"{ENV_TODO} must be td<number> and {ENV_JOB} a number "
            "(the session's environment is incomplete)"
        )
    return f"refs/factory/build/{int(todo_m.group(1))}/{int(job_m.group(0))}"


def ref_refusals(
    updates: Iterable[tuple[str, str, str]],
    own: str | None,
    *,
    worker: bool = False,
) -> list[Refusal]:
    """The ref rules over every update: deletions, then who may write which
    ref. ``own`` is :func:`session_ref`'s answer; ``worker`` is true for a
    push that carries :data:`ENV_WORKER`. With neither, every update that is
    not a deletion is refused for want of an identity."""
    out: list[Refusal] = []
    unidentified = False
    for _old, new, ref in updates:
        if new == _ZERO:
            out.append(
                Refusal(
                    REASON_DELETE,
                    (
                        f"{ref}: the hub takes no deletions by push",
                        "an attempt ref is deleted by the worker after a land "
                        "or a start-over; leave it alone",
                    ),
                )
            )
        elif own is not None:
            if ref != own:
                out.append(
                    Refusal(
                        REASON_REF,
                        (
                            f"{ref}: this session may push only {own}",
                            f"push with: git push origin HEAD:{own} "
                            "(add --force-with-lease after a rebase)",
                        ),
                    )
                )
        elif worker:
            if not _BUILD_REF.fullmatch(ref):
                out.append(
                    Refusal(
                        REASON_REF,
                        (
                            f"{ref}: the hub takes attempt refs "
                            "(refs/factory/build/<todo>/<job>) only",
                        ),
                    )
                )
        elif not unidentified:
            unidentified = True
            out.append(
                Refusal(
                    REASON_IDENTITY,
                    (
                        f"this push has no session identity: {ENV_TODO} and "
                        f"{ENV_JOB} are not set in its environment",
                        "the worker saves your work to the hub at the end of the "
                        "job; do not push from a shell that lost the session's "
                        "environment",
                    ),
                )
            )
    return out


def _git(args: Sequence[str], *, stdin: str | None = None) -> str:
    """Stdout of ``git <args>``. Raises :class:`HookError` on any failure."""
    try:
        proc = subprocess.run(
            ["git", "--no-replace-objects", "-c", "core.quotePath=false", *args],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HookError(f"git did not run ({type(exc).__name__})") from exc
    if proc.returncode != 0:
        raise HookError(f"git {args[0]} failed: {(proc.stderr or '').strip()[:200]}")
    return proc.stdout or ""


def new_commits(shas: Iterable[str]) -> list[str]:
    """The commits the push adds: reachable from a new tip, from no ref the
    hub holds now (inside the quarantine the new objects are visible, the
    new refs are not yet)."""
    found: dict[str, None] = {}
    for sha in shas:
        for line in _git(["rev-list", sha, "--not", "--all"]).splitlines():
            if _SHA.fullmatch(line.strip()):
                found[line.strip()] = None
    return list(found)


def scan_patch(text: str, nonce: str) -> list[Leak]:
    """Findings in the stream ``git log`` printed for ``log_flags(nonce)``:
    commit messages and added lines (a removed line is not new content, and
    a credential deleted from published history cannot be fixed by the
    pusher). A message runs from its ``<nonce>:commit:`` line to its
    ``<nonce>:end`` line, whatever it says; only after that line does a
    ``diff --git`` header count. In a patch, lines always start with a diff
    marker, so a line starting ``diff --git`` or ``@@`` is a header, never
    content."""
    from precis.utils.secret_scan import find_secrets

    start = f"{nonce}:commit:"
    end = f"{nonce}:end"
    out: list[Leak] = []
    commit = ""
    state = "between"
    path = ""
    msg_line = 0
    new_line = 0
    for raw in text.split("\n"):
        if raw.startswith(start):
            commit = raw[len(start) :].strip()[:7]
            state, path, msg_line = "message", "", 0
            continue
        if state == "message":
            if raw == end:
                state = "between"
                continue
            msg_line += 1
            for hit in find_secrets(raw):
                out.append(Leak(f"(message of {commit})", msg_line, hit.kind, commit))
            continue
        if raw.startswith("diff --git ") or raw.startswith("diff --cc "):
            state, path = "header", ""
            continue
        if state == "header":
            if raw.startswith("+++ "):
                name = raw[4:].split("\t")[0]
                path = name[2:] if name.startswith("b/") else name
            elif raw.startswith("@@"):
                state = "hunk"
                m = _HUNK.match(raw)
                new_line = int(m.group(1)) if m else 0
        elif state == "hunk":
            if raw.startswith("@@"):
                m = _HUNK.match(raw)
                new_line = int(m.group(1)) if m else new_line
            elif raw.startswith("+"):
                for hit in find_secrets(raw[1:]):
                    out.append(
                        Leak(path or "(unnamed file)", new_line, hit.kind, commit)
                    )
                new_line += 1
    return out


def scan_commits(commits: Sequence[str]) -> list[Leak]:
    """Findings across ``commits`` (one ``git log`` call), distinct by
    path, line and pattern."""
    if not commits:
        return []
    nonce = secrets.token_hex(12)
    text = _git(["log", *log_flags(nonce)], stdin="\n".join(commits) + "\n")
    seen: dict[tuple[str, int, str], Leak] = {}
    for f in scan_patch(text, nonce):
        seen.setdefault((f.path, f.line, f.kind), f)
    return list(seen.values())


def secret_refusal(findings: Sequence[Leak]) -> Refusal:
    """The refusal for ``findings``: each as ``path:line: <pattern>``, never
    the text, then the instruction."""
    lines = [
        f"{f.path}:{f.line}: {f.kind} (commit {f.commit})"
        for f in findings[:MAX_LISTED]
    ]
    if len(findings) > MAX_LISTED:
        lines.append(f"... and {len(findings) - MAX_LISTED} more")
    lines.append(
        "remove it from every commit you push (amend or rebase), then push again"
    )
    return Refusal(SECRET, tuple(lines))


def check(stdin_text: str, env: Mapping[str, str]) -> list[Refusal]:
    """Every refusal for one push; empty when it is accepted."""
    updates = parse_updates(stdin_text)
    try:
        own = session_ref(env)
    except ValueError as exc:
        return [Refusal(REASON_REF, (str(exc),))]
    refusals = ref_refusals(updates, own, worker=env.get(ENV_WORKER) == "1")
    if refusals:
        return refusals
    for _old, new, ref in updates:
        kind = _git(["cat-file", "-t", new]).strip()
        if kind != "commit":
            return [
                Refusal(
                    REASON_OBJECT,
                    (
                        f"{ref}: the new object is a {kind}, not a commit",
                        "push a branch tip: git push origin HEAD:<your ref>",
                    ),
                )
            ]
    findings = scan_commits(new_commits([new for _old, new, _ref in updates]))
    return [secret_refusal(findings)] if findings else []


def render(refusals: Sequence[Refusal]) -> str:
    """The stderr text: the marker line first, then each reason's detail."""
    head = f"{REFUSED} ({refusals[0].reason})"
    body = [head]
    for r in refusals:
        if r is not refusals[0]:
            body.append(f"also refused ({r.reason}):")
        body.extend(f"  {line}" for line in r.detail)
    return "\n".join(body) + "\n"


def main(argv: Sequence[str]) -> int:
    """``pre-receive``: 0 to accept, :data:`EXIT_REFUSED` to refuse."""
    if list(argv) != ["pre-receive"]:
        sys.stderr.write(f"{REFUSED} ({REASON_ERROR})\n  usage: hub_hook pre-receive\n")
        return EXIT_REFUSED
    try:
        refusals = check(sys.stdin.read(), os.environ)
    except Exception as exc:
        sys.stderr.write(
            f"{REFUSED} ({REASON_ERROR})\n  {type(exc).__name__}: {str(exc)[:300]}\n"
            "  the hub could not check this push, so it takes nothing; "
            "tell the operator\n"
        )
        return EXIT_REFUSED
    if refusals:
        sys.stderr.write(render(refusals))
        return EXIT_REFUSED
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
