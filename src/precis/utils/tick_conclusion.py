"""Parse the planner's structured tick-conclusion block.

The planner-coroutine pattern asks each plan_tick LLM to end its
output with a short structured block summarising what the tick
produced. That summary feeds the parent's next re-tick as a slim
one-paragraph synth — much more useful than the raw stdout, much
cheaper than re-reading every artefact.

Block shape (the contract documents it; the parser is lenient):

::

    === TICK CONCLUSION ===
    verdict: done | continue | yield | halt
    summary: One paragraph synthesising what this tick produced —
             what was written, what was cited, what's left.
    files: tex/intro.tex, tex/methods.tex
    === END ===

Parser semantics:

* The block is optional — older agents and one-shot leaves may omit
  it. ``parse(text)`` returns ``None`` in that case.
* Fields are case-insensitive on the key; values are stripped.
* ``summary:`` may span multiple lines until the next ``key:`` or
  the ``=== END ===`` sentinel.
* The runner does NOT translate verdict to tags — the LLM still
  calls ``tag(id=N, add=['STATUS:done'])`` directly. The block is
  for richer summarisation in the audit chunk, not for state
  transitions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_BLOCK_RE: re.Pattern[str] = re.compile(
    r"=== *TICK *CONCLUSION *===\s*(.+?)\s*=== *END *===",
    re.DOTALL | re.IGNORECASE,
)

_FIELD_RE: re.Pattern[str] = re.compile(
    r"^(?P<key>verdict|summary|files)\s*:\s*(?P<val>.*)$",
    re.IGNORECASE,
)

_VALID_VERDICTS: frozenset[str] = frozenset(
    {"done", "continue", "yield", "halt"}
)


@dataclass(frozen=True)
class TickConclusion:
    """One parsed tick-conclusion block.

    ``verdict`` is normalised lower-case. ``summary`` keeps internal
    newlines but is right-stripped. ``files`` is a list of relative
    paths the LLM claims to have written this tick.
    """

    verdict: str | None
    summary: str | None
    files: list[str]


def parse(stdout: str) -> TickConclusion | None:
    """Extract the conclusion block from ``stdout``; return ``None`` if absent.

    Tolerant of:

    * Block missing entirely (returns ``None``)
    * Unknown fields (silently skipped)
    * Verdict not in the allowed set (kept as ``None`` so the runner
      doesn't render a verdict the LLM intended differently)
    * Whitespace around delimiters
    """
    if not stdout:
        return None
    matches = list(_BLOCK_RE.finditer(stdout))
    if not matches:
        return None
    # Last block wins — agents that print scratch output and then a
    # final conclusion shouldn't have an earlier draft picked up.
    inner = matches[-1].group(1)
    return _parse_inner(inner)


def _parse_inner(inner: str) -> TickConclusion:
    """Walk lines, accumulating ``summary:`` until the next key."""
    verdict: str | None = None
    summary_lines: list[str] = []
    files_raw: str | None = None

    current: str | None = None
    for line in inner.splitlines():
        m = _FIELD_RE.match(line.strip())
        if m is not None:
            current = m.group("key").lower()
            val = m.group("val").strip()
            if current == "verdict":
                v = val.lower()
                verdict = v if v in _VALID_VERDICTS else None
            elif current == "summary":
                if val:
                    summary_lines.append(val)
            elif current == "files":
                files_raw = val
            continue
        # Continuation line — only summary accumulates multi-line.
        if current == "summary" and line.strip():
            summary_lines.append(line.rstrip())

    summary: str | None = (
        "\n".join(summary_lines).rstrip() if summary_lines else None
    )

    files: list[str] = []
    if files_raw:
        for part in re.split(r"[,;]", files_raw):
            p = part.strip()
            if p:
                files.append(p)

    return TickConclusion(verdict=verdict, summary=summary, files=files)


__all__ = ["TickConclusion", "parse"]
