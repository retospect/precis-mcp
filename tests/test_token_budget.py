"""Phase 6 regression guard: cold-start token budget.

The cold-start banner and ``tools/list`` response are the two
unconditional cost centres on a fresh MCP session. This test pins
their byte budgets so a careless docstring or banner edit doesn't
silently blow up the context every connecting agent eats on the
first message.

Two budgets, both lifted from
``docs/design/mcp-cold-start-token-budget.md`` Phase 6 step 1:

- ``tools/list`` JSON      < 12 KB (current measured baseline ~9 KB)
- per-verb description     <  1 KB (description-only, schema excluded)
- ``serverInfo.instructions`` < 2 KB on a clean runtime
- ``serverInfo.instructions`` < 4 KB with every Phase-3-5 feature engaged

The 12 KB ``tools/list`` ceiling reflects the post-Phase-1 measured
baseline. The original design target (8 KB) predated the per-arg
CLI help threading + ``edit``'s mode-coupling description suffixes
constraint, both of which inflated the wire shape past the design
estimate without adding agent-facing context cost on the cold-start
banner side. The cap gives ~33% headroom; if the actual size ever
hits 11 KB, investigate which schema or description grew.

Per-verb description caps (1 KB) target the docstring text — the
input schema is excluded because schema bytes are unavoidable and
mostly invisible to the agent (consumed at validation time, not
when reading the tool list).

The 2 KB / 4 KB ``instructions`` budgets pin the cold-start banner
shape. Static core lives in ``server._INSTRUCTIONS``; Phase-3-5
extras (sandbox preamble, kinds-loaded summary, kinds-unavailable
summary, pinned-skills banner) all compose on top.
"""

from __future__ import annotations

import json

import pytest

# ---------------------------------------------------------------------------
# tools/list wire-shape budget
# ---------------------------------------------------------------------------


def _tools_list_wire_shape() -> list[dict[str, object]]:
    """Reconstruct what FastMCP would send on ``tools/list``.

    FastMCP's ``ToolManager`` stores ``Tool`` objects with a
    pydantic-derived ``parameters`` JSON schema. The wire shape
    includes the tool name, description, and inputSchema (which is
    ``parameters`` verbatim). The actual server-side serialisation
    runs through MCP's ``MCPTool`` dataclass, but for byte-budget
    purposes the JSON serialisation of this shape is a faithful
    proxy — the field set is identical between FastMCP's
    ``list_tools()`` builder and what we reconstruct here.
    """
    from precis import server

    out: list[dict[str, object]] = []
    for tool in server.mcp._tool_manager.list_tools():
        out.append(
            {
                "name": tool.name,
                "description": tool.description or "",
                "inputSchema": tool.parameters or {},
            }
        )
    return out


def test_tools_list_under_byte_budget() -> None:
    """``tools/list`` JSON serialisation stays under 14 KB.

    The seven-verb surface (get, search, put, edit, delete, tag,
    link) plus FastMCP's input-schema overhead is the cost centre
    here. The schema bytes are largely unavoidable (mode-specific
    constraints on ``edit``, type/required information on every
    verb, plus the kind-specific kwargs on ``put`` that strict-schema
    clients need to accept findings/citations/jobs/etc.). The
    regression-guard target is therefore the verb *description*
    text — if this trips, the most likely culprit is a docstring
    that grew back beyond its post-Phase-1 trim; fix the docstring
    (push detail into a skill file) rather than bumping the cap.

    2026-06-11: cap raised from 12 KB → 14 KB to absorb the
    kind-specific kwargs added to ``put`` / ``edit`` (broad-pass
    usability findings #1 / #2). Schema-side growth is on-purpose;
    verb descriptions stayed tight.
    """
    serialised = json.dumps(_tools_list_wire_shape(), separators=(",", ":"))
    size = len(serialised.encode("utf-8"))
    assert size < 14 * 1024, (
        f"tools/list wire-shape JSON is {size} bytes (cap: 14 KB). "
        "Investigate which verb description or schema grew. The "
        "per-verb description cap (1 KB) is the easier diff to "
        "spot; bump that test's verbosity if needed."
    )


def test_tools_list_carries_every_seven_verb() -> None:
    """Symmetric guard against accidental drop: every verb on the
    seven-verb surface must register a tool.

    Without this, a hand-edited registration could shrink
    ``tools/list`` enough to satisfy the byte budget while removing
    a verb the agent depends on.
    """
    names = {tool["name"] for tool in _tools_list_wire_shape()}
    for verb in ("get", "search", "put", "edit", "delete", "tag", "link"):
        assert verb in names, (
            f"verb {verb!r} missing from tools/list — registration "
            f"regression. Saw: {sorted(names)}"
        )


@pytest.mark.parametrize(
    "verb",
    ["get", "search", "put", "edit", "delete", "tag", "link"],
)
def test_per_verb_description_under_budget(verb: str) -> None:
    """No single verb description should exceed 1 KB.

    Phase 1's docstring trim landed every verb under ~600 bytes;
    this guard gives ~70% headroom while still catching a careless
    revert that lands a verbose paragraph back in the docstring.
    Detail belongs in ``precis-<verb>-help.md``, not in the
    docstring that ships on every cold start.
    """
    from precis import server

    tool = server.mcp._tool_manager.get_tool(verb)
    assert tool is not None, f"tool {verb!r} not registered with FastMCP"
    desc = tool.description or ""
    size = len(desc.encode("utf-8"))
    assert size < 1024, (
        f"{verb}.description is {size} bytes (cap: 1 KB). Push detail "
        f"into precis-{verb}-help.md and trim the docstring."
    )


# ---------------------------------------------------------------------------
# serverInfo.instructions budget
# ---------------------------------------------------------------------------


def _instructions_for_clean_runtime() -> str:
    """Build the cold-start banner for a stateless runtime.

    Approximates the "minimal deployment" cold-start: no live
    sandbox preamble (no PRECIS_ROOT files), no startup-skills
    pinning, no prohibited kinds. This is the floor the banner
    must respect; per-deployment additions go on top but never
    materially.
    """
    from precis import server
    from precis.config import PrecisConfig
    from precis.runtime import PrecisRuntime

    class _FakeHub:
        kinds: set[str] = set()
        loadabilities: dict = {}

    return server._build_instructions(
        PrecisRuntime(config=PrecisConfig(), hub=_FakeHub())  # type: ignore[arg-type]
    )


def test_instructions_under_byte_budget_clean() -> None:
    """Stateless cold-start banner stays under 2 KB.

    The static core (skill-search CTA, seven-verb cheat sheet,
    per-verb hints) plus the trailing ``Kinds:`` line
    are the only mandatory components on a clean boot. If this
    trips, ``server._INSTRUCTIONS`` grew unexpectedly — push
    detail into ``precis-overview`` rather than bumping the cap.
    """
    text = _instructions_for_clean_runtime()
    size = len(text.encode("utf-8"))
    assert size < 2 * 1024, (
        f"serverInfo.instructions is {size} bytes on a clean runtime "
        "(cap: 2 KB). The static core lives in server._INSTRUCTIONS; "
        "push detail into precis-overview or per-verb help skills."
    )


def test_instructions_under_relaxed_budget_with_features() -> None:
    """Banner stays under 4 KB even with every Phase-3-5 feature
    engaged simultaneously.

    Worst-case unconditional banner shape:
      - sandbox preamble (PRECIS_ROOT with files)
      - Kinds: <several>
      - Kinds unavailable: <prohibited>
      - PRECIS_STARTUP_SKILLS pinned + kind_unavailable cross-check

    The 4 KB ceiling is the design's "even worst-case fits"
    promise — operators who turn on every knob still see a
    cold-start banner well under any sane MCP client's context
    budget.
    """
    from precis import server
    from precis.config import PrecisConfig
    from precis.kind_gate import Loadability
    from precis.runtime import PrecisRuntime

    class _FakeHub:
        kinds: set[str] = {"memory", "todo", "paper", "markdown"}
        loadabilities: dict[str, Loadability] = {
            "patent": Loadability("patent", False, "prohibited"),
            "web": Loadability("web", False, "missing FIRECRAWL_API_KEY"),
        }

    config = PrecisConfig(
        startup_skills="precis-overview,precis-search-help,precis-patent-help",
        startup_skills_cap_kb=50,
        kinds_disabled="patent",
    )
    rt = PrecisRuntime(config=config, hub=_FakeHub())  # type: ignore[arg-type]
    text = server._build_instructions(rt)
    size = len(text.encode("utf-8"))
    assert size < 4 * 1024, (
        f"serverInfo.instructions with every feature engaged is {size} "
        "bytes (cap: 4 KB). Phase 3/4/5 lines should each contribute "
        "<200 bytes; investigate which line grew."
    )


@pytest.mark.parametrize(
    "anchor",
    [
        "Discover:",
        "search(kind='skill', q=",
        "Kinds:",
    ],
)
def test_instructions_carries_anchor(anchor: str) -> None:
    """Every cold-start banner carries these structural anchors:

    - ``Discover:`` and the skill-search CTA pin the Phase-2
      framing — the first thing an agent should do on a non-trivial
      request is a skill search. (Tersified from "First action" in v8.7.6.)
    - ``Kinds:`` pins the Phase-2 trailer — every banner
      reports the live registry, even on a stateless build.

    Without these anchors, the banner would lose its discovery
    posture and revert to the pre-Phase-2 verb-cheat-sheet shape.
    """
    text = _instructions_for_clean_runtime()
    assert anchor in text, (
        f"anchor {anchor!r} missing from cold-start banner. "
        "Phase-2 framing regression — re-check server._INSTRUCTIONS "
        "and _build_instructions composition."
    )


# ---------------------------------------------------------------------------
# Cross-cutting: env-var parsers all share parse semantics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, ()),
        ("", ()),
        ("a", ("a",)),
        ("a,b,c", ("a", "b", "c")),
        ("  a , b  ,  c  ", ("a", "b", "c")),
        ("a,,b,", ("a", "b")),
        ("a,b,a", ("a", "b")),  # dedupe, first-occurrence wins
    ],
)
def test_env_var_parsers_share_semantics(
    value: str | None, expected: tuple[str, ...]
) -> None:
    """All three Phase 3/4/5 env-var parsers (``startup_skills.parse``,
    ``kind_gate.parse_disabled``, ``default_tags.parse``) share the
    same shape: comma-list, whitespace tolerant, empty-entry drop,
    duplicate dedupe.

    Pin the shared semantics so a future tweak to one parser
    doesn't silently desync the operator mental model. If the
    parsers diverge intentionally (e.g. one needs case-folding),
    update this test to mark which parser is the outlier.
    """
    from precis import default_tags, kind_gate, startup_skills

    # startup_skills.parse returns a list (operator order matters
    # for drop-tail truncation); the others return a tuple/frozenset.
    # Compare on contents not container.
    assert tuple(startup_skills.parse(value)) == expected
    assert default_tags.parse(value) == expected
    # parse_disabled returns a frozenset (membership-only); compare
    # as a set for that one.
    assert kind_gate.parse_disabled(value) == frozenset(expected)
