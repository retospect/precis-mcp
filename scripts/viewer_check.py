"""Browser-level check of the se 3D viewer: drive a real Chromium against a
local precis-web and assert, by canvas pixel-diff, that each control
CHANGES THE PICTURE. Run nightly by ``.github/workflows/viewer-check.yml``;
the requirements it encodes are in
``docs/backlog/se-viewer-browser-level-check.md``.

    viewer_check.py seed <ops.json> <slug>       # needs PRECIS_DATABASE_URL
    viewer_check.py probe <base-url> <slug> <out-dir>

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
  that merely looks similar.

Writes ``report.json`` plus every shot to ``<out-dir>``; exits 1 when any
check fails, so the workflow uploads the directory as the evidence.
"""

from __future__ import annotations

import json
import os
import pathlib
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
#: "Back to the base picture" — measured n=1 on the hand harness.
RESTORED_MAX = 200
#: Ceiling for any one wait on an observable condition.
WAIT_S = 60.0


@dataclass
class Check:
    name: str
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)


def seed(ops_path: str, slug: str) -> None:
    from psycopg_pool import ConnectionPool

    from precis.dispatch import boot
    from precis.store import Store
    from precis_se.handler import SeHandler

    payload = json.loads(pathlib.Path(ops_path).read_text(encoding="utf-8"))
    dsn = os.environ["PRECIS_DATABASE_URL"]
    pool = ConnectionPool(dsn, min_size=1, max_size=4, open=True)
    hub = boot(store=Store(pool, dsn=dsn))
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


def probe(base_url: str, slug: str, out_dir: str) -> int:
    import numpy as np
    from PIL import Image
    from playwright.sync_api import sync_playwright

    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    checks: list[Check] = []
    console: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=[
                "--use-gl=angle",
                "--use-angle=swiftshader",
                "--enable-unsafe-swiftshader",
            ]
        )
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on(
            "console",
            lambda m: (
                console.append(f"{m.type}: {m.text}") if m.type == "error" else None
            ),
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

        checks.append(Check("console_clean", not console, {"errors": console[:20]}))
        browser.close()

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


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[1] == "seed":
        seed(argv[2], argv[3])
        return 0
    if len(argv) == 5 and argv[1] == "probe":
        return probe(argv[2], argv[3], argv[4])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
