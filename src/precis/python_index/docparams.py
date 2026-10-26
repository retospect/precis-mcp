"""Merge a function's AST signature with its docstring's param docs.

`merge_params(node, docstring)` builds the per-param `ParamDoc`
records `view='params'` renders: signature order first
(``self``/``cls`` dropped), then documented-but-absent names in doc
order (``drift="doc-only"``). Signature args with no doc entry get
``drift="undocumented"``.

Docstring parsing is delegated to ``docstring_parser`` (auto-detects
Google, Sphinx ``:param:`` and NumPy styles) — third-party code bases
mostly document in NumPy style, ours in Google.

`first_sentence` is the doc-cell compactor: `view='params'` shows only
the lead sentence; the full doc lives behind the ``~<param>`` selector.
"""

from __future__ import annotations

import ast
import logging
import re

from docstring_parser import DocstringParam
from docstring_parser import parse as _parse_docstring

from precis.python_index.types import ParamDoc

log = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"^.*?[.!?](?=\s|$)", re.S)


def merge_params(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    docstring: str | None,
) -> tuple[ParamDoc, ...]:
    """Merge `node`'s signature with the param docs in `docstring`."""
    docmap = _doc_params(docstring)
    docstring_lineno = _docstring_lineno(node)
    merged: list[ParamDoc] = []
    for sig in _signature_params(node):
        dp = docmap.pop(sig.name, None)
        merged.append(
            ParamDoc(
                name=sig.name,
                hint=sig.hint or (dp.type_name if dp else None),
                default=sig.default or (dp.default if dp else None),
                doc=dp.description if dp else None,
                line=sig.line,
                drift=None if dp else "undocumented",
            )
        )
    for name, dp in docmap.items():
        merged.append(
            ParamDoc(
                name=name,
                hint=dp.type_name,
                default=dp.default,
                doc=dp.description,
                line=_doc_param_line(docstring, docstring_lineno, name),
                drift="doc-only",
            )
        )
    return tuple(merged)


def first_sentence(text: str | None) -> str | None:
    """The doc cell's lead sentence: up to the first ``. ! ?`` run.

    Whitespace is collapsed first so a multi-line entry still yields a
    single-line cell. No sentence-ending punctuation -> the whole
    (flattened) doc.
    """
    if not text:
        return text
    flat = " ".join(text.split())
    m = _SENTENCE_END.match(flat)
    return m.group(0) if m else flat


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _signature_params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ParamDoc]:
    """One `ParamDoc` per signature arg, in declaration order.

    Covers positional-only, positional, keyword-only, ``*args`` and
    ``**kwargs``. The leading ``self``/``cls`` is dropped — it is never
    a documented contract parameter.
    """
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    defaults: list[ast.expr | None] = [
        *([None] * (len(positional) - len(args.defaults))),
        *args.defaults,
    ]
    out: list[ParamDoc] = []
    for i, (a, d) in enumerate(zip(positional, defaults, strict=True)):
        if i == 0 and a.arg in ("self", "cls"):
            continue
        out.append(_sig_param(a, d))
    for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        out.append(_sig_param(a, d))
    for star_arg in (args.vararg, args.kwarg):
        if star_arg is not None:
            out.append(_sig_param(star_arg, None))
    return out


def _sig_param(a: ast.arg, default: ast.expr | None) -> ParamDoc:
    return ParamDoc(
        name=a.arg,
        hint=_unparse(a.annotation) if a.annotation is not None else None,
        default=_unparse(default) if default is not None else None,
        line=a.lineno,
    )


def _unparse(node: ast.expr | ast.arg) -> str:
    """Same squash as the indexer's signature rendering."""
    return " ".join(ast.unparse(node).split())


def _docstring_lineno(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int | None:
    """Line of `node`'s docstring expression (``node.body[0].lineno``)."""
    if (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ):
        return node.body[0].lineno
    return None


def _doc_params(docstring: str | None) -> dict[str, DocstringParam]:
    """`{bare_name: DocstringParam}` from any supported docstring style.

    ``*args``/``**kwargs`` doc names keep their stars in
    ``DocstringParam.arg_name``; they are normalised to the bare arg
    name here so they match the signature's names. A docstring that
    fails to parse contributes no params (parse failures never abort
    indexing).
    """
    if not docstring:
        return {}
    try:
        parsed = _parse_docstring(docstring)
    except Exception as e:  # parser must never break indexing
        log.debug("docstring parse failed: %s", e)
        return {}
    out: dict[str, DocstringParam] = {}
    for p in parsed.params:
        out.setdefault(p.arg_name.lstrip("*"), p)
    return out


def _doc_param_line(
    docstring: str | None, docstring_lineno: int | None, name: str
) -> int | None:
    """Source line where `name`'s doc entry starts (best-effort).

    Cleaned-docstring line N maps to raw docstring line N (cleandoc
    normalises margins, never drops lines), so
    ``docstring_lineno + index`` is the file line. Matches the entry's
    opening line across styles — Google ``name (t):`` / ``name:``,
    Sphinx ``:param name:``, NumPy ``name : t`` — then falls back to the
    first word-boundary mention, then the docstring's own start.
    """
    if not docstring or docstring_lineno is None:
        return None
    lines = docstring.split("\n")
    entry = re.compile(rf"^\s*(?::param\s+)?\*{{0,2}}{re.escape(name)}\s*[:(]")
    word = re.compile(rf"\b{re.escape(name)}\b")
    for i, line in enumerate(lines):
        if entry.search(line):
            return docstring_lineno + i
    for i, line in enumerate(lines):
        if word.search(line):
            return docstring_lineno + i
    return docstring_lineno
