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

import asyncio
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
    assert names == {"get", "search"}


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
    assert read_mcp.kind_refusal("get", "datasheet") is None
    assert read_mcp.kind_refusal("search", "datasheet") is None
    assert read_mcp.kind_refusal("more", None) is None


@pytest.mark.parametrize("verb", ["get", "search"])
def test_server_uses_the_shared_read_kind_allowlist(
    verb: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis import server

    async def read(**kw: Any) -> str:
        return "dispatched"

    gated = server._kind_gated(verb, read)
    for kind in read_mcp.READ_KIND_ALLOWLIST:
        assert asyncio.run(gated(kind=kind)) == "dispatched"
    # Changing the canonical policy must change the serving gate too;
    # a copied allowlist in server.py would fail both assertions.
    monkeypatch.setattr(read_mcp, "READ_KIND_ALLOWLIST", frozenset({"test-kind"}))
    assert asyncio.run(gated(kind="test-kind")) == "dispatched"
    assert asyncio.run(gated(kind="datasheet")).isError


@pytest.mark.parametrize("claim", ["pin_functions", "tie"])
@pytest.mark.parametrize(
    ("handle", "expected"),
    [
        ("ds:da1~7", []),
        ("ds:da1~999", ["source.unresolvable"]),
        ("ds:da2~7", ["source.wrong_part"]),
        ("ds:pa1~7", ["source.unresolvable"]),
        ("ds:da3~7", ["source.unresolvable"]),
    ],
    ids=["valid", "missing-chunk", "wrong-part", "wrong-kind", "converted-ref"],
)
def test_pcb_sources_through_sandbox_read_gate(
    claim: str,
    handle: str,
    expected: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Board → staged client → serving gate, with corpus reads faked."""
    from mcp.server.fastmcp import FastMCP
    from mcp.types import CallToolResult, TextContent

    from precis import server
    from precis.handlers.paper import _parse_paper_id
    from precis_geom import pcb

    def reply(text: str, *, error: bool = False) -> CallToolResult:
        return CallToolResult(
            content=[TextContent(type="text", text=text)], isError=error
        )

    # Stored corpus fixtures: no handler, DB, transport or live fetch.
    parts = {"da1": "C111", "da2": "C222"}
    reads: list[tuple[str, str]] = []

    async def get(kind: str, id: str) -> CallToolResult:
        assert kind == "datasheet"
        reads.append(("get", id))
        ref, chunk, view = _parse_paper_id(id, "datasheet")
        assert view is None
        # A retired datasheet handle can redirect to another kind: get
        # succeeds, but the kind-scoped status below must reject it.
        if (ref not in parts and ref != "da3") or chunk != (7, 7):
            return reply(f"[error:NotFound] no such datasheet chunk {id}", error=True)
        return reply("Pin 1: GPIO")

    async def search(kind: str, view: str, ids: list[str]) -> CallToolResult:
        assert (kind, view) == ("datasheet", "status")
        ref = ids[0]
        reads.append(("search", ref))
        if ref == "da3":
            return reply("datasheet status — 0 of 1 ids resolved:\n")
        return reply(
            "datasheet status — 1 of 1 ids resolved:\n\n"
            f"  {ref}  [vendor-sheet]  full (1 body chunks)  pdf\n"
            f"      part {parts[ref]}  pull: ok\n"
        )

    app = FastMCP("sandbox-pcb-source-test")
    app.add_tool(get, name="get")
    app.add_tool(search, name="search")
    for verb in ("put", "edit", "delete", "tag", "link"):
        app.add_tool(lambda: pytest.fail("write dispatched"), name=verb)
    monkeypatch.setenv("PRECIS_MCP_PROFILE", "typed")
    server._restrict_to_read_verbs(app)
    assert {t.name for t in app._tool_manager.list_tools()} == {"get", "search"}

    client = _load_client(tmp_path / "unused-mcp.json")

    def rpc(method: str, params: dict[str, Any], timeout: int) -> tuple:
        assert method == "tools/call"
        tool = app._tool_manager.get_tool(params["name"])
        assert tool is not None
        result = asyncio.run(tool.fn(**params["arguments"]))
        return "fake-session", {"result": result.model_dump()}

    monkeypatch.setattr(client, "_session", "fake-session")
    monkeypatch.setattr(client, "_rpc", rpc)
    monkeypatch.setitem(sys.modules, "precis_client", client)

    board = pcb.Board("sandbox-source")
    part = board.part("U1", lcsc="C111")
    net = board.net("N")
    if claim == "pin_functions":
        part.pin_functions({"1": ["GPIO"]}, source=handle)
        net.tie(part["1"])
    else:
        net.tie(part["1"], source=handle)
    result = board.check()
    assert [finding.code for finding in result.findings] == expected
    assert not result.not_checked
    assert all(not finding.not_checked for finding in result.findings)
    assert reads[0] == ("get", handle.removeprefix("ds:"))
    if expected != ["source.unresolvable"] or handle == "ds:da3~7":
        assert reads[1] == ("search", handle.removeprefix("ds:").split("~")[0])


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
    assert {t.name for t in app._tool_manager.list_tools()} == {"get", "search"}


def test_serve_cli_parses_uds_and_read_only() -> None:
    from precis.cli.main import _build_parser

    args = _build_parser(("serve",)).parse_args(
        ["serve", "--transport", "streamable-http", "--uds", "/x.sock", "--read-only"]
    )
    assert args.uds == "/x.sock"
    assert args.read_only is True
