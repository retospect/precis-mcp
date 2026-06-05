"""Pure rendering helpers for `precis.handlers.python.PythonHandler`.

Every function here takes already-resolved data (Symbol, ModuleIndex,
RepoIndex) and returns a `str` body. No I/O, no DB. Keeping the
renderers separate from the handler means tests can exercise rendering
shapes without standing up a handler+cache combo.

The rendered shapes follow `docs/user-facing/python-kind-spec.md § Views`. Headings
are `#`-delimited markdown so the runtime's footer machinery (cost
line, hint bus output) appends cleanly.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from precis.python_index import ModuleIndex, RepoIndex, Symbol

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

    lines = [f"# python - {len(roots)} repo{'s' if len(roots) != 1 else ''}\n"]
    for alias, root in roots.items():
        lines.append(f"  {alias:<24} {root}")
    lines.append("")
    lines.append("Next:")
    first = next(iter(roots))
    lines.append(f"  get(kind='python', id={first!r})")
    lines.append(f"  get(kind='python', id={first!r}, view='toc')")
    return "\n".join(lines)


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
        for pkg, count in top_packages.most_common():
            lines.append(f"  {pkg:<28} {count} module{'s' if count != 1 else ''}")

    # Surface any modules that failed to parse — agents need to know.
    bad = [m for m in idx.modules.values() if m.parse_error]
    if bad:
        lines.append("")
        lines.append(f"Parse errors ({len(bad)}):")
        for m in bad[:10]:
            lines.append(f"  {m.file:<40} {m.parse_error}")
        if len(bad) > 10:
            lines.append(f"  … ({len(bad) - 10} more)")

    lines.append("")
    lines.append("Next:")
    lines.append(f"  get(kind='python', id={alias!r}, view='toc')")
    if top_packages:
        first_pkg = top_packages.most_common(1)[0][0]
        lines.append(f"  get(kind='python', id='{alias}::{first_pkg}')")
    return "\n".join(lines)


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
    for mod in files_sorted:
        depth = mod.file.count("/")
        indent = "  " * depth
        leaf = mod.file.rsplit("/", 1)[-1]
        counts = _counts_for_file(mod)
        lines.append(f"{indent}{leaf:<32} {counts}")

    lines.append("")
    lines.append("Next:")
    sample_file = files_sorted[0].file if files_sorted else None
    if sample_file:
        lines.append(f"  get(kind='python', id='{alias}/{sample_file}')")
    lines.append(f"  get(kind='python', id={alias!r}, view='symbols')")
    return "\n".join(lines)


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
    funcs = [s for s in mod.symbols if s.kind == "function"]
    if funcs:
        lines.append("  FUNCTIONS")
        for f in funcs:
            lines.append(f"    L{f.start_line:<4} {f.signature}")
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
                lines.append(f"      L{m.start_line:<4} {m.signature}")
        lines.append("")

    lines.append("Next:")
    lines.append(f"  get(kind='python', id='{alias}/{mod.file}', view='source')")
    if classes:
        sample = classes[0]
        lines.append(f"  get(kind='python', id='{alias}::{sample.qualname}')")
    elif funcs:
        sample = funcs[0]
        lines.append(f"  get(kind='python', id='{alias}::{sample.qualname}')")
    return "\n".join(lines)


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
                ln = m.signature or m.name
                lines.append(f"  L{m.start_line:<4} {ln}")
            lines.append("")

    # Static call edges originating from this symbol.
    calls_from = _calls_from_symbol(sym, idx)
    if calls_from:
        lines.append("Calls (from this symbol):")
        for callee, hits in calls_from.most_common(10):
            tag = " [ext]" if callee.startswith("ext:") else ""
            display = callee[4:] if callee.startswith("ext:") else callee
            lines.append(f"  {display:<48}{tag}  {hits}×")
        if len(calls_from) > 10:
            lines.append(f"  … ({len(calls_from) - 10} more)")
        lines.append("")

    # Static callers — every CallEdge whose callee == this qualname or a
    # prefix-match for class members.
    callers = _callers_of_symbol(sym, idx)
    if callers:
        lines.append("Called by:")
        for caller, hits in callers.most_common(10):
            lines.append(f"  {caller:<48}  {hits}×")
        if len(callers) > 10:
            lines.append(f"  … ({len(callers) - 10} more)")
        lines.append("")

    lines.append("Next:")
    lines.append(f"  get(kind='python', id='{alias}::{sym.qualname}', view='source')")
    return "\n".join(lines).rstrip() + "\n"


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
