"""Render a reviewer-persona skill into a ready-for-claude prompt.

Thin wrapper around ``precis.handlers.skill._load_skill`` which
already resolves ``{{include doc:…}}`` directives — same code path
the MCP server uses when an agent fetches the skill. The only thing
this script adds is substituting ``<handle>`` with the paper handle
the harness is targeting, since the persona files carry it as a
placeholder.

Usage:
    uv run python scripts/review-paper/_render_persona.py \\
        --persona precis-adversarial-reviewer \\
        --handle paper:smith2024
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from precis.handlers.skill import _load_skill  # noqa: E402  (path setup above)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--persona", required=True, help="Persona slug (filename stem)")
    ap.add_argument(
        "--handle", required=True, help="Paper handle to substitute for <handle>"
    )
    args = ap.parse_args()

    body = _load_skill(args.persona)
    if body is None:
        raise SystemExit(f"persona {args.persona!r} not found in data/skills/")

    sys.stdout.write(body.replace("<handle>", args.handle))


if __name__ == "__main__":
    main()
