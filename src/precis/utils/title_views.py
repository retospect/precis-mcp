"""Paper-title views — raw / plain / md, one shared vocabulary (gr515227).

Titles arrive carrying inline markup: HTML (``<sup>``/``<sub>``/``<i>``/
entities) from Crossref and publishers, or LaTeX math (``$\\mathrm{CO_2}$``)
from arXiv. Storage keeps the raw string exactly as ingested; every
surface derives its view from this module — no per-surface regexes.

* ``title_plain`` — markup gone, entities decoded, math unwrapped:
  ``Pd<sup>II</sup> on CO<sub>2</sub>`` → ``PdII on CO2`` and
  ``$\\mathrm{CO_2}$`` → ``CO2``. For search, dedup, cite keys and the
  junk-title detector.
* ``title_md`` — the markdown/math form the web ``render_markdown``
  filter, MCP text output and BibTeX export speak: ``<sup>x</sup>`` →
  ``$^{x}$`` (letters upright — ``$^{\\mathrm{II}}$``, not italic),
  ``<sub>x</sub>`` → ``$_{x}$``, ``<i>`` → ``*..*``, ``<b>`` → ``**..**``,
  ``<sc>``/``<scp>`` → plain, LaTeX passes through, and a literal ``$``
  is escaped ``\\$`` ("The $100 Laptop" is not math).
* ``raw`` — the stored string itself; no function needed.

Pure, stdlib-only (``html.parser``), never raises on malformed input:
unclosed tags stay open to end-of-title, unmatched closers drop, and
unknown tags pass their text through.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_Node = tuple[str, list["_Node | str"]]

#: Word-level inline-formatting tags (incl. Elsevier's ``<scp>`` small-caps).
_INLINE_TAGS = frozenset(
    [
        "i",
        "b",
        "em",
        "u",
        "strong",
        "small",
        "span",
        "font",
        "scp",
        "sc",
        "tt",
        "mark",
        "s",
        "ins",
        "del",
    ]
)


class _TitleParser(HTMLParser):
    """A title's markup → a node tree. Lenient by construction:

    ``convert_charrefs`` decodes entities into text nodes; a close tag
    with no open partner is dropped; an unclosed tag stays open to the
    end of the title.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: _Node = ("", [])
        self._stack: list[_Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: object) -> None:
        node: _Node = (tag.lower(), [])
        self._stack[-1][1].append(node)
        self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: object) -> None:
        self._stack[-1][1].append((tag.lower(), []))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break
        # An unmatched closer is markup noise — ignore it.

    def handle_data(self, data: str) -> None:
        self._stack[-1][1].append(data)


def _parse(raw: str) -> _Node:
    # Decode entities first (twice — ``&amp;lt;`` double-encoded shapes land
    # as ``<``) so entity-encoded markup parses as markup, not text.
    p = _TitleParser()
    p.feed(html.unescape(html.unescape(raw)))
    p.close()
    return p.root


#: A removed tag leaves no trace when it sat between two word characters
#: (``planar<mml:math>…</mml:math>-bonded`` → ``planar …-bonded``) — the
#: ``\x00`` sentinel resolves to a space there and to "" elsewhere.
_BOUNDARY = "\x00"

#: ``$…$`` / ``$$…$$`` math spans inside title text. The opener must be
#: followed by a non-space, non-digit (``$100`` is a price, not math) and
#: the closer preceded by a non-space.
_MATH_RE = re.compile(r"\$\$[^$]+\$\$|\$(?!\s|\d)[^$]+?(?<!\s)\$")


def _math_plain(text: str) -> str:
    """Unwrap LaTeX wrappers inside a math span: ``\\mathrm{CO_2}`` →
    ``CO2``; command braces open (``\\textbf{x}`` → ``x``), a bare
    ``\\alpha`` keeps its letters, ``_{``/``^{`` groups and ``_``/``^``
    markers flatten to their content, ``~``/spacing commands → space."""
    s = re.sub(r"\\[,;:!~]", " ", text)
    s = s.replace("~", " ")
    s = re.sub(r"\\[a-zA-Z]+\s*\{", "{", s)  # \mathrm{ → {
    s = re.sub(r"\\[a-zA-Z]+", lambda m: m.group(0)[1:], s)  # \alpha → alpha
    s = s.replace(r"\$", "$")
    s = re.sub(r"\\.", "", s)
    return re.sub(r"[{}$^_]", "", s)


def _script_content(node: _Node) -> str:
    """Script body for ``$^{..}$``/``$_{..}$``: the plain text of the
    element's children (a nested ``<i>`` or stray tag can't inject markup
    into the math span)."""
    return _plain_children(node[1])


def _plain_node(node: _Node) -> str:
    tag, children = node
    if tag in ("sup", "sub"):
        return "".join(_plain_child(c) for c in children)
    inner = "".join(_plain_child(c) for c in children)
    if tag in _INLINE_TAGS:
        # A paired inline tag wrapping ≤2 chars is a sub/superscript in
        # disguise (``H2O<i><sub>y</sub></i>``) — glues tight; a longer
        # span is real formatting and marks a boundary.
        if len(inner.strip()) <= 2:
            return inner
        return f"{_BOUNDARY}{inner}{_BOUNDARY}"
    if tag.startswith("mml:") and tag != "mml:math":
        return inner  # MathML internals: tight, document order.
    # Every other tag (mml:math included — its tag soup carries its own
    # '/' separators and deeper structure plain doesn't need) marks a
    # boundary; post-processing turns it into a space between words. An
    # empty element (``<img>``) still splits its neighbours.
    return f"{_BOUNDARY}{inner}{_BOUNDARY}"


def _plain_child(child: _Node | str) -> str:
    return _plain_node(child) if isinstance(child, tuple) else child


def _plain_children(children: list[_Node | str]) -> str:
    return "".join(_plain_child(c) for c in children)


def title_plain(raw: str | None) -> str:
    """The plain-text view: tags stripped, entities decoded, LaTeX math
    unwrapped (``$\\mathrm{CO_2}$`` → ``CO2``, ``C$_{60}$`` → ``C60``).
    ``""`` for empty input."""
    if not raw:
        return ""
    s = _plain_node(_parse(raw))
    s = re.sub(rf"\w{_BOUNDARY}+\w", lambda m: f"{m.group(0)[0]} {m.group(0)[-1]}", s)
    s = s.replace(_BOUNDARY, "")
    # Math spans first (a ``$…$`` island unwraps to its plain content,
    # tight to its base), then any bare ``\cmd{…}``/``\cmd`` residue.
    s = _MATH_RE.sub(lambda m: _math_plain(m.group(0)), s)
    s = _math_plain(s)
    # Punctuation that marked a removed tag's boundary loses its space too.
    s = re.sub(r"([A-Za-z])([,;:])(?=[A-Za-z])", r"\1\2 ", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


def _script_wrap(inner: str) -> str:
    """Letters go upright (``\\mathrm``), digits/signs pass bare — so
    ``<sup>II</sup>`` → ``$^{\\mathrm{II}}$`` while ``<sup>-1</sup>`` →
    ``$^{-1}$``."""
    inner = inner.strip()
    if not inner:
        return ""
    return f"\\mathrm{{{inner}}}" if re.search(r"[A-Za-z]", inner) else inner


def _md_text(text: str) -> str:
    """Literal text → md: ``$…$`` math passes through, every other ``$``
    is escaped (``The $100 Laptop`` stays a price)."""
    out: list[str] = []
    pos = 0
    for m in _MATH_RE.finditer(text):
        out.append(text[pos : m.start()].replace("$", r"\$"))
        out.append(m.group(0))
        pos = m.end()
    out.append(text[pos:].replace("$", r"\$"))
    return "".join(out)


def _mml_script(node: _Node) -> tuple[str, str, str]:
    """``<mml:msub|msup|msubsup>`` → (base, sub, sup) rendered as md;
    missing children give ``""``."""
    kids = [c for c in node[1]]
    base = _md_children(kids[:1])
    script = _md_children(kids[1:2])
    sup = _md_children(kids[2:3])
    return base.strip(), script.strip(), sup.strip()


def _md_node(node: _Node) -> str:
    tag, children = node
    if tag in ("sup", "sub"):
        body = _script_wrap(_script_content(node))
        return f"${'^' if tag == 'sup' else '_'}{{{body}}}$" if body else ""
    if tag == "mml:msub":
        base, sub, _sup = _mml_script(node)
        return f"${base}_{{{sub}}}$" if base or sub else ""
    if tag == "mml:msup":
        base, _sub, sup = _mml_script(node)
        return f"${base}^{{{sup}}}$" if base or sup else ""
    if tag == "mml:msubsup":
        base, sub, sup = _mml_script(node)
        return f"${base}_{{{sub}}}^{{{sup}}}$" if base or sub or sup else ""
    inner = "".join(_md_child(c) for c in children)
    if not inner.strip():
        return inner if tag else ""
    if tag in ("i", "em"):
        stripped = inner.strip()
        return f"*{stripped}*" if stripped == inner else inner
    if tag in ("b", "strong"):
        stripped = inner.strip()
        return f"**{stripped}**" if stripped == inner else inner
    if tag.startswith("mml:"):
        return inner  # MathML internals keep their document order, tight.
    # sc/scp and unknown tags: dropped, content kept, word boundary.
    return f" {inner} "


def _md_child(child: _Node | str) -> str:
    return _md_node(child) if isinstance(child, tuple) else _md_text(child)


def _md_children(children: list[_Node | str]) -> str:
    return "".join(_md_child(c) for c in children)


def title_md(raw: str | None) -> str:
    """The markdown view: ``<sup>``/``<sub>`` → ``$^{..}$``/``$_{..}$``
    (letters upright), ``<i>`` → ``*..*``, ``<b>`` → ``**..**``, other
    markup dropped (contents kept), LaTeX passed through, literal ``$``
    escaped. ``""`` for empty input."""
    if not raw:
        return ""
    return re.sub(r"\s+", " ", _md_node(_parse(raw))).strip()


#: Atomic spans of a ``title_md`` string — ``$…$`` / ``$$…$$`` math and
#: ``*…*`` / ``**…**`` emphasis runs — that a display truncation must not
#: split (a cut inside leaves a dangling ``$``/``*`` that renders as
#: literal noise or, worse, pairs with a later ``$`` as fake math).
_MD_ATOM_RE = re.compile(r"\$\$[^$]+\$\$|\$[^$]+\$|\*+[^*\n]+\*+")


def title_md_clip(md: str, limit: int = 160) -> str:
    """Length-cap a ``title_md`` string without splitting a math or
    emphasis span: when the cap would land inside one, the cut moves back
    to the span's start (the whole span drops). A dangling ``\\`` of a
    ``\\$`` escape is also pulled back. Appends ``…`` on a cut."""
    if len(md) <= limit:
        return md
    cut = limit - 1
    for m in _MD_ATOM_RE.finditer(md):
        if m.start() >= cut:
            break
        if m.end() > cut:
            cut = m.start()
            break
    while cut > 0 and md[cut - 1] == "\\":
        cut -= 1
    return md[:cut].rstrip() + "…"


def title_plain_sql(col: str) -> str:
    """SQL-side approximation of :func:`title_plain` for the lexical
    (``to_tsvector`` / ``similarity``) legs — tags removed, ``\\cmd``
    words and ``$_^{}~`` math noise blanked. Entities aren't decoded
    (no ``unescape`` in SQL) — a rare false token at worst.
    """
    return (
        "regexp_replace(regexp_replace(" + col + ", '<[^>]+>', ' ', 'g'), "
        "'\\\\[a-zA-Z]+|[$_^{}~]', ' ', 'g')"
    )


__all__ = ["title_md", "title_md_clip", "title_plain", "title_plain_sql"]
