"""Persist source observations in the gripe ledger's short-handle namespace."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from precis.errors import BadInput
from precis.handlers._comment_timeline import HIDDEN_META_KEY
from precis.handlers._python_impact import _Git
from precis.python_index import ModuleIndex, RepoIndex, Symbol
from precis.store import Store

HANDLE = re.compile(r"an[0-9]+", re.IGNORECASE)
VIEWS = ("bare", "context", "meta", "fisheye")
MINT_NEXT = "put(kind='python', mode='anchor', id='<repo>/<file.py>~L12-L13')"

_LIVE = """
    kind = 'alias' AND retired_at IS NULL
    AND (meta->'alias'->>'expires_at' IS NULL
         OR (meta->'alias'->>'expires_at')::timestamptz > now())
"""
_PAYLOAD = "meta->'code_anchor'"


@dataclass(frozen=True)
class Observation:
    repo: str
    module: str
    qualname: str
    file: str
    start: int
    snippet: list[str]
    sha: str
    note: str | None
    source_digest: str


def matching_lines(mod: ModuleIndex, sym: Symbol, snippet: list[str]) -> list[int]:
    lines = (mod.source or "").splitlines()
    return [
        line
        for line in range(sym.start_line, sym.end_line - len(snippet) + 2)
        if lines[line - 1 : line - 1 + len(snippet)] == snippet
    ]


def matched_line(
    mod: ModuleIndex, sym: Symbol, observation: Observation
) -> tuple[int | None, str]:
    matches = matching_lines(mod, sym, observation.snippet)
    if (
        mod.file == observation.file
        and mod.sha256 == observation.source_digest
        and observation.start in matches
    ):
        return observation.start, ""
    if len(matches) == 1:
        return matches[0], ""
    return (
        None,
        "snippet is ambiguous in symbol" if matches else "snippet changed in symbol",
    )


def read(store: Store, handle: str) -> tuple[Observation | None, str] | None:
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT {_PAYLOAD}, CASE WHEN lower(meta->'alias'->>'a') = %s
                   THEN meta->'alias'->>'b' ELSE meta->'alias'->>'a' END
            FROM refs WHERE {_LIVE}
              AND %s IN (lower(meta->'alias'->>'a'), lower(meta->'alias'->>'b'))
            ORDER BY ref_id LIMIT 2
            """,
            (handle, handle),
        ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        return None, "ambiguous handle in the alias ledger"
    payload, location = rows[0]
    return (Observation(**payload) if payload else None), str(location)


def listed(
    store: Store, repo: str, *, file: str | None = None, qualname: str | None = None
) -> dict[str, list[str]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT {_PAYLOAD}->>'qualname', meta->'alias'->>'a'
            FROM refs WHERE {_LIVE} AND {_PAYLOAD}->>'repo' = %s
              AND ({_PAYLOAD}->>'file' = %s OR {_PAYLOAD}->>'qualname' = %s)
            ORDER BY ref_id
            """,
            (repo, file, qualname),
        ).fetchall()
    out: dict[str, list[str]] = {}
    for symbol, handle in rows:
        out.setdefault(symbol, []).append(handle)
    return out


def mint(
    store: Store, observation: Observation, mod: ModuleIndex, sym: Symbol
) -> tuple[str, int, bool]:
    # Imported here because the ledger owns the shared allocation lock.
    from precis.handlers._gripe_summary import ANCHOR_MINT_LOCK_NAMESPACE

    with store.tx() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)", (ANCHOR_MINT_LOCK_NAMESPACE, 0)
        )
        rows = conn.execute(
            f"""
            SELECT ref_id, meta->'alias'->>'a', {_PAYLOAD}
            FROM refs WHERE {_LIVE}
              AND {_PAYLOAD}->>'repo' = %s AND {_PAYLOAD}->>'qualname' = %s
            ORDER BY ref_id FOR UPDATE
            """,
            (observation.repo, observation.qualname),
        ).fetchall()
        location = f"{observation.file}::{observation.qualname}"
        for ref_id, handle, payload in rows:
            old = Observation(**payload)
            start, _ = matched_line(mod, sym, old)
            if start is None:
                continue
            if not (
                start < observation.start + len(observation.snippet)
                and observation.start < start + len(old.snippet)
            ):
                continue
            refreshed = asdict(observation)
            # Keep the finding's matched span when a later selection only
            # overlaps an edge. Repeated refreshes must not walk the anchor.
            refreshed.update(start=start, snippet=old.snippet)
            if observation.note is None:
                refreshed["note"] = old.note
            conn.execute(
                """
                UPDATE refs SET meta = jsonb_set(
                    jsonb_set(meta, '{code_anchor}', %s::jsonb),
                    '{alias,b}', to_jsonb(%s::text)), title = %s, updated_at = now()
                WHERE ref_id = %s
                """,
                (
                    json.dumps(refreshed),
                    location,
                    f"{handle} ⇄ {location}"[:200],
                    ref_id,
                ),
            )
            return str(handle), start, False

        # Both old spellings and both sides count, including retired rows.
        row = conn.execute(
            """
            SELECT coalesce(max(substring(side FROM 3)::numeric), 0)
            FROM refs CROSS JOIN LATERAL
                (VALUES (meta->'alias'->>'a'), (meta->'alias'->>'b')) AS sides(side)
            WHERE kind = 'alias' AND side ~* '^(an|co)[0-9]+$'
            """
        ).fetchone()
        assert row is not None
        handle = f"an{int(row[0]) + 1}"
        store.insert_ref(
            kind="alias",
            slug=None,
            title=f"{handle} ⇄ {location}"[:200],
            meta={
                "alias": {"a": handle, "b": location, "rel": "short-for"},
                "code_anchor": asdict(observation),
            },
            conn=conn,
        )
        return handle, observation.start, True


def comments(store: Store, handle: str) -> str:
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, c.ord
            FROM chunks c JOIN refs r USING (ref_id)
            WHERE r.kind = 'gripe' AND r.retired_at IS NULL
              AND c.chunk_kind = 'gripe_comment' AND c.text ~* %s
              AND NOT (coalesce(r.meta->%s::text, '[]'::jsonb) @> jsonb_build_array(c.ord))
            ORDER BY r.ref_id, c.ord
            LIMIT 21
            """,
            (rf"\m{handle}\M", HIDDEN_META_KEY),
        ).fetchall()
    labels = [f"gr{ref_id} comment {ord_}" for ref_id, ord_ in rows[:20]]
    if len(rows) > 20:
        labels.append("…")
    return ", ".join(labels) or "none"


def review_status(git: _Git, observation: Observation) -> str:
    try:
        git.resolve_rev(observation.sha)
        path = f":(literal){observation.file}"
        changed = (
            git.run(
                "diff",
                "--name-only",
                "--no-ext-diff",
                "--no-textconv",
                observation.sha,
                "HEAD",
                "--",
                path,
            )
            or git.run(
                "diff",
                "--name-only",
                "--no-ext-diff",
                "--no-textconv",
                observation.sha,
                "--",
                path,
            )
            or git.run(
                "ls-files",
                "--others",
                "--exclude-standard",
                "--",
                path,
            )
        )
    except BadInput:
        return "re-verify: recorded file history unavailable"
    if changed:
        return "re-verify: recorded file differs from observed commit"
    return ""


def relocate(
    root: Path, snippet: list[str], *, index: RepoIndex | None = None
) -> list[tuple[str, int]] | None:
    """Find exact whole-line matches, without choosing between candidates."""
    try:
        proc = subprocess.run(
            [
                "rg",
                "--no-config",
                "--json",
                "--multiline",
                "--fixed-strings",
                "--line-regexp",
                "--crlf",
                "--max-count",
                "21",
                "--regexp",
                "\n".join(snippet),
                ".",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        if index is None:
            return None
        # Some Python serving images do not carry rg. Use the already
        # built index in those images rather than disabling relocation.
        matches: list[tuple[str, int]] = []
        for mod in sorted(index.modules.values(), key=lambda mod: mod.file):
            for line in matching_lines(mod, mod.module_symbol, snippet):
                matches.append((mod.file, line))
                if len(matches) == 21:
                    return matches
        return matches
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):
        return None
    found: list[tuple[str, int]] = []
    for row in proc.stdout.splitlines():
        event = json.loads(row)
        if event["type"] == "match":
            data = event["data"]
            path = data["path"].get("text")
            lines = data["lines"].get("text")
            if path is None or lines is None:
                continue
            raw = lines.encode("utf-8")
            for match in data["submatches"]:
                offset = raw.count(b"\n", 0, match["start"])
                found.append((path.removeprefix("./"), data["line_number"] + offset))
    return sorted(found)[:21]


def listing_lines(anchors: dict[str, list[str]]) -> str:
    return "".join(
        f"\n{symbol} — {', '.join(handles)}" for symbol, handles in anchors.items()
    )
