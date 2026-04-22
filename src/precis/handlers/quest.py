"""Quest kind — paper-request lifecycle.

Folds ``acatome-quest-mcp``'s four-tool surface (submit / status / update /
submit_file) into precis as a state-backed kind.  Phase 12a ships the
**read surface only**; writes land in Phase 12b, MCP-layer retirement +
``cluster.quest.*`` schema promotion in Phase 12c.

Design notes:

- ``QuestHandler`` subclasses :class:`Handler` directly (not
  :class:`RefHandler`) because quest records are UUID-keyed, jsonb-heavy,
  and have no block/slug structure — the RefHandler plumbing around
  ``store.get_blocks()`` would be dead weight.
- Fully sync stack (April 2026): ``acatome-quest-mcp`` was rewritten to
  ``psycopg3`` + ``psycopg_pool``, dropping the former ``asyncpg`` layer.
  No bridge, no event loop, no ``asyncio.run``.
- Connection pool is held on the handler instance (``self._db``) and
  built on first use.  The registry caches one ``QuestHandler`` per
  process (see :func:`precis.registry.resolve`), so the pool is warm
  after the first tool call and reused thereafter.  ``import precis``
  stays cheap because construction doesn't touch PG.  Tests inject a
  mock via the ``db=`` constructor parameter.
- The handler is ``ImportError``-gated in ``registry.py`` — agents with a
  lean install never see the kind, no broken imports on startup.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from precis.protocol import ErrorCode, Handler, PrecisError, extract_kwargs

if TYPE_CHECKING:
    from acatome_quest_mcp.db import DB
    from acatome_quest_mcp.models import PaperRequest

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


_STATUS_EMOJI: dict[str, str] = {
    "queued": "⏳",
    "resolving": "🔎",
    "found_in_store": "📚",
    "needs_user": "❓",
    "fetching": "⬇",
    "ingesting": "⚙",
    "ingested": "✓",
    "extract_failed": "✗",
    "failed": "✗",
    "cancelled": "⊘",
}

_SEVERITY_EMOJI: dict[str, str] = {
    "critical": "🔴",
    "major": "🟠",
    "minor": "🟡",
    "info": "🔵",
}


def _short_id(uid: UUID | str) -> str:
    """First 8 chars of a UUID — enough to disambiguate within an agent session."""
    return str(uid).split("-")[0]


def _fmt_authors(authors: list[str] | None, max_n: int = 3) -> str:
    if not authors:
        return ""
    if len(authors) <= max_n:
        return ", ".join(authors)
    return f"{', '.join(authors[:max_n])}, et al."


def _fmt_ts(ts: datetime | None) -> str:
    if ts is None:
        return ""
    return ts.strftime("%Y-%m-%d %H:%M")


def _render_card(req: PaperRequest) -> str:
    """Compact multi-line card for a single request."""
    r = req.resolved
    i = req.input
    emoji = _STATUS_EMOJI.get(req.status.value, "•")

    title = r.title or i.title or "(untitled)"
    authors = _fmt_authors(r.authors or i.authors)
    year = r.year or i.year
    doi = r.doi or i.doi
    arxiv = r.arxiv or i.arxiv

    lines = [f"{emoji} quest:{_short_id(req.id)} — {req.status.value}"]
    lines.append(f"  {title}")
    meta_bits = []
    if authors:
        meta_bits.append(authors)
    if year:
        meta_bits.append(str(year))
    if meta_bits:
        lines.append(f"  {' · '.join(meta_bits)}")
    if doi:
        lines.append(f"  doi: {doi}")
    elif arxiv:
        lines.append(f"  arxiv: {arxiv}")

    if req.priority:
        lines.append(f"  priority: {req.priority}")
    if req.created_by:
        lines.append(f"  from: {req.created_by}")
    if req.source:
        doc = req.source.get("document")
        line = req.source.get("line")
        if doc:
            src = f"  source: {doc}"
            if line:
                src += f":{line}"
            lines.append(src)

    if req.candidates:
        lines.append(
            f"  {len(req.candidates)} candidates — get(id='quest:{_short_id(req.id)}/candidates')"
        )
    if req.misconceptions:
        codes = ", ".join(m.code.value for m in req.misconceptions)
        lines.append(f"  misconceptions: {codes}")

    if req.last_error:
        lines.append(f"  last error: {req.last_error}")

    lines.append(f"  updated: {_fmt_ts(req.updated_at)}")
    return "\n".join(lines)


def _render_list(requests: list[PaperRequest], heading: str) -> str:
    """Short-form multi-request listing."""
    if not requests:
        return f"{heading} (0)\n\n(empty — no matching quests)"

    lines = [f"{heading} ({len(requests)})", ""]
    for req in requests:
        r = req.resolved
        i = req.input
        emoji = _STATUS_EMOJI.get(req.status.value, "•")
        title = r.title or i.title or "(untitled)"
        authors = _fmt_authors(r.authors or i.authors, max_n=2)

        # One line summary.
        line = f"  {emoji} quest:{_short_id(req.id)} {title[:60]}"
        if len(title) > 60:
            line += "…"
        lines.append(line)

        bits = []
        if authors:
            bits.append(authors)
        if req.status.value == "needs_user" and req.candidates:
            bits.append(f"{len(req.candidates)} candidates")
        if req.misconceptions:
            bits.append(f"{len(req.misconceptions)} flags")
        if bits:
            lines.append(f"     ({' · '.join(bits)})")
    return "\n".join(lines)


def _render_candidates(req: PaperRequest) -> str:
    """Ambiguity-resolution view for a needs_user request."""
    if not req.candidates:
        return (
            f"quest:{_short_id(req.id)} — no candidates\n\n"
            f"Status: {req.status.value}. "
            f"This request is not waiting on disambiguation."
        )

    lines = [f"quest:{_short_id(req.id)} — {len(req.candidates)} candidates", ""]
    for idx, cand in enumerate(req.candidates):
        r = cand.ref
        title = r.title or "(untitled)"
        authors = _fmt_authors(r.authors, max_n=3)
        year = f" ({r.year})" if r.year else ""
        lines.append(f"  [{idx}] {title}{year}")
        if authors:
            lines.append(f"      {authors}")
        if r.doi:
            lines.append(f"      doi: {r.doi}")
        if r.score:
            lines.append(f"      score: {r.score:.2f}  source: {r.source}")
        if cand.reason:
            lines.append(f"      reason: {cand.reason}")
        lines.append("")

    lines.append("Next:")
    lines.append(
        f"  put(id='quest:{_short_id(req.id)}', mode='confirm', choice=<n>) — pick one"
    )
    lines.append(
        f"  put(id='quest:{_short_id(req.id)}', mode='repoint', doi='…') — override DOI"
    )
    lines.append(f"  put(id='quest:{_short_id(req.id)}', mode='cancel') — abandon")
    return "\n".join(lines)


def _render_misconceptions(req: PaperRequest) -> str:
    """Show all misconception flags attached to a request."""
    if not req.misconceptions:
        return (
            f"quest:{_short_id(req.id)} — no misconceptions\n\n"
            f"This request has a clean record."
        )

    lines = [
        f"quest:{_short_id(req.id)} — {len(req.misconceptions)} misconceptions",
        "",
    ]
    for m in req.misconceptions:
        sev_emoji = _SEVERITY_EMOJI.get(m.severity.value, "•")
        lines.append(f"  {sev_emoji} {m.code.value} ({m.severity.value})")
        if m.evidence:
            lines.append(f"      {m.evidence}")
        lines.append(f"      source: {m.source}  at: {_fmt_ts(m.created_at)}")
        lines.append("")

    lines.append("Next:")
    lines.append(
        f"  put(id='quest:{_short_id(req.id)}', mode='flag', code='…', evidence='…')"
    )
    lines.append("  see get(id='skill:quest-disambiguate') for how to act on each code")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class QuestHandler(Handler):
    """Handler for ``quest:`` scheme — paper-request queue (read-only in 12a).

    URI grammar::

        quest:                         — bare list (recent requests)
        quest:<uuid8>                  — single request card
        quest:<uuid8>/candidates       — disambiguation options
        quest:<uuid8>/misconceptions   — attached flags
        quest:/recent                  — most-recent, any status
        quest:/queued                  — waiting for runner
        quest:/needs-user              — awaiting disambiguation / repoint
        quest:/failed                  — failed + extract_failed
        quest:/ingesting               — in-flight downloads / extractions
        quest:/agent/<agent-id>        — everything created by one agent

    Short UUIDs (first 8 chars) are accepted so agents can copy-paste
    friendlier ids; collisions fall back to ``ID_AMBIGUOUS``.
    """

    scheme = "quest"
    writable = False  # Phase 12b will flip this
    onboarding_skill = "find-paper"

    def __init__(self, db: DB | None = None) -> None:
        """Construct a handler.

        ``db``:  Pre-built :class:`acatome_quest_mcp.db.DB` (used by tests
        to inject a fake).  When ``None``, the handler builds one from
        ``DATABASE_URL`` / ``QUEST_SCHEMA`` on first use — construction
        itself never touches PG so ``import precis`` stays cheap.
        """
        self._db: DB | None = db

    # View-name → dispatch-method-name.  Uniform signature:
    #   (self, **kwargs) -> str
    # where kwargs always include at least ``_path`` (the URL path segment
    # stripped of the scheme prefix).  Validation happens method-locally.
    views = {
        "recent": "_read_recent_view",
        "queued": "_read_queued_view",
        "needs-user": "_read_needs_user_view",
        "failed": "_read_failed_view",
        "ingesting": "_read_ingesting_view",
        "agent": "_read_agent_view",
        "candidates": "_read_candidates_view",
        "misconceptions": "_read_misconceptions_view",
        "help": "_read_help_view",
    }

    _DEFAULT_LIMIT = 50

    # ── Read dispatch ────────────────────────────────────────────────

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
    ) -> str:
        # Quest paths: ``path`` is the fragment after ``quest:``.
        # Examples:
        #   ""              → bare list
        #   "<uuid8>"       → single card
        #   "/recent"       → /recent view
        #   "/agent/asa"    → /agent/<id> view; subview='asa'
        #   "<uuid>/candidates" → selector handled below

        # Path-leading slash means a registry view (/recent, /queued, …).
        if path.startswith("/"):
            parts = path.lstrip("/").split("/", 1)
            view_name = parts[0]
            view_arg = parts[1] if len(parts) > 1 else None
            return self._dispatch_view(view_name, view_arg=view_arg, query=query)

        # Single-id path; may carry a sub-selector.
        if path:
            # Selector passed via ``selector`` kwarg (›path) takes precedence.
            # Also accept trailing /candidates or /misconceptions for URL-ish
            # convenience (quest:<id>/candidates).
            sub = selector
            id_token = path
            if not sub and "/" in path:
                id_token, sub = path.split("/", 1)

            if sub in ("candidates", "misconceptions"):
                return self._dispatch_view(sub, id_token=id_token)

            if view == "help":
                return self._read_help_view()

            return self._read_single(id_token)

        # Bare call — recent.
        if query:
            return self._read_query(query)
        return self._read_recent_view(limit=20)

    def _dispatch_view(
        self,
        view_name: str,
        *,
        id_token: str | None = None,
        view_arg: str | None = None,
        query: str = "",
    ) -> str:
        method_name = self.views.get(view_name)
        if method_name is None:
            raise PrecisError(
                ErrorCode.VIEW_UNKNOWN,
                cause=f"quest has no view '/{view_name}'",
            )
        kwargs: dict[str, Any] = {}
        if id_token is not None:
            kwargs["id_token"] = id_token
        if view_arg is not None:
            kwargs["agent"] = view_arg
        if query:
            kwargs["query"] = query
        return getattr(self, method_name)(**kwargs)

    # ── Single-id read ───────────────────────────────────────────────

    def _read_single(self, id_token: str) -> str:
        req = self._resolve_id(id_token)
        return _render_card(req)

    def _resolve_id(self, id_token: str) -> PaperRequest:
        """Accept a full UUID or the 8-char short form."""
        id_token = id_token.strip()
        if not id_token:
            raise PrecisError(
                ErrorCode.ID_MALFORMED,
                cause="empty quest id",
                next="get(type='quest', id='/recent') to list current backlog",
            )

        # Full UUID — direct lookup.
        try:
            uid = UUID(id_token)
        except ValueError:
            uid = None

        if uid is not None:
            req = self._db_get(uid)
            if req is None:
                raise PrecisError(
                    ErrorCode.ID_NOT_FOUND,
                    cause=f"no quest with id {uid}",
                    next="get(type='quest', id='/recent') to list current backlog",
                )
            return req

        # Short-form (8 hex chars) — scan recent rows for a match.
        if len(id_token) < 6 or not all(
            c in "0123456789abcdef-" for c in id_token.lower()
        ):
            raise PrecisError(
                ErrorCode.ID_MALFORMED,
                cause=f"'{id_token}' is not a UUID or 8-char prefix",
                next="quest ids look like 'a1b2c3d4' or a full UUID",
            )

        matches = self._db_find_by_prefix(id_token.lower())
        if not matches:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                cause=f"no quest id starts with '{id_token}'",
                next="get(type='quest', id='/recent') to list current backlog",
            )
        if len(matches) > 1:
            options = [_short_id(m.id) for m in matches[:5]]
            raise PrecisError(
                ErrorCode.ID_AMBIGUOUS,
                cause=f"'{id_token}' matches {len(matches)} requests",
                options=options,
                next="use a longer prefix or the full UUID",
            )
        return matches[0]

    # ── Views ────────────────────────────────────────────────────────

    def _read_recent_view(self, **kwargs) -> str:
        (limit,) = extract_kwargs(kwargs, ("limit",), context="quest/recent")
        limit_n = int(limit) if limit is not None else 20
        reqs = self._db_find(limit=limit_n)
        return _render_list(reqs, "Recent quests")

    def _read_queued_view(self, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="quest/queued")
        reqs = self._db_find(status="queued", limit=self._DEFAULT_LIMIT)
        return _render_list(reqs, "Queued quests")

    def _read_needs_user_view(self, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="quest/needs-user")
        reqs = self._db_find(status="needs_user", limit=self._DEFAULT_LIMIT)
        out = _render_list(reqs, "Quests awaiting user input")
        if reqs:
            out += (
                "\n\nNext:\n"
                "  see get(id='skill:quest-disambiguate') for the workflow\n"
                "  get(id='quest:<id>/candidates') for per-quest options"
            )
        return out

    def _read_failed_view(self, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="quest/failed")
        # Union of the two failure statuses.
        a = self._db_find(status="failed", limit=self._DEFAULT_LIMIT)
        b = self._db_find(status="extract_failed", limit=self._DEFAULT_LIMIT)
        merged = sorted(a + b, key=lambda r: r.updated_at, reverse=True)[
            : self._DEFAULT_LIMIT
        ]
        return _render_list(merged, "Failed quests")

    def _read_ingesting_view(self, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="quest/ingesting")
        # Union of fetching + ingesting — in-flight downloads and extractions.
        a = self._db_find(status="fetching", limit=self._DEFAULT_LIMIT)
        b = self._db_find(status="ingesting", limit=self._DEFAULT_LIMIT)
        merged = sorted(a + b, key=lambda r: r.updated_at, reverse=True)[
            : self._DEFAULT_LIMIT
        ]
        return _render_list(merged, "In-flight quests")

    def _read_agent_view(self, **kwargs) -> str:
        (agent,) = extract_kwargs(
            kwargs, ("agent",), required=("agent",), context="quest/agent"
        )
        reqs = self._db_find(created_by=agent, limit=self._DEFAULT_LIMIT)
        return _render_list(reqs, f"Quests from agent '{agent}'")

    def _read_candidates_view(self, **kwargs) -> str:
        (id_token,) = extract_kwargs(
            kwargs,
            ("id_token",),
            required=("id_token",),
            context="quest/<id>/candidates",
        )
        req = self._resolve_id(id_token)
        return _render_candidates(req)

    def _read_misconceptions_view(self, **kwargs) -> str:
        (id_token,) = extract_kwargs(
            kwargs,
            ("id_token",),
            required=("id_token",),
            context="quest/<id>/misconceptions",
        )
        req = self._resolve_id(id_token)
        return _render_misconceptions(req)

    def _read_help_view(self, **kwargs) -> str:
        """Inline the onboarding skill body (same pattern as RefHandler)."""
        extract_kwargs(kwargs, (), context="quest/help")
        if not self.onboarding_skill:
            raise PrecisError(
                ErrorCode.VIEW_UNKNOWN,
                cause="quest has no onboarding skill declared",
                next="search(type='skill', query='paper request') to find one",
            )
        from precis.handlers.skill import SkillHandler

        sh = SkillHandler()
        sh._ensure_fresh()
        try:
            return sh._render_skill(self.onboarding_skill)
        except PrecisError as exc:
            if exc.code is ErrorCode.ID_NOT_FOUND:
                raise PrecisError(
                    ErrorCode.ID_NOT_FOUND,
                    cause=(
                        f"quest declares onboarding_skill="
                        f"{self.onboarding_skill!r} but no SKILL.md for "
                        f"it is installed"
                    ),
                    next=(
                        f"create skill:{self.onboarding_skill} in "
                        f"~/.precis/skills/ or search(type='skill')"
                    ),
                ) from exc
            raise

    def _read_query(self, query: str) -> str:
        """Simple case-insensitive substring search over titles (v1).

        v1.2 replaces this with a pgvector index once the quest library
        grows past ~1k rows.
        """
        q = query.lower().strip()
        if not q:
            return self._read_recent_view(limit=20)
        reqs = self._db_find(limit=200)
        matches = []
        for r in reqs:
            title = (r.resolved.title or r.input.title or "").lower()
            if q in title:
                matches.append(r)
        return _render_list(matches, f"Quest search: '{query}'")

    # ── Search verb ──────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 5,
        scope: str = "",
        **kwargs: Any,
    ) -> str:
        """Search surface — same substring over titles as /query (v1)."""
        _ = kwargs  # reserved for future filter args (status, created_by)
        return self._read_query(query)

    # ── DB adaptors ──────────────────────────────────────────────────
    # Thin wrappers so tests can monkey-patch a single layer without
    # having to patch psycopg directly.

    def _get_db(self) -> DB:
        """Return the handler's DB, creating + connecting on first use."""
        if self._db is None:
            from acatome_quest_mcp.db import DB as _DB

            dsn = os.environ.get(
                "DATABASE_URL", "postgresql://localhost/cluster"
            )
            schema = os.environ.get("QUEST_SCHEMA", "papers")
            db = _DB(dsn, schema=schema)
            db.connect()
            self._db = db
        return self._db

    def _db_get(self, uid: UUID) -> PaperRequest | None:
        return self._get_db().get(uid)

    def _db_find(self, **kwargs: Any) -> list[PaperRequest]:
        return self._get_db().find(**kwargs)

    def _db_find_by_prefix(self, prefix: str) -> list[PaperRequest]:
        """Find requests whose UUID starts with ``prefix``.

        Not in the upstream DB API; implemented here via a raw SQL query
        against the pool.  Kept narrow — we only need it for short-id
        resolution.
        """
        db = self._get_db()
        assert db.pool is not None
        with db.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {db.schema}.requests "
                f"WHERE id::text LIKE %s "
                f"ORDER BY created_at DESC LIMIT 10",
                (f"{prefix}%",),
            )
            cols = [d.name for d in cur.description or []]
            rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
        from acatome_quest_mcp.db import _row_to_request

        return [r for r in (_row_to_request(row) for row in rows) if r is not None]
