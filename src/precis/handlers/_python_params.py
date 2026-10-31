"""``view='params'`` and the ``<alias>::<qualname>~<param>`` selector.

Renders a function/method symbol's `ParamDoc` records (built at index
time — see `precis.python_index.docparams`) as a TOON table with the
header ``params[N]{name,hint,default,doc,line}``; the doc cell carries
the first sentence only. ``~<param>`` returns one param's full doc.
Drift rows (``doc-only`` / ``undocumented``) are noted under the table.

Spec: gr520905 comments 1-2.
"""

from __future__ import annotations

from precis.errors import NotFound
from precis.format import render_agent_table
from precis.python_index.docparams import first_sentence
from precis.python_index.types import ParamDoc, Symbol

#: Column order pinned for stable output (matches the spec's header).
_PARAMS_SCHEMA = ["name", "hint", "default", "doc", "line"]


def render_params(alias: str, sym: Symbol) -> str:
    """The ``params[N]{name,hint,default,doc,line}`` TOON table."""
    rows = [
        {
            "name": p.name,
            "hint": p.hint,
            "default": p.default,
            "doc": first_sentence(p.doc),
            "line": p.line,
        }
        for p in sym.params
    ]
    body = (
        f"# {alias}::{sym.qualname} — {sym.file}:{sym.start_line}-{sym.end_line}\n"
        f"params[{len(rows)}]" + render_agent_table(rows, schema=_PARAMS_SCHEMA)
    )
    doc_only = [p.name for p in sym.params if p.drift == "doc-only"]
    undocumented = [p.name for p in sym.params if p.drift == "undocumented"]
    notes: list[str] = []
    if doc_only:
        notes.append("drift: documented, not in signature: " + ", ".join(doc_only))
    if undocumented and sym.docstring:
        notes.append("drift: no doc entry: " + ", ".join(undocumented))
    if sym.docstring is None:
        notes.append("no docstring — doc cells are empty")
    if notes:
        body += "\n" + "\n".join(notes)
    return body


def render_param(alias: str, sym: Symbol, selector: str) -> str:
    """One param's full doc — the ``id='<alias>::<qualname>~<param>'`` read."""
    name = selector.lstrip("*")
    param = _find_param(sym, name)
    if param is None:
        known = [p.name for p in sym.params]
        raise NotFound(
            f"no param {name!r} on {sym.qualname!r}"
            + (f" (params: {', '.join(known)})" if known else ""),
            next=f"get(kind='python', id='{alias}::{sym.qualname}', view='params')",
        )
    lines = [f"# {alias}::{sym.qualname}~{param.name} — {sym.file}:{param.line}"]
    meta = []
    if param.hint:
        meta.append(f"hint: {param.hint}")
    if param.default is not None:
        meta.append(f"default: {param.default}")
    if param.drift:
        meta.append(f"drift: {param.drift}")
    if meta:
        lines.append("\t".join(meta))
    lines.append("")
    lines.append(param.doc if param.doc else "(no doc entry)")
    return "\n".join(lines)


def _find_param(sym: Symbol, name: str) -> ParamDoc | None:
    for p in sym.params:
        if p.name == name:
            return p
    return None
