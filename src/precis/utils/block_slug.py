"""Stable, unique per-block slug minting, shared by the block parsers.

Markdown, plaintext, and tex parsers all need the same block-slug
contract so downstream code (anchored edit, search-result rendering)
and agent muscle memory carry over across kinds:

* **Headings** (markdown only) slugify the title, capped at 40 chars,
  with no content hash — the human-readable title *is* the identity.
* **Everything else** takes the first 5 words, slugified and capped at
  24 chars, plus a 6-char sha1 content hash so two paragraphs with the
  same leading words still get distinct slugs.
* Collisions within a single file are disambiguated with a numeric
  suffix (``-2``, ``-3``, …).

Markdown paragraphs additionally strip markdown decoration (``*_`~``
etc.) before deriving the leading words, via ``strip_markdown=True``.
"""

from __future__ import annotations

import hashlib
import re

from precis.utils.slug import slug_from_text

#: Markdown decoration stripped before deriving a paragraph slug so the
#: slug reads cleanly (``**bold**`` → ``bold``, not ``-bold-``).
_MD_DECORATION_RE = re.compile(r"[*_`~\[\]()!#>]+")


def mint_block_slug(
    text: str,
    taken: set[str],
    *,
    heading: bool = False,
    strip_markdown: bool = False,
) -> str:
    """Return a stable, unique slug for a block; record it in ``taken``.

    Args:
        text: The block's source text.
        taken: Slugs already used in this file; the minted slug is added.
        heading: Slugify the title with no content hash (markdown headings).
        strip_markdown: Strip markdown decoration before deriving words.
    """
    if heading:
        # Strip leading hashes from heading text before slugifying.
        title = text.lstrip("#").strip()
        base = slug_from_text(title, max_len=40)
    else:
        clean = _MD_DECORATION_RE.sub(" ", text) if strip_markdown else text
        first_words = " ".join(clean.split()[:5])
        base = slug_from_text(first_words, max_len=24)
        # Content hash makes block identity content-stable: two blocks
        # with the same leading words get *different* slugs.
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
        base = f"{base}-{h}" if base else f"p-{h}"

    if not base:
        # Defensive: pure-symbol heading fallback.
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
        base = f"h-{h}"

    if base not in taken:
        taken.add(base)
        return base

    # Collision (rare for hashed kinds; common for "Conclusion" headings).
    for n in range(2, 10000):
        candidate = f"{base}-{n}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    raise ValueError(f"unreachable: more than 10k collisions on {base!r}")


__all__ = ["mint_block_slug"]
