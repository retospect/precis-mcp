"""``CfpHandler`` — call-for-proposal / requirements document kind.

A ``cfp`` is a *spec-role* sibling of :class:`~precis.handlers.paper.PaperHandler`
(proposal writing). It is an ingested, read-only PDF document — the
**identical** Marker → chunks pipeline papers use (``precis add --as cfp``
/ ``inbox/cfp/``) — so it gets embeddings, per-chunk keywords, TOC, and
in-document search for free. The only differences from a paper are
declared, not duplicated:

* ``corpus_role='spec'`` — a CFP is the *requirements* a proposal must
  satisfy, **never** citable evidence. The citation handler resolves
  ``source_handle`` only against ``kind='paper'``, so a CFP is
  non-citable by construction; the flag formalises it for the planner
  prompt and the reader chrome, and keeps CFPs out of
  ``search(kind='paper', …)`` (a different kind entirely).
* ``put`` is URL intake only (gr690299) — ``put(args={'url': …})`` mints
  a bodiless ``cfp`` ref with ``meta.source_url`` set, for a call that
  is a web page, so a ``WATCH:*`` tag has a page to re-check. It never
  takes the inherited paper stub path (the DOI/arXiv/title backlog
  would mint a *paper*) and it fetches nothing: the watcher fetches the
  page later through ``safe_get``, and a body still arrives only by PDF
  ingest. The URL is stored as given, like ``edit(args={'source_url':
  …})`` stores it, and a second put of the same URL returns the ref
  that already tracks it.
* Restricted view enum — citation-export views (``bibtex`` / ``ris`` /
  ``endnote`` / ``bibliography``) are dropped; a spec is not a
  bibliographic entry.
* Typed call dates — ``meta.deadline`` / ``meta.opens_on`` (ISO
  ``YYYY-MM-DD``, set via ``edit(args=…)``) and ``meta.source_url``
  (the page ``workers/cfp_watch.py`` re-checks). A queryless
  ``search`` browses the corpus deadline-sorted.

Everything else (``get`` overview + chunk rendering, ``search`` /
``search_hits``, ``edit`` metadata repair, ``tag`` / ``link``) is the
paper machinery verbatim, parameterised on ``self.spec.kind`` so it
addresses the ``cfp`` corpus.

The inverse of the proposal-project ``has-requirement`` link
(``requirement-of``) lives on the CFP node — see ``store/types.py``.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, Any, ClassVar

from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.protocol import ArgsSpec, KindSpec
from precis.response import Response
from precis.utils import handle_registry
from precis.utils.idem import idem_lock_key
from precis.utils.link_tag_ops import apply_tag_ops
from precis.utils.match_all import fold_match_all_q
from precis.utils.url import slug_from_url

if TYPE_CHECKING:
    from precis.store import Ref

_PUT_EXAMPLE = "put(kind='cfp', args={'url': 'https://example.org/call'})"


def _source_url(raw: Any, *, next: str) -> str:
    """The one ``meta.source_url`` check, shared by ``put`` and ``edit``:
    an http(s) URL, stored stripped and otherwise as given."""
    url = str(raw).strip()
    if not re.match(r"https?://[^\s/]+", url):
        raise BadInput("source_url must be an http(s) URL", next=next)
    return url


# Views a spec-role document supports. Mirrors paper's enum minus the
# citation-export formats (a CFP is never cited, so bibtex/ris/endnote/
# bibliography are meaningless). ``toc`` / ``abstract`` / ``health`` /
# ``log`` / ``abbrevs`` all carry over unchanged.
_CFP_VIEWS: tuple[str, ...] = (
    "abstract",
    "toc",
    "health",
    "log",
    "abbrevs",
)


class CfpHandler(PaperHandler):
    """Call-for-proposal / requirements document (spec role); put mints
    one from its URL, never a paper stub."""

    # A CFP isn't acquired via `precis jobs ingest-bundles` (that's the
    # paper backlog) — it's dropped into `inbox/cfp/`, added directly
    # with `precis add --as cfp`, or minted from its URL; the
    # empty-listing hint says so instead of leaking paper-pipeline
    # phrasing (gr311347 #12).
    _empty_corpus_message: ClassVar[str] = (
        "no CFPs cached yet - drop a PDF into `inbox/cfp/`, run "
        "`precis add --as cfp <file>`, or mint a call that is a web page "
        f"with {_PUT_EXAMPLE}"
    )

    spec: ClassVar[KindSpec] = KindSpec(
        kind="cfp",
        title="Call for Proposal",
        description=(
            "Call-for-proposal / requirements document. A read-only "
            "ingested PDF (via `precis add --as cfp` or the inbox/cfp/ "
            "watch dir) that a proposal draft must satisfy; a call that "
            "is a web page is minted with put(args={'url': 'https://…'}) "
            "(no body; sets meta.source_url for WATCH:*). Addressable "
            "by slug; one ref per document, blocks per chunk — gets "
            "search / TOC / keywords like a paper. Spec role: NEVER "
            "citable evidence (it is the requirements, not a source). "
            "Link it to a proposal project with "
            "link(rel='has-requirement') so the planner consults it. "
            "Use get(view='toc') to read the required sections + limits."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        # URL intake only (gr690299): ``put`` mints a bodiless cfp from
        # the call's web page — never a paper stub into the fetch
        # backlog. A body still arrives only by PDF ingest.
        supports_put=True,
        # Metadata repair (title / year / abstract) reuses paper's edit;
        # useful for the web meta editor.
        supports_edit=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        corpus_role="spec",
        placement="corpus",
        views=_CFP_VIEWS,
    )

    def accepted_views(self, *, id: Any = None) -> list[str]:
        # Spec docs expose the reading views only — no citation exports.
        return list(_CFP_VIEWS)

    # ``source_url`` is the page the WATCH re-check fetches
    # (workers/cfp_watch.py); ``deadline``/``opens_on`` are the typed
    # dates the kind exists for (gr462088 slice 1). All live in
    # ``refs.meta``. ``put`` reads only ``url`` — the page a new cfp is
    # minted for, stored as ``meta.source_url`` (gr690299).
    ARGS: ClassVar[dict[str, ArgsSpec]] = {
        "put": ArgsSpec({"url"}),
        "edit": ArgsSpec({"source_url", "deadline", "opens_on"}),
    }

    #: Queryless ``search(kind='cfp')`` sorts by deadline — a call's
    #: deadline is the reason to browse the corpus at all.
    _BROWSE_SORT_DEFAULT: ClassVar[str] = "deadline"

    #: The browse sorts are handler-owned; declare them so the surface
    #: sort check lets them through (gr481617).
    EXTRA_SORTS: ClassVar[frozenset[str]] = frozenset(PaperHandler._BROWSE_SORTS)

    def put(
        self,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
        args: dict[str, Any] | None = None,
        **kw: Any,
    ) -> Response:
        """URL intake (gr690299): mint a cfp for a call that is a web page.

        ``put(kind='cfp', args={'url': 'https://example.org/call'})`` mints
        a bodiless ``cfp`` ref — never a paper — with ``meta.source_url``
        set, the page a ``WATCH:*`` tag has ``workers/cfp_watch.py``
        re-check. ``title=`` names it (default: the URL) and ``tags=``
        tags it in the same transaction, so a refused tag mints nothing.
        Nothing is fetched here: the watcher fetches the page, and a body
        arrives only by PDF ingest. A URL a live cfp already tracks
        returns that ref (``reused``) and still applies ``tags=``.

        ``url`` is the only door: every paper put field (``doi=``,
        ``text=``, ``mode=``, …) is refused, so no call lands on the
        inherited stub path that would mint a paper.
        """
        from precis.ingest.db_writer import resolve_slug

        extras = args or {}
        raw = extras.get("url")
        if raw is None:
            raw = kw.get("url")
        unknown = sorted(k for k in kw if k != "url")
        if unknown:
            raise BadInput(
                f"put(kind='cfp') mints a call from its url= (plus title=, "
                f"tags=); it does not accept {unknown!r}",
                next=_PUT_EXAMPLE,
            )
        if raw is None:
            raise BadInput(
                "put(kind='cfp') needs url= — the call's web page; a PDF "
                "call is ingested with `precis add --as cfp <file>`",
                next=_PUT_EXAMPLE,
            )
        url = _source_url(raw, next=_PUT_EXAMPLE)
        name = str(title).strip() if title is not None else ""
        with self.store.tx() as conn:
            # Two puts of one URL serialise here, so the second sees the
            # first's ref instead of minting a twin.
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (idem_lock_key(f"cfp-url:{url}"),),
            )
            row = conn.execute(
                "SELECT ref_id FROM refs WHERE kind = 'cfp' "
                "AND retired_at IS NULL AND meta->>'source_url' = %s "
                "ORDER BY ref_id LIMIT 1",
                (url,),
            ).fetchone()
            created = row is None
            if row is None:
                slug = resolve_slug(slug_from_url(url) or "cfp", conn=conn)
                ref_id = self.store.insert_ref(
                    kind="cfp",
                    slug=slug,
                    title=name or url,
                    meta={"source_url": url},
                    conn=conn,
                ).id
            else:
                ref_id = int(row[0])
            if tags:
                apply_tag_ops(
                    self.store, "cfp", ref_id, tags=tags, untags=None, conn=conn
                )
        ref = self.store.get_ref(kind="cfp", id=ref_id)
        ident = (ref.slug if ref is not None else None) or ref_id
        handle = handle_registry.format_handle("cfp", ref_id)
        lines = [
            f"cfp {handle} [{ident}] minted for {url} — meta.source_url set; "
            "no body (the page is not fetched at put time)."
            if created
            else f"cfp {handle} [{ident}] already tracks {url} — nothing minted."
        ]
        if tags:
            lines.append(f"tagged: {', '.join(str(t).strip() for t in tags)}")
        if not any(str(t).strip().upper().startswith("WATCH:") for t in tags or ()):
            lines.append(
                "Watch the page for changes: "
                f"tag(kind='cfp', id={ident!r}, add=['WATCH:weekly'])"
            )
        if created:
            lines.append(
                f"Set the call's dates: edit(kind='cfp', id={ident!r}, "
                "args={'deadline': 'YYYY-MM-DD'})"
            )
        return Response(body="\n".join(lines), ref_id=ref_id, reused=not created)

    def _edit_args(
        self, ref_id: int, args: dict[str, Any] | None, *, dry: bool
    ) -> list[str]:
        """``edit(args=…)`` — ``source_url``: the page ``WATCH:*`` re-checks
        fetch (workers/cfp_watch.py); ``deadline``/``opens_on``: ISO
        ``YYYY-MM-DD`` dates (a bad value is a BadInput, nothing written);
        an empty string clears a date."""
        args = args or {}
        labels: list[str] = []
        url = args.get("source_url")
        if url is not None:
            url = _source_url(
                url,
                next="edit(kind='cfp', id=<slug|id>, "
                "args={'source_url': 'https://example.org/call'})",
            )
            if not dry:
                self.store.stamp_ref_meta(ref_id, {"source_url": url})
            labels.append(f"source_url={url}")
        patch: dict[str, Any] = {}
        for key in ("deadline", "opens_on"):
            if key not in args:
                continue
            raw = args[key]
            if raw is None or not str(raw).strip():
                patch[key] = None
                labels.append(f"{key} cleared")
                continue
            day = str(raw).strip()
            try:
                parsed = date.fromisoformat(day)
            except ValueError:
                parsed = None
            if parsed is None or parsed.isoformat() != day:
                raise BadInput(
                    f"{key} must be an ISO date (YYYY-MM-DD), got {raw!r}",
                    next=f"edit(kind='cfp', id=<slug|id>, "
                    f"args={{'{key}': '2026-11-15'}})",
                )
            patch[key] = day
            labels.append(f"{key}={day}")
        if patch and not dry:
            self.store.stamp_ref_meta(ref_id, patch)
        return labels

    def _overview_meta_lines(self, ref: Ref) -> list[str]:
        meta = ref.meta or {}
        return [
            f"{label}: {meta[key]}"
            for key, label in (
                ("deadline", "deadline"),
                ("opens_on", "opens on"),
                # The page a URL-minted call came from and WATCH:*
                # re-checks — the only content a bodiless cfp has.
                ("source_url", "source"),
            )
            if meta.get(key)
        ]

    def search(self, *, sort: str | None = None, **kw: Any) -> Response:
        """Cfp search — paper's block search, plus a queryless browse.

        ``sort`` is a browse-only key: ``'deadline'`` (the default —
        soonest first, undated calls last), ``'opens_on'`` or
        ``'recency'``. With ``q=`` ranking owns the order and ``sort=``
        is rejected. ``search(kind='cfp')`` bare enumerates the corpus.
        """
        kw["q"] = fold_match_all_q(kw.get("q"))
        # ``args={'unread'/'ingested_after'/'ingested_before'/
        # 'id_min'/'id_max'}`` (gr653528) — the read-state / id-bounded
        # cohort lives on the paper path: route there so an ingest- or
        # id-bound browse gets the real filter and ``unread`` refuses
        # as paper-only, rather than either dropping silently into the
        # deadline browse.
        if (
            kw.get("unread") is not None
            or kw.get("ingested_after") is not None
            or kw.get("ingested_before") is not None
            or kw.get("id_min") is not None
            or kw.get("id_max") is not None
        ):
            if sort is not None:
                raise BadInput(
                    "sort= is a queryless cfp browse key — the "
                    "unread/ingested listing orders by ref id",
                    next="drop args={'sort': …}",
                )
            return super().search(**kw)
        if (
            (kw.get("q") and str(kw["q"]).strip())
            or kw.get("title")
            or kw.get("author")
        ):
            if sort is not None:
                raise BadInput(
                    "sort= is a queryless cfp browse key — a block search "
                    "ranks by relevance",
                    next="search(kind='cfp', args={'sort': 'deadline'})",
                )
            return super().search(**kw)
        for flag in ("good", "queries", "answers", "per_paper"):
            if kw.get(flag):
                raise BadInput(
                    f"{flag}= needs q= — a queryless cfp browse has no ranking to tune",
                    next="search(kind='cfp', q='…') or drop the flag",
                )
        if kw.get("include_ref_ids") is not None or kw.get("exclude_ref_ids"):
            raise BadInput(
                "cited=/uncited= needs q= for a cfp search — a queryless "
                "browse can't apply the ref filter",
                next="search(kind='cfp', q='…', uncited=<draft>)",
            )
        chosen = (
            str(sort).strip().lower() if sort is not None else self._BROWSE_SORT_DEFAULT
        )
        if chosen not in self._BROWSE_SORTS:
            raise BadInput(
                f"sort= must be one of {sorted(self._BROWSE_SORTS)}, got {sort!r}",
                next="search(kind='cfp', args={'sort': 'deadline'})",
            )
        return self._render_tags_only_search(
            tags=kw.get("tags"),
            page=int(kw.get("page") or 1),
            page_size=int(kw.get("page_size") or 10),
            sort=chosen,
            # ``args={'ids': …}`` rides the same include-channel fold
            # the paper tags-only door documents (gr719628) — without
            # the forward the browse would widen past the named cohort.
            ids=kw.get("ids"),
        )


__all__ = ["CfpHandler"]
