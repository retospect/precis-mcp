"""SkillHandler — markdown skill docs served from the data directory.

Slug-addressed, file-backed (not DB-backed). Each ``.md`` file under
``src/precis/data/skills/`` is one skill; the file's stem (without
the ``.md`` suffix) is the slug.

This is the agent's manual: ``get(kind='skill', id='precis-overview')``
returns the overview, ``get(kind='skill', id='precis-paper-help')``
returns the paper-handler docs, etc.

A bare ``get(kind='skill')`` lists every available skill so the agent
can discover what's documented. Front-matter ``title:`` is surfaced in
the index; a one-line description (the first non-front-matter line)
acts as a synopsis.

Read-only. Skills ship with the package; agents can't write them at
runtime — that's by design (skills are versioned with code).
"""

from __future__ import annotations

import logging
import re
from importlib import resources
from typing import Any, ClassVar

from precis.errors import BadInput, NotFound
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import Store
from precis.utils.next_block import render_next_section

log = logging.getLogger(__name__)


# Slugs are conservative: lowercase ASCII alphanumerics + hyphens.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")


class SkillHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="skill",
        title="Skill",
        description=(
            "Agent skill — markdown documentation served from the "
            "package data dir. Read-only; ships with code."
        ),
        supports_get=True,
        supports_search=True,
        supports_put=False,
        is_numeric=False,
        id_required=False,
    )

    def __init__(self, *, store: Store) -> None:
        # store is unused but kept in the constructor signature so the
        # registry's kw-args call shape works for every handler.
        self.store = store

    # ── get ────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or (isinstance(id, str) and id.startswith("/")):
            return self._render_index()

        slug = str(id).strip()
        if not _SLUG_RE.match(slug):
            raise BadInput(
                f"invalid skill slug: {slug!r}",
                next="skill slugs are lowercase letters/digits/hyphens",
            )
        text = _load_skill(slug)
        if text is None:
            available = sorted(_list_skills())
            raise NotFound(
                f"skill {slug!r} not found",
                options=available,
                next="get(kind='skill') to list every skill",
            )
        return Response(body=text)

    # ── search ─────────────────────────────────────────────────────

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        top_k: int = 10,
        **_kw: Any,
    ) -> Response:
        if q is None or not q.strip():
            raise BadInput(
                "search requires q=",
                next="search(kind='skill', q='your query')",
            )
        needle = q.lower()
        hits: list[tuple[str, int, str]] = []  # (slug, hit_count, preview)
        for slug in _list_skills():
            text = _load_skill(slug)
            if text is None:
                continue
            cnt = text.lower().count(needle)
            if cnt > 0:
                # Pull the first matching line as a preview.
                preview = ""
                for line in text.splitlines():
                    if needle in line.lower():
                        preview = line.strip()
                        break
                hits.append((slug, cnt, preview))

        if not hits:
            return Response(body=f"no skills mention {q!r}")
        hits.sort(key=lambda h: -h[1])  # most matches first
        hits = hits[:top_k]
        lines = [f"# {len(hits)} skill match(es) for {q!r}"]
        for slug, cnt, preview in hits:
            preview_short = (preview[:120] + "…") if len(preview) > 120 else preview
            lines.append(f"\n## {slug}  ({cnt} hits)\n{preview_short}")
        return Response(body="\n".join(lines))

    # ── helpers ────────────────────────────────────────────────────

    def _render_index(self) -> Response:
        skills = sorted(_list_skills())
        if not skills:
            return Response(body="no skills installed (this is a packaging bug)")
        lines = [f"# {len(skills)} skill(s) available"]
        for slug in skills:
            title = _skill_title(slug)
            if title:
                lines.append(f"  {slug:<32}  {title}")
            else:
                lines.append(f"  {slug}")
        body = "\n".join(lines)
        body += render_next_section(
            [
                ("get(kind='skill', id='precis-overview')", "start here"),
                (
                    "get(kind='skill', id='precis-navigation')",
                    "how to drill into refs",
                ),
                (
                    "search(kind='skill', q='...')",
                    "search across all skills",
                ),
            ]
        )
        return Response(body=body)


# ---------------------------------------------------------------------------
# File access (importlib.resources keeps this working from a wheel)
# ---------------------------------------------------------------------------


def _list_skills() -> list[str]:
    """Return all available skill slugs (without the ``.md`` suffix)."""
    try:
        files = resources.files("precis.data.skills")
    except (ModuleNotFoundError, FileNotFoundError):
        log.warning("precis.data.skills package missing")
        return []
    out: list[str] = []
    for entry in files.iterdir():  # type: ignore[union-attr]
        name = entry.name
        if name.endswith(".md"):
            stem = name[:-3]
            if _SLUG_RE.match(stem):
                out.append(stem)
    return out


def _load_skill(slug: str) -> str | None:
    """Return the raw markdown for a skill, or None if missing."""
    try:
        files = resources.files("precis.data.skills")
        path = files / f"{slug}.md"
        if not path.is_file():  # type: ignore[union-attr]
            return None
        return path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    except (ModuleNotFoundError, FileNotFoundError):
        return None


def _skill_title(slug: str) -> str:
    """Pull the YAML front-matter ``title:`` field from a skill, if any.

    Falls back to the first ``# H1`` line, then to an empty string.
    """
    text = _load_skill(slug)
    if text is None:
        return ""
    # Front-matter block.
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                line = line.strip()
                if line.lower().startswith("title:"):
                    return line.split(":", 1)[1].strip().strip("\"'")
    # First H1.
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""
