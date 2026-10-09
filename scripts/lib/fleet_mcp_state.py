"""Which Claude Code sessions have lost the precis MCP: one line per tmux pane.

`scripts/fleet status` calls this for its `mcp` column. A stranded session is
silent: Claude Code (v2.1.285) gives up on a dropped HTTP MCP after 5 reconnect
attempts (~15 s), or on one that refused at startup after ~7 s, and never
retries; its precis tools simply vanish. On 2026-10-02 nobody noticed the
orchestrator's for nine hours (gr462596).

Joins two files Claude Code writes per session:
- ``~/.claude/sessions/<pid>.json`` — ``sessionId`` and the ``tmux`` pane;
- the client MCP logs, ``<cache>/claude-cli-nodejs/*/mcp-logs-<server>/*.jsonl``,
  one JSON line per event, each carrying its ``sessionId``.

A session's state is its last decisive log line: ``ok`` after a successful
connect or tool call, ``DOWN`` after a give-up, an expired HTTP session
("Session not found") or a stdio child that went away ("Connection failed …
Connection closed"), ``-`` with no event yet.

    python3 scripts/lib/fleet_mcp_state.py [--server precis]
    → %3 ok
      %7 DOWN

    python3 scripts/lib/fleet_mcp_state.py --servers precis
    → %3 precis=ok
      %7 precis=DOWN

`scripts/fleet mcp-check` and `watch` use the second form (``--servers``
takes a comma-separated list; since claude-context was retired on
2026-10-08 the fleet asks for precis alone).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# A dropped connection exhausting its retries, or a startup connect on its
# last retry (a later success line flips it back).
DOWN_MARKERS = ("giving up", "retry 3/3", "Session not found")
OK_MARKERS = (
    "Connection established",
    "Successfully connected",
    "completed successfully",
    "reconnection successful",
    "Reconnected",
)


def is_down(text: str) -> bool:
    if any(m in text for m in DOWN_MARKERS):
        return True
    # A stdio server whose process is gone: the client logs
    # "Connection failed …: MCP error -32000: Connection closed".
    return "Connection failed" in text and "Connection closed" in text


def sessions_dir() -> Path:
    return Path(
        os.environ.get(
            "PRECIS_FLEET_SESSIONS_DIR", Path.home() / ".claude" / "sessions"
        )
    )


def cache_dir() -> Path:
    override = os.environ.get("PRECIS_FLEET_CLIENT_CACHE")
    if override:
        return Path(override)
    mac = Path.home() / "Library" / "Caches" / "claude-cli-nodejs"
    return mac if mac.is_dir() else Path.home() / ".cache" / "claude-cli-nodejs"


def panes(directory: Path) -> dict[str, tuple[str, float]]:
    """sessionId -> (tmux pane id like ``%3``, start time in epoch seconds)."""
    out: dict[str, tuple[str, float]] = {}
    for f in directory.glob("*.json"):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sid, tmux = rec.get("sessionId"), rec.get("tmux") or ""
        if sid and "%" in tmux:
            out[sid] = ("%" + tmux.split("%", 1)[1], rec.get("startedAt", 0) / 1000)
    return out


def states(
    cache: Path, server: str, wanted: dict[str, tuple[str, float]]
) -> dict[str, str]:
    """sessionId -> ``ok`` / ``DOWN`` from the newest decisive line in its logs."""
    oldest = min((start for _, start in wanted.values()), default=0)
    latest: dict[str, tuple[str, str]] = {}  # sid -> (timestamp, state)
    for f in cache.glob(f"*/mcp-logs-{server}/*.jsonl"):
        try:
            if f.stat().st_mtime < oldest:
                continue
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            sid = ev.get("sessionId")
            if sid not in wanted:
                continue
            text = ev.get("debug") or ev.get("error") or ""
            if is_down(text):
                state = "DOWN"
            elif any(m in text for m in OK_MARKERS):
                state = "ok"
            else:
                continue
            ts = ev.get("timestamp", "")
            if ts >= latest.get(sid, ("", ""))[0]:
                latest[sid] = (ts, state)
    return {sid: state for sid, (_, state) in latest.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--server", default="precis")
    parser.add_argument(
        "--servers", help="comma-separated; one `<pane> <server>=<state> …` line each"
    )
    args = parser.parse_args(argv)
    wanted = panes(sessions_dir())
    ordered = sorted(wanted.items(), key=lambda kv: kv[1][0])
    if args.servers:
        names = [n for n in args.servers.split(",") if n]
        per = {n: states(cache_dir(), n, wanted) for n in names}
        for sid, (pane, _) in ordered:
            cols = " ".join(f"{n}={per[n].get(sid, '-')}" for n in names)
            print(pane, cols)
        return 0
    found = states(cache_dir(), args.server, wanted)
    for sid, (pane, _) in ordered:
        print(pane, found.get(sid, "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
