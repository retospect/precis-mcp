"""Source-level checks on the se 3D viewer's static files — the fixes of
gr461146 (draw order of the atomic surface vs. atoms vs. target),
gr462703 (the load bar has an error state and clears on the first painted
frame), gr458084 (the revision scrubber no longer submits a form) and
gr485857 (no element colour may sit on the canvas background).

The pixel-level proof is ``scripts/viewer_check.py`` (nightly, real
Chromium); these pin the code shape that proof relies on, so a refactor
that drops a ``renderOrder`` or puts ``this.form.submit()`` back fails
here without a browser.
"""

from __future__ import annotations

import pathlib
import re

_STATIC = pathlib.Path(__file__).resolve().parents[2] / "src/precis_web/static"
_TEMPLATES = _STATIC.parent / "templates"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


# ── gr461146 ──────────────────────────────────────────────────────────────


def test_surface_material_does_not_write_depth_while_a_ghost() -> None:
    core = _read(_STATIC / "molecule-core.js")
    surf = re.search(r"const surfMesh = new THREE\.Mesh\((.*?)\);", core, re.S)
    assert surf is not None
    assert "depthWrite: false" in surf.group(1)
    # ...and writes depth once it is the dominant layer, at which point the
    # atoms and bonds (the ghost from then on) stop writing it.
    assert "const surfaceIsBody = t >= _SURFACE_BODY_T;" in core
    assert "surfMesh.material.depthWrite = surfaceIsBody;" in core
    assert (
        "atomMesh.material.depthWrite = bondMesh.material.depthWrite = !surfaceIsBody;"
        in core
    )
    assert re.search(r"const _SURFACE_BODY_T = 0\.5;", core)


def test_overlay_groups_carry_an_explicit_render_order() -> None:
    core = _read(_STATIC / "molecule-core.js")
    assert "group.renderOrder = _ORDER_ATOMIC;" in core
    assert "targetGroup.renderOrder = _ORDER_TARGET;" in core
    assert "surfMesh.renderOrder = _ORDER_SURFACE;" in core
    assert "atomMesh.renderOrder = _ORDER_ATOMS;" in core
    assert "bondMesh.renderOrder = _ORDER_ATOMS;" in core
    orders = {
        m.group(1): int(m.group(2))
        for m in re.finditer(r"const (_ORDER_\w+) = (\d+);", core)
    }
    # Surface before atoms within the overlay; overlay before the target;
    # both above the vendored CAD tree's 0 and below the pick markers' 1000.
    assert orders["_ORDER_SURFACE"] < orders["_ORDER_ATOMS"]
    assert 0 < orders["_ORDER_ATOMIC"] < orders["_ORDER_TARGET"] < 1000


# ── gr462703 ──────────────────────────────────────────────────────────────


def test_progress_bar_has_an_error_phase() -> None:
    js = _read(_STATIC / "blocktree-3d.js")
    css = _read(_STATIC / "blocktree-3d-overrides.css")
    assert re.search(r"error\(text\) \{\s*set\(\"error\", text\);", js)
    # The atom payload's non-timeout failure reaches it (not a bare hide).
    assert "prog.error(" in js
    assert '#bt3d-progress[data-phase="error"] #bt3d-progress-fill' in css


def test_progress_bar_clears_on_the_first_painted_frame() -> None:
    js = _read(_STATIC / "blocktree-3d.js")
    raf = re.search(
        r"requestAnimationFrame\(\(\) => \{(.*?)\}\);",
        js[js.index("atomicBuiltMarked = true;") :],
        re.S,
    )
    assert raf is not None
    assert "prog.done()" in raf.group(1)


# ── gr458084 ──────────────────────────────────────────────────────────────


def test_scrubber_rides_the_live_scene_seam() -> None:
    js = _read(_STATIC / "blocktree-3d.js")
    scrub = re.search(r"async function scrubTo\(rev\) \{(.*?)\n  \}\n", js, re.S)
    assert scrub is not None
    body = scrub.group(1)
    assert "await loadScene();" in body  # camera held, UI state re-applied
    assert "revisionEl.innerHTML = html;" in body
    assert "applyRevisionState();" in body
    # The URL is rewritten, never navigated: `rev` joins replaceState.
    sync = re.search(r"function syncUrl\(\) \{(.*?)\n  \}\n", js, re.S)
    assert sync is not None
    assert 'set("rev", shownRev === null ? null : String(shownRev));' in sync.group(1)
    assert "replaceState" in sync.group(1) and "pushState" not in sync.group(1)
    # The atom payload follows the revision too.
    assert "_setupAtomicOverlay(viewer, revUrl(atomicUrl)" in js
    # No template on the page submits the scrubber.
    for tpl in (_TEMPLATES / "blocktree").glob("*.j2"):
        assert "this.form.submit()" not in _read(tpl), tpl.name


# ── gr485857 ──────────────────────────────────────────────────────────────


def _srgb_luminance(css_hex: str) -> float:
    """WCAG relative luminance of a ``#rrggbb`` colour."""

    def channel(byte: str) -> float:
        c = int(byte, 16) / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * channel(css_hex[1:3])
        + 0.7152 * channel(css_hex[3:5])
        + 0.0722 * channel(css_hex[5:7])
    )


def test_no_element_colour_sits_on_a_canvas_background() -> None:
    """Hydrogen rendered pure white vanished on the se page's white canvas
    (gr485857). The CPK table is shared by every host, so each entry — and
    the unknown-element default — must stand off every canvas the viewer
    renders on: the se page's white/light background and the standalone
    host's dark clear colour (molecule-host.js)."""
    core = _read(_STATIC / "molecule-core.js")
    table = re.search(r"const _ATOMIC_CPK = \{(.*?)\};", core, re.S)
    assert table is not None
    entries = dict(re.findall(r'(\w+):\s*"(#[0-9a-fA-F]{6})"', table.group(1)))
    assert entries, "no element colours parsed from _ATOMIC_CPK"
    default = re.search(r'const _ATOMIC_CPK_DEFAULT = "(#[0-9a-fA-F]{6})";', core)
    assert default is not None
    entries["<default>"] = default.group(1)
    backgrounds = {
        "se page (white/light)": "#ffffff",
        "standalone host": "#101820",
    }
    floor = 0.05
    for element, colour in entries.items():
        for name, bg in backgrounds.items():
            distance = abs(_srgb_luminance(colour) - _srgb_luminance(bg))
            assert distance >= floor, (
                f"{element} {colour} is {distance:.3f} luminance from the "
                f"{name} canvas {bg} (< {floor})"
            )
