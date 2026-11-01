"""Operator-only acceptance of trace pairs; no graph writes, commits or pushes."""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import psycopg

from precis.cli._common import resolve_dsn

PROPOSALS_KEY = "recall_set_proposals"
SET_PATH = "src/precis/data/memory_recall_set.json"
FIELDS = {"query", "want", "title", "hook", "source", "why"}


def _todo_id(value: str) -> int:
    match = re.fullmatch(r"(?:td)?([1-9][0-9]*)", value)
    if match is None:
        raise argparse.ArgumentTypeError("use td<N> or a positive todo id")
    return int(match[1])


def _only(value: str) -> list[int]:
    if re.fullmatch(r"[1-9][0-9]*(?:,[1-9][0-9]*)*", value) is None:
        raise argparse.ArgumentTypeError("use 1-based pair numbers, e.g. --only 1,3")
    return list(dict.fromkeys(int(n) for n in value.split(",")))


def configure_parser(sub: argparse._SubParsersAction) -> None:
    """Extend memory's nested verbs, not the top-level command registry."""
    parser = sub.add_parser("recall-set", help="Review trace-derived recall pairs.")
    verbs = parser.add_subparsers(dest="recall_set_cmd", required=True)
    accept = verbs.add_parser(
        "accept", help="Append reviewed pairs in a repo checkout; never commit/push."
    )
    accept.add_argument("todo_id", type=_todo_id)
    accept.add_argument("--only", type=_only, default=None, metavar="N,M")
    accept.add_argument("--database-url", default=None)


def checkout_set_path() -> Path:
    """Require the tracked set in the current checkout, not the installed wheel."""
    root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if root.returncode:
        raise ValueError("run from a precis Git work tree; no files changed")
    checkout = Path(root.stdout.strip()).resolve()
    path = checkout / SET_PATH
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", SET_PATH],
        cwd=checkout,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if (
        tracked.returncode
        or not path.is_file()
        or not path.resolve().is_relative_to(checkout)
    ):
        raise ValueError(f"checkout must contain the tracked {SET_PATH}")
    return path


def accept_proposals(
    path: Path, proposals: Any, *, only: list[int] | None = None
) -> tuple[int, str]:
    """Validate before writing; keep the six-field schema and existing file lines."""
    if not isinstance(proposals, list) or not proposals:
        raise ValueError(
            "todo has no recall-set proposals; select a harvest review todo"
        )
    numbers = only if only is not None else list(range(1, len(proposals) + 1))
    if not numbers or any(n < 1 or n > len(proposals) for n in numbers):
        raise ValueError(f"--only must select pair numbers 1..{len(proposals)}")
    selected = []
    for n in dict.fromkeys(numbers):
        pair = proposals[n - 1]
        if (
            not isinstance(pair, dict)
            or pair.keys() != FIELDS
            or any(not isinstance(v, str) for v in pair.values())
            or not pair["query"].strip()
            or re.fullmatch(r"me[1-9][0-9]*", pair["want"]) is None
            or pair["source"] != "trace"
            or re.search(r"\bag[1-9][0-9]*\b", pair["why"]) is None
        ):
            raise ValueError(f"pair {n} is not a six-field trace-backed recall entry")
        selected.append(pair)
    before = path.read_text(encoding="utf-8")
    entries = json.loads(before)
    if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
        raise ValueError("recall set must be a JSON list of entries")
    seen = {(e["query"], e["want"]) for e in entries}
    additions = []
    for pair in selected:
        key = (pair["query"], pair["want"])
        if key not in seen:
            additions.append(pair)
            seen.add(key)
    if not additions:
        return 0, ""
    prefix = before[: before.rfind("]")].rstrip()
    lines = ["  " + json.dumps(pair, ensure_ascii=True) for pair in additions]
    after = prefix + ("," if entries else "") + "\n" + ",\n".join(lines) + "\n]\n"
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{SET_PATH}",
            tofile=f"b/{SET_PATH}",
        )
    )
    path.write_text(after, encoding="utf-8")
    return len(additions), diff


def run(args: argparse.Namespace) -> None:
    """Read one queue batch and edit only the checkout file."""
    try:
        path = checkout_set_path()
        with (
            psycopg.connect(resolve_dsn(args.database_url)) as conn,
            conn.transaction(),
        ):
            conn.execute("SET TRANSACTION READ ONLY")
            row = conn.execute(
                "SELECT meta FROM refs WHERE kind = 'todo' AND ref_id = %s "
                "AND retired_at IS NULL",
                (args.todo_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"td{args.todo_id} is not a live todo")
        count, diff = accept_proposals(path, row[0].get(PROPOSALS_KEY), only=args.only)
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit(f"precis memory recall-set accept: refused: {exc}") from exc
    print(diff, end="")
    print(
        f"Appended {count} pairs to {path}. No graph writes, commits or pushes. "
        "Operator: review the diff for private data, land it, then close the "
        "review todo when all pairs are settled; no fixer is queued."
    )
