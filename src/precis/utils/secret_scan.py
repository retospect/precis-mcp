"""Pre-write secret gate for agent-written text.

Memory (and any other agent-authored prose) is stored, embedded, mirrored to
disk and recalled into other sessions' context, so a pasted credential would
spread to every one of those. :func:`find_secrets` flags credential shapes;
:func:`refuse_secrets` is the write-path gate that turns a finding into a
:class:`~precis.errors.BadInput` before anything is written.

Credentials only: hostnames and LAN/Tailscale addresses are allowed here
(memory is private; the tree-wide address gate is a different concern). The
patterns are deliberately conservative -- a false positive is fixed by
tightening the pattern here, there is no per-write escape hatch. Findings
carry a masked excerpt (the first four characters of the match plus ``…``);
the full match is never logged, echoed or stored.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import overload

from precis.errors import BadInput

_log = logging.getLogger(__name__)

FIX = (
    "store the secret in the vault and reference it by name "
    "(e.g. `~/.secrets/pw/<NAME>`)"
)


@dataclass(frozen=True)
class SecretHit:
    line: int  # 1-based
    kind: str
    excerpt_masked: str  # first 4 chars of the match + "…"


_SPECIFIC: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic key/token", re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")),
    ("openai-style key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("github fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("aws access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "aws secret key",
        re.compile(
            r"(?i)aws_?secret_?(?:access_?)?key[\"']?\s*[=:]\s*[\"']?[A-Za-z0-9/+=]{40}\b"
        ),
    ),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    (
        "private key block",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"),
    ),
    (
        "bearer token",
        re.compile(
            r"(?i)authorization[\"']?\s*[:=]\s*[\"']?bearer\s+[A-Za-z0-9._~+/=\-]{20,}"
        ),
    ),
]

# scheme://user:password@host  (a bare user@host has no ``:password`` part)
_URL_PW = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:([^\s@/]+)@")
# password = literal / PASSWORD: literal
_PW_ASSIGN = re.compile(
    r"(?i)\b(?:password|passwd)[\"']?\s*[=:]\s*[\"']?([^\s\"'`,;)}\]]+)"
)
_GENERIC = re.compile(r"[A-Za-z0-9+/_\-]{32,}")

_PLACEHOLDER_START = ("$", "<", "{", "*", "%", "~", "[", "(", "…", "/", ".", "@")
_PLACEHOLDER_WORDS = re.compile(
    r"(?i)^(?:redacted|removed|hidden|masked|secret|password|passwd|pass|none|null|"
    r"nil|true|false|empty|unset|changeme|example|your[-_a-z]*|x{3,}|\.{3,}|\*+)$"
)
_HEX = re.compile(r"^[0-9a-fA-F]+$")
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_ENTROPY_MIN = 4.0


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _is_placeholder(value: str) -> bool:
    return value.startswith(_PLACEHOLDER_START) or bool(_PLACEHOLDER_WORDS.match(value))


def _password_literal(value: str) -> bool:
    """A password-assignment value that reads as a real literal, not prose or a
    placeholder: 8+ chars and not a plain lowercase word."""
    value = value.rstrip(".")
    if len(value) < 8 or _is_placeholder(value):
        return False
    return not value.isalpha() or not value.islower()


def _generic_candidate(tok: str) -> bool:
    if _UUID.match(tok) or _HEX.match(tok):  # git sha, uuid, sha256/md5 digest
        return False
    if not (
        len(re.findall(r"[0-9]", tok)) >= 2
        and re.search(r"[a-z]", tok)
        and re.search(r"[A-Z]", tok)
    ):
        return False
    # identifiers/paths: several short delimiter-separated pieces
    parts = [p for p in re.split(r"[_\-/]", tok) if p]
    if len(parts) >= 3 and all(len(p) <= 12 for p in parts):
        return False
    return _entropy(tok) >= _ENTROPY_MIN


def _mask(match: str) -> str:
    return match[:4] + "…"


def _scan_line(line: str) -> list[tuple[str, int, int]]:
    """``(kind, start, end)`` of every credential-shaped span in one line."""
    out: list[tuple[str, int, int]] = []

    def add(kind: str, m: re.Match[str]) -> None:
        out.append((kind, m.start(), m.end()))

    for kind, pat in _SPECIFIC:
        for m in pat.finditer(line):
            add(kind, m)
    for m in _URL_PW.finditer(line):
        if not _is_placeholder(m.group(1)):
            add("url with inline password", m)
    for m in _PW_ASSIGN.finditer(line):
        if _password_literal(m.group(1)):
            add("password literal", m)
    for m in _GENERIC.finditer(line):
        s, e = m.span()
        if any(s < be and bs < e for _k, bs, be in out):
            continue
        if re.search(r"(?i)\b(?:SHA256|MD5):$", line[:s]):
            continue  # ssh key fingerprint (public)
        if _generic_candidate(m.group(0)):
            add("high-entropy token", m)
    return out


def find_secrets(text: str) -> list[SecretHit]:
    """Credential-shaped substrings of ``text`` -- ``[]`` when clean.

    One finding per distinct (line, span); a generic high-entropy hit inside a
    span a specific pattern already flagged is not reported twice.
    """
    return [
        SecretHit(lineno, kind, _mask(line[s:e]))
        for lineno, line in enumerate(text.splitlines(), start=1)
        for kind, s, e in _scan_line(line)
    ]


@overload
def mask_secrets(text: str) -> str: ...
@overload
def mask_secrets(text: None) -> None: ...
def mask_secrets(text: str | None) -> str | None:
    """``text`` with every credential-shaped span replaced by
    ``<redacted:kind>``; ``None``/clean text come back unchanged.

    For automated writers (alert / forensics / importer / UI paths) that
    cannot rephrase: a refusal there would silently lose the record, so the
    record is kept with the secret removed. Agent-facing verb calls refuse
    instead (:func:`refuse_secrets`, applied at the dispatch boundary).
    """
    if not text:
        return text
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        tail = line[len(body) :]
        spans = sorted(_scan_line(body), key=lambda t: t[1])
        pos, pieces = 0, []
        for kind, st, en in spans:
            if st < pos:  # overlaps an earlier span
                continue
            # Kind only, never the match: a false positive here alters the
            # stored text with no refusal to tell anyone, so leave a trace.
            _log.warning("secret_scan: masked a %s in an automated write", kind)
            pieces.append(body[pos:st])
            pieces.append(f"<redacted:{kind}>")
            pos = en
        pieces.append(body[pos:])
        out.append("".join(pieces) + tail)
    return "".join(out)


def refuse_secrets(fields: Iterable[tuple[str, str | None]]) -> None:
    """Raise :class:`BadInput` if any ``(label, text)`` field holds a credential.

    The message names the field, line, kind and masked excerpt of up to three
    findings -- never the full match.
    """
    hits: list[str] = []
    total = 0
    for label, text in fields:
        if not text:
            continue
        for f in find_secrets(text):
            total += 1
            if len(hits) < 3:
                hits.append(f"{label} line {f.line}: {f.kind} ({f.excerpt_masked})")
    if not total:
        return
    more = f" (+{total - len(hits)} more)" if total > len(hits) else ""
    raise BadInput(
        "refusing to store what looks like a credential -- " + "; ".join(hits) + more,
        next=FIX,
    )
