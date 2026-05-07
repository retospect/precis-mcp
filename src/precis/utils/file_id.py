"""Shared addressing helpers for prose-file kinds (``markdown``,
``plaintext``, ``tex``).

Pulls the id-parsing / slug-nearest-match / write-result-formatter
logic that was duplicated across the per-kind handlers into one
module so a fix (and a test) covers every prose-file kind.

What this module does **not** do:

- It does not own ``_BlockSel`` — the handlers each define their
  own minimal dataclass over this module's ``BlockSel`` shape because
  the live type lived alongside handler-local helpers (``_find_block``
  etc.) and renaming every use site is out of scope for the bug-fix
  pass that landed this module.
- It does not resolve the selector against a file — ``_find_block`` /
  ``_find_block_by_lines`` live in the handler and know the handler's
  block type (``MdBlock`` / ``PlaintextBlock`` / ``TexBlock``).
- It does not walk the filesystem — the handler owns the root-aware
  path resolution.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from difflib import get_close_matches

from precis.errors import BadInput
from precis.utils.md_parse import file_slug_from_path, is_valid_file_slug

# ── Selector shape ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BlockSel:
    """Parsed form of the ``~…`` portion of a file-kind id.

    Three variants, discriminated by the boolean flags:

    - ``is_pos=True``  → ``value`` is a digit string, ``int(value)``
      is the 0-indexed block position.
    - ``is_line_range=True`` → ``value`` is the raw ``L<a>-<b>`` /
      ``L<a>`` form; ``line_start`` / ``line_end`` are 1-indexed and
      inclusive (``line_end == line_start`` for a single-line form).
    - Both False → ``value`` is a content-derived block slug.

    The dataclass is frozen so handlers can hash it / cache on it
    without worrying about aliasing mutations.
    """

    value: str
    is_pos: bool = False
    is_line_range: bool = False
    line_start: int = 0
    line_end: int = 0


_INT_RE = re.compile(r"^\d+$")
_LINE_SEL_RE = re.compile(r"^L(\d+)(?:-(\d+))?$")


def _path_ext_pattern(extensions: Sequence[str]) -> re.Pattern[str]:
    """Build a regex that matches any of ``extensions`` when the
    extension ends the path portion (end-of-string, ``~``, or ``/``).

    Extensions arrive as ``('.md', '.markdown')`` — we strip the
    leading dot, escape, and join with ``|``.
    """
    if not extensions:
        # Defensive: no extensions means no path-form canonicalisation
        # — we still want a compiled pattern that matches nothing.
        return re.compile(r"(?!)")
    alt = "|".join(re.escape(e.lstrip(".")) for e in extensions)
    return re.compile(rf"\.(?:{alt})(?=$|[/~])", re.IGNORECASE)


def canonicalize_path_id(raw: str, *, extensions: Sequence[str]) -> str:
    """Turn path-form ids into slug-form.

    The docs advertise both forms interchangeably::

        get(kind='markdown', id='notes/meeting.md')       # path-form
        get(kind='markdown', id='notes--meeting')         # slug-form

    Without this helper the path-form's ``/`` was parsed as a view
    separator and the extension was silently dropped — so
    ``notes/critique-probe.md`` collapsed to slug ``notes`` (MCP
    critic CRITICAL-C, 2026-05-02). This function detects a
    path-form id by looking for one of the handler's known
    extensions anchored before the end-of-string, a ``~`` selector,
    or a ``/view`` separator; the path portion is converted to a
    file-slug via :func:`file_slug_from_path` and the unchanged
    remainder (``~...`` or ``/...``) is re-attached.

    Also handles leading ``./`` (current directory) which would
    otherwise split into slug ``.`` (MCP critic CRITICAL-C 2026-05-03).

    Inputs that are already in slug-form (no ``/``, or a ``/`` that
    isn't preceded by a known extension) are returned unchanged.
    ``ValueError`` from :func:`file_slug_from_path` and a
    non-canonical slug from :func:`is_valid_file_slug` both
    fall-through to the original string; the downstream path
    resolver will raise a clearer error than this helper can.
    """
    # Strip leading ./ (and collapse ../ traversal to avoid escape)
    s = raw.removeprefix("./")
    if s.startswith("../"):
        raise BadInput(
            f"path traversal not allowed: {raw!r}",
            next="use a simple slug or a path under the configured root",
        )

    # Accept handler-specific extensions plus common prose extensions
    # so e.g. plaintext can accept ./request_doi.md → request-doi
    common_exts = (".md", ".markdown", ".txt", ".log", ".bib", ".tex")
    all_exts = tuple(dict.fromkeys(tuple(extensions) + common_exts))
    m = _path_ext_pattern(all_exts).search(s)
    if m is None:
        return s if s != raw else raw

    # If there's no / but we matched an extension (e.g., "file.md"),
    # canonicalise to slug form (e.g., "file") as long as the result
    # is a valid slug.
    if "/" not in s:
        path_part = s[: m.end()]
        rest = s[m.end() :]
        try:
            slug = file_slug_from_path(path_part)
        except ValueError:
            return s if s != raw else raw
        if is_valid_file_slug(slug):
            return slug + rest
        return s if s != raw else raw

    # Path contains / — split and canonicalise the path portion
    path_part = s[: m.end()]
    rest = s[m.end() :]
    try:
        slug = file_slug_from_path(path_part)
    except ValueError:
        return s if s != raw else raw
    if not is_valid_file_slug(slug):
        return s if s != raw else raw
    return slug + rest


def parse_line_range(after: str, *, raw_id: str) -> BlockSel | None:
    """Recognise a ``L<n>`` / ``L<n>-<m>`` selector, else return ``None``.

    ``raw_id`` is used only in the error message so the caller can
    refer back to the original id they typed.
    """
    m = _LINE_SEL_RE.match(after)
    if m is None:
        return None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    if start < 1 or end < start:
        raise BadInput(
            f"invalid line range {after!r} in {raw_id!r}",
            next=(
                "use ~L<n> for a single line or ~L<start>-<end> with 1 <= start <= end"
            ),
        )
    return BlockSel(
        value=after,
        is_line_range=True,
        line_start=start,
        line_end=end,
    )


# ── Slug-miss options= helper ─────────────────────────────────────


def nearest_slugs(
    target: str, candidates: Iterable[str], *, n: int = 5, cutoff: float = 0.4
) -> list[str]:
    """Top-N close matches for ``target`` against ``candidates``.

    Used to populate ``NotFound.options=`` on a block-slug miss so
    agents recovering from a one-character typo don't need to pay
    for a full ``/toc`` fetch. The cutoff is lower than difflib's
    default (0.6) because block slugs are short and a single
    character difference on an 8-char slug already drops similarity
    below 0.6.
    """
    cand_list = [c for c in candidates if c]
    if not cand_list:
        return []
    return get_close_matches(target, cand_list, n=n, cutoff=cutoff)


# ── Unified write-result formatter ────────────────────────────────


def format_write_result(
    *,
    verb: str,
    file_slug: str,
    block_pos: int | None = None,
    block_slug: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    span_count: int = 1,
) -> str:
    """One-line ack returned by every write verb on a prose-file kind.

    Shape::

        <verb> block <N> '<block-slug>' (L<a>-<b>) in '<file-slug>'

    Any of the block / line fields may be ``None``; this is defensive
    — fresh ingest always supplies them, but a caller who wrote a
    span crossing a block boundary (``find-replace match='all'``)
    gets the first span's location with ``[<N> spans]`` appended.

    The format is pinned by
    ``tests/test_files_write.py::test_edit_response_shape`` (MCP
    critic MAJOR-C, 2026-05-02).
    """
    block_parts: list[str] = []
    if block_pos is not None:
        block_parts.append(f"block {block_pos}")
    if block_slug:
        block_parts.append(f"{block_slug!r}")
    block_desc = " ".join(block_parts) or "file"

    line_part = ""
    if line_start is not None:
        if line_end is not None and line_end != line_start:
            line_part = f" (L{line_start}-{line_end})"
        else:
            line_part = f" (L{line_start})"

    span_note = f" [{span_count} spans]" if span_count > 1 else ""
    return f"{verb} {block_desc}{line_part} in {file_slug!r}{span_note}"


__all__ = [
    "BlockSel",
    "canonicalize_path_id",
    "format_write_result",
    "nearest_slugs",
    "parse_line_range",
]
