"""Normalized event schema shared by every stage of the session miner.

One record per *tool call* (plus a thin record per assistant turn, for token
accounting). Every corpus — local Claude Code ``.jsonl``, the prod
``tool_calls`` ledger, ``llm_call_log``, and ``kind='job'`` transcripts —
lands in this one shape so the detectors never learn where an event came
from beyond the ``corpus`` field.

Why a dataclass and not a bare dict: the detectors in :mod:`detect` are the
only consumers, and a typo in a key name there fails silently as "detector
never fires" — the worst failure mode for a mining pass, because a quiet
detector is indistinguishable from a clean corpus.

**Redaction boundary.** ``err_head`` / ``result_head`` / ``arg_digest`` are
the only fields carrying free text, and every writer passes them through
:func:`redact.scrub` before they reach disk. See ``README.md``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

#: Corpus labels. ``local`` is the only one carrying full payloads; the
#: ledger deliberately has none (migration 0133's "no payload content, ever").
CORPORA = ("local", "ledger", "llmlog", "jobs")

#: The precis MCP tool names as they appear in a local transcript. Both the
#: typed profile (``mcp__precis__get``) and the command profile
#: (``mcp__precis__precis``) are live; ``PRECIS_MCP_PROFILE`` picks which.
PRECIS_TOOL_PREFIX = "mcp__precis__"

#: Verbs the runtime dispatches. Kept here rather than imported from
#: ``precis.runtime`` so the miner runs with no precis install (it is an
#: operator script, and the corpus outlives any given checkout).
VERBS = ("get", "search", "put", "edit", "delete", "tag", "link", "more")

#: Head-slice length for free-text fields. Long enough to identify an error
#: or a render shape, short enough that an evidence card stays readable and a
#: scrub miss has a small blast radius.
HEAD_CHARS = 400


@dataclass(slots=True)
class Event:
    """One tool call, or one assistant turn when ``tool`` is ``None``."""

    corpus: str
    session: str
    seq: int
    ts: str | None = None

    # --- identity of the call -------------------------------------------
    tool: str | None = None
    """Raw tool name (``Bash``, ``Read``, ``mcp__precis__get``, ...)."""
    verb: str | None = None
    """precis verb, when this is a precis call."""
    kind: str | None = None
    """caller-supplied ``kind=``, pre-resolution (may be an invalid kind —
    that is itself signal, see detector D9)."""
    arg_keys: tuple[str, ...] = ()
    """Top-level argument NAMES. The one field the ledger and the local
    corpus both carry, so it is the join that makes them comparable."""
    arg_digest: str = ""
    """Short redacted rendering of the arguments, for evidence cards."""

    # --- outcome ---------------------------------------------------------
    is_error: bool = False
    err_type: str | None = None
    err_head: str = ""
    result_bytes: int = 0
    result_head: str = ""
    latency_ms: int | None = None

    # --- provenance / cost ----------------------------------------------
    model: str | None = None
    source: str | None = None
    """Model tier for ledger rows; caller label for llm_call_log rows."""
    profile: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    """``message.usage`` of the enclosing assistant turn. Tool-use blocks
    carry no token counts, so cost attribution is this join."""
    sidechain: bool = False
    cwd: str | None = None
    branch: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


def from_json(line: str) -> Event:
    return Event(**json.loads(line))


def is_precis_tool(name: str | None) -> bool:
    return bool(name) and name.startswith(PRECIS_TOOL_PREFIX)  # type: ignore[union-attr]


def verb_of(tool_name: str, args: dict[str, Any]) -> str | None:
    """Recover the verb from either MCP profile.

    Typed profile: one tool per verb (``mcp__precis__search``). Command
    profile: a single ``mcp__precis__precis`` tool whose ``command`` string
    opens with the verb (``search(kind='skill', q='...')``).
    """
    if not is_precis_tool(tool_name):
        return None
    tail = tool_name[len(PRECIS_TOOL_PREFIX) :]
    if tail in VERBS:
        return tail
    if tail == "precis":
        cmd = args.get("command")
        if isinstance(cmd, str):
            head = cmd.strip().split("(", 1)[0].strip()
            if head in VERBS:
                return head
    return None
