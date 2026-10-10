"""Facet counts over the instances under a taxon node.

Slice 3 of docs/backlog/taxon-facet-navigation.md —
``get(kind='taxon', id=…, view='facets')``. Two facet sources, kept apart in
the output:

* **taxon axes** — every other ``instance-of`` link the instances carry, to a
  taxon outside the node's own subtree and ancestry. The axis of a value is the
  label of the nearest labelled ``specialises`` edge above it (else the slug of
  its top ancestor); the value is the linked taxon itself (no roll-up).
* **categorizer axes** — the closed ref-level tag namespaces written by
  ``data/axes/*.yaml`` passes (``DOMAIN``, ``STUDYTYPE``, …) and the open
  ``topic:<slug>`` tags, machine-written; each carries the pass version from its
  ``<NS>CASCADE`` done-marker and an unclassified split (processed without a
  value vs not processed).

Everything is assembled into :class:`Facet` objects (axis name -> value ->
instance-id set) so sorting, the gap cross-tab and rendering share one shape.
The render is capped to stay near 2k tokens.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from precis.errors import BadInput

MAX_AXES = 8
MAX_MACHINE_AXES = 3  # of MAX_AXES, kept for categorizer facets when present
MAX_VALUES = 15
MAX_GAP_CELLS = 15
MAX_HIDDEN_NAMES = 20  # hidden-axis names listed in the footer line
RECENT_DAYS = 90
EVIDENCE_KINDS = ("finding", "measure")
SORTS: dict[str, str] = {
    "split": "axes by how evenly they split the set (best cut first); the default",
    "recent": "only instances created in the last 90 days",
    "evidence": "only findings and measures (not raw papers)",
    "gap": "two axes crossed, empty and thin cells first: args={'cross': [axisA, axisB]}",
    "name": "axes and values alphabetically",
}


@dataclass
class Facet:
    name: str
    machine: bool
    values: dict[str, set[int]]
    universe: set[int]  # instances this axis applies to
    note: str = ""  # machine facets: pass version
    unprocessed: int = 0  # machine: applicable instances the pass has not seen
    processed_empty: int = 0  # machine: seen, no value written
    handles: dict[str, str] = field(default_factory=dict)

    def classified(self) -> int:
        return len(set().union(*self.values.values())) if self.values else 0

    def entropy(self) -> float:
        counts = [len(v) for v in self.values.values() if v]
        n = sum(counts)
        if n == 0:
            return 0.0
        return -sum((c / n) * math.log2(c / n) for c in counts)


def _axis_of(store: Any, taxon_id: int, cache: dict[int, str]) -> str:
    """Label of the nearest labelled upward edge, else the top ancestor's slug."""
    if taxon_id in cache:
        return cache[taxon_id]
    seen = {taxon_id}
    cur = taxon_id
    label = ""
    while True:
        parents = store.taxon_parents(cur)
        if not parents:
            ref = store.get_ref(kind="taxon", id=cur)
            label = str(((ref.meta or {}).get("slug") if ref else None) or "?")
            break
        labelled = next((ax for _p, ax in parents if ax), None)
        if labelled:
            label = labelled
            break
        nxt = parents[0][0]
        if nxt in seen:
            label = "?"
            break
        seen.add(nxt)
        cur = nxt
    cache[taxon_id] = label
    return label


def _taxon_facets(store: Any, instances: set[int], excluded: set[int]) -> list[Facet]:
    from precis.utils import handle_registry

    cache: dict[int, str] = {}
    names: dict[int, str] = {}
    axes: dict[str, dict[str, set[int]]] = {}
    for inst, tx in store.taxon_instance_links(sorted(instances)):
        if tx in excluded:
            continue
        if tx not in names:
            ref = store.get_ref(kind="taxon", id=tx)
            nm = str(((ref.meta or {}).get("name") if ref else None) or tx)
            names[tx] = f"{handle_registry.format_handle('taxon', tx)} {nm}"
        axes.setdefault(_axis_of(store, tx, cache), {}).setdefault(
            names[tx], set()
        ).add(inst)
    return [Facet(a, False, v, set(instances)) for a, v in axes.items()]


def _machine_facets(store: Any, kinds: dict[int, str]) -> list[Facet]:
    from precis.workers import axis_pass, classify_topics

    specs: list[tuple[str, str, list[str]]] = []  # (name, ns, applies kinds)
    for axis_id in axis_pass.discover_axis_ids():
        try:
            axis = axis_pass._load_axis(axis_id)
        except Exception:
            continue
        if axis.get("level", "ref") != "ref":
            continue
        specs.append(
            (axis_id, axis_id.upper(), list(axis.get("applies_to_kinds") or ["paper"]))
        )
    specs.append(("topic", "TOPIC", ["paper", "patent"]))
    closed = [ns for _n, ns, _k in specs if ns != "TOPIC"]
    markers = [f"{ns}CASCADE" for _n, ns, _k in specs]
    markers[-1] = classify_topics.MARKER_NAMESPACE
    rows = store.ref_tag_rows(
        sorted(kinds), namespaces=[*closed, *markers], open_prefix="topic:"
    )
    values: dict[tuple[str, int], set[str]] = {}
    marked: dict[str, dict[int, str]] = {}
    for rid, ns, val in rows:
        if ns.endswith("CASCADE"):
            marked.setdefault(ns, {})[rid] = val
        elif ns == "OPEN":
            values.setdefault(("TOPIC", rid), set()).add(val[len("topic:") :])
        else:
            values.setdefault((ns, rid), set()).add(val)
    out: list[Facet] = []
    for (name, ns, applies), marker_ns in zip(specs, markers, strict=True):
        universe = {r for r, k in kinds.items() if k in applies}
        if not universe:
            continue
        vals: dict[str, set[int]] = {}
        for (vns, rid), vs in values.items():
            if vns == ns and rid in universe:
                for v in vs:
                    vals.setdefault(v, set()).add(rid)
        seen = {r for r in marked.get(marker_ns, {}) if r in universe}
        tagged = set().union(*vals.values()) if vals else set()
        versions = Counter(marked.get(marker_ns, {}).get(r, "?") for r in seen)
        ver = ", ".join(v for v, _c in versions.most_common(2)) or "never run"
        out.append(
            Facet(
                name,
                True,
                vals,
                universe,
                note=f"pass v{ver}" if seen else ver,
                unprocessed=len(universe - seen - tagged),
                processed_empty=len(seen - tagged),
            )
        )
    return out


def build_facets(
    store: Any,
    node_id: int,
    *,
    sort: str,
    now: datetime | None = None,
) -> tuple[list[Facet], set[int], int, str]:
    """``(facets, instances, total_before_filter, filter_note)`` for the node."""
    closure = [node_id] + [d for d, _depth, _ax in store.taxon_descendants(node_id)]
    all_instances = store.taxon_instance_ids(closure)
    total = len(all_instances)
    info = store.ref_kinds_created(sorted(all_instances))
    instances = set(info)
    note = ""
    if sort == "recent":
        cutoff = (now or datetime.now(UTC)) - timedelta(days=RECENT_DAYS)
        instances = {i for i, (_k, c) in info.items() if c >= cutoff}
        note = f"created in the last {RECENT_DAYS} days"
    elif sort == "evidence":
        instances = {i for i, (k, _c) in info.items() if k in EVIDENCE_KINDS}
        note = "findings and measures only"
    ancestors = {a for a, _d, _ax in store.taxon_ancestors(node_id)}
    excluded = set(closure) | ancestors
    facets = _taxon_facets(store, instances, excluded)
    facets += _machine_facets(store, {i: info[i][0] for i in instances})
    return facets, instances, total, note


def _order(facets: list[Facet], sort: str) -> list[Facet]:
    if sort == "name":
        return sorted(facets, key=lambda f: f.name.lower())
    return sorted(facets, key=lambda f: (-f.entropy(), f.name.lower()))


def _pick(facets: list[Facet], sort: str) -> tuple[list[Facet], list[Facet]]:
    """Shown/hidden split: at most MAX_AXES, categorizer axes keeping up to
    MAX_MACHINE_AXES slots when present. Taxon axes lead."""
    tax = _order([f for f in facets if not f.machine and f.values], sort)
    mac = _order([f for f in facets if f.machine], sort)
    take_t = min(len(tax), MAX_AXES - min(len(mac), MAX_MACHINE_AXES))
    take_m = min(len(mac), MAX_AXES - take_t)
    return tax[:take_t] + mac[:take_m], tax[take_t:] + mac[take_m:]


def _value_line(values: dict[str, set[int]], sort: str) -> str:
    items = [(v, len(s)) for v, s in values.items()]
    items.sort(
        key=(lambda t: t[0].lower()) if sort == "name" else (lambda t: (-t[1], t[0]))
    )
    shown, rest = items[:MAX_VALUES], items[MAX_VALUES:]
    line = " · ".join(f"{v} {n}" for v, n in shown)
    if rest:
        hidden = set().union(*(values[v] for v, _n in rest))
        line += f" · +{len(rest)} values ({len(hidden)} items)"
    return line


def _footer(sort: str) -> list[str]:
    out = ["", "other sorts (get(..., view='facets', args={'sort': ...})):"]
    out += [f"  {k}: {why}" for k, why in SORTS.items() if k != sort]
    out.append(
        "narrow with search(kind=<any>, under=<value handle>) instead of paging."
    )
    return out


def render_facets(
    store: Any,
    node_id: int,
    head: str,
    *,
    sort: str | None,
    cross: list[str] | None,
    now: datetime | None = None,
) -> str:
    sort = (sort or "split").strip().lower()
    if sort not in SORTS:
        raise BadInput(
            f"unknown facets sort {sort!r}",
            next="sort one of: " + ", ".join(SORTS),
        )
    if cross is not None and sort != "gap":
        raise BadInput(
            "cross= only applies with sort='gap'",
            next="get(kind='taxon', id=N, view='facets', args={'sort': 'gap', 'cross': ['a', 'b']})",
        )
    facets, instances, total, note = build_facets(store, node_id, sort=sort, now=now)
    n = len(instances)
    count_line = (
        f"instances: {n} of {total} ({note})" if note else f"instances: {total}"
    )
    out = [f"# {head} — facets (sort={sort})", count_line]
    if n == 0:
        out.append(
            "no instances under this node" if total == 0 else "none match this sort"
        )
        return "\n".join(out + _footer(sort))
    if sort == "gap":
        return "\n".join(out + _gap_lines(facets, cross) + _footer(sort))
    shown, hidden = _pick(facets, sort)
    tax = [f for f in shown if not f.machine]
    mac = [f for f in shown if f.machine]
    if tax:
        out.append("")
        out.append(
            "taxon axes (counts of instances; one instance can be in several values):"
        )
        for f in tax:
            miss = n - f.classified()
            line = f"- {f.name} ({f.classified()}/{n}): " + _value_line(f.values, sort)
            out.append(line + (f" · none {miss}" if miss else ""))
    if mac:
        out.append("")
        out.append("machine-written categorizer facets (LLM passes, not curated):")
        for f in mac:
            tail = f"unclassified {f.processed_empty + f.unprocessed}"
            if f.processed_empty or f.unprocessed:
                tail += (
                    f" (no value {f.processed_empty}, not processed {f.unprocessed})"
                )
            body = _value_line(f.values, sort) if f.values else "no values yet"
            out.append(
                f"- {f.name} [{f.note}] ({f.classified()}/{len(f.universe)} "
                f"applicable): {body} · {tail}"
            )
    if hidden:
        out.append("")
        names = ", ".join(f.name for f in hidden[:MAX_HIDDEN_NAMES])
        more = len(hidden) - MAX_HIDDEN_NAMES
        out.append(
            f"+{len(hidden)} axes not shown: {names}"
            + (f", … {more} more" if more > 0 else "")
        )
    if not tax and not mac:
        out.append("no other axes: the instances carry no other classification")
    return "\n".join(out + _footer(sort))


def _gap_lines(facets: list[Facet], cross: list[str] | None) -> list[str]:
    by_name = {f.name.lower(): f for f in facets if f.values}
    if not isinstance(cross, list) or len(cross) != 2:
        raise BadInput(
            "sort='gap' needs args={'cross': [axisA, axisB]}",
            next="axes here: " + (", ".join(sorted(by_name)) or "(none)"),
        )
    picked = []
    for name in cross:
        f = by_name.get(str(name).strip().lower())
        if f is None:
            raise BadInput(
                f"no axis {name!r} with values under this node",
                next="axes here: " + (", ".join(sorted(by_name)) or "(none)"),
            )
        picked.append(f)
    a, b = picked
    cells = [
        (len(a.values[va] & b.values[vb]), va, vb) for va in a.values for vb in b.values
    ]
    cells.sort(key=lambda c: (c[0], c[1], c[2]))
    empty = sum(1 for c in cells if c[0] == 0)
    out = [
        "",
        f"{a.name} x {b.name}: {len(cells)} cells, {empty} empty "
        "(values seen on each axis; thinnest first)",
    ]
    out += [f"  {va} x {vb}: {n}" for n, va, vb in cells[:MAX_GAP_CELLS]]
    if len(cells) > MAX_GAP_CELLS:
        out.append(f"  +{len(cells) - MAX_GAP_CELLS} thicker cells")
    return out
