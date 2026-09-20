"""Markdown bullet text → the structured list chunk tree (migration 0037).

The **one** place that knows markdown list syntax. A draft stores a list as
a ``ulist``/``olist`` container owning ``item`` children (nested lists hang
off an item), because that is what every consumer already reads: the web
reader, the LaTeX/PDF export's ``itemize``/``enumerate`` depth stack, the
docx export's ``List Bullet`` levels, and the ``.tex`` importer's own
output. Bullet *text* inside a ``paragraph`` only ever rendered in the web
reader, so it looked right in the browser and collapsed into one run-on
line in every export — the trap this module closes.

Bullets are therefore an **input syntax**, parsed at the door
(:meth:`precis.store._draft_ops.DraftStore.add_chunks`), never a storage
form. A caller that meant a literal hyphen-led paragraph gets it back with
``edit(list_kind='normal')``, which dissolves a container to paragraphs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: A bullet line: indent, marker, text. ``-``/``*``/``+`` open a ``ulist``;
#: ``1.``/``1)`` open an ``olist``.
_BULLET = re.compile(
    r"^(?P<indent>[ \t]*)(?P<marker>[-*+]|\d{1,9}[.)])[ \t]+(?P<text>\S.*)$"
)
#: A fenced code block is never a list, however its lines start.
_FENCE = re.compile(r"^[ \t]*(```|~~~)")


@dataclass(slots=True)
class Node:
    """One node of the parsed tree: a ``ulist``/``olist`` container (``text``
    empty, children are its items) or an ``item`` (children are nested
    containers)."""

    chunk_kind: str
    text: str = ""
    children: list[Node] = field(default_factory=list)


def _expand(indent: str) -> int:
    """Indent width in columns, tabs counted as 4 — so a tab-indented
    sublist nests rather than reading as a sibling."""
    return len(indent.replace("\t", "    "))


def _kind(marker: str) -> str:
    return "ulist" if marker in ("-", "*", "+") else "olist"


def parse_list_block(text: str) -> Node | None:
    """Parse one blank-line-delimited block as a markdown list.

    Returns the root container, or ``None`` when the block is not a list —
    the conservative answer, so ordinary prose is never restructured. A
    block qualifies only when it opens with a bullet and **every** non-blank
    line is either a bullet or a continuation of the item above it (markdown
    lazy continuation: the corpus hard-wraps items at column 0). That
    all-lines rule is what keeps a paragraph that merely opens with a dash
    out — its second sentence would have to be a bullet too.

    Continuations join with a single space; the items are prose.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if any(_FENCE.match(ln) for ln in lines):
        return None
    first = next((ln for ln in lines if ln.strip()), None)
    if first is None or not _BULLET.match(first):
        return None
    if sum(1 for ln in lines if _BULLET.match(ln)) < 2:
        # One bullet is not a list worth restructuring, and a paragraph
        # that opens with a dash and runs on ("- the clause, said X, then
        # …") would otherwise convert under lazy continuation. Across the
        # whole corpus exactly one genuine list has a single item, so this
        # trades that for the entire false-positive class.
        return None

    root: Node | None = None
    # (indent, container, last item of that container) innermost last.
    stack: list[tuple[int, Node, Node]] = []
    for ln in lines:
        if not ln.strip():
            continue  # loose list — blank lines between items are fine
        m = _BULLET.match(ln)
        if m is None:
            if not stack:  # unreachable: the first line is a bullet
                return None
            item = stack[-1][2]
            item.text = f"{item.text} {ln.strip()}".strip()
            continue
        indent, kind, body = _expand(m["indent"]), _kind(m["marker"]), m["text"].strip()
        while len(stack) > 1 and indent < stack[-1][0]:
            stack.pop()
        if not stack:
            root = Node(kind)
            stack.append((indent, root, Node("item")))
            stack[-1][1].children.append(stack[-1][2])
            stack[-1][2].text = body
            continue
        top_indent, container, last_item = stack[-1]
        if indent > top_indent:  # deeper → a sublist under the last item
            sub = Node(kind)
            last_item.children.append(sub)
            item = Node("item", body)
            sub.children.append(item)
            stack.append((indent, sub, item))
            continue
        # same level (or shallower than every open level but the root) —
        # a sibling item. A marker switch mid-level (``-`` then ``1.``)
        # means the block is two lists, which one container cannot hold:
        # decline, and the block stays the paragraph it already is.
        if kind != container.chunk_kind:
            return None
        item = Node("item", body)
        container.children.append(item)
        stack[-1] = (top_indent, container, item)
    return root


def count_items(node: Node) -> int:
    """Items in this container, nested ones included — the outline's gloss."""
    return sum(
        (1 if c.chunk_kind == "item" else 0) + count_items(c) for c in node.children
    )
