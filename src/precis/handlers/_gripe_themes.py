"""Recurring-gripe themes: slug -> case-insensitive regexes.

Matched against a new gripe's title + body inside
``GripeHandler.put``'s create path. Exactly one matching theme with a
live ``theme:<slug>``-tagged keeper means the filing lands as a ``+1``
comment on that keeper instead of minting a new gripe; one match with
no keeper tags the new gripe ``theme:<slug>`` so it becomes the
keeper; several matches (or none, or ``args={'new': True}``) file
normally. Filers never search first — the matching lives here, not in
the filing agent.

Seeded 2026-10-24 from the gripe-ux friction batch (Reto's "1,2,3 OK"
on filing staying trivial). Each pattern set is anchored to the listed
example gripes; :mod:`tests.test_gripe_themes` pins that each slug
hits its examples and that five unrelated real titles match nothing.
"""

from __future__ import annotations

import re
from typing import Final

#: tag prefix a keeper carries; the theme slug is the tag value.
THEME_TAG_PREFIX: Final = "theme:"

#: Raw (case-insensitive) patterns per slug. A slug matches when ANY of
#: its patterns hits the title+body text.
_PATTERNS: Final[dict[str, tuple[str, ...]]] = {
    # scripts/test container bring-up, slow single-file runs.
    # Examples: gr514478, gr514483, gr514502, gr514875.
    "test-speed": (
        r"scripts/test[^\n]{0,80}\b(slow|wait|bring.?up|idle|queue|queued|postgres|background)",
        r"\b(slow|wait|bring.?up|idle|queue|queued|background\w*)\b[^\n]{0,80}scripts/test\b",
        r"\bqueue\w*\b[^\n]{0,60}\bpostgres\b|\bpostgres\b[^\n]{0,60}\bqueue\w*\b",
        r"\bcontainer bring.?up\b|\bbring.?up\b[^\n]{0,40}\bcontainer\b",
        r"\buv run\b[^\n]{0,80}\d+(?:\.\d+)?\s?s\b",
    ),
    # heredoc or $() commit refused by the worktree guard, quoting
    # failures, the -F recipe. gr514943, gr514972, gr514895, gr514617,
    # gr514743.
    "commit-heredoc": (
        r"\bheredoc\b[^\n]{0,80}\bcommit\b|\bcommit\b[^\n]{0,80}\bheredoc\b",
        r"\bgit commit\b[^\n]{0,100}(\$\(|<<\s*'?\w*eof|backtick|apostrophe"
        r"|command.?substitut|unexpected eof|quot)",
        r"(\$\(|\bheredoc\b|<<\s*'?\w*eof)[^\n]{0,100}\bgit commit\b",
        r"\$\([^)]*\)[^\n]{0,60}\bcommit\b",
        r"\bworktree guard\b[^\n]{0,80}\bcommit\b|\bcommit\b[^\n]{0,80}\bworktree guard\b",
        r"\bbacktick\w*\b[^\n]{0,80}\bcommit\b|\bcommit\b[^\n]{0,80}\bbacktick\w*\b",
    ),
    # host mypy vs container: py.typed, .mypy_cache collision, stub
    # misses. gr514869, gr515069, gr514897, gr515001.
    "host-mypy": (
        r"\bhost\b[^\n]{0,60}\bmypy\b|\bmypy\b[^\n]{0,60}\bhost\b",
        r"\bmypy\b[^\n]{0,60}\bcontainer\b|\bcontainer\b[^\n]{0,60}\bmypy\b",
        r"\.mypy_cache\b",
        r"\bpy\.typed\b",
        r"\bmypy\b[^\n]{0,80}\b(stub|import-untyped|import-not-found|phantom)\b",
        r"\b(stub|import-untyped|import-not-found|phantom)\b[^\n]{0,80}\bmypy\b",
    ),
    # scripts/inflight slow or truncated. gr514898, gr514784, gr514947.
    "inflight-slow": (
        r"\binflight\b[^\n]{0,80}(slow|>\s*\d+\s*s|takes|second|truncat|overflow|cold)",
        r"(slow|takes|truncat|overflow)[^\n]{0,80}\binflight\b",
        r"scripts/inflight\b",
    ),
    # rtk digest rewrites identifiers or hides rows in grep output.
    # gr514332, gr514780, gr514944.
    "rtk-rewrite": (
        r"\brtk\b[^\n]{0,80}(rewrit|mangle|alter|hid(?:e|es|den)|drop|silently)",
        r"(rewrit|mangle|alter)[^\n]{0,80}\brtk\b",
        r"\brtk\b[^\n]{0,40}\bdigest",
    ),
    # test_skill_size's 32 KB cap squeezes skill edits. gr515088,
    # gr486399, gr472110, gr514415, gr510535.
    "skill-size-cap": (
        r"test_skill_size",
        r"\b32[, ]?768\b",
        r"\b32\s?kb\b[^\n]{0,50}\b(skill|cap)\b|\b(skill|cap)\b[^\n]{0,50}\b32\s?kb\b",
        r"\bskill[- ]size\b[^\n]{0,50}\b(cap|limit|hard.?cap)\b|\b(cap|limit|hard.?cap)\b[^\n]{0,50}\bskill[- ]size\b",
    ),
    # Devin builders have no SendMessage; how to reach the coordinator.
    # gr514682, gr515003.
    "devin-messaging": (
        r"\bsendmessage\b",
        r"\bmessag\w*\b[^\n]{0,60}\bcoordinator\b|\bcoordinator\b[^\n]{0,60}\bmessag\w*\b",
        r"scripts/fleet[^\n]{0,20}\b(say|panes)\b[^\n]{0,60}\b(message|coordinator|say)\b",
    ),
    # gripe-fix briefs carrying stale state, missing rulings, design
    # phrased as an existing API. gr514903, gr514870, gr514946, gr514962.
    "brief-quality": (
        r"\bbriefs?\b[^\n]{0,80}(omit|missing|stale|ambiguous|ignores?|wrong|conflict|ruling)",
        r"\bbriefs?\b[^\n]{0,80}(cite[sd]?|phras\w+)[^\n]{0,50}\b(design|api)\b",
        r"(omit|missing|stale|ambiguous|ignores?)[^\n]{0,80}\bbriefs?\b",
        r"\btriage\b[^\n]{0,80}(ignores?|omit|missing|stale|conflict)",
        r"\bgripe[- ]fix brief\b",
    ),
    # mypy's pyproject "unused section(s)" note; ruff parsing
    # extensionless scripts. gr514976, gr514997.
    "mypy-noise": (
        r"\bunused[- ]sections?\b",
        r"\bmypy\b[^\n]{0,80}\bpyproject\b|\bpyproject\b[^\n]{0,80}\bmypy\b",
        r"\bruff\b[^\n]{0,80}(extensionless|bash|invalid[- ]syntax|pars\w+)",
        r"(extensionless|invalid[- ]syntax)[^\n]{0,80}\bruff\b",
    ),
    # precis CLI / scripts/dev can't find the dev DB or secrets.
    # gr514999.
    "dev-db-env": (
        r"\bno database_url configured\b",
        r"\bdatabase_url\b[^\n]{0,80}(missing|absent|not configured|cannot find|not found|can't find)",
        r"\bscripts/dev\b[^\n]{0,80}(fail|cannot|can't|missing|no database|secret)",
        r"\bprecis\b[^\n]{0,80}(can'?t find|cannot find)[^\n]{0,60}\b(db|database|secrets?)\b",
    ),
}

#: Compiled registry: slug -> patterns (``re.IGNORECASE``).
THEMES: Final[dict[str, tuple[re.Pattern[str], ...]]] = {
    slug: tuple(re.compile(p, re.IGNORECASE) for p in pats)
    for slug, pats in _PATTERNS.items()
}


def match_themes(text: str) -> list[str]:
    """Sorted theme slugs whose patterns hit ``text`` (title + body)."""
    return sorted(
        slug for slug, pats in THEMES.items() if any(p.search(text) for p in pats)
    )
