"""Tests for the ``scripts/guide-*`` user-guide capture/annotation/narration
pipeline (docs/backlog/user-guide-demo.md, slices 2-3) — the pure,
docker/network/DB-free parts, factored into ``scripts/guide_lib.py`` (plus
``guide/build.py`` and ``scripts/guide-narrate --check``, run as
subprocesses):

* manifest loading/validation + ``<nn>-<slug>.json`` discovery,
* ``{id}`` route-placeholder resolution + ``--id slug=value`` parsing,
* manifest + capture-sidecar merging (the input to annotation),
* the animated-SVG builder (well-formed XML, one ``<animate>`` per step,
  no external references),
* the Pillow callout-drawing helper on a synthetic image (skipped if the
  ``guide`` extra/Pillow isn't installed in this environment),
* narration-file discovery + guide-section assembly (title from tour
  manifest or manual chapter),
* ``guide/build.py`` against the real repo tree (no captures/audio needed —
  the whole point is it degrades gracefully),
* ``scripts/guide-narrate --check`` (pure segmentation, no synth backend).

Nothing here touches docker, a real browser, a DB, or an actual TTS
backend — those live in ``scripts/guide_capture_inner.py`` / the outer
orchestrators / the container-first ``render_episode`` path, exercised
manually per the spec's smoke path.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import guide_lib as gl

try:
    import precis.draft.narrate  # noqa: F401

    HAVE_NARRATE = True
except ImportError:  # pragma: no cover — depends on install state
    HAVE_NARRATE = False

try:
    from PIL import Image

    HAVE_PIL = True
except ImportError:  # pragma: no cover — depends on install state
    HAVE_PIL = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, name: str, **overrides: object) -> Path:
    data: dict[str, object] = {
        "title": "Drive",
        "route": "/drive",
        "steps": [
            {
                "anchor": "drive-search",
                "heading": "Search everything",
                "text": "One box searches papers, patents, slides.",
                "placement": "bottom",
            },
            {
                "anchor": "drive-new",
                "heading": "+ New",
                "text": "Create a CAD part, a figure, or a draft.",
                "placement": "left",
            },
        ],
    }
    data.update(overrides)
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _sidecar_for(manifest: dict[str, Any]) -> dict[str, Any]:
    steps: list[dict[str, Any]] = manifest["steps"]
    return {
        "slug": "drive",
        "title": manifest["title"],
        "route": "/drive",
        "url": "http://example.test/drive",
        "viewport": {"width": 1600, "height": 1000},
        "steps": [
            {
                "index": i + 1,
                "anchor": step["anchor"],
                "png": f"step-{i + 1}.png",
                "rect": {"x": 10.0 + i * 5, "y": 20.0, "width": 100.0, "height": 40.0},
            }
            for i, step in enumerate(steps)
        ],
    }


# ---------------------------------------------------------------------------
# Manifest loading + discovery
# ---------------------------------------------------------------------------


def test_load_manifest_valid(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, "01-drive.json")
    manifest = gl.load_manifest(path)
    assert manifest["title"] == "Drive"
    assert len(manifest["steps"]) == 2


def test_load_manifest_missing_key(tmp_path: Path) -> None:
    path = tmp_path / "01-drive.json"
    path.write_text('{"title": "Drive", "steps": []}', encoding="utf-8")
    with pytest.raises(gl.GuideError, match="missing required key 'route'"):
        gl.load_manifest(path)


def test_load_manifest_bad_placement(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        "01-drive.json",
        steps=[
            {
                "anchor": "a",
                "heading": "h",
                "text": "t",
                "placement": "middle",
            }
        ],
    )
    with pytest.raises(gl.GuideError, match="placement"):
        gl.load_manifest(path)


def test_load_manifest_not_json(tmp_path: Path) -> None:
    path = tmp_path / "01-drive.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(gl.GuideError, match="not valid JSON"):
        gl.load_manifest(path)


def test_slug_from_manifest_path() -> None:
    assert (
        gl.slug_from_manifest_path(Path("02-writing-a-paper.json")) == "writing-a-paper"
    )


def test_slug_from_manifest_path_rejects_bad_name() -> None:
    with pytest.raises(gl.GuideError):
        gl.slug_from_manifest_path(Path("readme.json"))


def test_discover_manifests_sorted_and_filtered(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "02-writing-a-paper.json", title="Writing")
    _write_manifest(tmp_path, "01-drive.json", title="Drive")
    (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")

    found = gl.discover_manifests(tmp_path)
    assert [p.name for p in found] == ["01-drive.json", "02-writing-a-paper.json"]

    only = gl.discover_manifests(tmp_path, only="writing-a-paper")
    assert [p.name for p in only] == ["02-writing-a-paper.json"]


def test_discover_manifests_only_unknown_slug_errors(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "01-drive.json")
    with pytest.raises(gl.GuideError, match="no manifest with that slug"):
        gl.discover_manifests(tmp_path, only="nope")


# ---------------------------------------------------------------------------
# --id parsing + {id} route resolution
# ---------------------------------------------------------------------------


def test_parse_id_flags() -> None:
    ids = gl.parse_id_flags(["writing-a-paper=dr123", "reading-papers=pp456"])
    assert ids == {"writing-a-paper": "dr123", "reading-papers": "pp456"}


@pytest.mark.parametrize("bad", ["no-equals-sign", "=novalue", "noslug="])
def test_parse_id_flags_rejects_malformed(bad: str) -> None:
    with pytest.raises(gl.GuideError, match="expected 'slug=value'"):
        gl.parse_id_flags([bad])


def test_resolve_route_no_placeholder_passes_through() -> None:
    assert gl.resolve_route("/drive", "drive", {}) == "/drive"


def test_resolve_route_fills_id() -> None:
    route = gl.resolve_route(
        "/smartdraft/{id}", "writing-a-paper", {"writing-a-paper": "dr123"}
    )
    assert route == "/smartdraft/dr123"


def test_resolve_route_missing_id_is_hard_error() -> None:
    with pytest.raises(gl.GuideError, match="writing-a-paper.*needs \\{id\\}"):
        gl.resolve_route("/smartdraft/{id}", "writing-a-paper", {})


def test_resolve_route_uses_only_the_matching_slug() -> None:
    with pytest.raises(gl.GuideError):
        gl.resolve_route("/papers/{id}", "reading-papers", {"writing-a-paper": "dr123"})


# ---------------------------------------------------------------------------
# Manifest + sidecar merge
# ---------------------------------------------------------------------------


def test_merge_manifest_and_sidecar(tmp_path: Path) -> None:
    manifest = gl.load_manifest(_write_manifest(tmp_path, "01-drive.json"))
    sidecar = _sidecar_for(manifest)

    merged = gl.merge_manifest_and_sidecar(manifest, sidecar)

    assert [m.anchor for m in merged] == ["drive-search", "drive-new"]
    assert merged[0].heading == "Search everything"
    assert merged[0].png == "step-1.png"
    assert merged[0].rect == {"x": 10.0, "y": 20.0, "width": 100.0, "height": 40.0}
    assert merged[1].index == 2


def test_merge_manifest_and_sidecar_missing_anchor_errors(tmp_path: Path) -> None:
    manifest = gl.load_manifest(_write_manifest(tmp_path, "01-drive.json"))
    sidecar = _sidecar_for(manifest)
    sidecar["steps"] = sidecar["steps"][:1]  # drop the second step's capture

    with pytest.raises(gl.GuideError, match="drive-new"):
        gl.merge_manifest_and_sidecar(manifest, sidecar)


# ---------------------------------------------------------------------------
# SVG builder
# ---------------------------------------------------------------------------


def _merged_steps(tmp_path: Path) -> tuple[dict[str, Any], list[gl.MergedStep]]:
    manifest = gl.load_manifest(_write_manifest(tmp_path, "01-drive.json"))
    sidecar = _sidecar_for(manifest)
    return manifest, gl.merge_manifest_and_sidecar(manifest, sidecar)


def test_build_tour_svg_is_well_formed_xml(tmp_path: Path) -> None:
    manifest, merged = _merged_steps(tmp_path)
    svg = gl.build_tour_svg(
        slug="drive",
        title=manifest["title"],
        png_bytes=b"\x89PNG\r\n fake-but-nonempty",
        viewport={"width": 1600, "height": 1000},
        steps=merged,
    )
    root = ET.fromstring(svg)  # raises if malformed
    assert root.tag.endswith("svg")


def test_build_tour_svg_one_animate_per_step(tmp_path: Path) -> None:
    manifest, merged = _merged_steps(tmp_path)
    svg = gl.build_tour_svg(
        slug="drive",
        title=manifest["title"],
        png_bytes=b"fake-png-bytes",
        viewport={"width": 1600, "height": 1000},
        steps=merged,
    )
    root = ET.fromstring(svg)
    animates = root.findall(".//{http://www.w3.org/2000/svg}animate")
    assert len(animates) == len(merged) == 2


def test_build_tour_svg_no_external_refs(tmp_path: Path) -> None:
    manifest, merged = _merged_steps(tmp_path)
    svg = gl.build_tour_svg(
        slug="drive",
        title=manifest["title"],
        png_bytes=b"fake-png-bytes",
        viewport={"width": 1600, "height": 1000},
        steps=merged,
    )
    assert "<script" not in svg
    # The xmlns declaration is a namespace URI, not a fetched resource — the
    # only `href`/`src` in the document must be the embedded data: URI.
    assert 'href="http' not in svg and 'src="http' not in svg
    assert 'href="data:image/png;base64,' in svg


def test_build_tour_svg_embeds_given_png_bytes(tmp_path: Path) -> None:
    import base64

    manifest, merged = _merged_steps(tmp_path)
    png_bytes = b"totally-a-png"
    svg = gl.build_tour_svg(
        slug="drive",
        title=manifest["title"],
        png_bytes=png_bytes,
        viewport={"width": 1600, "height": 1000},
        steps=merged,
    )
    assert base64.b64encode(png_bytes).decode("ascii") in svg


def test_build_tour_svg_rejects_zero_steps() -> None:
    with pytest.raises(gl.GuideError, match="zero steps"):
        gl.build_tour_svg(
            slug="drive",
            title="Drive",
            png_bytes=b"x",
            viewport={"width": 1600, "height": 1000},
            steps=[],
        )


# ---------------------------------------------------------------------------
# PNG annotation (Pillow) — skipped if the `guide` extra isn't installed
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAVE_PIL, reason="Pillow ('guide' extra) not installed")
def test_annotate_png_draws_without_mutating_input() -> None:
    image = Image.new("RGB", (400, 300), color=(255, 255, 255))
    original_bytes = image.tobytes()

    out = gl.annotate_png(
        image,
        rect={"x": 50.0, "y": 60.0, "width": 100.0, "height": 40.0},
        heading="Search everything",
        text="One box searches papers, patents, slides, cached answers.",
        placement="bottom",
    )

    assert out.size == image.size
    assert image.tobytes() == original_bytes  # input untouched
    assert out.tobytes() != original_bytes  # output actually drew something


@pytest.mark.skipif(not HAVE_PIL, reason="Pillow ('guide' extra) not installed")
@pytest.mark.parametrize("placement", ["top", "bottom", "left", "right"])
def test_annotate_png_all_placements(placement: str) -> None:
    image = Image.new("RGB", (400, 300), color=(255, 255, 255))
    out = gl.annotate_png(
        image,
        rect={"x": 150.0, "y": 120.0, "width": 60.0, "height": 30.0},
        heading="H",
        text="body text",
        placement=placement,
    )
    assert out.size == (400, 300)


def test_annotate_png_without_pillow_raises_guide_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate the extra being absent: the error is a GuideError naming the
    extra to install, not a raw ImportError leaking out of guide_lib."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "PIL":
            raise ImportError("no module named PIL")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(gl.GuideError, match="guide"):
        gl.annotate_png(
            object(),
            {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            "h",
            "t",
            "bottom",
        )


# ---------------------------------------------------------------------------
# Repo layout helpers
# ---------------------------------------------------------------------------


def test_tour_dir_and_assets_dir_layout(tmp_path: Path) -> None:
    assert gl.tour_dir(tmp_path) == tmp_path / "src" / "precis_web" / "manual" / "tour"
    assert gl.assets_dir(tmp_path) == tmp_path / "guide" / "assets"


def test_repo_root_points_at_real_worktree() -> None:
    root = gl.repo_root()
    assert (root / "src" / "precis_web" / "manual" / "tour").is_dir()
    assert (root / "scripts" / "guide_lib.py").is_file()


# ---------------------------------------------------------------------------
# Narration discovery + guide-section assembly (slice 3)
# ---------------------------------------------------------------------------


def test_slug_from_narration_path() -> None:
    assert gl.slug_from_narration_path(Path("01-drive.md")) == "drive"


def test_slug_from_narration_path_rejects_bad_name() -> None:
    with pytest.raises(gl.GuideError):
        gl.slug_from_narration_path(Path("README.md"))


def test_discover_narration_sorted_and_filtered(tmp_path: Path) -> None:
    (tmp_path / "02-writing-a-paper.md").write_text("# Writing", encoding="utf-8")
    (tmp_path / "01-drive.md").write_text("# Drive", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")

    found = gl.discover_narration(tmp_path)
    assert [p.name for p in found] == ["01-drive.md", "02-writing-a-paper.md"]

    only = gl.discover_narration(tmp_path, only="drive")
    assert [p.name for p in only] == ["01-drive.md"]


def test_discover_narration_only_unknown_slug_errors(tmp_path: Path) -> None:
    (tmp_path / "01-drive.md").write_text("# Drive", encoding="utf-8")
    with pytest.raises(gl.GuideError, match="no narration file with that slug"):
        gl.discover_narration(tmp_path, only="nope")


def test_chapter_title_reads_first_h1(tmp_path: Path) -> None:
    path = tmp_path / "00-what-is-precis.md"
    path.write_text("\nsome preamble\n\n# What is precis\n\nbody\n", encoding="utf-8")
    assert gl.chapter_title(path) == "What is precis"


def test_chapter_title_missing_heading_errors(tmp_path: Path) -> None:
    path = tmp_path / "00-x.md"
    path.write_text("no heading here", encoding="utf-8")
    with pytest.raises(gl.GuideError, match="no '# '"):
        gl.chapter_title(path)


def test_discover_guide_sections_against_real_tree() -> None:
    """The real worktree's nine narration files (00 manual chapter + 01-08
    tour manifests) — every section titled, in filename order, section 00's
    title coming from the manual chapter rather than a tour manifest."""
    sections = gl.discover_guide_sections()
    assert [s.slug for s in sections] == [
        "what-is-precis",
        "drive",
        "writing-a-paper",
        "reading-papers",
        "claims-and-nanopubs",
        "figures-and-diagrams",
        "structures-and-cad",
        "the-loop",
        "attention",
    ]
    assert sections[0].index == "00"
    assert sections[0].manifest_path is None
    assert sections[0].title  # from the manual chapter's '# ' heading
    assert sections[1].manifest_path is not None
    assert sections[1].title == "Drive"


def test_discover_guide_sections_orphan_narration_errors(tmp_path: Path) -> None:
    root = tmp_path
    (root / "guide" / "narration").mkdir(parents=True)
    (root / "src" / "precis_web" / "manual" / "tour").mkdir(parents=True)
    (root / "guide" / "narration" / "09-nowhere.md").write_text(
        "# stub", encoding="utf-8"
    )
    with pytest.raises(gl.GuideError, match="no tour manifest"):
        gl.discover_guide_sections(root)


# ---------------------------------------------------------------------------
# guide/build.py — well-formed, self-contained HTML from the real tree,
# whatever asset state it is in (fresh checkout or with captures/audio).
# ---------------------------------------------------------------------------


def test_build_produces_all_section_anchors_and_nav(tmp_path: Path) -> None:
    out = tmp_path / "index.html"
    result = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "guide" / "build.py"),
            "--out",
            str(out),
            "--md-out",
            str(tmp_path / "README.md"),
            "--transcript-out",
            str(tmp_path / "transcript.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert out.is_file()
    doc = out.read_text(encoding="utf-8")

    import html as _html

    sections = gl.discover_guide_sections()
    for section in sections:
        assert f'id="{section.slug}"' in doc
        assert f'href="#{section.slug}"' in doc
        assert _html.escape(section.title) in doc

    # No external CDN references — self-contained per spec.
    assert "cdn." not in doc
    assert "unpkg.com" not in doc
    # Per section: a real asset reference or the pending placeholder —
    # never a broken <img>/<audio>, whatever state the tree's assets are in.
    # A manifest-less section (00) gets no asset slot at all.
    for section in sections:
        svg_ref = f"assets/{section.slug}/tour.svg"
        if section.manifest_path is None:
            assert svg_ref not in doc
        else:
            has_svg = (_REPO_ROOT / "guide" / svg_ref).is_file()
            assert (svg_ref in doc) == has_svg
            if not has_svg:
                assert "captures pending" in doc
        mp3_ref = f"assets/audio/{section.slug}.mp3"
        has_mp3 = (_REPO_ROOT / "guide" / mp3_ref).is_file()
        assert (mp3_ref in doc) == has_mp3
        if not has_mp3:
            assert "narration pending" in doc


def test_build_is_idempotent(tmp_path: Path) -> None:
    out1 = tmp_path / "one.html"
    out2 = tmp_path / "two.html"
    for i, out in enumerate((out1, out2)):
        result = subprocess.run(
            [
                sys.executable,
                str(_REPO_ROOT / "guide" / "build.py"),
                "--out",
                str(out),
                "--md-out",
                str(tmp_path / f"README-{i}.md"),
                "--transcript-out",
                str(tmp_path / "transcript.json"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    assert out1.read_text(encoding="utf-8") == out2.read_text(encoding="utf-8")


def test_build_well_formed_html(tmp_path: Path) -> None:
    from html.parser import HTMLParser

    out = tmp_path / "index.html"
    subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "guide" / "build.py"),
            "--out",
            str(out),
            "--md-out",
            str(tmp_path / "README.md"),
            "--transcript-out",
            str(tmp_path / "transcript.json"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    doc = out.read_text(encoding="utf-8")

    class _Strict(HTMLParser):
        def error(self, message: str) -> None:  # pragma: no cover - defensive
            raise AssertionError(message)

    _Strict(convert_charrefs=True).feed(doc)  # raises on malformed markup


def test_build_html_uses_assets_when_present(tmp_path: Path) -> None:
    """When a section's tour.svg / audio mp3 DO exist, build.py links them
    instead of rendering the placeholder — exercised against a synthetic
    root so it doesn't depend on real captures existing."""
    root = tmp_path
    tour_dir = root / "src" / "precis_web" / "manual" / "tour"
    manual_dir = root / "src" / "precis_web" / "manual"
    narration_dir = root / "guide" / "narration"
    assets_dir = root / "guide" / "assets"
    for d in (tour_dir, manual_dir, narration_dir, assets_dir):
        d.mkdir(parents=True, exist_ok=True)

    (manual_dir / "00-what-is-precis.md").write_text(
        "# What is precis\n\nThis is precis. It helps.\n", encoding="utf-8"
    )
    (narration_dir / "00-what-is-precis.md").write_text(
        "# What is precis\n\nThis is precis. It helps.\n", encoding="utf-8"
    )
    (tour_dir / "01-drive.json").write_text(
        json.dumps(
            {
                "title": "Drive",
                "route": "/drive",
                "steps": [
                    {
                        "anchor": "a",
                        "heading": "h",
                        "text": "t",
                        "placement": "bottom",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (narration_dir / "01-drive.md").write_text(
        "# Drive\n\nSearch everything.\n", encoding="utf-8"
    )
    (assets_dir / "drive").mkdir()
    (assets_dir / "drive" / "tour.svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8"
    )
    (assets_dir / "audio").mkdir()
    (assets_dir / "audio" / "drive.mp3").write_bytes(b"fake-mp3-bytes")

    sys.path.insert(0, str(_REPO_ROOT / "guide"))
    import importlib

    build = importlib.import_module("build")
    sections = gl.discover_guide_sections(root)
    doc = build.build_html(sections, assets_dir)

    assert 'src="assets/drive/tour.svg"' in doc
    assert 'src="assets/audio/drive.mp3"' in doc
    # drive's own section got real assets, not the placeholder — the 00
    # section (no assets given here) still falls back, so this checks the
    # drive <section> slice specifically rather than the whole document.
    drive_section = doc[doc.index('id="drive"') :]
    assert "captures pending" not in drive_section
    assert "narration pending" not in drive_section


# ---------------------------------------------------------------------------
# guide/build.py — guide/README.md (GitHub-flavored markdown), the in-repo
# counterpart to index.html. Same fresh-tree/synthetic-tree pattern as above.
# ---------------------------------------------------------------------------


def test_build_markdown_produces_all_sections_and_nav(tmp_path: Path) -> None:
    out = tmp_path / "README.md"
    result = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "guide" / "build.py"),
            "--md-out",
            str(out),
            "--transcript-out",
            str(tmp_path / "transcript.json"),
            "--out",
            str(tmp_path / "index.html"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert out.is_file()
    doc = out.read_text(encoding="utf-8")

    assert doc.startswith("<!-- Generated by guide/build.py")

    sections = gl.discover_guide_sections()
    for section in sections:
        assert f'<a id="{section.slug}"></a>' in doc
        assert f"(#{section.slug})" in doc
        assert section.title in doc

    # Per section: a real SVG reference or the markdown placeholder — never
    # a broken image, whatever state the tree's assets are in. A
    # manifest-less section (00) gets no asset line at all.
    for section in sections:
        svg_ref = f"assets/{section.slug}/tour.svg"
        if section.manifest_path is None:
            assert svg_ref not in doc
        else:
            has_svg = (_REPO_ROOT / "guide" / svg_ref).is_file()
            assert (svg_ref in doc) == has_svg
            if not has_svg:
                assert "*captures pending*" in doc
    # No audio in markdown — GFM can't embed it (the Pages/index.html
    # counterpart is where the narrated version lives).
    assert "<audio" not in doc


def test_build_markdown_nav_hrefs_match_emitted_anchors(tmp_path: Path) -> None:
    out = tmp_path / "README.md"
    subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "guide" / "build.py"),
            "--md-out",
            str(out),
            "--transcript-out",
            str(tmp_path / "transcript.json"),
            "--out",
            str(tmp_path / "index.html"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    doc = out.read_text(encoding="utf-8")

    nav_hrefs = set(re.findall(r"\]\(#([a-z0-9-]+)\)", doc))
    anchor_ids = set(re.findall(r'<a id="([a-z0-9-]+)"></a>', doc))
    assert nav_hrefs, "expected at least one nav link"
    assert nav_hrefs == anchor_ids


def test_build_markdown_is_idempotent(tmp_path: Path) -> None:
    out1 = tmp_path / "one.md"
    out2 = tmp_path / "two.md"
    for i, out in enumerate((out1, out2)):
        result = subprocess.run(
            [
                sys.executable,
                str(_REPO_ROOT / "guide" / "build.py"),
                "--md-out",
                str(out),
                "--transcript-out",
                str(tmp_path / "transcript.json"),
                "--out",
                str(tmp_path / f"index-{i}.html"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    assert out1.read_text(encoding="utf-8") == out2.read_text(encoding="utf-8")


def test_build_markdown_uses_svg_when_present(tmp_path: Path) -> None:
    """When a section's tour.svg DOES exist, build_markdown links it inline
    instead of the '*captures pending*' placeholder — exercised against a
    synthetic root so it doesn't depend on real captures existing."""
    root = tmp_path
    tour_dir = root / "src" / "precis_web" / "manual" / "tour"
    manual_dir = root / "src" / "precis_web" / "manual"
    narration_dir = root / "guide" / "narration"
    assets_dir = root / "guide" / "assets"
    for d in (tour_dir, manual_dir, narration_dir, assets_dir):
        d.mkdir(parents=True, exist_ok=True)

    (manual_dir / "00-what-is-precis.md").write_text(
        "# What is precis\n\nThis is precis. It helps.\n", encoding="utf-8"
    )
    (narration_dir / "00-what-is-precis.md").write_text(
        "# What is precis\n\nThis is precis. It helps.\n", encoding="utf-8"
    )
    (tour_dir / "01-drive.json").write_text(
        json.dumps(
            {
                "title": "Drive",
                "route": "/drive",
                "steps": [
                    {
                        "anchor": "a",
                        "heading": "h",
                        "text": "t",
                        "placement": "bottom",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (narration_dir / "01-drive.md").write_text(
        "# Drive\n\nSearch everything.\n", encoding="utf-8"
    )
    (assets_dir / "drive").mkdir()
    (assets_dir / "drive" / "tour.svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8"
    )

    sys.path.insert(0, str(_REPO_ROOT / "guide"))
    import importlib

    build = importlib.import_module("build")
    sections = gl.discover_guide_sections(root)
    doc = build.build_markdown(sections, assets_dir)

    assert "![Drive — animated tour](assets/drive/tour.svg)" in doc
    # drive's own section got the real image, not the placeholder — the 00
    # section (no assets given here) still falls back, so this checks the
    # drive slice specifically rather than the whole document.
    drive_section = doc[doc.index('<a id="drive">') :]
    assert "*captures pending*" not in drive_section


def test_guide_readme_md_matches_generated(tmp_path: Path) -> None:
    """Drift tripwire: the committed guide/README.md must be exactly what
    guide/build.py generates right now — a stale commit here is worse than
    no commit, since GitHub renders it as the guide."""
    out = tmp_path / "README.md"
    result = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "guide" / "build.py"),
            "--md-out",
            str(out),
            "--transcript-out",
            str(tmp_path / "transcript.json"),
            "--out",
            str(tmp_path / "index.html"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    generated = out.read_text(encoding="utf-8")
    committed = (_REPO_ROOT / "guide" / "README.md").read_text(encoding="utf-8")
    assert generated == committed, (
        "guide/README.md is stale — re-run guide/build.py and commit guide/README.md"
    )
    generated_tx = (tmp_path / "transcript.json").read_text(encoding="utf-8")
    committed_tx = (_REPO_ROOT / "guide" / "transcript.json").read_text(
        encoding="utf-8"
    )
    assert generated_tx == committed_tx, (
        "guide/transcript.json is stale — re-run guide/build.py and commit it"
    )


# ---------------------------------------------------------------------------
# scripts/guide-narrate --check — pure segmentation, no synth backend needed.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not HAVE_NARRATE, reason="precis.draft.narrate not importable in this env"
)
def test_guide_narrate_check_segments_all_nine() -> None:
    result = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / "guide-narrate"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "9 section(s) segmented, 0 synthesized" in result.stdout
    for slug in (
        "00-what-is-precis",
        "01-drive",
        "02-writing-a-paper",
        "03-reading-papers",
        "04-claims-and-nanopubs",
        "05-figures-and-diagrams",
        "06-structures-and-cad",
        "07-the-loop",
        "08-attention",
    ):
        assert slug in result.stdout


@pytest.mark.skipif(
    not HAVE_NARRATE, reason="precis.draft.narrate not importable in this env"
)
def test_guide_narrate_check_only_filters_to_one_section() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "scripts" / "guide-narrate"),
            "--check",
            "--only",
            "drive",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "1 section(s) segmented" in result.stdout
    assert "01-drive" in result.stdout
    assert "02-writing-a-paper" not in result.stdout


def test_guide_narrate_rejects_unknown_voice() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "scripts" / "guide-narrate"),
            "--check",
            "--voice",
            "not-a-real-voice",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "unknown voice" in result.stderr


# ---------------------------------------------------------------------------
# .github/workflows/pages.yml — parses, references guide/build.py, minimal
# permissions.
# ---------------------------------------------------------------------------


def test_pages_workflow_parses_and_builds_guide() -> None:
    yaml = pytest.importorskip("yaml")
    path = _REPO_ROOT / ".github" / "workflows" / "pages.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert "build" in doc["jobs"]
    assert "deploy" in doc["jobs"]
    steps = doc["jobs"]["build"]["steps"]
    assert any("guide/build.py" in (step.get("run") or "") for step in steps)

    # Top-level permissions are read-only; the deploy job elevates only what
    # actions/deploy-pages needs (pages: write, id-token: write) — never at
    # the workflow root.
    assert doc["permissions"] == {"contents": "read"}
    assert doc["jobs"]["deploy"]["permissions"] == {
        "pages": "write",
        "id-token": "write",
    }
