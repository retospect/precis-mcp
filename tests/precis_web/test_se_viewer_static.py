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


# ── pcb-workbench-layout.md slice 3: se adopts the shared viewport ────


def test_se_2d_reader_uses_viewport_js() -> None:
    """Slice 3 exit: se 2D rides static/viewport.js — the svg loads as an
    <object> (the img could never be a viewBox camera), se-2d.js attaches
    the module, and the toolbar carries the data-vp buttons whose keys
    shortcuts.js dispatches."""
    tpl = _read(_TEMPLATES / "blocktree" / "detail.html.j2")
    js = _read(_STATIC / "se-2d.js")
    assert '<object id="se-2d-object"' in tpl
    assert 'data="{{ svg_url }}"' in tpl
    assert "/static/viewport.js" in tpl and "/static/se-2d.js" in tpl
    for name in ("in", "out", "fit", "reset"):
        assert f'data-vp="{name}"' in tpl
    assert "PrecisViewport.attach" in js and "active: true" in js


def test_se_3d_key_map_matches_the_gesture_table() -> None:
    """Slice 3 exit: the se 3D key map is the table's 2D column — f fit,
    0 reset, + zoom in, - zoom out — registered through data-shortcut so
    `?` lists them, and wired to the vendored viewer's reset/zoom."""
    tpl = _read(_TEMPLATES / "blocktree" / "detail3d.html.j2")
    js = _read(_STATIC / "blocktree-3d.js")
    for keys, bid in (
        ('data-shortcut="+"', "bt3d-vp-in"),
        ('data-shortcut="-"', "bt3d-vp-out"),
        ('data-shortcut="f"', "bt3d-vp-fit"),
        ('data-shortcut="0"', "bt3d-vp-reset"),
    ):
        assert keys in tpl and bid in tpl
    assert "viewButtons" in tpl
    assert "viewButtons" in js
    assert "viewer.reset()" in js
    assert "setCameraZoom(viewer.getCameraZoom() * 1.25)" in js


def test_se_3d_gesture_conventions_are_applied_per_render() -> None:
    """The rest of the table's 3D column: zoom toward the cursor,
    middle-drag and space+drag pan, double-click empty space fits."""
    js = _read(_STATIC / "blocktree-3d.js")
    assert "zoomToCursor" in js
    assert "mouseButtons.MIDDLE" in js
    # space held swaps the left button to pan; controls are rebuilt per
    # render, so the convention is re-applied there
    assert "_applyGestureConventionsPerRender" in js
    assert 'e.key !== " "' in js
    assert 'addEventListener("dblclick"' in js
    assert "pickAt" in js and "pickVisible" in js


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


# ── gr508636 ──────────────────────────────────────────────────────────────


def test_strain_threshold_is_a_box_plot() -> None:
    """Reto, on /se/cone-gate-18to8-cone-30: "for bond strain and angle
    strain, can the slider be a box plot with outliers? So we can see what
    the approx distribution are." All three rows' threshold control is an
    inline SVG slider-role element blocktree-3d.js draws the layer's Tukey
    box into — whiskers, q1..q3 box, median, outlier dots, marker."""
    tpl = _read(_TEMPLATES / "blocktree" / "detail3d.html.j2")
    for tid in ("bt3d-dev-threshold", "bt3d-bond-threshold", "bt3d-angle-threshold"):
        tag = re.search(rf'<svg[^>]*id="{tid}"[^>]*>', tpl)
        assert tag is not None, tid
        assert 'data-role="threshold"' in tag.group(0)
        assert 'class="bt3d-boxplot"' in tag.group(0)
        assert 'role="slider"' in tag.group(0)
    assert 'type="range" id="bt3d-dev-threshold"' not in tpl
    assert 'type="range" id="bt3d-bond-threshold"' not in tpl
    assert 'type="range" id="bt3d-angle-threshold"' not in tpl


def test_strain_stats_carry_the_box_plot_five() -> None:
    """The plot's data: molecule-core's _layerStats returns quartiles,
    Tukey whisker bounds and a capped outlier sample alongside the
    min/max/p95 the rest of the row still reads."""
    core = _read(_STATIC / "molecule-core.js")
    stats = re.search(r"function _layerStats\(values\) \{(.*?)\n\}", core, re.S)
    assert stats is not None
    body = stats.group(1)
    for key in ("min", "max", "p95", "q1", "q2", "q3", "lo", "hi", "outliers"):
        assert re.search(rf"\b{key}\b", body), key
    assert "1.5 * iqr" in body
    assert "_MAX_PLOTTED_OUTLIERS" in body


def test_box_plot_draws_and_keeps_the_range_input_contract() -> None:
    """_drawStrainBox renders the SVG; the element still carries a numeric
    .value moved by an 'input' event (scripts/viewer_check.py's
    _set_range), and drags/keys move the threshold for real users."""
    js = _read(_STATIC / "blocktree-3d.js")
    assert "function _drawStrainBox(svg, stats, frac)" in js
    for cls in ("bp-whisker", "bp-box", "bp-median", "bp-outlier", "bp-threshold"):
        assert f'class="{cls}"' in js, cls
    assert 'box.addEventListener("input"' in js
    assert 'box.addEventListener("pointerdown"' in js
    assert 'box.addEventListener("keydown"' in js
    css = _read(_STATIC / "blocktree-3d-overrides.css")
    for cls in ("bp-whisker", "bp-box", "bp-median", "bp-outlier", "bp-threshold"):
        assert f".bt3d-boxplot .{cls}" in css, cls


# ── gr508637 ──────────────────────────────────────────────────────────────


def test_night_mode_and_full_window_buttons_are_wired() -> None:
    """Reto, on /se/cone-gate-18to8-cone-30: "can the viewer bit (the
    white patch) do night mode ... and can it have a 'full window'
    button?" The canvas is a transparent-clear WebGL canvas over the
    vendored --tcv-bg-color, so the vendored setTheme flips the patch;
    fullscreen rides the Fullscreen API on the shell."""
    tpl = _read(_TEMPLATES / "blocktree" / "detail3d.html.j2")
    js = _read(_STATIC / "blocktree-3d.js")
    for bid in ("bt3d-night", "bt3d-full"):
        assert f'id="{bid}"' in tpl
    assert 'nightButton: document.getElementById("bt3d-night")' in tpl
    assert 'fullButton: document.getElementById("bt3d-full")' in tpl
    assert 'viewer.setTheme(on ? "dark" : "light")' in js
    assert "atomicOverlay.setNight" in js
    assert "viewerEl.requestFullscreen()" in js
    assert 'addEventListener("fullscreenchange"' in js


def test_night_palette_moves_h_and_c_as_asked() -> None:
    """ "atom colors need to change a bit, make H a bit grayer and C a bit
    whiter": _ATOMIC_CPK_NIGHT overrides exactly those two — H darker,
    C lighter than their day entries — and both stay off the vendored
    dark chrome's #444 backdrop. setNight swaps the live palette through
    the last strain state."""
    core = _read(_STATIC / "molecule-core.js")
    day_m = re.search(r"const _ATOMIC_CPK = \{(.*?)\};", core, re.S)
    night_m = re.search(r"const _ATOMIC_CPK_NIGHT = \{(.*?)\};", core, re.S)
    assert day_m is not None and night_m is not None
    day = dict(re.findall(r'(\w+):\s*"(#[0-9a-fA-F]{6})"', day_m.group(1)))
    night = dict(re.findall(r'(\w+):\s*"(#[0-9a-fA-F]{6})"', night_m.group(1)))
    assert set(night) == {"H", "C"}
    assert _srgb_luminance(night["H"]) < _srgb_luminance(day["H"])
    assert _srgb_luminance(night["C"]) > _srgb_luminance(day["C"])
    dark_bg = _srgb_luminance("#444444")
    for element, colour in night.items():
        assert abs(_srgb_luminance(colour) - dark_bg) >= 0.05, element
    assert "function setNight(on)" in core
    assert "applyStrain(lastStrain)" in core
    assert "setNight," in core  # exported on the overlay's API


def test_night_and_fullscreen_css_exist() -> None:
    """The shell's :fullscreen rule sizes it to the viewport (refitViewer
    re-budgets the Display inside it), and our own overlays' dark-on-white
    styling flips under the vendored shell's data-theme="dark"."""
    css = _read(_STATIC / "blocktree-3d-overrides.css")
    assert "#bt3d-viewer:fullscreen" in css
    assert '#bt3d-viewer[data-theme="dark"] .bt3d-scale-bar' in css
    assert '#bt3d-viewer[data-theme="dark"] .bt3d-scale-bar-label' in css


# ── gr515634 / gr515635 ─────────────────────────────────────────────────


def test_viewer_colour_scheme_follows_the_system() -> None:
    """gr515635: the vendored chrome already rides ``theme: "browser"``
    (its own prefers-color-scheme listener); the atom palette and the
    night button now start on the same media query and keep tracking it —
    a dark desktop no longer gets dark chrome under a day-palette render."""
    js = _read(_STATIC / "blocktree-3d.js")
    assert 'theme: "browser"' in js
    assert 'matchMedia("(prefers-color-scheme: dark)")' in js
    assert "let nightOn = !!(_schemeDark && _schemeDark.matches);" in js
    assert '_schemeDark.addEventListener("change", (e) => applyNight(e.matches))' in js
    # The button opens labelled for the inherited scheme, not always day.
    assert 'nightButton.textContent = nightOn ? "day" : "night";' in js


def test_eye_restore_reapplies_shown_states_too() -> None:
    """gr515634: solid leaves can now ship edges-off by default (emitted
    server-side, tested in test_blocktree_3d.py), so the restore seam
    re-applies every plain pair — a user's outline-back-on survives the
    next render instead of silently reverting to off."""
    state = _read(_STATIC / "se-view-state.js")
    assert "trackedStates()" in state
    assert "viewer.setStates(tracked)" in state
