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

The markdown is repo-controlled (it ships in the wheel), so it is rendered
trusted — no sanitizer. ``slug`` is nonetheless resolved against the
discovered chapter set rather than joined onto a path, so a traversal
attempt 404s instead of reading the filesystem.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from markdown_it import MarkdownIt

from precis_web.deps import templates

router = APIRouter(tags=["manual"])

log = logging.getLogger(__name__)

MANUAL_DIR = Path(__file__).resolve().parent.parent / "manual"

#: ``01-writing-a-paper.md`` → chapter 1, slug ``writing-a-paper``. A file
#: that does not match is not a chapter and is skipped (drafts, notes).
_CHAPTER_RE = re.compile(r"^(\d+)-([a-z0-9-]+)\.md$")

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
        },
    )
