#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""scripts/lib/land_notify.py — after a land, move the refs the subject names.

    python3 scripts/lib/land_notify.py --sha SHA --subject 'feat(x): y (gr1, td2)'

scripts/ship calls this once its CAS push to main succeeded (gr541800,
td541978). It reads the parenthesised handle list ending the squash
subject — ``(gr123)``, ``(gr1, td2)``, ``(td9 gr8)`` — and, per handle:

- gripe ``grN``: comment ``resolved: <sha9> (main, release pending) —
  landed: <subject>`` with ``STATUS:verify`` in the same put (comment
  first, so the handler's ``resolved:`` guard sees it). The gripe now
  waits for scripts/gripe-validate to flip it done once the sha is on
  origin/prod.
- todo ``tdN``: comment ``landed: <sha9> — <subject>``, then
  ``STATUS:done`` (the todo handler releases every ``claimed-by:`` lease
  on a terminal STATUS).

Each write is one ``scripts/prod-precis tools put|tag`` call, run from the
first tree that contains origin/prod (scripts/gripe-note's ``write_tree``;
prod-precis refuses writes from a stale tree). Best-effort by contract: it
always exits 0 and prints ONE line naming what moved and what failed; a
missing DSN, a refusal, a crash or a call past TIMEOUT_S is reported, not
retried. Skips when PRECIS_LAND_NOTIFY=0 or nothing is named.

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

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
DSN_FILE = Path.home() / ".secrets" / "pw" / "PRECIS_DATABASE_URL"
#: One prod call's ceiling (`uv run` start + one handler call).
TIMEOUT_S = 60.0
#: A subject names a handful of refs; past this the tail is reported only.
MAX_HANDLES = 8

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


def payloads(sha: str, subject: str) -> list[tuple[str, list[list[str]]]]:
    """Per handle: its label and the ``prod-precis`` argv tails to run, in
    order. Pure — the unit under test."""
    short = sha[:9]
    title = subject.strip()
    out: list[tuple[str, list[list[str]]]] = []
    for kind, n in parse_handles(subject)[:MAX_HANDLES]:
        if kind == "gripe":
            text = f"resolved: {short} (main, release pending) — landed: {title}"
            calls = [
                [
                    "tools", "put", "--kind", "gripe", "--id", str(n),
                    "--text", text, "--tags", json.dumps(["STATUS:verify"]),
                ]
            ]  # fmt: skip
            out.append((f"gr{n} verify", calls))
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


def notify(
    sha: str, subject: str, *, base: list[str], run: Runner, timeout: float
) -> str:
    """Run every payload; return the one summary line."""
    plan = payloads(sha, subject)
    over = len(parse_handles(subject)) - len(plan)
    ok: list[str] = []
    failed: list[str] = []
    for label, calls in plan:
        handle = label.split()[0]
        for call in calls:
            try:
                cp = run([*base, *call], timeout)
            except (OSError, subprocess.SubprocessError) as e:
                failed.append(f"{handle} ({type(e).__name__})")
                break
            if cp.returncode != 0:
                failed.append(f"{handle} (exit {cp.returncode}: {_tail(cp.stderr)})")
                break
        else:
            ok.append(label)
    parts = []
    if ok:
        parts.append(" · ".join(ok))
    if failed:
        parts.append("failed: " + "; ".join(failed))
    if over > 0:
        parts.append(f"+{over} more not posted")
    return f"land-notify {sha[:9]}: " + (" · ".join(parts) or "nothing to do")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scripts/lib/land_notify.py", description=__doc__)
    ap.add_argument("--sha", required=True, help="the landed squash sha")
    ap.add_argument("--subject", required=True, help="the squash subject line")
    ap.add_argument("--dry-run", action="store_true", help="print, do not post")
    args = ap.parse_args(argv)

    plan = payloads(args.sha, args.subject)
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
        note = _gripe_note()
        base = (
            shlex.split(cli)
            if cli
            else [str(note.write_tree(SCRIPTS.parent) / "scripts" / "prod-precis")]
        )
        timeout = float(os.environ.get("PRECIS_LAND_NOTIFY_TIMEOUT") or TIMEOUT_S)
        line = notify(
            args.sha, args.subject, base=base, run=note.run_capped, timeout=timeout
        )
        print(line)
    except Exception as e:  # best-effort: never fail the ship
        print(f"land-notify: {type(e).__name__} — {named} not moved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
