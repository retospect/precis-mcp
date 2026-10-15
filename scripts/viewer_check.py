"""Browser-level check of the se 3D viewer: drive a real Chromium against a
local precis-web and assert, by canvas pixel-diff, that each control
CHANGES THE PICTURE. Run nightly by ``.github/workflows/viewer-check.yml``
over two committed fixtures: ``probe`` on the unicycle (blocks, selection,
explode, level, tool volumes, export, scroll) and ``strain`` on a small
smooth_drum (target surface, strain layers, pentagon θp).

    viewer_check.py seed <ops.json> <slug>       # needs PRECIS_DATABASE_URL
    viewer_check.py probe <base-url> <slug> <out-dir>
    viewer_check.py strain <base-url> <slug> <out-dir>   # an atomic design:
        # target surface + strain layers (nightly: small-drum.ops.json)
    viewer_check.py nowebgl <base-url> <slug> <out-dir>   # WebGL off: the
        # fallback message + inline 2D view appear (gr462702)

Why it exists: explode, selection highlighting and the level control were
each dead from the commit that introduced them, behind a green suite. A
clean console and a flipped label are not evidence; the canvas is.

How it measures (each rule paid for by a wrong reading on the hand harness):

- every wait is on an observable condition — the canvas holding still, the
  busy mark coming and going — never a fixed settle window;
- the noise floor is measured first, in the same run;
- the vendored transport bar (bottom edge) and toolbar (top edge) are
  cropped off before diffing, since they appear on their own;
- a held state (explode) is sampled as a series, not one frame;
- "back to normal" is asserted against a shot of the same state reached
  directly (the wheel selected from a clean slate), never against a shot
  that merely looks similar;
- "X still holds after a round trip" needs a shot between the two halves
  that DID change, or a canvas that rendered neither click passes;
- a numeric readout (the scale bar) is checked against a witness that does
  not share its arithmetic, e.g. the model's pixel extent vs a known size;
- the tree row set and the canvas fail independently: small parts near the
  edge of visible can leave a real swap at a few hundred pixels.

Not covered yet: atom click-pick (``probe``'s pick checks are
block-level only); atom hover is checked by ``strain``.

Writes ``report.json`` plus every shot to ``<out-dir>``; exits 1 when any
check fails, so the workflow uploads the directory as the evidence.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

#: Per-channel difference that counts a pixel as changed (out of 255).
THRESHOLD = 8
#: Rows cropped off the canvas before any diff: vendored toolbar / transport.
TOP_CROP = 40
BOTTOM_CROP = 60
#: Two shots with nothing in between must differ by at most this many pixels.
NOISE_MAX = 50
#: A control "changed the picture" when at least this many pixels moved.
CHANGED_MIN = 1000
#: The level control's change is smaller: on the unicycle fixture,
#: ``interfaces`` adds only the interface features (measured n=643).
LEVEL_CHANGED_MIN = 300
#: The target-surface overlay is small against the design's envelope-framed
#: camera: a 12 A cylinder around C60 inside a 5 nm sphere measured n=448
#: (local run, headless Chrome), so it gets its own, lower floor.
TARGET_CHANGED_MIN = 300
#: A strain layer recolours a few percent of the atoms or bonds, not whole
#: blocks; set from the first measured run on se:hexa-smooth-drum-v2.
STRAIN_CHANGED_MIN = 300
#: The tool-volumes overlay draws translucent bolt/driver cylinders around
#: a centimetre-scale fastener inside a metre-scale design — most of the
#: swept volume is hidden inside the blocks it collides with, so the
#: visible change is small (measured n=20 on se:unicycle-c1, headless
#: Chrome). The canvas floor alone is too thin to carry the check; the
#: check also requires the vendored tree to grow by the group's rows.
TOOLVOL_CHANGED_MIN = 10
#: "Back to the base picture" — measured n=1 on the hand harness.
RESTORED_MAX = 200
#: Ceiling for any one wait on an observable condition.
WAIT_S = 60.0
#: The PNG export vs a shot of the canvas: only the vendored filter
#: dropdown (page chrome, left out of the export) may differ.
EXPORT_DIFF_MAX = 0.01


@dataclass
class Check:
    name: str
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)


def _seed_bound_components(hub: Any, ops: list[dict[str, Any]]) -> list[str]:
    """Mint the standards-series components a fixture's ``set_binding``
    ops name, and return the minted slugs.

    ``set_binding`` carries only the slug — ``persist.attach_catalog``
    derives a bound block's facets at LOAD from ``component`` refs in the
    same DB — so an ops replay into a store without them renders the
    scene fine but leaves ``fasten`` seeing no screw-form fastener: every
    insertion-path check silently vacates and the tool-volumes endpoint
    answers empty (measured on the seeded unicycle-c1: 0 findings where
    prod shows 2). A slug no packaged series covers (``wheel-20in-
    assembly``, ``saddle-bicycle``) stays a dangling binding, which is a
    legal state and matches what a store that lacks it would do."""
    from precis import component_series
    from precis.handlers.component import ComponentHandler

    series = component_series.load_series()
    slugs = {
        str(o["design"])
        for o in ops
        if o.get("op") == "set_binding" and o.get("kind") == "component"
    }
    minted: list[str] = []
    handler = ComponentHandler(hub=hub)
    for slug in sorted(slugs):
        hit = next((s for s in series if slug.startswith(s.series_id + "-")), None)
        if hit is None:
            continue
        size = slug[len(hit.series_id) + 1 :].upper()
        resp = handler.put(id=slug, series=hit.series_id, size=size)
        if "skipped" in resp.body:
            raise RuntimeError(f"component mint incomplete for {slug}: {resp.body}")
        minted.append(slug)
    return minted


def seed(ops_path: str, slug: str) -> None:
    from psycopg_pool import ConnectionPool

    from precis.dispatch import boot
    from precis.store import Store
    from precis_se.handler import SeHandler

    payload = json.loads(pathlib.Path(ops_path).read_text(encoding="utf-8"))
    dsn = os.environ["PRECIS_DATABASE_URL"]
    with ConnectionPool(dsn, min_size=1, max_size=4, open=True) as pool:
        hub = boot(store=Store(pool, dsn=dsn))
        minted = _seed_bound_components(hub, payload["ops"])
        if minted:
            print(f"minted component(s): {', '.join(minted)}")
        print(f"seeding {len(payload['ops'])} op(s) -> se:{slug}")
        print(SeHandler(hub=hub).put(id=slug, text=json.dumps(payload)))


def _diff(a: Any, b: Any) -> dict[str, Any]:
    import numpy as np

    if a.shape != b.shape:
        return {"n": -1, "bbox": None, "why": f"shape {a.shape} vs {b.shape}"}
    changed = (np.abs(a - b) > THRESHOLD).any(axis=2)
    n = int(changed.sum())
    if not n:
        return {"n": 0, "bbox": None}
    ys, xs = np.nonzero(changed)
    return {
        "n": n,
        "bbox": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())],
    }


def _open(p: Any, out: pathlib.Path, console: list[str]) -> tuple[Any, ...]:
    """A Chromium page on software WebGL, collecting console errors into
    ``console``, plus its canvas ``shot`` (cropped) and ``settle``."""
    import numpy as np
    from PIL import Image

    browser = p.chromium.launch(
        args=[
            "--use-gl=angle",
            "--use-angle=swiftshader",
            "--enable-unsafe-swiftshader",
        ]
    )
    page = browser.new_page(
        viewport={"width": 1600, "height": 1000}, accept_downloads=True
    )
    page.on(
        "console",
        lambda m: console.append(f"{m.type}: {m.text}") if m.type == "error" else None,
    )
    page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))
    canvas = page.locator("#bt3d-viewer canvas").first

    def shot(name: str) -> Any:
        path = out / f"{name}.png"
        canvas.screenshot(path=str(path))
        a = np.asarray(Image.open(path).convert("RGB"), dtype=np.int16)
        return a[TOP_CROP : max(TOP_CROP + 1, a.shape[0] - BOTTOM_CROP)]

    def settle(name: str) -> Any:
        """Shoot until two consecutive frames agree — the canvas has
        stopped changing — and return the last one."""
        prev = shot(name)
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            page.wait_for_timeout(500)
            cur = shot(name)
            if _diff(prev, cur)["n"] <= NOISE_MAX:
                return cur
            prev = cur
        raise TimeoutError(f"canvas never settled for {name!r}")

    return browser, page, canvas, shot, settle


def _report(base_url: str, slug: str, out: pathlib.Path, checks: list[Check]) -> int:
    report = {
        "url": f"{base_url}/se/{slug}",
        "passed": all(c.passed for c in checks),
        "checks": [asdict(c) for c in checks],
    }
    (out / "report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    for c in checks:
        print(f"{'PASS' if c.passed else 'FAIL'}  {c.name}  {json.dumps(c.detail)}")
    return 0 if report["passed"] else 1


def probe(base_url: str, slug: str, out_dir: str) -> int:
    import numpy as np
    from PIL import Image
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    checks: list[Check] = []
    console: list[str] = []

    with sync_playwright() as p:
        browser, page, canvas, shot, settle = _open(p, out, console)

        def click_node(name: str) -> bool:
            return bool(
                page.evaluate(
                    """(name) => {
                      const host = document.getElementById('bt3d-topology');
                      const g = [...host.querySelectorAll('g')].find(g => {
                        const t = g.querySelector(':scope > text');
                        return t && t.textContent.trim() === name;
                      });
                      if (!g) return false;
                      g.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                      return true;
                    }""",
                    name,
                )
            )

        page.goto(f"{base_url}/se/{slug}", wait_until="networkidle", timeout=90000)
        page.wait_for_selector("#bt3d-viewer canvas", timeout=60000)
        base = settle("00_base")

        # Noise floor: nothing happens between these two shots.
        page.wait_for_timeout(600)
        floor = _diff(base, shot("01_noise"))
        checks.append(Check("noise_floor", floor["n"] <= NOISE_MAX, floor))

        # Selection tints the block itself and its partners; selecting
        # another block restores the first one's partners. The selection
        # is tinted too, so "restored" is measured against the same block
        # selected from a clean slate, not against the untouched base.
        found = click_node("wheel")
        wheel = settle("02_select_wheel")
        first = _diff(base, wheel)
        checks.append(
            Check(
                "select_tints_block",
                found and first["n"] >= CHANGED_MIN,
                {**first, "node_found": found},
            )
        )
        found = click_node("axle")
        sel = _diff(wheel, settle("03_select_axle"))
        checks.append(
            Check(
                "select_tints_partners",
                found and sel["n"] >= CHANGED_MIN,
                {**sel, "node_found": found},
            )
        )
        found = click_node("wheel")
        again = settle("04_select_wheel_again")
        back = _diff(wheel, again)
        checks.append(
            Check(
                "reselect_restores_colours",
                found and back["n"] <= RESTORED_MAX,
                {**back, "node_found": found},
            )
        )

        # Explode moves the parts and they STAY moved.
        page.click("#bt3d-explode")
        settle("05_explode")
        series = []
        for i in range(3):
            page.wait_for_timeout(1000)
            series.append(_diff(again, shot(f"06_explode_held_{i}"))["n"])
        checks.append(
            Check(
                "explode_moves_and_holds",
                min(series) >= CHANGED_MIN,
                {"series_n": series},
            )
        )
        page.click("#bt3d-explode")
        unexploded_shot = settle("07_unexplode")
        unexploded = _diff(again, unexploded_shot)
        checks.append(
            Check("unexplode_restores", unexploded["n"] <= RESTORED_MAX, unexploded)
        )

        # Tool volumes (se-mechanical-drc rulings 5/6): the toggle fetches
        # the swept fastener/driver/hand volumes of every blocked
        # insertion-path finding and appends them to the live scene. Wait
        # on the status line, not a timeout — the fetch runs `fasten`
        # server-side (seconds) while the canvas holds still, so a shot
        # taken early diffs the pre-toggle frame against itself. Three
        # witnesses: the status names the finding count, the vendored tree
        # grows by the group's rows, and the canvas moves by the small
        # visible sliver of the overlaid cylinders.
        status_js = (
            "() => (document.querySelector('#bt3d-validate [role=status]')"
            "?.textContent || '')"
        )
        rows_js = "() => document.querySelectorAll('.tv-tree-node').length"
        rows0 = page.evaluate(rows_js)
        page.click("#bt3d-tool-volumes")
        try:
            page.wait_for_function(
                f"() => ({status_js})().startsWith('tool volumes:')",
                timeout=int(WAIT_S * 1000),
            )
            drew = True
        except PlaywrightTimeout:
            drew = False
        status = page.evaluate(status_js)
        if drew:
            rows1 = page.evaluate(rows_js)
            vols_on = _diff(unexploded_shot, settle("07a_tool_volumes"))
            checks.append(
                Check(
                    "tool_volumes_draw",
                    vols_on["n"] >= TOOLVOL_CHANGED_MIN and rows1 > rows0,
                    {
                        **vols_on,
                        "status": status,
                        "rows_before": rows0,
                        "rows_after": rows1,
                    },
                )
            )
        else:
            checks.append(
                Check(
                    "tool_volumes_draw",
                    False,
                    {"status": status, "error": "status line never appeared"},
                )
            )
        if page.is_checked("#bt3d-tool-volumes"):
            page.click("#bt3d-tool-volumes")
        vols_off = _diff(unexploded_shot, settle("07b_tool_volumes_off"))
        rows2 = page.evaluate(rows_js)
        checks.append(
            Check(
                "tool_volumes_off_restores",
                vols_off["n"] <= RESTORED_MAX and rows2 == rows0,
                {**vols_off, "rows_before": rows0, "rows_after": rows2},
            )
        )

        # The pick panel lists the selected block's levels (outside the
        # canvas, so it cannot move a pixel above).
        rows = page.locator("#bt3d-pick-rows tr")
        rows.first.wait_for(timeout=int(WAIT_S * 1000))
        tokens = rows.locator("td:nth-child(3)").all_text_contents()
        checks.append(
            Check(
                "pick_panel_lists_levels",
                bool(tokens) and all(t.startswith("<se:") for t in tokens),
                {"tokens": tokens},
            )
        )

        # Level change: the busy mark shows while the scene refetches, then
        # the picture changes. Watch the mark from BEFORE the change so a
        # fast refetch cannot slip between two polls.
        page.evaluate(
            """() => {
              window.__busySeen = false;
              const el = document.getElementById('bt3d-busy');
              new MutationObserver(() => {
                if (!el.hidden) window.__busySeen = true;
              }).observe(el, {attributes: true});
            }"""
        )
        page.select_option("#bt3d-level", "interfaces")
        page.wait_for_function(
            "() => window.__busySeen && document.getElementById('bt3d-busy').hidden",
            timeout=int(WAIT_S * 1000),
        )
        # Against the shot just before the change: the swap re-renders the
        # scene and re-tints the selection, so the only difference left is
        # the level's own geometry.
        level = _diff(unexploded_shot, settle("08_level_interfaces"))
        checks.append(
            Check("level_change_redraws", level["n"] >= LEVEL_CHANGED_MIN, level)
        )

        # Per-block level chip, ambient now `interfaces`. Two witnesses,
        # because they fail independently (module docstring): the TREE for
        # a collapsed block opened through its I chip — on the unicycle a
        # crank, whose one ~5 mm bolt moves no canvas pixel (measured n=0)
        # — and the CANVAS for the root collapsed through its E chip. Each
        # active chip is then clicked again, which drops the override.
        scene = page.evaluate(
            "(u) => fetch(u).then(r => r.json())",
            f"{base_url}/se/{slug}/scene3d.json?level=interfaces",
        )
        boxed = next(
            (n for n in scene["nodes"] if n["kind"] == "box" and n["level_rungs"][1]),
            None,
        )
        root = next(
            (
                n
                for n in scene["nodes"]
                if n["level_rungs"][1]
                and n["level_active"] != "envelope"
                and not any(m["id"] == n["parent"] for m in scene["nodes"])
            ),
            None,
        )
        row_count = "() => document.querySelectorAll('.tv-tree-node').length"

        def click_chip(block: str, rung: str) -> None:
            page.evaluate("() => { window.__busySeen = false; }")
            page.locator(
                f'.bt3d-chip[data-block="{block}"][data-rung="{rung}"]'
            ).click()
            page.wait_for_function(
                "() => window.__busySeen"
                " && document.getElementById('bt3d-busy').hidden",
                timeout=int(WAIT_S * 1000),
            )

        if boxed is None or root is None:
            checks.append(
                Check(
                    "chip_opens_block",
                    False,
                    {"error": "no collapsed I-lettered block or open root"},
                )
            )
        else:
            rows_before = page.evaluate(row_count)
            click_chip(boxed["name"], "interfaces")
            rows_after = page.evaluate(row_count)
            checks.append(
                Check(
                    "chip_opens_block",
                    rows_after > rows_before,
                    {
                        "block": boxed["name"],
                        "rows_before": rows_before,
                        "rows_after": rows_after,
                    },
                )
            )
            click_chip(boxed["name"], "interfaces")
            rows_cleared = page.evaluate(row_count)
            checks.append(
                Check(
                    "chip_clear_restores_rows",
                    rows_cleared == rows_before,
                    {"rows_before": rows_before, "rows_cleared": rows_cleared},
                )
            )
            open_shot = settle("08b_root_open")
            click_chip(root["name"], "envelope")
            collapsed = _diff(open_shot, settle("08c_root_envelope"))
            checks.append(
                Check(
                    "chip_collapse_redraws",
                    collapsed["n"] >= CHANGED_MIN,
                    {**collapsed, "block": root["name"]},
                )
            )
            click_chip(root["name"], "envelope")
            reopened = _diff(open_shot, settle("08d_root_reopened"))
            checks.append(
                Check(
                    "chip_clear_restores_canvas",
                    reopened["n"] <= RESTORED_MAX,
                    reopened,
                )
            )

        # View export: the PNG is the canvas as on screen, scale bar
        # included (the vendored filter dropdown is page chrome, not in
        # it — hence a small tolerance, not zero); the SVG's scale-bar
        # label is the one on screen.
        screen_label = page.evaluate(
            "() => document.querySelector('.bt3d-scale-bar-label')?.textContent || ''"
        )
        screen = np.asarray(
            Image.open(io.BytesIO(canvas.screenshot())).convert("RGB")
        ).astype(int)
        with page.expect_download() as png_dl:
            page.click("#bt3d-export-png")
        png = np.asarray(Image.open(png_dl.value.path()).convert("RGB")).astype(int)
        frac = (
            float((np.abs(png - screen).max(axis=2) > THRESHOLD).mean())
            if png.shape == screen.shape
            else 1.0
        )
        with page.expect_download() as svg_dl:
            page.click("#bt3d-export-svg")
        svg = pathlib.Path(svg_dl.value.path()).read_text(encoding="utf-8")
        checks.append(
            Check(
                "export_matches_screen",
                bool(screen_label)
                and frac < EXPORT_DIFF_MAX
                and f">{screen_label}</text>" in svg
                and "data:image/png;base64," in svg,
                {
                    "png_shape": list(png.shape),
                    "screen_shape": list(screen.shape),
                    "png_diff_frac": round(frac, 4),
                    "label": screen_label,
                },
            )
        )

        # The page scrolls to what sits below the shell: a wheel off the
        # canvas (on it, the wheel zooms) must bring the design chat into
        # view. The vendored CSS's global body{overflow:hidden} once froze
        # this with no scrollbar.
        vh = page.viewport_size["height"] if page.viewport_size else 1000
        page.mouse.move(4, vh - 4)
        for _ in range(10):
            page.mouse.wheel(0, 400)
        try:
            page.wait_for_function("() => window.scrollY > 0", timeout=5000)
        except PlaywrightTimeout:
            pass  # reported as FAIL below, with scrollY 0 in the detail
        scroll = page.evaluate(
            """() => {
              const chat = document.getElementById('design-chat');
              return {scrollY: Math.round(scrollY), innerHeight,
                      chatTop: chat ? Math.round(chat.getBoundingClientRect().top) : null};
            }"""
        )
        checks.append(
            Check(
                "page_scrolls_to_chat",
                scroll["chatTop"] is not None
                and scroll["scrollY"] > 0
                and scroll["chatTop"] < scroll["innerHeight"],
                scroll,
            )
        )

        checks.append(Check("console_clean", not console, {"errors": console[:20]}))
        browser.close()

    return _report(base_url, slug, out, checks)


def _set_range(page: Any, selector: str, value: int) -> None:
    page.evaluate(
        """([sel, v]) => {
          const el = document.querySelector(sel);
          el.value = String(v);
          el.dispatchEvent(new Event('input', {bubbles: true}));
        }""",
        [selector, value],
    )


def _row_counts(page: Any, row_id: str) -> dict[str, int]:
    return dict(
        page.evaluate(
            """(id) => {
              const d = document.getElementById(id).dataset;
              return {coloured: Number(d.coloured), total: Number(d.total)};
            }""",
            row_id,
        )
    )


_PROGRESS_OBSERVER_JS = """
window.__bt3dPhases = [];
window.__bt3dBuildLabels = [];
new MutationObserver((records) => {
  for (const r of records) {
    const bar = document.getElementById("bt3d-progress");
    if (r.type === "attributes" && r.target.id === "bt3d-progress") {
      window.__bt3dPhases.push(r.oldValue);
    } else if (bar && bar.dataset.phase === "build" && bar.contains(r.target)) {
      window.__bt3dBuildLabels.push(
        document.getElementById("bt3d-progress-label").textContent
      );
    }
  }
}).observe(document, {
  subtree: true, childList: true, characterData: true,
  attributes: true, attributeFilter: ["data-phase"], attributeOldValue: true,
});
"""


def strain_probe(base_url: str, slug: str, out_dir: str) -> int:
    """The strain layers (Reto, 2026-10-02) on an atomic
    design: each layer's checkbox and threshold slider change the picture,
    the default threshold colours the top 5%, and — from the payload, not
    the page — every pentagon atom clears the default θp threshold."""
    import numpy as np
    from playwright.sync_api import sync_playwright

    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    checks: list[Check] = []
    console: list[str] = []

    with sync_playwright() as p:
        browser, page, _canvas, shot, settle = _open(p, out, console)
        # Installed before navigation so the observer sees the bar from its
        # first phase. data-phase's old values give the sequence even when
        # two changes land in one task; the build label is logged per
        # change (one per ~1000-atom slice).
        page.add_init_script(_PROGRESS_OBSERVER_JS)
        page.goto(f"{base_url}/se/{slug}", wait_until="networkidle", timeout=180000)
        page.wait_for_selector("#bt3d-viewer canvas", timeout=int(WAIT_S * 1000))
        # The overlay is built after the scene; its first paint stamps the
        # deviation row's counts.
        page.wait_for_function(
            "() => document.getElementById('bt3d-smooth-legend')?.dataset.total",
            timeout=int(WAIT_S * 3 * 1000),
        )
        page.wait_for_function(
            "() => document.getElementById('bt3d-progress')?.hidden",
            timeout=int(WAIT_S * 1000),
        )
        seen = page.evaluate(
            "() => [...window.__bt3dPhases, "
            "document.getElementById('bt3d-progress').dataset.phase]"
        )
        phases = [x for i, x in enumerate(seen) if i == 0 or x != seen[i - 1]]
        want = ["server", "download", "build", "done"]
        it = iter(phases)
        in_order = all(w in it for w in want)  # subsequence test
        checks.append(
            Check(
                "progress_phases_in_order",
                in_order and phases[-1] == "done",
                {"phases": phases, "want": want},
            )
        )
        build_labels = page.evaluate("() => [...new Set(window.__bt3dBuildLabels)]")
        checks.append(
            Check(
                "progress_build_repaints",
                len(build_labels) >= 2,
                {
                    "distinct_build_labels": len(build_labels),
                    "labels": build_labels[:5],
                },
            )
        )
        base = settle("s00_base")
        page.wait_for_timeout(600)
        floor = _diff(base, shot("s01_noise"))
        checks.append(Check("noise_floor", floor["n"] <= NOISE_MAX, floor))

        # Atom hover (atoms on, slider at its default atoms end): sweep a
        # 9x9 grid over the central 60% of the canvas; after each move wait
        # for two animation frames (the hover raycast runs in one rAF), then
        # read the tooltip. Passes on the first point that names an atom.
        box = page.locator("#bt3d-viewer canvas").bounding_box()
        found: dict[str, Any] = {}
        if box:
            for gy in range(9):
                for gx in range(9):
                    x = box["x"] + box["width"] * (0.2 + 0.6 * gx / 8)
                    y = box["y"] + box["height"] * (0.2 + 0.6 * gy / 8)
                    page.mouse.move(x, y)
                    page.evaluate(
                        "() => new Promise(r => requestAnimationFrame("
                        "() => requestAnimationFrame(r)))"
                    )
                    text = page.evaluate(
                        "() => { const t = document.querySelector('.bt3d-atom-tip');"
                        " return t && !t.hidden ? t.textContent : null; }"
                    )
                    # The hover table's first row: "atom" | "<name> (<el>)".
                    if text and re.match(r"^atom\s*\S+ \([A-Z][a-z]?\)", text):
                        found = {"text": text, "point": [round(x), round(y)]}
                        break
                if found:
                    break
            page.mouse.move(0, 0)
        checks.append(Check("atom_hover_readout", bool(found), found))

        # Target surface (smooth_drum's revolved surface_meridian): the
        # checkbox appears only when an atomic block carries a target, and
        # the nightly fixture is a smooth_drum, so its absence is a failure.
        shown = page.locator("#bt3d-target").is_visible()
        checks.append(Check("target_checkbox_shown", shown, {}))
        if shown:
            page.check("#bt3d-target")
            on = _diff(base, settle("s_target_on"))
            checks.append(
                Check("target_surface_on_redraws", on["n"] >= TARGET_CHANGED_MIN, on)
            )
            page.uncheck("#bt3d-target")
            off = _diff(base, settle("s_target_off"))
            checks.append(
                Check("target_surface_off_restores", off["n"] <= RESTORED_MAX, off)
            )

        for layer in ("bond", "angle"):
            row = f"bt3d-layer-{layer}"
            if not page.locator(f"#{row}").is_visible():
                checks.append(Check(f"{layer}_row_shown", False, {}))
                continue
            page.check(f"#bt3d-{layer}-strain")
            on_shot = settle(f"s_{layer}_on")
            on = _diff(base, on_shot)
            checks.append(
                Check(f"{layer}_on_redraws", on["n"] >= STRAIN_CHANGED_MIN, on)
            )
            counts = _row_counts(page, row)
            frac = counts["coloured"] / counts["total"] if counts["total"] else 0.0
            checks.append(
                Check(
                    f"{layer}_default_colours_top_5pct",
                    0.04 <= frac <= 0.06,
                    {**counts, "frac": round(frac, 4)},
                )
            )
            slider = f"#bt3d-{layer}-threshold"
            pos = int(page.eval_on_selector(slider, "el => Number(el.value)"))
            _set_range(page, slider, pos // 2)
            moved = _diff(on_shot, settle(f"s_{layer}_threshold_down"))
            checks.append(
                Check(
                    f"{layer}_threshold_redraws",
                    moved["n"] >= STRAIN_CHANGED_MIN,
                    {**moved, "slider": [pos, pos // 2]},
                )
            )
            if layer == "angle":
                before = settle("s_angle_before_switch")
                page.select_option("#bt3d-angle-measure", "120")
                switched = _diff(before, settle("s_angle_120"))
                checks.append(
                    Check(
                        "angle_measure_switch_redraws",
                        switched["n"] >= STRAIN_CHANGED_MIN,
                        {**switched, "counts": _row_counts(page, row)},
                    )
                )
                page.select_option("#bt3d-angle-measure", "thetap")
            page.uncheck(f"#bt3d-{layer}-strain")
            off = _diff(base, settle(f"s_{layer}_off"))
            checks.append(Check(f"{layer}_off_restores", off["n"] <= RESTORED_MAX, off))

        # Surface deviation: only drawn toward the smooth end of the slider.
        _set_range(page, "#bt3d-smooth", 100)
        smooth_shot = settle("s_smooth")
        pos = int(
            page.eval_on_selector("#bt3d-dev-threshold", "el => Number(el.value)")
        )
        _set_range(page, "#bt3d-dev-threshold", pos // 2)
        moved = _diff(smooth_shot, settle("s_dev_threshold_down"))
        checks.append(
            Check(
                "deviation_threshold_redraws",
                moved["n"] >= STRAIN_CHANGED_MIN,
                {**moved, "slider": [pos, pos // 2]},
            )
        )

        # Pentagons vs the default θp threshold, computed here from the
        # payload with numpy — not the page's own percentile code.
        data = page.evaluate(
            "(u) => fetch(u).then(r => r.json())", f"{base_url}/se/{slug}/atomic3d.json"
        )
        # The xyz download: fires, and its atom count (line 1) equals the
        # atoms atomic3d.json draws.
        if page.locator("#bt3d-export-xyz").is_visible():
            with page.expect_download() as dl:
                page.click("#bt3d-export-xyz")
            xyz_path = dl.value.path()
            first = pathlib.Path(xyz_path).read_text(encoding="utf-8").split("\n", 1)[0]
            drawn = sum(len(b["elements"]) for b in data["blocks"])
            checks.append(
                Check(
                    "xyz_export_atom_count",
                    first.strip().isdigit() and int(first) == drawn,
                    {"file": first.strip(), "atomic3d": drawn},
                )
            )
        thetap = np.array(
            [
                v
                for b in data["blocks"]
                for v in b.get("angle_strain_thetap", [])
                if v is not None
            ]
        )
        if thetap.size:
            thr = float(np.sort(thetap)[min(thetap.size - 1, int(0.95 * thetap.size))])
            pent = {
                (bi, a)
                for bi, b in enumerate(data["blocks"])
                for f in b.get("faces", [])
                if len(f) == 5
                for a in f
            }
            missed = [
                (bi, a)
                for bi, a in sorted(pent)
                if (data["blocks"][bi]["angle_strain_thetap"][a] or 0.0) < thr
            ]
            checks.append(
                Check(
                    "pentagon_atoms_clear_default_thetap",
                    bool(pent) and not missed,
                    {
                        "pentagon_atoms": len(pent),
                        "missed": len(missed),
                        "threshold_deg": round(thr, 2),
                    },
                )
            )

        checks.append(Check("console_clean", not console, {"errors": console[:20]}))
        browser.close()

    return _report(base_url, slug, out, checks)


#: (label, width, height): a phone, a small laptop, the desktop the other
#: checks use.
NOWEBGL_VIEWPORTS = (
    ("phone", 390, 844),
    ("laptop", 1280, 720),
    ("desktop", 1600, 1000),
)


def nowebgl_probe(base_url: str, slug: str, out_dir: str) -> int:
    """gr462702: with WebGL disabled the page shows ``#bt3d-fallback`` (a
    message naming WebGL 2 plus the design's 2D SVG inline), not a bare error."""
    from playwright.sync_api import sync_playwright

    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    checks: list[Check] = []
    errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-3d-apis"])
        # Which device fails is still open (review item se-3d-viewer-3),
        # so the fallback is checked at phone, laptop and desktop size.
        for label, width, height in NOWEBGL_VIEWPORTS:
            page = browser.new_page(viewport={"width": width, "height": height})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{base_url}/se/{slug}", wait_until="networkidle", timeout=180000)
            has_gl2 = page.evaluate(
                "() => document.createElement('canvas').getContext('webgl2') !== null"
            )
            checks.append(
                Check(f"{label}: webgl2 is unavailable in this browser", not has_gl2)
            )
            page.wait_for_selector("#bt3d-fallback", timeout=int(WAIT_S * 1000))
            text = page.inner_text("#bt3d-fallback")
            checks.append(
                Check(
                    f"{label}: message mentions WebGL 2",
                    "WebGL 2" in text,
                    {"text": text[:200]},
                )
            )
            page.wait_for_function(
                "() => { const i = document.querySelector('#bt3d-fallback img');"
                " return i && i.complete; }",
                timeout=int(WAIT_S * 1000),
            )
            box = page.evaluate(
                "() => { const r = (s) => document.querySelector(s)"
                ".getBoundingClientRect();"
                " const i = document.querySelector('#bt3d-fallback img');"
                " return {nat: i.naturalWidth, img: r('#bt3d-fallback img').right,"
                " msg: r('#bt3d-fallback-message').right,"
                " msgH: r('#bt3d-fallback-message').height}; }"
            )
            checks.append(Check(f"{label}: fallback <img> loaded", box["nat"] > 0, box))
            checks.append(
                Check(
                    f"{label}: message and 2D image fit the viewport width",
                    box["img"] <= width and box["msg"] <= width and box["msgH"] > 0,
                    {**box, "viewport": width},
                )
            )
            page.screenshot(path=str(out / f"nowebgl-{label}.png"))
            page.close()
        checks.append(Check("no uncaught pageerror", not errors, {"errors": errors}))
        browser.close()
    return _report(base_url, slug, out, checks)


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[1] == "seed":
        seed(argv[2], argv[3])
        return 0
    if len(argv) == 5 and argv[1] == "probe":
        return probe(argv[2], argv[3], argv[4])
    if len(argv) == 5 and argv[1] == "strain":
        return strain_probe(argv[2], argv[3], argv[4])
    if len(argv) == 5 and argv[1] == "nowebgl":
        return nowebgl_probe(argv[2], argv[3], argv[4])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
