"""Leak-gate: the public repo must carry NO cluster secrets.

The precis-mcp monorepo is **public**. Two different concerns live here, and
they have deliberately different scopes:

* **Cluster addresses, tree-wide.** Tailscale IPs, LAN IPs and vault blobs are
  topology: publishing them discloses the shape of the real cluster, and a
  public git push is forever (a later delete does not unpublish it). These are
  scanned across the **whole repo** — ``docs/``, ``scripts/``, ``src/``,
  ``tests/``, everything.
* **Real node hostnames, ``deploy/`` only.** This is an *architecture* check,
  not a disclosure one. ``deploy/`` ships as a portable, cluster-agnostic
  provisioning tree, so its roles must reference inventory groups and
  variables — a literal node name there means an un-parameterised role, which
  is the defect the migration exists to eliminate. Elsewhere the node names are
  load-bearing domain vocabulary (``llm_catalog``'s default serving host, the
  DB log handler, incident references in docstrings) across ~180 files;
  scrubbing those would rename the system's own vocabulary, not close a leak.

``deploy/inventory/`` is the LIVE per-cluster overlay (real ``hosts.yml``,
``group_vars/all/vault.yml`` …). It is **gitignored + local-only** and is
therefore skipped: it holds the operator's real secrets by design and is never
committed. The committed, scrubbed template shape lives in
``deploy/inventory.example/`` and IS scanned.

This runs inside the normal ``scripts/ship`` pytest gate, so a secret that
reaches any tracked file fails the ship before it can ever be pushed.
Filesystem walk (no ``git``) so it is robust in the container gate where
``.git`` may be absent.

**Legitimate uses exist** — the SSRF blocklist in ``utils/safe_fetch.py`` must
name RFC1918 and CGNAT *ranges*, and this file's own self-test must quote the
shapes it blocks. Those carry the ``secret-gate: allow`` marker with a reason.
Keep such uses vanishingly few: the marker is for naming a *range* or a
*sample*, never a real host address.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEPLOY = _REPO_ROOT / "deploy"

# Directory names skipped anywhere in the walk. ``inventory`` is the live
# overlay (gitignored, real secrets) — never scan it. ``worktrees`` holds full
# sibling checkouts of this same repo in the main clone: scanning them would be
# quadratic and would report another branch's lines as this tree's failures.
# The rest is build/cache noise.
_SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "collections",
    "dist",
    "htmlcov",
    "inventory",
    "node_modules",
    "venv",
    "worktrees",
}

# A line carrying this marker is exempt (rare, e.g. a comment that must name a
# forbidden token to explain the rule). Keep uses vanishingly few.
_ALLOW_MARKER = "secret-gate: allow"

# ── Tree-wide: cluster addresses. Disclosure, scanned everywhere. ────────────
_FORBIDDEN_ANYWHERE: list[tuple[str, re.Pattern[str]]] = [
    # An ansible-vault-encrypted blob (or a pasted one). The header is unique.
    (
        "ansible-vault blob",
        re.compile(r"\$ANSIBLE_VAULT"),  # secret-gate: allow — pattern
    ),
    # Tailscale CGNAT — the tailnet addresses of the nodes.
    (
        "tailscale ip (CGNAT)",
        re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
    ),
    # The cluster LAN (used for NFS). Real private addresses of the boxes.
    ("lan ip (192.168.x)", re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b")),
]

# ── deploy/ only: real node hostnames. Parameterisation, not disclosure. ─────
_FORBIDDEN_IN_DEPLOY: list[tuple[str, re.Pattern[str]]] = [
    (
        "real hostname / tailnet",
        re.compile(
            r"\b(?:melchior|caspar|balthazar|spark|hephaestus|finnmaccool|aidev)\b",
            re.IGNORECASE,
        ),
    ),
]

# Binary / non-text suffixes we don't scan.
_BINARY_SUFFIXES = {
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


def _scannable_files(root: Path) -> list[Path]:
    """Every text file under ``root``, minus skipped dirs and binaries."""
    if not root.is_dir():
        return []
    out: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() in _BINARY_SUFFIXES:
            continue
        out.append(path)
    return out


def _scan(root: Path, patterns: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    hits: list[str] = []
    for path in _scannable_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # not text we can meaningfully scan
        rel = path.relative_to(_REPO_ROOT)
        lines = text.splitlines()
        for lineno, line in enumerate(lines, start=1):
            # The marker exempts its own line *or* the line right after it.
            # ``ruff format`` reflows a long call across lines and carries the
            # trailing comment to the closing paren, stranding the literal on a
            # bare line — so an on-line-only rule turns a formatting pass into a
            # confusing red. Accepting the preceding line covers both shapes.
            if _ALLOW_MARKER in line or (
                lineno >= 2 and _ALLOW_MARKER in lines[lineno - 2]
            ):
                continue
            for label, pat in patterns:
                if pat.search(line):
                    hits.append(f"{rel}:{lineno}: {label} → {line.strip()[:100]}")
    return hits


def test_repo_carries_no_cluster_addresses() -> None:
    """No tailnet IP, LAN IP or vault blob anywhere in the public tree."""
    hits = _scan(_REPO_ROOT, _FORBIDDEN_ANYWHERE)
    assert not hits, (
        "cluster address(es) found in the public repo — parameterise them, or "
        f"mark a genuine range/sample with '{_ALLOW_MARKER} — <reason>':\n"
        + "\n".join(hits)
    )


def test_deploy_tree_is_cluster_agnostic() -> None:
    """No literal node hostname under ``deploy/`` — roles use inventory vars."""
    hits = _scan(_DEPLOY, _FORBIDDEN_IN_DEPLOY)
    assert not hits, (
        "real hostname(s) in the portable deploy/ tree — reference an inventory "
        "group or variable instead:\n" + "\n".join(hits)
    )


def test_gate_patterns_actually_match() -> None:
    """Self-check: the regexes catch the real shapes they are meant to block
    (a green gate on an empty tree must not mean a broken gate)."""
    samples = {
        # secret-gate: allow — samples naming the blocked shapes, not real hosts
        "ansible-vault blob": "$ANSIBLE_VAULT;1.1;AES256",  # secret-gate: allow
        "tailscale ip (CGNAT)": "ansible_host: 100.126.0.1",  # secret-gate: allow
        "lan ip (192.168.x)": "lan_ip: 192.168.0.1",  # secret-gate: allow
        "real hostname / tailnet": "when: inventory_hostname == 'melchior'",
    }
    by_label = {label: pat for label, pat in _FORBIDDEN_ANYWHERE}
    by_label.update({label: pat for label, pat in _FORBIDDEN_IN_DEPLOY})
    for label, sample in samples.items():
        assert by_label[label].search(sample), f"{label} regex failed to match sample"
    # And a documentation IP (RFC 5737) or placeholder must NOT trip the gate.
    all_pats = _FORBIDDEN_ANYWHERE + _FORBIDDEN_IN_DEPLOY
    for benign in ("ansible_host: 203.0.113.10", "host: node-gateway", "100.5.4.3"):
        assert not any(pat.search(benign) for _, pat in all_pats), (
            f"gate false-positived on benign sample: {benign}"
        )


def test_allow_marker_is_not_overused() -> None:
    """The exemption must stay rare — an unbounded allowlist is no gate.

    Every marked line is a place a human decided a forbidden shape is genuinely
    required (an SSRF range blocklist, a self-test sample). If this count grows,
    the marker is being used to silence the gate rather than to document an
    exception.
    """
    marked = [
        f"{path.relative_to(_REPO_ROOT)}:{lineno}"
        for path in _scannable_files(_REPO_ROOT)
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1
        )
        if _ALLOW_MARKER in line
    ]
    assert len(marked) <= 20, (
        f"{len(marked)} lines carry '{_ALLOW_MARKER}' — the exemption is meant "
        "to be vanishingly rare. Parameterise instead:\n" + "\n".join(marked)
    )


@pytest.mark.skipif(not _DEPLOY.is_dir(), reason="deploy/ tree not present yet")
def test_deploy_tree_has_scannable_content() -> None:
    """Once ``deploy/`` exists it must have scannable files — a silently empty
    walk would make the leak-gate vacuously green."""
    assert _scannable_files(_DEPLOY), "deploy/ exists but nothing scannable was found"


def test_repo_walk_is_not_vacuous() -> None:
    """Same guard for the tree-wide walk: it must actually reach source files."""
    found = _scannable_files(_REPO_ROOT)
    assert len(found) > 100, f"tree-wide walk found only {len(found)} files"
    assert any(p.name == "safe_fetch.py" for p in found), "walk missed src/"
