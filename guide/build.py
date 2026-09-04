#!/usr/bin/env -S uv run python
"""guide/build.py — assemble ``guide/index.html`` AND ``guide/README.md``
from the tour manifests + narration + (once captured) the animated SVG/mp3
assets.

One source of truth per section, same as the rest of the pipeline
(``scripts/guide-capture`` / ``scripts/guide-annotate`` / ``scripts/guide-
narrate``): ``guide_lib.discover_guide_sections`` walks ``guide/narration/``
and titles each section from its tour manifest (``src/precis_web/manual/
tour/<nn>-<slug>.json``) or, for section 00 (no tour), the manual chapter's
own ``# `` heading. Two outputs, both pure functions of the same
``sections``/``assets``:

- ``index.html`` — a single self-contained page: inline CSS, a small inline
  ``<script>`` for the audio players, no CDN — works as a GitHub Pages
  artifact or a `file://` open with no build step besides this one.
  Gitignored; the Pages workflow regenerates it on every push.
- ``README.md`` — a GitHub-flavored markdown rendering of the same content
  (nav + per-section animated SVG + transcript, no audio — markdown can't
  embed it) so the guide is viewable **in the repo itself** with no Pages
  dependency. Unlike ``index.html`` this one IS committed — regenerate and
  commit it after any narration/capture change.

Degrades gracefully: a section with no ``guide/assets/<slug>/tour.svg`` yet
gets a neutral "captures pending" placeholder instead of a broken ``<img>``
(html) / image reference (markdown); one with no
``guide/assets/audio/<slug>.mp3`` yet just has no player (html only —
markdown never had one). Running this against a bare checkout (no captures,
no narration audio) still produces valid output for both — that's the state
exercised by ``tests/test_guide_scripts.py``.

Usage::

    uv run python guide/build.py                  # writes both outputs
    uv run python guide/build.py --out /tmp/g.html --md-out /tmp/g.md
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from guide_lib import (
    GuideError,
    GuideSection,
    assets_dir,
    discover_guide_sections,
    load_manifest,
    repo_root,
)

_REPO_URL = "https://github.com/retospect/precis-mcp"

_CSS = """
:root {
  color-scheme: light;
  --bg: #f8fafc;
  --panel: #ffffff;
  --ink: #0f172a;
  --muted: #475569;
  --border: #e2e8f0;
  --accent: #f59e0b;
  --nav-bg: #0f172a;
  --nav-ink: #e2e8f0;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    Helvetica, Arial, sans-serif;
  background: var(--bg);
  color: var(--ink);
  display: flex;
  min-height: 100vh;
}
nav.guide-nav {
  position: sticky;
  top: 0;
  height: 100vh;
  width: 240px;
  flex: 0 0 240px;
  background: var(--nav-bg);
  color: var(--nav-ink);
  overflow-y: auto;
  padding: 1.5rem 1rem;
}
nav.guide-nav .brand {
  font-weight: 700;
  font-size: 1.1rem;
  margin-bottom: 1rem;
  color: #fff;
}
nav.guide-nav ol {
  list-style: none;
  margin: 0;
  padding: 0;
}
nav.guide-nav li { margin: 0.15rem 0; }
nav.guide-nav a {
  display: block;
  padding: 0.4rem 0.6rem;
  border-radius: 6px;
  color: var(--nav-ink);
  text-decoration: none;
  font-size: 0.92rem;
}
nav.guide-nav a:hover { background: rgba(245, 158, 11, 0.15); color: var(--accent); }
main.guide-main {
  flex: 1;
  min-width: 0;
  padding: 2.5rem 3rem 6rem;
  max-width: 900px;
}
header.guide-header { margin-bottom: 2.5rem; }
header.guide-header h1 { margin: 0 0 0.4rem; font-size: 1.8rem; }
header.guide-header p.pitch { color: var(--muted); font-size: 1.1rem; margin: 0 0 0.6rem; }
header.guide-header a.repo-link { color: var(--accent); text-decoration: none; font-weight: 600; }
header.guide-header a.repo-link:hover { text-decoration: underline; }
section.guide-section {
  border-top: 1px solid var(--border);
  padding: 2.5rem 0;
  scroll-margin-top: 1rem;
}
section.guide-section:first-of-type { border-top: none; }
section.guide-section h2 { font-size: 1.3rem; margin: 0 0 1rem; }
.guide-asset { margin: 1rem 0; }
.guide-asset img { max-width: 100%; border-radius: 8px; border: 1px solid var(--border); }
.guide-asset .pending {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 220px;
  border: 1px dashed var(--border);
  border-radius: 8px;
  color: var(--muted);
  background: var(--panel);
  font-size: 0.95rem;
}
.guide-audio { margin: 1rem 0; }
.guide-audio audio { width: 100%; }
.guide-audio .pending { color: var(--muted); font-size: 0.9rem; font-style: italic; }
.guide-transcript p { line-height: 1.6; color: var(--ink); }
"""

_JS = """
document.addEventListener('DOMContentLoaded', function () {
  // Pausing every other player when one starts keeps the page from
  // narrating two sections over each other if a visitor clicks around.
  var players = document.querySelectorAll('audio');
  players.forEach(function (p) {
    p.addEventListener('play', function () {
      players.forEach(function (other) {
        if (other !== p) other.pause();
      });
    });
  });
});
"""

_SENTENCE_RE = re.compile(r".+?[.!?](?=\s|$)", re.DOTALL)


def _pitch_from_narration(text: str) -> str:
    """The header's one-line pitch: the first sentence of the first prose
    block (i.e. skipping the leading ``# `` heading) in the section-00
    narration."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    prose = next((b for b in blocks if not b.lstrip().startswith("#")), "")
    if not prose:
        return ""
    joined = " ".join(line.strip() for line in prose.splitlines())
    m = _SENTENCE_RE.match(joined)
    return (m.group(0) if m else joined).strip()


def _transcript_paragraphs(text: str) -> list[str]:
    """Narration markdown -> plain paragraphs (drop the leading heading)."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    out = []
    for block in blocks:
        if block.lstrip().startswith("#"):
            continue
        out.append(" ".join(line.strip() for line in block.splitlines()))
    return out


def _render_asset(section: GuideSection, assets: Path) -> str:
    if section.manifest_path is None:
        # No tour manifest (section 00) — a capture will never exist, so
        # no asset slot at all rather than an eternal "pending".
        return ""
    svg_path = assets / section.slug / "tour.svg"
    if svg_path.is_file():
        rel = f"assets/{section.slug}/tour.svg"
        return (
            f'<div class="guide-asset"><img src="{html.escape(rel)}" '
            f'alt="{html.escape(section.title)} — animated tour"></div>'
        )
    return '<div class="guide-asset"><div class="pending">captures pending</div></div>'


def _render_audio(section: GuideSection, assets: Path) -> str:
    mp3_path = assets / "audio" / f"{section.slug}.mp3"
    if mp3_path.is_file():
        rel = f"assets/audio/{section.slug}.mp3"
        return (
            f'<div class="guide-audio"><audio controls src="{html.escape(rel)}">'
            "</audio></div>"
        )
    return (
        '<div class="guide-audio"><span class="pending">narration pending</span></div>'
    )


def _render_section(section: GuideSection, assets: Path) -> str:
    paragraphs = _transcript_paragraphs(
        section.narration_path.read_text(encoding="utf-8")
    )
    transcript = "\n".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)
    parts = [
        f'<section class="guide-section" id="{html.escape(section.slug)}">',
        f"  <h2>{html.escape(section.index)} · {html.escape(section.title)}</h2>",
    ]
    asset = _render_asset(section, assets)
    if asset:
        parts.append(f"  {asset}")
    parts.append(f"  {_render_audio(section, assets)}")
    parts.append(f'  <div class="guide-transcript">\n{transcript}\n  </div>')
    parts.append("</section>")
    return "\n".join(parts)


def _render_nav(sections: list[GuideSection]) -> str:
    items = "\n".join(
        f'    <li><a href="#{html.escape(s.slug)}">{html.escape(s.index)} '
        f"{html.escape(s.title)}</a></li>"
        for s in sections
    )
    return f'<nav class="guide-nav">\n  <div class="brand">precis</div>\n  <ol>\n{items}\n  </ol>\n</nav>'


def build_html(sections: list[GuideSection], assets: Path) -> str:
    """Assemble the full ``index.html`` document. Pure function of
    ``sections`` + whatever asset files currently exist under ``assets`` —
    deterministic, idempotent, no network/DB."""
    if not sections:
        raise GuideError("no guide sections found (guide/narration/ is empty?)")

    pitch = ""
    for s in sections:
        if s.index == "00":
            pitch = _pitch_from_narration(s.narration_path.read_text(encoding="utf-8"))
            break

    nav = _render_nav(sections)
    body_sections = "\n".join(_render_section(s, assets) for s in sections)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>precis — a two-minute tour</title>
<style>{_CSS}</style>
</head>
<body>
{nav}
<main class="guide-main">
  <header class="guide-header">
    <h1>precis</h1>
    <p class="pitch">{html.escape(pitch)}</p>
    <a class="repo-link" href="{html.escape(_REPO_URL)}">{html.escape(_REPO_URL)}</a>
  </header>
{body_sections}
</main>
<script>{_JS}</script>
</body>
</html>
"""


_MD_HEADER_COMMENT = (
    "<!-- Generated by guide/build.py — do not edit by hand; edit "
    "guide/narration/ and the tour manifests, then re-run. -->"
)

_PAGES_URL = "https://retospect.github.io/precis-mcp/"


def _render_md_asset(section: GuideSection, assets: Path) -> str:
    if section.manifest_path is None:
        # No tour manifest (section 00) — a capture will never exist, so
        # no asset line at all rather than an eternal "pending".
        return ""
    svg_path = assets / section.slug / "tour.svg"
    if svg_path.is_file():
        rel = f"assets/{section.slug}/tour.svg"
        return f"![{section.title} — animated tour]({rel})"
    return "*captures pending*"


def _render_md_section(section: GuideSection, assets: Path) -> str:
    paragraphs = _transcript_paragraphs(
        section.narration_path.read_text(encoding="utf-8")
    )
    transcript = "\n\n".join(paragraphs)
    parts = [
        f'<a id="{section.slug}"></a>\n## {section.index} · {section.title}',
    ]
    asset = _render_md_asset(section, assets)
    if asset:
        parts.append(asset)
    parts.append(transcript)
    return "\n\n".join(parts)


def _render_md_nav(sections: list[GuideSection]) -> str:
    items = "\n".join(f"- [{s.index} · {s.title}](#{s.slug})" for s in sections)
    return items


def build_markdown(sections: list[GuideSection], assets: Path) -> str:
    """Assemble ``guide/README.md``: the same content as :func:`build_html`,
    rendered as GitHub-flavored markdown so the guide is viewable in the
    repo itself with no Pages dependency. Pure function of ``sections`` +
    whatever asset files currently exist under ``assets`` — deterministic,
    idempotent, no network/DB.

    Nav links target an explicit ``<a id="<slug>"></a>`` placed immediately
    before each section heading rather than relying on GitHub's heading
    auto-slugger — headings containing ``·`` and leading digits produce
    fragile/inconsistent auto-slugs, and GFM preserves inline HTML anchors.
    """
    if not sections:
        raise GuideError("no guide sections found (guide/narration/ is empty?)")

    pitch = ""
    for s in sections:
        if s.index == "00":
            pitch = _pitch_from_narration(s.narration_path.read_text(encoding="utf-8"))
            break

    nav = _render_md_nav(sections)
    body_sections = "\n\n".join(_render_md_section(s, assets) for s in sections)

    return f"""{_MD_HEADER_COMMENT}

# precis — a two-minute tour

{pitch}

The narrated version, with audio, lives on the Pages site (once enabled):
[{_PAGES_URL}]({_PAGES_URL})

Agents: a machine-readable version of this tour — every section's title,
route, and full transcript — is [transcript.json](transcript.json)
(sources: [narration/](narration/)).

{nav}

{body_sections}
"""


def build_transcript(sections: list[GuideSection], assets: Path) -> str:
    """Assemble ``guide/transcript.json``: the same sections as the two
    rendered documents, as structured data — for agents asked about the
    tour, so they can fetch the full narration without scraping the
    README's prose. Pure function, deterministic (sorted keys, stable
    order, trailing newline)."""
    if not sections:
        raise GuideError("no guide sections found (guide/narration/ is empty?)")
    entries = []
    for s in sections:
        route = None
        if s.manifest_path is not None:
            route = load_manifest(s.manifest_path).get("route")
        svg = f"assets/{s.slug}/tour.svg"
        mp3 = f"assets/audio/{s.slug}.mp3"
        entries.append(
            {
                "index": s.index,
                "slug": s.slug,
                "title": s.title,
                "route": route,
                "tour_svg": svg if (assets / s.slug / "tour.svg").is_file() else None,
                "audio_mp3": mp3
                if (assets / "audio" / f"{s.slug}.mp3").is_file()
                else None,
                "transcript": _transcript_paragraphs(
                    s.narration_path.read_text(encoding="utf-8")
                ),
            }
        )
    return json.dumps(entries, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out", type=Path, default=None, help="Output path (default guide/index.html)."
    )
    p.add_argument(
        "--md-out",
        type=Path,
        default=None,
        help="Markdown output path (default guide/README.md).",
    )
    p.add_argument(
        "--transcript-out",
        type=Path,
        default=None,
        help="Transcript JSON output path (default guide/transcript.json).",
    )
    p.add_argument(
        "--root", type=Path, default=None, help="Repo root override (tests)."
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    root = args.root or repo_root()
    out = args.out or (root / "guide" / "index.html")
    md_out = args.md_out or (root / "guide" / "README.md")
    transcript_out = args.transcript_out or (root / "guide" / "transcript.json")

    try:
        sections = discover_guide_sections(root)
        assets = assets_dir(root)
        doc = build_html(sections, assets)
        md_doc = build_markdown(sections, assets)
        transcript = build_transcript(sections, assets)
    except GuideError as exc:
        print(f"guide/build.py: {exc}", file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    md_out.parent.mkdir(parents=True, exist_ok=True)
    md_out.write_text(md_doc, encoding="utf-8")
    transcript_out.parent.mkdir(parents=True, exist_ok=True)
    transcript_out.write_text(transcript, encoding="utf-8")
    print(
        f"guide/build.py: wrote {out}, {md_out} and {transcript_out} "
        f"({len(sections)} section(s))"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
