"""Skill handler — filesystem-backed SKILL.md directories.

Aligned with the Agent Skills de facto standard (Anthropic Claude Code,
Cursor, Gemini CLI).  Each skill is a directory containing a
``SKILL.md`` file with YAML frontmatter (``name``, ``description``,
optional ``user-invocable``, ``argument-hint``, ``allowed-tools``,
``path-scoping``) plus a markdown body.

Scan paths in precedence order:

* ``./skills/``         — project-local, git-committed
* ``~/.precis/skills/`` — user-global, precis-authored
* ``~/.claude/skills/`` — ecosystem interop, read-only

The handler indexes skills at first use and re-indexes when mtimes
change.  Writes go to ``~/.precis/skills/`` only — precis never mutates
Claude Code's directory.

Precis extensions to the standard frontmatter (additive, ignored by
other runtimes):

* ``applies-to: [<kind>, ...]``       — link-graph edges + ``/kind`` filter
* ``kind-onboarding: <kind>``         — marks this as a kind's entry skill
* ``state-trigger: {kind, condition}`` — notification threshold (Phase 12b v1.1)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from precis.protocol import ErrorCode, Handler, PrecisError, extract_kwargs

log = logging.getLogger(__name__)


# ── Data model ───────────────────────────────────────────────────────


@dataclass
class Skill:
    """Parsed SKILL.md entry."""

    slug: str  # directory name; used as the id
    name: str  # frontmatter 'name'
    description: str  # frontmatter 'description'
    body: str  # markdown body after the frontmatter
    frontmatter: dict[str, Any]  # full parsed frontmatter
    source_path: Path  # absolute path to the SKILL.md
    mtime: float  # source_path.stat().st_mtime at scan time

    # Standard optional fields (from Agent Skills convention)
    user_invocable: bool = False
    argument_hint: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)

    # Precis extensions
    applies_to: list[str] = field(default_factory=list)
    kind_onboarding: str | None = None
    tags: list[str] = field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────────


def _as_list(value: Any) -> list[str]:
    """Coerce ``str | list | None`` to ``list[str]``."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(x) for x in value]
    return []


def _builtin_skills_path() -> Path:
    """Directory bundled with the precis-mcp package (seed skills)."""
    return Path(__file__).parent.parent / "skills"


def _default_scan_paths() -> list[Path]:
    """Return the ordered list of directories to scan for skills.

    Precedence order (first wins on slug collision):

    1. ``./skills/``              — project-local, git-committed
    2. ``~/.precis/skills/``      — user-global, agent-authored
    3. ``~/.claude/skills/``      — ecosystem interop (read-only from here)
    4. ``<pkg>/precis/skills/``   — seed skills bundled with precis-mcp

    Package-bundled skills land last so users / projects / Claude Code can
    shadow them with their own variants.
    """
    return [
        Path.cwd() / "skills",
        Path.home() / ".precis" / "skills",
        Path.home() / ".claude" / "skills",
        _builtin_skills_path(),
    ]


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter + body from a SKILL.md source.

    Returns ``(frontmatter_dict, body)``.  Missing/invalid frontmatter
    yields an empty dict with the full text as the body.
    """
    if not text.startswith("---"):
        return {}, text
    # Find the closing '---'
    lines = text.splitlines(keepends=True)
    end_idx = None
    for i in range(1, len(lines)):
        stripped = lines[i].rstrip("\n").rstrip()
        if stripped == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, text
    fm_text = "".join(lines[1:end_idx])
    body = "".join(lines[end_idx + 1 :]).lstrip("\n")
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        log.warning("skill: invalid YAML frontmatter: %s", exc)
        return {}, text
    if not isinstance(fm, dict):
        return {}, text
    return fm, body


def _parse_skill_md(path: Path) -> Skill | None:
    """Parse a single SKILL.md file into a :class:`Skill`.

    Returns ``None`` only when the file cannot be read at all.  The
    directory name is authoritative for the slug; frontmatter ``name``
    and ``description`` are best-effort — the former falls back to the
    slug (so a minimal SKILL.md with just a body is still indexable)
    and the latter falls back to the empty string (with a log warning
    so agents know to add one).  Lenient parsing keeps agent-authored
    skills discoverable; strict validation at write-time belongs in
    :meth:`SkillHandler._put_write`, not here.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("skill: cannot read %s: %s", path, exc)
        return None
    fm, body = _split_frontmatter(text)

    slug = path.parent.name  # directory name — authoritative
    name = str(fm.get("name") or "").strip() or slug
    description = str(fm.get("description") or "").strip()
    if not description:
        log.info(
            "skill: %s has no frontmatter 'description' — listing will "
            "show the body's first line instead",
            path,
        )
        # Fall back to the first non-blank body line, truncated, so
        # listings render something useful rather than an empty slot.
        for ln in body.splitlines():
            stripped = ln.strip()
            if stripped and not stripped.startswith("#"):
                description = stripped[:200]
                break

    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0

    return Skill(
        slug=slug,
        name=name,
        description=description,
        body=body,
        frontmatter=fm,
        source_path=path,
        mtime=mtime,
        user_invocable=bool(fm.get("user-invocable", False)),
        argument_hint=_as_list(fm.get("argument-hint")),
        allowed_tools=_as_list(fm.get("allowed-tools")),
        applies_to=_as_list(fm.get("applies-to")),
        kind_onboarding=(
            str(fm["kind-onboarding"]) if fm.get("kind-onboarding") else None
        ),
        tags=_as_list(fm.get("tags")),
    )


# ── Handler ──────────────────────────────────────────────────────────


class SkillHandler(Handler):
    """Handler for the ``skill:`` scheme — filesystem-backed SKILL.md.

    Reads from configured scan paths; writes to the first writable one
    (``~/.precis/skills/``).  Indexes skills in-memory with mtime-based
    invalidation.  No PG dependency.
    """

    scheme = "skill"
    writable = True
    views = {
        "meta": "_read_meta_view",
        "recent": "_read_recent_view",
        "kind": "_read_kind_view",
        "topic": "_read_topic_view",
    }
    allowed_modes = {"append", "replace", "note", "delete"}

    def __init__(
        self,
        scan_paths: list[Path] | None = None,
    ) -> None:
        self._scan_paths = scan_paths or _default_scan_paths()
        self._index: dict[str, Skill] = {}
        self._scan_mtime: dict[Path, float] = {}

    # ── Scan & index ─────────────────────────────────────────────────

    def _scan(self) -> None:
        """Populate ``self._index`` from disk.

        Precedence: earlier scan paths win on slug collisions.  Invalid
        skills (missing frontmatter, unparseable YAML) are logged and
        skipped without aborting the whole scan.
        """
        new_index: dict[str, Skill] = {}
        new_mtime: dict[Path, float] = {}
        for base in self._scan_paths:
            if not base.is_dir():
                continue
            try:
                mtime = base.stat().st_mtime
            except OSError:
                continue
            new_mtime[base] = mtime
            for entry in sorted(base.iterdir()):
                if not entry.is_dir():
                    continue
                md = entry / "SKILL.md"
                if not md.is_file():
                    continue
                skill = _parse_skill_md(md)
                if skill is None:
                    continue
                # Precedence: don't override an earlier-path winner.
                if skill.slug not in new_index:
                    new_index[skill.slug] = skill
        self._index = new_index
        self._scan_mtime = new_mtime

    def _ensure_fresh(self) -> None:
        """Scan on first use; rescan if any watched dir's mtime changed."""
        if not self._scan_mtime:
            self._scan()
            return
        for base, cached_mtime in self._scan_mtime.items():
            try:
                current = base.stat().st_mtime
            except OSError:
                current = 0.0
            if current != cached_mtime:
                self._scan()
                return

    # ── Read surface ─────────────────────────────────────────────────

    def read(
        self,
        path: str,
        selector: str | None,
        view: str | None,
        subview: str | None,
        query: str,
        summarize: bool,
        depth: int,
        page: int,
        **kwargs,
    ) -> str:
        self._ensure_fresh()

        # Bare call: search or list
        if not path and not view:
            if query:
                return self._search(query)
            return self._list_all()

        # Raw-path collection-view form used by direct handler tests
        # (``path="/kind/quest"`` without pre-parsing).  Live MCP traffic
        # never arrives in this shape — :func:`precis.uri.parse` already
        # splits the leading ``/`` into the view/subview triplet below.
        if path.startswith("/"):
            parts = path.lstrip("/").split("/", 1)
            v = parts[0]
            sub = parts[1] if len(parts) > 1 else None
            return self._dispatch_view(v, sub, **kwargs)

        # View dispatch.  Per-skill views carry the slug in ``path``
        # (``skill:find-paper/meta``); collection-level views arrive with
        # ``path=''`` because ``precis.uri.parse`` consumes the leading
        # ``/`` in ``skill:/kind/quest`` into the path/view split.
        if view:
            if path:
                return self._dispatch_view(view, subview, slug=path, **kwargs)
            return self._dispatch_view(view, subview, **kwargs)

        # Default: render the skill body
        return self._render_skill(path)

    def _dispatch_view(
        self,
        view: str,
        subview: str | None,
        **kwargs,
    ) -> str:
        method_name = self.views.get(view)
        if method_name is None:
            raise PrecisError(
                ErrorCode.VIEW_UNKNOWN,
                cause=f"view '/{view}' not supported on skill",
                options=sorted(self.views.keys()),
            )
        return getattr(self, method_name)(subview, **kwargs)

    # ── View dispatchers (uniform signature) ─────────────────────────

    def _read_meta_view(self, subview, **kwargs) -> str:
        slug = kwargs.pop("slug", None)
        extract_kwargs(kwargs, (), context="skill/meta")
        if not slug:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="skill slug required for /meta",
                next="get(id='skill:<slug>/meta')",
            )
        skill = self._require(slug)
        lines = [f"📋 skill:{skill.slug}", f"  name: {skill.name}", ""]
        lines.append("Frontmatter:")
        for key, value in sorted(skill.frontmatter.items()):
            lines.append(f"  {key}: {value}")
        lines.append("")
        # Same scan-path-relative form as ``_render_skill`` so the
        # /meta view doesn't leak the operator's home directory either.
        lines.append(f"Source: {self._relative_source(skill.source_path)}")
        return "\n".join(lines)

    def _read_recent_view(self, subview, **kwargs) -> str:
        (limit,) = extract_kwargs(kwargs, ("top_k",), context="skill/recent")
        n = int(limit) if limit else 20
        skills = sorted(self._index.values(), key=lambda s: -s.mtime)[:n]
        if not skills:
            return "No skills found."
        return self._format_listing(skills, header=f"📋 Recent skills ({len(skills)})")

    def _read_kind_view(self, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="skill/kind")
        if not subview:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="kind name required",
                next="get(id='skill:/kind/<kind>') e.g. skill:/kind/quest",
            )
        matching = [s for s in self._index.values() if subview in s.applies_to]
        if not matching:
            return f"No skills apply to kind '{subview}'."
        matching.sort(key=lambda s: s.slug)
        return self._format_listing(
            matching,
            header=f"📋 Skills for kind '{subview}' ({len(matching)})",
        )

    def _read_topic_view(self, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="skill/topic")
        if not subview:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="topic tag required",
                next="get(id='skill:/topic/<tag>') e.g. skill:/topic/papers",
            )
        matching = [s for s in self._index.values() if subview in s.tags]
        if not matching:
            return f"No skills tagged '{subview}'."
        matching.sort(key=lambda s: s.slug)
        return self._format_listing(
            matching,
            header=f"📋 Skills tagged '{subview}' ({len(matching)})",
        )

    # ── Rendering ────────────────────────────────────────────────────

    def _render_skill(self, slug: str) -> str:
        skill = self._require(slug)
        header = f"📋 skill:{skill.slug} — {skill.name}"
        # Use the scan-path-relative form so the response doesn't leak
        # the operator's home directory into every transcript / log.
        # Falls back to the basename if the source isn't under any
        # registered scan path (e.g. test fixtures with absolute
        # paths).  Review 2026-04-25 mcp-critic finding M (skill
        # source path leak).
        tail = f"\n\nSource: {self._relative_source(skill.source_path)}"
        return f"{header}\n\n{skill.body}{tail}"

    def _relative_source(self, source_path: Path) -> str:
        """Render ``source_path`` relative to its scan-path root.

        Looks up the deepest matching ``self._scan_paths`` entry and
        returns the suffix below it (``builtin/clock-basics/SKILL.md``).
        Falls back to ``parent/SKILL.md`` when no scan path matches —
        still hides the absolute path while keeping the slug visible.
        """
        try:
            for base in self._scan_paths:
                try:
                    rel = source_path.relative_to(base)
                except ValueError:
                    continue
                # Prefix with the base directory's name so two skills
                # with the same slug from different scan paths stay
                # distinguishable in the response.
                return f"{base.name}/{rel}"
        except Exception:
            pass
        return f"{source_path.parent.name}/{source_path.name}"

    def _list_all(self) -> str:
        if not self._index:
            return (
                "No skills configured.\n\n"
                "Scan paths:\n  "
                + "\n  ".join(str(p) for p in self._scan_paths)
                + "\n\nCreate a skill:\n"
                "  put(type='skill', title='my-skill', text='...')"
            )
        skills = sorted(self._index.values(), key=lambda s: s.slug)
        return self._format_listing(skills, header=f"📋 Skills ({len(skills)})")

    def _search(self, query: str) -> str:
        """Grep over name + description with AND-across-tokens (v1;
        pgvector in v1.2).

        BUG-G regression — the previous implementation did a single
        ``needle in haystack`` check, so multi-word queries like
        ``'acquire paper'`` returned zero hits even when both words
        appeared in the same skill.  Tokenising on whitespace and
        requiring every token to appear somewhere in the
        ``name + description`` blob matches what the agent expects from
        a search verb.  Empty-token splits are dropped so
        ``query='  paper  '`` behaves like ``'paper'``.
        """
        tokens = [t.lower() for t in query.split() if t.strip()]
        if not tokens:
            return f"No skills match '{query}'."
        matches = []
        for s in self._index.values():
            blob = f"{s.name.lower()} {s.description.lower()}"
            if all(t in blob for t in tokens):
                matches.append(s)
        if not matches:
            return f"No skills match '{query}'."
        matches.sort(key=lambda s: s.slug)
        return self._format_listing(
            matches, header=f"📋 Matches for '{query}' ({len(matches)})"
        )

    def _format_listing(self, skills: list[Skill], *, header: str) -> str:
        lines = [header, ""]
        for s in skills:
            invoc = " [user-invocable]" if s.user_invocable else ""
            applies = f"  applies-to: {', '.join(s.applies_to)}" if s.applies_to else ""
            desc = s.description.replace("\n", " ").strip()
            lines.append(f"  skill:{s.slug}{invoc}")
            lines.append(f"    {desc[:200]}")
            if applies:
                lines.append(applies)
        lines.append("")
        lines.append("Next:")
        lines.append("  get(id='skill:<slug>')        — full skill body")
        lines.append("  get(id='skill:<slug>/meta')   — frontmatter detail")
        return "\n".join(lines)

    def _require(self, slug: str) -> Skill:
        skill = self._index.get(slug)
        if skill is None:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                cause=f"skill {slug!r} not found",
                options=sorted(self._index.keys())[:10],
                next="get(id='skill:/') to list all skills",
            )
        return skill

    # ── Write surface ────────────────────────────────────────────────

    def put(
        self,
        path: str,
        selector: str | None,
        text: str,
        mode: str,
        **kwargs,
    ) -> str:
        self._ensure_fresh()

        if mode in ("append", "replace"):
            return self._put_write(path, text, mode, **kwargs)
        if mode == "delete":
            return self._put_delete(path)
        if mode == "note":
            # v1 note mode: raise — notes land in v1.2 when PG schema lands.
            raise PrecisError(
                ErrorCode.MODE_UNSUPPORTED,
                cause="mode='note' on skill not yet available (Phase 12b v1.2)",
                next="edit the SKILL.md directly or use mode='replace'",
            )
        raise PrecisError(
            ErrorCode.MODE_UNSUPPORTED,
            cause=f"mode {mode!r} not supported on skill",
        )

    def _writable_root(self) -> Path:
        """The directory precis writes to (``~/.precis/skills/``)."""
        root = Path.home() / ".precis" / "skills"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _put_write(
        self,
        path: str,
        text: str,
        mode: str,
        **kwargs,
    ) -> str:
        (title,) = extract_kwargs(
            kwargs, ("title",), context=f"skill put mode={mode!r}"
        )
        if not text:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"text= required for mode={mode!r}",
            )

        if mode == "append":
            slug = path or (title if isinstance(title, str) else "") or ""
            if not slug:
                # Last-resort fallback: look in the posted frontmatter
                # for ``name:`` (canonical Agent Skills field) or
                # ``slug:`` (the convention the smoke-test plan uses).
                # Keeps the strict "some slug must be present somewhere"
                # guarantee while accepting both authoring conventions.
                fm, _body = _split_frontmatter(text)
                slug = str(fm.get("name") or fm.get("slug") or "")
            slug = slug.strip().lower().replace(" ", "-")
            if not slug:
                raise PrecisError(
                    ErrorCode.PARAM_INVALID,
                    cause=(
                        "skill slug required — pass id='skill:<slug>', "
                        "title='<slug>', or include 'name:' or 'slug:' "
                        "in the text's frontmatter"
                    ),
                )
            dest_dir = self._writable_root() / slug
            if dest_dir.exists():
                raise PrecisError(
                    ErrorCode.ID_AMBIGUOUS,
                    cause=f"skill {slug!r} already exists",
                    next=f"use put(id='skill:{slug}', mode='replace') to overwrite",
                )
            dest_dir.mkdir(parents=True, exist_ok=False)
            dest = dest_dir / "SKILL.md"
            dest.write_text(text, encoding="utf-8")
            self._scan()
            return f"+ skill:{slug}\n  {dest}\n  ({len(text)} bytes)"

        # mode == "replace"
        if not path:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="id= required for mode='replace'",
            )
        skill = self._require(path)
        if not str(skill.source_path).startswith(str(self._writable_root())):
            raise PrecisError(
                ErrorCode.DENIED,
                cause=(
                    f"cannot edit {skill.slug!r}: source is outside "
                    f"~/.precis/skills/ ({skill.source_path.parent})"
                ),
                next=(
                    "copy the skill to ~/.precis/skills/ first "
                    "or edit the source file directly"
                ),
            )
        skill.source_path.write_text(text, encoding="utf-8")
        self._scan()
        return f"~ skill:{skill.slug}\n  {skill.source_path}\n  ({len(text)} bytes)"

    def _put_delete(self, path: str) -> str:
        if not path:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="id= required for mode='delete'",
            )
        skill = self._require(path)
        if not str(skill.source_path).startswith(str(self._writable_root())):
            raise PrecisError(
                ErrorCode.DENIED,
                cause=(
                    f"cannot delete {skill.slug!r}: source is outside "
                    f"~/.precis/skills/ ({skill.source_path.parent})"
                ),
            )
        skill.source_path.unlink()
        # Remove the containing directory if empty
        parent = skill.source_path.parent
        try:
            parent.rmdir()
        except OSError:
            pass
        self._scan()
        return f"- skill:{skill.slug}\n  removed"
