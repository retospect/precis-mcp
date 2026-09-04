"""Shared pure logic for the ``scripts/guide-*`` user-guide pipeline.

``scripts/guide-capture`` and ``scripts/guide-annotate`` are thin CLI shells
around the functions here, so the parts worth unit-testing (route/{id}
resolution, manifest+sidecar merging, SVG templating, PNG callout drawing)
live in one importable module with no docker/network/DB dependency of their
own — ``tests/test_guide_scripts.py`` exercises this module directly.

Manifest schema (``src/precis_web/manual/tour/<nn>-<slug>.json``, defined by
slice 1 in ``src/precis_web/routes/manual.py``'s ``_validate_tour``):
``{title, route, steps: [{anchor, heading, text, placement}]}``. This module
re-validates independently rather than importing ``precis_web`` — the
capture pipeline (esp. ``guide_capture_inner.py``, run inside a bare
Playwright container) must stay decoupled from the web app's own package.

Sidecar schema (``guide/assets/<slug>/steps.json``, written by
``guide_capture_inner.py``)::

    {
      "slug": "...", "route": "...", "url": "...",
      "viewport": {"width": 1600, "height": 1000},
      "steps": [
        {"anchor": "...", "index": 1, "png": "step-1.png",
         "rect": {"x": .., "y": .., "width": .., "height": ..}},
        ...
      ]
    }
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GuideError(RuntimeError):
    """A guide-pipeline defect worth failing loudly on (UI drift, a bad
    manifest, a missing --id) — never swallowed, always names the file/route/
    anchor at fault."""


# ---------------------------------------------------------------------------
# Manifest loading + validation (mirrors precis_web.routes.manual._validate_tour)
# ---------------------------------------------------------------------------

_PLACEMENTS = {"top", "bottom", "left", "right"}


def load_manifest(path: Path) -> dict[str, Any]:
    """Parse + validate one ``<nn>-<slug>.json`` tour manifest.

    Raises :class:`GuideError` naming the file and the exact defect — same
    contract as the web route's ``_validate_tour``, so a manifest that would
    500 the in-app tour also fails loudly here instead of producing a
    silently-wrong capture.
    """
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GuideError(f"{path.name}: not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GuideError(f"{path.name}: top level must be a JSON object")
    for key in ("title", "route", "steps"):
        if key not in data:
            raise GuideError(f"{path.name}: missing required key {key!r}")
    if not isinstance(data["title"], str) or not data["title"].strip():
        raise GuideError(f"{path.name}: 'title' must be a non-empty string")
    if not isinstance(data["route"], str) or not data["route"].strip():
        raise GuideError(f"{path.name}: 'route' must be a non-empty string")
    steps = data["steps"]
    if not isinstance(steps, list) or not steps:
        raise GuideError(f"{path.name}: 'steps' must be a non-empty list")
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise GuideError(f"{path.name}: step {i} is not an object")
        for key in ("anchor", "heading", "text", "placement"):
            if not isinstance(step.get(key), str) or not step[key].strip():
                raise GuideError(f"{path.name}: step {i} has no non-empty {key!r}")
        if step["placement"] not in _PLACEMENTS:
            raise GuideError(
                f"{path.name}: step {i} placement {step['placement']!r} not "
                f"in {sorted(_PLACEMENTS)}"
            )
    return data


def slug_from_manifest_path(path: Path) -> str:
    """``01-drive.json`` -> ``drive`` (same ``<nn>-<slug>`` convention as the
    manual chapters / the web route's ``_TOUR_RE``)."""
    m = re.match(r"^(\d+)-([a-z0-9-]+)\.json$", path.name)
    if m is None:
        raise GuideError(f"{path.name}: not a '<nn>-<slug>.json' tour manifest")
    return m.group(2)


def discover_manifests(tour_dir: Path, only: str | None = None) -> list[Path]:
    """Every ``<nn>-<slug>.json`` under ``tour_dir``, sorted by filename (=
    section order). ``only`` filters to one slug, raising :class:`GuideError`
    if it doesn't match anything found."""
    found = sorted(
        p
        for p in tour_dir.glob("*.json")
        if re.match(r"^\d+-[a-z0-9-]+\.json$", p.name)
    )
    if only is None:
        return found
    matches = [p for p in found if slug_from_manifest_path(p) == only]
    if not matches:
        known = ", ".join(slug_from_manifest_path(p) for p in found)
        raise GuideError(f"--only {only!r}: no manifest with that slug (have: {known})")
    return matches


# ---------------------------------------------------------------------------
# Route / {id} resolution
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def parse_id_flags(pairs: list[str]) -> dict[str, str]:
    """``["writing-a-paper=dr123", "reading-papers=pp456"]`` -> a
    ``slug -> value`` dict. Each entry must be ``slug=value``; a malformed
    entry (no ``=``, or an empty slug/value) is a hard error."""
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise GuideError(f"--id {pair!r}: expected 'slug=value' (missing '=')")
        slug, _, value = pair.partition("=")
        slug, value = slug.strip(), value.strip()
        if not slug or not value:
            raise GuideError(f"--id {pair!r}: expected 'slug=value' (empty slug/value)")
        out[slug] = value
    return out


def resolve_route(route: str, slug: str, ids: dict[str, str]) -> str:
    """Fill the ``{id}`` (or any other ``{placeholder}``) in a manifest's
    ``route`` template for section ``slug``.

    Every placeholder in the route is filled from the SAME ``--id
    <slug>=<value>`` value — manifests only ever carry one placeholder
    (``{id}``) per the slice-1 schema, but this stays general rather than
    hard-coding the name. Missing value -> :class:`GuideError` naming the
    slug and the placeholder, which is the UI-drift/operator-error tripwire
    the spec calls for ("error if missing").
    """
    placeholders = _PLACEHOLDER_RE.findall(route)
    if not placeholders:
        return route
    value = ids.get(slug)
    if value is None:
        names = ", ".join(sorted(set(placeholders)))
        raise GuideError(
            f"section {slug!r}: route {route!r} needs {{{names}}} but no "
            f"--id {slug}=<value> was given"
        )
    resolved = route
    for name in set(placeholders):
        resolved = resolved.replace("{" + name + "}", value)
    return resolved


# ---------------------------------------------------------------------------
# Sidecar + manifest merge (the input to annotation rendering)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergedStep:
    """One tour step with both its manifest content (heading/text/placement)
    and its captured geometry (anchor bounding rect) — what
    ``guide-annotate`` needs to burn a callout onto the screenshot."""

    index: int
    anchor: str
    heading: str
    text: str
    placement: str
    png: str
    rect: dict[str, float]


def merge_manifest_and_sidecar(
    manifest: dict[str, Any], sidecar: dict[str, Any]
) -> list[MergedStep]:
    """Pair each manifest step with its captured rect from the sidecar, by
    anchor. Order follows the manifest (= tour order); a manifest step with
    no matching sidecar entry is a hard error (a capture that silently
    dropped a step must not render a step-less/blank annotation)."""
    by_anchor = {s["anchor"]: s for s in sidecar.get("steps", [])}
    merged: list[MergedStep] = []
    for step in manifest["steps"]:
        anchor = step["anchor"]
        captured = by_anchor.get(anchor)
        if captured is None:
            raise GuideError(
                f"sidecar has no captured step for anchor {anchor!r} "
                f"(manifest {manifest.get('title')!r})"
            )
        merged.append(
            MergedStep(
                index=captured.get("index", len(merged) + 1),
                anchor=anchor,
                heading=step["heading"],
                text=step["text"],
                placement=step["placement"],
                png=captured["png"],
                rect=captured["rect"],
            )
        )
    return merged


# ---------------------------------------------------------------------------
# Animated SVG builder (plain string templating — no XML library dependency
# beyond stdlib, matching the "pure-python SVG templating" call in the spec)
# ---------------------------------------------------------------------------


def _esc(text: str) -> str:
    """Minimal XML text escaping for the strings we interpolate (heading /
    text come from repo-controlled manifests, but escape regardless — this
    SVG is embedded verbatim in a rendered README)."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _wrap_text(text: str, max_chars: int) -> list[str]:
    """Greedy word-wrap for the callout body — SVG has no native reflow."""
    words = text.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        candidate = f"{cur} {word}".strip()
        if len(candidate) > max_chars and cur:
            lines.append(cur)
            cur = word
        else:
            cur = candidate
    if cur:
        lines.append(cur)
    return lines


def build_tour_svg(
    *,
    slug: str,
    title: str,
    png_bytes: bytes,
    viewport: dict[str, float],
    steps: list[MergedStep],
    step_seconds: float = 4.0,
) -> str:
    """One self-contained animated SVG per section: the step-1 screenshot as
    a base64-embedded raster layer, plus a highlight/connector/callout group
    per step that fades in/out in sequence via SMIL ``<animate>`` on a loop.

    Self-contained by construction: the only image reference is a ``data:``
    URI (no external ``href``), and there is no ``<script>`` — SMIL is
    native SVG animation, which is exactly what GitHub's README sanitizer
    renders inline.
    """
    if not steps:
        raise GuideError(f"{slug}: cannot build an SVG with zero steps")
    width = viewport.get("width", 1600)
    height = viewport.get("height", 1000)
    b64 = base64.b64encode(png_bytes).decode("ascii")
    total = step_seconds * len(steps)

    groups: list[str] = []
    for i, step in enumerate(steps):
        begin = i * step_seconds
        rect = step.rect
        rx, ry, rw, rh = rect["x"], rect["y"], rect["width"], rect["height"]

        # Callout box placement: offset from the anchor rect in the
        # direction named by `placement`, clamped onto the canvas.
        box_w, box_h = min(360, width - 40), 120
        if step.placement == "right":
            bx, by = min(rx + rw + 24, width - box_w - 20), ry
        elif step.placement == "left":
            bx, by = max(rx - box_w - 24, 20), ry
        elif step.placement == "top":
            bx, by = rx, max(ry - box_h - 24, 20)
        else:  # bottom
            bx, by = rx, min(ry + rh + 24, height - box_h - 20)
        bx = max(20, min(bx, width - box_w - 20))
        by = max(20, min(by, height - box_h - 20))

        # Connector: anchor-rect centre -> callout-box centre.
        ax, ay = rx + rw / 2, ry + rh / 2
        tx, ty = bx + box_w / 2, by + box_h / 2

        lines = _wrap_text(step.text, 46)[:4]
        text_tspans = "".join(
            f'<tspan x="{bx + 16}" dy="{"0" if j == 0 else "18"}">{_esc(line)}</tspan>'
            for j, line in enumerate(lines)
        )

        groups.append(
            f'<g opacity="0">'
            f'<animate attributeName="opacity" dur="{total}s" '
            f'repeatCount="indefinite" '
            f'keyTimes="0;{begin / total:.4f};{(begin + 0.15) / total:.4f};'
            f'{(begin + step_seconds - 0.15) / total:.4f};{(begin + step_seconds) / total:.4f};1" '
            f'values="0;0;1;1;0;0"/>'
            f'<rect x="{rx}" y="{ry}" width="{rw}" height="{rh}" fill="none" '
            f'stroke="#f59e0b" stroke-width="3" rx="4"/>'
            f'<line x1="{ax}" y1="{ay}" x2="{tx}" y2="{ty}" stroke="#f59e0b" '
            f'stroke-width="2"/>'
            f'<polygon points="{tx - 6},{ty - 6} {tx + 6},{ty - 6} {tx},{ty + 8}" '
            f'fill="#f59e0b"/>'
            f'<rect x="{bx}" y="{by}" width="{box_w}" height="{box_h}" rx="10" '
            f'fill="#0f172a" fill-opacity="0.92" stroke="#f59e0b" stroke-width="1.5"/>'
            f'<text x="{bx + 16}" y="{by + 28}" fill="#f59e0b" '
            f'font-family="sans-serif" font-size="16" font-weight="bold">'
            f"{_esc(step.heading)}</text>"
            f'<text x="{bx + 16}" y="{by + 52}" fill="#e2e8f0" '
            f'font-family="sans-serif" font-size="13">{text_tspans}</text>'
            f"</g>"
        )

    body = "\n  ".join(groups)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{_esc(title)} — animated tour">\n'
        f"  <title>{_esc(title)}</title>\n"
        f'  <image x="0" y="0" width="{width}" height="{height}" '
        f'href="data:image/png;base64,{b64}"/>\n'
        f"  {body}\n"
        "</svg>\n"
    )


# ---------------------------------------------------------------------------
# Static annotated PNG (Pillow — lazily imported so the pure-logic functions
# above stay importable/testable without the `guide` extra installed)
# ---------------------------------------------------------------------------


def annotate_png(
    image: Any, rect: dict[str, float], heading: str, text: str, placement: str
) -> Any:
    """Burn a highlight rectangle + connector arrow + callout box onto a
    copy of ``image`` (a ``PIL.Image.Image``) for one step. Pure function —
    returns a new image, never mutates the input.

    Raises :class:`GuideError` (wrapping ``ImportError``) if Pillow isn't
    installed — callers (``guide-annotate``) let that surface as "install
    ``precis-mcp[guide]``"; tests import-skip instead of failing.
    """
    try:
        from PIL import ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover — exercised by install state
        raise GuideError(
            "annotate_png needs Pillow — install 'precis-mcp[guide]'"
        ) from exc

    # Pillow's built-in bitmap font has no em-dash (or much else beyond
    # ASCII) — manifest prose renders with tofu boxes. Prefer a real
    # TrueType face; the SVG twin doesn't need this (browser text).
    def _font(size: int) -> Any:
        for cand in (
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",  # macOS
            "/System/Library/Fonts/Helvetica.ttc",  # macOS
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Linux
        ):
            try:
                return ImageFont.truetype(cand, size)
            except OSError:
                continue
        return ImageFont.load_default()

    # Emoji live outside every system text face — drop astral-plane chars
    # from the burned PNG (the animated SVG keeps them).
    def _pngsafe(s: str) -> str:
        return " ".join("".join(c for c in s if ord(c) <= 0xFFFF).split())

    heading, text = _pngsafe(heading), _pngsafe(text)

    out = image.convert("RGB").copy()
    draw = ImageDraw.Draw(out)
    width, height = out.size

    rx, ry, rw, rh = rect["x"], rect["y"], rect["width"], rect["height"]
    draw.rectangle([rx, ry, rx + rw, ry + rh], outline=(245, 158, 11), width=3)

    box_w, box_h = min(360, width - 40), 140
    if placement == "right":
        bx, by = min(rx + rw + 24, width - box_w - 20), ry
    elif placement == "left":
        bx, by = max(rx - box_w - 24, 20), ry
    elif placement == "top":
        bx, by = rx, max(ry - box_h - 24, 20)
    else:  # bottom
        bx, by = rx, min(ry + rh + 24, height - box_h - 20)
    bx = max(20, min(bx, width - box_w - 20))
    by = max(20, min(by, height - box_h - 20))

    ax, ay = rx + rw / 2, ry + rh / 2
    tx, ty = bx + box_w / 2, by + box_h / 2
    draw.line([(ax, ay), (tx, ty)], fill=(245, 158, 11), width=3)
    draw.polygon(
        [(tx - 7, ty - 7), (tx + 7, ty - 7), (tx, ty + 9)], fill=(245, 158, 11)
    )

    draw.rounded_rectangle(
        [bx, by, bx + box_w, by + box_h],
        radius=12,
        fill=(15, 23, 42, 235),
        outline=(245, 158, 11),
        width=2,
    )
    draw.text((bx + 16, by + 14), heading, fill=(245, 158, 11), font=_font(15))
    y = by + 40
    body_font = _font(13)
    for line in _wrap_text(text, 46)[:5]:
        draw.text((bx + 16, y), line, fill=(226, 232, 240), font=body_font)
        y += 18

    return out


# ---------------------------------------------------------------------------
# Repo layout helpers
# ---------------------------------------------------------------------------


def repo_root() -> Path:
    """This file lives at ``<repo>/scripts/guide_lib.py``."""
    return Path(__file__).resolve().parent.parent


def tour_dir(root: Path | None = None) -> Path:
    return (root or repo_root()) / "src" / "precis_web" / "manual" / "tour"


def manual_dir(root: Path | None = None) -> Path:
    return (root or repo_root()) / "src" / "precis_web" / "manual"


def assets_dir(root: Path | None = None) -> Path:
    return (root or repo_root()) / "guide" / "assets"


def narration_dir(root: Path | None = None) -> Path:
    return (root or repo_root()) / "guide" / "narration"


# ---------------------------------------------------------------------------
# Narration markdown discovery (``guide/narration/<nn>-<slug>.md``) — the
# render driver (``scripts/guide-narrate``) and the site builder
# (``guide/build.py``) both need "which sections exist, in order", so that
# discovery lives here rather than being duplicated in each script, same as
# ``discover_manifests`` above.
# ---------------------------------------------------------------------------

_NARRATION_RE = re.compile(r"^(\d+)-([a-z0-9-]+)\.md$")


def slug_from_narration_path(path: Path) -> str:
    """``01-drive.md`` -> ``drive`` (same ``<nn>-<slug>`` convention as the
    tour manifests / manual chapters)."""
    m = _NARRATION_RE.match(path.name)
    if m is None:
        raise GuideError(f"{path.name}: not a '<nn>-<slug>.md' narration file")
    return m.group(2)


def discover_narration(dir_: Path, only: str | None = None) -> list[Path]:
    """Every ``<nn>-<slug>.md`` under ``dir_``, sorted by filename (= section
    order). ``only`` filters to one slug, raising :class:`GuideError` if it
    doesn't match anything found."""
    found = sorted(p for p in dir_.glob("*.md") if _NARRATION_RE.match(p.name))
    if only is None:
        return found
    matches = [p for p in found if slug_from_narration_path(p) == only]
    if not matches:
        known = ", ".join(slug_from_narration_path(p) for p in found)
        raise GuideError(
            f"--only {only!r}: no narration file with that slug (have: {known})"
        )
    return matches


def chapter_title(path: Path) -> str:
    """The title of a manual chapter / narration file: its first ``# `` (h1)
    heading line."""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    raise GuideError(f"{path}: no '# ' (h1) heading found for a title")


@dataclass(frozen=True)
class GuideSection:
    """One guide-site section: a narration file plus wherever its title
    comes from — a tour manifest (sections 01-08) or a manual chapter with no
    tour of its own (section 00, "the underlying model"). ``manifest_path``
    is ``None`` for the latter, which callers use to know a tour SVG/capture
    will never exist for this section."""

    index: str
    slug: str
    title: str
    narration_path: Path
    manifest_path: Path | None


def discover_guide_sections(root: Path | None = None) -> list[GuideSection]:
    """The full ordered list of guide sections, built from
    ``guide/narration/<nn>-<slug>.md`` — one source of truth for "which
    sections exist, in what order, titled how". A narration file's title
    comes from the matching tour manifest if one exists at
    ``<nn>-<slug>.json``, else the matching manual chapter
    ``<nn>-<slug>.md``; neither existing is a hard error (an orphaned
    narration file with nothing to title it)."""
    root = root or repo_root()
    sections: list[GuideSection] = []
    for narr in discover_narration(narration_dir(root)):
        m = _NARRATION_RE.match(narr.name)
        assert m is not None  # discover_narration already filtered on this
        index, slug = m.group(1), m.group(2)
        candidate_manifest = tour_dir(root) / f"{index}-{slug}.json"
        manifest_path: Path | None
        if candidate_manifest.is_file():
            manifest_path = candidate_manifest
            title = load_manifest(candidate_manifest)["title"]
        else:
            manifest_path = None
            chapter_path = manual_dir(root) / f"{index}-{slug}.md"
            if not chapter_path.is_file():
                raise GuideError(
                    f"{narr.name}: no tour manifest ({index}-{slug}.json) and "
                    f"no manual chapter ({index}-{slug}.md) to title this section"
                )
            title = chapter_title(chapter_path)
        sections.append(
            GuideSection(
                index=index,
                slug=slug,
                title=title,
                narration_path=narr,
                manifest_path=manifest_path,
            )
        )
    return sections


# ---------------------------------------------------------------------------
# Video assembly helpers (``scripts/guide-video``) — the pure, testable
# parts: mp3-duration parsing (the static imageio-ffmpeg binary ships no
# ffprobe, so duration comes from ``ffmpeg -i`` stderr), even frame timing,
# and ffconcat playlist rendering. The ffmpeg invocations themselves live in
# the script; nothing here spawns a process.
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")


def parse_ffmpeg_duration(text: str) -> float:
    """Seconds from an ``ffmpeg -i <file>`` stderr dump (``Duration:
    HH:MM:SS.cc``). ``ffmpeg -i`` with no output exits non-zero by design —
    callers pass its stderr here regardless of exit code."""
    m = _DURATION_RE.search(text)
    if m is None:
        raise GuideError("could not parse a Duration: line from ffmpeg output")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def even_frame_durations(n_frames: int, total_seconds: float) -> list[float]:
    """Split ``total_seconds`` evenly across ``n_frames`` (the spec's "step
    PNGs timed across that section's mp3"). The last frame absorbs rounding
    so the list sums to exactly ``total_seconds`` (at ms precision)."""
    if n_frames <= 0:
        raise GuideError(f"even_frame_durations: n_frames must be >= 1, got {n_frames}")
    if total_seconds <= 0:
        raise GuideError(
            f"even_frame_durations: total_seconds must be > 0, got {total_seconds}"
        )
    per = round(total_seconds / n_frames, 3)
    durations = [per] * (n_frames - 1)
    durations.append(round(total_seconds - per * (n_frames - 1), 3))
    return durations


def ffconcat_playlist(frames: list[Path], durations: list[float]) -> str:
    """An ``ffconcat version 1.0`` playlist showing each frame for its
    duration. The final frame is listed once more without a duration — the
    concat demuxer convention that keeps the last frame on screen instead of
    ending the video track a frame early."""
    if len(frames) != len(durations):
        raise GuideError(
            f"ffconcat_playlist: {len(frames)} frame(s) vs {len(durations)} duration(s)"
        )
    if not frames:
        raise GuideError("ffconcat_playlist: no frames")
    lines = ["ffconcat version 1.0"]
    for frame, duration in zip(frames, durations, strict=True):
        lines.append(f"file '{frame}'")
        lines.append(f"duration {duration:.3f}")
    lines.append(f"file '{frames[-1]}'")
    return "\n".join(lines) + "\n"


def section_video_frames(assets: Path, slug: str) -> list[Path]:
    """The section's annotated step PNGs in step order (numeric — ``step-10``
    sorts after ``step-2``). Empty list when the section has no captures
    (section 00): the video script renders a title card instead."""
    step_re = re.compile(r"^step-(\d+)-annotated\.png$")
    found: list[tuple[int, Path]] = []
    section_dir = assets / slug
    if section_dir.is_dir():
        for p in section_dir.iterdir():
            m = step_re.match(p.name)
            if m:
                found.append((int(m.group(1)), p))
    return [p for _, p in sorted(found)]


def render_title_card(
    out_path: Path,
    title: str,
    subtitle: str,
    *,
    size: tuple[int, int] = (1600, 1000),
) -> None:
    """A dark title-card PNG for a section with no captures (section 00) —
    same canvas size as the screenshots so the video filter chain treats
    every frame identically. Amber brand + white title + wrapped subtitle,
    matching the callout style. Needs Pillow (the ``guide`` extra), like
    :func:`annotate_png`."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover — exercised by install state
        raise GuideError(
            "render_title_card needs Pillow — install 'precis-mcp[guide]'"
        ) from exc

    def _font(font_size: int) -> Any:
        for cand in (
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",  # macOS
            "/System/Library/Fonts/Helvetica.ttc",  # macOS
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Linux
        ):
            try:
                return ImageFont.truetype(cand, font_size)
            except OSError:
                continue
        return ImageFont.load_default()

    width, height = size
    img = Image.new("RGB", size, (15, 23, 42))
    draw = ImageDraw.Draw(img)
    draw.text(
        (width // 2, int(height * 0.32)),
        "precis",
        fill=(245, 158, 11),
        font=_font(72),
        anchor="mm",
    )
    draw.text(
        (width // 2, int(height * 0.46)),
        title,
        fill=(226, 232, 240),
        font=_font(44),
        anchor="mm",
    )
    y = int(height * 0.58)
    for line in _wrap_text(subtitle, 70)[:4]:
        draw.text(
            (width // 2, y), line, fill=(148, 163, 184), font=_font(26), anchor="mm"
        )
        y += 40
    img.save(out_path)
