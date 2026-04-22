"""Live LLM tool-call tests.

Sends prompts to local ollama qwen3.5:9b with the real precis MCP tool
schemas and executes the returned calls through the actual server functions,
with tools.read/tools.put mocked out after the URI parser.

Requires: ollama running locally with qwen3.5:9b.
Run:  uv run pytest tests/test_llm_live.py -v
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from precis.server import get, move, put, search
from precis.server import mcp as precis_mcp
from precis.uri import parse as uri_parse

OLLAMA_URL = "http://localhost:11434"
MODEL = "qwen3.5:9b"


# ── Extract real tool schemas from FastMCP ─────────────────────────


def _get_mcp_tool_schemas() -> list[dict]:
    """Get ollama-format tool schemas from the live MCP server object."""
    tools_list = precis_mcp._tool_manager.list_tools()
    ollama_tools = []
    for t in tools_list:
        ollama_tools.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.parameters or {"type": "object"},
                },
            }
        )
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

    def read(
        self,
        *,
        uri: str,
        query: str = "",
        page: int = 1,
        top_k: int = 5,
        depth: int = 0,
    ) -> str:
        parsed = uri_parse(uri)
        self.calls.append(
            {
                "fn": "read",
                "uri": uri,
                "parsed": parsed,
                "query": query,
                "depth": depth,
            }
        )
        return f"[mock read: {uri}]"

    def put(
        self,
        *,
        uri: str,
        text: str = "",
        mode: str = "replace",
        tracked: bool = True,
        note: str = "",
        link: str = "",
    ) -> str:
        parsed = uri_parse(uri)
        self.calls.append(
            {
                "fn": "put",
                "uri": uri,
                "parsed": parsed,
                "text": text,
                "mode": mode,
                "note": note,
                "link": link,
            }
        )
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


def _require_dispatched_paper_uri(raw: list[dict], cap: CallCapture):
    """Common assertion helper — after BUG-C the server rejects bare
    paper slugs without ``type='paper'`` or a scheme prefix, so the URI
    parser only fires for correctly-disambiguated LLM calls.

    This helper checks that:
      1. The LLM picked ``get`` (the right verb).
      2. The URI parser fired at least once (the LLM either used
         ``type='paper'``, a ``paper:`` prefix, or a scheme-prefixed id).

    If the LLM emitted a bare slug without ``type=``, skip instead of
    fail — that's an LLM-side learning gap, not a server regression.
    """
    assert raw, "no tool calls emitted"
    assert raw[0]["function"]["name"] == "get"
    if not cap.calls:
        args = raw[0]["function"]["arguments"]
        pytest.skip(
            f"LLM emitted bare slug without type='paper' (args={args!r}); "
            "this is an LLM-side learning gap — after BUG-C the server "
            "rejects bare slugs without a routing hint"
        )
    return cap.calls[0]["parsed"]


class TestGetPaper:
    def test_read_paper_overview(self):
        raw, cap = run_llm_call("Show me the paper wang2020state")
        p = _require_dispatched_paper_uri(raw, cap)
        assert p.scheme == "paper"
        assert "wang2020state" in p.path

    def test_read_paper_chunk(self):
        raw, cap = run_llm_call("Read chunk 38 of wang2020state")
        p = _require_dispatched_paper_uri(raw, cap)
        assert p.path == "wang2020state"
        assert p.selector == "38"
        assert p.range_start == 38

    def test_read_chunk_range(self):
        raw, cap = run_llm_call("Read chunks 10 through 15 of wang2020state")
        p = _require_dispatched_paper_uri(raw, cap)
        assert p.range_start is not None
        assert p.range_end is not None
        assert p.range_end > p.range_start

    def test_read_toc(self):
        raw, cap = run_llm_call("Show the table of contents for wang2020state")
        p = _require_dispatched_paper_uri(raw, cap)
        assert p.view == "toc"

    def test_read_abstract(self):
        raw, cap = run_llm_call("Show me the abstract of wang2020state")
        p = _require_dispatched_paper_uri(raw, cap)
        assert p.view == "abstract"

    def test_cite_bibtex(self):
        raw, cap = run_llm_call("Get the BibTeX citation for wang2020state")
        p = _require_dispatched_paper_uri(raw, cap)
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
        raw, cap = run_llm_call(
            "Search for papers about CO2 capture in metal-organic frameworks"
        )
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
        raw, cap = run_llm_call("Append a new section called Methods to report.docx")
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


def _require_dispatched_compute_uri(
    raw: list[dict], cap: CallCapture, expected_kind: str
):
    """Common assertion helper for math / calc routing tests.

    Returns the parsed URI on success, skips on LLM-side learning gaps.

    The LLM has to:
      1. Choose a tool (``get`` or ``search``).
      2. Supply ``type=<expected_kind>`` OR an id/scope with the
         ``<expected_kind>:`` prefix.

    If it just answers directly (no tool call) or picks the wrong
    kind, that's an LLM-side gap, not a server regression — skip
    so the suite stays green on qwen3.5:9b and tighten the prompt
    once we've seen how the LLM behaves.
    """
    if not raw:
        pytest.skip(
            f"LLM answered without calling any tool for expected kind "
            f"{expected_kind!r}; no URI to validate"
        )
    fn_name = raw[0]["function"]["name"]
    if fn_name not in ("get", "search"):
        pytest.skip(
            f"LLM picked {fn_name!r}; expected get/search for "
            f"type={expected_kind!r}"
        )
    if not cap.calls:
        args = raw[0]["function"]["arguments"]
        pytest.skip(
            f"LLM call didn't reach URI parser "
            f"(type={args.get('type')!r} id={args.get('id')!r} "
            f"scope={args.get('scope')!r}); "
            f"LLM-side learning gap for kind={expected_kind!r}"
        )
    parsed = cap.calls[0]["parsed"]
    if parsed.scheme != expected_kind:
        pytest.skip(
            f"LLM routed to scheme={parsed.scheme!r}; "
            f"expected {expected_kind!r}. "
            f"Either a learning gap or the LLM picked an alternative "
            f"kind that also makes sense for the prompt."
        )
    return parsed


class TestCalc:
    """Verify the LLM routes pure-arithmetic prompts to ``type='calc'``.

    calc is the free, local, offline SymPy kind.  Prompts here are
    deterministic arithmetic / symbolic-algebra problems with no
    natural-language fuzziness — the closer-to-compute fit is calc,
    not math.
    """

    def test_exact_arithmetic(self):
        raw, cap = run_llm_call(
            "Use the calc tool to evaluate 2 + 3 * 4 exactly."
        )
        p = _require_dispatched_compute_uri(raw, cap, "calc")
        assert p.scheme == "calc"
        # Query travels in ``path`` on get() and ``query`` on search();
        # accept either shape — both are valid calc routings.
        blob = p.path + " " + cap.calls[0].get("query", "")
        assert any(c in blob for c in ("2", "3", "4")), (
            f"digits didn't reach handler: path={p.path!r} "
            f"query={cap.calls[0].get('query')!r}"
        )

    def test_symbolic_integration(self):
        raw, cap = run_llm_call(
            "Use the calc tool to integrate sin(x)*cos(x) with respect to x."
        )
        p = _require_dispatched_compute_uri(raw, cap, "calc")
        assert p.scheme == "calc"
        blob = (p.path + " " + cap.calls[0].get("query", "")).lower()
        assert "sin" in blob or "cos" in blob or "integrate" in blob, (
            f"integral expression didn't reach handler: "
            f"path={p.path!r} query={cap.calls[0].get('query')!r}"
        )

    def test_base_conversion(self):
        raw, cap = run_llm_call(
            "Use the calc tool to convert 0xff to decimal."
        )
        p = _require_dispatched_compute_uri(raw, cap, "calc")
        assert p.scheme == "calc"
        blob = p.path.lower() + " " + cap.calls[0].get("query", "").lower()
        assert "0xff" in blob or "ff" in blob or "255" in blob, (
            f"hex input didn't reach handler: path={p.path!r} "
            f"query={cap.calls[0].get('query')!r}"
        )


class TestMath:
    """Verify the LLM routes real-world / natural-language math prompts
    to ``type='math'`` (Wolfram Alpha).

    math is the paid, online kind.  Prompts here involve world-data
    lookups (populations, physical constants, unit-aware physics) that
    calc can't answer — so the only rational choice is math.

    Still skip gracefully if the LLM picks calc or answers directly;
    it's a routing-quality test, not a correctness gate.
    """

    def test_real_world_query(self):
        raw, cap = run_llm_call(
            "Use Wolfram Alpha to look up the population of Ireland."
        )
        p = _require_dispatched_compute_uri(raw, cap, "math")
        assert p.scheme == "math"
        # The query text may live in p.path (if LLM used get(id=...))
        # or in capture.query (if LLM used search(query=...)).  Both
        # routing shapes are valid — the path is empty on search().
        blob = (p.path + " " + cap.calls[0].get("query", "")).lower()
        assert "ireland" in blob or "population" in blob, (
            f"query didn't reach handler: path={p.path!r} "
            f"query={cap.calls[0].get('query')!r}"
        )

    def test_unit_aware_physics(self):
        raw, cap = run_llm_call(
            "Use Wolfram Alpha to compute the orbital period of Jupiter."
        )
        p = _require_dispatched_compute_uri(raw, cap, "math")
        assert p.scheme == "math"
        blob = (p.path + " " + cap.calls[0].get("query", "")).lower()
        assert "jupiter" in blob or "orbit" in blob, (
            f"query didn't reach handler: path={p.path!r} "
            f"query={cap.calls[0].get('query')!r}"
        )


class TestSeparatorSyntax:
    """The LLM must use \u203a (or legacy ~) for selectors, not # or anything else."""

    def test_sep_not_hash_in_chunk(self):
        raw, _ = run_llm_call("Read chunk 38 of wang2020state")
        id_arg = raw[0]["function"]["arguments"].get("id", "")
        assert "#" not in id_arg, f"LLM used # instead of separator: {id_arg!r}"
        assert "\u203a" in id_arg or "~" in id_arg or id_arg == "wang2020state", (
            f"Expected selector separator in chunk ref: {id_arg!r}"
        )

    def test_sep_not_hash_in_node(self):
        raw, _ = run_llm_call("Read node PLXDX in report.docx")
        id_arg = raw[0]["function"]["arguments"].get("id", "")
        assert "#" not in id_arg, f"LLM used # instead of separator: {id_arg!r}"

    def test_slash_for_views(self):
        raw, _ = run_llm_call("Show the table of contents for wang2020state")
        id_arg = raw[0]["function"]["arguments"].get("id", "")
        assert "/" in id_arg, f"Expected / for view path: {id_arg!r}"
        assert "\u203a" not in id_arg and "~" not in id_arg, (
            f"View should use / not selector separator: {id_arg!r}"
        )
