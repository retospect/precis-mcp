"""Tier-0 prompt-injection scan — free, inline regex (email-kind slice 3).

The cheap first rung of the cascade from ``docs/design/email-kind.md``: loud,
attacker-tell patterns in an email body that reaches an LLM holding *other*
tools. It is deliberately **coarse** — it only ever says ``clean`` (no signal)
or ``suspect`` (something fired). A confident ``high`` verdict is the job of the
slice-4 ``inject_scan`` local model; tier-0 just decides who it looks at.

**The scan is a signal, not the protection.** Every email body is delimited as
untrusted data whenever it reaches an LLM regardless of verdict (the
``safe_fetch``/SSRF analogue for text). A false negative here does not let the
body issue instructions; it only means the message wasn't flagged for a human /
deeper look. So this errs toward recall — false positives are expected (any
newsletter *about* prompt injection trips these) and are why the verdict is
merely ``suspect`` and the evidence is recorded per-signal, so it stays tunable.

Pure: no IMAP, no DB. ``mail_poll`` runs it inline and persists the result to
``email_scan``; tests exercise it directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Bump when the signal set changes — persisted in ``evidence.version`` so a
#: later pass can tell a stale verdict from a current one and re-scan (the
#: CLASSIFY_VERSION discipline of the ADR 0047 cascade).
TIER0_VERSION = 1

#: Unicode code points that hide or obfuscate text (zero-width joiners, BOM,
#: word-joiner, LTR/RTL overrides). Legitimate mail almost never carries them;
#: injection payloads use them to smuggle instructions past a human skim.
_HIDDEN_CHARS = (
    "​"  # zero-width space
    "‌"  # zero-width non-joiner
    "‍"  # zero-width joiner
    "⁠"  # word joiner
    "﻿"  # BOM / zero-width no-break space
    "‪‫‬‭‮"  # bidi embedding / override
)
_HIDDEN_RE = re.compile(f"[{_HIDDEN_CHARS}]")

#: ``(signal_name, pattern)`` — each a distinct tell so the evidence names
#: *which* fired (tunable per false-positive). Case-insensitive, dotall off.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore-previous",
        re.compile(
            r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}"
            r"\b(?:previous|prior|above|earlier|all)\b[^.\n]{0,20}"
            r"\b(?:instruction|prompt|direction|context|rule)",
            re.IGNORECASE,
        ),
    ),
    (
        "role-reassign",
        re.compile(
            r"\byou\s+are\s+now\s+(?:a\b|an\b|the\b|going\s+to\b|in\b|"
            r"dan\b|no\s+longer\b)"
            r"|\bpretend\s+(?:to\s+be|you\s+are)\b"
            r"|\bact\s+as\s+(?:a\b|an\b|the\b|if\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "system-prompt-framing",
        re.compile(
            r"</?\s*system\s*>|\[/?\s*system\s*\]|\bsystem\s+prompt\b"
            r"|\bnew\s+(?:instruction|task|directive|system)s?\s*:",
            re.IGNORECASE,
        ),
    ),
    (
        "tool-command-framing",
        re.compile(
            r"\b(?:run|execute|invoke|call)\b[^.\n]{0,30}"
            r"\b(?:the\s+following|this)\b[^.\n]{0,20}"
            r"\b(?:command|code|tool|script|function)"
            r"|\bcurl\s+https?://",
            re.IGNORECASE,
        ),
    ),
    (
        "exfil-instruction",
        re.compile(
            r"\b(?:send|forward|email|post|upload|leak)\b[^.\n]{0,40}"
            r"\b(?:password|secret|api[\s_-]?key|token|credential|"
            r"private\s+key|env(?:ironment)?\s+var)",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class Tier0Result:
    """Outcome of a tier-0 scan: a coarse verdict plus its evidence."""

    verdict: str  # "clean" | "suspect"
    signals: tuple[str, ...]  # which named patterns fired (sorted, deduped)
    version: int = TIER0_VERSION

    @property
    def evidence(self) -> dict[str, object]:
        """The JSONB payload persisted to ``email_scan.evidence``."""
        return {"signals": list(self.signals), "version": self.version}


def scan_tier0(subject: str, body: str) -> Tier0Result:
    """Regex-scan a message's subject + body for loud injection tells.

    Returns ``suspect`` (with the named signals) if anything fired, else
    ``clean``. Coarse by design — see the module docstring.
    """
    text = f"{subject}\n{body}"
    fired: set[str] = {name for name, pat in _PATTERNS if pat.search(text)}
    if _HIDDEN_RE.search(text):
        fired.add("hidden-unicode")
    signals = tuple(sorted(fired))
    return Tier0Result(verdict="suspect" if signals else "clean", signals=signals)


__all__ = ["TIER0_VERSION", "Tier0Result", "scan_tier0"]
