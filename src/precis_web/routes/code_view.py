"""The design code viewer — ``GET /{se,pcb,cad}/{slug}/code`` (gr516943).

A read-only page over the code attached to a design. A design's
*workspace* is a ``folder`` ref marked ``meta['workspace']`` (whose
``slug`` names the workspace and ``design`` names the owning design —
:mod:`precis.handlers._python_ws` reads the same record), or one linked
``part-of`` the design; its files are the folder's ``plaintext``
children whose relative path is ``meta.workspace_path`` or, for
harvested folders, ``meta.harvest_orig_path``. The design's
``meta['workspaces'][<slug>]`` run record (``run_job`` / ``script_sha``,
written by ``precis_se.workspace_import.import_workspace_run``) renders
in the workspace header.

Each file is a tab (Alpine, one ``x-data`` over the page); the body is
syntax-highlighted server-side by pygments (``get_lexer_for_filename``,
``TextLexer`` fallback, ``cv-`` class prefix scoped to this page) and
shown in ``<pre><code>`` — rendered as TEXT only, never executed.
Import/include lines that name a sibling file of the same workspace
link to that tab (``?ws=…&file=…``); other python references go through
``linkify_python_refs``' passes
(:func:`precis_web.routes.python._linkify_escaped`) so ``ws-<slug>::`` /
``main::`` ids resolve to their ``/python`` eye pages when the alias
resolves.

Security (this page renders stored code):

* pygments' HtmlFormatter escapes the code itself — its output is the
  only content the template marks safe; raw file text never is. Past
  :data:`_MAX_FILE_BYTES` the file renders as *plain* escaped text (no
  highlighting), so a giant file never runs the link passes over span
  soup either;
* file size is capped at :data:`_MAX_FILE_BYTES` (truncation note);
  the per-folder file count is capped at :data:`_MAX_FILES`; the
  page's total stored bytes at :data:`_MAX_PAGE_BYTES` — past it
  files list as paths only;
* python-ref linkification resolves only ``main`` and this page's
  own ``ws-<slug>`` aliases — a stored file can't stage an arbitrary
  workspace (or any other root) by naming ``ws-<name>::…``
  (:class:`_ScopedLinks`);
* a file is reachable only as a ``plaintext`` child of one of the
  design's own folders — absolute paths and ``..`` segments are
  dropped before anything renders (same guard as ``_python_ws._files``);
* the route sits behind the app's HTTP Basic middleware like every
  other page — nothing here adds an exemption.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_for_filename
from pygments.util import ClassNotFound

from precis.errors import NotFound
from precis.utils.slug_ref import resolve_live_slug_ref
from precis_web.deps import get_runtime, get_store, templates, timed_response
from precis_web.routes.python import (
    _EyeLinks,
    _linkify_escaped,
    _per_segment,
)

if TYPE_CHECKING:
    from precis.python_index import RepoIndex

log = logging.getLogger(__name__)

router = APIRouter(tags=["code"])

#: Per-file display cap — stored code can be a multi-megabyte log or a
#: vendored blob; over the cap the first slice renders with a note.
_MAX_FILE_BYTES = 200 * 1024

#: Per-folder file cap — past it the tab row keeps only the paths.
_MAX_FILES = 128

#: Whole-page body budget: workspaces × :data:`_MAX_FILES` files could
#: make a multi-MB page, and every file's body is a chunk read plus the
#: linkify passes. Past the budget the remaining files list as paths
#: only — no body read, no tab.
_MAX_PAGE_BYTES = 2 * 1024 * 1024

#: Workspace folders of one design: ``meta['workspace']`` marked, and
#: either naming this design in ``meta.workspace.design`` or sitting
#: ``part-of`` it. The design value is ``'<kind>:<slug>'`` always, or
#: the bare ``'<slug>'`` only for ``se`` — the importer writes the bare
#: form for se designs (the python-ws read accepts both spellings), so
#: without the kind guard a pcb/cad design named ``foo`` would show
#: ``se:foo``'s workspace.
_FOLDERS_SQL = """
SELECT r.ref_id, r.meta
  FROM refs r
 WHERE r.kind = 'folder' AND r.retired_at IS NULL
   AND r.meta ? 'workspace'
   AND (r.meta->'workspace'->>'design' = %s
        OR (%s AND r.meta->'workspace'->>'design' = %s)
        OR EXISTS (SELECT 1 FROM links l
                    WHERE l.src_ref_id = r.ref_id
                      AND l.dst_ref_id = %s
                      AND l.relation = 'part-of'))
 ORDER BY r.ref_id
"""

#: A folder's file children — same columns ``_python_ws._FILES_SQL``
#: reads: the relpath the workspace script / harvester recorded.
_FILES_SQL = """
SELECT ref_id,
       COALESCE(meta->>'workspace_path', meta->>'harvest_orig_path')
  FROM refs
 WHERE parent_id = %s AND kind = 'plaintext' AND retired_at IS NULL
 ORDER BY 2 NULLS LAST, ref_id
"""


# ── import/include linkification ─────────────────────────────────────
#: ``from a.b import c`` (dotted group 2) and ``import a.b`` (group 2 of
#: the second pattern). The lookbehind keeps attribute access
#: (``x.import``) and prose out; module names never carry ``<``/``&``,
#: so these match identically on escaped text.
_FROM_IMPORT = re.compile(
    r"(?<![\w.])(from[ \t]+)(\.{0,3})([A-Za-z_][\w.]*)?(?=[ \t]+import)"
)
_IMPORT = re.compile(r"(?<![\w.])(import[ \t]+)([A-Za-z_][\w.]*)")


def _module_paths(module: str, *, base: str = "") -> list[str]:
    """Candidate workspace relpaths for a dotted module — longest first:
    ``a.b`` → ``a/b.py``, ``a/b/__init__.py``, ``a.py``, ``a/__init__.py``.
    ``base`` is the directory a relative import resolves against."""
    parts = [p for p in module.split(".") if p]
    out: list[str] = []
    for i in range(len(parts), 0, -1):
        rel = "/".join(parts[:i])
        prefix = f"{base}/" if base else ""
        out += [f"{prefix}{rel}.py", f"{prefix}{rel}/__init__.py"]
    return out


def _import_linker(
    files: set[str], file_path: str, ws_slug: str
) -> Callable[[str], str]:
    """A ``_per_segment`` pass: rewrite ``import``/``from … import``
    module names that resolve to a sibling file into tab anchors.

    ``from . import x`` / ``from .a import b`` resolve against the
    file's own directory — each leading dot beyond the first climbs one
    level. A name that resolves to no sibling is left for the python-ref
    passes.
    """
    file_dir = str(PurePosixPath(file_path).parent)
    if file_dir == ".":
        file_dir = ""

    def _anchor(label: str, path: str) -> str:
        href = f"?ws={quote(ws_slug, safe='')}&amp;file={quote(path, safe='/')}"
        return f'<a class="eye-handle" href="{href}">{label}</a>'

    def _base(dots: str) -> str:
        if not dots:
            return ""  # absolute import — resolve from the folder root
        up = len(dots) - 1
        parts = file_dir.split("/") if file_dir else []
        if up > len(parts):
            return ""
        return "/".join(parts[: len(parts) - up]) if up else file_dir

    def _rel(sub: re.Match[str]) -> str:
        dots, mod = sub.group(2), sub.group(3)
        candidates = _module_paths(mod, base=_base(dots)) if mod else []
        # ``from pkg import member`` — the member may itself be a module
        # (``from . import util``): try ``pkg/member.py`` too via the
        # names after ``import`` — handled by the plain-import rule for
        # the dotted case only; relative ``from . import x`` leaves the
        # name for the sibling-path pass below.
        for cand in candidates:
            if cand in files:
                label = sub.group(2) + (mod or "")
                return sub.group(1) + _anchor(label, cand)
        return sub.group(0)

    def _abs(sub: re.Match[str]) -> str:
        # Root first, then the file's own directory — ``import util``
        # inside ``pkg/main.py`` (and the ``import x`` tail of a
        # ``from . import x``) resolves siblings beside the file.
        for cand in _module_paths(sub.group(2)) + _module_paths(
            sub.group(2), base=file_dir
        ):
            if cand in files:
                return sub.group(1) + _anchor(sub.group(2), cand)
        return sub.group(0)

    def _run(seg: str) -> str:
        seg = _FROM_IMPORT.sub(_rel, seg)
        return _IMPORT.sub(_abs, seg)

    return _run


def _path_linker(files: set[str], ws_slug: str) -> Callable[[str], str]:
    """A ``_per_segment`` pass linking verbatim sibling-path mentions
    (``main.py``, ``pkg/util.py``) to their tab — covers ``open('x.py')``
    and include-style lines the import grammar doesn't. The pattern is
    built from the folder's own (escaped) paths, so a path-shaped word
    that isn't a sibling stays text. The lookbehind's ``&``/``#`` keep a
    sibling named like an entity body (``quot``, ``amp``, ``39``) from
    matching inside ``&quot;``/``&#39;`` — the entities the escaping
    itself emits."""
    if not files:
        return lambda seg: seg
    pat = re.compile(
        r"(?<![\w./@&#-])("
        + "|".join(
            re.escape(html.escape(p, quote=False))
            for p in sorted(files, key=len, reverse=True)
        )
        + r")(?![\w.-])"
    )

    def _run(seg: str) -> str:
        def rep(m: re.Match[str]) -> str:
            path = html.unescape(m.group(1))
            href = f"?ws={quote(ws_slug, safe='')}&amp;file={quote(path, safe='/')}"
            return f'<a class="eye-handle" href="{href}">{m.group(1)}</a>'

        return pat.sub(rep, seg)

    return _run


# ── highlighted rendering ────────────────────────────────────────────
#: ``nowrap`` emits bare ``<span class="cv-…">`` runs — the template
#: owns the ``<pre>``; the ``cv-`` prefix scopes every token class to
#: this page's inline styles.
_FORMATTER = HtmlFormatter(nowrap=True, classprefix="cv-")


def _style_defs(style: str) -> str:
    """Token rules for ``<pre class="cv-code">``, minus the root
    ``.cv-code { background… }`` rule — the surface/text theme classes
    own those so the block follows light/dark like every other page."""
    defs = HtmlFormatter(nowrap=True, classprefix="cv-", style=style).get_style_defs(
        ".cv-code"
    )
    return re.sub(r"(?m)^\.cv-code \{[^}]*\}\n?", "", defs)


#: The inline ``<style>`` payload — default palette plus github-dark
#: under ``prefers-color-scheme: dark`` (the app is dual-scheme).
_STYLE_DEFS = (
    _style_defs("default")
    + "\n@media (prefers-color-scheme: dark) {\n"
    + _style_defs("github-dark")
    + "}\n"
)

#: Whitespace between tokens on highlighted output — a bare space or a
#: ``cv-w`` run.
_HL_WS = r"(?:[ \t]|<span class=\"cv-w\">[^<]*</span>)+"

#: ``from <mod> import`` on highlighted python: the keyword is ``cv-kn``
#: and the (possibly relative) module one ``cv-nn`` span — leading dots
#: ride inside it (``..pkg``). The lookahead leaves ``import`` for the
#: member-name pass, mirroring plain-text ``_FROM_IMPORT``.
_HL_FROM = re.compile(
    r"(<span class=\"cv-kn\">from</span>" + _HL_WS + r")"
    r"(<span class=\"cv-nn\">(\.*)([A-Za-z_][\w.]*)?</span>)"
    r"(?=" + _HL_WS + r"<span class=\"cv-kn\">import</span>)"
)

#: ``import <name>`` — ``cv-nn`` for the module of a plain import, and
#: ``cv-n`` for the member names of a ``from … import``; both resolve
#: root-first then beside the file, like plain-text ``_IMPORT``.
_HL_IMPORT = re.compile(
    r"(<span class=\"cv-kn\">import</span>" + _HL_WS + r")"
    r"(<span class=\"cv-(?:nn|n)\">([A-Za-z_][\w.]*)</span>)"
)

#: Splits highlighted markup into text nodes (even indices) and
#: spans/anchors (odd) — a pass that rewrites text must never see a tag:
#: a match spanning two tokens can't become one anchor, and a
#: substitution inside a tag would corrupt it.
_HL_TAG = re.compile(r"(<a\b[^>]*>.*?</a>|<[^>]+>)", re.DOTALL)


def _hl_import_linker(
    files: set[str], file_path: str, ws_slug: str
) -> Callable[[str], str]:
    """The sibling-import pass over highlighted markup — same resolution
    rules as :func:`_import_linker`, matching pygments' token spans
    instead of bare text. The anchor wraps the name's own span, so the
    linked text keeps its token colour."""
    file_dir = str(PurePosixPath(file_path).parent)
    if file_dir == ".":
        file_dir = ""

    def _anchor(markup: str, path: str) -> str:
        href = f"?ws={quote(ws_slug, safe='')}&amp;file={quote(path, safe='/')}"
        return f'<a class="eye-handle" href="{href}">{markup}</a>'

    def _base(dots: str) -> str:
        if not dots:
            return ""
        up = len(dots) - 1
        parts = file_dir.split("/") if file_dir else []
        if up > len(parts):
            return ""
        return "/".join(parts[: len(parts) - up]) if up else file_dir

    def _rel(sub: re.Match[str]) -> str:
        dots, mod = sub.group(3), sub.group(4)
        if mod:
            for cand in _module_paths(mod, base=_base(dots)):
                if cand in files:
                    return sub.group(1) + _anchor(sub.group(2), cand)
        return sub.group(0)

    def _abs(sub: re.Match[str]) -> str:
        for cand in _module_paths(sub.group(3)) + _module_paths(
            sub.group(3), base=file_dir
        ):
            if cand in files:
                return sub.group(1) + _anchor(sub.group(2), cand)
        return sub.group(0)

    def _run(doc: str) -> str:
        doc = _HL_FROM.sub(_rel, doc)
        return _HL_IMPORT.sub(_abs, doc)

    return _run


def _per_token(markup: str, fn: Callable[[str], str]) -> str:
    """:func:`_per_segment` for highlighted markup: ``fn`` sees text
    nodes only — spans and already-emitted anchors pass through."""
    parts = _HL_TAG.split(markup)
    for i in range(0, len(parts), 2):
        parts[i] = fn(parts[i])
    return "".join(parts)


def _line_anchors(markup: str) -> str:
    """One empty ``<a id="L<n>" class="cv-l"></a>`` marker per line start,
    so a ``#L<n>`` fragment (the Selection box's board-script link,
    gr520540) names the exact line. Only text nodes are walked — a
    newline inside a tag or link anchor is not a line boundary."""
    parts = _HL_TAG.split(markup)
    n = 1
    out = ['<a id="L1" class="cv-l"></a>']
    for i, part in enumerate(parts):
        if i % 2:
            out.append(part)
            continue
        for j, seg in enumerate(part.split("\n")):
            if j:
                n += 1
                out.append(f'\n<a id="L{n}" class="cv-l"></a>')
            out.append(seg)
    return "".join(out)


class _ScopedLinks(_EyeLinks):
    """``_EyeLinks`` fenced to ``main`` plus this page's own ``ws-*``
    aliases.

    Stored file text can mention any ``alias::qn``; resolving each
    distinct ``ws-<name>`` would stage and index a whole workspace per
    page view, and ``wt-*``/other configured roots would resolve too.
    An alias outside the allowed set stays plain text.
    """

    def __init__(self, handler: Any | None, allowed: set[str]) -> None:
        super().__init__(handler)
        self._allowed = allowed

    def index(self, alias: str | None) -> RepoIndex | None:
        if alias not in self._allowed:
            return None
        return super().index(alias)


def _render_plain(
    body: str,
    *,
    files: set[str],
    path: str,
    ws_slug: str,
    links: _EyeLinks,
) -> str:
    """The un-highlighted pipeline — escape, sibling links, python refs.
    Truncated files and a lexer that fails both fall back here."""
    out = html.escape(body, quote=False)
    out = _per_segment(out, _import_linker(files, path, ws_slug))
    out = _per_segment(out, _path_linker(files, ws_slug))
    return _line_anchors(
        _linkify_escaped(
            out,
            links=links,
            focus_alias=f"ws-{ws_slug}" if ws_slug else None,
        )
    )


def _render_file(
    body: str,
    *,
    files: set[str],
    path: str,
    ws_slug: str,
    links: _EyeLinks,
) -> tuple[str, bool]:
    """``(safe-html, truncated)`` for one file's viewable body.

    Highlight first (pygments escapes the code itself), then the
    sibling-import pass over the token spans, then the path and
    python-ref passes per text node — our tab links win for sibling
    files, resolvable ``alias::qn`` / ``alias/path.py`` refs link to
    ``/python``, everything else stays text. Past the byte cap — or if
    highlighting itself fails — the file falls back to the plain-escaped
    pipeline.
    """
    raw = body.encode("utf-8", errors="replace")
    if len(raw) > _MAX_FILE_BYTES:
        body = raw[:_MAX_FILE_BYTES].decode("utf-8", errors="ignore")
        return (
            _render_plain(body, files=files, path=path, ws_slug=ws_slug, links=links),
            True,
        )
    try:
        lexer = get_lexer_for_filename(path, body)
    except ClassNotFound:
        lexer = TextLexer()
    try:
        out = highlight(body, lexer, _FORMATTER)
    except Exception:
        return (
            _render_plain(body, files=files, path=path, ws_slug=ws_slug, links=links),
            False,
        )
    out = _hl_import_linker(files, path, ws_slug)(out)
    out = _per_token(out, _path_linker(files, ws_slug))
    out = _per_token(
        out,
        lambda seg: _linkify_escaped(
            seg,
            links=links,
            focus_alias=f"ws-{ws_slug}" if ws_slug else None,
        ),
    )
    return _line_anchors(out), False


def _ws_slug_of(meta: dict[str, Any], folder_id: int) -> str:
    """The folder's workspace slug — ``meta.workspace.slug``, else a
    stable ``f<id>`` placeholder so the tab keying still works."""
    ws = meta.get("workspace") if isinstance(meta, dict) else None
    slug = (ws or {}).get("slug")
    return str(slug) if slug else f"folder-{folder_id}"


def _python_handler(request: Request) -> Any | None:
    """The runtime's PythonHandler, or ``None`` — the same defensive
    lookup ``routes/python.py`` uses; a slim test double without a hub
    degrades linkification to plain text."""
    hub = getattr(get_runtime(request), "hub", None)
    if hub is None:
        return None
    try:
        return hub.handler_for("python")
    except Exception:
        return None


def _workspaces(
    store: Any, ref: Any, kind: str, handler: Any | None
) -> list[dict[str, Any]]:
    """The design's workspace sections: ``{slug, run, files, skipped}``
    rows for the template. ``run`` is the design's
    ``meta.workspaces[slug]`` record (``run_job``/``script_sha``);
    ``files`` are ``{key, path, body, truncated, size}`` dicts with
    ``body`` the linkified (already-escaped) HTML; ``skipped`` lists
    paths past the page byte budget — listed only, no body read."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            _FOLDERS_SQL,
            (f"{kind}:{ref.slug}", kind == "se", ref.slug, int(ref.id)),
        ).fetchall()
    links = _ScopedLinks(
        handler,
        {"main"} | {f"ws-{_ws_slug_of(meta or {}, int(fid))}" for fid, meta in rows},
    )
    records = (ref.meta or {}).get("workspaces") or {}
    out: list[dict[str, Any]] = []
    n_files = 0  # tab keys are page-global — workspaces share one ``tab`` state
    page_bytes = 0  # …as is the page byte budget
    for folder_id, meta in rows:
        ws_slug = _ws_slug_of(meta or {}, int(folder_id))
        with store.pool.connection() as conn:
            file_rows = conn.execute(_FILES_SQL, (int(folder_id),)).fetchall()
        # First pass: the path → ref_id map (sibling links resolve
        # against the whole set, including files listed after the one
        # being rendered). Out-of-tree paths drop here, before any read.
        paths: dict[str, int] = {}
        for fid, rel in file_rows:
            if not rel:
                rel = f"ref-{fid}"
            p = PurePosixPath(str(rel))
            if p.is_absolute() or ".." in p.parts:
                log.warning("code view: dropping out-of-tree path %r", rel)
                continue
            key = p.as_posix()
            if key in paths:
                key = f"{key}#ref-{fid}"
            paths[key] = int(fid)
        files: list[dict[str, Any]] = []
        skipped: list[str] = []
        shown = dict(list(paths.items())[:_MAX_FILES])
        extra = len(paths) - len(shown)
        siblings = set(shown)
        for path, fid in shown.items():
            if page_bytes >= _MAX_PAGE_BYTES:
                skipped.append(path)
                continue
            body = "".join(
                c.text
                for c in store.chunks.list_chunks_for_ref(fid)
                if c.ord >= 0  # body chunks only — card_* rows are ord < 0
            )
            size = len(body.encode("utf-8", errors="replace"))
            page_bytes += size
            rendered, truncated = _render_file(
                body,
                files=siblings,
                path=path,
                ws_slug=ws_slug,
                links=links,
            )
            files.append(
                {
                    "key": f"f{n_files}",
                    "path": path,
                    "body": rendered,
                    "truncated": truncated,
                    "size": size,
                }
            )
            n_files += 1
        run = records.get(ws_slug)
        out.append(
            {
                "slug": ws_slug,
                "run": dict(run) if isinstance(run, dict) else None,
                "files": files,
                "skipped": skipped,
                "extra": extra,
                "root_alias": f"ws-{ws_slug}",
            }
        )
    return out


def _not_found(request: Request, kind: str, slug: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html.j2",
        {
            "title": f"{kind} design not found",
            "detail": f"no live {kind} design with slug {slug!r}",
            "status": 404,
        },
        status_code=404,
    )


async def _code_page(
    request: Request, kind: str, slug: str, ws: str | None, file: str | None
) -> HTMLResponse:
    t0 = time.monotonic()
    store = get_store(request)
    try:
        ref = await asyncio.to_thread(resolve_live_slug_ref, store, kind=kind, id=slug)
    except NotFound:
        return _not_found(request, kind, slug)
    workspaces = await asyncio.to_thread(
        _workspaces, store, ref, kind, _python_handler(request)
    )
    # First file is the default; a ?file= match (scoped by ?ws= when given)
    # replaces it, and the first such match wins so a path present in
    # several workspaces resolves deterministically.
    selected = ""
    matched = False
    for w in workspaces:
        for f in w["files"]:
            if not selected:
                selected = f["key"]
            if not matched and file == f["path"] and (ws is None or ws == w["slug"]):
                selected = f["key"]
                matched = True
    return timed_response(
        request,
        templates.TemplateResponse(
            request,
            "design/code.html.j2",
            {
                "active_tab": kind,
                "kind": kind,
                "slug": ref.slug,
                "title": ref.title or ref.slug,
                "workspaces": workspaces,
                "selected": selected,
                "max_kb": _MAX_FILE_BYTES // 1024,
                "cv_css": _STYLE_DEFS,
            },
        ),
        t0,
    )


@router.get("/se/{slug}/code", response_class=HTMLResponse)
async def se_code(
    request: Request, slug: str, ws: str | None = None, file: str | None = None
) -> HTMLResponse:
    return await _code_page(request, "se", slug, ws, file)


@router.get("/pcb/{slug}/code", response_class=HTMLResponse)
async def pcb_code(
    request: Request, slug: str, ws: str | None = None, file: str | None = None
) -> HTMLResponse:
    return await _code_page(request, "pcb", slug, ws, file)


@router.get("/cad/{slug}/code", response_class=HTMLResponse)
async def cad_code(
    request: Request, slug: str, ws: str | None = None, file: str | None = None
) -> HTMLResponse:
    return await _code_page(request, "cad", slug, ws, file)
