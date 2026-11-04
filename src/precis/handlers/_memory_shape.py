"""Mechanical memory-shape advice, never a write gate.

These deliberately conservative text predicates need no model or trigger.
Grouping is lexical, so proposed titles are excerpts for the writer to refine,
not assertions that a semantic split has been proved. Git is optional: one
bounded local lookup, cached on success, with no fetch or history walk. An
unavailable check is distinct from a clean check so it cannot erase an audit.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from precis.utils.handle_registry import CHUNK_CODES, KIND_CODES

SHAPE_CODES = frozenset(
    {
        "grab-bag",
        "unanchored-plan",
        "old-sha",
        "history-density",
        "missing-route",
        "title-shape",
    }
)
_DATE = r"\b\d{4}-\d{2}-\d{2}\b"
_DATE_RANGE = re.compile(rf"{_DATE}\s*(?:to|through|[–—-]|\.\.)\s*{_DATE}", re.I)
_YEAR_RANGE = re.compile(r"\b(?:19|20)\d{2}\s*(?:to|[–—-])\s*(?:19|20)\d{2}\b")
_PLAN = re.compile(
    r"\b(?:(?i:(?:slice|round|leg|wave)\s*[-:#]?\s*\d+[a-z]?)|R\d{2,})\b"
)
_HANDLE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(c) for c in {*KIND_CODES.values(), *CHUNK_CODES.values()})
    + r")(?:\d+|_[\w-]+)\b|\b(?:"
    + "|".join(re.escape(k) for k in KIND_CODES)
    + r"):[^\s:]+"
)
_SHA = re.compile(
    r"(?<![\w/#])(?=[a-fA-F0-9]*[a-fA-F])(?=[a-fA-F0-9]*\d)"
    r"([a-fA-F0-9]{7,40})(?![\w/])"
)
_HISTORY = re.compile(
    r"\b(?:landed|merged|shipped|deployed|completed|resolved)\b|"
    r"\b(?:was|were|has been|have been)\s+(?:fixed|closed|retired|removed)\b",
    re.I,
)
_DATED_ACTION = re.compile(
    r"\b(?:added|fixed|removed|changed|ran|finished|started)\b", re.I
)
_HEDGE = re.compile(
    r"\b(?:may|might|perhaps|possibly|probably|uncertain|hypothesis|suspect|seems?)\b",
    re.I,
)
_CLAIM = re.compile(
    r"\b(?:always|never|only)\b|"
    r"\b(?:is|are|was|were|measures?|measured|averages?|averaged|takes?|took|"
    r"returns?)\s+(?:[<>~=]\s*)?\d+(?:\.\d+)?\b",
    re.I,
)
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "is",
        "are",
        "be",
        "to",
        "of",
        "for",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "as",
        "and",
        "or",
        "but",
        "not",
        "no",
        "one",
        "when",
        "then",
        "use",
        "uses",
        "should",
        "must",
        "can",
        "will",
        "why",
        "what",
        "how",
        "do",
        "does",
        "fix",
        "fixed",
        "note",
        "notes",
    ]
)
_GIT_TIMEOUT = 0.05
_GIT_MAX_SHAS = 16


@dataclass(frozen=True)
class ShapeFinding:
    code: str
    message: str
    titles: tuple[str, ...] = ()

    def advisory(self) -> str:
        return f"AUDIT:{self.code}: {self.message}"


@dataclass(frozen=True)
class ShapeAudit:
    findings: tuple[ShapeFinding, ...]
    checked_codes: frozenset[str]
    git_context: tuple[str, str] | None = None


def _prose_lines(body: str) -> list[str]:
    """Ignore fenced examples; keep indentation for top-level list detection."""
    lines = []
    fence = ""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if not fence:
                fence = marker
            elif marker == fence:
                fence = ""
            continue
        if stripped and not fence:
            lines.append(line)
    return lines


def _atomic_titles(title: str, lines: list[str]) -> tuple[str, ...]:
    """Require body topic groups; title conjunctions alone prove no split.

    Numbered steps are ordered procedure, not independent claims. Only
    unordered bullets and headings supply evidence for a grab-bag.
    """
    bullets = [
        (len(s) - len(s.lstrip()), m[1])
        for s in lines
        if (m := re.match(r"^\s*[-*+] (.+)", s))
    ]
    heads = [
        (len(m[1]), m[2]) for s in lines if (m := re.match(r"^(#{1,6})\s+(.+)", s))
    ]
    # One document heading is a wrapper, not an extra claim.
    if heads and sum(level == min(h[0] for h in heads) for level, _ in heads) == 1:
        heads = [h for h in heads if h[0] != min(level for level, _ in heads)]
    items = [text for level, text in heads if level == min(h[0] for h in heads)]
    items += [text for indent, text in bullets if indent == min(b[0] for b in bullets)]
    groups: list[tuple[set[str], str]] = []
    for item in items:
        words = {
            w.rstrip("s")
            for w in re.findall(r"[a-z]{3,}", item.lower())
            if w not in _STOP
        }
        if not words:
            continue
        if any(
            len(words & seed) / min(len(words), len(seed)) >= 0.5 for seed, _ in groups
        ):
            continue
        excerpt = re.sub(r"[*_`]", "", item).strip()
        groups.append((words, " ".join(excerpt.split()[:12]).rstrip(".;:")))
    title_signal = bool(
        re.search(r"\b(?:and|two)\b", title, re.I)
        or _DATE_RANGE.search(title)
        or _YEAR_RANGE.search(title)
    )
    if len(items) >= 4 and len(groups) >= 3:
        return tuple(excerpt for _, excerpt in groups)
    if title_signal and len(groups) >= 2:
        return tuple(excerpt for _, excerpt in groups)
    return ()


@lru_cache(maxsize=256)
def _commit_times(shas: tuple[str, ...]) -> tuple[tuple[str, int], ...]:
    """Cache successful bounded lookups only; exceptions are handled by caller."""
    result = subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "log",
            "--no-walk",
            "--ignore-missing",
            "--format=%H:%ct",
            *(f"{sha}^{{commit}}" for sha in shas),
            "--",
        ],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=_GIT_TIMEOUT,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    return tuple(
        (sha, int(stamp))
        for row in result.stdout.splitlines()
        if ":" in row
        for sha, stamp in [row.split(":", 1)]
    )


def _old_shas(body: str) -> tuple[str, ...] | None:
    shas = tuple(dict.fromkeys(m[1].lower() for m in _SHA.finditer(body)))
    if not shas:
        return ()
    # Never turn a partial scan into a clean verdict.
    if len(shas) > _GIT_MAX_SHAS:
        return None
    try:
        commits = _commit_times(shas)
        cutoff = (datetime.now(UTC) - timedelta(days=30)).timestamp()
        return tuple(
            short
            for short in shas
            if any(full.startswith(short) and stamp < cutoff for full, stamp in commits)
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def audit_shape(
    *,
    title: str,
    body: str,
    tags: Iterable[str] = (),
    meta: Mapping[str, Any] | None = None,
    has_evidence: bool = False,
    check_git: bool = True,
) -> ShapeAudit:
    """Return advice and the codes safe to clear on this prospective write.

    A reference needs a route only for an unhedged numeric measurement or
    an absolute (always/never/only) claim in its body. Ordinary descriptive
    verbs and sentence punctuation are not evidence of such a claim.
    Incident narratives intentionally group cause, impact and timeline;
    only their grab-bag check is exempt, not the other shape checks.
    """
    lines = _prose_lines(body)
    findings: list[ShapeFinding] = []
    checked = set(SHAPE_CODES)
    tag_set = set(tags)
    titles = () if "incident" in tag_set else _atomic_titles(title, lines)
    if titles:
        count = max(2, len(titles))
        findings.append(
            ShapeFinding(
                "grab-bag",
                f"this body carries ~{count} claims; {count} memories find better; "
                "split into one claim per memory",
                titles,
            )
        )
    if any(_PLAN.search(line) and not _HANDLE.search(line) for line in [title, *lines]):
        findings.append(
            ShapeFinding(
                "unanchored-plan",
                "plan labels lack a handle on their line; add the owning "
                "todo/gripe or other ref handle, or remove the label",
            )
        )
    old = _old_shas("\n".join([title, *lines])) if check_git else None
    if old is None:
        checked.remove("old-sha")
    elif old:
        findings.append(
            ShapeFinding(
                "old-sha",
                "commit shas older than 30 days: "
                + ", ".join(old)
                + "; replace with a live anchor and keep the date",
            )
        )
    history = sum(
        bool(
            _HISTORY.search(line)
            or (re.search(_DATE, line) and _DATED_ACTION.search(line))
        )
        for line in lines
    )
    if len(lines) >= 4 and history / len(lines) >= 0.5:
        findings.append(
            ShapeFinding(
                "history-density",
                "at least half the body is history; move still-true claims "
                "to atomic memories and remove landed history",
            )
        )
    reference = (
        "section:reference" in tag_set or (meta or {}).get("type") == "reference"
    )
    route = has_evidence or any(
        t.startswith("route:") and t[6:].strip() for t in tag_set
    )
    route = route or any(re.match(r"^\s*Source:\s*\S", line, re.I) for line in lines)
    confident = any(
        _CLAIM.search(line)
        and not _HEDGE.search(line)
        and "?" not in line
        and not re.match(r"^\s*Source:", line, re.I)
        for line in lines
    )
    if reference and confident and not route:
        findings.append(
            ShapeFinding(
                "missing-route",
                "confident reference claim has no route; add a cites/derived-from "
                "edge, a Source: line, or a route: tag",
            )
        )
    if (
        re.fullmatch(r"[a-z0-9]+(?:[_-][a-z0-9]+)+", title.strip())
        or re.fullmatch(r"\S*[/\\]\S*", title.strip())
        or re.fullmatch(r"\S+\.(?:py|md|txt|yaml|json)", title.strip())
        or len(title.split()) > 16
        or re.search(_DATE, title)
    ):
        findings.append(
            ShapeFinding(
                "title-shape",
                "title is a slug, path, long label, or dated snapshot; "
                "state one claim in at most 12 words and put the date in the gloss",
            )
        )
    return ShapeAudit(
        tuple(findings),
        frozenset(checked),
        (title, body) if "old-sha" in checked else None,
    )
