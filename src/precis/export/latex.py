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
  occurrence becomes ``\\gls{key}`` — so first use expands to the full
  term and every later use is the abbreviation, automatically, with the
  page-number "where it occurs" list in the glossary.

This module produces the *project files* (``main.tex`` + ``refs.bib`` +
the copied ``preamble.tex``). Compiling them (latexmk + biber +
makeglossaries) and the post-compile LLM repair loop are a separate
increment; so is the Word/pandoc path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from pylatexenc.latexencode import UnicodeToLatexEncoder

from precis.utils import handle_registry, mentions
from precis.utils.draft_markup import DRAFT_CITE_PATTERN

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


@dataclass
class RenderResult:
    """The assembled body plus what it referenced."""

    body: str
    cited_slugs: list[str] = field(default_factory=list)
    acronyms: dict[str, str] = field(default_factory=dict)  # short → long
    acronym_keys: dict[str, str] = field(default_factory=dict)  # short → gls key
    warnings: list[str] = field(default_factory=list)


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


def _glsify(escaped: str, keymap: dict[str, str]) -> str:
    """Replace whole-word occurrences of each known abbreviation short
    with ``\\gls{key}`` (longest-first, word-bounded). Runs on
    already-escaped prose; shorts are alphanumerics so escaping never
    touched them."""
    if not keymap:
        return escaped
    shorts = sorted((s for s in keymap if s), key=len, reverse=True)
    pat = re.compile(
        r"(?<![\w-])(" + "|".join(re.escape(s) for s in shorts) + r")(?![\w-])"
    )
    return pat.sub(lambda m: f"\\gls{{{keymap[m.group(1)]}}}", escaped)


@dataclass
class _Ctx:
    """Per-export render state threaded through the inline pass."""

    keymap: dict[str, str]  # abbreviation short → glossary key
    known_handles: set[str]  # every live dc<id> chunk handle in the draft
    store: Any = None  # to resolve a paper handle (pc/pa) → cite_key
    legacy_to_dc: dict[str, str] = field(default_factory=dict)  # ¶base58 → dc
    cited: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


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
    # 6. Abbreviations → \gls.
    s = _glsify(s, ctx.keymap)
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
        if kind == "paper":
            slug = _handle_cite_key(tgt, ctx)
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
    return ""  # other authoring targets — provenance only


def _cite(slug: str, ctx: _Ctx) -> str:
    # Cite the PAPER, not the chunk: ``a~3`` / ``a~9`` → one \cite{a} and
    # one bib entry (biblatex collapses repeated cites; build_bib resolves
    # the bare slug).
    slug = slug.split("~", 1)[0]
    if slug not in ctx.cited:
        ctx.cited.append(slug)
    return f"\\cite{{{slug}}}"


def _render_inline(text: str, ctx: _Ctx) -> str:
    """Render a chunk's text: walk references (rendered as LaTeX markup),
    LaTeX-escape + markdownify the gaps between them. Single pass, mirrors
    the web linkifier so the two never diverge."""
    out: list[str] = []
    last = 0
    for m in _COMBINED.finditer(text):
        out.append(_render_gap(text[last : m.start()], ctx))
        out.append(_render_reference(m, ctx))
        last = m.end()
    out.append(_render_gap(text[last:], ctx))
    return "".join(out)


#: chunk depth → sectioning command. Deeper than subsubsection collapses
#: to a run-in paragraph heading.
_SECTION_CMD = ["section", "subsection", "subsubsection", "paragraph"]


def render_body(store: Any, ref: Any) -> RenderResult:
    """Render the whole draft body to LaTeX (no preamble/title chrome)."""
    chunks = store.reading_order(ref.id)
    abbrevs: dict[str, str] = store.defined_abbrevs(ref.id)
    ctx = _Ctx(
        keymap=_acronym_keymap(abbrevs),
        known_handles={c.dc for c in chunks},
        store=store,
        legacy_to_dc={c.handle: c.dc for c in chunks},
    )
    lines: list[str] = []
    for c in chunks:
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
        if c.chunk_kind == "heading":
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
    return RenderResult(
        body="\n".join(lines).strip() + "\n",
        cited_slugs=ctx.cited,
        acronyms=abbrevs,
        acronym_keys=ctx.keymap,
        warnings=ctx.warnings,
    )


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


def build_bib(store: Any, slugs: list[str], warnings: list[str]) -> str:
    """Generate ``refs.bib`` from the cited paper refs. Resolves each
    slug to its paper ref, pulling title / authors / year / DOI / arXiv.
    A slug with no matching paper gets a stub entry + a warning so the
    document still compiles."""
    entries: list[str] = []
    ref_by_slug: dict[str, Any] = {}
    ids: list[int] = []
    for slug in slugs:
        pref = store.get_ref(kind="paper", id=slug)
        if pref is None:
            warnings.append(f"cite {slug!r}: no paper in corpus — stub bib entry")
            entries.append(
                f"@misc{{{slug},\n  title = {{[missing paper {slug}]}},\n"
                "  note = {Auto-stub by precis export; cited slug not in corpus.},\n}"
            )
            continue
        ref_by_slug[slug] = pref
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
        entries.append(f"@article{{{slug},\n" + ",\n".join(fields) + ",\n}")
    return "\n\n".join(entries) + ("\n" if entries else "")


def _template_text(name: str) -> str:
    """A checked-in export template file, read from package data."""
    return (
        resources.files("precis.data.templates.draft")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


def assemble_document(*, title: str, author: str, body: str, acronyms: str) -> str:
    """Assemble the full ``main.tex`` around the checked-in preamble."""
    parts = [
        _template_text("preamble.tex").rstrip(),
        "",
        "\\addbibresource{refs.bib}",
        "\\makeglossaries",
    ]
    if acronyms:
        parts += ["", "% ── acronyms (auto-generated from defined terms) ──", acronyms]
    parts += [
        "",
        f"\\title{{{_encode_unicode(_latex_escape(title))}}}",
        f"\\author{{{_encode_unicode(_latex_escape(author))}}}",
        "\\date{\\today}",
        "",
        "\\begin{document}",
        "\\maketitle",
        "",
        body.rstrip(),
        "",
        "\\printglossaries",
        "\\printbibliography",
        "\\end{document}",
        "",
    ]
    return "\n".join(parts)


def export_draft(store: Any, ref: Any, *, target_dir: Path) -> ExportResult:
    """Render a draft into a compilable LaTeX project under
    ``target_dir``: ``main.tex`` + ``refs.bib`` + a copy of the
    checked-in ``preamble.tex`` (so the project is self-contained)."""
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    rendered = render_body(store, ref)
    acronyms_tex = build_acronyms(rendered.acronyms, rendered.acronym_keys)
    bib_text = build_bib(store, rendered.cited_slugs, rendered.warnings)
    title = (ref.title or ref.slug or "Untitled").split("\n", 1)[0]
    author = str((ref.meta or {}).get("author") or "precis")
    main_tex = assemble_document(
        title=title, author=author, body=rendered.body, acronyms=acronyms_tex
    )

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
    )


__all__ = [
    "ExportResult",
    "RenderResult",
    "assemble_document",
    "build_acronyms",
    "build_bib",
    "export_draft",
    "render_body",
]
