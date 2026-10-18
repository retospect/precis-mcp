"""The sandbox read MCP's unix socket serves read verbs only.

A real ``precis.server`` streamable-http app runs in a child process on
a unix socket after ``_restrict_to_read_verbs`` (what ``precis serve
--read-only`` does before its runtime starts), and the in-container
client (``_sandbox_read_mcp.CLIENT_SOURCE``) talks to it over that
socket. Write verbs must be refused by the serving process itself; the
container side has no say. No DB is needed: a refused call never
reaches the runtime.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from precis.workers.executors import _sandbox_read_mcp as read_mcp

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="unix sockets are POSIX-only"
)

_TOKEN = "tok-socket-test"

_SERVE = """
import sys
from precis import server
server._restrict_to_read_verbs(server.mcp)
server._run_network_transport(
    transport="streamable-http", host="127.0.0.1", port=0, token=sys.argv[2],
    uds=sys.argv[1],
)
"""


def _load_client(cfg_path: Path) -> types.ModuleType:
    mod = types.ModuleType("precis_client_under_test")
    exec(compile(read_mcp.CLIENT_SOURCE, "precis_client.py", "exec"), mod.__dict__)
    mod.__dict__["_CFG_PATH"] = str(cfg_path)
    return mod


@pytest.fixture(scope="module")
def served() -> Iterator[dict[str, Any]]:
    # A short path: AF_UNIX paths cap at ~104 bytes on macOS.
    tmp = Path(tempfile.mkdtemp(prefix="rmcp-", dir="/tmp"))
    sock = tmp / read_mcp.SOCKET_NAME
    env = {k: v for k, v in os.environ.items() if k != "PRECIS_MCP_PROFILE"}
    proc = subprocess.Popen(
        [sys.executable, "-c", _SERVE, str(sock), _TOKEN],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 60
        while not sock.exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                err = (
                    proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
                )
                pytest.fail(f"read-only serve did not bind {sock}: {err[-2000:]}")
            time.sleep(0.1)
        cfg = tmp / "mcp.json"
        payload = read_mcp.mcp_json_payload(token=_TOKEN)
        payload["socket"] = str(sock)
        cfg.write_text(json.dumps(payload), encoding="utf-8")
        yield {"sock": sock, "cfg": cfg, "tmp": tmp}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - safety net
            proc.kill()
            proc.wait(timeout=5)
        shutil.rmtree(tmp, ignore_errors=True)


def test_socket_is_connectable_by_other_uids(served: dict[str, Any]) -> None:
    # The container's subordinate uid is not the serving user.
    assert served["sock"].stat().st_mode & 0o777 == 0o666


def test_tools_list_over_the_socket_is_the_read_set(served: dict[str, Any]) -> None:
    client = _load_client(served["cfg"])
    client._connect(30)
    _, msg = client._rpc("tools/list", {}, 30)
    names = {t["name"] for t in msg["result"]["tools"]}
    assert names == {"get", "search", "more"}


@pytest.mark.parametrize("verb", ["put", "edit", "delete", "tag", "link"])
def test_write_verb_over_the_socket_is_refused(
    served: dict[str, Any], verb: str
) -> None:
    client = _load_client(served["cfg"])
    with pytest.raises(RuntimeError, match=f"Unknown tool: {verb}"):
        client.call(verb, wait_s=30, kind="todo", id=1, text="x")


@pytest.mark.parametrize(
    ("kind", "args"),
    [
        ("web", {"id": "https://example.invalid/cache-miss"}),
        ("perplexity-reasoning", {"q": "anything"}),
        ("websearch", {"q": "anything"}),
    ],
)
def test_live_fetch_and_paid_kinds_over_the_socket_are_refused(
    served: dict[str, Any], kind: str, args: dict[str, str]
) -> None:
    client = _load_client(served["cfg"])
    with pytest.raises(RuntimeError, match=r"not served by the sandbox") as exc:
        client.call("get", wait_s=30, kind=kind, **args)
    assert "next:" in str(exc.value)


def test_search_without_a_kind_is_refused(served: dict[str, Any]) -> None:
    client = _load_client(served["cfg"])
    with pytest.raises(RuntimeError, match=r"next:"):
        client.call("search", wait_s=30, q="anything")


def test_allowlist_excludes_every_fetch_and_paid_kind() -> None:
    fetch_or_paid = {
        "web",
        "youtube",
        "wikipedia",
        "news",
        "datasheet",
        "paper",
        "patent",
        "edgar",
        "perplexity-reasoning",
        "perplexity-research",
        "websearch",
        "llm",
        "oracle",
        "semanticscholar",
        "email",
    }
    assert not fetch_or_paid & read_mcp.READ_KIND_ALLOWLIST
    assert read_mcp.kind_refusal("get", "skill") is None
    assert read_mcp.kind_refusal("more", None) is None


def test_wrong_token_is_401(served: dict[str, Any]) -> None:
    bad = served["tmp"] / "bad.json"
    payload = json.loads(served["cfg"].read_text(encoding="utf-8"))
    payload["token"] = "not-the-token"
    bad.write_text(json.dumps(payload), encoding="utf-8")
    client = _load_client(bad)
    with pytest.raises(RuntimeError, match="HTTP 401"):
        client.call("get", wait_s=30, kind="skill", id="toc")


def test_read_only_refuses_the_command_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    # The command profile's single tool dispatches every verb, so a
    # read-only serve must not start under it.
    from mcp.server.fastmcp import FastMCP

    from precis import server

    monkeypatch.setenv("PRECIS_MCP_PROFILE", "command")
    with pytest.raises(ValueError, match="typed profile"):
        server._restrict_to_read_verbs(FastMCP("t"))


def test_restrict_removes_every_non_read_tool() -> None:
    from mcp.server.fastmcp import FastMCP

    from precis import server

    app = FastMCP("t")
    for name in ("get", "search", "more", "put", "edit", "delete", "tag", "link", "x"):
        app.add_tool(lambda: None, name=name)
    server._restrict_to_read_verbs(app)
    assert {t.name for t in app._tool_manager.list_tools()} == {"get", "search", "more"}


def test_serve_cli_parses_uds_and_read_only() -> None:
    from precis.cli.main import _build_parser

    args = _build_parser(("serve",)).parse_args(
        ["serve", "--transport", "streamable-http", "--uds", "/x.sock", "--read-only"]
    )
    assert args.uds == "/x.sock"
    assert args.read_only is True
