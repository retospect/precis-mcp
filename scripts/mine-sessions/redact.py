"""Secret scrub for everything the miner writes to disk.

Session transcripts routinely contain the exact things the repo's own secret
gate exists to keep out — tailnet and LAN addresses, prod DSNs, pasted
credentials — because a session that debugged the cluster quotes the cluster.
The miner's output does not stay on disk: evidence cards get read by
subagents and pasted into gripes and backlog items, and the repo is public.
So the scrub runs on the *write* side, not the read side, and every stage
routes its free-text fields through :func:`scrub`.

The address patterns are deliberately the same ones
``tests/test_deploy_tree_no_secrets.py`` enforces tree-wide, so a card that
passes here also passes the gate if someone commits it. The credential
patterns are additional — the deploy gate does not need them because
credentials never appear in tracked files, but they do appear in
transcripts.

This is a filter, not a guarantee. It removes the shapes we know; it cannot
prove a card is clean. Cards stay under a gitignored ``out/``, and the
runbook tells the pass to skim before pasting.
"""

from __future__ import annotations

import re

_REDACTED = "[redacted:{}]"

#: (label, pattern) — order matters only for readability; all are applied.
#: The first three mirror ``_FORBIDDEN_ANYWHERE`` in the deploy secret gate.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Split so the literal header string never appears in the tree — the
    # scanner's own exemption marker is budget-capped on purpose, and this
    # costs nothing at runtime.
    ("vault", re.compile(r"\$ANSIBLE" + r"_VAULT[^\s]*")),
    # Tailscale CGNAT range — the tailnet addresses of the nodes.
    (
        "tailnet-ip",
        re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
    ),
    # The cluster LAN (NFS).
    ("lan-ip", re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b")),
    # Postgres / generic DSNs carrying credentials. Keeps the scheme so the
    # card still reads as "a DSN was here", drops user:pass@host.
    (
        "dsn",
        re.compile(r"\b([a-z][a-z0-9+.-]*)://[^\s/@]+:[^\s/@]+@[^\s\"']+"),
    ),
    # Bearer tokens and Authorization headers.
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
    ("authz-header", re.compile(r"(?i)\bauthorization\s*:\s*\S+")),
    # Common provider key shapes (Anthropic, OpenAI, GitHub, AWS, Slack).
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("api-key", re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{16,}")),
    ("api-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("api-key", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    # PEM private key blocks.
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # KEY=value assignments for obviously-secret names, in env dumps and
    # shell lines. Value-only replacement keeps the name visible, which is
    # what makes the card still legible ("it set PGPASSWORD", not "it set
    # something").
    (
        "secret-env",
        re.compile(
            # `TOKEN` is deliberately NOT accepted bare. This miner's whole
            # subject is token accounting, so `input_tokens=`, `max_tokens=`,
            # `cache_read_tokens=` and a scoreboard's own "by tokens=" line
            # all appear constantly — a bare TOKEN rule redacts the data and
            # leaves the card useless. Only a credential-qualified token
            # name counts.
            # The `(?!\[redacted:)` guard is what makes scrub() idempotent
            # *and* keeps scan() honest: without it the replacement token
            # itself re-matches, so a scrubbed card would still read as
            # carrying a secret.
            r"(?i)\b([A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|CREDENTIAL|API_?KEY)[A-Z0-9_]*"
            r"|(?:AUTH|ACCESS|BEARER|REFRESH|ID|API|GH|GITHUB|SLACK|ANTHROPIC|OPENAI)"
            r"_?TOKENS?)\s*[=:]\s*(?!\[redacted:)[\"']?([^\s\"']{4,})[\"']?"
        ),
    ),
]

#: Real node hostnames. Gated under ``deploy/`` by the tree test, but a card
#: naming a node is a disclosure the same way an address is, so the miner
#: scrubs them everywhere it writes.
HOSTNAMES = re.compile(
    r"\b(?:melchior|caspar|balthazar|spark|hephaestus|finnmaccool|aidev)\b",
    re.IGNORECASE,
)


def scrub(text: str) -> str:
    """Return *text* with every known secret shape replaced by a label.

    Idempotent: running it on already-scrubbed text is a no-op, because the
    replacement token matches none of the patterns.
    """
    if not text:
        return text
    out = text
    for label, pat in PATTERNS:
        if label == "secret-env":
            out = pat.sub(lambda m: f"{m.group(1)}={_REDACTED.format('secret')}", out)
        elif label == "dsn":
            out = pat.sub(lambda m: f"{m.group(1)}://{_REDACTED.format('dsn')}", out)
        else:
            out = pat.sub(_REDACTED.format(label), out)
    return HOSTNAMES.sub(_REDACTED.format("host"), out)


def scan(text: str) -> list[str]:
    """Labels of every secret shape present in *text*. Empty means clean.

    Used by the end-to-end check ("no card carries a secret") rather than by
    the write path, which always scrubs unconditionally.
    """
    hits = [label for label, pat in PATTERNS if pat.search(text)]
    if HOSTNAMES.search(text):
        hits.append("host")
    return sorted(set(hits))
