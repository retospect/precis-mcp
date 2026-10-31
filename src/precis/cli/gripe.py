"""``precis gripes`` — DEPRECATED dump of filed gripes.

Was the v0 human-triage path when ``gripe`` was a write-only kind
on the MCP surface. As of migration 0005 gripe is a full
first-class kind — ``get(kind='gripe', id=N)`` reads the title card
(``view='body'`` / ``view='comments'`` go deeper), ``search(kind='gripe', q=...)`` finds matches
by text or tag, ``tag``/``link``/``delete`` work normally. This
CLI duplicates that surface from the terminal and will be
removed in a follow-up release.

The implementation is preserved verbatim so existing shell
scripts keep working; only the entry point prints a one-line
deprecation notice to stderr.

``--summary`` is not deprecated: it prints the one-line open-gripe
digest (``N open · M older than 3d · oldest grX (D days)``) that the
session-start hook shows, and nothing else. ``--note TEXT --ids N,M``
is not deprecated either: it comments TEXT on each listed gripe that
does not already carry it (scripts/gripe-note's one prod call after a
landing or deploy) and prints one summary line. ``--verify-list`` /
``--verify-done TEXT --ids N,M`` are scripts/gripe-validate's two prod
calls (gr541800): list the STATUS:verify gripes with their resolved sha,
then close the ones whose sha reached origin/prod. ``--shorten-titles``
is the one-time short-title gardening pass (:func:`shorten_titles`); it
only lists unless ``--apply`` is given.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from typing import Any

import psycopg

from precis.cli._common import resolve_dsn


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``gripes`` subparser on ``sub``."""
    dp = sub.add_parser(
        "gripes",
        help="Dump all filed gripes for human triage.",
        description=(
            "Dump every filed gripe. The gripe kind is write-only from "
            "the agent surface; this CLI is the human read/triage path."
        ),
    )
    dp.add_argument("--database-url", default=None)
    dp.add_argument(
        "--include-deleted",
        action="store_true",
        help=(
            "Also print soft-deleted gripes (tombstones retained for "
            "audit are normally hidden)."
        ),
    )
    dp.add_argument(
        "--only-deleted",
        action="store_true",
        help="Print only soft-deleted gripes (implies --include-deleted).",
    )
    dp.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    dp.add_argument(
        "--oldest-first",
        action="store_true",
        help="Walk the backlog in filed order (default: newest first).",
    )
    dp.add_argument(
        "--summary",
        action="store_true",
        help="Print only the one-line open-gripe digest (session-start hook).",
    )
    dp.add_argument(
        "--note",
        default=None,
        metavar="TEXT",
        help="Comment TEXT on each --ids gripe that lacks it (scripts/gripe-note).",
    )
    dp.add_argument(
        "--ids",
        default="",
        help=(
            "Comma-separated gripe ids for --note / --verify-done "
            "(or to scope --shorten-titles)."
        ),
    )
    dp.add_argument(
        "--verify-list",
        action="store_true",
        help=(
            "Print one JSON line {id, sha} per live STATUS:verify gripe; sha "
            "is from its newest 'resolved: <sha>' / 'fix-sha: <sha>' comment "
            "(scripts/gripe-validate). Read-only."
        ),
    )
    dp.add_argument(
        "--verify-done",
        default=None,
        metavar="TEXT",
        help=(
            "Comment TEXT ({sha} = the gripe's own resolved sha) and tag "
            "STATUS:done on each --ids gripe still at STATUS:verify "
            "(scripts/gripe-validate)."
        ),
    )
    dp.add_argument(
        "--shorten-titles",
        action="store_true",
        help=(
            "List open gripes whose title is longer than the short-title cap "
            "with the derived title; writes nothing without --apply."
        ),
    )
    dp.add_argument(
        "--apply",
        action="store_true",
        help="With --shorten-titles: store each title, old title in a comment.",
    )
    dp.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of gripes printed (default: no limit).",
    )
    return dp


def run(args: argparse.Namespace) -> None:
    """Implements ``precis gripes``."""
    from precis.config import load_config
    from precis.store import Store

    if args.summary:
        _print_summary(args)
        return
    if args.verify_list or args.verify_done is not None:
        store = Store.connect(resolve_dsn(args.database_url, cfg=load_config()))
        try:
            if args.verify_list:
                for gid, sha in verify_list(store):
                    print(json.dumps({"id": gid, "sha": sha}))
            else:
                ids = [int(i) for i in args.ids.replace(" ", "").split(",") if i]
                print(verify_done(store, args.verify_done, ids))
        finally:
            store.close()
        return
    if args.note is not None:
        ids = [int(i) for i in args.ids.replace(" ", "").split(",") if i]
        store = Store.connect(resolve_dsn(args.database_url, cfg=load_config()))
        try:
            print(note_gripes(store, args.note, ids))
        finally:
            store.close()
        return
    if args.shorten_titles:
        store = Store.connect(resolve_dsn(args.database_url, cfg=load_config()))
        try:
            ids = [int(i) for i in args.ids.replace(" ", "").split(",") if i]
            for line in shorten_titles(
                store, apply=args.apply, limit=args.limit, ids=ids or None
            ):
                print(line)
        finally:
            store.close()
        return

    print(
        "precis gripes: DEPRECATED — gripe is now a full MCP kind. "
        "Use get(kind='gripe', id=N) / search(kind='gripe', q=...). "
        "This CLI will be removed in a follow-up release.",
        file=sys.stderr,
    )

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)

    include_deleted = args.include_deleted or args.only_deleted

    clauses = ["kind = %s"]
    params: list[Any] = ["gripe"]
    if args.only_deleted:
        clauses.append("retired_at IS NOT NULL")
    elif not include_deleted:
        clauses.append("retired_at IS NULL")

    order = "ASC" if args.oldest_first else "DESC"
    sql = (
        "SELECT ref_id, title, created_at, updated_at, retired_at "
        "FROM refs WHERE " + " AND ".join(clauses) + f" ORDER BY ref_id {order}"
    )
    if args.limit is not None:
        if args.limit < 0:
            print("--limit must be >= 0", file=sys.stderr)
            sys.exit(2)
        sql += " LIMIT %s"
        params.append(args.limit)

    store = Store.connect(dsn)
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()

        if args.format == "json":
            for row in rows:
                print(json.dumps(_row_to_json(row)))
            return

        # text format
        if not rows:
            scope = "gripes (incl. deleted)" if include_deleted else "live gripes"
            print(f"no {scope} on file")
            return

        total_live = sum(1 for r in rows if r[4] is None)
        total_deleted = len(rows) - total_live
        header_parts = [f"{len(rows)} gripe(s)"]
        if include_deleted:
            header_parts.append(f"live={total_live}")
            header_parts.append(f"deleted={total_deleted}")
        print(f"# {' '.join(header_parts)}")
        print()
        for row in rows:
            _print_text(row)
    finally:
        store.close()


def _print_summary(args: argparse.Namespace) -> None:
    from precis.config import load_config
    from precis.handlers.gripe import _TERMINAL_STATUS_TAGS, age_summary
    from precis.store import Store

    store = Store.connect(resolve_dsn(args.database_url, cfg=load_config()))
    try:
        # "open" is the live cohort — every non-terminal STATUS, the
        # same set get(kind='gripe', id='/open') lists (gr530682).
        line = age_summary(store, [], exclude_tags=sorted(_TERMINAL_STATUS_TAGS))
        print(f"gripes: {line} — get(kind='gripe', id='/open')")
    finally:
        store.close()


def note_gripes(store: Any, text: str, ids: list[int]) -> str:
    """Comment ``text`` on each gripe in ``ids`` unless it already has it.

    Returns ``commented grA grB · already had it: grC · not found: grD``
    (empty groups omitted). A soft-deleted or unknown id is "not found";
    a refused comment is reported under "failed", never raised.
    """
    from precis.dispatch import Hub
    from precis.errors import PrecisError
    from precis.handlers.gripe import GripeHandler

    handler = GripeHandler(hub=Hub(store=store))
    groups: dict[str, list[str]] = {
        "commented": [],
        "already had it:": [],
        "not found:": [],
        "failed:": [],
    }
    for gid in ids:
        if store.get_ref(kind="gripe", id=gid) is None:
            groups["not found:"].append(f"gr{gid}")
            continue
        existing = {
            c.text.strip()
            for c in store.chunks.list_chunks_for_ref(gid)
            if c.chunk_kind == "gripe_comment"
        }
        if text.strip() in existing:
            groups["already had it:"].append(f"gr{gid}")
            continue
        try:
            handler.put(id=gid, text=text)
        except PrecisError as e:
            groups["failed:"].append(f"gr{gid} ({type(e).__name__})")
            continue
        groups["commented"].append(f"gr{gid}")
    return " · ".join(
        f"{label} {' '.join(items)}" for label, items in groups.items() if items
    )


#: The sha a verify gripe waits on: ``resolved: <sha>`` (scripts/ship's
#: land note, a builder's close) or ``fix-sha: <sha>`` (the triage worker).
_VERIFY_SHA_RE = re.compile(r"^\s*(?:resolved|fix-sha):\s*([0-9a-f]{7,40})\b")


def verify_sha(store: Any, gid: int) -> str | None:
    """The sha in the newest comment of ``gid`` that names one, else None."""
    comments = sorted(
        (
            c
            for c in store.chunks.list_chunks_for_ref(gid)
            if c.chunk_kind == "gripe_comment"
        ),
        key=lambda c: c.ord,
        reverse=True,
    )
    for c in comments:
        if m := _VERIFY_SHA_RE.match(c.text):
            return m.group(1)
    return None


def verify_list(store: Any) -> list[tuple[int, str | None]]:
    """Every live STATUS:verify gripe with the sha it waits on (gr541800)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id FROM refs r "
            "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "AND t.namespace = 'STATUS' AND t.value = 'verify' "
            "WHERE r.kind = 'gripe' AND r.retired_at IS NULL "
            "ORDER BY r.ref_id"
        ).fetchall()
    return [(int(r[0]), verify_sha(store, int(r[0]))) for r in rows]


def verify_done(store: Any, text: str, ids: list[int]) -> str:
    """Close each ``ids`` gripe still at STATUS:verify: comment ``text``
    (``{sha}`` → its own verify sha) then STATUS:done, through the handler
    so the ``resolved:`` guard and the close cascade both run.

    Returns ``done grA grB · not verify: grC · not found: grD · failed: …``
    (empty groups omitted). A gripe that moved off verify since the list
    was read is skipped, never re-closed.
    """
    from precis.dispatch import Hub
    from precis.errors import PrecisError
    from precis.handlers.gripe import GripeHandler

    handler = GripeHandler(hub=Hub(store=store))
    groups: dict[str, list[str]] = {
        "done": [],
        "not verify:": [],
        "not found:": [],
        "failed:": [],
    }
    for gid in ids:
        if store.get_ref(kind="gripe", id=gid) is None:
            groups["not found:"].append(f"gr{gid}")
            continue
        if "STATUS:verify" not in {str(t) for t in store.tags_for(gid)}:
            groups["not verify:"].append(f"gr{gid}")
            continue
        sha = verify_sha(store, gid) or "?"
        try:
            handler.put(id=gid, text=text.replace("{sha}", sha), tags=["STATUS:done"])
        except PrecisError as e:
            groups["failed:"].append(f"gr{gid} ({type(e).__name__})")
            continue
        groups["done"].append(f"gr{gid}")
    line = " · ".join(
        f"{label} {' '.join(items)}" for label, items in groups.items() if items
    )
    return line or "nothing to do"


def shorten_titles(
    store: Any,
    *,
    apply: bool,
    limit: int | None = None,
    ids: list[int] | None = None,
) -> list[str]:
    """The one-time title gardening pass (gripe system batch F slice 6).

    Lists every open gripe whose title has a shorter derived form
    (:func:`precis.handlers._gripe_title.short_title`), oldest first, as
    ``grN: "old" → "new"``. With ``apply`` it first comments
    ``title shortened from: <old>`` on the gripe, then stores the new
    title, so the old one stays readable on the timeline. The last line
    counts what it found and wrote. ``ids`` restricts the pass to those
    gripes (``--ids``).
    """
    from precis.dispatch import Hub
    from precis.errors import PrecisError
    from precis.handlers._gripe_title import short_title
    from precis.handlers.gripe import GripeHandler
    from precis.store.revision_context import revision_context

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, coalesce(r.title, '') FROM refs r "
            "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "AND t.namespace = 'STATUS' AND t.value = 'open' "
            "WHERE r.kind = 'gripe' AND r.retired_at IS NULL "
            "AND (%s::bigint[] IS NULL OR r.ref_id = ANY(%s::bigint[])) "
            "ORDER BY r.created_at, r.ref_id",
            (ids, ids),
        ).fetchall()
    handler = GripeHandler(hub=Hub(store=store))
    out: list[str] = []
    found = written = 0
    skipped: list[str] = []
    for ref_id, title in rows:
        new = short_title(title)
        if new is None or new == title:
            continue
        if limit is not None and found >= limit:
            break
        found += 1
        old = " ".join(title.split())
        out.append(f'gr{ref_id}: "{old[:100]}" → "{new}"')
        if not apply:
            continue
        # The title is derived mechanically by this pass, so it declares
        # itself ``computed`` (an operator-run gardening pass), not a human
        # or reviewer. Title first: a refused row is skipped before it
        # gets a "shortened from" comment it would not honour.
        try:
            with revision_context(
                "gripe title gardening pass",
                actor="cli:gripes --shorten-titles",
                trust_origin="computed",
                trust_actor_type="operator",
            ):
                store.update_ref(int(ref_id), title=new)
                handler.put(id=int(ref_id), text=f"title shortened from: {old}")
        except (PrecisError, psycopg.Error) as e:
            out.append(f"  failed: {type(e).__name__}")
            skipped.append(f"gr{ref_id} ({type(e).__name__}: {_first_line(e)})")
            continue
        written += 1
    verb = f"shortened {written}" if apply else "dry run, nothing written"
    out.append(f"{found} open gripe title(s) to shorten; {verb}")
    if skipped:
        out.append(f"skipped {len(skipped)}: " + " · ".join(skipped))
    return out


def _first_line(exc: BaseException) -> str:
    return (str(exc).strip().splitlines() or [""])[0][:160]


# ---------------------------------------------------------------------------
# Row formatters
# ---------------------------------------------------------------------------


def _row_to_json(row: tuple[Any, ...]) -> dict[str, Any]:
    ref_id, title, created_at, updated_at, retired_at = row
    out: dict[str, Any] = {
        "id": int(ref_id),
        "text": title,
        "created_at": _iso(created_at),
        "updated_at": _iso(updated_at),
    }
    if retired_at is not None:
        out["retired_at"] = _iso(retired_at)
    return out


def _print_text(row: tuple[Any, ...]) -> None:
    ref_id, title, created_at, _updated_at, retired_at = row
    marker = "  (deleted)" if retired_at is not None else ""
    print(f"## gripe {ref_id}  [{_iso(created_at)}]{marker}")
    print(title)
    print()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


__all__ = ["add_parser", "run"]
