"""memory-closure — static import closure of the precis memory subsystem.

Read-only analysis for td605609 (slice 0 of docs/backlog/memory-ablation-bench.md):
can the memory stack be extracted into a small demo repo?  Parses every .py
under src/precis with ``ast`` (nothing is imported), builds the intra-package
import graph, and reports the transitive closure from the memory entry modules.

Usage:

    uv run python -I scripts/_memory_closure.py [--out /tmp/memory-closure-report.md]

Two closures are computed: ``all`` (every import statement, including ones
inside functions and ``if TYPE_CHECKING``) and ``eager`` (only imports that run
at module import time: not inside a function body, not under TYPE_CHECKING).
Verdict numbers use ``all`` (conservative: lazy imports still need the module
installed when the code path runs).
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
PKG = "precis"

ENTRY_NAMES = [
    "precis.store",
    "precis.utils.eye_render",
    "precis.utils.ref_hybrid",
    "precis.utils.search_merge",
    "precis.handlers.memory",
    "precis.handlers.paper",
    "precis.handlers.skill",
    "precis.tools.core",
]
# Resolved by name: (label, predicate on module name)
ENTRY_PATTERNS = [
    (
        "ingest chunker",
        lambda m: m.startswith("precis.ingest.") and "chunk" in m.rsplit(".", 1)[-1],
    ),
    (
        "worker chunk_keywords",
        lambda m: (
            m.startswith("precis.workers.") and m.rsplit(".", 1)[-1] == "chunk_keywords"
        ),
    ),
    (
        "worker summar*",
        lambda m: (
            m.startswith("precis.workers.")
            and m.rsplit(".", 1)[-1].startswith("summar")
        ),
    ),
    (
        "worker categor*",
        lambda m: (
            m.startswith("precis.workers.")
            and m.rsplit(".", 1)[-1].startswith("categor")
        ),
    ),
]

HEAVY = [
    "rdkit",
    "numba",
    "shapely",
    "ase",
    "spglib",
    "manifold3d",
    "mermaidx",
    "nanopub",
    "matplotlib",
    "torch",
    "cadquery",
    "scipy",
    "sympy",
]


def module_name(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def is_package(path: Path) -> bool:
    return path.name == "__init__.py"


class Collector(ast.NodeVisitor):
    """Collect import statements with an `eager` flag (runs at import time)."""

    def __init__(self) -> None:
        self.imports: list[tuple[ast.AST, bool]] = []
        self._lazy = 0

    def _enter_lazy(self, node: ast.AST) -> None:
        self._lazy += 1
        self.generic_visit(node)
        self._lazy -= 1

    visit_FunctionDef = _enter_lazy
    visit_AsyncFunctionDef = _enter_lazy
    visit_Lambda = _enter_lazy

    def visit_If(self, node: ast.If) -> None:
        t = node.test
        is_tc = (isinstance(t, ast.Name) and t.id == "TYPE_CHECKING") or (
            isinstance(t, ast.Attribute) and t.attr == "TYPE_CHECKING"
        )
        if is_tc:
            self._lazy += 1
            for child in node.body:
                self.visit(child)
            self._lazy -= 1
            for child in node.orelse:
                self.visit(child)
        else:
            self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.append((node, self._lazy == 0))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.imports.append((node, self._lazy == 0))


def load_modules() -> dict[str, Path]:
    mods: dict[str, Path] = {}
    for p in sorted((SRC / PKG).rglob("*.py")):
        mods[module_name(p)] = p
    return mods


def resolve_targets(dotted: str, mods: dict[str, Path]) -> set[str]:
    """Longest existing module prefix of ``dotted`` plus every parent package
    (importing a.b.c executes a/__init__ and a/b/__init__)."""
    parts = dotted.split(".")
    out: set[str] = set()
    for i in range(len(parts), 0, -1):
        cand = ".".join(parts[:i])
        if cand in mods:
            out.add(cand)
            for j in range(1, i):
                parent = ".".join(parts[:j])
                if parent in mods:
                    out.add(parent)
            break
    return out


def build_graph(mods: dict[str, Path]):
    """Return (edges_all, edges_eager, third_party) where third_party maps
    top-level name -> {importing module: eager?}."""
    stdlib = set(sys.stdlib_module_names)
    edges_all: dict[str, set[str]] = {m: set() for m in mods}
    edges_eager: dict[str, set[str]] = {m: set() for m in mods}
    third: dict[str, dict[str, bool]] = defaultdict(dict)
    parse_errors: list[str] = []

    for name, path in mods.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as e:  # report, don't fail
            parse_errors.append(f"{name}: {e}")
            continue
        col = Collector()
        col.visit(tree)
        pkg_parts = name.split(".") if is_package(path) else name.split(".")[:-1]
        for node, eager in col.imports:
            targets: set[str] = set()
            if isinstance(node, ast.Import):
                for a in node.names:
                    top = a.name.split(".")[0]
                    if top == PKG:
                        targets |= resolve_targets(a.name, mods)
                    elif top not in stdlib:
                        third[top][name] = third[top].get(name, False) or eager
            else:
                if node.level:
                    base_parts = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                    base = ".".join(base_parts + ([node.module] if node.module else []))
                    is_local = True
                else:
                    base = node.module or ""
                    is_local = base.split(".")[0] == PKG
                if is_local and base:
                    targets |= resolve_targets(base, mods)
                    for a in node.names:
                        if a.name != "*":
                            sub = f"{base}.{a.name}"
                            if sub in mods:
                                targets |= resolve_targets(sub, mods)
                elif base:
                    top = base.split(".")[0]
                    if top not in stdlib:
                        third[top][name] = third[top].get(name, False) or eager
            targets.discard(name)
            edges_all[name] |= targets
            if eager:
                edges_eager[name] |= targets
    return edges_all, edges_eager, third, parse_errors


def shortest_path(
    entries: set[str], edges: dict[str, set[str]], targets: set[str]
) -> list[str]:
    """Shortest entry -> target-member import chain (BFS), [] if unreachable."""
    parent: dict[str, str | None] = {e: None for e in sorted(entries)}
    queue = list(parent)
    for cur in queue:
        if cur in targets:
            chain = [cur]
            while parent[chain[-1]] is not None:
                chain.append(parent[chain[-1]])  # type: ignore[arg-type]
            return chain[::-1]
        for nxt in sorted(edges.get(cur, ())):
            if nxt not in parent:
                parent[nxt] = cur
                queue.append(nxt)
    return []


def closure(
    entries: set[str], edges: dict[str, set[str]], removed: str | None = None
) -> set[str]:
    seen = set(e for e in entries if e != removed)
    stack = list(seen)
    while stack:
        cur = stack.pop()
        for nxt in edges.get(cur, ()):
            if nxt != removed and nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def group_of(mod: str, mods: dict[str, Path]) -> str:
    parts = mod.split(".")
    if len(parts) == 1:
        return "precis (root __init__)"
    sub = f"precis.{parts[1]}"
    # a top-level plain module (precis/foo.py) is not a subpackage
    if len(parts) == 2 and not is_package(mods[mod]):
        return "precis.<top-level modules>"
    return sub


def third_in(
    mod_set: set[str], third: dict[str, dict[str, bool]]
) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for top, importers in third.items():
        hit = {m for m in importers if m in mod_set}
        if hit:
            out[top] = hit
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="/tmp/memory-closure-report.md")
    args = ap.parse_args()

    mods = load_modules()
    edges_all, edges_eager, third, parse_errors = build_graph(mods)

    entries: dict[str, list[str]] = {}  # label -> resolved modules
    unresolved: list[str] = []
    for n in ENTRY_NAMES:
        if n in mods:
            entries[n] = [n]
        else:
            unresolved.append(n)
    for label, pred in ENTRY_PATTERNS:
        hit = sorted(m for m in mods if pred(m))
        if hit:
            entries[label] = hit
        else:
            unresolved.append(label)
    if "worker categor*" in unresolved and "precis.categorizers" in mods:
        # no categor* module under workers/; the pass lives at precis/categorizers.py
        unresolved.remove("worker categor*")
        entries["categorizers (fallback: precis.categorizers)"] = [
            "precis.categorizers"
        ]
    entry_set = {m for v in entries.values() for m in v}

    total = len(mods)
    cl = closure(entry_set, edges_all)
    cl_eager = closure(entry_set, edges_eager)
    share = 100 * len(cl) / total
    share_eager = 100 * len(cl_eager) / total

    # third party
    tp = third_in(cl, third)
    tp_eager = third_in(
        cl_eager, {t: {m: e for m, e in imp.items() if e} for t, imp in third.items()}
    )
    heavy_hit = {h: tp[h] for h in HEAVY if h in tp}
    heavy_hit_eager = {h: tp_eager[h] for h in HEAVY if h in tp_eager}

    # bridges
    base_heavy = {h: tp[h] for h in heavy_hit}
    bridges = []
    for m in sorted(cl - entry_set):
        rest = closure(entry_set, edges_all, removed=m)
        shrink = len(cl) - len(rest)
        if shrink <= 1 and not any(m in v for v in base_heavy.values()):
            continue
        lost = cl - rest
        lost_heavy = sorted(
            h for h, importers in base_heavy.items() if importers <= lost
        )
        bridges.append((shrink, m, lost_heavy, len(lost)))
    bridges.sort(key=lambda b: (-b[0], b[1]))
    top_bridges = bridges[:20]

    # per-subpackage
    tot_by_g: dict[str, int] = defaultdict(int)
    cl_by_g: dict[str, int] = defaultdict(int)
    for m in mods:
        tot_by_g[group_of(m, mods)] += 1
    for m in cl:
        cl_by_g[group_of(m, mods)] += 1
    groups = sorted(tot_by_g, key=lambda g: (-cl_by_g[g], g))

    # --- report
    L: list[str] = []
    L.append("# Memory subsystem import closure (td605609)\n")
    L.append(
        f"Source: `{SRC / PKG}`; {total} modules parsed with `ast` (nothing executed).\n"
    )
    L.append("## Entries\n")
    for label, ms in entries.items():
        L.append(f"- {label}: {', '.join(ms)}")
    L.append("")
    L.append(f"Unresolved entries: {', '.join(unresolved) if unresolved else 'none'}\n")
    L.append("## Closure\n")
    L.append(f"- total modules in src/precis: {total}")
    L.append(
        f"- closure (all imports, incl. lazy/TYPE_CHECKING): {len(cl)} = {share:.1f}%"
    )
    L.append(
        f"- closure (eager module-level imports only): {len(cl_eager)} = {share_eager:.1f}%"
    )
    L.append(f"- entry modules: {len(entry_set)}")
    if parse_errors:
        L.append(f"- parse errors: {len(parse_errors)}: " + "; ".join(parse_errors[:5]))
    L.append("")
    L.append("## Third-party top-level imports reached (all-imports closure)\n")
    L.append(f"{len(tp)} distinct: " + ", ".join(sorted(tp)) + "\n")
    L.append("### Heavy deps\n")
    if heavy_hit:
        for h, imp in heavy_hit.items():
            kind = "eager" if h in heavy_hit_eager else "lazy only"
            L.append(
                f"- **{h}** ({kind}): "
                + ", ".join(sorted(imp)[:12])
                + (" ..." if len(imp) > 12 else "")
            )
    else:
        L.append("- none")
    L.append("")
    L.append("### Shortest import chain to a module importing each heavy dep\n")
    L.append(
        "(eager = module reachable via import-time edges; the heavy import itself may still be lazy, see above)\n"
    )
    for h, imp in heavy_hit.items():
        chain = shortest_path(entry_set, edges_eager, set(imp)) or shortest_path(
            entry_set, edges_all, set(imp)
        )
        tag = "eager" if shortest_path(entry_set, edges_eager, set(imp)) else "via lazy"
        L.append(f"- {h} ({tag}): " + " -> ".join(chain))
    L.append("")
    L.append("### Heavy deps NOT reached\n")
    L.append(", ".join(h for h in HEAVY if h not in heavy_hit) or "none")
    L.append("")
    L.append("### Third-party names with importer counts (closure modules)\n")
    for t in sorted(tp, key=lambda t: (-len(tp[t]), t)):
        L.append(f"- {t}: {len(tp[t])}")
    L.append("")
    L.append("## Top 20 bridge modules\n")
    L.append(
        "shrink = modules dropped from the closure if this module (and edges through it) is removed; "
        "heavy = heavy deps whose every closure importer is dropped with it.\n"
    )
    L.append("| # | module | shrink | heavy dropped |")
    L.append("|---|--------|--------|---------------|")
    for i, (shrink, m, lh, _) in enumerate(top_bridges, 1):
        L.append(f"| {i} | {m} | {shrink} | {', '.join(lh) or '-'} |")
    L.append("")
    if heavy_hit:
        L.append("### Bridges that carry heavy deps (any, ranked by shrink)\n")
        carriers = [b for b in bridges if b[2]][:20]
        for shrink, m, lh, _ in carriers:
            L.append(f"- {m}: shrink {shrink}, heavy {', '.join(lh)}")
        L.append("")
    L.append("## Closure share per subpackage\n")
    L.append("| group | in closure | total | share |")
    L.append("|-------|-----------:|------:|------:|")
    for g in groups:
        L.append(
            f"| {g} | {cl_by_g[g]} | {tot_by_g[g]} | {100 * cl_by_g[g] / tot_by_g[g]:.0f}% |"
        )
    L.append("")
    L.append("## Closure members\n")
    for m in sorted(cl):
        L.append(f"- {m}")
    report = "\n".join(L) + "\n"
    Path(args.out).write_text(report, encoding="utf-8")

    # --- 15-line summary
    S: list[str] = []
    S.append(
        f"modules: {total} total; closure {len(cl)} = {share:.1f}% (eager-only {len(cl_eager)} = {share_eager:.1f}%)"
    )
    S.append(
        f"entries resolved: {len(entry_set)} modules; unresolved: {', '.join(unresolved) or 'none'}"
    )
    S.append(f"third-party top-level names reached: {len(tp)}")
    S.append(
        "heavy deps reached: "
        + (
            ", ".join(
                f"{h}({'eager' if h in heavy_hit_eager else 'lazy'})" for h in heavy_hit
            )
            or "none"
        )
    )
    S.append("top 5 bridges (shrink; heavy dropped):")
    for shrink, m, lh, _ in top_bridges[:5]:
        S.append(f"  {m}  {shrink}  {','.join(lh) or '-'}")
    S.append(
        "fattest subpackages in closure: "
        + ", ".join(f"{g} {cl_by_g[g]}/{tot_by_g[g]}" for g in groups[:5])
    )
    verdict_ok = share < 35 and not heavy_hit
    S.append(
        f"rule (<35% and no heavy dep): {'demo clone viable' if verdict_ok else 'NOT met -> full image'}"
    )
    S.append(f"full report: {args.out}")
    print("\n".join(S))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
