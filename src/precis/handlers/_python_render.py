"""Pure rendering helpers for `precis.handlers.python.PythonHandler`.

Every function here takes already-resolved data (Symbol, ModuleIndex,
RepoIndex) and returns a `str` body. No I/O, no DB. Keeping the
renderers separate from the handler means tests can exercise rendering
shapes without standing up a handler+cache combo.

The rendered view shapes are the contract below. Headings
are `#`-delimited markdown so the runtime's footer machinery (cost
line, hint bus output) appends cleanly.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from precis.format import render_agent_table
from precis.python_index import ModuleIndex, RepoIndex, Symbol
from precis.utils.next_block import render_next_section

# ---------------------------------------------------------------------------
# Index — list configured roots
# ---------------------------------------------------------------------------


def render_index(roots: dict[str, Path]) -> str:
    """List every alias the handler knows about with a hint to drill in.

    `roots` is the alias→absolute-path map the handler holds.
    """
    if not roots:
        return (
            "# python - no repos configured\n\n"
            "Set `PRECIS_PYTHON_ROOTS=alias:/abs/path,alias2:/abs/path` to "
            "register one or more Python repos.\n"
        )

    from precis.format import render_agent_table

    lines = [f"# python - {len(roots)} repo{'s' if len(roots) != 1 else ''}\n"]
    lines.append(
        render_agent_table(
            [{"alias": a, "root": str(r)} for a, r in roots.items()],
            schema=["alias", "root"],
        )
    )
    first = next(iter(roots))
    body = "\n".join(lines)
    body += render_next_section(
        [
            (f"get(kind='python', id={first!r})", "repo overview"),
            (f"get(kind='python', id={first!r}, view='toc')", "package tree"),
        ]
    )
    return body


# ---------------------------------------------------------------------------
# Repo overview
# ---------------------------------------------------------------------------


def render_repo_overview(alias: str, idx: RepoIndex) -> str:
    """Quick stats + top-level packages + drill-in hints."""
    n_modules = idx.n_modules
    n_symbols = idx.n_symbols

    # Top-level packages = root-most segment of every module qualname.
    top_packages: Counter[str] = Counter()
    for qn in idx.modules:
        top_packages[qn.split(".", 1)[0]] += 1

    lines = [f"# {alias} - Python repo overview\n"]
    lines.append(f"  Root:     {idx.root}")
    lines.append(f"  Modules:  {n_modules}")
    lines.append(f"  Symbols:  {n_symbols}")
    if top_packages:
        lines.append("")
        lines.append("Top-level packages:")
        lines.append(
            render_agent_table(
                [
                    {"package": pkg, "modules": count}
                    for pkg, count in top_packages.most_common()
                ],
                schema=["package", "modules"],
            )
        )

    # Surface any modules that failed to parse — agents need to know.
    bad = [m for m in idx.modules.values() if m.parse_error]
    if bad:
        lines.append("")
        lines.append(f"Parse errors ({len(bad)}):")
        lines.append(
            render_agent_table(
                [{"file": m.file, "error": m.parse_error} for m in bad[:10]],
                schema=["file", "error"],
            )
        )
        if len(bad) > 10:
            lines.append(f"  … ({len(bad) - 10} more)")

    nxt = [(f"get(kind='python', id={alias!r}, view='toc')", "package tree")]
    if top_packages:
        first_pkg = top_packages.most_common(1)[0][0]
        nxt.append(
            (f"get(kind='python', id='{alias}::{first_pkg}')", "top package's symbols")
        )
    body = "\n".join(lines)
    body += render_next_section(nxt)
    return body


# ---------------------------------------------------------------------------
# TOC — package tree
# ---------------------------------------------------------------------------


def render_toc(alias: str, idx: RepoIndex) -> str:
    """Hierarchical package tree, with per-file class/fn counts.

    Ordering: filesystem-natural (the modules dict is already in walk
    order). Files are grouped under their package directory.
    """
    n_modules = idx.n_modules
    packages = sum(1 for m in idx.modules.values() if m.file.endswith("__init__.py"))

    lines = [
        f"# {alias} - TOC ({n_modules} module{'s' if n_modules != 1 else ''}, "
        f"{packages} package{'s' if packages != 1 else ''})\n"
    ]

    # Emit every file in sorted-by-path order, with two-space indent per
    # depth and a 3-column counts line.
    files_sorted = sorted(idx.modules.values(), key=lambda m: m.file)
    toc_rows: list[dict[str, str]] = []
    for mod in files_sorted:
        depth = mod.file.count("/")
        indent = "  " * depth
        leaf = mod.file.rsplit("/", 1)[-1]
        counts = _counts_for_file(mod)
        toc_rows.append({"file": f"{indent}{leaf}", "symbols": counts})
    lines.append(render_agent_table(toc_rows, schema=["file", "symbols"]))
    nxt = []
    sample_file = files_sorted[0].file if files_sorted else None
    if sample_file:
        nxt.append((f"get(kind='python', id='{alias}/{sample_file}')", "file outline"))
    nxt.append((f"get(kind='python', id={alias!r}, view='symbols')", "every symbol"))
    body = "\n".join(lines)
    body += render_next_section(nxt)
    return body


def _counts_for_file(mod: ModuleIndex) -> str:
    """`'2 cls, 8 fn'` style summary of a module's symbols."""
    if mod.parse_error:
        return "[parse error]"
    classes = sum(1 for s in mod.symbols if s.kind == "class")
    funcs = sum(1 for s in mod.symbols if s.kind in ("function", "method"))
    parts: list[str] = []
    if classes:
        parts.append(f"{classes} cls")
    if funcs:
        parts.append(f"{funcs} fn")
    return ", ".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# File outline
# ---------------------------------------------------------------------------


def render_file_outline(alias: str, mod: ModuleIndex) -> str:
    """Outline of one file: imports, top-level functions, classes + methods."""
    lines = [f"# {mod.file} - outline\n"]

    if mod.parse_error:
        lines.append(f"  Parse error: {mod.parse_error}")
        return "\n".join(lines)

    # Imports section (deterministic alphabetical order). Skip
    # `from __future__ import …` since those don't bind useful names.
    visible_imports = {
        bound: qn
        for bound, qn in mod.imports.items()
        if not qn.startswith("__future__.")
    }
    if visible_imports:
        lines.append("  IMPORTS")
        for bound, qn in sorted(visible_imports.items()):
            # Suppress the redundant `as X` when the bound name is just
            # the rightmost segment of the qualname (the common case).
            display = qn if qn.rsplit(".", 1)[-1] == bound else f"{qn} as {bound}"
            lines.append(f"    {display}")
        lines.append("")

    # Functions (top-level only — kind='function')
    funcs = [
        s for s in mod.symbols if s.kind == "function" and s.parent == mod.qualname
    ]
    if funcs:
        lines.append("  FUNCTIONS")
        for f in funcs:
            sig = _elide_signature(f.signature) if f.signature else f.name
            lines.append(f"    L{f.start_line:<4} {sig}")
            if f.docstring:
                lines.append(f"           {_oneline(f.docstring)}")
        lines.append("")

    # Classes with their methods.
    classes = [s for s in mod.symbols if s.kind == "class"]
    if classes:
        lines.append("  CLASSES")
        for c in classes:
            lines.append(f"    L{c.start_line:<4} {c.name}")
            if c.docstring:
                lines.append(f"           {_oneline(c.docstring)}")
            # Methods owned by this class.
            class_methods = [
                s for s in mod.symbols if s.kind == "method" and s.parent == c.qualname
            ]
            for m in class_methods:
                sig = _elide_signature(m.signature) if m.signature else m.name
                lines.append(f"      L{m.start_line:<4} {sig}")
        lines.append("")

    nxt = [(f"get(kind='python', id='{alias}/{mod.file}', view='source')", "source")]
    if classes:
        sample = classes[0]
        nxt.append(
            (f"get(kind='python', id='{alias}::{sample.qualname}')", "class detail")
        )
    elif funcs:
        sample = funcs[0]
        nxt.append(
            (f"get(kind='python', id='{alias}::{sample.qualname}')", "symbol detail")
        )
    body = "\n".join(lines)
    body += render_next_section(nxt)
    return body


# ---------------------------------------------------------------------------
# Symbol drill-down
# ---------------------------------------------------------------------------


def render_symbol(alias: str, sym: Symbol, idx: RepoIndex) -> str:
    """Drill-down on a single symbol.

    Includes header (kind + file:lines), docstring, and:

    - For classes: list of methods with their signatures and lines.
    - For functions/methods: signature is in the header line.
    - Calls *from* this symbol (resolved + ext, deduped with hit counts).
    - Calls *into* this symbol (callers across the whole repo).
    """
    lines = [
        f"# {sym.qualname}  ({sym.kind}, {sym.file}:{sym.start_line}-{sym.end_line})\n"
    ]
    if sym.signature:
        lines.append(f"  {sym.signature}\n")
    if sym.docstring:
        # Indent the docstring two spaces for readability inside the body.
        for ln in sym.docstring.splitlines():
            lines.append(f"  {ln}".rstrip())
        lines.append("")

    # If this is a class, surface its methods.
    if sym.kind == "class":
        methods = [
            s
            for s in idx.symbols_in(sym.qualname)
            if s.kind == "method" and s.parent == sym.qualname
        ]
        if methods:
            lines.append("Methods:")
            for m in methods:
                ln = _elide_signature(m.signature) if m.signature else m.name
                lines.append(f"  L{m.start_line:<4} {ln}")
            lines.append("")

    # Static call edges originating from this symbol.
    calls_from = _calls_from_symbol(sym, idx)
    if calls_from:
        lines.append("Calls (from this symbol):")
        lines.append(
            render_agent_table(
                [
                    {
                        "callee": (callee[4:] if callee.startswith("ext:") else callee),
                        "ext": "ext" if callee.startswith("ext:") else "",
                        "hits": f"{hits}×",
                    }
                    for callee, hits in calls_from.most_common(10)
                ],
                schema=["callee", "ext", "hits"],
            )
        )
        if len(calls_from) > 10:
            lines.append(f"  … ({len(calls_from) - 10} more)")
        lines.append("")

    # Static callers — every CallEdge whose callee == this qualname or a
    # prefix-match for class members.
    callers = _callers_of_symbol(sym, idx)
    if callers:
        lines.append("Called by:")
        lines.append(
            render_agent_table(
                [
                    {"caller": caller, "hits": f"{hits}×"}
                    for caller, hits in callers.most_common(10)
                ],
                schema=["caller", "hits"],
            )
        )
        if len(callers) > 10:
            lines.append(f"  … ({len(callers) - 10} more)")
        lines.append("")

    body = "\n".join(lines).rstrip() + "\n"
    body += render_next_section(
        [
            (
                f"get(kind='python', id='{alias}::{sym.qualname}', view='source')",
                "source",
            )
        ]
    )
    return body


def _calls_from_symbol(sym: Symbol, idx: RepoIndex) -> Counter[str]:
    """Counter of callees originating in `sym` (or any of its members
    if `sym` is a class — since CallEdges are per-method)."""
    counts: Counter[str] = Counter()
    target_prefix = sym.qualname + "."
    for mod in idx.modules.values():
        for edge in mod.calls:
            if edge.caller == sym.qualname or edge.caller.startswith(target_prefix):
                counts[edge.callee] += 1
    return counts


def _callers_of_symbol(sym: Symbol, idx: RepoIndex) -> Counter[str]:
    """Counter of callers whose callee matches `sym.qualname` (or starts
    with `sym.qualname.` for class-level "called by"). Self-calls within
    the symbol itself are excluded."""
    counts: Counter[str] = Counter()
    target_prefix = sym.qualname + "."
    for mod in idx.modules.values():
        for edge in mod.calls:
            hits_self = edge.callee == sym.qualname or edge.callee.startswith(
                target_prefix
            )
            if not hits_self:
                continue
            # Drop self-calls (caller is the same symbol or one of its own members).
            if edge.caller == sym.qualname or edge.caller.startswith(target_prefix):
                continue
            counts[edge.caller] += 1
    return counts


# ---------------------------------------------------------------------------
# Source rendering
# ---------------------------------------------------------------------------


def render_source(
    text: str,
    *,
    file_label: str,
    start_line: int,
    end_line: int,
) -> str:
    """Render a slice of source code with a file:line header.

    Lines are taken inclusive on both ends (1-indexed). `text` is the
    full file content; we slice locally instead of having callers
    pre-slice so the start/end conventions live in one place.
    """
    all_lines = text.splitlines()
    # Clamp to valid range.
    lo = max(start_line, 1)
    hi = min(end_line, len(all_lines))
    body = "\n".join(all_lines[lo - 1 : hi])
    return f"# {file_label}:{lo}-{hi}\n\n{body}\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _oneline(text: str, *, max_len: int = 72) -> str:
    """First non-empty line of `text`, truncated to `max_len` with `…`."""
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if len(first) > max_len:
        first = first[: max_len - 1] + "…"
    return first


def _elide_signature(sig: str, *, max_params: int = 6, max_len: int = 110) -> str:
    """Compress an overlong `def name(...) -> ret` signature for outline views.

    Outline/list views exist to compress; a 70-param signature (e.g.
    `precis.tools.core.put`) shouldn't dominate an outline's token cost.
    Full signatures still show in symbol drill-down (`render_symbol`'s
    header), which is deliberately left un-elided.

    Splits the outermost paren group's contents on top-level commas only
    (bracket-depth-tracked across `()[]{}`, so `dict[str, Any]`
    annotations and defaults with commas don't inflate the param count).
    Anything that doesn't parse as `def foo(...)` — no parens, unbalanced
    parens — is returned unchanged rather than risk a bad render.
    """
    open_idx = sig.find("(")
    if open_idx == -1:
        return sig

    depth = 0
    close_idx = -1
    for i in range(open_idx, len(sig)):
        ch = sig[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                close_idx = i
                break
    if close_idx == -1:
        return sig  # unbalanced parens

    params = [p for p in _split_top_level(sig[open_idx + 1 : close_idx]) if p.strip()]
    if len(params) <= max_params and len(sig) <= max_len:
        return sig

    n_elided = len(params) - max_params
    if n_elided <= 0:
        # Already within the param budget; whatever made this long (a
        # verbose annotation/default) won't shrink by eliding params.
        return sig

    kept = ", ".join(p.strip() for p in params[:max_params])
    suffix = sig[close_idx:]  # ")" plus any trailing " -> ret"
    return f"{sig[: open_idx + 1]}{kept}, … +{n_elided} more{suffix}"


def _split_top_level(text: str) -> list[str]:
    """Split `text` on commas outside any `()[]{}` nesting."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch in "([{":
            depth += 1
            current.append(ch)
        elif ch in ")]}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts
