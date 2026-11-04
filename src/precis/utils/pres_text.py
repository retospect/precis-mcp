"""Remove LaTeXiT's embedded payloads without cleaning surrounding slide prose.

Shared by PDF extraction and reads: existing body chunks are append-only,
so render-time filtering keeps old decks usable without rewriting storage.
"""

import re

_LATEXIT_RE = re.compile(r"<latexit(?=\s|>)[^>]*>.*?</latexit\s*>", re.DOTALL)


def strip_latexit(text: str) -> str:
    """Remove complete LaTeXiT elements only, including multiline payloads."""
    return _LATEXIT_RE.sub("", text)
