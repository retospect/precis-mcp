"""Publication gate for generated artifacts: the repo secret scan, factored.

``tests/test_deploy_tree_no_secrets.py`` gates the tracked tree two ways —
cluster addresses everywhere, real node hostnames under ``deploy/`` — plus
credential shapes via :func:`precis.utils.secret_scan.find_secrets`. A
*published artifact* (the pathway repro bundle, any future export archive)
must pass the same shapes before it can leave the cluster: its staged files
are scanned with the identical patterns here, so "the archive gate" and "the
repo gate" can never drift apart.

Two publication-only additions that the repo gate does not need: the deploy
hostname pattern is an *architecture* check in the repo but a *disclosure*
check in an artifact that ships off-cluster, and absolute per-user paths
(``/Users/<name>``, ``/home/<name>``) leak the builder's account layout.
Both apply to staged artifacts only — never to the repo scan.

The ``secret-gate: allow`` marker convention is honoured exactly like the
repo gate: a marked line, or the line right after the marker, is exempt.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from precis.utils.secret_scan import find_secrets

#: The exemption marker, shared verbatim with the repo gate.
ALLOW_MARKER = "secret-gate: allow"

#: Cluster addresses — disclosure tree-wide in the repo AND in artifacts.
#: Same patterns as ``tests/test_deploy_tree_no_secrets.py``'s
#: ``_FORBIDDEN_ANYWHERE`` (which now imports them from here).
ADDRESS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "ansible-vault blob",
        re.compile(r"\$ANSIBLE_VAULT"),  # secret-gate: allow — pattern
    ),
    (
        "tailscale ip (CGNAT)",
        re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
    ),
    ("lan ip (192.168.x)", re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b")),
]

#: Real node hostnames — an architecture check under ``deploy/`` in the repo,
#: a disclosure check in anything published. Shared pattern source.
NODE_HOSTNAME_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "real hostname / tailnet",
        re.compile(
            r"\b(?:melchior|caspar|balthazar|spark|hephaestus|finnmaccool|aidev)\b",
            re.IGNORECASE,
        ),
    ),
]

#: Publication-only additions on top of the tree-wide address gate.
PUBLICATION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "absolute user path",
        re.compile(r"\b/(?:Users|home)/[A-Za-z0-9][A-Za-z0-9._-]*/"),
    ),
    *NODE_HOSTNAME_PATTERNS,
]

#: Suffixes treated as opaque bytes — never text-scanned (the repo gate's set).
BINARY_SUFFIXES = {
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".so",
    ".whl",
    ".zip",
}


def _marked(lines: list[str], lineno: int) -> bool:
    """The marker exempts its own line *or* the line right after it — the
    same two-line rule the repo scan applies (a reflowed literal can land
    on the bare line under the marker)."""
    return ALLOW_MARKER in lines[lineno - 1] or (
        lineno >= 2 and ALLOW_MARKER in lines[lineno - 2]
    )


def findings_in_text(
    text: str,
    *,
    extra_patterns: list[tuple[str, re.Pattern[str]]] | None = None,
) -> list[str]:
    """Credential + cluster-address findings in one text blob, formatted
    ``line N: kind (masked)`` — never echoing the match itself."""
    lines = text.splitlines()
    patterns = ADDRESS_PATTERNS + (extra_patterns or [])
    out: list[str] = []
    for lineno, line in enumerate(lines, start=1):
        if _marked(lines, lineno):
            continue
        for label, pat in patterns:
            if pat.search(line):
                out.append(f"line {lineno}: {label}")
    for hit in find_secrets(text):
        if _marked(lines, hit.line):
            continue
        out.append(f"line {hit.line}: {hit.kind} ({hit.excerpt_masked})")
    return out


def scan_public_files(
    files: Mapping[str, bytes],
    *,
    extra_patterns: list[tuple[str, re.Pattern[str]]] | None = None,
) -> dict[str, list[str]]:
    """Scan a staged ``{relpath: bytes}`` map for publication findings.

    Returns ``{relpath: [finding, ...]}`` for files with hits. Binary
    suffixes and undecodable bytes are skipped, like the repo gate.
    """
    extra = extra_patterns if extra_patterns is not None else PUBLICATION_PATTERNS
    out: dict[str, list[str]] = {}
    for rel, data in files.items():
        if Path(rel).suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        hits = findings_in_text(text, extra_patterns=extra)
        if hits:
            out[rel] = hits
    return out


def scan_public_tree(
    root: Path,
    *,
    extra_patterns: list[tuple[str, re.Pattern[str]]] | None = None,
) -> dict[str, list[str]]:
    """Walk a staged directory the same way — for callers that write files
    to disk before scanning. Symlinks are never followed into reads."""
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        files[path.relative_to(root).as_posix()] = path.read_bytes()
    return scan_public_files(files, extra_patterns=extra_patterns)


__all__ = [
    "ADDRESS_PATTERNS",
    "ALLOW_MARKER",
    "BINARY_SUFFIXES",
    "NODE_HOSTNAME_PATTERNS",
    "PUBLICATION_PATTERNS",
    "findings_in_text",
    "scan_public_files",
    "scan_public_tree",
]
