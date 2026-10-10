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

* **Credential shapes, tree-wide.** API keys, tokens, private-key blocks, URLs
  with inline passwords and high-entropy strings, found by
  :func:`precis.utils.secret_scan.find_secrets` -- the single pattern source,
  shared with the agent-write gate. Findings are reported as ``path:line`` plus
  kind only; the match is never printed. Generated / vendored data and the
  explicit :data:`_CREDENTIAL_ALLOWLIST` (generated / template / constant files;
  never tests -- fakes in tests are built from fragments) are exempt; a stale allowlist entry fails.

An ``OSError`` during the walk or a read (ENFILE on a shared VM, 2026-09-29)
surfaces as ``ScanIncomplete`` — never as a violation, never as a pass over a
partial tree. See ``tests/_policy_scan.py``.
"""

from __future__ import annotations

import errno
import re
from pathlib import Path
from typing import Any

import pytest

from tests._policy_scan import ScanIncomplete, read_text, walk_files

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


# ── Tree-wide: credential shapes (patterns live in utils/secret_scan). ───────
# Path prefixes / suffixes of generated or vendored data where high-entropy
# strings are content (base64 blobs, lockfile hashes, minified JS, svg paths).
_CREDENTIAL_SKIP_PREFIXES = (
    "src/precis_web/static/",
    "src/precis/migrations/",
    "src/precis/thermo/",
    "tests/fixtures/",
    "guide/assets/",
)
_CREDENTIAL_SKIP_SUFFIXES = (".svg", ".lock", ".min.js", ".mjs")

#: Non-test files that legitimately hold credential-shaped strings. path -> why.
#: No test file may be listed: a test's fake credential is assembled from
#: fragments at runtime (``"gh" + "p_" + ...``), so a real token pasted into a
#: test later is still caught. ``test_credential_allowlist_has_no_stale_entries``
#: fails an entry that no longer hits.
_TEMPLATE = "committed example template naming the shape a real value takes"
_CREDENTIAL_ALLOWLIST: dict[str, str] = {
    "deploy/inventory.example/group_vars/all/vault.yml.example": _TEMPLATE,
    "paper-extraction-pilot/evaluate.py": "alphabet constant, not a secret",
    "src/precis/handlers/draft.py": "base64 PNG icon constant",
    "src/precis/utils/fractional.py": "base-62 digit alphabet",
    "src/precis/utils/handles.py": "handle alphabet",
}


def _scannable_files(root: Path) -> list[Path]:
    """Every text file under ``root``, minus skipped dirs and binaries."""
    return [
        path
        for path in walk_files(root, skip_dirs=_SKIP_DIRS)
        if path.suffix.lower() not in _BINARY_SUFFIXES
    ]


def _scan(root: Path, patterns: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    hits: list[str] = []
    for path in _scannable_files(root):
        try:
            text = read_text(path)
        except UnicodeDecodeError:
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


def _credential_findings(root: Path) -> dict[str, list[str]]:
    """``{relpath: ["line N: kind (xxxx…)", ...]}`` for every file with a
    credential shape. Only the masked 4-char excerpt is ever recorded."""
    from precis.utils.secret_scan import find_secrets

    out: dict[str, list[str]] = {}
    for path in _scannable_files(root):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(_CREDENTIAL_SKIP_PREFIXES) or rel.endswith(
            _CREDENTIAL_SKIP_SUFFIXES
        ):
            continue
        try:
            text = read_text(path)
        except UnicodeDecodeError:
            continue
        lines = text.splitlines()
        for f in find_secrets(text):
            here = lines[f.line - 1]
            prev = lines[f.line - 2] if f.line >= 2 else ""
            if _ALLOW_MARKER in here or _ALLOW_MARKER in prev:
                continue
            out.setdefault(rel, []).append(
                f"line {f.line}: {f.kind} ({f.excerpt_masked})"
            )
    return out


def test_repo_carries_no_credentials() -> None:
    """No API key / token / private key / inline-password URL in the tree.

    The patterns are :func:`precis.utils.secret_scan.find_secrets`'s. A fake
    credential that a test must hold either gets its file listed in
    :data:`_CREDENTIAL_ALLOWLIST` (with a reason) or is assembled at runtime
    so the source carries no literal."""
    found = _credential_findings(_REPO_ROOT)
    bad = {rel: v for rel, v in found.items() if rel not in _CREDENTIAL_ALLOWLIST}
    assert not bad, (
        "credential-shaped string(s) in the public repo (matches not shown) -- "
        "move the secret to the vault; for a deliberate fake, build it at "
        "runtime or add the file to _CREDENTIAL_ALLOWLIST with a reason:\n"
        + "\n".join(f"{rel}: {'; '.join(v[:3])}" for rel, v in sorted(bad.items()))
    )


def test_credential_allowlist_has_no_stale_entries() -> None:
    found = _credential_findings(_REPO_ROOT)
    stale = [
        rel
        for rel in _CREDENTIAL_ALLOWLIST
        if rel not in found or not (_REPO_ROOT / rel).is_file()
    ]
    assert not stale, f"allowlisted files with no credential shape left: {stale}"


def test_credential_gate_flags_a_planted_secret_without_echoing_it(
    tmp_path: Path,
) -> None:
    secret = "gh" + "p_" + ("aB3dE5fG7hJ9kL1mN3pQ" + "5rS7tU9vW1xY3zA5")
    (tmp_path / "notes.md").write_text(f"token {secret}\n", encoding="utf-8")
    (tmp_path / "ok.md").write_text("nothing here\n", encoding="utf-8")
    found = _credential_findings(tmp_path)
    assert set(found) == {"notes.md"}
    assert secret not in str(found)
    # the marker convention exempts a line, as for the address checks
    (tmp_path / "notes.md").write_text(
        f"token {secret}  # {_ALLOW_MARKER} - fake\n", encoding="utf-8"
    )
    assert _credential_findings(tmp_path) == {}


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
            read_text(path, errors="ignore").splitlines(), start=1
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


_ENFILE = OSError(errno.ENFILE, "Too many open files in system")


def test_oserror_mid_walk_is_an_incomplete_scan_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-09-29 shape: ``is_file()`` dies with ENFILE partway through."""
    real_is_file, seen = Path.is_file, 0

    def flaky(self: Path) -> bool:
        nonlocal seen
        seen += 1
        if seen == 3:
            raise _ENFILE
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", flaky)
    with pytest.raises(ScanIncomplete) as info:
        _scan(_REPO_ROOT, _FORBIDDEN_ANYWHERE)
    msg = str(info.value)
    assert "ABORTED" in msg and "ENFILE" in msg and "NOT a verdict" in msg
    assert "cluster address" not in msg


def test_oserror_mid_read_is_an_incomplete_scan_not_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read that fails must not be skipped: a partial scan is no scan."""
    real_read, seen = Path.read_text, 0

    def flaky(self: Path, *a: Any, **kw: Any) -> str:
        nonlocal seen
        seen += 1
        if seen == 3:
            raise _ENFILE
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", flaky)
    with pytest.raises(ScanIncomplete, match="ENFILE"):
        _scan(_REPO_ROOT, _FORBIDDEN_ANYWHERE)
    seen = 0
    with pytest.raises(ScanIncomplete, match="ENFILE"):
        test_allow_marker_is_not_overused()
    # A stray ``except OSError`` must not swallow it; a summary line must not
    # read as an assertion about the tree.
    assert not issubclass(ScanIncomplete, (OSError, AssertionError))


def test_a_real_hit_still_fails_as_a_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The violation path is untouched: a plain ``AssertionError`` naming it."""
    tailnet = ".".join(("100", "100", "7", "7"))  # assembled, so this file stays clean
    (tmp_path / "hosts.yml").write_text(f"ansible_host: {tailnet}\n", encoding="utf-8")
    monkeypatch.setattr(f"{__name__}._REPO_ROOT", tmp_path)
    with pytest.raises(AssertionError, match=r"cluster address\(es\) found") as info:
        test_repo_carries_no_cluster_addresses()
    assert f"hosts.yml:1: tailscale ip (CGNAT) → ansible_host: {tailnet}" in str(
        info.value
    )
