"""``view='parameterize'`` — propose a parameter-extracting rewrite of a
workspace script (gr521589).

A ray hit or a trace card names a ``frame:<key>``; the frame's ``args``
digest names the value one call argument was bound to and its
``src``/``line_in_def`` locate the call site in the workspace script.
This module turns ``(script text, call-site line, arg name)`` into the
smallest parameter-extraction rewrite Gonzalez-UIST'24-style:

* a new defaulted parameter on the call site's enclosing def
  (``def bundle(H=12)``) — or, when the site sits at module level, a
  module-level assignment (``H = 12``) just above the containing
  statement;
* the literal at the call site replaced by the new name
  (``o.ray(..., length=0.028)`` → ``o.ray(..., length=LENGTH)``);
* every other occurrence of the same literal inside the same scope
  listed as a candidate — reported, never rewritten (a blind
  replace-all is the agent's call, not ours).

The result is a unified diff plus a one-line summary; nothing is
written anywhere. The ask is read-only: the agent applies the diff
through the normal script edit path (``edit(kind='plaintext')`` on the
workspace file ref, then a re-run).

Refusals (:class:`BadInput` with ``next=``) cover the honest cases: the
script does not parse, the call moved or was removed since the run
(``src`` no longer matches), the arg is already a name/expression
rather than a literal, or several call sites on the line take the arg
(so the frame's own call cannot be told apart).
"""

from __future__ import annotations

import ast
import difflib
import io
import keyword
import re
import tokenize
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, NamedTuple

from precis.errors import BadInput

_MISSING: Any = object()

_NUM = (int, float)
_SIGN_OPS = (ast.USub, ast.UAdd)


class _Lit(NamedTuple):
    """A replaceable literal: the node whose source span is rewritten,
    the value it stands for, and the source text that becomes the
    parameter's default."""

    node: ast.expr
    value: Any
    text: str


@dataclass
class ParamCandidate:
    """One other occurrence of the same literal in the same scope —
    listed, not rewritten."""

    line: int
    col: int
    text: str


@dataclass
class Rewrite:
    """The proposed parameter extraction: everything the view renders."""

    name: str
    default: str
    arg: str
    scope: str
    scope_line: int
    literal_line: int
    candidates: list[ParamCandidate]
    diff: str
    summary: str
    note: str = ""


def _signed_num(node: ast.expr) -> _Lit | None:
    """``(span, value)`` for a numeric literal unit: ``12``, ``2.5``,
    ``-4``, ``+3``. Strings/bools excluded — a bool is an int to
    ``isinstance`` but is never a dimensioned literal."""
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, _NUM)
        and not isinstance(node.value, bool)
    ):
        return _Lit(node, node.value, "")
    if (
        isinstance(node, ast.UnaryOp)
        and type(node.op) in _SIGN_OPS
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, _NUM)
        and not isinstance(node.operand.value, bool)
    ):
        v = node.operand.value
        return _Lit(node, -v if isinstance(node.op, ast.USub) else +v, "")
    return None


def _unit_side(node: ast.expr) -> bool:
    """A bare name/attribute playing the unit role in ``2.5*mm``."""
    return isinstance(node, (ast.Name, ast.Attribute))


def _literal(node: ast.expr, lines: list[str]) -> _Lit | None:
    """The replaceable literal inside one call-argument expression —
    the Constant itself, a signed number, or the numeric side of a
    ``<num>*<unit>`` / ``<unit>*<num>`` / ``<num>/<unit>`` product (the
    unit stays at the call site; the parameter carries the bare
    number). ``None`` when the expression is not a literal."""
    if isinstance(node, ast.BinOp):
        num = _signed_num(node.left)
        if (
            num is not None
            and isinstance(node.op, (ast.Mult, ast.Div))
            and _unit_side(node.right)
        ):
            return _Lit(
                num.node,
                num.value,
                _segment(lines, num.node),
            )
        num = _signed_num(node.right)
        if isinstance(node.op, ast.Mult) and _unit_side(node.left) and num is not None:
            return _Lit(num.node, num.value, _segment(lines, num.node))
        return None
    signed = _signed_num(node)
    if signed is not None:
        return _Lit(signed.node, signed.value, _segment(lines, signed.node))
    if isinstance(node, ast.Constant):
        return _Lit(node, node.value, _segment(lines, node))
    return None


def _char_col(line_text: str, byte_col: int) -> int:
    """``ast`` col offsets are UTF-8 *byte* offsets — the char column a
    ``str`` slice needs (a ``µ`` earlier on the line is one char but two
    bytes; slicing chars by bytes yields broken source)."""
    return len(line_text.encode("utf-8")[:byte_col].decode("utf-8"))


def _segment(lines: list[str], node: ast.expr) -> str:
    """The source text one node spans — single-line literals only, so a
    straight slice; multi-line falls back to ``ast.unparse``-shaped
    ``repr`` never needed here."""
    if node.end_lineno == node.lineno:
        row = lines[node.lineno - 1]
        return row[
            _char_col(row, node.col_offset) : _char_col(row, node.end_col_offset or 0)
        ]
    return ast.unparse(node)


def _digest_eq(lit_value: Any, digest: Any) -> bool:
    """Literal value ↔ the trace's ``_arg_digest`` — floats digested at
    ``%.6g``, bools kept distinct from ints."""
    if isinstance(digest, bool) or isinstance(lit_value, bool):
        return type(lit_value) is type(digest) and lit_value == digest
    if isinstance(digest, float) and isinstance(lit_value, _NUM):
        return float(f"{lit_value:.6g}") == digest
    return type(lit_value) is type(digest) and lit_value == digest


def _enclosing(tree: ast.AST, line: int) -> ast.stmt | ast.expr | None:
    """The innermost def/lambda/class whose span covers ``line`` —
    ``None`` means module scope."""
    best: ast.stmt | ast.expr | None = None
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            continue
        end = node.end_lineno or node.lineno
        if node.lineno <= line <= end and (
            best is None
            or (node.lineno, -(end)) > (best.lineno, -(best.end_lineno or 0))
        ):
            best = node
    return best


def _scope_name(tree: ast.AST, scope: ast.AST, stem: str) -> str:
    """``main.build`` — stem plus the def's nested qualname path."""

    def visit(node: ast.AST, prefix: str, target: ast.AST) -> str | None:
        for child in ast.iter_child_nodes(node):
            if child is target:
                return f"{prefix}.{getattr(child, 'name', '?')}"
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                sub = prefix + f".{child.name}"
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sub += ".<locals>"
                got = visit(child, sub, target)
                if got is not None:
                    return got
        return None

    got = visit(tree, stem, scope)
    return got or str(getattr(scope, "name", stem) or stem)


def _line_offsets(script: str) -> list[int]:
    offs = []
    n = 0
    for line in script.splitlines(keepends=True):
        offs.append(n)
        n += len(line)
    return offs


class _Sig(NamedTuple):
    """Insertion edits ``[(offset, text)]`` for the new parameter."""

    edits: list[tuple[int, str]]


def _sig_insert(
    script: str, node: ast.FunctionDef | ast.AsyncFunctionDef, name: str, default: str
) -> _Sig:
    """Where ``{name}={default}`` lands in ``node``'s signature. Rules:
    before ``**kw`` when present (it must stay last); else before the
    close paren — a trailing-comma tail takes the bare ``name=default``
    (the comma is already there), a wrapped signature whose close paren
    sits on its own line gets its own ``name=default,`` line matching
    the last argument's indent, and a missing trailing comma is added
    first so the splice never produces ``a\\nname=default``."""
    toks = list(tokenize.generate_tokens(io.StringIO(script).readline))
    open_i = next(
        i
        for i, t in enumerate(toks)
        if t.type == tokenize.OP and t.string == "(" and t.start[0] >= node.lineno
    )
    depth = 0
    close_i = -1
    splat_i = -1
    prev: tokenize.TokenInfo | None = None
    last_arg_tok: tokenize.TokenInfo | None = None
    last_comment_tok: tokenize.TokenInfo | None = None
    for i in range(open_i, len(toks)):
        t = toks[i]
        if t.type == tokenize.OP:
            if t.string == "(":
                depth += 1
            elif t.string == ")":
                depth -= 1
                if depth == 0:
                    close_i = i
                    break
            elif (
                depth == 1
                and t.string == "**"
                and splat_i < 0
                and (prev is None or prev.string in ("(", ","))
            ):
                splat_i = i
        elif t.type == tokenize.COMMENT and depth >= 1:
            last_comment_tok = t
        if (
            t.type
            not in (
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.COMMENT,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.ENCODING,
            )
            and i != open_i
        ):
            prev = t
            if depth == 1:
                last_arg_tok = t
    assert close_i >= 0  # ast.parse already proved the parens balance
    lines = script.splitlines(keepends=True)
    offs = _line_offsets(script)

    def pos(t: tokenize.TokenInfo) -> int:
        return offs[t.start[0] - 1] + t.start[1]

    def end_pos(t: tokenize.TokenInfo) -> int:
        return offs[t.end[0] - 1] + t.end[1]

    open_tok, close_tok = toks[open_i], toks[close_i]
    tail = script[end_pos(open_tok) : pos(close_tok)].rstrip()

    if splat_i >= 0:
        return _Sig([(pos(toks[splat_i]), f"{name}={default}, ")])

    if not tail:
        return _Sig([(end_pos(open_tok), f"{name}={default}")])

    close_line = lines[close_tok.start[0] - 1]
    if close_line.lstrip().startswith(")"):
        # Wrapped signature, ``)`` on its own line: the new parameter
        # takes a whole line at the last argument's indent.
        arg_indent = "    "
        if last_arg_tok is not None:
            m = re.match(r"[ \t]*", lines[last_arg_tok.start[0] - 1])
            arg_indent = m.group(0) if m else "    "
        edits: list[tuple[int, str]] = []
        code_end = (
            pos(last_comment_tok) if last_comment_tok is not None else pos(close_tok)
        )
        code_tail = script[end_pos(open_tok) : code_end].rstrip()
        if not code_tail.endswith(","):
            # ``a\n):`` needs its own comma before the new parameter line —
            # appended after the last ARGUMENT, never inside a trailing
            # comment (gr521589).
            edits.append((end_pos(open_tok) + len(code_tail), ","))
        edits.append(
            (
                offs[close_tok.start[0] - 1],
                f"{arg_indent}{name}={default},\n",
            )
        )
        return _Sig(edits)

    # ``)`` shares its line with arguments. A comment inside the
    # signature tail owns everything after it — splice the new parameter
    # BEFORE the comment, or the comma lands in the comment text and the
    # rewrite stops parsing (gr521589).
    if last_comment_tok is not None:
        code_tail = script[end_pos(open_tok) : pos(last_comment_tok)].rstrip()
        lead = "" if not code_tail or code_tail.endswith(",") else ","
        return _Sig([(pos(last_comment_tok), f"{lead} {name}={default} ".lstrip(" "))])
    if tail.endswith(","):
        return _Sig([(pos(close_tok), f" {name}={default}")])
    return _Sig([(pos(close_tok), f", {name}={default}")])


def _module_insert(
    script: str, tree: ast.AST, line: int, name: str, default: str
) -> tuple[list[tuple[int, str]], int]:
    """``{name} = {default}`` on its own line above the top-level
    statement containing ``line`` (decorators count as part of their
    statement so the splice never splits ``@dec`` from ``def``)."""
    stmt = None
    for child in ast.iter_child_nodes(tree):
        if not isinstance(child, ast.stmt):
            continue
        end = child.end_lineno or child.lineno
        start = min(
            [child.lineno, *(d.lineno for d in getattr(child, "decorator_list", []))]
        )
        if start <= line <= end:
            stmt = child
            break
    assert stmt is not None  # ``line`` is in range — a statement covers it
    start = min([stmt.lineno, *(d.lineno for d in getattr(stmt, "decorator_list", []))])
    offs = _line_offsets(script)
    return [(offs[start - 1], f"{name} = {default}\n")], start


def _name_for(arg: str, script_tree: ast.AST) -> str:
    """``arg`` → a valid UPPER identifier unused anywhere in the script
    (conservative: avoids shadowing a global, not just scope locals)."""
    base = re.sub(r"\W", "_", arg).upper() or "P"
    if not (base[0].isalpha() or base[0] == "_"):
        base = "_" + base
    taken = {n.id for n in ast.walk(script_tree) if isinstance(n, ast.Name)}
    taken |= {a.arg for n in ast.walk(script_tree) for a in _args_of(n)}
    # def/class/import names are identifiers too — a parameter named
    # after any of them rebinding the module name silently hijacks it.
    taken |= {
        n.name
        for n in ast.walk(script_tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    taken |= {
        al.asname or al.name.split(".")[0]
        for n in ast.walk(script_tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for al in n.names
    }
    name, i = base, 2
    while name in taken or keyword.iskeyword(name.lower()):
        name = f"{base}_{i}"
        i += 1
    return name


def _args_of(node: ast.AST) -> list[ast.arg]:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return []
    a = node.args
    out = list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
    for v in (a.vararg, a.kwarg):
        if v is not None:
            out.append(v)
    return out


def _same_literals(scope: ast.AST, target: Any, skip: ast.AST) -> list[ParamCandidate]:
    """Other literal units inside ``scope`` evaluating to ``target``
    (same type — ``12`` ≠ ``12.0`` ≠ ``True``), excluding the node
    being rewritten. A ``-4`` under a sign unary counts only against a
    negative target; its inner ``4`` does not count against a positive
    one."""
    parents: dict[int, ast.AST] = {}
    for n in ast.walk(scope):
        for ch in ast.iter_child_nodes(n):
            parents[id(ch)] = n
    out: list[ParamCandidate] = []
    for n in ast.walk(scope):
        lit: _Lit | None = None
        if isinstance(n, ast.Constant):
            p = parents.get(id(n))
            if (
                isinstance(p, ast.UnaryOp)
                and type(p.op) in _SIGN_OPS
                and p.operand is n
            ):
                continue  # the unary parent is the literal unit
            lit = _Lit(n, n.value, "")
        elif (
            isinstance(n, ast.UnaryOp)
            and type(n.op) in _SIGN_OPS
            and isinstance(n.operand, ast.Constant)
            and isinstance(n.operand.value, _NUM)
            and not isinstance(n.operand.value, bool)
        ):
            v = n.operand.value
            lit = _Lit(n, -v if isinstance(n.op, ast.USub) else +v, "")
        if lit is None or n is skip:
            continue
        if type(lit.value) is type(target) and lit.value == target:
            out.append(ParamCandidate(lit.node.lineno, lit.node.col_offset, ""))
    return out


def _resolve_line(
    lines: list[str],
    line: int,
    src: str | None,
    file: str,
    script: str,
    frame: dict[str, Any] | None = None,
) -> int:
    """The current call-site line: ``line`` when the stored ``src``
    still matches it, else the unique line in the file that does. When
    the text matches SEVERAL lines and the caller supplies the frame,
    :func:`precis_se.traceview.reanchor` resolves it through the frame's
    own def + ``line_in_def`` offset before the ambiguity refusal."""
    if src is None:
        if not 1 <= line <= len(lines):
            raise BadInput(
                f"call-site line {line} is outside {file} ({len(lines)} lines)",
                next="re-run the workspace — the frame predates the current script",
            )
        return line
    want = src.strip()[:120]
    if 1 <= line <= len(lines) and lines[line - 1].strip()[:120] == want:
        return line
    hits = [i + 1 for i, l in enumerate(lines) if l.strip()[:120] == want]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1 and frame is not None:
        from precis_se import traceview as tv

        anchor = tv.reanchor({**frame, "file": file, "src": src}, script)
        if anchor.granularity == "line" and anchor.line is not None:
            return anchor.line
    if not hits:
        raise BadInput(
            f"the frame's call line moved or was removed from {file} "
            f"(was line {line}: {want!r})",
            next="re-run the workspace, then parameterize the fresh frame",
        )
    raise BadInput(
        f"the frame's call line {want!r} matches {len(hits)} lines in "
        f"{file} ({', '.join(map(str, hits))}) — cannot tell which is "
        "the frame's call",
        next="re-run the workspace, then parameterize the fresh frame",
    )


def parameterize(
    script: str,
    line: int,
    arg: str,
    *,
    value: Any = _MISSING,
    src: str | None = None,
    filename: str = "script.py",
    frame: dict[str, Any] | None = None,
) -> Rewrite:
    """Propose the parameter-extracting rewrite for ``arg`` at the call
    site on ``line`` — pure: script text in, a :class:`Rewrite` (unified
    diff + summary + candidates) out. ``value`` is the frame's arg
    digest (needed to locate the literal when the arg is positional);
    ``src`` the frame's stripped source line (drift check); ``filename``
    only names the diff header."""
    file = filename or "script.py"
    try:
        tree = ast.parse(script)
    except (SyntaxError, ValueError) as exc:
        raise BadInput(
            f"{file} does not parse ({exc})",
            next="fix the script first — parameterize rewrites valid Python only",
        ) from exc
    lines = script.splitlines()
    line = _resolve_line(lines, line, src, file, script, frame)

    # The call sites on the line taking ``arg``.
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and n.lineno <= line <= (n.end_lineno or n.lineno)
    ]
    keyed = [
        (c, kw)
        for c in calls
        for kw in c.keywords
        if kw.arg == arg and kw.value is not None
    ]
    target: _Lit | None = None
    if len(keyed) > 1:
        literal_keyed = [(c, kw) for c, kw in keyed if _literal(kw.value, lines)]
        if len(literal_keyed) == 1:
            keyed = literal_keyed
        else:
            raise BadInput(
                f"{len(keyed)} call sites on {file}:{line} take "
                f"{arg!r} — cannot tell which the frame made",
                next="pick a frame whose call sits on its own line",
            )
    if keyed:
        kw = keyed[0][1]
        target = _literal(kw.value, lines)
        if target is None:
            shape = (
                f"the name {kw.value.id!r} — already a variable"
                if isinstance(kw.value, ast.Name)
                else (
                    "a call expression"
                    if isinstance(kw.value, ast.Call)
                    else "an expression, not a literal"
                )
            )
            raise BadInput(
                f"arg {arg!r} at {file}:{line} is {shape} — "
                "parameterize extracts a literal",
                next="pick an arg passed a literal (the frame's args "
                "digest shows what was passed), or name the existing "
                "variable directly",
            )
    else:
        # Positional: the literal is the positional arg whose value
        # digests to the frame's recorded value.
        positional = [
            (c, a) for c in calls for a in c.args if not isinstance(a, ast.Starred)
        ]
        hits = []
        if value is not _MISSING:
            for c, a in positional:
                lit = _literal(a, lines)
                if lit is not None and _digest_eq(lit.value, value):
                    hits.append((c, a, lit))
        if len(hits) != 1:
            what = (
                f"no call site on {file}:{line} passes {arg!r} a literal"
                if not hits
                else f"{len(hits)} literals on {file}:{line} match "
                f"{arg!r}'s recorded value"
            )
            raise BadInput(
                f"{what} — the arg is positional, already a "
                "variable/expression, or the frame moved",
                next="pass the arg by keyword at the call site, or pick "
                "an arg the frame recorded as a scalar",
            )
        _c, _a, target = hits[0]

    # The scope that takes the parameter — innermost def, else module.
    scope = _enclosing(tree, line)
    if isinstance(scope, ast.Lambda):
        raise BadInput(
            f"the call site sits inside a lambda at {file}:{line} — a "
            "lambda has no named signature to extend",
            next="name the helper a def, or parameterize its caller",
        )
    stem = PurePath(file).stem
    edits: list[tuple[int, str]] = []
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        in_sig = any(target.node is d for d in ast.walk(scope.args))
        if in_sig:
            raise BadInput(
                f"the literal is inside {scope.name}'s own signature "
                f"({file}:{line}) — a parameter default cannot name "
                "another parameter",
                next="parameterize the call's caller instead",
            )
        scope_name = _scope_name(tree, scope, stem)
        taken_tree = tree
        name = _name_for(arg, taken_tree)
        edits = _sig_insert(script, scope, name, target.text).edits
        scope_line = scope.lineno
        cand_scope: ast.AST = scope
    else:
        scope_name = "<module>"
        name = _name_for(arg, tree)
        edits, scope_line = _module_insert(script, tree, line, name, target.text)
        cand_scope = tree

    offs = _line_offsets(script)
    lit_row = lines[target.node.lineno - 1]
    lit_start = offs[target.node.lineno - 1] + _char_col(
        lit_row, target.node.col_offset
    )
    lit_end_row = lines[(target.node.end_lineno or target.node.lineno) - 1]
    lit_end = offs[(target.node.end_lineno or target.node.lineno) - 1] + _char_col(
        lit_end_row, target.node.end_col_offset or 0
    )
    # Point inserts and the literal's span replace, all against original
    # offsets — applying in descending order keeps them valid.
    spans = [(off, off, text) for off, text in edits]
    spans.append((lit_start, lit_end, name))
    new = script
    for s, e, text in sorted(spans, key=lambda x: -x[0]):
        new = new[:s] + text + new[e:]
    try:
        ast.parse(new)  # the proposal must stay valid Python
    except (SyntaxError, ValueError) as exc:
        raise BadInput(
            f"the proposed rewrite of {file} does not parse ({exc}) — "
            "the splice hit an edge this proposer cannot handle",
            next="apply the literal edit by hand (edit the workspace "
            "script, then re-run), or re-run first and parameterize the "
            "fresh frame",
        ) from exc

    diff = "".join(
        difflib.unified_diff(
            script.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{file}",
            tofile=f"b/{file}",
        )
    )
    candidates = _same_literals(cand_scope, target.value, target.node)
    for cand in candidates:
        cand.text = lines[cand.line - 1].strip()[:120]
    note = ""
    if (
        value is not _MISSING
        and not _digest_eq(target.value, value)
        and not (isinstance(value, str) and value.startswith("<"))
    ):
        note = (
            f"note: the script's literal is {target.text!r} but the "
            f"frame recorded {value!r} — the script drifted since the "
            "run"
        )
    summary = (
        f"{name} = {target.text} on {scope_name} "
        f"({file}:{scope_line}); {arg}'s literal at {file}:{line} "
        f"becomes {name}"
    )
    if candidates:
        summary += f"; {len(candidates)} other occurrence(s) listed"
    return Rewrite(
        name=name,
        default=target.text,
        arg=arg,
        scope=scope_name,
        scope_line=scope_line,
        literal_line=line,
        candidates=candidates,
        diff=diff,
        summary=summary,
        note=note,
    )


__all__ = ["ParamCandidate", "Rewrite", "parameterize"]
