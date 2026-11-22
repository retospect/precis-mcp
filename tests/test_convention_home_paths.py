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
    "docs/repo-dev-start-page.md",
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


# ── plain-shell recipes (gr514953) ───────────────────────────────────────────
#
# AGENTS.md prefers one plain command per Bash call, and the harness guard
# refuses git inside a chain or ``$(…)``. The commands, skills and agents the
# repo ships must not prescribe the shapes the rule forbids, or agents learn
# the rule is optional. Only fenced code blocks are scanned — prose may name a
# forbidden shape in order to forbid it.

_RECIPE_GLOBS = (
    ".claude/commands/*.md",
    ".claude/skills/*/SKILL.md",
    ".claude/agents/*.md",
)
_FENCE_RE = re.compile(r"^\s*```")
_CMD = r"(?:scripts/|git\b|rtk git\b)"
_RECIPE_PATTERNS = {
    '; echo "': re.compile(r';\s*echo\s+"'),
    "&& between two commands": re.compile(_CMD + r"[^\n]*?\s&&\s+" + _CMD),
    "$(": re.compile(r"\$\("),
}
#: ``(relpath, token)`` -> why a fenced block may keep the shape. Keep short.
_RECIPE_ALLOW: dict[tuple[str, str], str] = {}


def _fenced_lines(text: str) -> list[tuple[int, str]]:
    """``(lineno, line)`` for every line inside a fenced code block, with a
    backslash-continued command joined onto its first line."""
    out: list[tuple[int, str]] = []
    inside = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(line):
            inside = not inside
            continue
        if not inside:
            continue
        if out and out[-1][1].endswith("\\") and out[-1][0] == lineno - 1:
            out[-1] = (lineno, out[-1][1][:-1] + " " + line.strip())
        else:
            out.append((lineno, line))
    return out


def _recipe_hits() -> tuple[list[str], int]:
    hits: list[str] = []
    scanned = 0
    for glob in _RECIPE_GLOBS:
        for path in sorted(_ROOT.glob(glob)):
            scanned += 1
            rel = path.relative_to(_ROOT).as_posix()
            for lineno, line in _fenced_lines(read_text(path)):
                for token, pat in _RECIPE_PATTERNS.items():
                    if pat.search(line) and (rel, token) not in _RECIPE_ALLOW:
                        hits.append(f"{rel}:{lineno}: {token}: {line.strip()}")
    return hits, scanned


def test_commands_and_agents_prescribe_plain_shell() -> None:
    hits, scanned = _recipe_hits()
    assert scanned > 10, f"recipe globs matched only {scanned} files — vacuous"
    assert not hits, (
        "fenced recipe blocks prescribe shapes the one-plain-command rule "
        "forbids (AGENTS.md; docs/conventions/container-ops.md §Refused "
        "shapes) — split into one call per command, or add a justified "
        "_RECIPE_ALLOW entry:\n  " + "\n  ".join(hits)
    )


def test_recipe_patterns_catch_the_forbidden_shapes() -> None:
    """The patterns flag what the rule forbids and pass plain commands."""

    def flagged(line: str) -> set[str]:
        return {t for t, p in _RECIPE_PATTERNS.items() if p.search(line)}

    assert flagged('scripts/release > l 2>&1; echo "X=$?" >> l') == {'; echo "'}
    assert flagged("git add -u && git commit -F m") == {"&& between two commands"}
    assert flagged("scripts/a && scripts/b") == {"&& between two commands"}
    assert flagged('git commit -m "$(cat m)"') == {"$("}
    assert flagged("git add -u") == set()
    assert flagged("scripts/release > .claude/release.log 2>&1") == set()
    assert flagged("cd x && make") == set()
    assert _fenced_lines("a\n```\nx \\\n  y\n```\nb\n") == [(4, "x  y")]


# ── commit text names the footer (gr676944) ──────────────────────────────────
#
# scripts/hooks/guard-commit-on-main.py denies a commit whose message has no
# ``Co-Authored-By:`` trailer. An agent definition that says how to commit
# names the footer or the ``git commit -F`` recipe on the same line, or the
# agent following it is denied once and rediscovers the rule.

_COMMIT_TEXT_RE = re.compile(r"git commit|Commit with")
_COMMIT_FOOTER_RE = re.compile(r"Co-Authored-By|(?<![\w-])-F\b")


def commit_text_gaps(text: str) -> list[tuple[int, str]]:
    """``(lineno, line)`` for each commit instruction naming no footer or ``-F``."""
    return [
        (lineno, line.strip())
        for lineno, line in enumerate(text.splitlines(), start=1)
        if _COMMIT_TEXT_RE.search(line) and not _COMMIT_FOOTER_RE.search(line)
    ]


def test_agent_commit_text_names_footer() -> None:
    paths = sorted(_ROOT.glob(".claude/agents/*.md"))
    assert len(paths) > 10, f"only {len(paths)} agent definitions — vacuous"
    hits = [
        f"{path.relative_to(_ROOT).as_posix()}:{lineno}: {line}"
        for path in paths
        for lineno, line in commit_text_gaps(read_text(path))
    ]
    assert not hits, (
        "agent definitions say how to commit without the Co-Authored-By "
        "footer or the `git commit -F <file>` recipe; the commit hook denies "
        "a footerless message (docs/conventions/shipping.md §Long form):\n  "
        + "\n  ".join(hits)
    )


def test_commit_text_gate_flags_a_footerless_recipe() -> None:
    """A footerless commit instruction is flagged; the footer or ``-F`` passes."""
    flagged = [
        "Commit with a conventional subject, or pass `-m`.",
        'git commit -m "fix(x): y"',
        "git commit --no-FF-ish -m x",
    ]
    for line in flagged:
        assert commit_text_gaps(line), f"not flagged: {line}"
    passed = [
        "Commit with `type(scope): what` plus the `Co-Authored-By:` footer.",
        "write the message to a file and `git commit -F <file>`",
        "do NOT commit anything while ship runs",
    ]
    for line in passed:
        assert not commit_text_gaps(line), f"false positive: {line}"
