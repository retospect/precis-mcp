"""Live LLM tool-call tests.

Sends prompts to local ollama qwen3.5:9b with the real precis MCP tool
schemas and executes the returned calls through the actual server functions,
with tools.read/tools.put mocked out after the URI parser.

Requires: ollama running locally with qwen3.5:9b.
Run:  uv run pytest tests/test_llm_live.py -v
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch, MagicMock

import httpx
import pytest

from precis.server import mcp as precis_mcp, get, put, search, move
from precis.uri import parse as uri_parse

OLLAMA_URL = "http://localhost:11434"
MODEL = "qwen3.5:9b"


# ── Extract real tool schemas from FastMCP ─────────────────────────

def _get_mcp_tool_schemas() -> list[dict]:
    """Get ollama-format tool schemas from the live MCP server object."""
    tools_list = precis_mcp._tool_manager.list_tools()
    ollama_tools = []
    for t in tools_list:
        ollama_tools.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.parameters or {"type": "object"},
            },
        })
    return ollama_tools


TOOLS = _get_mcp_tool_schemas()


# ── Helpers ────────────────────────────────────────────────────────

def _ollama_call(prompt: str) -> list[dict]:
    """Send prompt to ollama, return raw tool_calls."""
    resp = httpx.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "tools": TOOLS,
            "stream": False,
            "think": False,
            "options": {"num_predict": 300, "temperature": 0},
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    msg = resp.json().get("message", {})
    return msg.get("tool_calls", [])


class CallCapture:
    """Mock for tools.read / tools.put that captures the URI and returns a stub."""

    def __init__(self):
        self.calls: list[dict] = []

    def read(self, *, uri: str, query: str = "", page: int = 1,
             top_k: int = 5, depth: int = 0) -> str:
        parsed = uri_parse(uri)
        self.calls.append({
            "fn": "read", "uri": uri, "parsed": parsed,
            "query": query, "depth": depth,
        })
        return f"[mock read: {uri}]"

    def put(self, *, uri: str, text: str = "", mode: str = "replace",
            tracked: bool = True, note: str = "", link: str = "") -> str:
        parsed = uri_parse(uri)
        self.calls.append({
            "fn": "put", "uri": uri, "parsed": parsed,
            "text": text, "mode": mode, "note": note, "link": link,
        })
        return f"[mock put: {uri}]"


def run_llm_call(prompt: str) -> tuple[list[dict], CallCapture]:
    """Send prompt to LLM, execute returned tool calls through server, return captures.

    Returns (raw_tool_calls, capture) where capture has all intercepted URIs.
    """
    raw_calls = _ollama_call(prompt)
    cap = CallCapture()

    with patch("precis.server.tools") as mock_tools:
        mock_tools.read = cap.read
        mock_tools.put = cap.put

        for tc in raw_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", {})

            if name == "get":
                get(**args)
            elif name == "put":
                put(**args)
            elif name == "search":
                search(**args)
            elif name == "move":
                move(**args)

    return raw_calls, cap


# ── Fixtures ───────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def check_ollama():
    """Skip all if ollama not running or model not available."""
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3.0)
        models = {m["name"] for m in r.json().get("models", [])}
        if MODEL not in models:
            pytest.skip(f"{MODEL} not available")
    except Exception:
        pytest.skip("ollama not running")


# ── Test scenarios ─────────────────────────────────────────────────

class TestGetPaper:
    def test_read_paper_overview(self):
        raw, cap = run_llm_call("Show me the paper wang2020state")
        assert len(raw) >= 1
        assert raw[0]["function"]["name"] == "get"
        assert cap.calls, "no URI captured"
        assert cap.calls[0]["parsed"].scheme == "paper"
        assert "wang2020state" in cap.calls[0]["parsed"].path

    def test_read_paper_chunk(self):
        raw, cap = run_llm_call("Read chunk 38 of wang2020state")
        assert raw[0]["function"]["name"] == "get"
        p = cap.calls[0]["parsed"]
        assert p.path == "wang2020state"
        assert p.selector == "38"
        assert p.range_start == 38

    def test_read_chunk_range(self):
        raw, cap = run_llm_call("Read chunks 10 through 15 of wang2020state")
        assert raw[0]["function"]["name"] == "get"
        p = cap.calls[0]["parsed"]
        assert p.range_start is not None
        assert p.range_end is not None
        assert p.range_end > p.range_start

    def test_read_toc(self):
        raw, cap = run_llm_call("Show the table of contents for wang2020state")
        assert raw[0]["function"]["name"] == "get"
        p = cap.calls[0]["parsed"]
        assert p.view == "toc"

    def test_read_abstract(self):
        raw, cap = run_llm_call("Show me the abstract of wang2020state")
        assert raw[0]["function"]["name"] == "get"
        p = cap.calls[0]["parsed"]
        assert p.view == "abstract"

    def test_cite_bibtex(self):
        raw, cap = run_llm_call("Get the BibTeX citation for wang2020state")
        assert raw[0]["function"]["name"] == "get"
        p = cap.calls[0]["parsed"]
        assert p.view == "cite"
        assert p.subview == "bib"


class TestGetDocument:
    def test_read_docx_toc(self):
        raw, cap = run_llm_call("Show the table of contents of report.docx")
        assert raw[0]["function"]["name"] == "get"
        p = cap.calls[0]["parsed"]
        assert p.scheme == "file"
        assert "report.docx" in p.path

    def test_read_docx_node(self):
        raw, cap = run_llm_call("Read node PLXDX in report.docx")
        assert raw[0]["function"]["name"] == "get"
        p = cap.calls[0]["parsed"]
        assert p.scheme == "file"
        assert p.selector == "PLXDX"


class TestSearch:
    def test_search_papers(self):
        raw, cap = run_llm_call("Search for papers about CO2 capture in metal-organic frameworks")
        assert raw[0]["function"]["name"] == "search"
        args = raw[0]["function"]["arguments"]
        assert "query" in args
        assert len(args["query"]) > 3

    def test_search_within_paper(self):
        raw, cap = run_llm_call("Search for 'selectivity' within wang2020state")
        assert raw[0]["function"]["name"] == "search"
        args = raw[0]["function"]["arguments"]
        assert "scope" in args


class TestPut:
    def test_append_to_docx(self):
        raw, cap = run_llm_call(
            "Append a new section called Methods to report.docx"
        )
        assert raw[0]["function"]["name"] == "put"
        p = cap.calls[0]["parsed"]
        assert p.scheme == "file"
        assert cap.calls[0]["mode"] == "append"

    def test_replace_node(self):
        raw, cap = run_llm_call(
            "Replace the content of node PLXDX in report.docx with 'Revised paragraph.'"
        )
        assert raw[0]["function"]["name"] == "put"
        p = cap.calls[0]["parsed"]
        assert p.scheme == "file"
        assert p.selector == "PLXDX"

    def test_add_note_to_paper(self):
        raw, cap = run_llm_call(
            "Add a note 'Important finding about selectivity' to wang2020state"
        )
        assert raw[0]["function"]["name"] == "put"
        c = cap.calls[0]
        # Could use note= arg or mode='note' — both valid
        assert c.get("note") or c.get("mode") == "note"


class TestSeparatorSyntax:
    """The LLM must use ~ for selectors, not # or anything else."""

    def test_tilde_not_hash_in_chunk(self):
        raw, _ = run_llm_call("Read chunk 38 of wang2020state")
        id_arg = raw[0]["function"]["arguments"].get("id", "")
        assert "#" not in id_arg, f"LLM used # instead of ~: {id_arg!r}"
        assert "~" in id_arg or id_arg == "wang2020state", \
            f"Expected ~ in chunk ref: {id_arg!r}"

    def test_tilde_not_hash_in_node(self):
        raw, _ = run_llm_call("Read node PLXDX in report.docx")
        id_arg = raw[0]["function"]["arguments"].get("id", "")
        assert "#" not in id_arg, f"LLM used # instead of ~: {id_arg!r}"

    def test_slash_for_views(self):
        raw, _ = run_llm_call("Show the table of contents for wang2020state")
        id_arg = raw[0]["function"]["arguments"].get("id", "")
        assert "/" in id_arg, f"Expected / for view path: {id_arg!r}"
        assert "~" not in id_arg, f"View should use / not ~: {id_arg!r}"
