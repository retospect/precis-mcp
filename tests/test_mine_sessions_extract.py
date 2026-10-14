"""Tests for ``scripts/mine-sessions/extract.py`` — the normalization stage
of the session-mining toolkit (``scripts/mine-sessions/README.md``). All
pure: synthetic transcript fixtures under ``tmp_path``, no network, no real
DB. ``scripts/`` is not a package under ``src/``, so ``extract``/``redact``
are imported via a ``sys.path`` insert — same pattern as
``tests/test_guide_scripts.py``'s ``guide_lib`` import.

Covers: the local-corpus block-walker (tool_use/tool_result join, the
command- vs typed-profile ``kind=`` parsing, both ``tool_result.content``
shapes, error outcomes, user-text vs tool-result-only user turns, redaction
on the write side, and unparseable-line bookkeeping) plus the row->Event
mapping for the ``tool_calls`` ledger corpus. The sidechain glob test is the
regression pin for the missed-sidechain bug the spec calls out (91% of the
real corpus is ``<session>/subagents/*.jsonl`` files an earlier audit's glob
never matched).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "mine-sessions"))

import extract


def _write_jsonl(path: Path, lines: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(d) for d in lines) + "\n", encoding="utf-8")


def _tool_use_event(
    session: str,
    tool_use_id: str,
    tool_name: str,
    tool_input: dict[str, object],
    *,
    ts: str = "2026-09-20T10:00:00.000Z",
    usage: dict[str, object] | None = None,
    sidechain: bool = False,
) -> dict[str, object]:
    return {
        "type": "assistant",
        "sessionId": session,
        "uuid": f"a-{tool_use_id}",
        "isSidechain": sidechain,
        "cwd": "/work",
        "gitBranch": "main",
        "timestamp": ts,
        "message": {
            "model": "claude-sonnet-5",
            "usage": usage if usage is not None else {"input_tokens": 5},
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": tool_name,
                    "input": tool_input,
                }
            ],
        },
    }


def _tool_result_event(
    session: str,
    tool_use_id: str,
    content: object,
    *,
    ts: str = "2026-09-20T10:00:01.000Z",
    is_error: bool = False,
    sidechain: bool = False,
) -> dict[str, object]:
    block: dict[str, object] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }
    if is_error:
        block["is_error"] = True
    return {
        "type": "user",
        "sessionId": session,
        "uuid": f"u-{tool_use_id}",
        "isSidechain": sidechain,
        "timestamp": ts,
        "message": {"content": [block]},
    }


# ---------------------------------------------------------------------------
# 1. main-session tool_use + later tool_result, joined with usage
# ---------------------------------------------------------------------------


def test_local_tool_use_joins_result_with_usage() -> None:
    session = "11111111-1111-1111-1111-111111111111"
    lines = [
        json.dumps(
            _tool_use_event(
                session,
                "toolu_1",
                "mcp__precis__search",
                {"kind": "skill", "q": "hello"},
                usage={"input_tokens": 5, "output_tokens": 9},
            )
        ),
        json.dumps(_tool_result_event(session, "toolu_1", "3 hits")),
    ]

    stats = extract.Stats()
    events = extract._extract_from_lines(
        lines,
        corpus="local",
        session_fallback=session,
        is_sidechain_default=False,
        stats=stats,
    )

    tool_events = [e for e in events if e.tool == "mcp__precis__search"]
    assert len(tool_events) == 1
    ev = tool_events[0]
    assert ev.verb == "search"
    assert ev.kind == "skill"
    assert ev.arg_keys == ("kind", "q")
    assert ev.result_head == "3 hits"
    assert ev.result_bytes == len("3 hits")
    assert ev.usage == {"input_tokens": 5, "output_tokens": 9}
    assert ev.model == "claude-sonnet-5"
    assert ev.is_error is False
    assert stats.parse_failures == 0

    # Also a thin per-turn event for the enclosing assistant turn.
    assert any(e.tool is None and e.usage for e in events)


# ---------------------------------------------------------------------------
# 2. sidechain glob + flag — regression pin for the missed-sidechain bug
# ---------------------------------------------------------------------------


def test_local_sidechain_glob_and_flag(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    session = "22222222-2222-2222-2222-222222222222"
    sub_dir = root / "-Users-reto-someproj" / session / "subagents"
    sub_dir.mkdir(parents=True)
    _write_jsonl(
        sub_dir / "agent-abc123.jsonl",
        [
            {
                "type": "user",
                "sessionId": session,
                "isSidechain": True,
                "timestamp": "2026-09-20T10:00:00.000Z",
                "message": {"content": "go do the thing"},
            }
        ],
    )
    # A main-session file at the OTHER shape, in the same fixture, so the
    # test proves both globs fire, not just one.
    _write_jsonl(
        root / "-Users-reto-someproj" / f"{session}.jsonl",
        [
            {
                "type": "user",
                "sessionId": session,
                "isSidechain": False,
                "timestamp": "2026-09-20T09:00:00.000Z",
                "message": {"content": "main session turn"},
            }
        ],
    )

    files = extract.iter_local_files(str(root))
    names = {p.name for p in files}
    assert "agent-abc123.jsonl" in names
    assert f"{session}.jsonl" in names

    sidechain_path = next(p for p in files if p.name == "agent-abc123.jsonl")
    assert sidechain_path.parent.name == "subagents"

    stats = extract.Stats()
    with sidechain_path.open(encoding="utf-8") as fh:
        events = extract._extract_from_lines(
            fh,
            corpus="local",
            session_fallback=sidechain_path.stem,
            is_sidechain_default=(sidechain_path.parent.name == "subagents"),
            stats=stats,
        )
    assert events
    assert all(e.sidechain for e in events)


# ---------------------------------------------------------------------------
# 3. command-profile kind= parsing
# ---------------------------------------------------------------------------


def test_command_profile_kind_parsing() -> None:
    session = "s3"
    lines = [
        json.dumps(
            _tool_use_event(
                session,
                "toolu_cmd",
                "mcp__precis__precis",
                {"command": "search(kind='skill', q='foo')"},
            )
        ),
        json.dumps(_tool_result_event(session, "toolu_cmd", "ok")),
    ]

    stats = extract.Stats()
    events = extract._extract_from_lines(
        lines,
        corpus="local",
        session_fallback=session,
        is_sidechain_default=False,
        stats=stats,
    )
    ev = next(e for e in events if e.tool == "mcp__precis__precis")
    assert ev.verb == "search"
    assert ev.kind == "skill"


# ---------------------------------------------------------------------------
# 4. tool_result.content as a list of blocks == a plain string
# ---------------------------------------------------------------------------


def test_tool_result_content_list_of_blocks_matches_plain_string() -> None:
    def _run(content: object) -> str:
        session = "s4"
        lines = [
            json.dumps(
                _tool_use_event(
                    session, "toolu_x", "mcp__precis__get", {"kind": "skill"}
                )
            ),
            json.dumps(_tool_result_event(session, "toolu_x", content)),
        ]
        stats = extract.Stats()
        events = extract._extract_from_lines(
            lines,
            corpus="local",
            session_fallback=session,
            is_sidechain_default=False,
            stats=stats,
        )
        ev = next(e for e in events if e.tool == "mcp__precis__get")
        return ev.result_head

    as_string = _run("toc body")
    as_blocks = _run([{"type": "text", "text": "toc body"}])
    assert as_string == as_blocks == "toc body"


# ---------------------------------------------------------------------------
# 5. is_error sets is_error/err_head
# ---------------------------------------------------------------------------


def test_is_error_sets_err_head() -> None:
    session = "s5"
    lines = [
        json.dumps(_tool_use_event(session, "toolu_err", "Bash", {"command": "false"})),
        json.dumps(
            _tool_result_event(
                session, "toolu_err", "boom: command failed", is_error=True
            )
        ),
    ]
    stats = extract.Stats()
    events = extract._extract_from_lines(
        lines,
        corpus="local",
        session_fallback=session,
        is_sidechain_default=False,
        stats=stats,
    )
    ev = next(e for e in events if e.tool == "Bash")
    assert ev.is_error is True
    assert "boom: command failed" in ev.err_head


# ---------------------------------------------------------------------------
# 6. user text turn -> __user__ event; tool_result-only user event does not
# ---------------------------------------------------------------------------


def test_user_text_emits_dunder_user_event_tool_result_only_does_not() -> None:
    session = "s6"
    lines = [
        json.dumps(
            {
                "type": "user",
                "sessionId": session,
                "isSidechain": False,
                "timestamp": "2026-09-20T10:00:00.000Z",
                "message": {"content": "no, that's wrong — I meant the other kind"},
            }
        ),
        json.dumps(
            _tool_result_event(session, "toolu_unmatched", "irrelevant tool result")
        ),
    ]
    stats = extract.Stats()
    events = extract._extract_from_lines(
        lines,
        corpus="local",
        session_fallback=session,
        is_sidechain_default=False,
        stats=stats,
    )
    user_events = [e for e in events if e.tool == "__user__"]
    assert len(user_events) == 1
    assert "no, that's wrong" in user_events[0].result_head
    # The tool_result-only event names no pending call and produces nothing.
    assert not any(e.tool not in ("__user__", None) for e in events)


# ---------------------------------------------------------------------------
# 7. redaction — write-side scrub of a tailnet address + a credentialed DSN
# ---------------------------------------------------------------------------


def test_redaction_scrubs_result_head() -> None:
    session = "s7"
    # Assembled from parts rather than written out: a literal CGNAT address
    # anywhere in the tree trips tests/test_deploy_tree_no_secrets.py, and the
    # scanner's exemption marker is budget-capped on purpose (it is meant to
    # stay vanishingly rare, and that budget is nearly spent). Parameterising
    # is what that test tells you to do instead — the scrub sees an identical
    # string at runtime either way.
    synthetic_tailnet_ip = "100." + "101." + "102.103"
    secret_text = (
        "connecting via postgresql://agent_rw"
        + f":hunter2@{synthetic_tailnet_ip}"
        + ":6432/precis_prod"
    )
    lines = [
        json.dumps(
            _tool_use_event(session, "toolu_sec", "Bash", {"command": "psql ..."})
        ),
        json.dumps(_tool_result_event(session, "toolu_sec", secret_text)),
    ]
    stats = extract.Stats()
    events = extract._extract_from_lines(
        lines,
        corpus="local",
        session_fallback=session,
        is_sidechain_default=False,
        stats=stats,
    )
    ev = next(e for e in events if e.tool == "Bash")
    assert extract.redact.scan(ev.result_head) == []
    assert synthetic_tailnet_ip not in ev.result_head
    assert "hunter2" not in ev.result_head


# ---------------------------------------------------------------------------
# 8. unparseable lines are skipped and counted, not raised
# ---------------------------------------------------------------------------


def test_unparseable_lines_skipped_and_counted() -> None:
    lines = [
        "not json at all {{{",
        json.dumps({"type": "mode", "sessionId": "s8", "mode": "normal"}),
        "",
    ]
    stats = extract.Stats()
    events = extract._extract_from_lines(
        lines,
        corpus="local",
        session_fallback="s8",
        is_sidechain_default=False,
        stats=stats,
    )
    assert events == []
    assert stats.parse_failures == 1


# ---------------------------------------------------------------------------
# 9. ledger row -> Event mapping, no DB
# ---------------------------------------------------------------------------


def test_ledger_row_to_event_mapping() -> None:
    row = {
        "ts": "2026-09-20T10:00:00+00:00",
        "source": "sonnet",
        "profile": "typed",
        "verb": "search",
        "kind": "todo",
        "input_keys": ["kind", "view"],
        "outcome": "error",
        "error_type": "BadInput",
        "latency_ms": 42,
    }
    ev = extract.ledger_row_to_event(row, seq=1)
    assert ev.corpus == "ledger"
    assert ev.seq == 1
    assert ev.verb == "search"
    assert ev.kind == "todo"
    assert ev.arg_keys == ("kind", "view")
    assert ev.is_error is True
    assert ev.err_type == "BadInput"
    assert ev.latency_ms == 42
    assert ev.source == "sonnet"
    assert ev.profile == "typed"
    # No payload fields — tool_calls carries none, by design.
    assert ev.tool is None
    assert ev.arg_digest == ""
    assert ev.result_head == ""
    assert ev.err_head == ""


# ---------------------------------------------------------------------------
# 10. prod hops: --ro enforced, jobs export is plain SQL not \copy
# ---------------------------------------------------------------------------


def test_run_prod_psql_passes_ro_flag(monkeypatch) -> None:
    """The fleet rule: prod reads go through ``prod-psql --ro`` (agent_ro
    inside BEGIN READ ONLY). A regression here silently re-opens write
    access for a script that only ever needs SELECT."""
    captured: dict[str, list[str]] = {}

    class _Proc:
        returncode = 0
        stdout = "row1"
        stderr = ""

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _Proc()

    monkeypatch.setattr(extract.subprocess, "run", fake_run)
    out = extract._run_prod_psql("SELECT 1")
    assert out == "row1"
    argv = captured["argv"]
    assert argv[1] == "--ro"


def test_jobs_sql_is_plain_sql_no_meta_command() -> None:
    """``prod-psql --ro`` wraps input in BEGIN READ ONLY … COMMIT — a psql
    ``\\copy`` cannot appear inside that and is forbidden by the fleet
    rule regardless. The export must be plain SQL whose rows re-parse:
    one ``to_jsonb`` object per line keeps the transcript's newlines
    inside a JSON string."""
    from datetime import UTC, datetime

    sql = extract._jobs_sql(datetime.now(UTC), 25)
    assert not sql.lstrip().startswith("\\")
    assert "\\copy" not in sql
    assert "to_jsonb" in sql
    assert "LIMIT 25" in sql


def test_fetch_jobs_rows_parses_json_lines(monkeypatch) -> None:
    """to_jsonb rows interleaved with the --ro wrapper's command tags:
    BEGIN/COMMIT lines are skipped, transcript newlines survive inside
    the JSON string."""
    stdout = "\n".join(
        [
            "BEGIN",
            json.dumps(
                {
                    "ref_id": "j123",
                    "transcript": '{"type":"assistant"}\n{"type":"user"}',
                }
            ),
            json.dumps({"ref_id": "j124", "transcript": ""}),
            "COMMIT",
        ]
    )
    monkeypatch.setattr(extract, "_run_prod_psql", lambda sql: stdout)
    rows = extract.fetch_jobs_rows(None, None)
    assert rows == [
        ("j123", '{"type":"assistant"}\n{"type":"user"}'),
        ("j124", ""),
    ]


def test_fetch_jobs_rows_none_on_hop_failure(monkeypatch) -> None:
    monkeypatch.setattr(extract, "_run_prod_psql", lambda sql: None)
    assert extract.fetch_jobs_rows(None, None) == []


# ---------------------------------------------------------------------------
# --devin: sessions.db chat-forest extraction
# ---------------------------------------------------------------------------


def _devin_db(tmp_path: Path) -> Path:
    """A minimal sessions.db: one session whose assistant turn carries a
    precis ``mcp_call_tool`` and an ``exec`` call, one failed tool result,
    and a user prompt — the three shapes the adapter must map."""
    import sqlite3

    db = tmp_path / "sessions.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, working_directory TEXT NOT NULL,
            backend_type TEXT, model TEXT, agent_mode TEXT,
            created_at INTEGER, last_activity_at INTEGER,
            title TEXT, main_chain_id INTEGER,
            shell_last_seen_index INTEGER DEFAULT 0,
            cogs_json TEXT, workspace_dirs TEXT,
            hidden INTEGER NOT NULL DEFAULT 0, metadata TEXT);
        CREATE TABLE message_nodes (
            row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL, node_id INTEGER NOT NULL,
            parent_node_id INTEGER, chat_message TEXT NOT NULL,
            created_at INTEGER NOT NULL, metadata TEXT);
        CREATE TABLE tool_call_state (
            session_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
            tool_call_json TEXT, tool_call_update_json TEXT,
            PRIMARY KEY (session_id, tool_call_id));
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, working_directory, backend_type, model,"
        " agent_mode, created_at, last_activity_at, hidden) VALUES"
        " ('s1', '/work', 'devin', 'm', 'normal', 1791967400, 1791967600, 0)"
    )
    msgs = [
        {
            "role": "user",
            "content": "check gripe 484070",
            "created": 1791967410,
        },
        {
            "role": "assistant",
            "content": "looking",
            "tool_calls": [
                {
                    "id": "mcp_call_tool:0#aa",
                    "name": "mcp_call_tool",
                    "arguments": {
                        "server_name": "precis",
                        "tool_name": "get",
                        "arguments": {"kind": "gripe", "id": 484070},
                    },
                    "index": 0,
                },
                {
                    "id": "exec:0#bb",
                    "name": "exec",
                    "arguments": {"command": "ls"},
                    "index": 1,
                },
            ],
            "created": 1791967420,
        },
        {
            "role": "tool",
            "tool_call_id": "mcp_call_tool:0#aa",
            "content": 'MCP tool \'get\' returned an error: [\n  {"type": "text",'
            ' "text": "[error:BadInput] unknown view \'meta\'"}]',
            "created": 1791967430,
        },
        {
            "role": "tool",
            "tool_call_id": "exec:0#bb",
            "content": "file1\nfile2",
            "created": 1791967440,
        },
    ]
    for i, m in enumerate(msgs):
        conn.execute(
            "INSERT INTO message_nodes (session_id, node_id, chat_message,"
            " created_at) VALUES ('s1', ?, ?, ?)",
            (
                i,
                json.dumps({k: v for k, v in m.items() if k != "created"}),
                m["created"],
            ),
        )
    conn.execute(
        "INSERT INTO tool_call_state (session_id, tool_call_id,"
        " tool_call_update_json) VALUES ('s1', 'mcp_call_tool:0#aa', ?)",
        (json.dumps({"status": "failed"}),),
    )
    conn.execute(
        "INSERT INTO tool_call_state (session_id, tool_call_id,"
        " tool_call_update_json) VALUES ('s1', 'exec:0#bb', ?)",
        (json.dumps({"status": "completed"}),),
    )
    conn.commit()
    conn.close()
    return db


def test_devin_source_maps_precis_and_shell_calls(tmp_path) -> None:
    import devin_source

    events, coverage = devin_source.extract(_devin_db(tmp_path))
    assert not coverage["malformed_rows"]
    precis = [e for e in events if e.verb == "get"]
    assert len(precis) == 1
    call = precis[0]
    assert call.corpus == "devin"
    assert call.session == "devin:s1"
    assert call.tool == "mcp_call_tool"
    assert call.kind == "gripe"
    assert call.arg_keys == ("id", "kind")
    assert '"id"' in (call.arg_digest or "")
    # The tool result joined by tool_call_id; 'failed' status + the
    # [error:Type] marker supply the outcome the message itself lacks.
    assert call.is_error
    assert call.err_type == "BadInput"
    assert "BadInput" in (call.err_head or "")
    shell = [e for e in events if e.tool == "exec"]
    assert len(shell) == 1 and not shell[0].is_error
    assert shell[0].verb is None
    users = [e for e in events if e.tool == "__user__"]
    assert len(users) == 1 and "484070" in (users[0].result_head or "")
    # seq is the per-session ordinal in chat order.
    assert [e.seq for e in events] == list(range(1, len(events) + 1))


def test_devin_source_missing_db_reports_and_returns_empty(tmp_path) -> None:
    import devin_source

    events, coverage = devin_source.extract(tmp_path / "absent.db")
    assert events == []
    assert coverage["db_missing"] == 1
