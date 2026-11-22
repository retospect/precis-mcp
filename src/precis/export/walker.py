"""The shared export walker: decisions the LaTeX and docx backends share.

``export/latex.py`` and ``export/docx.py`` used to each re-implement the
same citation decisions (SI resolution, numbering, run merging, same-paper
collapse), so a fix in one left the other wrong
(the export-latex-docx-parity work, 2026-10-17: eight parity fixes in one churn
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
structure (sections, figures, tables, equations; it reads no inline text,
so it cannot renumber a cite); :func:`resolve_xref` decides liveness, the
downgrade warning and what number to print; :func:`merge_xref_runs` joins
whitespace-separated refs (LaTeX's ``\\cref{a,b}``) and
:func:`format_xref_run` prints such a run the way cleveref does ("Sections
1 and 2", "Figures 1 to 3"). LaTeX lets cleveref count and print at compile
time; docx prints the pre-pass numbers through :func:`format_xref_run`. A section
reference into the citing chunk's own section (or an ancestor) is the
word above/below in both (:func:`relative_word`). A legacy ``¶``
handle is still mapped to its ``dc`` in each backend's reference dispatch,
which moves with the chunk loop.

Back matter: :func:`back_matter` picks the blocks that apply (no references
in patent mode, the published-claims appendix only on request) in the one
shared order; claim-cite bookkeeping, the claims table's source numbers
and the acronym list's membership are here too. A backend formats each
block (:meth:`Renderer.back_matter`).

The body walk: :func:`walk_body` visits the draft once and calls the
renderer per chunk (title, anchor, lists, heading, figure, table, code,
aside, equation, paragraph); the poster genre runs it once per poster
section with the LaTeX renderer in poster mode.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from precis.export import _abbrev, _back_matter
from precis.store.si_links import SI_PDF_ROLE, cite_target_for
from precis.utils import handle_registry


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
    #: The block's plain text right before the mark and right after it
    #: (:func:`mark_context`); what :class:`RepeatMarks` reads to tell whether
    #: the sentence leans on the mark. Not part of the mark's identity.
    before: str = field(default="", compare=False)
    after: str = field(default="", compare=False)

    def joins(self, run: Sequence[CiteGroup]) -> bool:
        """May this mark join the adjacent ``run``? An SI mark joins and keeps
        its flag on its own number; only a paper already in the run with the
        OTHER flag (``[n]`` then ``[n] (SI)``) stays a separate mark."""
        flags = {k: s for g in run for k, s in zip(g.keys, g.si, strict=True)}
        return all(
            flags.get(k, s) == s for k, s in zip(self.keys, self.si, strict=True)
        )

    def by_number(self) -> CiteGroup:
        """A merged mark with an SI member prints its numbers ascending (the
        SI mark rides on its own number); other marks are left as they are."""
        if len(self.keys) < 2 or not any(self.si):
            return self
        order = sorted(range(len(self.keys)), key=lambda i: self.numbers[i])
        return CiteGroup(
            keys=tuple(self.keys[i] for i in order),
            numbers=tuple(self.numbers[i] for i in order),
            si=tuple(self.si[i] for i in order),
            chunk_ids=tuple(self.chunk_ids[i] for i in order)
            if len(self.chunk_ids) == len(self.keys)
            else self.chunk_ids,
            before=self.before,
            after=self.after,
        )


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

    #: Chunk kinds this backend renders as a code block / an aside box
    #: (:func:`walk_body`); any other kind falls to ``paragraph``.
    code_kinds: frozenset[str]
    aside_kinds: frozenset[str]

    def title(self, sink: Any, chunk: Any | None) -> None:
        """The draft title: the seeded title chunk, or ``None`` when the
        draft does not open with one."""

    def anchor(self, sink: Any, chunk: Any) -> None:
        """A chunk that prints nothing in the body (a Glossary heading or
        a term) but may still be a cross-ref target."""

    def list_open(self, sink: Any, chunk: Any, kind: str, level: int) -> None:
        """A list container (``ulist``/``olist``) opens at nesting ``level``."""

    def list_close(self, sink: Any, kind: str) -> None:
        """The innermost open list container closes."""

    def list_item(self, sink: Any, chunk: Any, kind: str, level: int) -> None:
        """One list item; ``kind``/``level`` from its list ancestors."""

    def heading(self, sink: Any, chunk: Any, info: HeadingInfo) -> None:
        """A body heading at ``info.depth`` with its number / back-matter flag."""

    def figure(self, sink: Any, chunk: Any) -> None:
        """A figure chunk (image or placeholder, caption)."""

    def table(self, sink: Any, chunk: Any) -> None:
        """A table chunk."""

    def code(self, sink: Any, chunk: Any) -> None:
        """A verbatim code block."""

    def aside(self, sink: Any, chunk: Any) -> None:
        """An aside / box."""

    def equation(
        self, sink: Any, chunk: Any, body: str, number: int | None, starred: bool
    ) -> None:
        """A display equation; ``number`` from the pre-pass (``None`` when
        the backend numbers it itself), ``starred`` = unnumbered."""

    def paragraph(self, sink: Any, chunk: Any) -> None:
        """A prose paragraph."""


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
        if resolved is None:
            # ``<record>~<sel>`` isn't a handle, but a chunk selector on a
            # citable record still resolves through its base — ``pa5~3``
            # cites ``pa5``.
            base, sel = handle_registry.split_selector(handle)
            if sel is not None:
                resolved = store.resolve_handle(base)
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


_DOI_PREFIX = re.compile(r"\s*(?:https?://(?:dx\.)?doi\.org/|doi:)\s*", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[\W_]+")
_CITED_KINDS = ("paper", "patent", "datasheet")


def normalise_doi(raw: Any) -> str:
    """A DOI for comparison: ``https://doi.org/`` prefix stripped, lower-case;
    ``""`` for none."""
    return _DOI_PREFIX.sub("", str(raw or ""), count=1).strip().lower()


def _first_family(authors: Any) -> str:
    """The lead author's family name, lower-case (``""`` for none)."""
    from precis.utils.authors import author_display

    for a in authors if isinstance(authors, list) else []:
        if isinstance(a, dict) and str(a.get("family") or "").strip():
            return str(a["family"]).strip().lower()
        name = author_display(a, order="sortable")
        if name:
            head = name.split(",")[0] if "," in name else name.rsplit(" ", 1)[-1]
            return head.strip().lower()
    return ""


@dataclass
class DoiAliases:
    """One reference number for two records of the same work. ``cited`` and
    ``warnings`` are the export's own lists. :meth:`canonical` maps the key
    about to be registered to the already cited record with the same DOI (the
    first one cited), so a second record of that work takes its number and no
    second list entry; the export warns once per such pair.
    :meth:`same_work_warnings` is the after-the-walk check for the records
    this cannot merge (no DOI, or different ones)."""

    store: Any
    cited: list[str]
    warnings: list[str]
    _refs: dict[str, Any] = field(default_factory=dict, repr=False)
    _alias: dict[str, str] = field(default_factory=dict, repr=False)
    _dois: dict[str, str] = field(default_factory=dict, repr=False)

    def _ref(self, key: str) -> Any:
        if key not in self._refs:
            get_ref = getattr(self.store, "get_ref", None)
            ref = None
            if callable(get_ref):
                for kind in _CITED_KINDS:
                    ref = get_ref(kind=kind, id=key)
                    if ref is not None:
                        break
            self._refs[key] = ref
        return self._refs[key]

    def doi(self, key: str) -> str:
        """The record's normalised DOI, ``""`` when it has none."""
        if key in self._dois:
            return self._dois[key]
        ref = self._ref(key)
        fn = getattr(self.store, "identifiers_for_refs", None)
        doi = ""
        if ref is not None and callable(fn):
            try:
                alias = fn([ref.id]).get(ref.id, {})
            except Exception:  # pragma: no cover — store hiccup
                alias = {}
            if isinstance(alias, dict):
                doi = normalise_doi(alias.get("doi"))
        self._dois[key] = doi
        return doi

    def canonical(self, key: str) -> str:
        """The key to register for ``key``: itself, or the first cited record
        with the same DOI."""
        if key in self._alias:
            return self._alias[key]
        if key in self.cited:
            return key
        doi = self.doi(key)
        if doi:
            for other in self.cited:
                if self.doi(other) == doi:
                    self._alias[key] = other
                    if self._ref(other).id != self._ref(key).id:
                        self.warnings.append(
                            f"references {other} and {key} share DOI {doi}: "
                            "printed as one entry"
                        )
                    return other
        return key

    def same_work_warnings(self) -> list[str]:
        """A warning per pair of cited records that look like one work (same
        normalised title, year and first author family) without sharing a
        DOI; nothing is merged."""
        out: list[str] = []
        seen: dict[tuple[str, Any, str], list[int]] = {}
        for i, key in enumerate(self.cited):
            ref = self._ref(key)
            if ref is None:
                continue
            title = _NON_ALNUM.sub(" ", str(ref.title or "").lower()).strip()
            if not title:
                continue
            ident = (title, ref.year, _first_family(ref.authors))
            for j in seen.get(ident, []):
                a, b = self.cited[j], key
                if self._ref(a).id == ref.id:
                    continue  # one record under two cite keys, not two records
                if self.doi(a) and self.doi(a) == self.doi(b):
                    continue
                out.append(
                    f"references [{j + 1}] and [{i + 1}] look like the same "
                    f"work ({a}, {b})"
                )
            seen.setdefault(ident, []).append(i)
        return out


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
    """LaTeX grouping: a run of directly adjacent marks (nothing but empty
    strings between them) becomes one mark, keys deduped in first-cited
    order. An SI postnote stays attached to its own key inside the group
    (``\\cites{a}[SI]{b}``, numbers ascending); a paper cited both plain and
    SI back to back keeps two marks (:meth:`CiteGroup.joins`)."""
    out: list[str | CiteGroup] = []
    run: list[CiteGroup] = []

    def flush() -> None:
        if not run:
            return
        if len(run) == 1:
            out.append(_dedupe(run[0]))
        else:
            out.append(_dedupe(_concat(run)).by_number())
        run.clear()

    for item in items:
        if isinstance(item, CiteGroup):
            if run and not item.joins(run):
                flush()
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
        before=groups[0].before,
        after=groups[-1].after,
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
        before=group.before,
        after=group.after,
    )


#: Punctuation that must hug the preceding word: a dropped cite mark leaves
#: its leading space behind, which is pulled off before one of these.
CLOSING_PUNCT = (".", ",", ";", ":", "!", "?", ")")

#: A conjunction stranded at the end of prose by a dropped cite
#: (``(see [pa12] and [fi10])`` -> ``(see [pa12] and``), with the comma of
#: an Oxford list before it.
_STRANDED_JOINER = re.compile(r"(?:[,;]\s*)?\b(?:and|or)\s*$", re.IGNORECASE)


def trim_before_dropped(prose: str) -> str:
    """Prose that precedes a cite mark dropped for no source, cleaned for
    the closing punctuation that now follows it: trailing spaces go, and so
    does a stranded ``and`` / ``or`` (``see [pa12] and`` -> ``see [pa12]``).
    Both backends call this at the same point so a dropped cite reads the
    same in LaTeX and Word."""
    trimmed = prose.rstrip(" ")
    stripped = _STRANDED_JOINER.sub("", trimmed)
    return stripped.rstrip(" ") if stripped != trimmed else trimmed


def close_up_dropped(before: str, after: str) -> tuple[str, str]:
    """Prose either side of a repeated cite mark that was not printed
    (:class:`RepeatMarks`), closed up: closing punctuation hugs the word
    before (``More [5].`` -> ``More.``, and a stranded ``and`` goes,
    :func:`trim_before_dropped`), the end of the block leaves no trailing
    space, and two spaces become one (``as [5] and`` -> ``as and``)."""
    if not after or after[0] in CLOSING_PUNCT:
        return trim_before_dropped(before), after
    if after[0] == " " and before.endswith(" "):
        return before, after.lstrip(" ")
    return before, after


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


#: Markup that is not text: emphasis, code and math delimiters, sub/sup
#: tags, script and brace characters, the backslash of an escape.
_MARKUP = re.compile(r"</?su[bp]>|[*_^{}$\\`]")
#: How much of the block either side of a mark :func:`mark_context` keeps.
_CONTEXT_BEFORE = 80
_CONTEXT_AFTER = 24


def mark_context(before_src: str, after_src: str) -> tuple[str, str]:
    """The plain text directly around a cite mark, from the block's source
    text before it (a display link as its surface text, as the exporters
    accumulate it) and after it: the input of :func:`leans_on_mark`. Both
    backends call this on the same source, so they decide alike."""
    return (
        _MARKUP.sub("", before_src[-_CONTEXT_BEFORE:]),
        _MARKUP.sub("", after_src[:_CONTEXT_AFTER]),
    )


#: A word that hands the sentence to the citation: ``see [5]``, ``in ref. [5]``.
_LEAN_WORDS = frozenset(
    {
        *("see", "cf", "in", "by", "from", "of", "and", "or", "to", "as", "per"),
        *("with", "also", "ref", "refs", "reference", "references", "following"),
        *("after", "e.g", "i.e"),
    }
)
_WORD_BEFORE = re.compile(r"(?<![\w.-])([^\W\d_]+(?:\.[^\W\d_]+)*)\.?\Z")
_CLOSERS = ")]%\u201d\u2019\u00bb\u203a"


def _ends_a_phrase(before: str) -> bool:
    """Does the (stripped, non-empty) ``before`` end in a letter, digit,
    closing bracket, closing quotation mark or ``%``? A straight quote
    closes when it follows a word, not a space or an opening bracket."""
    c = before[-1]
    if c.isalnum() or c in _CLOSERS:
        return True
    if c in "\"'":
        prev = before[-2:-1]
        return bool(prev) and not prev.isspace() and prev not in "([{"
    return False


def leans_on_mark(before: str, after: str) -> bool:
    """Does the sentence need the cite mark between ``before`` and ``after``
    (:func:`mark_context`, spaces next to the mark ignored)? Yes when nothing
    precedes it, when what precedes does not end a phrase (``(``, ``,``, ``;``,
    ``:``, ``.``, a dash, an opening quote), when the word before is one of
    :data:`_LEAN_WORDS`, or when ``'s`` follows. Dropping such a mark leaves
    ``(see)``, ``()``, ``ref..`` or ``the model 's core``."""
    head = before.strip()
    if not head or not _ends_a_phrase(head):
        return True
    word = _WORD_BEFORE.search(head)
    if word is not None and word.group(1).lower() in _LEAN_WORDS:
        return True
    return after.lstrip().startswith(("'s", "\u2019s"))


@dataclass
class RepeatMarks:
    """The repeated-citation rule for ONE rendered prose block (a paragraph,
    a list item, a caption, one table cell; a fresh instance per block): a
    mark naming the same set of references as the previous *printed* mark of
    the block is not printed again, however much prose lies between. A mark
    naming a different set prints and becomes the one compared against; an
    SI mark differs from the same paper without the flag. A mark the sentence
    leans on (:func:`leans_on_mark`, read from the group's ``before`` /
    ``after``) prints and becomes the one compared against. A mark that
    carries a trust mark always prints and, as the text now between it and
    the next mark, resets the comparison. ``enabled=False`` prints every
    mark."""

    enabled: bool = True
    _prev: frozenset[tuple[str, bool]] | None = None

    def admit(self, group: CiteGroup, *, trusted: bool = False) -> bool:
        """Should ``group`` print? Records it as the previous printed mark."""
        marks = frozenset(zip(group.keys, group.si, strict=True))
        if trusted:
            self._prev = None
            return True
        if (
            self.enabled
            and marks == self._prev
            and not leans_on_mark(group.before, group.after)
        ):
            return False
        self._prev = marks
        return True


@dataclass
class CiteRun:
    """docx in-walk grouping: directly adjacent cites buffer into one mark;
    consecutive cites of the same paper (same SI flag) collapse to one, even
    across whitespace; an SI cite joins the run and keeps its mark on its
    own number, except next to the same paper cited without the flag (two
    marks, as in LaTeX). Returned groups are ready for
    :meth:`Renderer.cite_group`."""

    _last: tuple[str, bool] | None = None
    _buffer: list[tuple[str, int, bool, int | None]] = field(default_factory=list)
    _before: str = ""
    _after: str = ""

    def add(
        self,
        key: str,
        number: int,
        *,
        si: bool,
        chunk_id: int | None,
        context: tuple[str, str] = ("", ""),
    ) -> list[CiteGroup]:
        """Cite ``key``; return the marks to emit now (possibly none).
        ``context`` is the plain text before and after the reference in its
        block (:func:`mark_context`): the run's mark carries the text before
        its first and after its last reference."""
        if self._last == (key, si):
            self._after = context[1]
            return []
        self._last = (key, si)
        flushed: list[CiteGroup] = []
        if any(k == key and s != si for k, _n, s, _c in self._buffer):
            mark = self.flush()
            flushed = [mark] if mark is not None else []
        if not self._buffer:
            self._before = context[0]
        self._after = context[1]
        self._buffer.append((key, number, si, chunk_id))
        return flushed

    def flush(self) -> CiteGroup | None:
        """End the pending run: one mark for it (a doubled paper once)."""
        run, self._buffer = self._buffer, []
        if not run:
            return None
        return _dedupe(
            CiteGroup(
                keys=tuple(k for k, _n, _s, _c in run),
                numbers=tuple(n for _k, n, _s, _c in run),
                si=tuple(s for _k, _n, s, _c in run),
                chunk_ids=tuple(c for _k, _n, _s, c in run),
                before=self._before,
                after=self._after,
            )
        ).by_number()

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


def has_sections(chunks: list[Any], title_dc: str | None) -> bool:
    """Whether the draft has a section heading besides its title (and the
    generated Glossary). Only then is the prose before the first heading an
    abstract; a note made of paragraphs alone has none."""
    return any(
        c.chunk_kind == "heading"
        and str(c.dc) != title_dc
        and not is_glossary_heading(c)
        for c in chunks
    )


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
    ``equations`` maps a standalone equation's dc to its number. ``order``
    (dc -> reading-order index) and ``section`` (dc -> the numbered section
    the chunk sits in; absent in front and back matter) place a reference
    relative to its citing chunk (:func:`relative_word`). ``types`` maps a
    dc to the cleveref type its ``\\label`` carries (``figure``, ``table``,
    ``equation``, ``section`` / ``subsection`` / ``subsubsection``) for the
    targets whose
    type is known, so a run of references to them can print as cleveref
    would (:func:`format_xref_run`); a list item, a code listing, an aside,
    a run-in heading or a back-matter heading is not in it. ``names`` maps
    a section-resolving target's dc to the heading text the reader finds
    it by — a numbered or run-in heading's own text, any other labelled
    chunk's enclosing numbered heading — the docx stand-in for the PDF's
    ``\\cpageref`` page marker (gr690475; Word cross-references carry no
    page-number field)."""

    labels: dict[str, tuple[str, str]]
    numbered_headings: set[str]
    equations: dict[str, int]
    order: dict[str, int] = field(default_factory=dict)
    section: dict[str, str] = field(default_factory=dict)
    types: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)


#: The cleveref type of a heading by its numbered depth.
XREF_SECTION_TYPES = ("section", "subsection", "subsubsection")

#: Chunk kinds whose ``\\label`` carries a type of its own in LaTeX (a list
#: item's ``enumi``, a listing, an aside box): a reference to one is never
#: part of a merged run, as :func:`format_xref_run` cannot name its type.
XREF_UNTYPED_KINDS = frozenset({"term", "item", "code", "listing", "aside", "box"})


def number_xrefs(
    chunks: list[Any],
    title: str | None,
    *,
    equation_of: Callable[[str | None], tuple[str, bool] | None],
) -> XrefNumbers:
    """Section / figure / table / equation numbers in reading order, the
    way the LaTeX layout counts them: a heading's depth picks section,
    subsection or subsubsection (the seeded title heading is skipped and,
    when the body is nested under it, every depth is lifted one level);
    the draft's own Glossary heading and its terms are skipped; journal
    back matter is unnumbered and a cross-ref to it prints its name; every
    figure steps the figure counter and every table the table counter
    (gr592852 — a table used to borrow its enclosing section's number);
    like ``\\cref`` on a label inside a section, a plain paragraph/item
    resolves to the enclosing numbered section. An unstarred standalone
    equation (``equation_of``, the one detector both backends use) takes
    the next equation number. Reads chunk structure only: no inline text
    is resolved here, so no cite is numbered."""
    labels: dict[str, tuple[str, str]] = {}
    types: dict[str, str] = {}
    shown: set[str] = set()
    counters = [0] * NUMBERED_DEPTHS
    figs = 0
    tabs = 0
    title_dc = title_heading_dc(chunks, title)
    shift = depth_shift(chunks, title_dc)
    back_matter = back_matter_handles(chunks)
    current = ""  # number of the enclosing numbered section
    current_name = ""  # its heading text
    in_back = False  # inside unnumbered journal back matter
    order: dict[str, int] = {}
    section: dict[str, str] = {}
    names: dict[str, str] = {}
    for i, c in enumerate(chunks):
        order[c.dc] = i
        if c.chunk_kind == "heading" and not is_glossary_heading(c):
            if c.dc in back_matter:
                in_back = True
            elif c.dc != title_dc:
                in_back = False
        if current and not in_back and c.chunk_kind not in ("ulist", "olist"):
            section[c.dc] = current  # a numbered heading overwrites below
        if c.chunk_kind == "figure":
            figs += 1
            labels[c.dc] = ("Figure", str(figs))
            types[c.dc] = "figure"
        elif c.chunk_kind == "table":
            tabs += 1
            labels[c.dc] = ("Table", str(tabs))
            types[c.dc] = "table"
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
                current_name = " ".join((c.text or "").split())
                types[c.dc] = XREF_SECTION_TYPES[d]
                names[c.dc] = current_name
            else:
                # run-in paragraph heading — resolves to the enclosing
                # section's number but names itself
                names[c.dc] = " ".join((c.text or "").split())
            labels[c.dc] = ("Section", current)
            if not in_back:
                section[c.dc] = current
        elif c.chunk_kind not in ("ulist", "olist") and current:
            labels[c.dc] = ("Section", current)
            names[c.dc] = current_name
            if c.chunk_kind not in XREF_UNTYPED_KINDS:
                # A \\label in a paragraph takes the type of the sectioning
                # counter last stepped: the enclosing section's depth.
                types[c.dc] = XREF_SECTION_TYPES[current.count(".")]
    equations: dict[str, int] = {}
    for c in chunks:
        if c.chunk_kind in EQ_SKIP_KINDS:
            continue
        eq = equation_of(c.text)
        if eq is None or eq[1]:  # not standalone, or the starred escape hatch
            continue
        equations[c.dc] = len(equations) + 1
    for dc in equations:
        # An equation's ``\label`` binds the equation counter, not the
        # section it sits in: ``\cref`` prints "Eq. (N)", and the reader
        # finds it by that number — no section name / page marker.
        types[dc] = "equation"
        names.pop(dc, None)
    return XrefNumbers(
        labels=labels,
        numbered_headings=shown,
        equations=equations,
        order=order,
        section=section,
        types=types,
        names=names,
    )


@dataclass(frozen=True, slots=True)
class Xref:
    """One intra-draft cross-ref as the walker resolved it. ``live`` False:
    the target is not a live chunk of this draft (render the surface, or the
    neutral ``fallback`` text — never the bare handle). ``surface``: the
    author's link text, which wins over
    any number. ``equation`` / ``label``: what to print (from the
    pre-pass, when the backend prints numbers itself); ``label`` is ``None``
    when there is nothing to print (front matter, glossary, or a section
    reference before the first numbered section). ``relative``: ``"above"``
    / ``"below"`` when the target is the citing chunk's own section or an
    ancestor of it (:func:`relative_word`), printed instead of the number."""

    dc: str
    surface: str | None
    live: bool
    equation: int | None = None
    label: tuple[str, str] | None = None
    relative: str | None = None
    #: Neutral text for a dead target with no authored surface (a retired
    #: chunk prints "a removed figure", never the bare ``dc…`` — gr493181).
    fallback: str | None = None
    #: The cleveref type of the printed number (``XrefNumbers.types``;
    #: ``"equation"`` for an equation) when a run of such references may
    #: merge (:func:`format_xref_run`); ``None``: the reference prints alone.
    kind: str | None = None


#: Placeholder for a dead ``[dc]`` cross-ref whose target is unknown or
#: lives in another draft.
DEAD_XREF_TEXT = "(removed)"

#: Neutral text for a dead cross-ref by the retired target's chunk kind —
#: "a removed figure" reads as prose where the bare handle used to leak.
_REMOVED_XREF_NOUN = {
    "figure": "a removed figure",
    "heading": "a removed section",
    "table": "a removed table",
    "ulist": "a removed list",
    "olist": "a removed list",
    "item": "a removed list item",
}
_REMOVED_XREF_DEFAULT = "a removed passage"


def resolve_xref(
    dc: str,
    surface: str | None,
    *,
    live: Collection[str] | None,
    numbers: XrefNumbers | None,
    warnings: list[str],
    dead: Callable[[str], tuple[str | None, str]] | None = None,
    placement: XrefNumbers | None = None,
    citing: str = "",
) -> Xref:
    """Resolve a ``[dc<id>]`` cross-ref. ``live`` is the draft's live dc
    handles (``None``: unknown, treat as live); a dead target warns and
    downgrades. ``numbers`` is the pre-pass, for a backend that prints the
    number itself (LaTeX's cleveref counts at compile time and passes
    ``None``). ``dead`` maps a dead handle to ``(replacement, text)``: a
    retired target resolves to its recorded live replacement, otherwise the
    xref prints ``text`` — a neutral placeholder, never the bare handle.
    ``placement`` is the pre-pass for the own-section test, which a backend
    that leaves the numbers to the typesetter (LaTeX, ``numbers=None``) still
    supplies; ``citing`` is the dc of the chunk being rendered."""
    if live is not None and dc not in live:
        replacement: str | None = None
        text = DEAD_XREF_TEXT
        if dead is not None:
            try:
                replacement, text = dead(dc)
            except Exception:  # store hiccup — the neutral text stands
                replacement, text = None, DEAD_XREF_TEXT
        if replacement is not None and replacement in live:
            warnings.append(
                f"cross-ref {dc}: retired — resolved to its replacement {replacement}"
            )
            return resolve_xref(
                replacement,
                surface,
                live=live,
                numbers=numbers,
                warnings=warnings,
                placement=placement,
                citing=citing,
            )
        warnings.append(f"cross-ref {dc}: no such live chunk — downgraded")
        return Xref(dc, surface, live=False, fallback=text)
    if surface:
        return Xref(dc, surface, live=True)
    word = relative_word(dc, citing, placement or numbers)
    if word is not None:
        return Xref(dc, None, live=True, relative=word)
    if numbers is None:
        return Xref(dc, surface, live=True)
    eq = numbers.equations.get(dc)
    if eq is not None:
        return Xref(dc, None, live=True, equation=eq, kind="equation")
    label = numbers.labels.get(dc)
    if label is None or not label[1]:
        return Xref(dc, None, live=True)
    return Xref(dc, None, live=True, label=label, kind=numbers.types.get(dc))


def relative_word(dc: str, citing: str, nums: XrefNumbers | None) -> str | None:
    """``"above"`` / ``"below"`` for a reference that resolves to a section
    number (a prose or heading target; not a figure, table or equation) when
    that section is the citing chunk's own innermost section or one of its
    ancestors: "section 3.1" inside 3.1 reads as noise. ``"above"`` when the
    target precedes the citing chunk in reading order, or is it; ``"below"``
    when it follows. ``None`` everywhere else (the number prints as usual).
    The one place both exporters get this decision."""
    if nums is None or not citing:
        return None
    label = nums.labels.get(dc)
    here = nums.section.get(citing)
    if label is None or label[0] != "Section" or not label[1] or not here:
        return None
    if dc in nums.equations or (
        here != label[1] and not here.startswith(label[1] + ".")
    ):
        return None
    if dc not in nums.order or citing not in nums.order:
        return None
    return "above" if nums.order[dc] <= nums.order[citing] else "below"


@dataclass
class XrefCursor:
    """The chunk the walk is rendering, set by :func:`walk_body` so a
    cross-ref deep in a chunk's inline text knows its citing chunk."""

    dc: str = ""


def dead_xref(
    store: Any,
    dc: str,
    live: Collection[str],
    cache: dict[str, tuple[str | None, str]],
) -> tuple[str | None, str]:
    """The ``dead`` callback for :func:`resolve_xref`: for a ``dc`` that is
    not a live chunk of this draft, the dc of its recorded live replacement
    (or ``None``) and the neutral text to print when there is none.
    Memoised per export in ``cache``."""
    if dc not in cache:
        cache[dc] = _dead_xref_lookup(store, dc, live)
    return cache[dc]


def _dead_xref_lookup(
    store: Any, dc: str, live: Collection[str]
) -> tuple[str | None, str]:
    chunk = _chunk_for_handle(store, dc)
    if chunk is None or not getattr(chunk, "retired", False):
        # Never existed, or a live chunk of another draft — either way
        # there is nothing this export can number.
        return None, DEAD_XREF_TEXT
    text = _REMOVED_XREF_NOUN.get(
        getattr(chunk, "chunk_kind", ""), _REMOVED_XREF_DEFAULT
    )
    return _chunk_replacement(store, chunk, live), text


def _chunk_for_handle(store: Any, handle: str) -> Any:
    get = getattr(getattr(store, "drafts", None), "get_draft_chunk", None)
    if not callable(get):
        return None
    try:
        return get(handle)
    except Exception:  # store hiccup — treat as unknown
        return None


def _chunk_replacement(store: Any, chunk: Any, live: Collection[str]) -> str | None:
    """The live dc a retired chunk was replaced by, when the replacement is
    recorded: the chunk's ``meta.superseded_by`` / ``meta.replaced_by``
    (a ``dc`` handle, a bare chunk id, or a legacy ``¶`` handle — the same
    convention ``refs.meta.superseded_by`` uses), else a chunk-level
    ``supersedes`` link (``new --supersedes--> old``)."""
    meta = getattr(chunk, "meta", None) or {}
    for key in ("superseded_by", "replaced_by"):
        cand = _dc_of(store, meta.get(key), live)
        if cand is not None and cand in live:
            return cand
    links_for = getattr(store, "links_for", None)
    chunk_id = getattr(chunk, "chunk_id", None)
    ref_id = getattr(chunk, "ref_id", None)
    if callable(links_for) and chunk_id is not None and ref_id is not None:
        try:
            links = links_for(ref_id, direction="in", relation="supersedes")
        except Exception:  # store hiccup — no redirect
            links = []
        for ln in links or []:
            if getattr(ln, "dst_chunk_id", None) == chunk_id:
                cand = _dc_of(store, getattr(ln, "src_chunk_id", None), live)
                if cand is not None and cand in live:
                    return cand
    return None


def _dc_of(store: Any, raw: Any, live_set: Collection[str]) -> str | None:
    """A successor pointer → its ``dc`` handle: a chunk id, a ``dc<id>``
    handle, or a legacy ``¶`` handle (looked up so its dc can be checked
    against the live set)."""
    if raw is None:
        return None
    if isinstance(raw, int):
        raw = f"dc{raw}"
    s = str(raw).strip()
    if not s:
        return None
    if s.isdigit():
        s = f"dc{s}"
    if s in live_set:  # already a live dc — no lookup needed
        return s
    chunk = _chunk_for_handle(store, s)
    return getattr(chunk, "dc", None) if chunk is not None else None


#: What may sit between two cross-references of one run: nothing, ``,``,
#: ``and`` or ``, and`` (spaces around; a line break is fine, a blank line
#: is a paragraph break and is not). ``or``, ``&``, ``;``, ``to``, a dash
#: or any other word ends the run.
_XREF_CONNECTOR = re.compile(r"(?:\s*|\s*,\s*|\s+and\s+|\s*,\s+and\s+)")


def xref_connector(gap: str) -> bool:
    """Do the words ``gap`` between two references let them join one run?"""
    return _XREF_CONNECTOR.fullmatch(gap) is not None and not re.search(r"\n\s*\n", gap)


def merge_xref_runs(
    items: Sequence[str | tuple[str, ...]],
) -> list[str | tuple[str, ...]]:
    """Cross-refs separated by nothing, whitespace, ``,``, ``and`` or
    ``, and`` (:func:`xref_connector`) become one multi-ref (``\\cref{a,b}``:
    cleveref prints "Sections 1 and 2" and supplies its own conjunctions);
    the connector between them is dropped. ``items`` are prose strings and
    label tuples."""
    out: list[str | tuple[str, ...]] = []
    for item in items:
        if isinstance(item, tuple):
            gap = out[-1] if out else None
            if isinstance(gap, str) and xref_connector(gap) and len(out) >= 2:
                if isinstance(out[-2], tuple):
                    out.pop()
            prev = out[-1] if out else None
            if isinstance(prev, tuple):
                out[-1] = prev + item
                continue
        out.append(item)
    return out


#: cleveref's singular / plural name of each label type: the ``\\crefname``
#: lines of ``data/templates/draft/preamble.tex`` (a test keeps them equal).
XREF_NAMES = {
    "figure": ("Figure", "Figures"),
    "table": ("Table", "Tables"),
    "section": ("Section", "Sections"),
    "subsection": ("Section", "Sections"),
    "subsubsection": ("Section", "Sections"),
    "equation": ("Eq.", "Eqs."),
}

#: The preamble's ``\\Crefname`` lines: the names ``\\Cref`` prints for the
#: first type of a run at the start of a sentence.
XREF_NAMES_CAPITAL = {"equation": ("Equation", "Equations")}


@dataclass(frozen=True, slots=True)
class XrefPart:
    """One piece of a printed reference run: literal ``text``, or the
    ``number`` of target ``dc`` (``noun``: its label word, ``""`` for an
    equation) that a backend prints as a live cross-reference field."""

    text: str = ""
    dc: str = ""
    number: str = ""
    noun: str = ""


def _xref_number(xref: Xref) -> str:
    if xref.equation is not None:
        return str(xref.equation)
    return xref.label[1] if xref.label is not None else ""


def _number_key(number: str) -> tuple[int, ...]:
    return tuple(int(p) if p.isdigit() else 0 for p in number.split("."))


def _follows(prev: tuple[int, ...], key: tuple[int, ...]) -> bool:
    """Does ``key`` repeat ``prev`` or step its last component by one (the
    next section of the same parent, the next figure)?"""
    return key[:-1] == prev[:-1] and key[-1] - prev[-1] in (0, 1)


def _xref_units(group: Sequence[Xref]) -> list[tuple[Xref, Xref]]:
    """One type's references as cleveref's ``sort&compress`` prints them:
    sorted by number; a stretch of three or more references whose numbers
    each repeat or follow the one before (counted per reference, a repeated
    target included) is a range ``(first, last)``, a shorter one lists its
    distinct numbers. Checked against cleveref 2.x in TeX Live 2026."""
    ordered = sorted(group, key=lambda x: _number_key(_xref_number(x)))
    units: list[tuple[Xref, Xref]] = []
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and _follows(
            _number_key(_xref_number(ordered[j])),
            _number_key(_xref_number(ordered[j + 1])),
        ):
            j += 1
        stretch = ordered[i : j + 1]
        if len(stretch) >= 3:
            units.append((stretch[0], stretch[-1]))
        else:
            for x in stretch:
                if not units or _xref_number(units[-1][1]) != _xref_number(x):
                    units.append((x, x))
        i = j + 1
    return units


def _number_parts(x: Xref) -> list[XrefPart]:
    noun = x.label[0] if x.label is not None else ""
    part = XrefPart(dc=x.dc, number=_xref_number(x), noun=noun)
    if x.kind == "equation":
        return [XrefPart(text="("), part, XrefPart(text=")")]
    return [part]


def _conjoin(items: list[list[XrefPart]], pair: str, last: str) -> list[XrefPart]:
    """``items`` joined the cleveref way: ``a``; ``a<pair>b``;
    ``a, b<last>c`` (the separator before the final item is ``last``)."""
    out: list[XrefPart] = []
    for i, item in enumerate(items):
        if i:
            out.append(
                XrefPart(
                    text=pair
                    if len(items) == 2
                    else (last if i == len(items) - 1 else ", ")
                )
            )
        out += item
    return out


def format_xref_run(
    run: Sequence[Xref],
    *,
    sentence_start: bool = False,
    names: Mapping[str, tuple[str, str]] = XREF_NAMES,
    capital_names: Mapping[str, tuple[str, str]] = XREF_NAMES_CAPITAL,
) -> list[XrefPart]:
    """What cleveref prints for ``\\cref{a,b,…}`` over ``run`` (live,
    numbered references in source order; every ``kind`` set), in the
    preamble's names: references group by type, the types in order of
    first appearance, one label word per type (plural when it holds more
    than one number); numbers sort within a type and compress to ``1 to 3``
    (:func:`_xref_units`); a type's numbers read ``1 and 2`` / ``1, 2 and
    4``, the types ``Sections 1 and 2 and Figure 3`` / ``A, B, and C``.
    An equation's number sits in parentheses. ``sentence_start`` is
    ``\\Cref``: the first type's name from ``capital_names``.

    ``names`` / ``capital_names`` override the label words (the Word
    export's per-export label style, ``_Ctx.xref_labels``); the defaults
    are the preamble's ``\\crefname`` / ``\\Crefname`` tables, which the
    LaTeX backend always uses. The pieces come back with their numbers as
    :class:`XrefPart` fields for the backend to print."""
    groups: dict[str, list[Xref]] = {}
    for x in run:
        groups.setdefault(x.kind or "", []).append(x)
    printed: list[list[XrefPart]] = []
    for n, (kind, members) in enumerate(groups.items()):
        units = _xref_units(members)
        many = len(units) > 1 or _xref_number(units[0][0]) != _xref_number(units[0][1])
        kind_names = names[kind]
        if sentence_start and n == 0:
            kind_names = capital_names.get(kind, kind_names)

        rendered: list[list[XrefPart]] = []
        for first, last in units:
            if _xref_number(first) == _xref_number(last):
                rendered.append(_number_parts(first))
            else:
                rendered.append(
                    _number_parts(first) + [XrefPart(text=" to ")] + _number_parts(last)
                )
        printed.append(
            [XrefPart(text=f"{kind_names[1 if many else 0]} ")]
            + _conjoin(rendered, " and ", " and ")
        )
    parts = _conjoin(printed, " and ", ", and ")
    out: list[XrefPart] = []
    for part in parts:  # one text run where pieces of literal text meet
        if out and not part.dc and not out[-1].dc:
            out[-1] = XrefPart(text=out[-1].text + part.text)
        else:
            out.append(part)
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


# ── the body walk ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HeadingInfo:
    """A body heading as the walker placed it: effective ``depth`` (0 =
    section; the title lift applied), the ``number`` it displays (``""``
    when unnumbered or when the backend numbers at compile time), and
    whether it sits in journal back matter (unnumbered, still in the TOC)."""

    depth: int
    number: str
    back_matter: bool


def named_headings(
    chunks: list[Any], depth_of: Callable[[Any], int], back_matter: frozenset[str]
) -> frozenset[str]:
    """Headings that print no number — run-in headings deeper than the
    numbered depths, and back matter — so a cross-ref to one names it."""
    return (
        frozenset(
            c.dc
            for c in chunks
            if c.chunk_kind == "heading" and depth_of(c) >= NUMBERED_DEPTHS
        )
        | back_matter
    )


def list_kind_level(
    chunk: Any, kind_by_id: dict[Any, str], parent_by_id: dict[Any, Any]
) -> tuple[str, int]:
    """An item's list ``(kind, level)``: the kind (``ulist``/``olist``) of
    its nearest list container (``ulist`` with none) and how many list
    containers enclose it."""
    kind, level = "ulist", 0
    pid = getattr(chunk, "parent_chunk_id", None)
    while pid is not None:
        pk = kind_by_id.get(pid)
        if pk in ("ulist", "olist"):
            if level == 0:
                kind = pk
            level += 1
        pid = parent_by_id.get(pid)
    return kind, level


def walk_body(
    chunks: list[Any],
    renderer: Any,
    sink: Any,
    *,
    title_dc: str | None,
    depth_of: Callable[[Any], int],
    back_matter: frozenset[str],
    equation_of: Callable[[str | None], tuple[str, bool] | None],
    numbers: XrefNumbers | None = None,
    on_chunk: Callable[[int], None] | None = None,
    cursor: XrefCursor | None = None,
) -> None:
    """Visit the draft body once, in reading order, and hand each chunk to
    the renderer as the method its kind calls for; the renderer only
    formats. The walker decides: the seeded title chunk (``title``), the
    draft's own Glossary heading and term chunks (``anchor``: they feed the
    generated acronym list), list containers opened and closed by depth and
    each item's ``(kind, level)`` from its ancestors, a heading's depth /
    number / back-matter flag, and which paragraph is a display equation
    (``equation_of``) with its pre-pass number. ``renderer.code_kinds`` and
    ``renderer.aside_kinds`` name the chunk kinds a backend has a block for
    (docx renders a listing and an aside as an ordinary paragraph: a known
    divergence, kept byte-identical). ``on_chunk`` reports progress;
    ``cursor`` follows the chunk being rendered."""
    # Chunk ids and parents only matter for list items; a caller's lighter
    # chunk shape (no ids) still walks.
    kind_by_id = {getattr(c, "chunk_id", None): c.chunk_kind for c in chunks}
    parent_by_id = {
        getattr(c, "chunk_id", None): getattr(c, "parent_chunk_id", None)
        for c in chunks
    }
    # Open list containers (migration 0037): a container opens; its `item`
    # children follow; it closes once we reach a chunk at or above the
    # container's own depth (i.e. we've left its subtree). The stack
    # handles nested lists.
    open_lists: list[tuple[str, int]] = []
    if title_dc is None:
        # No seeded title chunk opens the draft: the title comes first.
        renderer.title(sink, None)
    for n_done, c in enumerate(chunks):
        if on_chunk is not None:
            on_chunk(n_done)
        if cursor is not None:
            cursor.dc = c.dc
        depth = depth_of(c)
        while open_lists and depth <= open_lists[-1][1]:
            renderer.list_close(sink, open_lists.pop()[0])
        kind = c.chunk_kind
        if c.dc == title_dc:
            renderer.title(sink, c)
        elif kind == "term" or is_glossary_heading(c):
            renderer.anchor(sink, c)
        elif kind in ("ulist", "olist"):
            renderer.list_open(sink, c, kind, len(open_lists) + 1)
            open_lists.append((kind, depth))
        elif kind == "item":
            lkind, level = list_kind_level(c, kind_by_id, parent_by_id)
            renderer.list_item(sink, c, lkind, level)
        elif kind == "table":
            renderer.table(sink, c)
        elif kind == "figure":
            renderer.figure(sink, c)
        elif kind == "heading":
            number = ""
            if numbers is not None and c.dc in numbers.numbered_headings:
                number = numbers.labels[c.dc][1]
            renderer.heading(sink, c, HeadingInfo(depth, number, c.dc in back_matter))
        elif kind in renderer.code_kinds:
            renderer.code(sink, c)
        elif kind in renderer.aside_kinds:
            renderer.aside(sink, c)
        else:
            eq = equation_of(c.text)
            if eq is None:
                renderer.paragraph(sink, c)
            else:
                number_of = numbers.equations.get(c.dc) if numbers else None
                renderer.equation(sink, c, eq[0], number_of, eq[1])
    while open_lists:  # close any lists still open at the end
        renderer.list_close(sink, open_lists.pop()[0])


# ── legacy handles and poster sections ───────────────────────────


def legacy_map(chunks: list[Any]) -> dict[str, str]:
    """Legacy base-58 chunk handle (``[¶<handle>]``) → its ``dc`` handle."""
    return {c.handle: c.dc for c in chunks}


def resolve_legacy(
    handle: str,
    legacy: dict[str, str],
    *,
    accept_dc: bool,
    known: Collection[str] | None,
    warnings: list[str],
) -> str | None:
    """A legacy ``[¶<handle>]`` cross-ref → the ``dc`` it points at, or
    ``None`` (the caller downgrades it to text). ``accept_dc``: a
    ``[¶dc<id>]`` handle is itself the target (the ¶ is just the xref
    sigil; docx reads it so, LaTeX does not — a known divergence, kept
    byte-identical). An unresolved handle warns unless it is in ``known``
    (``None``: always warn)."""
    dc = legacy.get(handle)
    if dc is None and accept_dc:
        parsed = handle_registry.parse(handle)
        if parsed is not None and parsed[0] == "draft" and parsed[1]:
            dc = handle
    if dc is None and (known is None or handle not in known):
        warnings.append(f"cross-ref ¶{handle}: no such live chunk — downgraded")
    return dc


def poster_sections(
    chunks: list[Any],
    depth_of: Callable[[Any], int],
    title_dc: str | None,
) -> list[tuple[str, int | None, list[Any]]]:
    """Group reading order into poster sections: each effective-depth-0
    heading opens a new section (its text becomes the ``block`` title);
    everything under it is a member. Chunks before the first section
    heading form one anonymous leading block. Returns
    ``(title, poster_column_hint, members)`` triples."""
    sections: list[tuple[str, int | None, list[Any]]] = []
    cur_title = ""
    cur_col: int | None = None
    cur: list[Any] = []
    started = False

    def flush() -> None:
        nonlocal cur_title, cur_col, cur, started
        if started or cur:
            sections.append((cur_title, cur_col, cur))
        cur_title, cur_col, cur, started = "", None, [], False

    for c in chunks:
        if c.dc == title_dc:
            continue
        if c.chunk_kind == "heading" and depth_of(c) == 0:
            flush()
            started = True
            cur_title = c.text or ""
            meta = getattr(c, "meta", None) or {}
            raw_col = meta.get("poster_column")
            try:
                cur_col = int(raw_col) if raw_col is not None else None
            except (TypeError, ValueError):
                cur_col = None
        else:
            cur.append(c)
    flush()
    return sections


_EXT_BY_MIME_LOGO: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/tiff": "tiff",
    "image/bmp": "bmp",
    "application/pdf": "pdf",
}


def figure_logo_asset(store: Any, figure_id: str) -> tuple[bytes, str] | None:
    """``(bytes, ext)`` for a strip logo — a ``kind='figure'`` ref
    resolved by slug (or numeric id): its SVG canvas rasterises to PNG
    (same ``resvg`` path as figure export); a chunk blob passes through.
    ``None`` when the ref is absent or carries no usable asset."""
    fid: int | str = figure_id
    if str(figure_id).isdigit():
        fid = int(figure_id)
    try:
        ref = store.get_ref(kind="figure", id=fid)
    except Exception:
        return None
    if ref is None:
        return None
    from precis.utils.figure_source import _svg_to_png

    for c in store.drafts.reading_order(ref.id, kind="figure"):
        if c.chunk_kind == "figure_node" and (c.text or "").strip():
            png = _svg_to_png(c.text)
            return (png, "png") if png else None
        blob = store.drafts.get_chunk_blob(c.handle)
        if blob is not None:
            data, mime = blob
            mime = (mime or "").split(";", 1)[0].strip()
            if mime == "image/svg+xml":
                png = _svg_to_png(data.decode("utf-8", "replace"))
                return (png, "png") if png else None
            ext = _EXT_BY_MIME_LOGO.get(mime)
            if ext:
                return data, ext
    return None
