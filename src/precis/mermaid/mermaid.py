"""Pure mermaid helpers + the ``MermaidLang`` diagram-core instance.

Validation / render / export go through ``mermaidx`` (embedded QuickJS running
the real mermaid.js, resvg rasterizer) — lazy-imported so a non-mermaid build
never loads it. Node extraction is a small source scan (pure Python, no engine
needed) so the element→chunk bindings and the dangling lint work even where
``mermaidx`` is absent (the dark gate). Coordinates are topology here, not
geometry: a node's ``coords`` is its out-neighbours.
"""

from __future__ import annotations

import re
from itertools import pairwise
from typing import Any

from precis.diagram.lang import Element, LintFinding

# ── mermaidx engine (lazy) ────────────────────────────────────────────────


def _engine() -> Any | None:
    """The ``mermaidx`` module, or ``None`` when the ``[mermaid]`` extra is not
    installed (the dark path — validation degrades to accept-as-authored)."""
    try:
        import mermaidx  # type: ignore[import-not-found]
    except Exception:
        return None
    return mermaidx


def compile_error(source: str) -> str | None:
    """``None`` if the mermaid source renders, else a one-line reason. When the
    engine is absent we cannot validate, so we accept (the kind is dark then)."""
    if not source.strip():
        return "empty mermaid source"
    mx = _engine()
    if mx is None:
        return None
    try:
        mx.render(source).svg()
    except Exception as exc:  # mermaidx raises RuntimeError w/ the parse error
        return _one_line(str(exc))
    return None


def render_svg(source: str) -> str:
    """Render mermaid to an SVG string via ``mermaidx``. Raises if the engine
    is absent or the source is invalid — callers (the web route) guard."""
    mx = _engine()
    if mx is None:
        raise RuntimeError("mermaidx is not installed (the [mermaid] extra)")
    return str(mx.render(source).svg())


def _one_line(msg: str) -> str:
    line = msg.strip().splitlines()[0] if msg.strip() else "mermaid did not parse"
    return line.removeprefix("Mermaid rendering failed: ").strip()


# ── sanitize ──────────────────────────────────────────────────────────────

#: A mermaid interaction directive (``click <id> …`` binds a JS callback /
#: navigation). We render statically and never want interactivity, so these
#: are dropped — defense-in-depth on top of sanitizing the rendered SVG.
_CLICK_RE = re.compile(r"^\s*click\b", re.IGNORECASE)


def sanitize(source: str) -> str:
    """Drop ``click`` interaction directives. The real trust boundary is the
    rendered-SVG sanitizer (``figure.svg.sanitize_svg`` on ``render_svg``
    output); this just removes the mermaid-source vector for JS callbacks."""
    return "\n".join(ln for ln in source.splitlines() if not _CLICK_RE.match(ln))


# ── node extraction (source scan) ─────────────────────────────────────────

#: Non-node statement leaders — skipped when scanning for graph nodes.
_SKIP = (
    "flowchart",
    "graph",
    "sequencediagram",
    "statediagram",
    "statediagram-v2",
    "classdiagram",
    "erdiagram",
    "subgraph",
    "end",
    "classdef",
    "class",
    "style",
    "linkstyle",
    "direction",
    "click",
    "acctitle",
    "accdescr",
    "%%",
)

_SHAPE_TAG = {
    "[[": "subroutine",
    "([": "stadium",
    "((": "circle",
    "[(": "cylinder",
    "[": "rect",
    "(": "round",
    "{{": "hexagon",
    "{": "diamond",
    ">": "flag",
}
#: An ``id`` immediately followed by a shape opener (``A[Label]``, ``B{X}``).
_SHAPE_DECL = re.compile(
    r"(?:^|[\s>|.=-])([A-Za-z][\w-]*)\s*(\[\[|\(\[|\(\(|\[\(|\{\{|\[|\(|\{|>)"
)
#: A mermaid edge operator (a run of ``-.=`` with optional arrow/x/o heads).
_EDGE_OP = re.compile(r"<?[-.=]{2,}[->xo]?|--[xo]|[ox]--[ox]")
#: A sequence-diagram message arrow (``->>``, ``-->>``, ``-x``, ``-)`` …).
_SEQ_ARROW = re.compile(r"--?>>?|--?[)x]|<<-?-?>>")
_ID = re.compile(r"([A-Za-z][\w-]*)")


def elements(source: str) -> list[Element]:
    """Every bindable node id in the source, in first-seen order. Handles
    flowchart/graph well and sequence/state/other reasonably; not a full
    mermaid grammar (documented in the skill). ``coords`` is a node's
    out-neighbours (topology), or ``""``."""
    kind = _diagram_kind(source)
    if kind == "sequence":
        return _sequence_nodes(source)
    return _graph_nodes(source)


def _diagram_kind(source: str) -> str:
    for ln in source.splitlines():
        s = ln.strip().lower()
        if not s or s.startswith("%%"):
            continue
        if s.startswith("sequencediagram"):
            return "sequence"
        return "graph"
    return "graph"


def _graph_nodes(source: str) -> list[Element]:
    tags: dict[str, str] = {}
    order: list[str] = []
    edges: dict[str, list[str]] = {}

    def see(node: str) -> None:
        if node not in tags:
            tags[node] = "node"
            order.append(node)
            edges.setdefault(node, [])

    for raw in source.splitlines():
        line = raw.strip()
        if not line or _is_skip(line):
            continue
        # shape declarations carry the node's tag
        for nid, opener in _SHAPE_DECL.findall(line):
            see(nid)
            tags[nid] = _SHAPE_TAG.get(opener, "node")
        # edges: delabel, then split on edge operators
        bare = _delabel(line)
        parts = [p.strip() for p in _EDGE_OP.split(bare)]
        ids = [m.group(1) for p in parts if (m := _ID.match(p))]
        for nid in ids:
            see(nid)
        for a, b in pairwise(ids):
            if b not in edges[a]:
                edges[a].append(b)
    return [Element(id=n, tag=tags[n], coords=_topology(edges[n])) for n in order]


def _sequence_nodes(source: str) -> list[Element]:
    order: list[str] = []
    seen: set[str] = set()

    def see(node: str) -> None:
        if node and node not in seen:
            seen.add(node)
            order.append(node)

    for raw in source.splitlines():
        line = raw.strip()
        if not line or line.startswith("%%"):
            continue
        low = line.lower()
        if low.startswith(("participant", "actor")):
            rest = line.split(None, 1)[1] if " " in line else ""
            name = re.split(r"\bas\b", rest, maxsplit=1)[0].strip()
            if m := _ID.match(name):
                see(m.group(1))
            continue
        msg = line.split(":", 1)[0]
        endpoints = [p.strip() for p in _SEQ_ARROW.split(msg)]
        if len(endpoints) >= 2:
            for e in endpoints:
                if m := _ID.match(e):
                    see(m.group(1))
    return [Element(id=n, tag="participant", coords="") for n in order]


def _is_skip(line: str) -> bool:
    low = line.lower()
    return any(low.startswith(w) for w in _SKIP)


def _delabel(line: str) -> str:
    """Strip shape-label and edge-label contents, keeping the ids: ``A[Start]
    --> B{X}`` → ``A  --> B``."""
    prev = None
    while prev != line:
        prev = line
        line = re.sub(r"\[[^\[\]]*\]", " ", line)
        line = re.sub(r"\([^()]*\)", " ", line)
        line = re.sub(r"\{[^{}]*\}", " ", line)
        line = re.sub(r"\|[^|]*\|", " ", line)
    return line


def _topology(out_neighbours: list[str]) -> str:
    return "→" + ",".join(out_neighbours) if out_neighbours else ""


def lint_bindings(source: str, bound_ids: set[str]) -> list[LintFinding]:
    """A ``'binding'`` finding for each bound id absent from the source."""
    if not bound_ids:
        return []
    present = {e.id for e in elements(source)}
    return [
        LintFinding(
            "binding",
            nid,
            f"binding references node id {nid!r}, but the source has no such "
            f"node (renamed or removed?)",
        )
        for nid in sorted(bound_ids)
        if nid not in present
    ]


def default_source() -> str:
    """A valid starter diagram — a single labelled node (mermaid needs one)."""
    return "flowchart TD\n  start[Start]\n"


# ── prompt fragments ──────────────────────────────────────────────────────

_MERMAID_FLOOR = (
    "You are drawing a MERMAID diagram WITH a human. You maintain three "
    "things: the mermaid source; the shared VOCABULARY (high-level and "
    "human-facing — what the diagram is); and your private implementation "
    "NOTES (node ids, structure, conventions). Every turn: update the "
    "vocabulary and keep it high-level and concise (move any low-level "
    "detail into notes), keep notes accurate for consistent edits, and keep "
    "your chat reply short — the detail lives in the docs, not the chat. Edit "
    "by rewriting the WHOLE mermaid source (one diagram, first line is the "
    "type: flowchart / sequenceDiagram / stateDiagram-v2 / classDiagram …). "
    "Name every meaningful node with a stable, short id (e.g. `intake`, "
    "`review`) so it can be talked about and bound to a chunk. Mermaid "
    "auto-lays-out — structure the graph, don't place coordinates. Do NOT use "
    "`click` interactions (they are stripped)."
)

_MERMAID_JSON_CONTRACT = (
    'Reply with ONE JSON object and nothing else: {"reply": "<a SHORT chat '
    'message to the human>", "mermaid": "<the COMPLETE new mermaid source, or '
    'omit/empty to leave the diagram unchanged>", "vocab": "<the updated '
    'shared vocabulary — high-level, for the human — or omit if unchanged>", '
    '"notes": "<the updated implementation notes — your private design log — '
    'or omit if unchanged>", "links": [{"element": "<a stable node id in your '
    'source>", "target": "<a chunk handle: dc… draft / pc… paper / me… '
    'memory>", "relation": "depicts"}] (the COMPLETE desired set of '
    "node→chunk bindings — this replaces the current set; omit the key "
    "entirely to leave bindings unchanged)}."
)


class MermaidLang:
    """The mermaid :class:`~precis.diagram.lang.DiagramLang` — delegates source
    mechanics to this module and carries the mermaid prompt strings. Bounds are
    ``None`` (mermaid auto-lays-out; there is no coordinate frame)."""

    kind = "mermaid"
    source_kind = "mermaid_node"
    vocab_kind = "mermaid_vocab"
    notes_kind = "mermaid_notes"
    turn_kind = "mermaid_turn"
    skill_name = "precis-mermaid"
    source_key = "mermaid"
    bounds_meta_key = "mermaid_layout"  # unused — read_bounds is always None

    def parse_error(self, source: str) -> str | None:
        return compile_error(source)

    def sanitize(self, source: str) -> str:
        return sanitize(source)

    def lint(self, source: str, bounds: Any) -> list[LintFinding]:
        err = compile_error(source)
        return [LintFinding("compile", "", err)] if err else []

    def elements(self, source: str) -> list[Element]:
        return elements(source)

    def lint_bindings(self, source: str, bound_ids: set[str]) -> list[LintFinding]:
        return lint_bindings(source, bound_ids)

    def default_source(self, bounds: Any) -> str:
        return default_source()

    def read_bounds(self, source: str) -> Any | None:
        return None

    def default_bounds(self) -> Any:
        return None

    def bounds_from_meta(self, raw: Any) -> Any | None:
        return None

    def bounds_to_meta(self, bounds: Any) -> Any:
        return bounds

    def floor_guidance(self) -> str:
        return _MERMAID_FLOOR

    def canvas_section(self, bounds: Any) -> str:
        return (
            "## Canvas\nMermaid auto-lays-out — there is no coordinate frame. "
            "Structure the graph (nodes + edges), not positions."
        )

    def json_contract(self) -> str:
        return _MERMAID_JSON_CONTRACT


#: The singleton mermaid language instance the mermaid handler/route bind.
MERMAID_LANG = MermaidLang()
