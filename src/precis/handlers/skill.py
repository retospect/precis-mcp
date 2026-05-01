"""SkillHandler — markdown skill docs served from the data directory.

Slug-addressed, file-backed (not DB-backed). Each ``.md`` file under
``src/precis/data/skills/`` is one skill; the file's stem (without
the ``.md`` suffix) is the slug.

This is the agent's manual: ``get(kind='skill', id='precis-overview')``
returns the overview, ``get(kind='skill', id='precis-paper-help')``
returns the paper-handler docs, etc.

A bare ``get(kind='skill')`` lists every **available** skill — i.e.
every skill whose ``status:`` front-matter is ``active`` (or absent)
*and* whose subject kind is registered in the live runtime. Skills
documenting unregistered kinds (``precis-markdown-help`` when the
markdown handler isn't wired) and skills tagged ``status: planned``
or ``status: aspirational`` are filtered out — they remain
retrievable by exact slug, but with a banner warning the agent that
the recipes inside don't all execute on this build.

This filtering closes the MCP critic's CRITICAL #3 finding
("tools/list advertises skills for kinds the registry rejects")
and MAJOR #6 ("``precis-density`` documents three views that don't
exist") — the index now agrees with the runtime.

Front-matter ``title:`` is surfaced in the index; a one-line
description (the first non-front-matter line) acts as a synopsis.

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
from precis.utils.search_header import format_search_headline

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

    #: Special slugs synthesised at runtime rather than served from a
    #: markdown file.  Maps slug → one-line description used in the
    #: index view.  The actual rendering dispatches via ``_render_<slug
    #: stem>``: ``precis-help`` → ``_render_help``, ``precis-status``
    #: → ``_render_status``.
    #:
    #: Adding a synthesised skill is one entry here plus one render
    #: method.  All other gates (availability-gap, index, prompt
    #: enumeration) discover the slug through this dict.
    _SYNTHESIZED_SKILLS: ClassVar[dict[str, str]] = {
        "precis-help": ("active kinds + verbs (auto-generated from this server)"),
        "precis-status": ("optional dependencies + runtime health probe"),
    }

    #: Backwards-compat alias for the original single-slug attribute.
    #: Tests and the old availability-gap path reference this name; the
    #: new code paths consult ``_SYNTHESIZED_SKILLS`` directly.
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

        # Synthesised meta-skills: enumerate active kinds, probe
        # optional deps, …  Each entry in ``_SYNTHESIZED_SKILLS``
        # dispatches to ``_render_<slug-stem>``.
        if slug in self._SYNTHESIZED_SKILLS:
            method_name = "_render_" + slug.split("-", 1)[1].replace("-", "_")
            renderer = getattr(self, method_name, None)
            if renderer is None:  # pragma: no cover — registry typo
                raise NotFound(
                    f"synthesised skill {slug!r} has no renderer",
                    next="see SkillHandler._SYNTHESIZED_SKILLS",
                )
            return Response(body=renderer())

        text = _load_skill(slug)
        if text is None:
            available = sorted(_list_skills())
            available.extend(self._SYNTHESIZED_SKILLS)
            raise NotFound(
                f"skill {slug!r} not found",
                options=sorted(available),
                next="get(kind='skill') to list every skill",
            )
        # Banner if this skill is filtered from the index — the agent
        # asked for it explicitly, so we serve it, but we want them
        # to know the recipes inside may not all run on this build.
        # See CRITICAL #3 and MAJOR #6 in the MCP critique.
        gap = _availability_gap(slug, registry=self._registry)
        if gap is not None:
            text = f"> **Heads up:** {gap}\n\n" + text
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
        # Total before paging — same shape as ref/block search totals.
        total = len(hits)
        hits = hits[:top_k]
        lines = [
            format_search_headline(
                n_returned=len(hits),
                total=total,
                noun="skill match",
                query=q,
            )
        ]
        for slug, cnt, preview in hits:
            preview_short = (preview[:120] + "…") if len(preview) > 120 else preview
            # Mark skills whose subject kind isn't in the live
            # registry (or whose status is planned/aspirational).
            # The index view already hides them; search has to do
            # the same honesty work or 7B callers will quote the
            # title and invoke an [error:NotFound] kind. (MCP critic
            # MINOR — search surfaces unwired skills without marker.)
            gap = _availability_gap(slug, registry=self._registry)
            marker = " [unwired]" if gap is not None else ""
            lines.append(f"\n## {slug}{marker}  ({cnt} hits)\n{preview_short}")
        return Response(body="\n".join(lines))

    # ── helpers ────────────────────────────────────────────────────

    def _render_index(self) -> Response:
        skills = sorted(_list_skills())
        # Surface every synthesised meta-skill at the top so they're
        # the first things the agent sees, in registration order.
        index_entries: list[tuple[str, str]] = [
            (slug, desc) for slug, desc in self._SYNTHESIZED_SKILLS.items()
        ]
        hidden_slugs: list[str] = []
        for slug in skills:
            # Filter skills whose subject kind isn't in the registry
            # or whose front-matter status isn't ``active``. They
            # remain reachable via direct slug get(), but they don't
            # clutter the index that agents use for discovery.
            if _availability_gap(slug, registry=self._registry) is not None:
                hidden_slugs.append(slug)
                continue
            title = _skill_title(slug)
            index_entries.append((slug, title))

        lines = [f"# {len(index_entries)} skill(s) available"]
        for slug, title in index_entries:
            if title:
                lines.append(f"  {slug:<32}  {title}")
            else:
                lines.append(f"  {slug}")
        if hidden_slugs:
            lines.append("")
            lines.append(
                f"_(+ {len(hidden_slugs)} non-active skills hidden — "
                "documenting kinds not wired in this build, or marked "
                "status: planned. Reach them by exact slug if you need "
                "to.)_"
            )
        body = "\n".join(lines)
        body += render_next_section(
            [
                (
                    "get(kind='skill', id='precis-help')",
                    "what this server can do (active kinds)",
                ),
                (
                    "get(kind='skill', id='precis-status')",
                    "optional-deps + runtime health",
                ),
                ("get(kind='skill', id='precis-overview')", "start here"),
                (
                    "get(kind='skill', id='precis-tags')",
                    "tag axes + validation rules",
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

    def _render_status(self) -> str:
        """Render the synthesised ``precis-status`` skill.

        Probes optional Python dependencies and reports installed /
        missing per backing module.  The MCP critic's April 2026
        re-probe noted that the previous CRITICAL (sentence-
        transformers missing from `[paper]`) would have been caught
        by a health tool surfacing optional-deps state — this skill
        is that tool.

        Pure introspection, no DB or network.  Each probe is a
        ``(label, module, kind it backs, install-hint)`` row; the
        method imports each module lazily and tags the line OK /
        MISSING / ERROR.  Adding a new probe is one row in
        ``_OPTIONAL_DEP_PROBES``.
        """
        import importlib

        lines = [
            "# precis-status",
            "",
            "Optional-dependency health probe.  Each row tags the",
            "Python module that backs a precis kind or affordance,",
            "and reports whether it imports cleanly in this venv.",
            "",
        ]

        rows: list[tuple[str, str, str, str]] = []  # (label, status, kind, hint)
        worst = "OK"
        for module, label, backs, hint in _OPTIONAL_DEP_PROBES:
            try:
                mod = importlib.import_module(module)
            except ImportError:
                rows.append((label, "MISSING", backs, hint))
                worst = "DEGRADED"
                continue
            except Exception as exc:  # pragma: no cover — import-side bug
                rows.append((label, f"ERROR: {exc}", backs, hint))
                worst = "DEGRADED"
                continue
            version = getattr(mod, "__version__", None) or "(unknown)"
            rows.append((label, f"OK {version}", backs, ""))

        label_w = max(len(r[0]) for r in rows)
        status_w = max(len(r[1]) for r in rows)
        for label, status, backs, hint in rows:
            line = f"  {label:<{label_w}}  {status:<{status_w}}  {backs}"
            lines.append(line)
            if hint and not status.startswith("OK"):
                lines.append(f"    └─ install: {hint}")

        lines.append("")
        lines.append(f"**Overall: {worst}**")
        if worst == "DEGRADED":
            lines.append(
                "\nMissing entries above mean the listed kinds will "
                "raise at runtime even though `tools/list` advertises "
                "them.  Install the missing extra and restart the MCP."
            )

        # Embedder + store are deeper than a bare import probe, but
        # cheap to surface from the bound registry when wired.
        if self._registry is not None:
            try:
                kinds = sorted(self._registry.kinds())
            except Exception:  # pragma: no cover
                kinds = []
            if kinds:
                lines.append("")
                lines.append(
                    f"**Registered kinds ({len(kinds)}):** " + ", ".join(kinds)
                )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Optional-dep probe table — used by ``precis-status``
# ---------------------------------------------------------------------------

#: ``(import name, display label, kinds it backs, pip install hint)``.
#:
#: Adding a new probe is one row.  Keep the import name precise
#: (the exact ``importlib.import_module`` argument) and the install
#: hint copy-pasteable.
_OPTIONAL_DEP_PROBES: tuple[tuple[str, str, str, str], ...] = (
    (
        "sentence_transformers",
        "sentence-transformers",
        "paper / markdown / patent / quest semantic search",
        "pip install 'precis-mcp[paper]'",
    ),
    (
        "sympy",
        "sympy",
        "calc",
        "pip install 'precis-mcp[calc]'",
    ),
    (
        "wolframalpha",
        "wolframalpha",
        "math",
        "pip install 'precis-mcp[external]'",
    ),
    (
        "youtube_transcript_api",
        "youtube-transcript-api",
        "youtube",
        "pip install 'precis-mcp[external]'",
    ),
    (
        "httpx",
        "httpx",
        "web / perplexity (websearch / think / research)",
        "pip install 'precis-mcp[external]'",
    ),
    (
        "trafilatura",
        "trafilatura",
        "web (page → markdown extraction)",
        "pip install 'precis-mcp[external]'",
    ),
    (
        "docx",
        "python-docx",
        "docx file kind",
        "pip install 'precis-mcp[docx]'",
    ),
    (
        "lxml",
        "lxml",
        "tex / patent / docx XML parsing",
        "pip install 'precis-mcp[tex]' or [docx] or [patent]",
    ),
    (
        "epo_ops",
        "python-epo-ops-client",
        "patent (EPO OPS biblio + claims)",
        "pip install 'precis-mcp[patent]'",
    ),
    (
        "matplotlib",
        "matplotlib",
        "plot kind (declarative renderer)",
        "pip install 'precis-mcp[plot]'",
    ),
)


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


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract the YAML-style front-matter as a flat dict of strings.

    We don't pull in PyYAML for this — the front-matter we use is
    strictly key:value pairs. Lists and nested dicts aren't supported,
    which is fine because the only fields we read here are scalars
    (``status``, ``applies-to``, ``title``).
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out: dict[str, str] = {}
    for line in text[3:end].splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        out[key.strip().lower()] = val.strip().strip("\"'")
    return out


#: Match ``kind='X'`` (or ``kind="X"``) inside the front-matter
#: ``applies-to:`` line. Power-user skills like ``precis-patent-power``
#: don't follow the ``precis-<kind>-help`` naming pattern but their
#: front-matter still names the kind via ``applies-to: search
#: (kind='patent', q=<CQL>)``. Parsing this lets the gate hide the
#: skill alongside its ``-help`` sibling when the kind is missing.
_APPLIES_TO_KIND_RE = re.compile(r"""kind\s*=\s*['"]([a-z][a-z0-9_-]*)['"]""")


def _kinds_referenced_by_skill(slug: str, fm: dict[str, str]) -> list[str]:
    """Return every kind the skill claims to apply to.

    Two sources, in priority order:
      1. Front-matter ``applies-to:`` — extract every ``kind='X'``.
      2. Slug suffix ``precis-<kind>-help`` — derived as a fallback
         so existing ``-help`` skills without explicit front-matter
         still gate correctly.

    Returns an empty list for cross-cutting skills (``precis-overview``,
    ``precis-tags``, …) that don't reference any specific kind.
    """
    kinds: list[str] = []
    applies = fm.get("applies-to") or fm.get("applies_to") or ""
    if applies:
        kinds.extend(_APPLIES_TO_KIND_RE.findall(applies))
    if slug.startswith("precis-") and slug.endswith("-help"):
        derived = slug[len("precis-") : -len("-help")]
        if derived and derived not in kinds:
            kinds.append(derived)
    return kinds


def _availability_gap(slug: str, *, registry: Any) -> str | None:
    """Return a human-readable reason why this skill is filtered, or None.

    Two gates:

    1. Subject-kind gate. The skill names a kind via the slug
       (``precis-<kind>-help``) or the ``applies-to:`` front-matter
       (``kind='X'``). When *any* referenced kind is missing from the
       registry, the skill is filtered — the recipes don't all run.
       Power-user companions like ``precis-patent-power`` flow through
       the front-matter side of this gate.
    2. Status gate. Front-matter ``status:`` of ``planned`` or
       ``aspirational`` flags the skill as "describes a future API,
       don't follow recipes blind". Filtered.

    Cross-cutting skills (``precis-overview``, ``precis-tags``, …)
    reference no kind and pass gate 1 automatically. They're still
    subject to gate 2.

    Returns ``None`` if the skill is fully available.
    """
    if slug in SkillHandler._SYNTHESIZED_SKILLS:
        return None

    text = _load_skill(slug)
    if text is None:
        return None  # caller handles 'not found'

    fm = _parse_frontmatter(text)
    status = fm.get("status", "active").lower()
    if status in ("planned", "aspirational"):
        return (
            f"this skill is marked status: {status} — its examples "
            "describe a planned API, not the live runtime. Treat as "
            "design notes, not as recipes."
        )

    if registry is not None:
        for kind in _kinds_referenced_by_skill(slug, fm):
            if not _registry_has_kind(registry, kind):
                return (
                    f"this skill documents kind={kind!r} which is **not "
                    "wired** in this build — its examples will return "
                    "[error:NotFound] unknown kind."
                )

    return None


def _registry_has_kind(registry: Any, kind: str) -> bool:
    """Best-effort registry membership check.

    We access the registry through duck-typing because the
    SkillHandler may be constructed before the registry is fully
    set up (in tests, e.g.). Treat any AttributeError or KeyError
    as "kind not registered" so the filter stays conservative
    rather than crashing the index.
    """
    try:
        kinds = registry.kinds()
    except (AttributeError, KeyError):
        return False
    return kind in kinds


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
