"""LaTeX export for the ``draft`` kind — ADR 0033 Tier-B.

A draft lives as ordered ``chunks`` in Postgres (the canonical, editable
form). *Export* is a one-way resolution pass that renders those chunks
into a compilable LaTeX project; the output is **disposable, not
editable** (you re-export from the draft, you never hand-edit the .tex).
That assumption is what lets us stamp machine labels on everything:

* every block gets ``\\label{chunk:<handle>}``; an intra-draft ``[¶h]``
  cross-ref becomes ``\\cref{chunk:h}`` — cross-references resolve
  automatically;
* ``[§slug~n]`` / bare ``paper:slug~n`` citations become ``\\cite{slug}``
  and a ``refs.bib`` is generated from the cited paper refs (DOI/arXiv
  included when the corpus knows them);
* every defined abbreviation becomes a ``\\newacronym`` and each surface
  occurrence becomes a glossary call — so first use expands to the full
  term and every later use is the abbreviation, automatically, with the
  page-number "where it occurs" list in the glossary. Later uses are also
  wrapped in a non-printing ``\\pdftooltip`` so hovering the short reveals
  the full term on screen (the PDF analogue of the web reader's popup).

This module produces the *project files* (``main.tex`` + ``refs.bib`` +
the copied ``preamble.tex``). Compiling them (latexmk + biber +
makeglossaries) and the post-compile LLM repair loop are a separate
increment; so is the Word/pandoc path.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from pylatexenc.latexencode import UnicodeToLatexEncoder

from precis.export._patent_cite import format_patent_citation, paper_inline_citation
from precis.utils import handle_registry, mentions
from precis.utils.authors import build_byline
from precis.utils.draft_markup import DRAFT_CITE_PATTERN
from precis.utils.workspace import Workspace

#: ``meta.workspace.doc_type`` value that switches export into patent-spec
#: mode: prior-art rendered in-text, no ``\cite`` / no bibliography.
_PATENT_DOC_TYPE = "patent"

#: Translate non-ASCII glyphs to LaTeX commands. ``non_ascii_only`` leaves
#: ASCII (including the backslash escapes we emit) untouched, so it's safe
#: to run over already-escaped prose; ``keep`` leaves the rare glyph with
#: no known representation verbatim rather than raising.
_U2L = UnicodeToLatexEncoder(non_ascii_only=True, unknown_char_policy="keep")

# ── inline grammar (shared atoms; mirrors precis_web.linkify) ──────────
# The same superset the reader highlights: bracket/sigil forms ∪ bare
# ``kind:ref`` mentions. Built from the single-sourced atoms in
# ``mentions`` so the exporter can never drift from the parser/linkifier.
_COMBINED = re.compile(
    mentions.AUTHORING_PATTERN.pattern
    + "|"
    + mentions.DISPLAY_LINK_PATTERN.pattern
    + "|"
    + mentions.BARE_BRACKET_REF_PATTERN.pattern
    + "|"
    + r"(?P<ref>"
    + mentions.REF_PATTERN.pattern
    + r")"
    + "|"
    + r"(?P<bare_conv>"
    + mentions.BARE_CONV_PATTERN.pattern
    + r")"
    + "|"
    + r"(?P<bare_paper>"
    + mentions.BARE_PAPER_PATTERN.pattern
    + r")"
)

#: LaTeX special characters → their escaped forms (text mode). Applied to
#: plain prose only — never to math (``$…$``), code, or the markup we
#: emit ourselves.
_LATEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_LATEX_SPECIALS_RE = re.compile("|".join(re.escape(k) for k in _LATEX_SPECIALS))

_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_CODE = re.compile(r"`([^`]+)`")
_HTML_SUB = re.compile(r"<sub>(.+?)</sub>")
_HTML_SUP = re.compile(r"<sup>(.+?)</sup>")
#: ``$$…$$`` (display) before ``$…$`` (inline); both stashed verbatim.
_MATH = re.compile(r"\$\$.+?\$\$|\$[^$]+\$", re.DOTALL)


# ── shared draft-text normalisation (both exporters) ──────────────────
# Drafts may carry verbatim LaTeX a precis author wouldn't write — most
# often when an LLM drafts in LaTeX rather than precis markup. These two
# fixes run BEFORE the reference/prose walk so both exporters (this one and
# ``export/docx``) treat the input identically and never drift.

#: ``\cite{a,b}`` / ``\citep[p.5]{a}`` / ``\citet*{a}`` — LaTeX citation
#: commands. Folded to the explicit ``[§key]`` bracket form, which the
#: grammar already resolves (for any key length, unlike a bare key — the
#: bare-cite pattern needs ≥3 surname letters to stay off prose). So each
#: key becomes a single resolved ``\cite{key}`` / ``[n]`` mark instead of
#: leaking its ``\cite{…}`` wrapper as literal escaped text.
_LATEX_CITE = re.compile(r"\\cite[a-z]*\*?(?:\[[^\]]*\])*\{([^}]*)\}")

#: A ``$…$`` whose body starts with ``_`` or ``^`` has an EMPTY base — the
#: author put the base outside the math (chemistry style: ``Zr$_6$``,
#: ``UO$_2^{2+}$``). That renders as a floating subscript in LaTeX and an
#: empty-box placeholder (``<m:e/>``) in Word. Pull the adjacent preceding
#: token into the math so the base is non-empty (``Zr$_6$`` → ``$Zr_6$``).
#: The token may sit right after a closing ``$`` (``$W_{18}$O$_{49}$`` → the
#: ``O`` base), so the lookbehind only forbids a word char, not ``$``.
_EMPTY_BASE_MATH = re.compile(r"(?<!\w)([A-Za-z0-9)\]]+)\$([_^][^$]+)\$")


def _fold_cite(m: re.Match[str]) -> str:
    keys = [k.strip() for k in m.group(1).split(",") if k.strip()]
    return "".join(f"[§{k}]" for k in keys)


def preprocess_draft_inline(text: str) -> str:
    """Normalise a chunk's raw text before the reference/prose walk: fold
    LaTeX ``\\cite`` commands to ``[§key]`` citations, and repair empty-base
    ``$…$`` math (see ``_LATEX_CITE`` / ``_EMPTY_BASE_MATH``). Shared by both
    the LaTeX and docx exporters so they handle verbatim LaTeX identically."""
    text = _LATEX_CITE.sub(_fold_cite, text)
    text = _EMPTY_BASE_MATH.sub(r"$\1\2$", text)
    return text


@dataclass
class RenderResult:
    """The assembled body plus what it referenced."""

    body: str
    cited_slugs: list[str] = field(default_factory=list)
    acronyms: dict[str, str] = field(default_factory=dict)  # short → long
    acronym_keys: dict[str, str] = field(default_factory=dict)  # short → gls key
    warnings: list[str] = field(default_factory=list)
    #: figure assets to materialise beside main.tex — (relpath, bytes). ADR 0058
    #: slice 4: raster blobs pass through, SVG/canvas figures rasterise to PNG.
    figures: list[tuple[str, bytes]] = field(default_factory=list)


@dataclass
class ExportResult:
    """Paths written + diagnostics."""

    main_tex: Path
    bib: Path
    preamble: Path
    latexmkrc: Path
    cited_slugs: list[str]
    acronyms: dict[str, str]
    warnings: list[str]
    #: Set only when ``export_draft(include_sources=True)`` — the resolved
    #: cited-source bundle whose present PDFs were copied into ``sources/``
    #: and appended as a ``pdfpages`` appendix. ``None`` otherwise.
    source_bundle: Any | None = None


def _latex_escape(text: str) -> str:
    """Escape LaTeX specials in a run of plain prose."""
    return _LATEX_SPECIALS_RE.sub(lambda m: _LATEX_SPECIALS[m.group(0)], text)


def _acronym_key(short: str) -> str:
    """A bare ``\\newacronym`` key from an abbreviation's short form.

    Lowercased, non-alphanumerics dropped, ``a`` prefix guards a
    digit-leading short (``3d`` → ``a3d``) so the key is a valid LaTeX
    control-sequence argument. Collisions are resolved later by
    :func:`_acronym_keymap`, which owns the final short→key mapping."""
    key = re.sub(r"[^a-z0-9]", "", short.lower())
    if not key:
        key = "x"
    if key[0].isdigit():
        key = "a" + key
    return key


def _acronym_keymap(abbrevs: dict[str, str]) -> dict[str, str]:
    """Deterministic ``{short: glossary-key}`` map with **collision
    resolution** — two distinct shorts that sanitise to the same key
    (``PEI`` and ``P.E.I.`` → ``pei``) would otherwise emit a duplicate
    ``\\newacronym`` and a fatal LaTeX error. Sorted iteration + numeric
    suffix on collision keeps the mapping stable across runs."""
    out: dict[str, str] = {}
    used: set[str] = set()
    for short in sorted(abbrevs):
        base = _acronym_key(short)
        key = base
        n = 2
        while key in used:
            key = f"{base}{n}"
            n += 1
        used.add(key)
        out[short] = key
    return out


#: Unicode subscript / superscript digits + signs → the literal chars
#: they stand for. pylatexenc has no mapping for these (it would ``keep``
#: them verbatim, and a literal ``₂`` is a hard ``inputenc`` error under
#: pdflatex — the MoS₂ / CO₂ class), so we transliterate runs of them to
#: ``\textsubscript{…}`` / ``\textsuperscript{…}`` *before* pylatexenc.
_SUBSCRIPT: dict[int, str] = {0x2080 + i: str(i) for i in range(10)}
_SUBSCRIPT.update({0x208A: "+", 0x208B: "-", 0x208C: "=", 0x208D: "(", 0x208E: ")"})
_SUPERSCRIPT: dict[int, str] = {0x2070: "0", 0x00B9: "1", 0x00B2: "2", 0x00B3: "3"}
_SUPERSCRIPT.update({0x2074 + i: str(i + 4) for i in range(6)})  # ⁴..⁹
_SUPERSCRIPT.update(
    {0x207A: "+", 0x207B: "-", 0x207C: "=", 0x207D: "(", 0x207E: ")", 0x207F: "n"}
)
_SUB_RUN = re.compile("[" + "".join(map(chr, _SUBSCRIPT)) + "]+")
_SUP_RUN = re.compile("[" + "".join(map(chr, _SUPERSCRIPT)) + "]+")


def _normalize_subsup(s: str) -> str:
    """Collapse runs of Unicode sub/superscript characters into a single
    ``\\textsubscript{…}`` / ``\\textsuperscript{…}`` (so ``MoS₂`` →
    ``MoS\\textsubscript{2}`` and ``10⁻³`` → ``10\\textsuperscript{-3}``).
    Runs into one command so ``₁₀`` is one subscript, not two boxes.
    Runs on already-LaTeX-escaped text — the braces it introduces are
    real LaTeX grouping, deliberately not re-escaped."""
    s = _SUB_RUN.sub(
        lambda m: (
            r"\textsubscript{" + "".join(_SUBSCRIPT[ord(c)] for c in m.group(0)) + "}"
        ),
        s,
    )
    return _SUP_RUN.sub(
        lambda m: (
            r"\textsuperscript{"
            + "".join(_SUPERSCRIPT[ord(c)] for c in m.group(0))
            + "}"
        ),
        s,
    )


def _encode_unicode(escaped: str) -> str:
    """Translate non-ASCII characters to LaTeX commands (``≈`` →
    ``\\approx``, ``α`` → ``\\alpha``), leaving ASCII — including the
    backslash escapes we just emitted — untouched (``non_ascii_only``).
    Sub/superscript digits are transliterated first (pylatexenc has no
    mapping and would pass them through verbatim). The single biggest
    determinism lever: the engine never hits a "missing character" on
    arbitrary scientific prose. Unknown glyphs are kept verbatim — under
    lualatex (the export engine) that's a recoverable missing-glyph
    warning, not a fatal error (the compile-repair loop is the backstop)."""
    return _U2L.unicode_to_latex(_normalize_subsup(escaped))


def _glsify(escaped: str, keymap: dict[str, str], seen: set[str] | None = None) -> str:
    """Replace whole-word occurrences of each known abbreviation short
    with a glossary call (longest-first, word-bounded). Runs on
    already-escaped prose; shorts are alphanumerics so escaping never
    touched them.

    A trailing plural ``s`` is absorbed and rendered with the plural form
    — so a defined ``MOF`` term links *both* ``MOF`` and ``MOFs`` to the one
    glossary entry, rather than leaving the plural un-linked. Longest-first
    ordering means a literal short ``As`` still wins over treating ``A`` +
    plural ``s``. (Irregular plurals still need the explicit form; ``s``
    covers the overwhelming majority.)

    First-use vs. later-use split (``seen`` threads the set of already-seen
    keys across the whole render, so document order == glossaries' first-use
    order): the *first* occurrence renders as a plain ``\\gls`` / ``\\glspl``
    so the glossaries package expands it inline (full term + short). Every
    *later* occurrence renders as ``\\glstip`` / ``\\glspltip`` — the bare
    short wrapped in a non-printing ``\\pdftooltip`` that reveals the full
    term on hover (the PDF analogue of the web reader's abbreviation popup).
    Only later uses are wrapped because a pdftooltip box can't break across a
    line and the first-use expansion is multi-word. When ``seen`` is omitted
    every occurrence renders plain (used by isolated unit tests)."""
    if not keymap:
        return escaped
    shorts = sorted((s for s in keymap if s), key=len, reverse=True)
    pat = re.compile(
        r"(?<![\w-])(" + "|".join(re.escape(s) for s in shorts) + r")(s)?(?![\w-])"
    )

    def _sub(m: re.Match[str]) -> str:
        key = keymap[m.group(1)]
        plural = m.group(2)
        if seen is None or key not in seen:
            if seen is not None:
                seen.add(key)
            return f"\\glspl{{{key}}}" if plural else f"\\gls{{{key}}}"
        return f"\\glspltip{{{key}}}" if plural else f"\\glstip{{{key}}}"

    return pat.sub(_sub, escaped)


@dataclass
class _Ctx:
    """Per-export render state threaded through the inline pass."""

    keymap: dict[str, str]  # abbreviation short → glossary key
    known_handles: set[str]  # every live dc<id> chunk handle in the draft
    store: Any = None  # to resolve a paper handle (pc/pa) → cite_key
    legacy_to_dc: dict[str, str] = field(default_factory=dict)  # ¶base58 → dc
    cited: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    seen_acr: set[str] = field(default_factory=set)  # glossary keys already emitted
    figures: list[tuple[str, bytes]] = field(default_factory=list)  # (relpath, bytes)
    doc_type: str = ""  # meta.workspace.doc_type; "patent" → in-text cites, no bib

    @property
    def patent_mode(self) -> bool:
        return self.doc_type == _PATENT_DOC_TYPE


def _render_gap(text: str, ctx: _Ctx) -> str:
    """Render a non-reference run of prose to LaTeX: math + code stashed
    verbatim, sub/sup → ``\\textsubscript`` / ``\\textsuperscript``,
    ``**bold**`` → ``\\textbf``, ``\\gls`` for known abbreviations,
    non-ASCII → LaTeX commands, the rest LaTeX-escaped."""
    if not text:
        return ""
    stash: list[str] = []

    def _stash(rendered: str) -> str:
        stash.append(rendered)
        return f"\x00{len(stash) - 1}\x00"

    # 1. Math verbatim (keeps _ ^ \ and unicode intact for KaTeX/LaTeX).
    s = _MATH.sub(lambda m: _stash(m.group(0)), text)
    # 2. Inline code → \texttt with its content escaped.
    s = _MD_CODE.sub(lambda m: _stash(f"\\texttt{{{_latex_escape(m.group(1))}}}"), s)
    # 3. sub/sup BEFORE escaping (the angle brackets must not be escaped).
    s = _HTML_SUB.sub(
        lambda m: _stash(f"\\textsubscript{{{_latex_escape(m.group(1))}}}"), s
    )
    s = _HTML_SUP.sub(
        lambda m: _stash(f"\\textsuperscript{{{_latex_escape(m.group(1))}}}"), s
    )
    # 4. Escape the remaining prose, then translate any non-ASCII glyphs.
    s = _encode_unicode(_latex_escape(s))
    # 5. Bold (the ** survived escaping — * is not a LaTeX special).
    s = _MD_BOLD.sub(r"\\textbf{\1}", s)
    # 6. Abbreviations → \gls (first use) / \glstip tooltip (later uses).
    s = _glsify(s, ctx.keymap, ctx.seen_acr)
    # 7. Restore the stashed verbatim spans.
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], s)


def _render_reference(m: re.Match[str], ctx: _Ctx) -> str:
    """Render one matched inline reference to LaTeX. Citations collect
    their slug into ``ctx.cited``. Authoring links / bare thought mentions
    render to nothing (provenance only, never a citation)."""
    if m.group("auth") is not None:
        return ""  # [[…]] authoring link — provenance only
    if m.group("disp") is not None:
        return _render_target(m.group("tgt"), m.group("disp"), ctx)
    if m.group("bare") is not None:
        return _render_target(m.group("bare"), None, ctx)
    if m.group("ref") is not None:
        kind, raw_id = m.group("kind"), m.group("id")
        if kind == "paper":
            return _cite(raw_id, ctx)
        return ""  # bare memory:/think:/… — not citeable, drop
    if m.group("bare_conv") is not None:
        return ""
    if m.group("bare_paper") is not None:
        slug = m.group("bare_paper").split("~", 1)[0]
        return _cite(slug, ctx)
    return ""


def _draft_xref(dc: str, surface: str | None, ctx: _Ctx) -> str:
    """An intra-draft chunk cross-ref (``dc<id>``) → ``\\cref`` / hyperref,
    downgraded to text + a warning when the chunk isn't live in this draft."""
    if dc not in ctx.known_handles:
        ctx.warnings.append(f"cross-ref {dc}: no such live chunk — downgraded")
        return _encode_unicode(_latex_escape(surface or dc))
    if surface:
        return f"\\hyperref[chunk:{dc}]{{{_encode_unicode(_latex_escape(surface))}}}"
    return f"\\cref{{chunk:{dc}}}"


def _handle_cite_key(tgt: str, ctx: _Ctx) -> str | None:
    """A paper handle (``pc<chunk_id>`` / ``pa<ref_id>``) → its cite_key, via
    the one resolver. ``None`` if it doesn't resolve to a live paper."""
    if ctx.store is None:
        return None
    try:
        resolved = ctx.store.resolve_handle(tgt)
    except Exception:  # pragma: no cover — store hiccup
        return None
    return resolved.public_id if resolved is not None else None


def _finding_cite_key(tgt: str, ctx: _Ctx) -> str | None:
    """A finding handle (``fi<id>``) → its bibliographic key: the primary
    cite_key once the chase establishes it (so it merges with a direct
    cite of that paper and ``build_bib`` renders a real entry), else the
    ``pub_id`` placeholder (an in-flight finding gets a stub bib entry
    until it resolves). ``None`` if it doesn't resolve to a live finding."""
    if ctx.store is None:
        return None
    parsed = handle_registry.parse(tgt)
    if parsed is None:
        return None
    _kind, _is_chunk, pk = parsed
    ref = ctx.store.fetch_refs_by_ids([pk]).get(pk)
    if ref is None:
        return None
    meta = ref.meta or {}
    key = meta.get("primary_cite_key") or meta.get("pub_id")
    return str(key) if key else None


def _render_target(tgt: str, surface: str | None, ctx: _Ctx) -> str:
    """Render a bracket reference target (``dc<id>`` / ``pc<id>`` / ``§slug~n``
    / legacy ``¶h`` / URL).

    A cross-ref whose handle isn't a live chunk in this draft is
    **downgraded** to its surface text (or the literal handle) + a
    warning — never a dangling ``\\cref`` (which would compile to a ``??``
    and break determinism / linkcheck)."""
    # ADR 0036 universal handle: ``[dc41]`` (this draft) → cross-ref;
    # ``[pc10]`` / ``[pa5]`` (a paper) → a citation; a record handle for a
    # thought (``[me5]``) is provenance-only → dropped.
    parsed = handle_registry.parse(tgt)
    if parsed is not None:
        kind, is_chunk, _pk = parsed
        if kind in ("paper", "patent"):
            if ctx.patent_mode:
                return _inline_source_cite(tgt, kind, surface, ctx)
            slug = _handle_cite_key(tgt, ctx)
            return _cite(slug, ctx) if slug else ""
        if kind == "finding":
            slug = _finding_cite_key(tgt, ctx)
            return _cite(slug, ctx) if slug else ""
        if kind == "draft" and is_chunk:
            return _draft_xref(tgt, surface, ctx)
        return ""  # other record/chunk handle — provenance only
    if tgt.startswith("¶"):  # legacy base-58 chunk handle
        dc = ctx.legacy_to_dc.get(tgt[1:])
        if dc is None:
            ctx.warnings.append(f"cross-ref {tgt}: no such live chunk — downgraded")
            return _encode_unicode(_latex_escape(surface or tgt))
        return _draft_xref(dc, surface, ctx)
    if tgt.startswith("§"):
        cm = DRAFT_CITE_PATTERN.fullmatch(tgt)
        if cm is not None:
            return _cite(cm.group("slug"), ctx)
        return ""
    if tgt.startswith(("http://", "https://")):
        if surface:
            return f"\\href{{{tgt}}}{{{_encode_unicode(_latex_escape(surface))}}}"
        return f"\\url{{{tgt}}}"
    # ADR 0036 single-bracket handles (dc/pc/pa) are handled above via
    # handle_registry.parse(); anything else is provenance-only.
    return ""  # other authoring targets — provenance only


def _inline_source_cite(tgt: str, kind: str, surface: str | None, ctx: _Ctx) -> str:
    """Patent-spec mode: render a prior-art reference **in-text** (no
    ``\\cite`` / no bibliography). A display-link's authored text wins
    (WYSIWYG — what you proofread is what compiles); otherwise a patent
    formats to its citation string ("U.S. Patent No. …") and a paper to a
    light ``(Author, Year)``."""
    if surface:
        return _encode_unicode(_latex_escape(surface))
    slug = _handle_cite_key(tgt, ctx)
    if not slug or ctx.store is None:
        return ""
    ref = ctx.store.get_ref(kind=kind, id=slug)
    if ref is None:
        return _encode_unicode(_latex_escape(slug))
    if kind == "patent":
        text = format_patent_citation(getattr(ref, "meta", None), slug)
    else:
        text = paper_inline_citation(ref)
    return _encode_unicode(_latex_escape(text))


def _inline_paper_by_slug(slug: str, ctx: _Ctx) -> str:
    """Patent-spec mode for a bare paper cite (``§slug`` / ``paper:slug`` /
    a raw cite_key) that arrives without a handle: resolve the slug and
    render its in-text ``(Author, Year)`` instead of ``\\cite``."""
    if ctx.store is None:
        return _encode_unicode(_latex_escape(slug))
    ref = ctx.store.get_ref(kind="paper", id=slug)
    text = paper_inline_citation(ref) if ref is not None else slug
    return _encode_unicode(_latex_escape(text))


def _cite(slug: str, ctx: _Ctx) -> str:
    # Cite the PAPER, not the chunk: ``a~3`` / ``a~9`` → one \cite{a} and
    # one bib entry (biblatex collapses repeated cites; build_bib resolves
    # the bare slug).
    slug = slug.split("~", 1)[0]
    if ctx.patent_mode:
        # A patent specification has no bibliography — every source is
        # cited in the running text.
        return _inline_paper_by_slug(slug, ctx)
    if slug not in ctx.cited:
        ctx.cited.append(slug)
    return f"\\cite{{{slug}}}"


#: A run of directly-adjacent ``\cite{…}`` (no separator) → one grouped
#: ``\cite{a,b}`` so biblatex prints a single ``[1, 2]`` bracket rather than
#: ``[1][2]``. The adjacency comes from folding a multi-key ``\cite{a,b}``
#: through the one-key-per-bracket grammar; cites the author spaced apart
#: keep their separator and are left alone.
_ADJ_CITES = re.compile(r"(?:\\cite\{[^}]*\})+")


def _merge_adjacent_cites(s: str) -> str:
    def repl(m: re.Match[str]) -> str:
        keys = re.findall(r"\\cite\{([^}]*)\}", m.group(0))
        return "\\cite{" + ",".join(keys) + "}"

    return _ADJ_CITES.sub(repl, s)


def _render_inline(text: str, ctx: _Ctx) -> str:
    """Render a chunk's text: walk references (rendered as LaTeX markup),
    LaTeX-escape + markdownify the gaps between them. Single pass, mirrors
    the web linkifier so the two never diverge."""
    text = preprocess_draft_inline(text)
    out: list[str] = []
    last = 0
    for m in _COMBINED.finditer(text):
        out.append(_render_gap(text[last : m.start()], ctx))
        out.append(_render_reference(m, ctx))
        last = m.end()
    out.append(_render_gap(text[last:], ctx))
    return _merge_adjacent_cites("".join(out))


def _render_table(chunk: Any, ctx: _Ctx, label: str) -> list[str]:
    """Render a ``chunk_kind='table'`` chunk as a ``longtable`` (ADR 0035
    §1) — page-breaking, booktabs-ruled, equal-width ``p{}`` columns so long
    cells wrap and the table never overflows the text width. Cells go through
    the same inline grammar as prose (citations / math / abbreviations
    resolve in-cell). Falls back to a plain paragraph if no table is
    recoverable. The optional legend renders as a bold lead-in line."""
    from precis.utils.table_data import table_payload

    payload = table_payload(getattr(chunk, "meta", None), chunk.text)
    if payload is None:
        return [f"{_render_inline(chunk.text or '', ctx)}{label}"]
    header, rows, caption = payload["header"], payload["rows"], payload["caption"]
    n = max(1, len(header))
    width = f"\\dimexpr(\\linewidth-\\tabcolsep*{2 * n})/{n}\\relax"
    colspec = "".join(
        f">{{\\raggedright\\arraybackslash}}p{{{width}}}" for _ in range(n)
    )
    out: list[str] = []
    if caption:
        out.append(
            f"\\noindent\\textbf{{{_render_inline(caption, ctx)}}}\\par\\nopagebreak"
        )
    out.append(f"\\begin{{longtable}}{{{colspec}}}")
    out.append("\\toprule")
    out.append(" & ".join(_render_inline(h, ctx) for h in header) + r" \\")
    out.append("\\midrule\\endhead")
    out.append("\\bottomrule\\endlastfoot")
    for row in rows:
        cells = [_render_inline(row[j], ctx) if j < len(row) else "" for j in range(n)]
        out.append(" & ".join(cells) + r" \\")
    out.append("\\end{longtable}")
    out.append(label)
    return out


#: chunk depth → sectioning command. Deeper than subsubsection collapses
#: to a run-in paragraph heading.
_SECTION_CMD = ["section", "subsection", "subsubsection", "paragraph"]


def render_body(store: Any, ref: Any, *, doc_type: str = "") -> RenderResult:
    """Render the whole draft body to LaTeX (no preamble/title chrome).

    ``doc_type='patent'`` renders prior-art references in-text (no
    ``\\cite`` / no bibliography) — see ``docs/design/patent-authoring-loop.md``."""
    chunks = store.reading_order(ref.id)
    abbrevs: dict[str, str] = store.defined_abbrevs(ref.id)
    ctx = _Ctx(
        keymap=_acronym_keymap(abbrevs),
        known_handles={c.dc for c in chunks},
        store=store,
        legacy_to_dc={c.handle: c.dc for c in chunks},
        doc_type=doc_type,
    )
    lines: list[str] = []
    # Open list environments (migration 0037): ulist→itemize, olist→
    # enumerate. A container opens an env; its `item` children emit \item;
    # the env closes once we reach a chunk at or above the container's own
    # depth (i.e. we've left its subtree). The stack handles nested lists.
    list_stack: list[tuple[str, int]] = []
    for c in chunks:
        while list_stack and c.depth <= list_stack[-1][1]:
            lines.append(f"\\end{{{list_stack.pop()[0]}}}")
        label = f"\\label{{chunk:{c.dc}}}"
        # term + Glossary heading don't render as body — but keep an
        # invisible label so any [¶handle] cross-ref to them still
        # resolves (to the glossary) rather than dangling.
        is_glossary_heading = (
            c.chunk_kind == "heading" and (c.text or "").strip().lower() == "glossary"
        )
        if c.chunk_kind == "term" or is_glossary_heading:
            lines.append(f"\\phantomsection{label}%")
            continue
        if c.chunk_kind in ("ulist", "olist"):
            env = "itemize" if c.chunk_kind == "ulist" else "enumerate"
            lines.append(f"\\begin{{{env}}}")
            list_stack.append((env, c.depth))
            continue  # the container carries no prose; its items do
        if c.chunk_kind == "item":
            body = _render_inline(c.text or "", ctx)
            lines.append(f"\\item {body}{label}")
            lines.append("")
            continue
        if c.chunk_kind == "table":
            lines.extend(_render_table(c, ctx, label))
            lines.append("")
            continue
        if c.chunk_kind == "figure":
            lines.extend(_render_figure(c, ctx, label))
        elif c.chunk_kind == "heading":
            cmd = _SECTION_CMD[min(c.depth, len(_SECTION_CMD) - 1)]
            title = _render_inline(c.text or "", ctx)
            lines.append(f"\\{cmd}{{{title}}}{label}")
        elif c.chunk_kind in ("listing", "code"):
            # Code is verbatim — no inline rendering / escaping.
            lines.append(f"% {label[1:]}")
            lines.append("\\begin{lstlisting}")
            lines.append(c.text or "")
            lines.append("\\end{lstlisting}")
        elif c.chunk_kind in ("aside", "box"):
            body = _render_inline(c.text or "", ctx)
            lines.append(f"\\begin{{precisaside}}{label}{body}\\end{{precisaside}}")
        else:  # paragraph and friends
            body = _render_inline(c.text or "", ctx)
            lines.append(f"{body}{label}")
        lines.append("")  # blank line → paragraph break
    while list_stack:  # close any lists still open at the document end
        lines.append(f"\\end{{{list_stack.pop()[0]}}}")
    return RenderResult(
        body="\n".join(lines).strip() + "\n",
        cited_slugs=ctx.cited,
        acronyms=abbrevs,
        acronym_keys=ctx.keymap,
        warnings=ctx.warnings,
        figures=ctx.figures,
    )


def _render_figure(c: Any, ctx: _Ctx, label: str) -> list[str]:
    """Render a ``chunk_kind='figure'`` chunk as a LaTeX ``figure`` float
    (ADR 0058 slice 4). The image asset is resolved to bytes+ext and recorded
    on ``ctx.figures`` for the caller to write under ``pics/``; the caption is
    the chunk text. An asset-less figure (should be caught by the clearance
    gate first) emits a visible placeholder + a warning rather than vanishing."""
    from precis.utils.figure_source import figure_export_asset

    caption = _render_inline(c.text or "", ctx)
    asset = figure_export_asset(ctx.store, c)
    if asset is None:
        ctx.warnings.append(f"figure {c.dc} has no exportable image — placeholder used")
        return [
            "\\begin{figure}[htbp]",
            "\\centering",
            "\\fbox{\\emph{[figure image pending]}}",
            f"\\caption{{{caption}}}{label}",
            "\\end{figure}",
        ]
    data, ext = asset
    relpath = f"pics/{c.dc}.{ext}"
    ctx.figures.append((relpath, data))
    return [
        "\\begin{figure}[htbp]",
        "\\centering",
        f"\\includegraphics[width=\\linewidth,height=0.42\\textheight,"
        f"keepaspectratio]{{{relpath}}}",
        f"\\caption{{{caption}}}{label}",
        "\\end{figure}",
    ]


def build_acronyms(
    abbrevs: dict[str, str], keymap: dict[str, str] | None = None
) -> str:
    """``\\newacronym`` lines for every defined abbreviation, keyed by the
    same collision-resolved map the body's ``\\gls`` calls use (so every
    ``\\gls{key}`` has exactly one matching definition)."""
    keymap = keymap or _acronym_keymap(abbrevs)
    lines = []
    for short, long in sorted(abbrevs.items()):
        lines.append(
            f"\\newacronym{{{keymap[short]}}}{{{_latex_escape(short)}}}"
            f"{{{_encode_unicode(_latex_escape(long))}}}"
        )
    return "\n".join(lines)


def _bibtex_authors(authors: list[dict[str, Any]] | None) -> str:
    """A BibTeX ``author = {A and B and …}`` value from the ref's authors
    list (each ``{name|family|given}``). Empty when unknown."""
    if not authors:
        return ""
    names = []
    for a in authors:
        name = a.get("name") or " ".join(
            x for x in (a.get("given"), a.get("family")) if x
        )
        if name:
            names.append(name)
    return " and ".join(names)


# A cited handle resolves to one of these citeable kinds (checked in order),
# each with its BibTeX entry type. Datasheets are citeable evidence
# (``corpus_role='evidence'``) so they can land in the cited set alongside
# papers / patents — a ``@manual`` entry (BibTeX's technical-manual type) is
# the natural home for a datasheet: it carries ``organization`` (the vendor)
# and ``howpublished`` (the sub-type) instead of degrading to a bare stub.
_CITE_ENTRY_TYPES = (
    ("paper", "article"),
    ("patent", "patent"),
    ("datasheet", "manual"),
)

# Human labels for the ``meta.subtype`` sub-genre of a datasheet — surfaced in
# the bibliography (``howpublished``) + the reader badge + the docx reference
# line so a cited app-note reads as an app-note, not a bare "Datasheet".
_DATASHEET_SUBTYPE_LABELS = {
    "datasheet": "Datasheet",
    "app-note": "Application note",
    "errata": "Errata",
    "reference-manual": "Reference manual",
}


def datasheet_pub_label(meta: dict[str, Any] | None) -> str:
    """The human sub-type label for a datasheet ref's ``meta`` (default
    ``"Datasheet"``). Shared by the LaTeX bib, the docx reference line, and
    the web reader badge so all three agree on the genre string."""
    sub = str((meta or {}).get("subtype") or "datasheet")
    return _DATASHEET_SUBTYPE_LABELS.get(sub, "Datasheet")


def build_bib(store: Any, slugs: list[str], warnings: list[str]) -> str:
    """Generate ``refs.bib`` from the cited source refs. Resolves each
    slug to its paper / patent / datasheet ref, pulling
    title / authors / year / DOI / arXiv. A slug with no matching source
    gets a stub entry + a warning so the document still compiles."""
    entries: list[str] = []
    ref_by_slug: dict[str, Any] = {}
    entry_type_by_slug: dict[str, str] = {}
    ids: list[int] = []
    for slug in slugs:
        # A cited handle resolves to a paper, a patent, or a datasheet (all
        # citeable); a finding has already been mapped to its primary cite_key
        # (a paper) or its pub_id placeholder upstream, so it lands here as a
        # paper slug or a stub.
        pref = None
        entry_type = "misc"
        for kind, etype in _CITE_ENTRY_TYPES:
            pref = store.get_ref(kind=kind, id=slug)
            if pref is not None:
                entry_type = etype
                break
        if pref is None:
            warnings.append(f"cite {slug!r}: no source in corpus — stub bib entry")
            entries.append(
                f"@misc{{{slug},\n  title = {{[missing source {slug}]}},\n"
                "  note = {Auto-stub by precis export; cited slug not in corpus.},\n}"
            )
            continue
        ref_by_slug[slug] = pref
        entry_type_by_slug[slug] = entry_type
        ids.append(pref.id)
    aliases = store.identifiers_for_refs(ids) if ids else {}
    for slug, pref in ref_by_slug.items():
        fields = [f"  title = {{{_encode_unicode(_latex_escape(pref.title or slug))}}}"]
        authors = _encode_unicode(_bibtex_authors(pref.authors))
        if authors:
            fields.append(f"  author = {{{authors}}}")
        if pref.year:
            fields.append(f"  year = {{{pref.year}}}")
        alias = aliases.get(pref.id, {})
        if alias.get("doi"):
            fields.append(f"  doi = {{{alias['doi']}}}")
        if alias.get("arxiv"):
            fields.append(
                f"  eprint = {{{alias['arxiv']}}},\n  archiveprefix = {{arXiv}}"
            )
        entry_type = entry_type_by_slug[slug]
        if entry_type == "manual":
            # A datasheet: vendor → @manual organization, sub-type → the
            # howpublished genre label, and the documented part (if recorded
            # in meta) as a note. All optional — absent fields just drop.
            meta = pref.meta or {}
            vendor = str(meta.get("vendor") or "").strip()
            if vendor:
                fields.append(f"  organization = {{{_tex(vendor)}}}")
            fields.append(f"  howpublished = {{{datasheet_pub_label(meta)}}}")
            part = str(meta.get("part_lcsc") or "").strip()
            if part:
                fields.append(f"  note = {{Part {_tex(part)}}}")
        entries.append(f"@{entry_type}{{{slug},\n" + ",\n".join(fields) + ",\n}")
    return "\n\n".join(entries) + ("\n" if entries else "")


def _template_text(name: str) -> str:
    """A checked-in export template file, read from package data."""
    return (
        resources.files("precis.data.templates.draft")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


def _tex(text: str) -> str:
    """Escape + unicode-encode a run of plain prose for LaTeX."""
    return _encode_unicode(_latex_escape(text))


def _affil_tex(org: str, ror: str) -> str:
    """One affiliation's rendered LaTeX — the org name, hyperlinked to its
    ROR id when known (hyperref is loaded in the preamble; ``\\url``-safe
    id, org text escaped)."""
    body = _tex(org) if org else _tex(ror)
    if ror:
        # ROR ids are simple https URLs; keep the target raw (no escape).
        return f"\\href{{{ror}}}{{{body}}}"
    return body


def build_author_block(authors_raw: Any, *, fallback: str) -> str:
    """The ``\\author{}`` (+ ``authblk`` ``\\affil{}``) block for the title.

    From a draft ref's ``authors`` column: one ``\\author[marks]{Name}``
    per author + one ``\\affil[i]{Org}`` per distinct affiliation (ROR
    hyperlinked), deduped + numbered by :func:`build_byline`. A single
    shared affiliation drops the numbers. When the draft has no authors
    the block degrades to a single ``\\author{<fallback>}`` (the legacy
    ``meta.author`` string), so old drafts export unchanged.
    """
    byline = build_byline(authors_raw)
    people = byline["authors"]
    if not people:
        return f"\\author{{{_tex(fallback)}}}"
    lines: list[str] = []
    multi = byline["multi"]
    for a in people:
        opt = f"[{a['sup']}]" if (multi and a["sup"]) else ""
        lines.append(f"\\author{opt}{{{_tex(a['name'])}}}")
    for aff in byline["affiliations"]:
        opt = f"[{aff['index']}]" if multi else ""
        lines.append(f"\\affil{opt}{{{_affil_tex(aff['org'], aff['ror'])}}}")
    return "\n".join(lines)


def build_source_appendix(bundle: Any, warnings: list[str]) -> str:
    """A ``pdfpages`` appendix that inlines every cited source PDF the host
    holds, one bookmarked entry each. Empty string when nothing is present.

    Each present source is preceded by an ``\\addcontentsline`` so it gets a
    TOC / PDF-bookmark entry, then ``\\includepdf[pages=-]`` pulls in all its
    pages. Missing sources become a ``% not bundled`` comment + a warning, so
    the .tex records the gap without breaking the build.
    """
    present = bundle.present
    lines = ["\\clearpage", "\\appendix", "\\section{Referenced Sources}"]
    from precis.export.sources import safe_source_filename

    for e in present:
        heading = _tex(e.title)
        fname = safe_source_filename(e.slug)
        lines += [
            "\\phantomsection",
            f"\\addcontentsline{{toc}}{{subsection}}{{{heading}}}",
            f"\\includepdf[pages=-]{{sources/{fname}.pdf}}",
        ]
    for e in bundle.missing:
        warnings.append(f"source {e.slug!r} not bundled: {e.reason or 'unknown'}")
        lines.append(f"% not bundled: {e.kind or 'source'}:{e.slug} — {e.reason}")
    if not present:
        # No PDFs to inline — a bare "\appendix \section" with only comments
        # would still typeset an empty section; skip the appendix entirely.
        return ""
    return "\n".join(lines)


def assemble_document(
    *,
    title: str,
    author_block: str,
    body: str,
    acronyms: str,
    appendix: str = "",
    doc_type: str = "",
) -> str:
    """Assemble the full ``main.tex`` around the checked-in preamble.

    ``author_block`` is the pre-rendered ``\\author{}`` / ``\\affil{}``
    lines from :func:`build_author_block` (already escaped). ``appendix`` is
    an optional pre-rendered block (e.g. the ``pdfpages`` referenced-sources
    appendix) placed after the bibliography.

    ``doc_type='patent'`` suppresses the bibliography (``\\addbibresource`` /
    ``\\printbibliography``) — a patent specification cites prior art in the
    running text, not in a reference list.
    """
    patent_mode = doc_type == _PATENT_DOC_TYPE
    parts = [
        _template_text("preamble.tex").rstrip(),
        "",
    ]
    if not patent_mode:
        parts.append("\\addbibresource{refs.bib}")
    parts.append("\\makeglossaries")
    if acronyms:
        parts += ["", "% ── acronyms (auto-generated from defined terms) ──", acronyms]
    parts += [
        "",
        f"\\title{{{_encode_unicode(_latex_escape(title))}}}",
        author_block,
        "\\date{\\today}",
        "",
        "\\begin{document}",
        "\\maketitle",
        "",
        body.rstrip(),
        "",
        "\\printglossaries",
    ]
    if not patent_mode:
        parts.append("\\printbibliography")
    if appendix:
        parts += ["", appendix]
    parts += ["\\end{document}", ""]
    return "\n".join(parts)


def export_draft(
    store: Any,
    ref: Any,
    *,
    target_dir: Path,
    include_sources: bool = False,
    doc_type: str | None = None,
) -> ExportResult:
    """Render a draft into a compilable LaTeX project under
    ``target_dir``: ``main.tex`` + ``refs.bib`` + a copy of the
    checked-in ``preamble.tex`` (so the project is self-contained).

    ``include_sources=True`` additionally copies every cited source PDF the
    host holds into ``target_dir/sources/`` and appends them as a
    ``pdfpages`` appendix, so the compiled PDF is self-contained (report +
    its referenced papers / datasheets). Sources the host can't locate are
    listed in ``ExportResult.warnings`` and ``source_bundle.missing``."""
    from precis.export import guard_exportable

    guard_exportable(ref)
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    # doc_type drives the citation genre. Caller may pass it (the export
    # worker already loads the project Workspace); otherwise fall back to
    # the draft ref's own cascaded ``meta.workspace.doc_type``.
    if doc_type is None:
        ws = Workspace.from_meta(getattr(ref, "meta", None))
        doc_type = ws.doc_type if ws else ""
    patent_mode = doc_type == _PATENT_DOC_TYPE

    rendered = render_body(store, ref, doc_type=doc_type)
    acronyms_tex = build_acronyms(rendered.acronyms, rendered.acronym_keys)
    # A patent specification has no bibliography — everything is cited
    # in-text, so ``cited_slugs`` stays empty and refs.bib is a stub.
    bib_text = (
        "" if patent_mode else build_bib(store, rendered.cited_slugs, rendered.warnings)
    )
    title = (ref.title or ref.slug or "Untitled").split("\n", 1)[0]
    fallback = str((ref.meta or {}).get("author") or "precis")
    author_block = build_author_block(getattr(ref, "authors", None), fallback=fallback)

    appendix_tex = ""
    source_bundle = None
    if include_sources:
        from precis.export.sources import collect_cited_sources, safe_source_filename

        source_bundle = collect_cited_sources(
            store, ref, cited_slugs=rendered.cited_slugs
        )
        if source_bundle.present:
            src_dir = target_dir / "sources"
            src_dir.mkdir(parents=True, exist_ok=True)
            for e in source_bundle.present:
                assert e.local_path is not None
                shutil.copyfile(
                    e.local_path, src_dir / f"{safe_source_filename(e.slug)}.pdf"
                )
        appendix_tex = build_source_appendix(source_bundle, rendered.warnings)

    main_tex = assemble_document(
        title=title,
        author_block=author_block,
        body=rendered.body,
        acronyms=acronyms_tex,
        appendix=appendix_tex,
        doc_type=doc_type,
    )

    # Materialise figure images beside main.tex (ADR 0058 slice 4) — the body
    # references them as pics/<dc>.<ext> via \includegraphics.
    if rendered.figures:
        pics_dir = target_dir / "pics"
        pics_dir.mkdir(parents=True, exist_ok=True)
        for relpath, data in rendered.figures:
            (target_dir / relpath).write_bytes(data)

    main_path = target_dir / "main.tex"
    bib_path = target_dir / "refs.bib"
    preamble_path = target_dir / "preamble.tex"
    latexmkrc_path = target_dir / ".latexmkrc"
    main_path.write_text(main_tex, encoding="utf-8")
    bib_path.write_text(bib_text, encoding="utf-8")
    preamble_path.write_text(_template_text("preamble.tex"), encoding="utf-8")
    # The .latexmkrc makes a bare `latexmk -pdf main.tex` run biber +
    # makeglossaries, so the project is self-contained / reproducible.
    latexmkrc_path.write_text(_template_text("latexmkrc"), encoding="utf-8")

    return ExportResult(
        main_tex=main_path,
        bib=bib_path,
        preamble=preamble_path,
        latexmkrc=latexmkrc_path,
        cited_slugs=rendered.cited_slugs,
        acronyms=rendered.acronyms,
        warnings=rendered.warnings,
        source_bundle=source_bundle,
    )


__all__ = [
    "ExportResult",
    "RenderResult",
    "assemble_document",
    "build_acronyms",
    "build_author_block",
    "build_bib",
    "build_source_appendix",
    "export_draft",
    "render_body",
]
