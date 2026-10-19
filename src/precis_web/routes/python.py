"""The code eye page — ``/python/<alias>::<qualname>`` (gr499107).

``/eye/<handle>`` is the refs ladder's browser door; this is the same
idea for the in-memory code index. The body comes from
``get(kind='python', id=<ref>, extent=<rung>)`` through the same
dispatcher the MCP uses, so rings, budgets and refusals are the
handler's — this page never re-implements ring logic. It HTML-escapes
the body and turns the references it names into links that re-centre
the eye on their own page:

* ``<alias>::<qualname>`` — callers, callees, importers, search hits
* ``<alias>/<file>.py`` and bare ``<file>.py`` — file pages
* ``tests/test_x.py::Class::test_y`` — pytest nodeids
* the ``L<line>`` member rows of the module span / member list —
  resolved through the focus file's ``{start_line: qualname}`` map
* ring-2's bare qualnames — only when they resolve to a real node

Every candidate resolves against the live ``RepoIndex`` (the same one
the handler just rendered from) before it gets an anchor, so a name
that isn't a real node — ``ext:`` callees, dead references — stays
text rather than growing a dead link.

* ``GET /python/`` — a box over ``search(kind='python',
  mode='pattern')``. An id-shaped query (``alias::qn``,
  ``alias/path.py``, a configured alias) redirects to its page.
* ``GET /python/{ref}?extent=<rung>`` — the eye; the rung defaults to
  ``fisheye+1hop`` and the ladder row offers every rung except
  ``+recall`` (the handler refuses it — semantic proximity is the
  search box).
* ``GET /python/{ref}?view=<view>`` — the same ref through a ``get``
  view (``source`` stays raw text; ``outline``/``callers``/``tests``/
  ``importers`` get the same linkification).
"""

from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from precis.handlers._eye import EYE_LADDER, is_eye_view
from precis.handlers.python import _parse_id
from precis_web.deps import await_dispatch, get_runtime, templates, timed_response

if TYPE_CHECKING:
    from precis.python_index import RepoIndex

log = logging.getLogger(__name__)

router = APIRouter(prefix="/python", tags=["python"])

#: The rung the page opens on: the neighbourhood is why one comes here
#: (same default as ``/eye``).
DEFAULT_EXTENT = "fisheye+1hop"

#: ``get`` views offered on the toggle row, next to the ladder.
_NODE_VIEWS = ("source", "outline", "callers", "importers", "tests")
_ALIAS_VIEWS = ("toc", "entries")

# ── linkification ────────────────────────────────────────────────────
#: An already-rendered anchor — later passes must not reach inside
#: (href attributes contain dotted names the bare-qualname pass would
#: otherwise mangle).
_ANCHOR = re.compile(r"(<a\b[^>]*>.*?</a>)")

#: ``<alias>::<qualname>`` — the canonical node address the eye, the
#: search body and the callers/importers views all print. The ``.``/``:``
#: in the lookbehind keep ``py::test`` (inside a nodeid) and
#: ``x.y::z`` out.
_ALIAS_QN = re.compile(r"(?<![\w.:@-])([A-Za-z0-9_-]+)::([A-Za-z_][\w.]*)")

#: A pytest nodeid as ``_tests_rows`` and ``Run:`` lines print it:
#: ``tests/test_x.py::Class::test_y`` (``[params]`` trimmed).
_NODEID = re.compile(
    r"(?<![\w./:@-])([A-Za-z0-9_][\w.-]*(?:/[\w.-]+)*\.py)::([A-Za-z_][\w:.\[\]()-]*)"
)

#: ``<alias>/<file>.py`` as printed inside ``id='…'`` hints. A
#: trailing ``:N`` (``path.py:5`` evidence) is left outside the link.
_ALIAS_FILE = re.compile(
    r"(?<![\w.:/@-])([A-Za-z0-9_-]+)/([A-Za-z0-9_./-]+\.py)(?![\w.-])"
)

#: A bare ``<file>.py`` path — ``(pkg/runner.py)`` in the module-span
#: header, ``Indexed file:`` lines, outline bodies.
_PYFILE = re.compile(r"(?<![\w.:/@-])([A-Za-z0-9_][\w.-]*(?:/[\w.-]+)*\.py)(?![\w.-])")

#: The ring-1 package gloss: ``Package pkg.name — "<doc>"`` — the
#: qualname alone links, the word ``Package`` stays.
_PKG_GLOSS = re.compile(r"(?<=Package )([A-Za-z_][\w.]*)")

#: A member row's ``L<line>`` marker — ``_module_span`` (``▸ L12  def
#: …``), ``Methods:``/``Members:`` (``    L12  def …``), the symbol
#: view's method list. Anchored at row start so ``range: L1-9`` and
#: ``id='…~L42'`` never match.
_LROW = re.compile(r"(?m)^([ \t]*(?:▸[ \t]*)?)L(\d+)(?=[ \t])")

#: A dotted name — ring-2's ``second hop: a.b.c, d.e.f``, ``their
#: modules:``, sibling-module rows, ``Called by:`` qualnames. Must
#: resolve against the index to become a link.
_BARE_QN = re.compile(r"(?<![\w.:/@-])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)(?![\w.:(@-])")


class _EyeLinks:
    """Resolve link candidates against the handler's live indexes.

    ``handler._resolve_alias`` + ``handler._index`` are the handler's
    own lookups — the eye just rendered from the same ``RepoIndex``, so
    every anchor is a node the index knows. Anything that raises (bad
    alias, stale worktree, missing handler) yields ``None`` and the
    candidate stays plain text. Results are memoised per request.
    """

    def __init__(self, handler: Any | None) -> None:
        self._handler = handler
        self._idxs: dict[str, RepoIndex | None] = {}

    def index(self, alias: str | None) -> RepoIndex | None:
        if not alias or self._handler is None:
            return None
        if alias not in self._idxs:
            try:
                root = self._handler._resolve_alias(alias)
                self._idxs[alias] = self._handler._index(alias, root)
            except Exception:
                self._idxs[alias] = None
        return self._idxs[alias]


def _href(ref: str, *, extent: str | None = None, view: str | None = None) -> str:
    out = "/python/" + quote(ref, safe=":~/")
    if view:
        return out + "?view=" + quote(view, safe="")
    if extent:
        return out + "?extent=" + quote(extent, safe="")
    return out


def _per_segment(html_text: str, fn: Callable[[str], str]) -> str:
    """Apply ``fn`` to the non-anchor segments of ``html_text`` only.

    ``re.split`` with a capture group interleaves the anchors it finds;
    they sit at odd indices and pass through untouched, so a pass can
    never substitute inside an ``<a …>`` it or an earlier pass emitted.
    """
    parts = _ANCHOR.split(html_text)
    for i in range(0, len(parts), 2):
        parts[i] = fn(parts[i])
    return "".join(parts)


def linkify_python_refs(
    text: str,
    *,
    links: _EyeLinks,
    focus_alias: str | None,
    extent: str = DEFAULT_EXTENT,
    line_map: dict[int, str] | None = None,
) -> str:
    """HTML-escape ``text`` and link every resolvable python reference in
    it to its ``/python`` page at ``extent``.

    ``links`` resolves candidates against the live index;
    ``focus_alias`` is the eye's own alias — the index nodeids, bare
    paths, ``L<line>`` rows and bare qualnames resolve against it.
    ``line_map`` maps a member row's ``L<line>`` to the qualname it
    names (the focus file's ``{start_line: qualname}``); ``None`` when
    the focus is a bare alias or unparseable, which leaves ``L`` rows
    as text.
    """
    out = html.escape(text, quote=False)

    def _anchor(label: str, ref: str) -> str:
        return f'<a class="eye-handle" href="{_href(ref, extent=extent)}">{label}</a>'

    def _nodeids(seg: str) -> str:
        idx = links.index(focus_alias)

        def rep(m: re.Match[str]) -> str:
            if idx is None:
                return m.group(0)
            mod = idx.file(m.group(1))
            if mod is None:
                return m.group(0)
            qn = mod.qualname + "." + m.group(2).replace("::", ".")
            if idx.symbol(qn) is None:
                return m.group(0)
            return _anchor(m.group(0), f"{focus_alias}::{qn}")

        return _NODEID.sub(rep, seg)

    def _alias_qn(seg: str) -> str:
        def rep(m: re.Match[str]) -> str:
            alias, qn = m.group(1), m.group(2)
            idx = links.index(alias)
            if idx is None or idx.symbol(qn) is None:
                return m.group(0)
            return _anchor(m.group(0), f"{alias}::{qn}")

        return _ALIAS_QN.sub(rep, seg)

    def _alias_file(seg: str) -> str:
        def rep(m: re.Match[str]) -> str:
            alias, path = m.group(1), m.group(2)
            idx = links.index(alias)
            if idx is None or idx.file(path) is None:
                return m.group(0)
            return _anchor(m.group(0), f"{alias}/{path}")

        return _ALIAS_FILE.sub(rep, seg)

    def _pyfile(seg: str) -> str:
        idx = links.index(focus_alias)

        def rep(m: re.Match[str]) -> str:
            if idx is None or idx.file(m.group(1)) is None:
                return m.group(0)
            return _anchor(m.group(0), f"{focus_alias}/{m.group(1)}")

        return _PYFILE.sub(rep, seg)

    def _pkg_gloss(seg: str) -> str:
        idx = links.index(focus_alias)

        def rep(m: re.Match[str]) -> str:
            if idx is None or idx.symbol(m.group(0)) is None:
                return m.group(0)
            return _anchor(m.group(0), f"{focus_alias}::{m.group(0)}")

        return _PKG_GLOSS.sub(rep, seg)

    def _lrows(seg: str) -> str:
        if not line_map or focus_alias is None:
            return seg

        def rep(m: re.Match[str]) -> str:
            qn = line_map.get(int(m.group(2)))
            if qn is None:
                return m.group(0)
            return f"{m.group(1)}{_anchor('L' + m.group(2), f'{focus_alias}::{qn}')}"

        return _LROW.sub(rep, seg)

    def _bare_qn(seg: str) -> str:
        idx = links.index(focus_alias)
        if idx is None:
            return seg

        def rep(m: re.Match[str]) -> str:
            # ``ext:`` list rows name out-of-repo symbols — never links.
            line = seg[seg.rfind("\n", 0, m.start()) + 1 : m.start()]
            if line.lstrip().startswith("ext:"):
                return m.group(0)
            if idx.symbol(m.group(1)) is None:
                return m.group(0)
            return _anchor(m.group(1), f"{focus_alias}::{m.group(1)}")

        return _BARE_QN.sub(rep, seg)

    for fn in (_nodeids, _alias_qn, _alias_file, _pyfile, _pkg_gloss, _lrows, _bare_qn):
        out = _per_segment(out, fn)
    return out


# ── routes ───────────────────────────────────────────────────────────


def _python_handler(request: Request) -> Any | None:
    """The runtime's PythonHandler, or ``None`` (kind disabled, no
    runtime, test double) — linkification degrades to plain text."""
    hub = getattr(get_runtime(request), "hub", None)
    if hub is None:
        return None
    try:
        return hub.handler_for("python")
    except Exception:
        return None


def _id_target(q: str, handler: Any | None) -> str | None:
    """``q`` as a python page address, or ``None`` (it's a query).
    ``::``/``/`` shapes are ids by form; a bare word is an id only when
    it names a configured alias."""
    if "::" in q or "/" in q:
        return q
    if handler is not None:
        try:
            if q in handler.roots or (
                handler.worktrees is not None and q in handler._worktree_listing()
            ):
                return q
        except Exception:
            pass
    return None


def _line_map(idx: RepoIndex | None, parsed: Any) -> dict[int, str] | None:
    """``{start_line: qualname}`` for the focus's own file — the map a
    member row's ``L<line>`` resolves through. ``None`` for a bare
    alias or an unresolvable focus."""
    if idx is None or parsed is None:
        return None
    mod = None
    if parsed.qualname is not None:
        sym = idx.symbol(parsed.qualname)
        if sym is not None:
            mod = idx.file(sym.file)
    elif parsed.file is not None:
        mod = idx.file(parsed.file)
    if mod is None:
        return None
    return {s.start_line: s.qualname for s in mod.symbols if s.kind != "module"}


def _ladder(ref: str, extent: str) -> list[dict[str, Any]]:
    """The rung row — the full ladder minus ``+recall``, which the
    python handler refuses by design (semantic proximity is the search
    box, not a rung)."""
    return [
        {
            "label": r,
            "href": _href(ref, extent=r),
            "on": r == extent,
        }
        for r in EYE_LADDER
    ]


def _view_row(ref: str, *, is_node: bool, view: str | None) -> list[dict[str, Any]]:
    views = _NODE_VIEWS if is_node else _ALIAS_VIEWS
    return [{"label": v, "href": _href(ref, view=v), "on": v == view} for v in views]


@router.get("/", response_class=HTMLResponse)
async def py_index(request: Request, q: str | None = None) -> Any:
    """The box: an id redirects to its page, anything else runs a
    ``mode='pattern'`` search and links every hit's ``alias::qn``."""
    handler = _python_handler(request)
    hits: str | None = None
    error: str | None = None
    if q and q.strip():
        q = q.strip()
        if (target := _id_target(q, handler)) is not None:
            return RedirectResponse(_href(target), status_code=303)
        body, is_error = await await_dispatch(
            request, "search", {"kind": "python", "q": q, "mode": "pattern"}
        )
        if is_error:
            error = body
        else:
            hits = linkify_python_refs(body, links=_EyeLinks(handler), focus_alias=None)
    return templates.TemplateResponse(
        request,
        "python/index.html.j2",
        {"q": q or "", "hits": hits, "error": error, "ladder": EYE_LADDER},
    )


@router.get("/{ref:path}", response_class=HTMLResponse)
async def py_focus(
    request: Request,
    ref: str,
    extent: str = DEFAULT_EXTENT,
    view: str | None = None,
) -> Any:
    """The eye on one symbol/module/file at one rung — or the same ref
    through a ``get`` view when ``view=`` is set."""
    t0 = time.monotonic()
    ref = ref.strip()
    handler = _python_handler(request)
    links = _EyeLinks(handler)
    try:
        parsed = _parse_id(ref)
        focus_alias: str | None = parsed.alias
    except Exception:
        parsed, focus_alias = None, None
    is_node = (
        parsed is not None
        and (parsed.qualname is not None or parsed.file is not None)
        and parsed.start_line is None
        and parsed.block_selector is None
    )
    extent = extent.strip() or DEFAULT_EXTENT
    view = (view or "").strip() or None
    if view is not None and is_eye_view(view):
        # A ladder label in view= is the same rung — keep the toggle on
        # "eye" rather than splitting the door.
        extent, view = view, None

    args: dict[str, Any] = {"kind": "python", "id": ref}
    if view is not None:
        args["view"] = view
    elif is_node:
        args["extent"] = extent
    # A bare alias gets the plain get (repo overview) — the eye refuses
    # it ("the repo is not a node"), and so does a ``~`` selector id,
    # which reads through get unchanged.
    body, is_error = await await_dispatch(request, "get", args)
    status = 200
    if is_error:
        first = body.splitlines()[0] if body else ""
        status = 404 if "[error:NotFound]" in first else 200

    if view == "source":
        rendered = html.escape(body, quote=False)  # raw source — never linkified
    else:
        rendered = linkify_python_refs(
            body,
            links=links,
            focus_alias=focus_alias,
            extent=extent,
            line_map=_line_map(links.index(focus_alias), parsed),
        )
    return timed_response(
        request,
        templates.TemplateResponse(
            request,
            "python/focus.html.j2",
            {
                "ref": ref,
                "extent": extent,
                "view": view,
                "body": rendered,
                "is_error": is_error,
                "eye_href": _href(ref, extent=extent),
                "is_node": is_node,
                "ladder": _ladder(ref, extent) if is_node and view is None else None,
                "views": _view_row(ref, is_node=is_node, view=view),
            },
            status_code=status,
        ),
        t0,
    )
