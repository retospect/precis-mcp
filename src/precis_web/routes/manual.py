"""``/manual`` — the user-facing manual, rendered from markdown in the tree.

The chapters live as ``.md`` files in ``src/precis_web/manual/`` — *inside*
the package, not under ``docs/``, for two reasons. The wheel ships only the
``src/`` packages (``docs/`` is sdist-only), so a chapter under ``docs/``
would simply be absent on a deployed web node; and the manual documents the
routes in this very package, so a change to a button and the sentence
describing it land in the same diff.

Chapter order and slug both come from the filename: ``01-writing-a-paper.md``
is chapter 1 at ``/manual/writing-a-paper``. Title is the file's first ``#``
heading, blurb the first paragraph under it — so the index needs no separate
table of contents to drift out of sync.

Routes:

* ``GET /manual`` — the chapter index.
* ``GET /manual/{slug}`` — one chapter.
* ``GET /manual/tour/{slug}.json`` — one in-app guided-tour manifest
  (``manual/tour/NN-slug.json``), fetched by ``static/tour.js`` when a page
  carries ``?tour=<slug>`` in its URL. A chapter whose slug has a matching
  manifest gets a "take the tour" link (see ``_tour_for_chapter``); any page
  whose request path matches a manifest's ``route`` gets the header's "?"
  tour-launch button (see ``tour_slug_for_path``, wired in ``nav.py``).

The markdown is repo-controlled (it ships in the wheel), so it is rendered
trusted — no sanitizer. ``slug`` is nonetheless resolved against the
discovered chapter set rather than joined onto a path, so a traversal
attempt 404s instead of reading the filesystem.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from markdown_it import MarkdownIt

from precis_web.deps import templates

router = APIRouter(tags=["manual"])

log = logging.getLogger(__name__)

MANUAL_DIR = Path(__file__).resolve().parent.parent / "manual"

#: ``01-writing-a-paper.md`` → chapter 1, slug ``writing-a-paper``. A file
#: that does not match is not a chapter and is skipped (drafts, notes).
_CHAPTER_RE = re.compile(r"^(\d+)-([a-z0-9-]+)\.md$")

#: In-app guided-tour manifests (docs/backlog/user-guide-demo.md), one JSON
#: file per guide section — same ``NN-slug`` filename convention as the
#: chapters above, so ordering/slug derivation is identical and a chapter's
#: "take the tour" link can look its own slug up directly.
TOUR_DIR = MANUAL_DIR / "tour"

_TOUR_RE = re.compile(r"^(\d+)-([a-z0-9-]+)\.json$")

#: The only placements ``tour.js`` (static/tour.js) knows how to position a
#: callout card against.
_TOUR_PLACEMENTS = {"top", "bottom", "left", "right"}

#: Inline-emphasis markers stripped from an index blurb — the blurb is
#: rendered as plain text, so raw ``**bold**`` would leak through.
_INLINE_MD_RE = re.compile(r"(\*\*|\*|`|_)")


@dataclass(frozen=True)
class Chapter:
    """One manual chapter: its ordering, URL, and index-page display."""

    number: int
    slug: str
    title: str
    blurb: str
    path: Path


def _split(text: str) -> tuple[str, str, str]:
    """``(title, blurb, body)`` — the first ``# `` heading, the paragraph
    under it, and the chapter with that heading removed.

    One scan feeds both the index card and the page render, so the title
    the index shows and the title the body drops are the same line by
    construction. (They were once found two different ways — a chapter
    whose H1 wasn't on line 1 got the right index entry and a duplicated
    heading in the body.)

    A chapter missing either degrades to empty strings rather than
    raising — a half-written chapter should still be listed and readable.
    """
    title = ""
    blurb_lines: list[str] = []
    lines = text.splitlines()
    idx = 0
    h1_at: int | None = None
    for i, line in enumerate(lines):
        if line.startswith("# "):
            title = line[2:].strip()
            h1_at = i
            idx = i + 1
            break
    # Skip blanks, then take the run of non-blank lines that is not itself
    # a heading or a blockquote callout.
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    while idx < len(lines) and lines[idx].strip():
        if lines[idx].startswith(("#", ">")):
            break
        blurb_lines.append(lines[idx].strip())
        idx += 1
    body = text if h1_at is None else "\n".join(lines[:h1_at] + lines[h1_at + 1 :])
    return title, _INLINE_MD_RE.sub("", " ".join(blurb_lines)), body


@lru_cache(maxsize=1)
def _chapters() -> tuple[Chapter, ...]:
    """Discover + parse every chapter, ordered by filename number.

    Cached for the life of the process: the chapters are read-only package
    data shipped in the wheel, so nothing can change them under a running
    server. Anything that writes a chapter file at runtime (only a test
    would) must call ``_chapters.cache_clear()`` — the cache never expires
    on its own.
    """
    out: list[Chapter] = []
    if not MANUAL_DIR.is_dir():
        log.warning("manual: no chapter directory at %s", MANUAL_DIR)
        return ()
    for path in sorted(MANUAL_DIR.glob("*.md")):
        m = _CHAPTER_RE.match(path.name)
        if m is None:
            continue
        text = path.read_text(encoding="utf-8")
        title, blurb, _ = _split(text)
        out.append(
            Chapter(
                number=int(m.group(1)),
                slug=m.group(2),
                title=title or m.group(2).replace("-", " ").capitalize(),
                blurb=blurb,
                path=path,
            )
        )
    return tuple(out)


def _render(chapter: Chapter) -> str:
    """Chapter markdown → HTML. Trusted input (package data), so no
    sanitizer; the page template scopes the styling under ``.manual``.

    ``commonmark`` + ``table``: the chapters use tables for the state /
    origin / marker grids, and commonmark already covers the fenced code
    blocks and blockquote callouts. Nothing else is enabled — a chapter
    should not be able to reach for syntax the styling doesn't cover.

    The H1 is dropped: the page template renders the title in its own
    header, and a second copy inside the body reads as a duplicate.
    """
    _, _, body = _split(chapter.path.read_text(encoding="utf-8"))
    return MarkdownIt("commonmark").enable("table").render(body)


def _validate_tour(data: Any, path: Path) -> dict[str, Any]:
    """Enforce the manifest shape ``tour.js`` and the (future) capture
    pipeline both assume, raising ``ValueError`` with a message that names
    the file and the exact defect — this is the "fail loudly" half of the
    tour endpoint: a malformed manifest must 500 with a clear log line
    rather than serve something ``tour.js`` renders as a blank/broken card.
    """
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: top level must be a JSON object")
    for key in ("title", "route", "steps"):
        if key not in data:
            raise ValueError(f"{path.name}: missing required key {key!r}")
    if not isinstance(data["title"], str) or not data["title"].strip():
        raise ValueError(f"{path.name}: 'title' must be a non-empty string")
    if not isinstance(data["route"], str) or not data["route"].strip():
        raise ValueError(f"{path.name}: 'route' must be a non-empty string")
    steps = data["steps"]
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"{path.name}: 'steps' must be a non-empty list")
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"{path.name}: step {i} is not an object")
        for key in ("anchor", "heading", "text", "placement"):
            if not isinstance(step.get(key), str) or not step[key].strip():
                raise ValueError(f"{path.name}: step {i} has no non-empty {key!r}")
        if step["placement"] not in _TOUR_PLACEMENTS:
            raise ValueError(
                f"{path.name}: step {i} placement {step['placement']!r} not in "
                f"{sorted(_TOUR_PLACEMENTS)}"
            )
    return data


@lru_cache(maxsize=1)
def _tours() -> dict[str, dict[str, Any]]:
    """Discover + parse + validate every tour manifest, keyed by slug.

    Cached like ``_chapters()`` (read-only package data; a test that writes
    one at runtime must call ``_tours.cache_clear()``). Unlike ``_chapters``,
    a single malformed manifest fails the WHOLE call — raised out of here,
    the tour endpoint turns that into a 500 with a clear log line rather
    than dropping the bad file and quietly serving the rest, so a broken
    manifest is loud in CI/tests instead of surfacing as a silent gap only
    when someone happens to tour that one section.
    """
    out: dict[str, dict[str, Any]] = {}
    if not TOUR_DIR.is_dir():
        log.warning("manual: no tour directory at %s", TOUR_DIR)
        return out
    for path in sorted(TOUR_DIR.glob("*.json")):
        m = _TOUR_RE.match(path.name)
        if m is None:
            continue
        text = path.read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name}: not valid JSON: {exc}") from exc
        out[m.group(2)] = _validate_tour(data, path)
    return out


#: A ``{name}`` route segment (``/smartdraft/{id}``) — matched against
#: exactly one non-empty path segment, never zero and never more than one.
_ROUTE_PARAM_RE = re.compile(r"^\{[a-zA-Z_][a-zA-Z0-9_]*\}$")


@cache
def _route_pattern(route: str) -> re.Pattern[str]:
    """Compile a manifest ``route`` into the regex that matches it.

    Each ``/``-separated segment is either matched literally (``re.escape``)
    or, if it is a whole ``{name}`` placeholder, matched against exactly one
    non-empty segment (``[^/]+``) — so ``/smartdraft/{id}`` matches
    ``/smartdraft/dr173020`` but neither ``/smartdraft`` (segment missing)
    nor ``/smartdraft/a/b`` (one segment too many). Cached: there are only a
    handful of manifests and the route strings repeat on every request.
    """
    segments = [
        "[^/]+" if _ROUTE_PARAM_RE.match(seg) else re.escape(seg)
        for seg in route.split("/")
    ]
    return re.compile("^" + "/".join(segments) + "$")


def tour_slug_for_path(path: str) -> str | None:
    """The tour manifest slug whose ``route`` matches ``path`` — or
    ``None`` if nothing matches (or the tour set is currently broken, same
    fail-quiet posture as ``_tour_for_chapter``).

    This is what puts the "?" tour-launch button (``nav.py``, `header in
    ``base.html.j2``) on a page: server-side path matching against the
    already-cached manifests, so surfacing the button costs no extra
    client-side fetch. First match wins, in the ``NN-slug`` filename order
    ``_tours()`` discovers manifests in (a dict is insertion-ordered, and
    that insertion order comes from ``sorted(TOUR_DIR.glob("*.json"))``).
    """
    try:
        tours = _tours()
    except ValueError:
        log.exception("manual: tour manifests failed to load")
        return None
    for slug, tour in tours.items():
        if _route_pattern(tour["route"]).match(path):
            return slug
    return None


def _tour_for_chapter(slug: str) -> dict[str, Any] | None:
    """The tour manifest whose slug matches a manual chapter's, for that
    chapter page's "take the tour" link — or ``None`` if there isn't one, or
    if the tour set as a whole is currently broken. A malformed tour
    manifest must not take an unrelated chapter page down with it; the
    ``/manual/tour/{slug}.json`` endpoint is where that failure is loud.
    """
    try:
        tours = _tours()
    except ValueError:
        log.exception("manual: tour manifests failed to load")
        return None
    return tours.get(slug)


@router.get("/manual", response_class=HTMLResponse)
async def manual_index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "manual/index.html.j2",
        {"active_tab": "manual", "chapters": _chapters()},
    )


@router.get("/manual/{slug}", response_class=HTMLResponse)
async def manual_chapter(request: Request, slug: str) -> HTMLResponse:
    chapters = _chapters()
    match = next((c for c in chapters if c.slug == slug), None)
    if match is None:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "No such chapter",
                "detail": f"The manual has no chapter {slug!r}.",
                "status": 404,
            },
            status_code=404,
        )
    rank = chapters.index(match)
    return templates.TemplateResponse(
        request,
        "manual/page.html.j2",
        {
            "active_tab": "manual",
            "chapter": match,
            "chapters": chapters,
            "body": _render(match),
            "prev": chapters[rank - 1] if rank > 0 else None,
            "next": chapters[rank + 1] if rank + 1 < len(chapters) else None,
            "tour": _tour_for_chapter(match.slug),
        },
    )


@router.get("/manual/tour/{slug}.json")
async def manual_tour(slug: str) -> JSONResponse:
    """One tour manifest, verbatim (``tour.js`` fetches this directly).

    404 for a slug with no manifest; 500 (loudly, with a log line) for a
    slug whose manifest is on disk but fails ``_validate_tour`` — the two
    failure modes ``docs/backlog/user-guide-demo.md`` calls out by name:
    an unknown tour is a normal "no tour here" 404, a malformed one is a
    bug that must not serve garbage to ``tour.js``.
    """
    try:
        tours = _tours()
    except ValueError as exc:
        log.error("manual: tour manifest %r failed to load: %s", slug, exc)
        raise HTTPException(
            status_code=500, detail="tour manifest failed to load"
        ) from exc
    tour = tours.get(slug)
    if tour is None:
        raise HTTPException(status_code=404, detail=f"no such tour {slug!r}")
    return JSONResponse(tour)
