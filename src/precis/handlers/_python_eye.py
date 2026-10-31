"""The fisheye ladder for the python kind — ``get(kind='python',
id='<alias>::<qualname>', extent=<rung>)`` (gr498663).

Same rung semantics as the refs ladder (``precis-fisheye-help``): each
rung strictly contains the previous, the focus comes **in full**, ring 1
at *signature + first docstring line*, ring 2 at *names only*. Only the
surroundings change shape — the index has no refs rows, so a code eye's
neighbourhood is the module's member list (the reading-order analogue,
the ``fisheye`` rung) plus the *code edge ring*: resolved callers and
callees, the enclosing package's ``__init__`` gloss and the tests that
reference the focus (``fisheye+1hop``), then the second hop as bare
names (``fisheye+2hop`` — each first-hop edge member's own neighbours
and the other top-level symbols of the files those neighbours live in).

Every neighbourhood section carries a stated character budget (~4 chars
per token) with a visible ``… +N more`` overflow line, mirroring the
``+N more`` caps of the link rings — never a silent truncation. The
focus card itself is unbudgeted: it is what the eye is *for*.

Pure rendering — no I/O beyond the index the handler already holds.
"""

from __future__ import annotations

from collections.abc import Iterable

from precis.handlers import _python_tests as ptests
from precis.handlers._python_reverse import _import_target
from precis.python_index import ModuleIndex, RepoIndex, Symbol
from precis.workers.working_set import Extent

#: Section budgets, in characters (~4 per token). The focus card is the
#: eye's object — rings are what is budgeted.
_SPAN_CAP = 3000
_RING1_CAP = 3500
_RING2_CAP = 2000

_RING1_TESTS_CAP = 8


def _sig(sym: Symbol) -> str:
    """The one-line callable form — signature, else name + kind."""
    return sym.signature or f"{sym.name} ({sym.kind})"


def _docline(sym: Symbol) -> str:
    """The first non-empty docstring line, else ''."""
    if sym.docstring:
        return next((ln.strip() for ln in sym.docstring.splitlines() if ln.strip()), "")
    return ""


def _row(sym: Symbol, alias: str | None = None) -> str:
    """A ring-1 row: ``<alias>::<qn>  <sig> — <docline>``."""
    head = f"{alias}::{sym.qualname}" if alias else sym.qualname
    doc = _docline(sym)
    return f"  {head}  {_sig(sym)}" + (f" — {doc}" if doc else "")


def _fit(lines: list[str], cap: int, *, more_hint: str = "") -> list[str]:
    """Budget a section's rows: keep whole rows under ``cap`` chars,
    then a visible overflow line — never a silent truncation."""
    out: list[str] = []
    used = 0
    for i, ln in enumerate(lines):
        if used + len(ln) + 1 > cap and out:
            hint = f" — {more_hint}" if more_hint else ""
            out.append(f"  … +{len(lines) - i} more{hint}")
            return out
        out.append(ln)
        used += len(ln) + 1
    return out


def _package_module(mod: ModuleIndex, idx: RepoIndex) -> ModuleIndex | None:
    """The ``__init__`` module of the package ``mod`` lives in, if any."""
    if "." not in mod.qualname:
        return None
    return idx.module(mod.qualname.rsplit(".", 1)[0])


def _callers_of(sym: Symbol, idx: RepoIndex) -> list[str]:
    """Resolved in-repo caller qualnames (``_python_render``'s counter)."""
    from precis.handlers import _python_render as render

    return [qn for qn, _ in render._callers_of_symbol(sym, idx).most_common()]


def _callees_of(sym: Symbol, idx: RepoIndex) -> tuple[list[str], list[str]]:
    """(resolved in-repo callees, ext names) — the focus's own calls."""
    from precis.handlers import _python_render as render

    resolved: list[str] = []
    ext: list[str] = []
    for qn, _ in render._calls_from_symbol(sym, idx).most_common():
        if qn.startswith("ext:"):
            ext.append(qn.removeprefix("ext:"))
        else:
            resolved.append(qn)
    return resolved, ext


def _edge_neighbours(qn: str, idx: RepoIndex) -> Iterable[str]:
    """Every in-repo qualname one call edge from ``qn``, either
    direction, ``ext:`` names excluded."""
    for mod in idx.modules.values():
        for edge in mod.calls:
            if edge.caller == qn and not edge.callee.startswith("ext:"):
                yield edge.callee
            if edge.callee == qn and not edge.caller.startswith("ext:"):
                yield edge.caller


def _module_neighbours(mod_qn: str, idx: RepoIndex) -> Iterable[str]:
    """Module-level code edges for the module eye: modules ``mod_qn``
    imports (module-scope or function-local) and modules importing it."""
    for other in idx.modules.values():
        if other.qualname == mod_qn:
            continue
        targets = {_import_target(v, idx) for v in other.imports.values()}
        targets |= {_import_target(v, idx) for _f, _b, v in other.local_imports}
        if mod_qn in targets:
            yield other.qualname


def _module_span(lines: list[str], sym: Symbol, mod: ModuleIndex) -> None:
    """The ``fisheye`` spatial neighborhood: the focus inside its
    module's member list, source order, one line per member."""
    members = [s for s in mod.symbols if s.kind != "module"]
    rows: list[str] = []
    for s in sorted(members, key=lambda s: s.start_line):
        doc = _docline(s)
        mark = "▸ " if s.qualname == sym.qualname else "  "
        rows.append(
            f"{mark}L{s.start_line:<4} {_sig(s)}" + (f" — {doc}" if doc else "")
        )
    lines.append(
        f"\n— in {mod.qualname} ({mod.file}), {len(members)} member"
        f"{'s' if len(members) != 1 else ''}; budget ≤{_SPAN_CAP} chars —"
    )
    doc = _docline(mod.module_symbol)
    if doc:
        lines.append(f"  {doc}")
    lines.extend(_fit(rows, _SPAN_CAP, more_hint="view='outline' lists the file"))


def _package_span(lines: list[str], mod: ModuleIndex, idx: RepoIndex) -> None:
    """A module focus's spatial neighborhood: sibling modules of the
    same package, one line each (name + first docstring line)."""
    pkg = _package_module(mod, idx)
    depth = mod.qualname.count(".")
    prefix = (pkg.qualname + ".") if pkg is not None else ""
    sibs = [
        m
        for m in idx.modules.values()
        if m.qualname != mod.qualname
        and m.qualname.count(".") == depth
        and (not prefix or m.qualname.startswith(prefix))
    ]
    if not sibs:
        return
    label = pkg.qualname if pkg is not None else "(top level)"
    lines.append(f"\n— sibling modules of {label}; budget ≤{_SPAN_CAP} chars —")
    rows = [
        f"  {m.qualname}"
        + (f" — {_docline(m.module_symbol)}" if _docline(m.module_symbol) else "")
        for m in sorted(sibs, key=lambda m: m.qualname)
    ]
    lines.extend(_fit(rows, _SPAN_CAP, more_hint="view='toc' lists the repo"))


def _package_line(pkg: ModuleIndex | None) -> str | None:
    """The enclosing package's ``__init__`` gloss, one line."""
    if pkg is None:
        return None
    doc = _docline(pkg.module_symbol)
    return f'Package {pkg.qualname} — "{doc}"' if doc else None


def _tests_rows(
    hits: list[ptests.TestHit], *, cap: int = _RING1_TESTS_CAP
) -> list[str]:
    rows = [f"  {h.nodeid}  [{h.confidence}]  {h.evidence}" for h in hits[:cap]]
    if len(hits) > cap:
        rows.append(f"  … +{len(hits) - cap} more — view='tests' ranks the full ladder")
    return rows


def _ring1_symbol(
    lines: list[str],
    alias: str,
    sym: Symbol,
    mod: ModuleIndex,
    idx: RepoIndex,
    hits: list[ptests.TestHit],
    placed: list[str] | None = None,
) -> list[str]:
    """The ``fisheye+1hop`` code ring for a symbol focus: callers,
    callees, the package gloss, the tests — each at signature + first
    docstring line. Returns the edge members for the second hop.
    ``placed`` is the workspace Placed group (``ws-<slug>`` roots only;
    structure-workspaces slice 2p), rendered after Tests."""
    body: list[str] = []
    seeds: list[str] = []

    if line := _package_line(_package_module(mod, idx)):
        body.append(line)

    caller_syms: list[Symbol] = []
    n_test_callers = 0
    for qn in _callers_of(sym, idx):
        csym = idx.symbol(qn)
        if csym is None:
            continue
        if ptests.is_test_path(csym.file):
            n_test_callers += 1  # covered under Tests below
            continue
        caller_syms.append(csym)
    if caller_syms:
        body.append("Callers:")
        body.extend(_row(s, alias) for s in caller_syms)
        seeds.extend(s.qualname for s in caller_syms)

    callees, ext = _callees_of(sym, idx)
    callee_syms = [s for qn in callees if (s := idx.symbol(qn)) is not None]
    if callee_syms:
        body.append("Calls:")
        body.extend(_row(s, alias) for s in callee_syms)
        seeds.extend(s.qualname for s in callee_syms)
    if ext:
        body.append("  ext: " + ", ".join(sorted(set(ext))[:10]))

    if hits:
        body.append("Tests:")
        body.extend(_tests_rows(hits))
    if placed:
        body.append("Placed:")
        body.extend(f"  {r}" for r in placed)
    if n_test_callers:
        body.append(
            f"  ({n_test_callers} test caller{'s' if n_test_callers != 1 else ''}"
            " under Tests)"
        )
    if not body:
        return seeds
    lines.append(f"\n— ring 1: sig + first doc line; budget ≤{_RING1_CAP} chars —")
    lines.extend(_fit(body, _RING1_CAP, more_hint="view='callers'/'tests' expand"))
    return seeds


def _ring1_module(
    lines: list[str],
    alias: str,
    mod: ModuleIndex,
    idx: RepoIndex,
    hits: list[ptests.TestHit],
) -> list[str]:
    """The same ring for a module focus: importers and imports stand in
    for callers/callees (the module-level code edges)."""
    body: list[str] = []
    seeds: list[str] = []

    if line := _package_line(_package_module(mod, idx)):
        body.append(line)

    importers: list[tuple[str, str]] = []
    for other in idx.modules.values():
        if other.qualname == mod.qualname:
            continue
        names = [
            b
            for b, v in other.imports.items()
            if _import_target(v, idx) == mod.qualname
        ]
        names += [
            f"{b} (in {fn.removeprefix(other.qualname + '.')})"
            for fn, b, v in other.local_imports
            if _import_target(v, idx) == mod.qualname
        ]
        if names:
            importers.append((other.qualname, ", ".join(sorted(names))))
    if importers:
        body.append("Importers:")
        for qn, bound_names in sorted(importers):
            body.append(f"  {alias}::{qn}  [{bound_names}]")
            seeds.append(qn)

    imports = sorted(
        {_import_target(v, idx) for v in mod.imports.values()}
        | {_import_target(v, idx) for _f, _b, v in mod.local_imports}
    )
    imports = [t for t in imports if t in idx.modules and t != mod.qualname]
    if imports:
        body.append("Imports:")
        for qn in imports:
            m2 = idx.module(qn)
            doc = _docline(m2.module_symbol) if m2 else ""
            body.append(f"  {alias}::{qn}" + (f" — {doc}" if doc else ""))
            seeds.append(qn)

    if hits:
        body.append("Tests:")
        body.extend(_tests_rows(hits))
    if not body:
        return seeds
    lines.append(f"\n— ring 1: sig + first doc line; budget ≤{_RING1_CAP} chars —")
    lines.extend(_fit(body, _RING1_CAP, more_hint="view='importers' expands"))
    return seeds


def _ring2_symbols(
    lines: list[str], seeds: list[str], seen: set[str], idx: RepoIndex
) -> None:
    """``fisheye+2hop`` for a symbol focus: one call edge further than
    ring 1, then the rest of the files that edge lands in — names only."""
    second = {n for qn in seeds for n in _edge_neighbours(qn, idx) if n not in seen}
    if not second:
        return
    mates: set[str] = set()
    for qn in second:
        sym = idx.symbol(qn)
        mod = idx.file(sym.file) if sym is not None else None
        if mod is None:
            continue
        for s in mod.symbols:
            if s.kind != "module" and s.qualname not in seen | second:
                mates.add(s.qualname)
    lines.append(f"\n— ring 2: names only; budget ≤{_RING2_CAP} chars —")
    rows = ["  second hop: " + ", ".join(sorted(second))]
    if mates:
        rows.append("  their modules: " + ", ".join(sorted(mates)))
    lines.extend(_fit(rows, _RING2_CAP))


def _ring2_modules(
    lines: list[str], seeds: list[str], seen: set[str], idx: RepoIndex
) -> None:
    """``fisheye+2hop`` for a module focus: modules one import edge
    further, named with their top-level members."""
    second = {n for qn in seeds for n in _module_neighbours(qn, idx) if n not in seen}
    if not second:
        return
    lines.append(f"\n— ring 2: names only; budget ≤{_RING2_CAP} chars —")
    rows: list[str] = []
    for qn in sorted(second):
        mod = idx.module(qn)
        if mod is None:
            continue
        members = ", ".join(s.name for s in mod.symbols if s.kind != "module")
        rows.append(f"  {qn}: {members}" if members else f"  {qn}")
    lines.extend(_fit(rows, _RING2_CAP))


def _card(lines: list[str], alias: str, sym: Symbol, idx: RepoIndex) -> None:
    """The focus, in full: header + decorators + signature + the whole
    docstring (a class adds its method list, a module its outline)."""
    lines.append(
        f"▸ {alias}::{sym.qualname}  ({sym.kind}, {sym.file}:{sym.start_line}-{sym.end_line})\n"
    )
    for dec in sym.decorators:
        lines.append(f"  @{dec}")
    if sym.signature:
        lines.append(f"  {sym.signature}")
    if sym.docstring:
        lines.append("")
        for ln in sym.docstring.splitlines():
            lines.append(f"  {ln}".rstrip())
    if sym.kind == "class":
        methods = [
            s
            for s in idx.symbols_in(sym.qualname)
            if s.kind == "method" and s.parent == sym.qualname
        ]
        if methods:
            lines.append("\n  Methods:")
            lines.extend(f"    L{m.start_line:<4} {_sig(m)}" for m in methods)
    if sym.kind == "module":
        members = [s for s in idx.symbols_in(sym.qualname) if s.kind != "module"]
        if members:
            lines.append("\n  Members:")
            for s in sorted(members, key=lambda s: s.start_line):
                doc = _docline(s)
                lines.append(
                    f"    L{s.start_line:<4} {_sig(s)}" + (f" — {doc}" if doc else "")
                )


def render_eye(
    *,
    alias: str,
    sym: Symbol,
    idx: RepoIndex,
    extent: Extent,
    hits: list[ptests.TestHit],
    placed: list[str] | None = None,
) -> str:
    """The python eye on ``sym`` at ``extent`` — the body
    ``PythonHandler.eye`` returns. ``hits`` is the focus's ranked test
    list (``_python_tests.discover_tests``), computed by the handler.
    ``placed`` is the ws-root Placed group (pre-rendered rows)."""
    mod = idx.file(sym.file)
    assert mod is not None
    head = f"# eye on {alias}::{sym.qualname} — {extent.label}"
    lines: list[str] = []

    if extent <= Extent.TOC:  # kwd (or a collapsed NONE) — a bookmark
        doc = _docline(sym)
        lines.append(
            f"▸ {alias}::{sym.qualname}  ({sym.kind}, {sym.file}:{sym.start_line}-{sym.end_line})"
            + (f" — {doc}" if doc else "")
        )
        return head + "\n" + "\n".join(lines)

    if extent is Extent.SUMMARY:
        doc = _docline(sym)
        lines.append(
            f"▸ {alias}::{sym.qualname}  {_sig(sym)}" + (f"\n  {doc}" if doc else "")
        )
        return head + "\n" + "\n".join(lines)

    _card(lines, alias, sym, idx)  # verbatim and up: the focus in full

    seeds: list[str] = []
    seen: set[str] = {s.qualname for s in mod.symbols}
    if extent >= Extent.FIDELITY:
        if sym.kind == "module":
            _package_span(lines, mod, idx)
        else:
            _module_span(lines, sym, mod)
    if extent >= Extent.HOP1:
        if sym.kind == "module":
            seeds = _ring1_module(lines, alias, mod, idx, hits)
            seen.update(s.qualname for s in idx.symbols_in(mod.qualname))
        else:
            seeds = _ring1_symbol(lines, alias, sym, mod, idx, hits, placed=placed)
        seen.update(seeds)
    if extent >= Extent.HOP2:
        if sym.kind == "module":
            _ring2_modules(lines, seeds, seen | {mod.qualname}, idx)
        else:
            _ring2_symbols(lines, seeds, seen, idx)

    from precis.utils.next_block import render_next_section

    lines.append(
        render_next_section(
            [
                (
                    f"get(kind='python', id='{alias}::{sym.qualname}', view='source')",
                    "source",
                )
            ]
        )
    )
    return head + "\n" + "\n".join(lines)
