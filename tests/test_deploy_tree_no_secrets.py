"""Leak-gate: the public ``deploy/`` ansible tree must carry NO cluster secrets.

The precis-mcp monorepo is **public**. The portable provisioning tree under
``deploy/`` ships with it, but the real cluster's inventory — Tailscale IPs,
LAN IPs, node hostnames, the encrypted vault — must NEVER be committed into it
(an irreversible leak: a public git push is forever, even after a later delete).

The boundary (design-of-record §15c / §16, ``docs/design/factory-console-and-
scheduling.md``):

* ``deploy/`` — portable roles + playbooks + example overlay. Scanned here.
  Roles reference capabilities and inventory *variables*, never literal node
  names or addresses, so this tree is cluster-agnostic and safe to publish.
* ``deploy/inventory/`` — the LIVE per-cluster overlay (real ``hosts.yml``,
  ``group_vars/all/vault.yml`` …). It is **gitignored + local-only** and is
  therefore *skipped* by this gate (it holds the operator's real secrets by
  design; it is never committed). The committed, scrubbed template shape lives
  in ``deploy/inventory.example/`` and IS scanned.

This runs inside the normal ``scripts/ship`` pytest gate, so a secret that
reaches ``deploy/`` fails the ship before it can ever be pushed. Filesystem
walk (no ``git``) so it is robust in the container gate where ``.git`` may be
absent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEPLOY = _REPO_ROOT / "deploy"

# Directory names to skip while walking ``deploy/``. ``inventory`` is the live
# overlay (gitignored, real secrets) — never scan it. The rest are noise.
_SKIP_DIRS = {"inventory", ".git", "__pycache__", "collections", ".venv"}

# A line carrying this marker is exempt (rare, e.g. a comment that must name a
# forbidden token to explain the rule). Keep uses vanishingly few.
_ALLOW_MARKER = "secret-gate: allow"

# ── Forbidden patterns — each would leak the real cluster ────────────────────
_FORBIDDEN: list[tuple[str, re.Pattern[str]]] = [
    # An ansible-vault-encrypted blob (or a pasted one). The header is unique.
    ("ansible-vault blob", re.compile(r"\$ANSIBLE_VAULT")),
    # Tailscale CGNAT range 100.64.0.0/10 — the tailnet addresses of the nodes.
    (
        "tailscale ip (100.64.0.0/10)",
        re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
    ),
    # The cluster LAN (used for NFS). Real private addresses of the boxes.
    ("lan ip (192.168.x)", re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b")),
    # Real node hostnames + NAS + tailnet name. Portable roles must reference
    # inventory groups/vars instead, so a literal here means an un-parameterised
    # role — the thing the migration is meant to eliminate.
    (
        "real hostname / tailnet",
        re.compile(
            r"\b(?:melchior|caspar|balthazar|spark|hephaestus|finnmaccool|aidev)\b",
            re.IGNORECASE,
        ),
    ),
]

# Binary / non-text suffixes we don't scan.
_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".gz", ".zip"}


def _scannable_files() -> list[Path]:
    if not _DEPLOY.is_dir():
        return []
    out: list[Path] = []
    for path in _DEPLOY.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(_DEPLOY).parts):
            continue
        if path.suffix.lower() in _BINARY_SUFFIXES:
            continue
        out.append(path)
    return out


def test_deploy_tree_carries_no_cluster_secrets() -> None:
    """Every scannable file under ``deploy/`` (minus the live overlay) is clean."""
    hits: list[str] = []
    for path in _scannable_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # not text we can meaningfully scan
        rel = path.relative_to(_REPO_ROOT)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _ALLOW_MARKER in line:
                continue
            for label, pat in _FORBIDDEN:
                if pat.search(line):
                    hits.append(f"{rel}:{lineno}: {label} → {line.strip()[:100]}")
    assert not hits, (
        "cluster secret(s) found in the public deploy/ tree:\n" + "\n".join(hits)
    )


def test_gate_patterns_actually_match() -> None:
    """Self-check: the regexes catch the real shapes they are meant to block
    (a green gate on an empty tree must not mean a broken gate)."""
    samples = {
        "ansible-vault blob": "$ANSIBLE_VAULT;1.1;AES256",
        "tailscale ip (100.64.0.0/10)": "ansible_host: 100.126.127.107",
        "lan ip (192.168.x)": "lan_ip: 192.168.6.8",
        "real hostname / tailnet": "when: inventory_hostname == 'melchior'",
    }
    by_label = {label: pat for label, pat in _FORBIDDEN}
    for label, sample in samples.items():
        assert by_label[label].search(sample), f"{label} regex failed to match sample"
    # And a documentation IP (RFC 5737) or placeholder must NOT trip the gate.
    for benign in ("ansible_host: 203.0.113.10", "host: node-gateway", "100.5.4.3"):
        assert not any(pat.search(benign) for _, pat in _FORBIDDEN), (
            f"gate false-positived on benign sample: {benign}"
        )


@pytest.mark.skipif(not _DEPLOY.is_dir(), reason="deploy/ tree not present yet")
def test_deploy_tree_has_scannable_content() -> None:
    """Once ``deploy/`` exists it must have scannable files — a silently empty
    walk would make the leak-gate vacuously green."""
    assert _scannable_files(), "deploy/ exists but nothing scannable was found"
