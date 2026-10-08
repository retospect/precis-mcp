"""Rendered validation-selection proof using fixture-only HTTP and real WebGL.

First export a real route page/JSON with scripts/test test_validation_selection.py
--basetemp=/app/.scratch/validation-browser. Then run this with uv and ephemeral
playwright/Pillow extras, passing that basetemp and an evidence directory.
No DB, production endpoint, job submission or service configuration is touched.
"""

from __future__ import annotations

import importlib
import io
import json
import mimetypes
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


def probe(fixtures: Path, output: Path, browser_name: str = "chromium") -> None:
    from PIL import Image, ImageChops

    sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright

    fixture = next(fixtures.rglob("validation-fixture.json"))
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    html = fixture.with_suffix(".html").read_bytes()
    atomic_fixture = next(fixtures.rglob("atomic-validation-fixture.json"))
    atomic_payload = json.loads(atomic_fixture.read_text(encoding="utf-8"))
    atomic_html = atomic_fixture.with_suffix(".html").read_bytes()
    static = Path(__file__).resolve().parents[2] / "src/precis_web/static"
    output.mkdir(parents=True, exist_ok=True)
    requests: list[str] = []
    errors: list[str] = []
    hold = threading.Event()
    entered = threading.Event()
    delayed_subject: list[str | None] = [None]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append(self.command)
            parsed = urlparse(self.path)
            active = (
                atomic_payload
                if parsed.path.startswith("/se/selection_atomic")
                else payload
            )
            if parsed.path == "/se/validation_ports":
                data, mime = html, "text/html"
            elif parsed.path == "/se/selection_atomic":
                data, mime = atomic_html, "text/html"
            elif parsed.path in ("/molecule-fixture", "/fixture.js"):
                suffix = "html" if parsed.path == "/molecule-fixture" else "js"
                path = (
                    Path(__file__).parent / "fixtures" / f"molecule-renderer.{suffix}"
                )
                data = path.read_bytes()
                mime = "text/html" if suffix == "html" else "text/javascript"
            elif parsed.path.endswith("/scene3d.json"):
                data, mime = json.dumps(active["scene"]).encode(), "application/json"
            elif parsed.path.endswith("/atomic3d.json"):
                data, mime = json.dumps(active["atomic"]).encode(), "application/json"
            elif parsed.path.endswith("/validation-targets"):
                subject = parse_qs(parsed.query)["subject"][0]
                if subject == delayed_subject[0]:
                    entered.set()
                    hold.wait(15)
                data = json.dumps(active["targets"][subject]).encode()
                mime = "application/json"
            elif parsed.path.startswith("/static/"):
                path = static / parsed.path.removeprefix("/static/")
                if not path.is_file():
                    self.send_error(404)
                    return
                data = path.read_bytes()
                if path.name == "blocktree-3d.js":
                    # Expose the real viewer only in this probe transport, so
                    # camera/mesh witnesses can be checked against route data.
                    data = data.replace(
                        b"viewer = new Viewer(display, viewerOptions, notify);",
                        b"viewer = new Viewer(display, viewerOptions, notify); window.__validationViewer = viewer;",
                    )
                mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            else:
                data, mime = b"{}", "application/json"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            requests.append(self.command)
            self.send_error(405)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    report: dict[str, object] = {}

    def check(condition, message):
        assert condition, message
        report[message] = True

    try:
        with sync_playwright() as playwright:
            launch: dict[str, Any] = {"timeout": 15_000}
            if browser_name == "chromium":
                launch["args"] = [
                    "--use-gl=angle",
                    "--use-angle=swiftshader",
                    "--enable-unsafe-swiftshader",
                ]
            if executable := os.environ.get("PRECIS_VIEWER_BROWSER_EXECUTABLE"):
                launch["executable_path"] = executable
            browser = getattr(playwright, browser_name).launch(**launch)
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/se/validation_ports")
            page.wait_for_function(
                "window.__validationViewer && document.querySelector('#bt3d-viewer canvas')"
            )
            page.locator("#bt3d-validate summary").click()
            page.locator("#bt3d-isolate").select_option("ball12")
            canvas = page.locator("#bt3d-viewer canvas").first
            status = page.locator("#bt3d-validation-status")

            def shot(name):
                raw = canvas.screenshot(path=str(output / f"{name}.png"))
                return Image.open(io.BytesIO(raw)).convert("RGB")

            def settle(name):
                previous = shot(name)
                for _ in range(30):
                    page.wait_for_timeout(100)
                    current = shot(name)
                    if ImageChops.difference(previous, current).getbbox() is None:
                        return current
                    previous = current
                raise AssertionError("canvas did not settle")

            before = settle("before")
            noise = ImageChops.difference(before, settle("noise")).getbbox()
            check(noise is None, "stable canvas noise floor")
            button = page.locator('[data-validation-subject="cyl12open.q_out"]')
            button.focus()
            button.press("Enter")
            page.wait_for_function(
                "document.querySelector('[data-validation-subject=\"cyl12open.q_out\"]').getAttribute('aria-pressed') === 'true'"
            )
            after = settle("keyboard-q-out")
            diff = ImageChops.difference(before, after)
            changed = sum(1 for pixel in diff.getdata() if max(pixel) > 8)
            check(changed > 1000, "keyboard selection changes rendered picture")
            report["changed_pixels"] = changed
            check(
                page.locator("#bt3d-isolate").input_value() == "",
                "hidden subject revealed",
            )
            witness = page.evaluate("""() => {
                const v = window.__validationViewer;
                const group = v._rendered.scene.getObjectByName('bt3d-validation-marker');
                return {target: v.getCameraTarget(), points: group.children.map(m => m.position.toArray()),
                        colour: group.children[0].material.color.getHexString()};
            }""")
            expected = payload["targets"]["cyl12open.q_out"]["targets"][0]["point"]
            check(
                all(
                    abs(a - b) < 1e-7
                    for a, b in zip(witness["target"], expected, strict=True)
                ),
                "camera centred on actual port",
            )
            check(witness["points"] == [expected], "marker at actual transformed port")
            yellow = sum(
                1 for r, g, b in after.getdata() if r > 180 and g > 150 and b < 100
            )
            check(yellow > 50, "port marker visibly rendered")
            report["marker_pixels"] = yellow

            page.locator('[data-validation-subject="ball12r11.s_rim"]').locator(
                "xpath=ancestor::tr"
            ).locator("td").nth(2).click()
            page.wait_for_function(
                "document.querySelector('[data-validation-subject=\"ball12r11.s_rim\"]').getAttribute('aria-pressed') === 'true'"
            )
            check(
                page.locator('[aria-pressed="true"][data-validation-subject]').count()
                == 1,
                "replacement clears previous row",
            )
            check(
                page.evaluate(
                    "window.__validationViewer._rendered.scene.getObjectByName('bt3d-validation-marker').children.length"
                )
                == 1,
                "replacement leaves one marker",
            )
            page.locator("#bt3d-isolate").select_option("ball12")
            check(
                page.locator('[aria-pressed="true"][data-validation-subject]').count()
                == 0,
                "view change clears row",
            )
            check(
                page.evaluate(
                    "!window.__validationViewer._rendered.scene.getObjectByName('bt3d-validation-marker')"
                ),
                "view change clears marker",
            )

            delayed_subject[0] = "ball12.s_rim"
            page.locator('[data-validation-subject="ball12.s_rim"]').click()
            check(entered.wait(5), "delayed target request reached fixture server")
            page.locator('[data-validation-subject="cyl12open.s_rim"]').click()
            page.wait_for_function(
                "document.querySelector('[data-validation-subject=\"cyl12open.s_rim\"]').getAttribute('aria-pressed') === 'true'"
            )
            hold.set()
            page.wait_for_timeout(200)
            check(
                page.locator(
                    '[data-validation-subject="cyl12open.s_rim"]'
                ).get_attribute("aria-pressed")
                == "true",
                "late response cannot replace newer selection",
            )

            hold.clear()
            entered.clear()
            page.locator('[data-validation-subject="ball12.s_rim"]').click()
            check(entered.wait(5), "second delayed request reached server")
            page.locator("#bt3d-isolate").select_option("ball12r11")
            hold.set()
            page.wait_for_function(
                "document.querySelector('#bt3d-validation-status').textContent.includes('view changed')"
            )
            check(
                page.locator('[aria-pressed="true"][data-validation-subject]').count()
                == 0,
                "render-generation guard rejects late response",
            )

            delayed_subject[0] = None
            payload["scene"]["validation_identity"] = "different-shown-version"
            page.locator("#bt3d-level").select_option("envelope")
            page.wait_for_function("document.querySelector('#bt3d-busy').hidden")
            button.click()
            page.wait_for_function(
                "document.querySelector('#bt3d-validation-status').textContent.includes('displayed design changed')"
            )
            check(
                page.locator('[aria-pressed="true"][data-validation-subject]').count()
                == 0,
                "shown-version mismatch rejects selection",
            )
            check(
                page.locator("[data-validation-refresh]").is_visible(),
                "stale selection offers read-only refresh",
            )
            check(
                "0 error(s), 4 warning(s)"
                in page.locator("#bt3d-validate summary").inner_text(),
                "validation counts preserved",
            )
            page.goto(f"http://127.0.0.1:{server.server_port}/se/selection_atomic")
            page.wait_for_function(
                "window.__validationViewer && document.querySelector('#bt3d-viewer canvas')"
            )
            page.wait_for_function("""() => {
                const group = window.__validationViewer._rendered.scene
                    .getObjectByName('bt3d-atomic-overlay');
                return group && group.children.some(m => m.isInstancedMesh && m.count === 90);
            }""")

            def molecular_witness():
                return page.evaluate("""() => {
                    const rendered = window.__validationViewer._rendered;
                    const group = rendered.scene.getObjectByName('bt3d-atomic-overlay');
                    const atoms = group.children.find(m => m.isInstancedMesh && m.count === 60);
                    const bonds = group.children.find(m => m.isInstancedMesh && m.count === 90);
                    const surface = group.children.find(m => m.isMesh && !m.isInstancedMesh);
                    return {
                        atoms: atoms.count, bonds: bonds.count, visible: group.visible,
                        positions: Array.from({length: atoms.count}, (_, i) =>
                            Array.from(atoms.instanceMatrix.array.slice(i * 16 + 12, i * 16 + 15))),
                        atomVisible: atoms.visible, bondVisible: bonds.visible,
                        atomOpacity: atoms.material.opacity, bondOpacity: bonds.material.opacity,
                        surfaceVisible: surface.visible, surfaceOpacity: surface.material.opacity,
                        surfacePositions: Array.from(surface.geometry.attributes.position.array),
                        envelopes: Object.values(rendered.nestedGroup.groups)
                            .filter(g => g.front).map(g => g.front.material.visible),
                    };
                }""")

            molecular = molecular_witness()
            check(
                molecular["atoms"] == 60 and molecular["bonds"] == 90,
                "extracted core renders 60 atoms and 90 bonds",
            )
            for slider_value in (0, 50, 100):
                page.locator("#bt3d-smooth").evaluate(
                    "(el, value) => { el.value = value; el.dispatchEvent(new Event('input', {bubbles: true})); }",
                    slider_value,
                )
                page.wait_for_function(
                    """opacity => {
                        const group = window.__validationViewer._rendered.scene
                            .getObjectByName('bt3d-atomic-overlay');
                        return group.children.find(m => m.isInstancedMesh && m.count === 60)
                            .material.opacity === opacity;
                    }""",
                    arg=1 - slider_value / 100,
                )
                molecular = molecular_witness()
                t = slider_value / 100
                block = atomic_payload["atomic"]["blocks"][0]
                expected_positions = [
                    [a + (b - a) * t for a, b in zip(c, s, strict=True)]
                    for c, s in zip(block["coords"], block["smooth"], strict=True)
                ]
                check(
                    all(
                        abs(actual - expected) < 1e-4
                        for actual_row, expected_row in zip(
                            molecular["positions"], expected_positions, strict=True
                        )
                        for actual, expected in zip(
                            actual_row, expected_row, strict=True
                        )
                    )
                    and all(
                        abs(actual - expected) < 1e-4
                        for actual, expected in zip(
                            molecular["surfacePositions"],
                            [v for row in expected_positions for v in row],
                            strict=True,
                        )
                    ),
                    f"slider {slider_value} places atoms and surface at payload interpolation",
                )
                check(
                    molecular["atomOpacity"] == molecular["bondOpacity"] == 1 - t
                    and molecular["surfaceOpacity"] == t
                    and molecular["atomVisible"] == molecular["bondVisible"] == (t < 1)
                    and molecular["surfaceVisible"] == (t > 0),
                    f"slider {slider_value} preserves molecular visibility and opacity",
                )
            for on in (False, True):
                page.locator("#bt3d-atoms").set_checked(on)
                molecular = molecular_witness()
                check(
                    molecular["visible"] == on
                    and bool(molecular["envelopes"])
                    and all(visible == (not on) for visible in molecular["envelopes"]),
                    f"atoms {'on' if on else 'off'} restores matching envelope visibility",
                )
            page.locator("#bt3d-smooth").evaluate(
                "el => { el.value = 0; el.dispatchEvent(new Event('input', {bubbles: true})); }"
            )
            page.evaluate("""() => {
                const group = window.__validationViewer._rendered.scene
                    .getObjectByName('bt3d-atomic-overlay');
                const geometries = new Set(), materials = new Set();
                group.traverse(mesh => {
                    if (mesh.geometry) geometries.add(mesh.geometry);
                    if (mesh.material) materials.add(mesh.material);
                });
                window.__molecularDisposal = {group, geometries: geometries.size,
                    materials: materials.size, geometryEvents: 0, materialEvents: 0};
                for (const geometry of geometries) geometry.addEventListener('dispose', () =>
                    window.__molecularDisposal.geometryEvents++);
                for (const material of materials) material.addEventListener('dispose', () =>
                    window.__molecularDisposal.materialEvents++);
            }""")
            page.locator("#bt3d-isolate").select_option("hub")
            page.wait_for_function("""() => {
                const old = window.__molecularDisposal;
                const group = window.__validationViewer._rendered.scene
                    .getObjectByName('bt3d-atomic-overlay');
                return group && group !== old.group && group.children.length >= 3;
            }""")
            check(
                page.evaluate("""() => {
                    const old = window.__molecularDisposal;
                    let count = 0;
                    window.__validationViewer._rendered.scene.traverse(object => {
                        if (object.name === 'bt3d-atomic-overlay') count++;
                    });
                    return count === 1 && old.group.parent === null
                        && old.geometryEvents === old.geometries
                        && old.materialEvents === old.materials;
                }"""),
                "scene replacement disposes old molecular resources and leaves one group",
            )
            page.locator("#bt3d-validate summary").click()
            bound_button = page.locator('[data-validation-subject="hub.rim"]')
            bound_button.focus()
            bound_button.press("Enter")
            page.wait_for_function(
                "document.querySelector('[data-validation-subject=\"hub.rim\"]').getAttribute('aria-pressed') === 'true'"
            )
            bound_point = atomic_payload["targets"]["hub.rim"]["targets"][0]["point"]
            check(
                page.evaluate(
                    "window.__validationViewer._rendered.scene.getObjectByName('bt3d-validation-marker').children[0].position.toArray()"
                )
                == bound_point,
                "legacy bound atom marker uses actual payload position",
            )
            atomic_pixels = settle("legacy-bound-atom")
            check(
                sum(
                    1
                    for r, g, b in atomic_pixels.getdata()
                    if r > 180 and g > 150 and b < 100
                )
                > 50,
                "legacy bound atom marker visibly rendered",
            )
            atomic_payload["atomic"]["blocks"][0]["binding"]["version"] += 1
            page.locator("#bt3d-isolate").select_option("hub")
            bound_button.click()
            page.wait_for_function(
                "document.querySelector('#bt3d-validation-status').textContent.includes('Bound atom geometry changed')"
            )
            check(
                bound_button.get_attribute("aria-pressed") == "false",
                "atomic overlay identity mismatch rejects selection",
            )
            page.goto(f"http://127.0.0.1:{server.server_port}/molecule-fixture")
            page.wait_for_function("window.fixtureRendered || window.fixtureDone")
            page.screenshot(path=str(output / "structure-core-rendered.png"))
            page.wait_for_function("window.fixtureDone")
            structure = page.evaluate("window.fixtureReport")
            check(
                structure.get("pass") is True, f"standalone extracted core: {structure}"
            )
            report["standalone_structure"] = structure
            check(set(requests) == {"GET"}, "interaction issues no mutation requests")
            check(not errors, "no browser runtime errors")
            page.screenshot(path=str(output / "final-page.png"), full_page=True)
            browser.close()
    finally:
        hold.set()
        server.shutdown()
        server.server_close()
        report["page_errors"] = errors
        (output / "report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(report))


if __name__ == "__main__":
    probe(
        Path(sys.argv[1]),
        Path(sys.argv[2]),
        sys.argv[3] if len(sys.argv) > 3 else "chromium",
    )
