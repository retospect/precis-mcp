"""Home-path gate: tracked docs name paths relative to this checkout.

A doc that says ``/Users/reto/catpath`` or ``~/work/projects/code/catpath``
bakes one machine's home layout into text every agent and checkout reads —
the same path is ``~/catpath`` on a cluster node and does not exist at all
elsewhere. The convention (Reto 2026-10-22): a **sibling repo is named
relative to this checkout** — ``../catpath``, ``../hexfold``,
``../../infrastructure`` — which resolves wherever the checkouts sit side by
side; a file inside this repo is ``./scripts/…``-relative. Repo-dev memory
nodes get the same rule via ``scripts/memory-lint`` check 11, which scans the
exported node bodies.

Flagged shapes:

* ``/Users/<name>/…`` and ``/home/<name>/…`` — an absolute home path.
* ``~/<name>/…`` where ``<name>`` is not dot-prefixed — a path inside a
  user's home *layout* (``~/work``, ``~/catpath``, ``~/precis-experiments``,
  ``~/Library``).
* ``~/.claude/projects/-<escaped>`` — Claude Code's escaped project dir
  encodes the absolute checkout path (``-Users-reto-precis-mcp`` is
  ``/Users/reto/precis-mcp``); docs write the ``<…>`` placeholder form.

**Not** flagged:

* ``~/.<tool>/…`` — a tool's canonical per-user dot location
  (``~/.ssh/config``, ``~/.cache/precis/…``, ``~/.pgpass``): portable
  notation that resolves correctly for any account on any machine.
* ``~<user>/…`` — explicit other-account home (``~deploy/.pgpass``):
  host-ops notation, likewise per-account portable.
* ``~20`` / ``~630/…`` — approximate quantities, not paths.
* ``../<repo>`` — the correct relative sibling form.

A genuine need to name an absolute path (a real host path a runbook must
cite, an API example showing absolute-path input) carries the
``home-path: allow — <reason>`` marker on the same line; the marker also
exempts the line right after it, matching the secret-gate convention.

Scanned roots are the tracked doc surfaces: ``docs/``, ``.claude/skills/``
and ``src/precis/data/skills/``. ``.claude/commands/`` and
``.claude/agents/`` are Reto-only surfaces (coordinator ruling) and stay
outside this gate. The hook-generated index views are skipped (gitignored,
regenerated from their sources).
"""

from __future__ import annotations

import re
from pathlib import Path

from tests._policy_scan import read_text, walk_files

_ROOT = Path(__file__).resolve().parent.parent

_SCAN_ROOTS = (
    "docs",
    ".claude/skills",
    "src/precis/data/skills",
)

#: Hook-generated views (gitignored, absent or stale in CI): findings here
#: would re-flag source text the generator copies verbatim.
_GENERATED = {
    "docs/backlog/INDEX.md",
    "docs/backlog/threads/PRIORITIES.md",
    "docs/codebase-map.md",
    "docs/runbooks/INDEX.md",
}

_ALLOW_MARKER = "home-path: allow"

# An absolute home path. The lookbehind keeps `foo/Users` (a path fragment,
# not a home root) out while deliberately allowing a second `/` before it —
# `/opt/Users/` and `file:///Users/` still name a home.
_ABS_HOME = re.compile(r"(?<![\w.~-])/(?:Users|home)/[\w.-]+")
# `~/` + a non-dot component, or the escaped project-dir form that encodes an
# absolute checkout. `~/.tool`, `~user/` and bare `~` quantities pass.
_TILDE_HOME = re.compile(r"(?<![\w/.~-])~/(?:\.claude/projects/-[\w-]+|[\w-][\w./~-]*)")

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

_PATTERNS = (_ABS_HOME, _TILDE_HOME)


def home_path_hits(text: str) -> list[str]:
    """Every flagged home-path token in ``text`` — markers not consulted."""
    return [
        m.group(0)
        for line in text.splitlines()
        for pat in _PATTERNS
        for m in pat.finditer(line)
    ]


def _scan() -> list[str]:
    """``relpath:line: token`` for every unmarked hit under the scan roots."""
    hits: list[str] = []
    for root in _SCAN_ROOTS:
        for path in walk_files(_ROOT / root):
            rel = path.relative_to(_ROOT).as_posix()
            if rel in _GENERATED or path.suffix.lower() in _BINARY_SUFFIXES:
                continue
            try:
                text = read_text(path)
            except UnicodeDecodeError:
                continue
            for lineno, token in _line_hits(text):
                hits.append(f"{rel}:{lineno}: {token}")
    return hits


def _line_hits(text: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    return [
        (lineno, m.group(0))
        for lineno, line in enumerate(lines, start=1)
        if _ALLOW_MARKER not in line
        and not (lineno >= 2 and _ALLOW_MARKER in lines[lineno - 2])
        for pat in _PATTERNS
        for m in pat.finditer(line)
    ]


def test_tracked_docs_carry_no_home_paths() -> None:
    hits = _scan()
    assert not hits, (
        "home path(s) in tracked docs — write a sibling repo relative to "
        "this checkout (../catpath), a repo file ./relative, or mark a "
        f"genuine absolute need '{_ALLOW_MARKER} — <reason>':\n" + "\n".join(hits)
    )


def test_home_path_patterns_match_and_exempt() -> None:
    """A hit is flagged; a relative sibling and per-account notations pass."""
    flagged = [
        "/Users/reto/catpath on the Mac",
        "checkout under /home/deploy/precis-mcp",
        "built in ~/catpath then shipped",
        "reads ~/work/infrastructure/compose.yaml",
        "log at ~/Library/Logs/precis-embedder.log",
        "seed ~/2026-09-13-report.md",
        "under ~/.claude/projects/-Users-reto-precis-mcp/scratch/",
    ]
    for line in flagged:
        assert home_path_hits(line), f"not flagged: {line}"
    passed = [
        "the sibling engine lives in ../catpath",
        "infra repo at ../../infrastructure/precis-mcp/",
        "key ~/.ssh/cluster and config ~/.codex/config.toml",
        "the deploy account's ~deploy/.pgpass (0600)",
        "roughly ~630 calls and ~0.3 of the budget",
        "logs to ~/.claude/projects/<project>/ — placeholder, not a host",
    ]
    for line in passed:
        assert not home_path_hits(line), f"false positive: {line}"


def test_a_marked_hit_passes() -> None:
    text = (
        "see /Users/hermes/.asa/asa-bot.log  <!-- home-path: allow — host path -->\n"
        "<!-- home-path: allow — real host path -->\n"
        "credential /Users/hermes/.claude/.credentials.json\n"
    )
    assert not _line_hits(text)


def test_scan_roots_are_not_vacuous() -> None:
    for root in _SCAN_ROOTS:
        files = [
            p
            for p in walk_files(_ROOT / root)
            if p.suffix.lower() not in _BINARY_SUFFIXES
        ]
        assert files, f"{root}/ has no scannable files — gate would be vacuous"
