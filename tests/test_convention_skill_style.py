"""Enforcers for docs/conventions/skill-authoring-style.md (static cut-list).

Rules covered:

- ``skill-authoring-style/drop-decorative-frontmatter``: no ``tier:`` /
  ``floor:`` / ``last-updated:`` keys, no ``status: phase-N``, no ``## Status``
  body section.
- ``skill-authoring-style/cut-internal-names``: storage/worker/model names
  (``ref_segments``, ``segment_toc``, ``bge-m3``, ``RRF``, ``KeyBERT``, ``TOON``
  ...) stay out of skill bodies. ``precis-toon`` documents TOON itself.
- ``skill-authoring-style/examples-and-skeleton``: no "Not yet" / negative-list
  headings, no ``Note:`` / ``Important:`` / ``Tip:`` callouts.

Not gated (judgement): ``goal-voice-h2`` (the doc says no static gate can tell
a goal from a nominalisation) and ``contract-not-implementation``.

Existing offenders are frozen in the ratchets below; entries may only shrink.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests._skill_roots import skill_files

_DECORATIVE_KEYS = re.compile(r"^(tier|floor|last-updated)\s*:", re.M)
_PHASE_STATUS = re.compile(r"^status\s*:\s*phase-\d+", re.M | re.I)
_STATUS_H2 = re.compile(r"^##\s+Status\s*$", re.M | re.I)
_INTERNAL = re.compile(
    r"ref_segments|ref_segment_sentences|chunk_embeddings|segment_toc"
    r"|FileCorpusIndex|bge-m3|\bRRF\b|KeyBERT|tsvector|pgvector|\bTOON\b"
)
_NOT_YET_H = re.compile(
    r"^#{2,}\s*(?:not yet\b|not (?:yet )?(?:supported|implemented))", re.I | re.M
)
_CALLOUT = re.compile(r"^\s*(?:[-*>]\s+)?\**(?:Note|Important|Tip)\**\s*:", re.M)

# Frozen on 2026-10-17: files still carrying `last-updated:` frontmatter.
_LAST_UPDATED_ALLOWLIST = frozenset(
    {
        "precis-adversarial-reviewer.md",
        "precis-citation-reviewer.md",
        "precis-claim-adversary.md",
        "precis-draft-reviewer.md",
        "precis-review-authoring.md",
    }
)
# Frozen on 2026-10-17: internal-name occurrences per skill (mostly worked
# examples quoting a memory). Counts may only shrink; delete at zero.
_INTERNAL_ALLOWLIST = {
    "precis-cad-help.md": 1,
    "precis-common-reviewer.md": 1,
    "precis-firstline-help.md": 5,
    "precis-inner-life-help.md": 2,
    "precis-overview.md": 2,
    "precis-pres-help.md": 1,
    "precis-put-help.md": 1,
    "precis-tags.md": 1,
    "precis-voice.md": 1,
}
_INTERNAL_EXEMPT = {"precis-toon.md"}


def split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---", 3)
    if end == -1:
        return "", text
    return text[3:end], text[end + 4 :]


def decorative_frontmatter(text: str) -> list[str]:
    fm, _ = split_frontmatter(text)
    return [m.group(1) for m in _DECORATIVE_KEYS.finditer(fm)] + [
        m.group(0) for m in _PHASE_STATUS.finditer(fm)
    ]


def status_sections(text: str) -> list[str]:
    return _STATUS_H2.findall(split_frontmatter(text)[1])


def internal_names(text: str) -> list[str]:
    return _INTERNAL.findall(split_frontmatter(text)[1])


def style_violations(text: str) -> list[str]:
    body = split_frontmatter(text)[1]
    return [m.group(0) for m in _NOT_YET_H.finditer(body)] + [
        m.group(0) for m in _CALLOUT.finditer(body)
    ]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_decorative_frontmatter_ratchet() -> None:
    offenders: dict[str, list[str]] = {}
    carrying: set[str] = set()
    for p in skill_files():
        found = decorative_frontmatter(_read(p))
        if not found:
            continue
        if set(found) == {"last-updated"} and p.name in _LAST_UPDATED_ALLOWLIST:
            carrying.add(p.name)
        else:
            offenders[p.name] = found
    assert not offenders, (
        f"decorative frontmatter (drop tier/floor/last-updated/phase-N): {offenders}"
    )
    stale = _LAST_UPDATED_ALLOWLIST - carrying
    assert not stale, f"fixed; remove from _LAST_UPDATED_ALLOWLIST: {sorted(stale)}"


def test_no_status_body_section() -> None:
    bad = [p.name for p in skill_files() if status_sections(_read(p))]
    assert not bad, f"`## Status` body sections (use frontmatter status:): {bad}"


def test_internal_names_ratchet() -> None:
    counts: dict[str, int] = {}
    for p in skill_files():
        if p.name in _INTERNAL_EXEMPT:
            continue
        n = len(internal_names(_read(p)))
        if n:
            counts[p.name] = n
    over = {k: v for k, v in counts.items() if v > _INTERNAL_ALLOWLIST.get(k, 0)}
    assert not over, (
        f"internal names in skill bodies (cut them; allowed={_INTERNAL_ALLOWLIST}): {over}"
    )
    shrunk = {
        k: counts.get(k, 0)
        for k, v in _INTERNAL_ALLOWLIST.items()
        if counts.get(k, 0) < v
    }
    assert not shrunk, f"ratchet can tighten; lower _INTERNAL_ALLOWLIST to {shrunk}"


def test_no_not_yet_sections_or_callouts() -> None:
    found = {p.name: style_violations(_read(p)) for p in skill_files()}
    bad = {k: v for k, v in found.items() if v}
    assert not bad, f"'Not yet' headings / Note:/Important:/Tip: callouts: {bad}"


# --- permanent negative cases: each checker must fire on a synthetic violation


def test_checkers_fire_on_synthetic_violations() -> None:
    bad_fm = (
        "---\nid: x\ntier: 2\nfloor: 1\nlast-updated: 2026-01-01\n"
        "status: phase-3\n---\nbody"
    )
    assert decorative_frontmatter(bad_fm) == [
        "tier",
        "floor",
        "last-updated",
        "status: phase-3",
    ]
    assert decorative_frontmatter("---\nid: x\nstatus: active\n---\nbody") == []
    assert status_sections("---\nid: x\n---\n## Status\nwip\n") == ["## Status"]
    assert internal_names(
        "---\nid: x\n---\nserved from ref_segments via RRF and bge-m3"
    )
    assert not internal_names("---\nid: x\nsummary: RRF\n---\nclean")
    assert style_violations("---\nid: x\n---\n## Not yet supported\n")
    assert style_violations("---\nid: x\n---\n**Note:** careful\n")
    assert style_violations("---\nid: x\n---\n- Tip: do this\n")
    assert not style_violations(
        "---\nid: x\n---\n## Find patents not yet in the store\n"
    )
