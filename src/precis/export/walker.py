"""The shared export walker: decisions the LaTeX and docx backends share.

``export/latex.py`` and ``export/docx.py`` used to each re-implement the
same citation decisions (SI resolution, numbering, run merging, same-paper
collapse), so a fix in one left the other wrong
(docs/backlog/export-latex-docx-parity.md: eight parity fixes in one churn
window). This module owns those decisions; each backend is a
:class:`Renderer` that only formats the resolved values it is handed
(``\\cite`` markup and biblatex link groups; python-docx runs and EndNote
fields).

Shape (Reto 2026-10-17): a pluggable generator, not a materialized tree.
The backends still drive their own chunk loops until the
headings/figures/equations/tables slice moves them here; until then each
backend calls into this module at the decision points and passes the
result to its renderer.

Citations:

* A cite number is the key's first-use position in reading order
  (:func:`register`). Nothing prints a cite number before its first use,
  so the walk itself is the numbering pass for cites; the forward-reference
  numbers (sections, figures, equations) get their pre-pass with the xref
  slice.
* A supplementary-information record cites as its parent paper with an
  SI flag; an alias cite key canonicalises to the store's slug
  (:func:`resolve_si_slug`).
* The two backends still group adjacent marks differently, and the
  migration is output-preserving, so both rules live here side by side:
  LaTeX merges directly adjacent plain marks after the paragraph is
  emitted (:func:`merge_adjacent`, with :class:`RepeatGuard` dropping a
  mark identical to the one right before it); docx buffers a run as it
  walks (:class:`CiteRun`, which collapses consecutive cites of one paper
  across whitespace). Unifying the two is an output change and gets its own
  commit.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from precis.store.si_links import SI_PDF_ROLE, cite_target_for


@dataclass(frozen=True, slots=True)
class CiteGroup:
    """One citation mark as resolved by the walker: the bibliography keys
    it cites in citation order (distinct), each key's 1-based reference
    number, whether each key was cited from its supplementary-information
    record, and the cited passage (``pc<id>`` chunk id) per key, if any."""

    keys: tuple[str, ...]
    numbers: tuple[int, ...]
    si: tuple[bool, ...]
    chunk_ids: tuple[int | None, ...] = ()

    @property
    def mergeable(self) -> bool:
        """A plain mark: may join an adjacent run (an SI postnote never does)."""
        return not any(self.si)


class Renderer(Protocol):
    """What a document backend implements. The walker passes resolved
    values; a renderer formats them into its ``sink`` (a ``list[str]`` of
    TeX for LaTeX, a python-docx paragraph for docx) and decides nothing."""

    def cite_group(self, sink: Any, group: CiteGroup) -> None:
        """Format one citation mark and its link group."""


def register(cited: list[str], key: str) -> int:
    """Register ``key`` in the export's bibliography (first-use order) and
    return its 1-based reference number. Idempotent."""
    if key not in cited:
        cited.append(key)
    return cited.index(key) + 1


def resolve_si_slug(
    slug: str,
    store: Any,
    *,
    si_cache: dict[str, tuple[str, bool]],
    target_cache: dict[int, tuple[Any, bool]],
    warnings: list[str],
) -> tuple[str, bool]:
    """A bare cite slug -> ``(key_to_cite, is_si)``. An alias resolves to the
    one slug the store returns for its ref, so aliases share one entry. A
    supplementary-information record resolves to its parent's slug
    (``is_si`` True); with no live parent it keeps its own slug and warns.
    Memoised in ``si_cache`` per export."""
    if store is None:
        return slug, False
    if slug in si_cache:
        return si_cache[slug]
    result = (slug, False)
    get_ref = getattr(store, "get_ref", None)
    ref = get_ref(kind="paper", id=slug) if callable(get_ref) else None
    # Canonical key: a ref can hold several cite_key aliases; cite and bib it
    # under the one slug the store returns for it, so aliases merge.
    own = getattr(ref, "slug", None)
    if isinstance(own, str) and own:
        result = (own, False)
    if ref is not None and getattr(ref, "pdf_role", None) == SI_PDF_ROLE:
        target, is_si = cite_target_for(store, ref, target_cache)
        if is_si and target.slug:
            result = (str(target.slug), True)
        else:
            warnings.append(
                f"cite {slug!r}: supplementary record has no live parent paper"
                " - cited under its own entry"
            )
    si_cache[slug] = result
    return result


def resolve_cite_handle(store: Any, handle: str) -> tuple[str, int | None, bool] | None:
    """A paper/patent handle (``pc<chunk_id>`` / ``pa<ref_id>`` / ``pt<id>``)
    -> ``(cite_key, chunk_id, is_si)`` via the store's one resolver, or
    ``None`` when it does not resolve. A handle inside a supplementary
    record cites its parent's key (``is_si`` True); ``chunk_id`` is set only
    for a chunk handle (the cited passage)."""
    if store is None:
        return None
    try:
        resolved = store.resolve_handle(handle)
    except Exception:  # pragma: no cover — store hiccup
        return None
    if resolved is None or not resolved.public_id:
        return None
    # An SI ref cites as its parent paper (si-attachments ruling 3).
    parent = getattr(resolved, "cite_public_id", None)
    return (
        parent or resolved.public_id,
        getattr(resolved, "chunk_id", None),
        bool(parent and parent != resolved.public_id),
    )


def key_group(
    keys: Sequence[str], resolve: Callable[[str], tuple[str, bool]]
) -> list[tuple[str, bool]]:
    """Several cite keys for one mark (a finding with several sources) ->
    distinct ``(key, is_si)`` pairs after SI resolution, in order. A key is
    SI only when every occurrence of it came from a supplementary record."""
    resolved = [resolve(k) for k in keys]
    distinct = list(dict.fromkeys(k for k, _si in resolved))
    return [(k, all(s for kk, s in resolved if kk == k)) for k in distinct]


def register_group(
    pairs: Sequence[tuple[str, bool]],
    cited: list[str],
    si_cited: list[str] | None = None,
) -> CiteGroup:
    """Register every key of a resolved group (``si_cited`` also records the
    SI ones) and return the mark."""
    numbers = []
    for key, si in pairs:
        numbers.append(register(cited, key))
        if si and si_cited is not None:
            register(si_cited, key)
    return CiteGroup(
        keys=tuple(k for k, _si in pairs),
        numbers=tuple(numbers),
        si=tuple(si for _k, si in pairs),
    )


def merge_adjacent(
    items: Sequence[str | CiteGroup],
) -> list[str | CiteGroup]:
    """LaTeX grouping: a run of directly adjacent plain marks (nothing but
    empty strings between them) becomes one mark, keys deduped in first-cited
    order; a mark with an SI postnote stands alone and breaks the run."""
    out: list[str | CiteGroup] = []
    run: list[CiteGroup] = []

    def flush() -> None:
        if not run:
            return
        if len(run) == 1:
            out.append(_dedupe(run[0]))
        else:
            out.append(_dedupe(_concat(run)))
        run.clear()

    for item in items:
        if isinstance(item, CiteGroup) and item.mergeable:
            run.append(item)
        elif isinstance(item, str) and not item:
            continue
        else:
            flush()
            out.append(item)
    flush()
    return out


def _concat(groups: Sequence[CiteGroup]) -> CiteGroup:
    return CiteGroup(
        keys=tuple(k for g in groups for k in g.keys),
        numbers=tuple(n for g in groups for n in g.numbers),
        si=tuple(s for g in groups for s in g.si),
        chunk_ids=tuple(c for g in groups for c in g.chunk_ids),
    )


def _dedupe(group: CiteGroup) -> CiteGroup:
    first: dict[str, int] = {}
    for i, k in enumerate(group.keys):
        first.setdefault(k, i)
    keep = list(first.values())
    return CiteGroup(
        keys=tuple(group.keys[i] for i in keep),
        numbers=tuple(group.numbers[i] for i in keep),
        si=tuple(group.si[i] for i in keep),
        chunk_ids=tuple(group.chunk_ids[i] for i in keep if i < len(group.chunk_ids)),
    )


@dataclass
class RepeatGuard:
    """LaTeX same-mark collapse within one paragraph: a reference whose
    rendered cite equals the one directly before it (no gap text between)
    prints once, so ``[fi1][fi2]`` resolving to one paper gives one mark."""

    _prev: str = ""

    def admit(self, rendered: str, *, gap: str, is_cite: bool) -> bool:
        """Should ``rendered`` (one reference's output, after ``gap``) print?"""
        if gap:
            self._prev = ""
        if is_cite:
            if rendered == self._prev:
                return False
            self._prev = rendered
        elif rendered:
            self._prev = ""
        return True


@dataclass
class CiteRun:
    """docx in-walk grouping: directly adjacent cites buffer into one mark;
    consecutive cites of the same paper (same SI flag) collapse to one, even
    across whitespace; an SI cite flushes the run and prints its own mark.
    Returned groups are ready for :meth:`Renderer.cite_group`."""

    _last: tuple[str, bool] | None = None
    _buffer: list[tuple[str, int, int | None]] = field(default_factory=list)

    def add(
        self, key: str, number: int, *, si: bool, chunk_id: int | None
    ) -> list[CiteGroup]:
        """Cite ``key``; return the marks to emit now (possibly none)."""
        if self._last == (key, si):
            return []
        self._last = (key, si)
        if not si:
            self._buffer.append((key, number, chunk_id))
            return []
        flushed = self.flush()
        own = CiteGroup((key,), (number,), (True,), (chunk_id,))
        return [own] if flushed is None else [flushed, own]

    def flush(self) -> CiteGroup | None:
        """End the pending run: one mark for it (a doubled paper once)."""
        run, self._buffer = self._buffer, []
        if not run:
            return None
        return _dedupe(
            CiteGroup(
                keys=tuple(k for k, _n, _c in run),
                numbers=tuple(n for _k, n, _c in run),
                si=tuple(False for _ in run),
                chunk_ids=tuple(c for _k, _n, c in run),
            )
        )

    def gap(self, text: str) -> CiteGroup | None:
        """Prose between references: any content ends the run; real
        (non-whitespace) prose also ends the same-paper collapse."""
        if not text:
            return None
        if text.strip():
            self._last = None
        return self.flush()

    def interrupt(self) -> None:
        """Non-cite content (or a new chunk) ends the same-paper collapse."""
        self._last = None
