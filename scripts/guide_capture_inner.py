#!/usr/bin/env python3
"""Runs INSIDE the official Playwright container, driving one tour section's
clean-page screenshots.

Not invoked directly by a developer — ``scripts/guide-capture`` (the outer,
host-side orchestrator) mounts the worktree into a Playwright container
(``mcr.microsoft.com/playwright/python``, the proven local-web-demo recipe:
host Playwright can't launch on macOS) and runs this file once per tour
manifest, with the target route already resolved (any ``{id}`` filled).

Deliberately minimal: only the stdlib + ``playwright`` + this file's sibling
``guide_lib.py`` (imported via a ``sys.path`` insert, not installed — the
Playwright image has no ``precis-mcp`` venv). No tour overlay is activated
(no ``?tour=``) — this captures the page exactly as a real visitor sees it;
``scripts/guide-annotate`` burns the callouts on afterwards from the sidecar
this script writes.

Usage (see ``scripts/guide-capture`` for the real invocation)::

    python guide_capture_inner.py \\
        --manifest /work/src/precis_web/manual/tour/01-drive.json \\
        --route /drive --base-url http://precis-web-demo:9105 \\
        --out /work/guide/assets/drive --width 1600 --height 1000
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from guide_lib import GuideError, load_manifest, slug_from_manifest_path


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", required=True, type=Path)
    p.add_argument("--route", required=True, help="Already-{id}-resolved route.")
    p.add_argument("--base-url", required=True)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--width", type=int, default=1600)
    p.add_argument("--height", type=int, default=1000)
    p.add_argument(
        "--timeout-ms", type=int, default=15000, help="Navigation/idle timeout."
    )
    return p.parse_args(argv)


def capture(args: argparse.Namespace) -> None:
    from playwright.sync_api import sync_playwright

    manifest = load_manifest(args.manifest)
    slug = slug_from_manifest_path(args.manifest)
    args.out.mkdir(parents=True, exist_ok=True)

    url = args.base_url.rstrip("/") + args.route
    steps_out: list[dict[str, object]] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(
                viewport={"width": args.width, "height": args.height}
            )
            page.goto(url, timeout=args.timeout_ms)
            page.wait_for_load_state("networkidle", timeout=args.timeout_ms)

            for i, step in enumerate(manifest["steps"], start=1):
                anchor = step["anchor"]
                locator = page.locator(f'[data-tour="{anchor}"]')
                if locator.count() == 0:
                    raise GuideError(
                        f"missing anchor {anchor!r} on route {args.route!r} "
                        f"(manifest {manifest['title']!r}, step {i}) — "
                        "the page and the tour manifest have drifted apart"
                    )
                locator.first.scroll_into_view_if_needed(timeout=args.timeout_ms)
                page.wait_for_timeout(150)  # settle any scroll/layout animation

                box = locator.first.bounding_box()
                if box is None:
                    raise GuideError(
                        f"anchor {anchor!r} on route {args.route!r} resolved "
                        "but has no bounding box (hidden/zero-size element)"
                    )

                png_name = f"step-{i}.png"
                page.screenshot(path=str(args.out / png_name), full_page=False)

                steps_out.append(
                    {
                        "index": i,
                        "anchor": anchor,
                        "png": png_name,
                        "rect": {
                            "x": box["x"],
                            "y": box["y"],
                            "width": box["width"],
                            "height": box["height"],
                        },
                    }
                )

            final_url = page.url
        finally:
            browser.close()

    sidecar = {
        "slug": slug,
        "title": manifest["title"],
        "route": args.route,
        "url": final_url,
        "viewport": {"width": args.width, "height": args.height},
        "steps": steps_out,
    }
    (args.out / "steps.json").write_text(
        json.dumps(sidecar, indent=2) + "\n", encoding="utf-8"
    )
    print(f"guide-capture: {slug} -> {args.out} ({len(steps_out)} steps)")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        capture(args)
    except GuideError as exc:
        print(f"GUIDE-CAPTURE ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
