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

Abbreviations: :func:`term_segments` decides which occurrence defines a
short (first use in reading order, nested long forms, never in a heading;
the rules are :mod:`precis.export._abbrev`'s); :meth:`Renderer.term`
only formats each use (``\\gls`` family; ``long (SHORT)`` text).

Cross-references: :func:`number_xrefs` is the numbering pre-pass over chunk
structure (sections, figures, equations; it reads no inline text, so it
cannot renumber a cite); :func:`resolve_xref` decides liveness, the
downgrade warning and what number to print; :func:`merge_xref_runs` joins
whitespace-separated refs (LaTeX's ``\\cref{a,b}``). LaTeX lets cleveref
count at compile time; docx prints the pre-pass numbers. A legacy ``¶``
handle is still mapped to its ``dc`` in each backend's reference dispatch,
which moves with the chunk loop.

Back matter: :func:`back_matter` picks the blocks that apply (no references
in patent mode, the published-claims appendix only on request) in the one
shared order; claim-cite bookkeeping, the claims table's source numbers
and the acronym list's membership are here too. A backend formats each
block (:meth:`Renderer.back_matter`).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from precis.export import _abbrev, _back_matter
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

    def term(self, sink: Any, use: _abbrev.Use) -> None:
        """Format one abbreviation use: a definition (``mode='first'``,
        possibly a ``pair``), a later use, a bare heading use, or an
        authored definition that only marks the short defined."""

    def xref(self, sink: Any, xref: Xref) -> None:
        """Format one intra-draft cross-ref: downgraded text, the author's
        surface as a link, or the target's number / name."""

    def back_matter(self, sink: Any, name: str, block: Any) -> None:
        """Format one back-matter block (:func:`back_matter` picks which
        and in what order); ``block`` is the backend's own payload."""


def term_segments(
    text: str,
    abbrevs: dict[str, str],
    seen: set[str],
    used: set[str] | None = None,
    *,
    preceding: str | None,
    heading: bool,
) -> list[_abbrev.Segment]:
    """Split one prose run into literal text and abbreviation uses, deciding
    which occurrence defines a short (:func:`precis.export._abbrev.expand`,
    gr491599). ``seen`` (defined shorts) and ``used`` (shorts that occurred)
    thread through the whole export in reading order; ``preceding`` is the
    paragraph text already rendered before ``text``; in a heading every use
    is bare and defines nothing. Literal strings go out as prose; each
    :class:`~precis.export._abbrev.Use` goes to :meth:`Renderer.term`."""
    return _abbrev.expand(
        text, abbrevs, seen, used, preceding=preceding, heading=heading
    )


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


# ── cross-references ─────────────────────────────────────────────


#: Top-level headings that are journal back matter: exported unnumbered
#: (``\\section*`` + a TOC line), their subsections likewise.
BACK_MATTER_HEADINGS = frozenset(
    {
        "author information",
        "author contributions",
        "acknowledgements",
        "acknowledgments",
        "notes",
        "conflicts of interest",
        "competing interests",
        "data availability",
        "supporting information",
    }
)

#: LaTeX's sectioning depth that still carries a number (``article``:
#: section, subsection, subsubsection); deeper headings are run-in paragraphs.
NUMBERED_DEPTHS = 3

#: Chunk kinds the render loops dispatch on their own branch — a
#: standalone-equation paragraph can only occur in whatever's left over
#: (the "paragraph (default)" branch).
EQ_SKIP_KINDS = frozenset(
    {"ulist", "olist", "item", "term", "heading", "code", "listing", "table", "figure"}
)


def is_back_matter_heading(text: str) -> bool:
    return " ".join((text or "").split()).casefold() in BACK_MATTER_HEADINGS


def is_glossary_heading(c: Any) -> bool:
    """The draft's own "Glossary" heading — the acronym list is generated,
    so it renders as nothing and consumes no section number."""
    return c.chunk_kind == "heading" and (c.text or "").strip().lower() == "glossary"


def title_heading_dc(chunks: list[Any], title: str | None) -> str | None:
    """The ``dc`` of a first-in-reading-order depth-0 heading whose text is
    the draft's title (whitespace-normalised, case-insensitive) — the chunk
    ``create_draft`` seeds; ``None`` when the draft doesn't start that way."""
    if not chunks or not title:
        return None
    first = chunks[0]
    if first.chunk_kind != "heading" or int(first.depth) != 0:
        return None

    def norm(s: str | None) -> str:
        return " ".join((s or "").split()).lower()

    return str(first.dc) if norm(first.text) == norm(title) else None


def depth_shift(chunks: list[Any], title_dc: str | None) -> int:
    """1 when the body is nested under the seeded title chunk (so sections
    number from 1, not 1.x), else 0; a draft whose sections sit beside the
    title at depth 0 already numbers from 1."""
    return 1 if title_dc and len(chunks) > 1 and int(chunks[1].depth) >= 1 else 0


def back_matter_handles(chunks: list[Any]) -> frozenset[str]:
    """``dc`` handles of every heading inside a back-matter section: a
    matching depth-0 heading and the headings nested under it, up to the
    next depth-0 heading."""
    out: set[str] = set()
    inside = False
    for c in chunks:
        if c.chunk_kind != "heading":
            continue
        if c.depth == 0:
            inside = is_back_matter_heading(c.text or "")
        if inside:
            out.add(c.dc)
    return frozenset(out)


@dataclass(frozen=True)
class XrefNumbers:
    """The numbering pre-pass, walked over the whole draft before any
    rendering (a cross-ref can precede its target): ``labels`` maps a dc
    handle to ``(noun, number)`` as ``\\cref`` would print it (an empty
    noun: ``number`` is already the full label, a back-matter heading's
    name); ``numbered_headings`` are the headings that display a number;
    ``equations`` maps a standalone equation's dc to its number."""

    labels: dict[str, tuple[str, str]]
    numbered_headings: set[str]
    equations: dict[str, int]


def number_xrefs(
    chunks: list[Any],
    title: str | None,
    *,
    equation_of: Callable[[str | None], tuple[str, bool] | None],
) -> XrefNumbers:
    """Section / figure / equation numbers in reading order, the way the
    LaTeX layout counts them: a heading's depth picks section, subsection or
    subsubsection (the seeded title heading is skipped and, when the body is
    nested under it, every depth is lifted one level); the draft's own
    Glossary heading and its terms are skipped; journal back matter is
    unnumbered and a cross-ref to it prints its name; every figure steps the
    figure counter; like ``\\cref`` on a label inside a section, a plain
    paragraph/item/table resolves to the enclosing numbered section. An
    unstarred standalone equation (``equation_of``, the one detector both
    backends use) takes the next equation number. Reads chunk structure
    only: no inline text is resolved here, so no cite is numbered."""
    labels: dict[str, tuple[str, str]] = {}
    shown: set[str] = set()
    counters = [0] * NUMBERED_DEPTHS
    figs = 0
    title_dc = title_heading_dc(chunks, title)
    shift = depth_shift(chunks, title_dc)
    back_matter = back_matter_handles(chunks)
    current = ""  # number of the enclosing numbered section
    for c in chunks:
        if c.chunk_kind == "figure":
            figs += 1
            labels[c.dc] = ("fig.", str(figs))
        elif c.chunk_kind == "heading":
            if is_glossary_heading(c) or c.dc == title_dc:
                continue
            if c.dc in back_matter:
                labels[c.dc] = ("", " ".join((c.text or "").split()))
                continue
            d = max(int(c.depth) - shift, 0)
            if d < NUMBERED_DEPTHS:
                counters[d] += 1
                for i in range(d + 1, NUMBERED_DEPTHS):
                    counters[i] = 0
                shown.add(c.dc)
                current = ".".join(str(n) for n in counters[: d + 1])
            # else: run-in paragraph heading — resolves to the enclosing section
            labels[c.dc] = ("section", current)
        elif c.chunk_kind not in ("ulist", "olist") and current:
            labels[c.dc] = ("section", current)
    equations: dict[str, int] = {}
    for c in chunks:
        if c.chunk_kind in EQ_SKIP_KINDS:
            continue
        eq = equation_of(c.text)
        if eq is None or eq[1]:  # not standalone, or the starred escape hatch
            continue
        equations[c.dc] = len(equations) + 1
    return XrefNumbers(labels=labels, numbered_headings=shown, equations=equations)


@dataclass(frozen=True, slots=True)
class Xref:
    """One intra-draft cross-ref as the walker resolved it. ``live`` False:
    the target is not a live chunk of this draft (render the surface or the
    handle as text). ``surface``: the author's link text, which wins over
    any number. ``equation`` / ``label``: what to print (from the
    pre-pass, when the backend prints numbers itself); ``label`` is ``None``
    when there is nothing to print (front matter, glossary, or a section
    reference before the first numbered section)."""

    dc: str
    surface: str | None
    live: bool
    equation: int | None = None
    label: tuple[str, str] | None = None


def resolve_xref(
    dc: str,
    surface: str | None,
    *,
    live: Collection[str] | None,
    numbers: XrefNumbers | None,
    warnings: list[str],
) -> Xref:
    """Resolve a ``[dc<id>]`` cross-ref. ``live`` is the draft's live dc
    handles (``None``: unknown, treat as live); a dead target warns and
    downgrades. ``numbers`` is the pre-pass, for a backend that prints the
    number itself (LaTeX's cleveref counts at compile time and passes
    ``None``)."""
    if live is not None and dc not in live:
        warnings.append(f"cross-ref {dc}: no such live chunk — downgraded")
        return Xref(dc, surface, live=False)
    if surface or numbers is None:
        return Xref(dc, surface, live=True)
    eq = numbers.equations.get(dc)
    if eq is not None:
        return Xref(dc, None, live=True, equation=eq)
    label = numbers.labels.get(dc)
    if label is None or not label[1]:
        return Xref(dc, None, live=True)
    return Xref(dc, None, live=True, label=label)


def merge_xref_runs(
    items: Sequence[str | tuple[str, ...]],
) -> list[str | tuple[str, ...]]:
    """Cross-refs separated by nothing but whitespace become one multi-ref
    (``\\cref{a,b}``: cleveref prints "sections 1 and 2"); the whitespace
    between them is dropped. ``items`` are prose strings and label tuples."""
    out: list[str | tuple[str, ...]] = []
    for item in items:
        if isinstance(item, tuple):
            gap = out[-1] if out else None
            if isinstance(gap, str) and not gap.strip() and len(out) >= 2:
                if isinstance(out[-2], tuple):
                    out.pop()
            prev = out[-1] if out else None
            if isinstance(prev, tuple):
                out[-1] = prev + item
                continue
        out.append(item)
    return out


# ── back matter ──────────────────────────────────────────────────


def back_matter[T](
    blocks: Mapping[str, T | None], *, patent: bool, claim_appendix: bool = True
) -> Iterator[tuple[str, T]]:
    """The back-matter blocks this export prints, as ``(name, payload)`` in
    the one shared order (:data:`precis.export._back_matter.BACK_MATTER_ORDER`,
    read at call time): a patent specification cites in the running text,
    so it has no references; the published-claims appendix only when asked
    for; a block whose payload is ``None`` is absent."""
    kept = {
        name: payload
        for name, payload in blocks.items()
        if not (name == "references" and patent)
        and not (name == "published" and not claim_appendix)
    }
    return _back_matter.ordered_blocks(kept)


def record_claim_keys(
    seen: list[str],
    keys: Sequence[str],
    cited: list[str],
    resolve: Callable[[str], str],
) -> None:
    """Remember which bibliography keys a finding's cite site printed, for
    the published-claims section: each key resolved to the one it was cited
    under, kept only when the body already cited it (recording never mints
    a citation, so the bibliography is the same with or without the
    section), first-seen order."""
    for key in keys:
        base = resolve(key)
        if base in cited and base not in seen:
            seen.append(base)


def claim_source_numbers(keys: Sequence[str], cited: list[str]) -> list[int]:
    """Reference numbers of a claim's recorded source keys, ascending."""
    return sorted(cited.index(k) + 1 for k in keys if k in cited)


def acronym_list(used: set[str], abbrevs: dict[str, str]) -> list[str]:
    """The shorts an acronym list carries: every defined short the prose
    used (headings included), case-insensitively sorted."""
    return sorted((s for s in used if s in abbrevs), key=str.casefold)
