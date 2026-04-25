"""GripeHandler — agent-feedback log (Phase 6d).

Filesystem-backed, append-only.  Captures bug reports, "this didn't
work as advertised" notes, and "you should add X" wishes from agents
that hit unexpected behaviour.  No database, no API, no cost.

Why a dedicated kind?  Several error envelopes across the server tell
the agent *"if this looks like a bug, gripe about it: put(type='gripe',
text='…')"*.  Until this handler landed, that hint was a credibility
bug — following it produced ``ERROR [kind_unknown]: unknown scheme
'gripe'`` and the agent learned the docs were wrong.  Now the same
``put(type='gripe', text='…')`` writes a timestamped entry to
``~/.precis/gripes.md`` and returns an acknowledgement.

Storage format (markdown, append-only)::

    ## 2026-04-25T07:55:12Z  [tag1, tag2]
    paper:wang2020 has no body — re-ingest needed

    ## 2026-04-25T08:01:33Z
    /toc returns ERROR [unavailable] for stub refs.  Skill points at
    /abstract but that also returns empty.

The file is plain markdown so a human can read it directly; structured
metadata (timestamp, tags) lives in the heading line.

Dispatch::

    put(type='gripe', text='…')              — append a gripe
    put(type='gripe', text='…', tags=[…])    — with tags
    get(type='gripe', id='/recent')          — last 20 entries
    get(type='gripe', id='/')                — same (root)
    get(type='gripe', id='/all')             — every entry

Bare ``get(id='gripe:')`` lands the same as ``/recent``.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from precis.protocol import ErrorCode, Handler, PrecisError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


_DEFAULT_PATH = Path.home() / ".precis" / "gripes.md"
_HEADER_RE = re.compile(
    r"^##\s+(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)"
    r"(?:\s+\[(?P<tags>[^\]]*)\])?\s*$"
)


def _gripe_path() -> Path:
    """Resolve the gripe log path.

    Honours ``PRECIS_GRIPE_PATH`` so tests / Ansible can redirect to a
    known location without touching the agent's home directory.  Falls
    back to ``~/.precis/gripes.md``; the parent dir is created on first
    write.
    """
    env = os.environ.get("PRECIS_GRIPE_PATH", "")
    return Path(env).expanduser() if env else _DEFAULT_PATH


def _append_gripe(text: str, tags: list[str] | None = None) -> str:
    """Append a single gripe entry.  Returns the ISO timestamp used."""
    path = _gripe_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    tag_segment = f"  [{', '.join(tags)}]" if tags else ""
    block = f"## {ts}{tag_segment}\n{text.rstrip()}\n\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(block)
    return ts


def _read_entries(path: Path) -> list[dict[str, Any]]:
    """Parse the gripe log into a list of {ts, tags, text} dicts.

    Order: oldest first (reading order).  Caller reverses for
    /recent.  Tolerates a missing file (returns []) and stray content
    that doesn't match the header pattern (the orphan text is folded
    into a synthetic ``[malformed]`` entry so it isn't lost silently).
    """
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _HEADER_RE.match(line)
        if m:
            if current is not None:
                current["text"] = current["text"].rstrip()
                entries.append(current)
            tags_raw = m.group("tags") or ""
            tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
            current = {"ts": m.group("ts"), "tags": tags, "text": ""}
        elif current is not None:
            current["text"] += line + "\n"
        # Lines before the first header are ignored — likely a manual
        # editor preamble.
    if current is not None:
        current["text"] = current["text"].rstrip()
        entries.append(current)
    return entries


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class GripeHandler(Handler):
    """Handler for the ``gripe:`` scheme — agent-feedback log.

    Agent usage::

        put(type='gripe', text='wang2020 has no body — re-ingest needed')
        put(type='gripe', text='…', tags=['ingestion', 'urgent'])
        get(type='gripe', id='/recent')
    """

    scheme = "gripe"
    writable = True
    views: ClassVar[set[str] | dict[str, str]] = {"recent", "all"}

    # ---- Read ---------------------------------------------------------

    def read(
        self,
        path: str = "",
        selector: str | None = None,
        view: str | None = None,
        subview: str | None = None,
        query: str = "",
        summarize: bool = False,
        depth: int = 0,
        page: int = 1,
    ) -> str:
        """Render gripe entries.

        Three landing variants — all behave the same for /recent /
        empty path / root view.  ``/all`` returns every entry oldest-
        first; the others return the last 20 newest-first.
        """
        del selector, subview, query, summarize, depth, page  # unused
        gp = _gripe_path()
        entries = _read_entries(gp)
        if not entries:
            return (
                f"📣 gripe log is empty ({gp})\n\n"
                "Next:\n"
                "  put(type='gripe', text='…')        — log a gripe\n"
                "  put(type='gripe', text='…', tags=['ingestion'])"
            )

        all_view = view == "all" or path == "/all"
        if all_view:
            ordered = entries
            header = f"📣 gripe log — {len(ordered)} entries  ({gp})"
        else:
            ordered = list(reversed(entries))[:20]
            header = (
                f"📣 gripe log — last {len(ordered)} of "
                f"{len(entries)} entries  ({gp})"
            )

        lines = [header, ""]
        for e in ordered:
            tag_seg = f"  [{', '.join(e['tags'])}]" if e["tags"] else ""
            lines.append(f"## {e['ts']}{tag_seg}")
            lines.append(e["text"])
            lines.append("")
        if not all_view and len(entries) > 20:
            lines.append(
                f"({len(entries) - 20} older entries — get(id='gripe:/all'))"
            )
        return "\n".join(lines)

    # ---- Write --------------------------------------------------------

    def put(
        self,
        path: str = "",
        text: str = "",
        mode: str = "append",
        **kwargs: Any,
    ) -> str:
        """Append a gripe.  Default mode is ``append``; ``replace`` /
        ``delete`` aren't supported (gripes are append-only — the log
        is auditable history, not editable state).
        """
        del path  # gripes are append-only with no per-entry id yet
        # Accept any non-destructive mode and treat it as append.  The
        # server's ``put`` defaults to ``mode='replace'`` so a bare
        # ``put(type='gripe', text='…')`` arrives with replace; for an
        # append-only log the distinction is meaningless (every write
        # is a new entry), so collapse them.  Only modes that imply
        # state mutation we don't support get rejected.
        if mode in {"delete", "move"}:
            raise PrecisError(
                ErrorCode.MODE_UNSUPPORTED,
                cause=(
                    f"gripe: mode={mode!r} not supported — log is "
                    "append-only (auditable history)"
                ),
                next="put(type='gripe', text='…')  — append a new entry",
            )
        if not text or not text.strip():
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="gripe: text= required and must be non-empty",
                next=(
                    "put(type='gripe', text='<what you expected vs "
                    "what happened>')"
                ),
            )
        tags = kwargs.get("tags") or []
        if tags and not isinstance(tags, list):
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"gripe: tags must be a list of strings, got {type(tags).__name__}",
            )
        ts = _append_gripe(text, tags=tags or None)
        gp = _gripe_path()
        return (
            f"📣 logged at {ts} ({gp})\n"
            f"{text.rstrip()}\n"
            "\n"
            "Next:\n"
            "  get(type='gripe', id='/recent')  — review the log"
        )
