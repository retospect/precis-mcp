#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""scripts/lib/land_notify.py — after a land, move the refs the subject names.

    python3 scripts/lib/land_notify.py --sha SHA --subject 'feat(x): y (gr1, td2)'
    python3 scripts/lib/land_notify.py --replay [--pending-dir DIR]

scripts/ship calls this once its CAS push to main succeeded (gr541800,
td541978). It reads the parenthesised handle list ending the squash
subject — ``(gr123)``, ``(gr1, td2)``, ``(td9 gr8)`` — and, per handle:

- gripe ``grN``: comment ``resolved: <sha9> (main, release pending) —
  landed: <subject>`` with ``STATUS:auto_verify`` in the same put
  (comment first, so the handler's ``resolved:`` guard sees it). The
  gripe now waits for scripts/gripe-validate to flip it done once the
  sha is on origin/prod. ``--verify-kind user`` (or
  ``PRECIS_VERIFY_KIND=user``) writes ``STATUS:user_verify`` instead —
  the lander picks which of the two verify states the check needs
  (gr543584): code can check ``auto_verify``, a human checks
  ``user_verify``.
- todo ``tdN``: comment ``landed: <sha9> — <subject>``, then
  ``STATUS:done`` (the todo handler releases every ``claimed-by:`` lease
  on a terminal STATUS).

Each write is one ``scripts/prod-precis tools put|tag`` call, run from the
first tree that contains origin/prod (scripts/gripe-note's ``write_tree``;
prod-precis refuses writes from a stale tree). Best-effort by contract: it
always exits 0 and prints ONE line naming what moved and what failed; a
missing DSN, a refusal or a crash is reported. A call faulting past
TIMEOUT_S (or on another transient error) is retried ONCE at double the
timeout (gr543833 — a momentarily dead prod used to lose the note
outright). Skips when PRECIS_LAND_NOTIFY=0 or nothing is named.

The notes are journaled, at-least-once: each handle's exact calls are
written to ``<repo>/scratch/land-notify-pending/<sha9>-<handle>.json``
BEFORE they run and deleted only once the handle posted — a notify that
fails, crashes or is killed mid-run leaves its unsent notes replayable.
``--replay`` posts every file in the dir and deletes it on success;
``scripts/ship --replay-notes`` is the same thing from the ship driver.
A failed call also stamps ``failure`` onto its note — the argv tail,
the exit and the FULL stderr (gr544215: the console's one ``_tail``
line hid the resolve-put refusal's cause).

Test seams: PRECIS_LAND_NOTIFY_CLI replaces the prod-precis command (and
skips the DSN check); PRECIS_LAND_NOTIFY_TIMEOUT overrides TIMEOUT_S.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
DSN_FILE = Path.home() / ".secrets" / "pw" / "PRECIS_DATABASE_URL"
#: One prod call's ceiling (`uv run` start + one handler call).
TIMEOUT_S = 60.0
#: A subject names a handful of refs; past this the tail is reported only.
MAX_HANDLES = 8
#: Journal for unsent notes: one ``<sha9>-<handle>.json`` per handle,
#: drained by ``--replay`` / ``scripts/ship --replay-notes``.
PENDING_DIR = SCRIPTS.parent / "scratch" / "land-notify-pending"
REPLAY_HINT = "replay: scripts/ship --replay-notes"

_HANDLE = r"(?:gr|td)\d+"
_SUBJECT_RE = re.compile(rf"\(({_HANDLE}(?:[,\s]+{_HANDLE})*)\)\s*$")
_ITEM_RE = re.compile(r"(gr|td)(\d+)")

Runner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


def parse_handles(subject: str) -> list[tuple[str, int]]:
    """``[('gripe', N) | ('todo', N), ...]`` from the subject's trailing
    parenthesised list, distinct, in order. Any other mention is context."""
    m = _SUBJECT_RE.search(subject.strip())
    if m is None:
        return []
    seen: dict[tuple[str, int], None] = {}
    for item in _ITEM_RE.finditer(m.group(1)):
        kind = "gripe" if item.group(1) == "gr" else "todo"
        seen.setdefault((kind, int(item.group(2))), None)
    return list(seen)


def payloads(
    sha: str, subject: str, *, verify_kind: str = "auto"
) -> list[tuple[str, list[list[str]]]]:
    """Per handle: its label and the ``prod-precis`` argv tails to run, in
    order. ``verify_kind`` is ``auto`` (``STATUS:auto_verify``) or
    ``user`` (``STATUS:user_verify``) — the lander's pick. Pure — the
    unit under test."""
    short = sha[:9]
    title = subject.strip()
    status = "auto_verify" if verify_kind == "auto" else "user_verify"
    out: list[tuple[str, list[list[str]]]] = []
    for kind, n in parse_handles(subject)[:MAX_HANDLES]:
        if kind == "gripe":
            text = f"resolved: {short} (main, release pending) — landed: {title}"
            calls = [
                [
                    "tools", "put", "--kind", "gripe", "--id", str(n),
                    "--text", text, "--tags", json.dumps([f"STATUS:{status}"]),
                ]
            ]  # fmt: skip
            out.append((f"gr{n} {status}", calls))
        else:
            text = f"landed: {short} — {title}"
            calls = [
                ["tools", "put", "--kind", "todo", "--id", str(n), "--text", text],
                [
                    "tools", "tag", "--kind", "todo", "--id", str(n),
                    "--add", json.dumps(["STATUS:done"]),
                ],
            ]  # fmt: skip
            out.append((f"td{n} done", calls))
    return out


def _gripe_note() -> ModuleType:
    """scripts/gripe-note as a module — its ``write_tree`` / ``run_capped``."""
    path = SCRIPTS / "gripe-note"
    loader = importlib.machinery.SourceFileLoader("_gripe_note", str(path))
    spec = importlib.util.spec_from_loader("_gripe_note", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), mod.__dict__)
    return mod


def _tail(text: str) -> str:
    last = (text.strip().splitlines() or [""])[-1]
    return re.sub(r"[a-z]+://\S+", "<url>", last)[:160]


def _post(
    run: Runner, cmd: list[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """One prod call, retried once at double the timeout on a transient
    fault (gr543833). A non-zero rc is a refusal — returned, not retried;
    a second fault propagates."""
    try:
        return run(cmd, timeout)
    except (OSError, subprocess.SubprocessError):
        return run(cmd, timeout * 2)


def _record_failure(path: Path, failure: dict[str, Any] | None) -> None:
    """Stamp the failed call's detail onto its pending note.

    The console line carries one ``_tail`` line; the note keeps the
    call's argv tail, exit and FULL stderr for the postmortem
    (gr544215). Best-effort like the journal itself — a scratch-dir
    fault loses the detail, not the note.
    """
    if failure is None:
        return
    try:
        note = json.loads(path.read_text(encoding="utf-8"))
        note["failure"] = failure
        path.write_text(json.dumps(note, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, TypeError):
        pass


def _journal(
    pending_dir: Path, sha: str, subject: str, label: str, calls: list[list[str]]
) -> Path | None:
    """The at-least-once record: the handle's exact ``prod-precis`` argv
    tails, written BEFORE they run so a kill mid-notify still leaves the
    note replayable (gr543833). A scratch-dir fault costs the journal, not
    the notify."""
    path = pending_dir / f"{sha[:9]}-{label.split()[0]}.json"
    try:
        pending_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"sha": sha, "subject": subject, "label": label, "calls": calls},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        return None
    return path


def notify(
    sha: str,
    subject: str,
    *,
    base: list[str],
    run: Runner,
    timeout: float,
    verify_kind: str = "auto",
    pending_dir: Path | None = None,
) -> str:
    """Run every payload; return the one summary line. ``pending_dir``
    turns on the journal — every handle's file is written up front, deleted
    when it posts and left behind (named in the summary) when it fails."""
    plan = payloads(sha, subject, verify_kind=verify_kind)
    over = len(parse_handles(subject)) - len(plan)
    # Journal EVERY handle before the first call runs — a kill mid-notify
    # then leaves each unposted handle's calls replayable (gr543833).
    pending = (
        {
            label.split()[0]: _journal(pending_dir, sha, subject, label, calls)
            for label, calls in plan
        }
        if pending_dir is not None
        else {}
    )
    ok: list[str] = []
    failed: list[str] = []
    saved: list[str] = []
    for label, calls in plan:
        handle = label.split()[0]
        why = ""
        failure: dict[str, Any] | None = None
        for call in calls:
            try:
                cp = _post(run, [*base, *call], timeout)
            except (OSError, subprocess.SubprocessError) as e:
                why = f"{handle} ({type(e).__name__})"
                failure = {"call": call, "error": type(e).__name__}
                break
            if cp.returncode != 0:
                why = f"{handle} (exit {cp.returncode}: {_tail(cp.stderr)})"
                failure = {
                    "call": call,
                    "exit": cp.returncode,
                    "stderr": cp.stderr,
                }
                break
        p = pending.get(handle)
        if why:
            failed.append(why)
            if p is not None:
                _record_failure(p, failure)
                saved.append(p.name)
        else:
            ok.append(label)
            if p is not None:
                p.unlink(missing_ok=True)
    parts = []
    if ok:
        parts.append(" · ".join(ok))
    if failed:
        parts.append("failed: " + "; ".join(failed))
    if over > 0:
        parts.append(f"+{over} more not posted")
    if saved:
        parts.append(f"unsent: {', '.join(saved)} in {pending_dir} — {REPLAY_HINT}")
    return f"land-notify {sha[:9]}: " + (" · ".join(parts) or "nothing to do")


def replay(
    pending_dir: Path, *, base: list[str], run: Runner, timeout: float
) -> tuple[int, int]:
    """Post every journaled note under ``pending_dir``; a file is deleted
    only once its calls all succeeded. Returns ``(posted, left)``."""
    posted = left = 0
    for path in sorted(pending_dir.glob("*.json")):
        try:
            note = json.loads(path.read_text(encoding="utf-8"))
            calls = [list(c) for c in note["calls"]]
        except (OSError, ValueError, TypeError) as e:
            print(
                f"land-notify replay: {path.name} unreadable ({type(e).__name__}) — kept"
            )
            left += 1
            continue
        label = str(note.get("label") or path.stem)
        why = ""
        failure: dict[str, Any] | None = None
        for call in calls:
            try:
                cp = _post(run, [*base, *call], timeout)
            except (OSError, subprocess.SubprocessError) as e:
                why = type(e).__name__
                failure = {"call": call, "error": why}
                break
            if cp.returncode != 0:
                why = f"exit {cp.returncode}: {_tail(cp.stderr)}"
                failure = {"call": call, "exit": cp.returncode, "stderr": cp.stderr}
                break
        if why:
            _record_failure(path, failure)
            print(f"land-notify replay: {label} failed ({why}) — kept {path.name}")
            left += 1
        else:
            path.unlink()
            posted += 1
            print(f"land-notify replay: {label} posted — deleted {path.name}")
    return posted, left


def _transport() -> tuple[list[str] | None, Runner]:
    """(prod-precis argv prefix, runner) — prefix ``None`` when this host
    cannot write prod (no CLI seam and no DSN)."""
    note = _gripe_note()
    cli = os.environ.get("PRECIS_LAND_NOTIFY_CLI", "").strip()
    if cli:
        return shlex.split(cli), note.run_capped
    if not DSN_FILE.is_file():
        return None, note.run_capped
    return [
        str(note.write_tree(SCRIPTS.parent) / "scripts" / "prod-precis")
    ], note.run_capped


def _replay(args: argparse.Namespace) -> int:
    """``--replay``: drain the pending dir. Unlike the notify path this is
    an operator verb, so it reports honestly — exit 1 while anything is
    left pending."""
    if os.environ.get("PRECIS_LAND_NOTIFY", "1") == "0":
        print("land-notify replay: PRECIS_LAND_NOTIFY=0 — skipped")
        return 1
    files = sorted(args.pending_dir.glob("*.json"))
    if not files:
        print(f"land-notify replay: nothing pending in {args.pending_dir}")
        return 0
    try:
        base, run = _transport()
    except Exception as e:
        print(f"land-notify replay: {type(e).__name__} — {len(files)} left pending")
        return 1
    if base is None:
        print(
            f"land-notify replay: no prod DSN on this host — {len(files)} left pending"
        )
        return 1
    timeout = float(os.environ.get("PRECIS_LAND_NOTIFY_TIMEOUT") or TIMEOUT_S)
    posted, left = replay(args.pending_dir, base=base, run=run, timeout=timeout)
    print(f"land-notify replay: {posted} posted, {left} pending")
    return 0 if left == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scripts/lib/land_notify.py", description=__doc__)
    ap.add_argument("--sha", help="the landed squash sha")
    ap.add_argument("--subject", help="the squash subject line")
    ap.add_argument("--dry-run", action="store_true", help="print, do not post")
    ap.add_argument(
        "--verify-kind",
        choices=("auto", "user"),
        default="",
        help=(
            "verify state the named gripes take: 'auto' (STATUS:auto_verify, "
            "default) or 'user' (STATUS:user_verify). Env PRECIS_VERIFY_KIND."
        ),
    )
    ap.add_argument(
        "--replay",
        action="store_true",
        help="post every pending note under --pending-dir, deleting each on success",
    )
    ap.add_argument(
        "--pending-dir",
        type=Path,
        default=PENDING_DIR,
        help="journal dir for unsent notes (default %(default)s)",
    )
    args = ap.parse_args(argv)
    if args.replay:
        return _replay(args)
    if not args.sha or not args.subject:
        ap.error("needs --sha and --subject, or --replay")

    verify_kind = args.verify_kind or os.environ.get("PRECIS_VERIFY_KIND", "auto")
    if verify_kind not in ("auto", "user"):
        print(f"land-notify: bad verify kind {verify_kind!r} — using auto")
        verify_kind = "auto"
    plan = payloads(args.sha, args.subject, verify_kind=verify_kind)
    if not plan:
        return 0
    named = " ".join(label.split()[0] for label, _ in plan)
    if args.dry_run:
        print(f"land-notify: would move {' · '.join(lbl for lbl, _ in plan)}")
        return 0
    if os.environ.get("PRECIS_LAND_NOTIFY", "1") == "0":
        print(f"land-notify: PRECIS_LAND_NOTIFY=0 — skipped {named}")
        return 0
    cli = os.environ.get("PRECIS_LAND_NOTIFY_CLI", "").strip()
    if not cli and not DSN_FILE.is_file():
        print(f"land-notify: no prod DSN on this host — skipped {named}")
        return 0
    try:
        base, run = _transport()
        assert base is not None  # cli set or DSN present — checked above
        timeout = float(os.environ.get("PRECIS_LAND_NOTIFY_TIMEOUT") or TIMEOUT_S)
        line = notify(
            args.sha,
            args.subject,
            base=base,
            run=run,
            timeout=timeout,
            verify_kind=verify_kind,
            pending_dir=args.pending_dir,
        )
        print(line)
    except Exception as e:  # best-effort: never fail the ship
        print(f"land-notify: {type(e).__name__} — {named} not moved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
