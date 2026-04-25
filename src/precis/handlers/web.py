"""WebHandler — stored website bookmarks with summaries + tags.

Phase 1 of :doc:`docs/websites-plan`.  The ``web:`` scheme replaces
the Perplexity-live-search kind (now ``websearch:``) with a curated
library of links + user/agent-written summaries.  A future Phase 2
will add on-demand fetching.

Design choices:

- **URL in ``id`` on create.**  ``put(type='web', id='<URL>', text='<summary>')``
  creates a bookmark.  The handler canonicalises the URL, derives a
  readable slug (``web:github-com-foo-bar``), and stores the canonical
  URL in ``meta.url``.  Follow-up ops address the bookmark by slug.
- **Idempotent.**  A second create for the same canonical URL returns
  the existing slug rather than failing or duplicating.
- **Archive on by default.**  Wayback ``Save Page Now`` is triggered
  for every public URL unless the caller passes ``archive=False`` or
  the env var ``PRECIS_WEB_AUTO_ARCHIVE=0`` is set.  Private URLs
  (localhost, RFC1918, Tailscale CGNAT, ``.local``, …) are never
  archived regardless of the flag.  See :mod:`precis.web_archive`.
- **Shallow meta.**  ``url``, ``canonical_url``, ``kind``,
  ``captured_at``, ``wayback_url``, ``archive_skipped_reason``,
  ``status``.  Nothing clever; no fetch fields until Phase 2.

Agent usage::

    # Create
    put(type='web', id='https://github.com/foo/bar',
        text='Great CLI for X.', tags=['tool', 'dev'])

    # List / search
    get(id='web:/recent')
    get(id='web:/tags')
    get(id='web:/kinds')
    search(query='CLI', type='web')

    # Per-bookmark
    get(id='web:github-com-foo-bar')
    put(id='web:github-com-foo-bar', text='Updated summary.', mode='replace')
    put(id='web:github-com-foo-bar', text='Extra note.', mode='append')
    put(id='web:github-com-foo-bar', mode='delete')
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from precis.handlers._ref_base import RefHandler, _get_store, _parse_tags
from precis.protocol import ErrorCode, PrecisError, extract_kwargs
from precis.uri import SEP
from precis.url_canonical import (
    canonicalise_url,
    host_of,
    is_http_url,
    slug_from_url,
)
from precis.web_archive import archive_url

log = logging.getLogger(__name__)

#: Per-kind tag inference rules.  Applied in declared order; first match
#: wins.  Keep the list short and host-based; path-based heuristics
#: (e.g. github.com/<user>/<repo> → ``repo``) run after these in
#: :func:`_infer_kind`.
_HOST_KINDS: tuple[tuple[str, str], ...] = (
    ("youtube.com", "video"),
    ("youtu.be", "video"),
    ("vimeo.com", "video"),
    ("arxiv.org", "paper"),
    ("doi.org", "paper"),
    ("pubmed.ncbi.nlm.nih.gov", "paper"),
    ("scholar.google.com", "paper"),
)

#: Valid bookmark kinds — used for options= on PARAM_INVALID.
_VALID_KINDS: tuple[str, ...] = (
    "tool",
    "article",
    "repo",
    "db",
    "video",
    "paper",
    "other",
)

#: Max blocks preview in list/overview rendering.
_MAX_SUMMARY_PREVIEW = 200


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_meta(ref: dict) -> dict:
    """Return the JSON-parsed ``meta`` dict for a ref (defensive)."""
    raw = ref.get("meta") or ref.get("metadata") or {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _fmt_ts(raw: str | None) -> str:
    if not raw:
        return "—"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return raw


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _infer_kind(canonical_url: str) -> str:
    """Best-effort kind inference from a canonical URL.

    Returns one of :data:`_VALID_KINDS`.  Falls back to ``'other'`` when
    no rule matches — the user can always override via ``kind=`` on put.
    """
    host = host_of(canonical_url)
    if not host:
        return "other"

    for suffix, kind in _HOST_KINDS:
        if host == suffix or host.endswith("." + suffix):
            return kind

    # GitHub: the bare host matched above but we want finer granularity.
    if host == "github.com":
        # /<user>/<repo> → repo; /<user> → other (profile).
        pass  # handled below once we look at the path
    path = canonical_url.split(host, 1)[-1].lstrip("/")
    segs = [s for s in path.split("/") if s]

    if host == "github.com":
        if len(segs) >= 2 and segs[1] not in ("topics", "search"):
            return "repo"
        return "other"

    if host == "news.ycombinator.com":
        return "article"

    # Very generic: anything with a ``/blog/`` or ``/posts/`` segment is
    # probably an article.  Not exhaustive — users/agents can override.
    if any(s in ("blog", "posts", "article") for s in segs):
        return "article"

    return "other"


def _normalise_tags(tags) -> list[str]:
    """Accept ``tags=['a', 'b']`` or ``tags='a, b,c'`` and return a list."""
    if not tags:
        return []
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    return [str(t).strip() for t in tags if str(t).strip()]


# ─────────────────────────────────────────────────────────────────────
# Handler
# ─────────────────────────────────────────────────────────────────────


class WebHandler(RefHandler):
    """Handler for the ``web:`` scheme — stored website bookmarks.

    See module docstring for the API contract.  Mostly parallel to
    :class:`precis.handlers.memory.MemoryHandler` with URL-specific
    slug derivation, archive.org integration, and kind inference.
    """

    scheme = "web"
    writable = True
    corpus_id = "websites"
    views = {
        **RefHandler.views,
        "recent": "_read_recent_view",
        "tags": "_read_tags_view",
        "kinds": "_read_kinds_view",
    }
    allowed_modes = {"append", "add", "replace", "delete", "note"}
    extensions: set[str] = set()

    _ref_noun = "bookmark"
    _ref_emoji = "🔖"
    _slug_prefix = "web"

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
        **kwargs,
    ) -> str:
        """Dispatch ``web:/recent``, ``/tags``, ``/kinds`` collection views.

        Delegates per-ref reads, search, and grep to
        :class:`RefHandler`.  Mirrors :class:`MemoryHandler.read`.
        """
        store = _get_store()

        # Bare ``web:`` — landing page.
        if (not path or path == "/") and not view and not selector and not query:
            return self._list_overview(store)

        # Collection-level views reachable via ``web:/<view>`` which
        # arrives here as ``path='/<view>'`` or ``view='<view>'``.
        if path in ("/recent", "recent") or view == "recent":
            limit_raw = kwargs.get("top_k") or 20
            try:
                limit = int(limit_raw)
            except (TypeError, ValueError):
                limit = 20
            return self._read_recent(store, limit=limit)

        if path in ("/tags", "tags") or view == "tags":
            return self._read_tags(store)

        if path in ("/kinds", "kinds") or view == "kinds":
            return self._read_kinds(store)

        return super().read(
            path, selector, view, subview, query, summarize, depth, page, **kwargs
        )

    # ── View dispatchers (uniform signature) ─────────────────────────

    def _read_recent_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="web/recent")
        return self._read_recent(store)

    def _read_tags_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="web/tags")
        return self._read_tags(store)

    def _read_kinds_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="web/kinds")
        return self._read_kinds(store)

    # ── Overview rendering ───────────────────────────────────────────

    def _read_overview(self, store, ref: dict) -> str:
        slug = ref.get("slug", "???")
        title = ref.get("title", "")
        meta = _parse_meta(ref)
        tags = _parse_tags(ref)

        url = meta.get("url") or meta.get("canonical_url") or ""
        kind = meta.get("kind", "other")
        status = meta.get("status", "ok")
        captured = meta.get("captured_at") or ref.get("first_seen_at")
        wayback = meta.get("wayback_url")
        archive_skip = meta.get("archive_skipped_reason")

        lines: list[str] = [f"{self._ref_emoji} {slug}  [{kind}]"]
        if title:
            lines.append(f"   {title}")
        if url:
            lines.append(f"   {url}")
        if tags:
            lines.append(f"   tags: {', '.join(tags)}")
        lines.append(f"   captured: {_fmt_ts(str(captured))}")
        if wayback:
            lines.append(f"   archived: {wayback}")
        elif archive_skip:
            lines.append(f"   archive: skipped ({archive_skip})")
        if status and status != "ok":
            lines.append(f"   status: {status}")
        if meta.get("deleted"):
            lines.append("   [deleted]")
        lines.append("")

        # Summary preview (first text block).
        try:
            blocks = store.get_blocks(slug, block_type="text")
        except Exception:
            blocks = []
        if blocks:
            preview = (blocks[0].get("text") or "").strip()
            if preview:
                lines.append(preview)
                lines.append("")

        lines.append("Next:")
        lines.append(
            f"  put(id='{slug}', text='…', mode='replace')   — rewrite summary"
        )
        lines.append(
            f"  put(id='{slug}', text='…', mode='append')    — add a note"
        )
        lines.append(f"  get(id='{slug}{SEP}0..5')                   — read blocks")
        lines.append(f"  put(id='{slug}', mode='delete')              — remove")
        return "\n".join(lines)

    def _list_overview(self, store) -> str:
        """Top-level ``web:`` landing — counts + recent + tag/kind peek."""
        refs = self._query_corpus_refs(store)
        if not refs:
            return (
                "🔖 No bookmarks yet.\n\n"
                "Create one:\n"
                "  put(type='web', id='https://<URL>', text='<summary>',\n"
                "      tags=['tool', 'dev'])\n"
            )

        lines = [f"🔖 {len(refs)} bookmarks", ""]
        lines.append("Recent (top 5):")
        for r in refs[:5]:
            lines.append(self._list_entry(r))
        lines.append("")

        # Kind histogram — small inline.
        kinds: dict[str, int] = {}
        for r in refs:
            k = _parse_meta(r).get("kind", "other")
            kinds[k] = kinds.get(k, 0) + 1
        if kinds:
            parts = [f"{k}({n})" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])]
            lines.append("Kinds: " + ", ".join(parts))

        # Tag histogram (top 8).
        tag_counts: dict[str, int] = {}
        for r in refs:
            for t in r.get("tags") or []:
                tag_counts[t] = tag_counts.get(t, 0) + 1
        if tag_counts:
            top = sorted(tag_counts.items(), key=lambda kv: -kv[1])[:8]
            lines.append("Tags:  " + ", ".join(f"{t}({n})" for t, n in top))
        lines.append("")

        lines.append("Next:")
        lines.append("  get(id='web:/recent')   — last 20")
        lines.append("  get(id='web:/tags')     — full tag histogram")
        lines.append("  get(id='web:/kinds')    — full kind histogram")
        lines.append("  search(query='…', type='web')")
        return "\n".join(lines)

    def _list_entry(self, ref: dict) -> str:
        slug = ref.get("slug", "???")
        meta = _parse_meta(ref)
        kind = meta.get("kind", "other")
        url = meta.get("url", "")
        created = ref.get("first_seen_at") or meta.get("captured_at") or "?"
        host = host_of(url) if url else ""
        return f"  {_fmt_ts(str(created))}  {slug}  [{kind}]  {host}"

    def _list_header(self, count: int, grep: str = "") -> str:
        extra = f" (grep={grep!r})" if grep else ""
        return f"🔖 {count} bookmarks{extra}"

    # ── /recent view ─────────────────────────────────────────────────

    def _read_recent(self, store, *, limit: int = 20) -> str:
        refs = self._query_corpus_refs(store)
        if not refs:
            return (
                "🔖 No bookmarks yet.\n\n"
                "Create one: put(type='web', id='https://<URL>', text='<summary>')"
            )
        recent = refs[:limit]
        lines = [f"🔖 {len(recent)} recent bookmarks (of {len(refs)} total)", ""]
        for r in recent:
            lines.append(self._list_entry(r))
        return "\n".join(lines)

    # ── /tags view ───────────────────────────────────────────────────

    def _read_tags(self, store) -> str:
        refs = self._query_corpus_refs(store)
        counts: dict[str, int] = {}
        for r in refs:
            for t in r.get("tags") or []:
                counts[t] = counts.get(t, 0) + 1
        if not counts:
            return "🔖 No tagged bookmarks yet."
        lines = [f"🔖 tags ({len(counts)} distinct)", ""]
        for tag, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {n:>3}  {tag}")
        return "\n".join(lines)

    # ── /kinds view ──────────────────────────────────────────────────

    def _read_kinds(self, store) -> str:
        refs = self._query_corpus_refs(store)
        counts: dict[str, int] = {}
        for r in refs:
            k = _parse_meta(r).get("kind", "other")
            counts[k] = counts.get(k, 0) + 1
        if not counts:
            return "🔖 No bookmarks yet."
        lines = [f"🔖 kinds ({len(counts)} distinct)", ""]
        for k, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {n:>3}  {k}")
        return "\n".join(lines)

    # ── Write dispatch ───────────────────────────────────────────────

    def put(
        self,
        path: str,
        selector: str | None,
        text: str,
        mode: str,
        **kwargs,
    ) -> str:
        store = _get_store()

        if mode in ("append", "add", "create"):
            return self._create_or_append(store, path, text, **kwargs)

        if mode == "replace":
            return self._replace_summary(store, path, text, **kwargs)

        if mode == "delete":
            return self._delete_bookmark(store, path)

        # Fall through to RefHandler for ``note`` (annotations on blocks).
        return super().put(path, selector, text, mode, **kwargs)

    # ── Create / append ──────────────────────────────────────────────

    def _create_or_append(self, store, path: str, text: str, **kwargs) -> str:
        """Either create a new bookmark or append a note to an existing one.

        Decision rule:

        - ``path`` looks like an http(s) URL → create (or return
          existing slug for idempotency).
        - ``path`` starts with ``web:`` → append a note block to the
          existing ref.
        - ``path`` empty → try to extract URL from the first line of
          ``text``; create if found.
        """
        # Case 1: path is a URL → create path.
        if path and is_http_url(path):
            return self._create_bookmark(store, url=path, summary=text, **kwargs)

        # Case 2: empty path but text starts with a URL → create path.
        if not path and text:
            first_line = text.splitlines()[0].strip()
            if is_http_url(first_line):
                summary = "\n".join(text.splitlines()[1:]).strip()
                return self._create_bookmark(
                    store, url=first_line, summary=summary, **kwargs
                )

        # Case 3: path is a web: slug → append a note block.
        if path:
            slug = path if path.startswith("web:") else f"web:{path}"
            return self._append_note(store, slug, text)

        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=(
                "web: creation needs a URL in id= or on the first line of text=. "
                "Example: put(type='web', id='https://example.com/', text='summary')"
            ),
            next=(
                "put(type='web', id='https://<URL>', text='<summary>', "
                "tags=['tool'])"
            ),
        )

    def _create_bookmark(
        self,
        store,
        *,
        url: str,
        summary: str,
        **kwargs,
    ) -> str:
        """Canonicalise + slug + dedupe + store + archive a bookmark."""
        try:
            canonical = canonicalise_url(url)
        except ValueError as exc:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"web: invalid URL — {exc}",
                next="URL must have an http:// or https:// scheme and a host",
            ) from exc

        # Idempotency: if we already have a bookmark for this canonical
        # URL, return it without creating a duplicate.  Looks up by meta
        # rather than by slug so URL-rewrites (e.g. a different tracking
        # stripping pass) still de-dupe.
        existing = self._find_by_canonical_url(store, canonical)
        if existing is not None:
            return (
                f"🔖 Already bookmarked: {existing.get('slug')}\n"
                f"   {canonical}\n\n"
                "Next:\n"
                f"  get(id='{existing.get('slug')}')            — view\n"
                f"  put(id='{existing.get('slug')}', text='…', mode='replace') "
                "— update summary"
            )

        # Slug derivation + collision disambiguation.
        base = slug_from_url(canonical)
        if not base:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"web: could not derive a slug from URL {url!r}",
            )
        slug = f"web:{base}"

        title = kwargs.get("title", "").strip()
        kind = kwargs.get("kind") or _infer_kind(canonical)
        if kind not in _VALID_KINDS:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"web: kind={kind!r} not recognised",
                options=list(_VALID_KINDS),
            )
        tags = _normalise_tags(kwargs.get("tags"))

        # Archive.org call (best-effort, before DB write so meta is
        # populated atomically).  The ``archive`` kwarg is forwarded by
        # ``server.put()`` — None means "use env default".
        archive_flag = kwargs.get("archive")
        archive_result = archive_url(canonical, requested=archive_flag)

        meta: dict = {
            "url": url,
            "canonical_url": canonical,
            "kind": kind,
            "status": "ok",
            "captured_at": _now_iso(),
            "wayback_url": archive_result.wayback_url,
        }
        if archive_result.skipped_reason is not None:
            meta["archive_skipped_reason"] = archive_result.skipped_reason.value
            if archive_result.detail:
                meta["archive_skipped_detail"] = archive_result.detail

        blocks = (
            [{"text": summary, "block_type": "text", "section_path": []}]
            if summary
            else []
        )

        # Disambiguate slug on collision (deterministic -a, -b, -c suffix).
        base_slug = slug
        suffix = 0
        while True:
            try:
                store.create_ref(
                    slug=slug,
                    corpus_id=self.corpus_id,
                    title=title or _truncate(summary, 120) or host_of(canonical),
                    metadata=meta,
                    tags=tags if tags else None,
                    blocks=blocks,
                )
                break
            except ValueError as exc:
                msg = str(exc).lower()
                if "already exists" in msg and suffix < 26:
                    suffix += 1
                    slug = f"{base_slug}-{chr(96 + suffix)}"
                    continue
                raise PrecisError(
                    ErrorCode.ID_AMBIGUOUS,
                    cause=f"web: could not create '{slug}': {exc}",
                ) from exc

        # Build the response.
        lines = [f"🔖 Bookmarked: {slug}  [{kind}]"]
        lines.append(f"   {canonical}")
        if tags:
            lines.append(f"   tags: {', '.join(tags)}")
        if archive_result.ok:
            lines.append(f"   archived: {archive_result.wayback_url}")
        elif archive_result.skipped_reason is not None:
            lines.append(
                f"   archive: skipped ({archive_result.skipped_reason.value})"
            )
        lines.append("")
        lines.append("Next:")
        lines.append(f"  get(id='{slug}')                        — view")
        lines.append(
            f"  put(id='{slug}', text='…', mode='replace') — update summary"
        )
        return "\n".join(lines)

    def _append_note(self, store, slug: str, text: str) -> str:
        """Append a note block to an existing bookmark."""
        if not text:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="web: text= required when appending to an existing bookmark",
            )
        ref = store.get(slug)
        if ref is None:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                cause=f"web: no bookmark {slug!r}",
                next=(
                    "List existing: get(id='web:/recent').  "
                    "Create new:   put(type='web', id='<URL>', text='…')"
                ),
            )
        try:
            store.add_block(slug, text=text, block_type="text")
        except ValueError as exc:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                cause=str(exc),
            ) from exc
        return (
            f"🔖 Note appended to {slug}\n"
            f"   {_truncate(text, _MAX_SUMMARY_PREVIEW)}"
        )

    # ── Replace summary ──────────────────────────────────────────────

    def _replace_summary(self, store, path: str, text: str, **kwargs) -> str:
        if not path:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="web: id= required for replace",
            )
        if not text:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="web: text= required for replace",
            )
        slug = path if path.startswith("web:") else f"web:{path}"
        ref = store.get(slug)
        if ref is None:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                cause=f"web: no bookmark {slug!r}",
                next="put(type='web', id='<URL>', text='…', mode='append') to create",
            )

        # Replace the first text block (the summary).
        blocks = store.get_blocks(slug, block_type="text")
        if blocks:
            node_id = blocks[0].get("node_id")
            if node_id:
                store.update_block_text(slug, node_id, text)
        else:
            # No summary block yet (rare — a bookmark created with
            # empty text).  Add one.
            store.add_block(slug, text=text, block_type="text")

        # Optionally update tags if provided.
        tags = kwargs.get("tags")
        if tags is not None:
            # update_ref_metadata doesn't touch tags; skip for now.
            # Tags live on Ref.tags, not in meta.  A full tag-edit
            # surface is out of Phase 1 scope.
            pass

        return f"🔖 Summary replaced: {slug}"

    # ── Delete ───────────────────────────────────────────────────────

    def _delete_bookmark(self, store, path: str) -> str:
        if not path:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="web: id= required for delete",
            )
        slug = path if path.startswith("web:") else f"web:{path}"
        ref = store.get(slug)
        if ref is None:
            raise PrecisError(
                ErrorCode.ID_NOT_FOUND,
                cause=f"web: no bookmark {slug!r}",
            )
        meta = _parse_meta(ref)
        meta["deleted"] = True
        meta["deleted_at"] = _now_iso()
        store.update_ref_metadata(slug, meta, merge=True)
        return (
            f"🔖 Bookmark soft-deleted: {slug}\n"
            "(Hidden from /recent, /tags, /kinds.  Content preserved.)"
        )

    # ── Corpus query helpers ─────────────────────────────────────────

    def _query_corpus_refs(self, store) -> list[dict]:
        """Return all non-deleted bookmarks, newest first."""
        try:
            from acatome_store.models import Ref
            from sqlalchemy import select
        except ImportError as exc:
            raise PrecisError(
                ErrorCode.KIND_UNAVAILABLE,
                cause="web: acatome-store not installed",
                next="pip install precis-mcp[paper]",
            ) from exc

        with store._Session() as session:
            stmt = (
                select(Ref)
                .where(Ref.corpus_id == self.corpus_id)
                .order_by(Ref.first_seen_at.desc())
            )
            rows = session.execute(stmt).scalars().all()
            results: list[dict] = []
            for r in rows:
                d = r.to_dict()
                meta = _parse_meta(d)
                if meta.get("deleted"):
                    continue
                d["tags"] = _parse_tags(d)
                results.append(d)
            return results

    def _find_by_canonical_url(self, store, canonical: str) -> dict | None:
        """Return the ref for ``canonical`` if it exists (idempotency)."""
        refs = self._query_corpus_refs(store)
        for r in refs:
            meta = _parse_meta(r)
            if meta.get("canonical_url") == canonical:
                return r
        return None
