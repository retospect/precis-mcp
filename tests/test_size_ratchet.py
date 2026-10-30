"""Size sweep ratchet: every renderer that lists fetchable rows shows
``size_cell`` (``<n>kB`` at ≥1 kB, ``""`` below).

Two pinned surfaces:

* ``_SIZED_SITES`` — ``file::enclosing function`` sites whose source must
  call ``size_cell(`` (or carry a ``"size"`` schema key). Removing a size
  from a listed site fails.
* The universe of ``render_agent_table`` call sites whose schema names an
  id-ish column (a row you can ``get``). Any such site must either put
  ``"size"`` in the schema or appear in ``_EXEMPT`` with a reason —
  a new fetchable-row table without a size fails until it carries one
  or is deliberately exempted.

Sizes must come from stored lengths (a length column, text the row
already loaded, or a SQL ``octet_length``/``length`` in the existing
query) — never a body rendered just to measure it, never a per-row
query.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "precis"

#: Renderers that show sizes on their rows — pinned so a size never
#: silently drops out. ``file::enclosing function``.
_SIZED_SITES: frozenset[str] = frozenset(
    {
        "handlers/_numeric_ref.py::_id_lines",
        "handlers/_numeric_ref.py::_render_hits_table",
        "handlers/_numeric_ref.py::_search_body_chunks",
        "handlers/_numeric_ref.py::_get_batch",
        "handlers/_links_render.py::render_links_section",
        "handlers/memory.py::_render_hits_table",
        "handlers/memory.py::_search_inner",
        "handlers/memory.py::_index_listing",
        "handlers/skill.py::_render_index",
        "handlers/skill.py::_render_toc",
        "handlers/skill.py::search",
        "handlers/_paper_search.py::render",
        "handlers/_paper_search.py::run",
        "handlers/paper.py::_render_summaries",
        "handlers/md.py::search",
        "handlers/md.py::_render_file_outline",
        "handlers/markdown.py::_overview_body_extras",
        "handlers/markdown.py::_render_index",
        "handlers/plaintext.py::_render_index",
        "handlers/draft.py::_render_outline",
        "utils/eye_render.py::_cluster_label",
        "utils/eye_render.py::_fisheye_split",
        "utils/eye_render.py::_render_ring",
        "utils/eye_render.py::_expand_first_hop",
        "utils/refeye.py::collect_ring",
        "utils/fisheye.py::_render_fidelity_span",
        "utils/search_merge.py::_render_hit",
        "utils/search_merge.py::_render_toon_table",
        "utils/search_merge.py::_render_keywords_table",
        "utils/toc.py::render",
        "utils/toc_db.py::render_from_store",
        "utils/toc_db.py::_render_per_chunk",
        "handlers/_paper_toc.py::_collect_rows",
        "todo/views.py::render_status_flat",
        "todo/views.py::render_flat",
        "todo/views.py::render_waiting",
        "todo/views.py::render_blocked",
    }
)

#: Schema keys that mark a table row as a fetchable thing.
_IDISH: frozenset[str] = frozenset({"id", "handle", "slug", "related to"})

#: Sites that must NOT carry a computed size — each pinned with the
#: reason. ``_NEVER_QUERY`` is the mechanically-checked subset: no
#: ``body_byte_counts`` (one bulk read per page) may appear in the
#: scope's subtree — the rows name refs the agent might fetch, but a
#: ring walks far enough that the read is not bounded by page_size.
_NEVER_SIZED: dict[str, str] = {
    "handlers/_numeric_ref.py::_id_lines view='ids'": (
        "bare handles per line, fed back as id= — enforced by "
        "test_ids_view_stays_bare_handles_list_view_carries_size"
    ),
}
_NEVER_QUERY: frozenset[str] = frozenset(
    s.split()[0] for s in _NEVER_SIZED if "view='ids'" not in s
)

#: ``render_agent_table`` sites with an id-ish column that carry no
#: ``"size"`` — each pinned with the reason a size can't be had cheaply
#: or doesn't apply. ``file::enclosing function -> reason``.
_EXEMPT: dict[str, str] = {
    "handlers/skill.py::_render_usage": "usage stats, not fetchable rows",
    "handlers/profile.py::_profile_table": "config rows, not refs",
    "handlers/paper.py::_render_bibliography": "intra-paper citation table",
    "handlers/tag.py::_render_metadata": "tag-vocab rows, not bodies",
    "handlers/cad.py::_tree_table": "op-tree rows inside one design",
    "handlers/cad.py::search": "design rows — sweep follow-up",
    "handlers/pcb.py::search": "design rows — sweep follow-up",
    "handlers/structure.py::_render_hydrated": "dataset rows — sweep follow-up",
    "handlers/structure.py::_render_literature": "crossref rows — external",
    "handlers/structure.py::search": "design rows — sweep follow-up",
    "handlers/component.py::_list_components": "catalog rows — sweep follow-up",
    "handlers/component.py::_render_supplier_categories": "category tree, not rows",
    "handlers/component.py::_search_entities": "catalog rows — sweep follow-up",
    "handlers/material.py::_list_materials": "catalog rows — sweep follow-up",
    "handlers/material.py::_search_entities": "catalog rows — sweep follow-up",
    "handlers/part.py::_categories_view": "catalog categories, not rows",
    "handlers/folder.py::_render_one": "folder rows — sweep follow-up",
    "handlers/taxon.py::_render_meta_view": "vocab rows, not bodies",
}


class _SiteVisitor(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.stack: list[str] = []
        self.sites: dict[str, set[str]] = {}
        self.sized_scopes: set[str] = set()
        self.counted_scopes: set[str] = set()

    def _scope(self, node: ast.AST, name: str) -> None:
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scope(node, node.name)

    def _mark(self, out: set[str]) -> None:
        """Credit every enclosing scope — a marker in a nested helper
        still belongs to the renderer that wraps it."""
        for i in range(1, len(self.stack) + 1):
            out.add(f"{self.rel}::{'.'.join(self.stack[:i])}")

    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        name = (
            fn.id
            if isinstance(fn, ast.Name)
            else fn.attr
            if isinstance(fn, ast.Attribute)
            else None
        )
        # A site counts as sized only when its subtree calls
        # ``size_cell`` itself or a named helper that does (``_*size_cell``
        # wraps it — ``_skill_size_cell``, ``_file_size_cell``,
        # ``_chunk_size_cell``). No loose name or kwarg heuristics.
        if name and name.endswith("size_cell"):
            self._mark(self.sized_scopes)
        if name == "body_byte_counts":
            self._mark(self.counted_scopes)
        if name in ("render_agent_table", "_render_toon_table"):
            for kw in node.keywords:
                if kw.arg == "schema":
                    self.sites.setdefault(
                        f"{self.rel}::{'.'.join(self.stack)}", set()
                    ).update(_schema_columns(kw.value))
        self.generic_visit(node)


def _schema_columns(node: ast.AST) -> set[str]:
    """Constant strings in a schema list/tuple literal (empty when the
    schema is a variable — treated as no column info)."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return {
            elt.value
            for elt in node.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        }
    return set()


def _scan() -> tuple[dict[str, set[str]], set[str], set[str], set[str]]:
    """``(render_agent_table sites -> schema cols, sized scopes,
    sites whose schema carries "size", body_byte_counts scopes)``."""
    sites: dict[str, set[str]] = {}
    sized: set[str] = set()
    sized_schemas: set[str] = set()
    counted: set[str] = set()
    for f in sorted(SRC.rglob("*.py")):
        rel = str(f.relative_to(SRC))
        v = _SiteVisitor(rel)
        tree = ast.parse(f.read_text(encoding="utf-8"))
        v.visit(tree)
        for site, cols in v.sites.items():
            sites.setdefault(site, set()).update(cols)
            if "size" in cols:
                sized_schemas.add(site)
        sized |= v.sized_scopes
        counted |= v.counted_scopes
    return sites, sized, sized_schemas, counted


def _covered(site: str, sized: set[str], sized_schemas: set[str]) -> bool:
    """A pin ``file::<name>`` holds when a sized scope in that file is
    ``<name>`` itself or a ``Class.<name>`` method — nested helpers are
    already credited to every enclosing scope by ``_mark`` — or when a
    ``render_agent_table`` site there carries a ``"size"`` column."""
    rel, _, name = site.partition("::")
    for k in sized | sized_schemas:
        krel, _, kname = k.partition("::")
        if krel != rel:
            continue
        if kname == name or kname.endswith("." + name):
            return True
    return False


def test_sized_renderers_still_carry_a_size() -> None:
    sites, sized, sized_schemas, _counted = _scan()
    missing = {s for s in _SIZED_SITES if not _covered(s, sized, sized_schemas)}
    assert not missing, (
        f"renderer(s) in the size contract lost their size_cell/size "
        f"column: {sorted(missing)} — restore the size or remove the "
        "pin (and say why in the commit body)"
    )


def _norm(site: str) -> str:
    """``file::Class.method`` → ``file::method`` — exemptions key on the
    leaf function, same as the pins."""
    rel, _, name = site.partition("::")
    return f"{rel}::{name.split('.')[-1]}"


def test_never_sized_sites_run_no_bulk_size_query() -> None:
    """``_NEVER_QUERY`` sites — the eye/hub ring lists — must not pay a
    bulk ``body_byte_counts`` read for row sizes; a ring isn't bounded
    by page_size. ``view='ids'`` is the one ``_NEVER_SIZED`` entry kept
    out of this check (its enclosing ``_id_lines`` legitimately queries
    for ``view='list'``); the behavioral test pins its bare handles."""
    _sites, _sized, _schemas, counted = _scan()
    bad = {s for s in _NEVER_QUERY if _covered(s, counted, set())}
    assert not bad, (
        f"never-sized site(s) now run a bulk body_byte_counts read: "
        f"{sorted(bad)} — ring rows size from text already loaded only; "
        "update _NEVER_SIZED if the shape changed deliberately"
    )


def test_new_fetchable_tables_carry_size_or_are_exempt() -> None:
    sites, _sized, sized_schemas, _counted = _scan()
    bad: dict[str, set[str]] = {}
    stale = set(_EXEMPT)
    for site, cols in sites.items():
        if not cols & _IDISH:
            continue
        if site in sized_schemas or "size" in cols:
            continue
        norm = _norm(site)
        if norm in _EXEMPT:
            stale.discard(norm)
            continue
        bad[site] = cols
    assert not bad, (
        f"fetchable-row table(s) without a size column and without an "
        f"exemption: {bad} — add a ``size`` column (stored lengths only) "
        "or pin an exemption with a reason in _EXEMPT"
    )
    assert not stale, (
        f"_EXEMPT entries that no longer exist or now carry a size: "
        f"{sorted(stale)} — drop them"
    )
