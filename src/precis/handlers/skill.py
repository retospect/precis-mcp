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

    #: Special slug that's synthesized at runtime from the active
    #: registry (rather than served from a markdown file).
    _SYNTHESIZED_SLUG: ClassVar[str] = "precis-help"

    def __init__(self, *, store: Store) -> None:
        # store is unused but kept in the constructor signature so the
        # registry's kw-args call shape works for every handler.
        self.store = store
        self._registry: Any = None  # set later via bind_registry()

    def bind_registry(self, registry: Any) -> None:
        """Hook for the runtime: gives this handler a registry reference
        so the synthesized ``precis-help`` skill can list active kinds."""
        self._registry = registry

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

        # Synthesized meta-skill: enumerate every active kind.
        if slug == self._SYNTHESIZED_SLUG:
            return Response(body=self._render_help())

        text = _load_skill(slug)
        if text is None:
            available = sorted(_list_skills())
            available.append(self._SYNTHESIZED_SLUG)
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
        # Surface the synthesized meta-skill at the top so it's the
        # first thing the agent sees.
        index_entries: list[tuple[str, str]] = [
            (
                self._SYNTHESIZED_SLUG,
                "active kinds + verbs (auto-generated from this server)",
            )
        ]
        for slug in skills:
            title = _skill_title(slug)
            index_entries.append((slug, title))

        lines = [f"# {len(index_entries)} skill(s) available"]
        for slug, title in index_entries:
            if title:
                lines.append(f"  {slug:<32}  {title}")
            else:
                lines.append(f"  {slug}")
        body = "\n".join(lines)
        body += render_next_section(
            [
                (
                    f"get(kind='skill', id={self._SYNTHESIZED_SLUG!r})",
                    "what this server can do (active kinds)",
                ),
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

    def _render_help(self) -> str:
        """Render the synthesized ``precis-help`` skill.

        Lists every kind currently active in this server's registry,
        grouped by class (state / cache / file / paid), with the verbs
        each supports. When the registry isn't wired (e.g. unit tests
        that build a SkillHandler directly), falls back to a static
        introduction.
        """
        lines = [
            "# precis-help",
            "",
            "What this server can do **right now** — active kinds",
            "and supported verbs, generated from the live registry.",
            "",
        ]
        if self._registry is None:
            lines.append(
                "_(registry not wired; see precis-overview for the canonical list.)_"
            )
            return "\n".join(lines)

        rows: list[tuple[str, str, str]] = []  # (kind, verbs, desc)
        for kind in self._registry.kinds():
            handler = self._registry.get(kind)
            spec = handler.spec
            verbs: list[str] = []
            if spec.supports_get:
                verbs.append("get")
            if spec.supports_search:
                verbs.append("search")
            if spec.supports_put:
                verbs.append("put")
            verb_str = " / ".join(verbs)
            desc = (spec.description or "").splitlines()[0] if spec.description else ""
            if len(desc) > 90:
                desc = desc[:87] + "…"
            rows.append((kind, verb_str, desc))

        if not rows:
            lines.append("_(no kinds available)_")
            return "\n".join(lines)

        kind_w = max(len(r[0]) for r in rows)
        verb_w = max(len(r[1]) for r in rows)
        for kind, verbs, desc in rows:
            lines.append(f"  {kind:<{kind_w}}  {verbs:<{verb_w}}  {desc}")

        lines.append("")
        lines.append(
            f"**{len(rows)} kinds active.** "
            "For deeper docs on any kind, try "
            "`get(kind='skill', id='precis-<kind>-help')`."
        )
        return "\n".join(lines)


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
