"""Grep syntax parser and matcher."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class GrepPattern:
    """Compiled grep pattern."""

    pattern: re.Pattern
    raw: str

    def matches(self, text: str) -> bool:
        return bool(self.pattern.search(text))


def parse_grep(query: str) -> GrepPattern:
    """Parse grep syntax into a compiled pattern.

    Syntax:
        wibble          Case-insensitive substring (default)
        /Wibble/        Case-sensitive substring
        /wib{2}le/i     Regex, case-insensitive
    """
    if not query:
        raise ValueError("Empty grep query")

    # /pattern/ or /pattern/i
    m = re.match(r"^/(.+)/([i]?)$", query)
    if m:
        raw_pattern = m.group(1)
        flags_str = m.group(2)
        flags = 0
        if "i" in flags_str:
            flags |= re.IGNORECASE
        try:
            compiled = re.compile(raw_pattern, flags)
        except re.error as e:
            raise ValueError(f"Invalid regex: {raw_pattern!r}: {e}") from e
        return GrepPattern(pattern=compiled, raw=query)

    # Plain substring — case-insensitive
    escaped = re.escape(query)
    compiled = re.compile(escaped, re.IGNORECASE)
    return GrepPattern(pattern=compiled, raw=query)
