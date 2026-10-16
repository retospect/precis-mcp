"""Shared paper-identifier lookup for the LaTeX/docx cite-link groups.

``precis.export.latex`` and ``precis.export.docx`` each append a small
``doi`` / ``UL`` hyperlink pair next to a citation; both need "the one
identifier a cite-link group hangs off" — a DOI, else an
``arxiv:``-prefixed arXiv id, else ``""`` — resolved via the store's
``identifiers_for_refs`` batch alias lookup. Kept here so the logic isn't
duplicated byte-for-byte across the two exporters.

Each exporter still memoizes the result on its own ``_Ctx`` (the two
``_Ctx`` types are unrelated), so this module stays a plain stateless
function — no cache, no store type, just the callable each store exposes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def paper_identifier(
    ref_id: int,
    identifiers_for_refs: Callable[[list[int]], dict[int, Any]] | None,
) -> str:
    """DOI (bare) or ``arxiv:<id>`` for ``ref_id``, else ``""``.

    ``identifiers_for_refs`` is the store's batch alias lookup
    (``Store.identifiers_for_refs`` shape, ``{ref_id: {"doi": ..., "arxiv":
    ...}}``); pass ``None`` (or anything non-callable) when the store
    doesn't expose it — this then just returns ``""``.
    """
    if not callable(identifiers_for_refs):
        return ""
    try:
        alias = identifiers_for_refs([ref_id]).get(ref_id, {})
    except Exception:  # pragma: no cover — store hiccup
        alias = {}
    if isinstance(alias, dict):
        if alias.get("doi"):
            return str(alias["doi"]).strip()
        if alias.get("arxiv"):
            return f"arxiv:{str(alias['arxiv']).strip()}"
    return ""


def merged_cite_numbers(nums: list[int]) -> str:
    """The bracket text for a merged citation run: ``[n]`` for one paper,
    ``[1, 2]`` for a short list, a run of ≥3 consecutive numbers collapsing
    to ``[1–3]`` — what biblatex's numeric-comp prints for the merged
    ``\\cite{…}`` that ``export/latex.py::_merge_adjacent_cites`` builds.
    Shared by the docx plain mark (:func:`export.docx._flush_cites`) and the
    cached display of an EndNote grouped-citation field
    (:func:`export.endnote.add_citation_field`), which have no biblatex to
    do the compression for them. ``nums`` must be sorted ascending."""
    parts: list[str] = []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        if j - i >= 2:
            parts.append(f"{nums[i]}–{nums[j]}")
        else:
            parts.extend(str(n) for n in nums[i : j + 1])
        i = j + 1
    return "[" + ", ".join(parts) + "]"
