"""Prove a halted tick can retry from its complete, uncapped wire audit.

The tool ledger is best-effort and records completed dispatches only, so a
missing row cannot prove that no write started. Inspect the runner's raw
Claude stream before its storage cap instead. Unknown transports, tools and
query variants require human review. Even acknowledged writes are refused:
the tick may repeat them and their idempotence is not part of this contract.
"""

from __future__ import annotations

import json
import re
from typing import Any

_READ_TOOLS = frozenset({"Read", "Grep", "Glob", "ToolSearch"})
_QUERY_KINDS = frozenset(
    {"skill", "memory", "taxon", "python", "md", "markdown", "plaintext", "tex"}
)
_QUERY_KEYS = frozenset(
    {
        "kind",
        "id",
        "q",
        "view",
        "extent",
        "scope",
        "tags",
        "page_size",
        "page",
        "cursor",
        "sort",
        "limit",
        "k",
        "args",
        "mode",
    }
)
_QUERY_ARG_KEYS = frozenset({"under", "depth", "offset", "limit", "heading"})
_QUERY_VIEWS = frozenset(
    {
        "index",
        "page",
        "brief",
        "table",
        "outline",
        "source",
        "provenance",
        "toc",
        "processing",
    }
)
_SEARCH_MODES = frozenset({"pattern", "lexical", "semantic", "hybrid", "browse"})
_FISHEYE = re.compile(r"fisheye(?:\+[12]hop)?(?:\+recall)?\Z")


def _query_is_read(name: str, args: dict[str, Any]) -> bool:
    """A small allowlist; kind-specific compute doors are not ordinary reads."""
    if name not in {"mcp__precis__get", "mcp__precis__search"}:
        return False
    kind = args.get("kind")
    if not isinstance(kind, str) or kind not in _QUERY_KINDS:
        return False
    if set(args) - _QUERY_KEYS:
        return False
    extras = args.get("args")
    if extras is not None and (
        not isinstance(extras, dict) or set(extras) - _QUERY_ARG_KEYS
    ):
        return False
    view = args.get("view")
    if view is not None and (
        not isinstance(view, str)
        or (view not in _QUERY_VIEWS and _FISHEYE.fullmatch(view) is None)
    ):
        return False
    mode = args.get("mode")
    if mode is not None and (
        name != "mcp__precis__search"
        or not isinstance(mode, str)
        or mode not in _SEARCH_MODES
    ):
        return False
    return True


def assess_halt_retry(raw_stream: str) -> tuple[bool, str]:
    """Return whether the complete tick audit proves automatic retry safe.

    Sidechain events are deliberately included: a subagent's writes count.
    Error results from proven reads are safe, but every invocation must have
    a result. Plain text, partial streams and unrecognized tools fail closed.
    """
    initialized = False
    finished = False
    calls: set[str] = set()
    results: set[str] = set()
    for line in raw_stream.splitlines():
        if not line.strip():
            continue
        if finished:
            return False, "audit has events after its terminal result"
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return False, "complete stream audit unavailable"
        if not isinstance(event, dict):
            return False, "malformed stream audit event"
        event_type = event.get("type")
        if not initialized:
            if event_type != "system" or event.get("subtype") != "init":
                return False, "stream audit has no initial session receipt"
            initialized = True
            continue
        if event_type == "result":
            if not isinstance(event.get("is_error"), bool):
                return False, "terminal audit result has unknown outcome"
            finished = True
            continue
        if event_type in {"system", "control_response", "rate_limit_event"}:
            continue
        if event_type not in {"assistant", "user"}:
            return False, "unrecognized stream audit event"
        message = event.get("message")
        content = (
            message.get("content")
            if isinstance(message, dict)
            else event.get("content")
        )
        if isinstance(content, str):
            continue
        if not isinstance(content, list):
            return False, "stream message content is unavailable"
        for block in content:
            if not isinstance(block, dict):
                return False, "malformed stream content block"
            block_type = block.get("type")
            if block_type == "tool_use":
                call_id = block.get("id")
                name = block.get("name")
                args = block.get("input")
                if (
                    event_type != "assistant"
                    or not isinstance(call_id, str)
                    or not call_id
                    or call_id in calls
                    or not isinstance(name, str)
                    or not isinstance(args, dict)
                ):
                    return False, "malformed or duplicate tool invocation"
                if name not in _READ_TOOLS and not _query_is_read(name, args):
                    return (
                        False,
                        "tick invoked a tool whose retry effects require human review",
                    )
                calls.add(call_id)
            elif block_type == "tool_result":
                call_id = block.get("tool_use_id")
                if (
                    event_type != "user"
                    or not isinstance(call_id, str)
                    or call_id not in calls
                    or call_id in results
                ):
                    return False, "unmatched or duplicate tool result"
                results.add(call_id)
            elif block_type not in {"text", "thinking", "redacted_thinking"}:
                return False, "unrecognized stream content block"
    if not initialized or not finished:
        return False, "complete stream audit unavailable"
    if calls != results:
        return False, "tick has tool invocations without completion receipts"
    return True, "complete audit contains only acknowledged read operations"
