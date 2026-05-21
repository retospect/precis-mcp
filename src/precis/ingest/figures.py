"""Figure image extraction and caption matching."""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

FIGURE_CAPTION_RE = re.compile(
    r"^(?:Fig(?:ure)?\.?\s*\d+)\s*[:\.\-—–]\s*(.+)",
    re.IGNORECASE | re.MULTILINE,
)


def match_figure_captions(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match figure blocks with captions from adjacent text blocks.

    Scans blocks sequentially. When a figure block is found, looks at
    the next block for a 'Figure N:' caption pattern. If found, merges
    the caption into the figure block's text field.

    Args:
        blocks: List of block dicts from Marker output.

    Returns:
        Blocks with figure captions merged.
    """
    result = []
    skip_next = False

    for i, block in enumerate(blocks):
        if skip_next:
            skip_next = False
            continue

        if block.get("type") == "figure":
            # Look at next block for caption
            if i + 1 < len(blocks):
                next_block = blocks[i + 1]
                next_text = next_block.get("text", "")
                match = FIGURE_CAPTION_RE.match(next_text.strip())
                if match:
                    block["text"] = next_text.strip()
                    skip_next = True

        result.append(block)

    return result


def encode_image(image_path: str | Path) -> tuple[str, str]:
    """Base64-encode an image file.

    Returns:
        Tuple of (base64_string, mime_type).
    """
    path = Path(image_path)
    suffix = path.suffix.lower()
    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
    }
    mime = mime_map.get(suffix, "image/png")
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return data, mime
