"""MCP stdio server. Thin FastMCP wrapper around `PrecisRuntime`.

Seven tools — `get`, `search`, `put`, `edit`, `delete`, `tag`, `link`
— are registered as plain sync functions. FastMCP runs sync tool
callables in a worker thread, so the rest of the codebase (runtime,
store, handlers) stays sync.

The runtime — including the postgres connection pool — is built before
`mcp.run()` and torn down after it returns. Only the FastMCP loop
itself is async; everything below this file is sync.

Tests should not import this module; they construct `PrecisRuntime`
directly via fixtures and call `.dispatch(verb, args)` to bypass the
MCP transport.
"""

from __future__ import annotations

import atexit
import logging
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from precis.runtime import PrecisRuntime, build_runtime
from precis.tools import TOOL_REGISTRY

# FastMCP refuses ``str | CallToolResult`` return annotations (it bans
# CallToolResult inside unions; see ``func_metadata.py``). We still
# return ``CallToolResult`` at runtime on errors — FastMCP's
# ``FuncMetadata.convert_result`` passes ``CallToolResult`` instances
# through verbatim so the protocol-level ``isError`` flag is preserved.
# Each tool's annotation therefore stays ``str``; the actual return
# type is ``str | CallToolResult`` but only ``str`` is advertised to
# FastMCP.
_ToolReturn = Any  # documents runtime: str on success, CallToolResult on error

# mcp 1.27.0's ``FuncMetadata.convert_result`` validates
# ``CallToolResult.structuredContent`` against the auto-generated output
# schema whenever the tool has one.  Our error path returns a
# ``CallToolResult`` with only ``content`` + ``isError`` set, so the
# validation against the ``str``-shaped schema rejects ``structuredContent
# = None``.  Disabling structured output skips that validation and lets
# the success path render plain ``TextContent`` directly.  Agents see no
# difference — every wrapper has always grokked the ``[error:Class]
# cause / options / next`` text.
_TOOL_KW: dict[str, Any] = {"structured_output": False}

_INSTRUCTIONS = (
    "precis-mcp v2 - seven-verb agent tool surface.\n\n"
    "First action on any non-trivial request:\n"
    "  search(kind='skill', q='<your goal in 2-5 words>')\n"
    "This returns ranked help skills (verb mechanics, kind specifics,\n"
    "tag axes, edit protocol, ...). For the full index:\n"
    "  get(kind='skill', id='toc').\n\n"
    "Verbs: get, search, put, edit, delete, tag, link.  Discriminator: kind=."
)

# Sanity check the instructions actually advertise every verb. The MCP
# critic flagged ``put, put`` as a silent typo that hid a verb from
# every caller relying on serverInfo.instructions; an assertion here
# catches future regressions at import time.
assert all(
    v in _INSTRUCTIONS
    for v in ("get", "search", "put", "edit", "delete", "tag", "link")
), "_INSTRUCTIONS must list every agent-facing verb"


# File-rooted kinds and their extensions. Used by the instructions
# composer to count files in ``PRECIS_ROOT`` so an MCP client sees
# *"Editable sandbox (3 markdown, 1 plaintext, 1 tex)"* on connect
# instead of a kind-agnostic verb blurb. (MCP critic MAJOR-C — cold-
# start discoverability: tool descriptions don't reveal that
# ``PRECIS_ROOT`` exists or that ``get(kind='markdown')`` lists it.)
_FILE_KIND_EXTS: dict[str, frozenset[str]] = {
    "markdown": frozenset({".md"}),
    "plaintext": frozenset({".txt", ".log"}),
    "tex": frozenset({".tex"}),
}


def _file_kind_counts(root: str, kinds: list[str]) -> dict[str, int]:
    """One ``os.walk`` over ``root`` counting files per file-rooted kind.

    ``kinds`` is the subset of :data:`_FILE_KIND_EXTS` actually registered
    in the current build — a kind absent from the runtime hub (e.g.
    ``tex`` on a build that never registered it) is left out of the
    tally. Every listed kind appears in the return value, possibly with
    ``0`` so callers can distinguish *"registered but empty"* from
    *"not registered"*.
    """
    counts: dict[str, int] = {k: 0 for k in kinds}
    if not counts:
        return counts
    ext_to_kind: dict[str, str] = {
        ext: kind for kind in kinds for ext in _FILE_KIND_EXTS.get(kind, ())
    }
    root_path = Path(root)
    if not root_path.is_dir():
        return counts
    for _dirpath, _dirnames, files in os.walk(root_path):
        for name in files:
            kind = ext_to_kind.get(Path(name).suffix.lower())
            if kind is not None:
                counts[kind] += 1
    return counts


def _startup_skills_banner(runtime: PrecisRuntime) -> str:
    """Render the ``PRECIS_STARTUP_SKILLS`` notice (or empty string).

    Wraps :mod:`precis.startup_skills` so the server module owns the
    config-to-banner translation and `_build_instructions` stays
    declarative. Returns ``""`` when the env var is unset / empty
    and no errors occurred — the design's "zero unconditional bytes
    paid by operators who don't opt in" guarantee.
    """
    from precis import startup_skills

    config = runtime.config
    raw = getattr(config, "startup_skills", None)
    cap_kb = getattr(config, "startup_skills_cap_kb", 50)
    slugs = startup_skills.parse(raw)
    if not slugs:
        return ""
    verdicts = getattr(runtime.hub, "loadabilities", None) or {}
    unavailable = frozenset(
        v.kind for v in verdicts.values() if not getattr(v, "loaded", True)
    )
    return startup_skills.format_banner(
        startup_skills.resolve(slugs, cap_kb=cap_kb, unavailable_kinds=unavailable)
    )


def _kinds_loaded_line(runtime: PrecisRuntime) -> str:
    """Render the ``Kinds loaded: ...`` summary appended to every banner.

    Sourced from ``runtime.hub.kinds`` (a set of every kind with at
    least one registered ability) sorted for stable rendering across
    boots. Empty registries render as ``Kinds loaded: (none)``; a
    stateless build with no handlers wired still produces a
    well-formed line rather than a dangling ``Kinds loaded: ``.
    """
    kinds = sorted(getattr(runtime.hub, "kinds", ()) or ())
    if not kinds:
        return "Kinds loaded: (none)"
    return "Kinds loaded: " + ", ".join(kinds) + "."


def _kinds_unavailable_line(runtime: PrecisRuntime) -> str:
    """Render the ``Kinds unavailable: ...`` summary, or ``""``.

    Sourced from ``runtime.hub.loadabilities``: every kind boot
    *attempted* and gated out (prohibited / missing-env / init-failed)
    gets a one-entry. Kinds we never even tried (no store, no
    PRECIS_ROOT, no python roots) intentionally don't appear — they
    aren't ``unavailable``, they're ``not configured``, and surfacing
    them would dwarf the banner. The boot-time log line names them
    for the operator who wants to audit.
    """
    from precis.kind_gate import format_unavailable

    verdicts = getattr(runtime.hub, "loadabilities", None) or {}
    return format_unavailable(verdicts)


def _build_instructions(runtime: PrecisRuntime) -> str:
    """Static verb blurb plus a workspace preamble plus a live-kinds summary.

    Composition (top to bottom):

    1. Optional sandbox preamble — when ``PRECIS_ROOT`` is set AND a
       file-rooted handler is registered. Names file counts and the
       ``get(kind='…')`` index calls a cold-start agent would
       otherwise not know to try.
    2. Static core (:data:`_INSTRUCTIONS`) — the discovery CTA plus
       the seven-verb list. Pinned by the import-time assertion at
       :data:`_INSTRUCTIONS` and by
       ``test_instructions_advertises_every_verb``.
    3. ``Kinds loaded:`` line — sorted live registry. Always appended;
       the empty case renders as ``Kinds loaded: (none)`` rather than
       a dangling colon.
    4. Optional ``Kinds unavailable:`` line — emitted only when boot
       attempted at least one kind and gated it out (prohibited,
       missing env var, init failure). Zero bytes on a clean boot.
    5. Optional ``PRECIS_STARTUP_SKILLS`` banner — pinned-skill ids,
       plus warning lines for unknown slugs and cap truncation. Zero
       bytes when the env var is unset and no errors occurred.

    Branches for the preamble specifically:

    - No ``root`` configured (``PRECIS_ROOT`` unset): no preamble.
    - ``root`` set but no file-rooted handler registered (e.g. store-
      only build that somehow has the env var): no preamble — the
      preamble would lie.
    - ``root`` set, at least one file kind registered, tree empty:
      preamble invites the agent to create a file.
    - ``root`` set and files present: preamble lists counts and the
      per-kind index call.

    The workspace-tag hint carries a concrete ``search(q=...,
    tags=['workspace'])`` example rather than prose — empty-``q``
    listing isn't supported today, so the banner teaches the shape
    that actually runs.
    """
    core = _INSTRUCTIONS
    kinds_line = _kinds_loaded_line(runtime)
    unavailable_line = _kinds_unavailable_line(runtime)
    startup = _startup_skills_banner(runtime)
    tail = f"\n\n{kinds_line}"
    if unavailable_line:
        tail += f"\n{unavailable_line}"
    if startup:
        tail += f"\n{startup}"
    root = getattr(runtime.config, "root", None)
    if not root:
        return core + tail
    registered_kinds = runtime.hub.kinds
    file_kinds = [k for k in _FILE_KIND_EXTS if k in registered_kinds]
    if not file_kinds:
        return core + tail
    counts = _file_kind_counts(root, file_kinds)
    total = sum(counts.values())
    if total == 0:
        preamble = (
            "Editable sandbox under PRECIS_ROOT is empty. Create a file with\n"
            "  put(kind='markdown'|'plaintext'|'tex', id='<slug>',\n"
            "      text='...', mode='create')\n"
            "Everything here carries the `workspace` tag - scope search with:\n"
            "  search(q='<keyword>', tags=['workspace'])\n\n"
        )
    else:
        summary = ", ".join(f"{counts[k]} {k}" for k in file_kinds if counts[k])
        by_kind = "\n".join(f"  get(kind='{k}')" for k in file_kinds)
        preamble = (
            f"Editable sandbox under PRECIS_ROOT ({summary}). List with:\n"
            f"{by_kind}\n"
            "Everything here carries the `workspace` tag - scope search with:\n"
            "  search(q='<keyword>', tags=['workspace'])\n\n"
        )
    return preamble + core + tail


_runtime: PrecisRuntime | None = None


def _rt() -> PrecisRuntime:
    if _runtime is None:
        raise RuntimeError(
            "precis runtime not initialised - call _init_runtime() first"
        )
    return _runtime


def _init_runtime() -> PrecisRuntime:
    """Build the runtime once and register cleanup at process exit.

    Also wires the prompts and resources modalities — skill files
    surface as ``prompts/list`` entries and kind handlers as
    ``resources/list`` + ``resources/templates/list``.  Both
    surfaces delegate to the runtime so there is no parallel
    rendering pipeline.  See :mod:`precis.mcp_modalities`.

    Finally, rewrites ``serverInfo.instructions`` so the MCP client
    sees a sandbox preamble on connect when ``PRECIS_ROOT`` is set.
    The default, module-load-time instructions are still the static
    verb blurb (``_INSTRUCTIONS``) so tests that import ``server``
    without initialising the runtime continue to see the pinned
    string.
    """
    global _runtime
    if _runtime is not None:
        return _runtime
    _runtime = build_runtime()
    _wire_modalities(_runtime)
    _apply_instructions(mcp, _runtime)
    atexit.register(_shutdown_runtime)
    return _runtime


def _apply_instructions(fastmcp: FastMCP, runtime: PrecisRuntime) -> None:
    """Overwrite ``serverInfo.instructions`` on the underlying MCP server.

    FastMCP 1.x exposes ``instructions`` as a read-only property that
    delegates to ``self._mcp_server.instructions`` (a plain attribute
    on the lowlevel ``MCPServer``). No setter; best-effort mutation of
    the underlying attribute is the only knob until upstream grows one.
    On failure we log and keep the static core — the server still
    boots, agents just miss the dynamic preamble.
    """
    try:
        fastmcp._mcp_server.instructions = _build_instructions(runtime)
    except Exception:
        log.exception("failed to apply dynamic instructions")


def _wire_modalities(runtime: PrecisRuntime) -> None:
    """Register prompts + resources for the running MCP.

    Best-effort: if either registration fails we log and continue —
    a wiring bug must not prevent the MCP from booting and serving
    the four verb tools.  The MCP critic flagged the modality gap
    as MINOR; the tools surface remains the priority.
    """
    from precis.mcp_modalities import register_resources, register_skill_prompts

    try:
        register_skill_prompts(mcp, runtime)
    except Exception:
        log.exception("failed to register skill prompts")
    try:
        register_resources(mcp, runtime)
    except Exception:
        log.exception("failed to register resources")


def _shutdown_runtime() -> None:
    global _runtime
    if _runtime is not None and _runtime.store is not None:
        try:
            _runtime.store.close()
        except Exception:
            log.exception("error closing store")
    _runtime = None


log = logging.getLogger(__name__)
# Server name is ``precis-mcp`` so log lines and serverInfo unambiguously
# point at this package (the bare ``precis`` collides with other tooling).
# MCP 2025-06-18 §initialize recommends a human-facing ``serverInfo.title``
# too; FastMCP 1.x doesn't expose it through the constructor and the
# attribute path has changed between revisions. Leave unset until the
# upstream library grows a stable API for it. (Critic MINOR-C A1.)
mcp: FastMCP = FastMCP("precis-mcp", instructions=_INSTRUCTIONS)


# ---------------------------------------------------------------------------
# Tools — the four verbs
# ---------------------------------------------------------------------------


def _dispatch(verb: str, payload: dict[str, Any]) -> _ToolReturn:
    """Dispatch one verb call and shape the MCP-level result.

    On success, returns the rendered string — FastMCP wraps it as the
    sole text content of the tool result. On error, returns a
    :class:`CallToolResult` with ``isError=True`` so the protocol
    surface matches the body. The body itself stays the same
    ``[error:Class] cause / options / next`` text the runtime always
    rendered, so wrappers that already grok that shape keep working.
    (MCP critic MAJOR — errors-as-strings without ``isError``.)
    """
    body, is_error = _rt().dispatch_with_status(verb, payload)
    if not is_error:
        return body
    return CallToolResult(
        content=[TextContent(type="text", text=body)],
        isError=True,
    )


def _validation_error(body: str) -> _ToolReturn:
    """Wrap a pre-dispatch validation error in a CallToolResult.

    Used by the ``search`` and ``get`` tools when they reject malformed
    arguments before reaching the runtime. Keeps the protocol surface
    consistent with runtime-side errors.
    """
    return CallToolResult(
        content=[TextContent(type="text", text=body)],
        isError=True,
    )


# Tool functions are now imported from shared registry


# ---------------------------------------------------------------------------
# Tool registration from shared registry
# ---------------------------------------------------------------------------


def _register_tools_from_registry() -> None:
    """Register all tools from the shared registry with FastMCP."""
    for tool_name, tool_info in TOOL_REGISTRY.items():
        # Register the tool function with FastMCP
        mcp.tool(**_TOOL_KW)(tool_info["func"])

        # Apply special schema constraints for edit tool
        if tool_name == "edit":
            _install_edit_schema_constraints(mcp)


def _install_edit_schema_constraints(mcp_app: FastMCP) -> None:
    """Rewrite ``edit``'s inputSchema to encode per-mode required args.

    Pydantic generates a flat schema where ``text``, ``find`` and
    ``where`` are all optional (because the Python-level defaults
    are ``None``). That's a lie — **every** wire-level mode requires
    ``text=``, ``find-replace`` and ``insert`` additionally require
    ``find=``, and ``insert`` also requires ``where=``. Small models
    read the schema's ``required`` array at face value; burying the
    coupling in prose cost a retry loop on qwen3:8b (MCP critic
    MAJOR-C, 2026-05-03 — small models called ``edit(... mode=
    'find-replace', find=..., before=..., after=...)`` repeatedly
    with no ``text=`` and did not recover from the runtime error).

    Constraints applied:

    - ``text`` is appended to top-level ``required`` (unconditional —
      every mode needs it).
    - The mode-conditional coupling for ``find`` / ``where`` is encoded
      in per-property ``description`` fields. We *cannot* use top-level
      ``allOf`` + ``if/then`` here: Anthropic's ``/v1/messages`` API
      rejects ``oneOf``/``allOf``/``anyOf`` at the root of
      ``tools[].custom.input_schema`` with a 400, which blocks every
      tool call. Property descriptions are the next-best signal for
      schema-reading clients, and the runtime ``BadInput`` path remains
      the safety net.

    The mutation runs once at module import. FastMCP's ``list_tools``
    copies ``tool.parameters`` into the wire ``inputSchema`` on every
    call, so the constraint surfaces to every client.
    """
    tool = mcp_app._tool_manager.get_tool("edit")
    if tool is None:  # pragma: no cover — edit always registers above
        return
    params = tool.parameters
    # Sentinel keeps the mutation idempotent: multiple imports or a
    # second call from a test harness must not re-append ``text`` to
    # ``required`` or duplicate the coupling notes.
    if params.get("x-precis-edit-constraints-installed"):
        return

    # ``text`` is required on every wire-level mode. Top-level
    # ``required`` is the field small models read first.
    required = list(params.get("required", []))
    if "text" not in required:
        required.append("text")
    params["required"] = required

    # Mode-conditional coupling, encoded as description suffixes on the
    # affected properties. Small models scan property descriptions
    # alongside the ``required`` array; the runtime ``BadInput`` path
    # is the safety net when they ignore both.
    properties = params.get("properties") or {}

    _MODE_COUPLING = (
        " REQUIRED WHEN: mode='find-replace' (default) or mode='insert'."
    )
    _WHERE_COUPLING = " REQUIRED WHEN: mode='insert'. Value: 'before' or 'after'."
    _MODE_NOTE = (
        " Per-mode required args: 'find-replace' (default) → find=, text=;"
        " 'insert' → find=, text=, where=; 'append'/'replace' → text=."
    )

    def _append_desc(prop: str, suffix: str) -> None:
        schema = properties.get(prop)
        if not isinstance(schema, dict):
            return
        existing = schema.get("description") or ""
        if suffix.strip() in existing:
            return
        schema["description"] = (existing + suffix).strip()

    _append_desc("find", _MODE_COUPLING)
    _append_desc("where", _WHERE_COUPLING)
    _append_desc("mode", _MODE_NOTE)

    params["x-precis-edit-constraints-installed"] = True


# Register all tools from the shared registry
_register_tools_from_registry()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP stdio server.

    Build the runtime (including postgres pool) before mcp.run takes
    over, register atexit shutdown, then hand control to FastMCP.
    """
    from precis.config import load_config

    config = load_config()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    _init_runtime()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
