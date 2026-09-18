"""Quest results table — a lineage-ordered, one-row-per-candidate context
block for the tick prompt (docs/backlog/pathway-conditions-effects-report.md
decision 4).

The tick is a single structured LLM call with no live ``get``/``search``
(:mod:`precis.quest.tick`'s module docstring), so all it otherwise sees of a
finished run is :func:`precis.quest.tick._frontier_summary`'s one-line-per-
candidate digest — enough to rank, not enough to notice "we already tried
Ag at this coverage, twice" before proposing a third. This module builds
that missing tabular view: every candidate across all four Pareto bands
(frontier/dominated/provisional/unevaluated — :mod:`precis.quest.frontier`),
one row each, ordered by **lineage** (base composition, then what varies)
rather than by band, so a doped-slab series reads as adjacent rows.

Two renderings share the same rows: :func:`render_results_table` (a
fixed-width text table for the tick prompt) and the quest handler's
``view='results'`` (the same rows as a TOON table, :mod:`precis.format.toon`).

Dopant/site/co-adsorbate columns are derived at READ time from the
candidate's own ``meta.params`` when the proposer stamped one, else from the
candidate structure's atoms (:meth:`Store.structure_load`) compared against
the quest's declared slab element (``meta.reaction_config.slab.element``) —
best-effort: an undeliverable structure load, an unstamped params dict, or an
ambiguous site geometry degrades to ``'-'``/``'?'`` rather than raising or
guessing hard.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.quest.frontier import Candidate, FrontierResult
    from precis.store import Store

#: Token budget for the rendered text table (chars/4, same convention as
#: :data:`precis.quest.tick._LITERATURE_TOKEN_BUDGET`).
_RESULTS_TABLE_TOKEN_BUDGET = 2500
#: Public alias — the handler's ``view='results'``/``'frontier'`` default
#: budget (``args={'budget': N}`` overrides), so the standalone views and
#: the tick's embedded copy can't drift apart (gr345353).
RESULTS_TABLE_TOKEN_BUDGET = _RESULTS_TABLE_TOKEN_BUDGET

_CHARS_PER_TOKEN = 4

#: The newest-by-ref_id rows a budget truncation never drops — the most
#: recently minted candidates are the ones a proposer is most likely to be
#: about to re-discover.
_KEEP_NEWEST = 10

#: Elements never counted as "the dopant" — adsorbate/reaction-intermediate
#: species (H/O from a co-adsorbate, N/C from the reaction network itself),
#: distinct from a substitutional/adatom dopant on the slab.
_ADSORBATE_ELEMENTS = frozenset({"H", "O", "N", "C"})

#: A dopant atom sitting more than this far (Å) above the slab's own top
#: layer reads as an adatom; at or below, a substitution. Comfortably under
#: a metal's interlayer spacing (~2 Å+) so it never misreads a slightly
#: rumpled top layer as an adatom.
_SITE_Z_MARGIN = 0.3

_COLUMNS: tuple[str, ...] = (
    "handle",
    "name",
    "band",
    "dopant",
    "n_dopant",
    "site",
    "coads",
    "tier",
    "seeds",
    "barrier",
    "span_at_Uopt",
    "U_L",
    "P_side",
    "trusted",
    "rls",
    "blocker",
    "worst_problem",
)

#: Free-text columns clipped (with an ellipsis) so one verbose value can't
#: blow the whole table's width out.
_COL_MAX_WIDTH: dict[str, int] = {
    "name": 24,
    "seeds": 14,
    "rls": 22,
    "blocker": 30,
    "worst_problem": 36,
}


def _slab_element_for(quest_ref: Any) -> str | None:
    """The quest's declared slab element (``meta.reaction_config.slab.
    element``), or ``None`` when the quest carries no reaction config."""
    if quest_ref is None:
        return None
    rc = (quest_ref.meta or {}).get("reaction_config")
    if not isinstance(rc, dict):
        return None
    slab = rc.get("slab")
    if not isinstance(slab, dict):
        return None
    el = slab.get("element")
    return str(el) if el else None


def _structure_atoms(
    store: Store, ref_id: int
) -> list[tuple[str, float | None]] | None:
    """``[(element, cartesian_z), ...]`` for a candidate structure, or
    ``None`` on any load failure (never raises — best-effort context)."""
    try:
        scene, _handles = store.structure_load(ref_id)
    except Exception:
        return None
    atoms: list[tuple[str, float | None]] = []
    for atom in scene.atoms.values():
        try:
            z = float(scene.cell.frac_to_cart(atom.frac)[2])
        except Exception:
            z = None
        atoms.append((str(atom.element), z))
    return atoms or None


def _dopant_from_atoms(
    atoms: list[tuple[str, float | None]] | None, slab_element: str | None
) -> tuple[str, int, str]:
    """``(dopant, n_dopant, site)`` derived from a structure's atoms —
    everything that isn't the slab element or a co-adsorbate species."""
    if not atoms or not slab_element:
        return "-", 0, "?"
    dopant_atoms = [
        (el, z)
        for el, z in atoms
        if el != slab_element and el not in _ADSORBATE_ELEMENTS
    ]
    if not dopant_atoms:
        return "-", 0, "-"
    dopant = ",".join(sorted({el for el, _z in dopant_atoms}))
    n_dopant = len(dopant_atoms)
    slab_zs = [z for el, z in atoms if el == slab_element and z is not None]
    if not slab_zs:
        return dopant, n_dopant, "?"
    top = max(slab_zs)
    sites = {
        "?" if z is None else ("adatom" if z > top + _SITE_Z_MARGIN else "subst")
        for _el, z in dopant_atoms
    }
    site = next(iter(sites)) if len(sites) == 1 else "?"
    return dopant, n_dopant, site


def _dopant_and_site(
    candidate: Candidate,
    slab_element: str | None,
    atoms: list[tuple[str, float | None]] | None,
) -> tuple[str, int, str]:
    """``(dopant, n_dopant, site)`` — prefers a proposer-stamped
    ``meta.params`` (§7.8 named param space) over deriving from atoms."""
    params = candidate.params or {}
    if any(k in params for k in ("dopant", "n_dopant", "site")):
        dopant = str(params.get("dopant") or "-")
        try:
            n_dopant = int(params.get("n_dopant") or 0)
        except (TypeError, ValueError):
            n_dopant = 0
        site = str(params.get("site") or "?")
        return dopant, n_dopant, site
    return _dopant_from_atoms(atoms, slab_element)


def _coads(atoms: list[tuple[str, float | None]] | None) -> tuple[str, int, int]:
    """``(display, h_count, o_count)`` — the candidate structure's own H/O
    co-adsorbate counts (before any reaction network is placed on it)."""
    if not atoms:
        return "-", 0, 0
    h = sum(1 for el, _z in atoms if el == "H")
    o = sum(1 for el, _z in atoms if el == "O")
    parts = [f"H{h}" for _ in (1,) if h] + [f"O{o}" for _ in (1,) if o]
    return (" ".join(parts) or "-"), h, o


def _linked_pathway_meta(store: Store, ref_id: int) -> dict[str, Any] | None:
    """The most-recent linked `pathway`'s meta (``related-to``, the relation
    :func:`precis.quest.compute._link_pathway` wires), or ``None``."""
    try:
        links = store.links_for(ref_id, direction="both", relation="related-to")
    except Exception:
        return None
    other_ids = {
        ln.dst_ref_id if ln.src_ref_id == ref_id else ln.src_ref_id for ln in links
    }
    if not other_ids:
        return None
    try:
        refs = store.fetch_refs_by_ids(other_ids)
    except Exception:
        return None
    pathways = [r for r in refs.values() if r.kind == "pathway"]
    if not pathways:
        return None
    return max(pathways, key=lambda r: r.id).meta or {}


def _seeds_display(pw_meta: dict[str, Any] | None) -> str:
    if not pw_meta:
        return "-"
    seeds: Any = None
    results = pw_meta.get("results")
    if isinstance(results, dict):
        seeds = results.get("seeds")
    if not seeds:
        config = pw_meta.get("config")
        if isinstance(config, dict):
            seeds = (config.get("search") or {}).get("seeds")
    if not isinstance(seeds, list) or not seeds:
        return "-"
    try:
        return ",".join(str(int(s)) for s in seeds)
    except (TypeError, ValueError):
        return "-"


def _rls_display(pw_meta: dict[str, Any] | None) -> str:
    """The rate-limiting step name off the pathway's own reaction graph
    (:func:`precis_pathway.analysis.rate_limiting`'s ``step``), or ``'-'``."""
    if not pw_meta:
        return "-"
    graph = pw_meta.get("graph")
    results = pw_meta.get("results")
    if not isinstance(graph, dict) or not graph or not isinstance(results, dict):
        return "-"
    try:
        from precis_pathway import analysis

        root, target = analysis.roots(graph, results)
        summ = analysis.summarize(graph, root, target)
        step = (summ.get("rate_limiting") or {}).get("step")
        return str(step) if step else "-"
    except Exception:
        return "-"


def _trusted_str(candidate: Candidate) -> str:
    v = candidate.flags.get("barrier_trusted")
    if v is True:
        return "yes"
    if v is False:
        return "no"
    return "-"


def _blocker_display(candidate: Candidate) -> str:
    """The first fatal on-route trust id, or the ``worst_problem`` one-liner,
    or ``'-'`` — clipped to 60 chars (a prompt-table cell, not a report)."""
    blocked = candidate.flags.get("barrier_blocked_by")
    if isinstance(blocked, list) and blocked:
        first = blocked[0]
        val: Any = first
        if isinstance(first, dict):
            val = (
                first.get("id")
                or first.get("record_id")
                or first.get("reason")
                or first
            )
        return str(val)[:60]
    worst = candidate.flags.get("worst_problem")
    if isinstance(worst, str) and worst:
        return worst[:60]
    return "-"


def _measure_str(
    measures: dict[str, float], untrusted_keys: frozenset[str], key: str
) -> str:
    v = measures.get(key)
    if not isinstance(v, (int, float)):
        return "-"
    s = f"{v:.3g}"
    return f"≈{s}" if key in untrusted_keys else s


def build_results_rows(
    store: Store, quest_id: int, *, fr: FrontierResult | None = None
) -> list[dict[str, Any]]:
    """One row per candidate across ALL four Pareto bands, lineage-ordered.

    ``fr`` reuses an already-computed :class:`precis.quest.frontier.
    FrontierResult` (the tick threads its own — see :mod:`precis.quest.tick`)
    instead of a second live-candidate scan; ``None`` (default, every unit
    test) computes one fresh.

    Ordering groups rows by base composition (the quest's slab element is
    constant, so effectively by dopant), then by what varies within that
    group (dopant count, then H/O co-adsorbate counts) — a doped-slab series
    reads as adjacent rows regardless of which Pareto band each member
    landed in. ``ref_id`` breaks ties for determinism; it also rides along
    on each row (dropped by both renderers' explicit column lists) so
    :func:`render_results_table` can identify the newest candidates a
    budget truncation must never drop.
    """
    from precis.quest.frontier import quest_frontier

    if fr is None:
        fr = quest_frontier(store, quest_id)
    quest_ref = store.get_ref(kind="quest", id=quest_id)
    slab_element = _slab_element_for(quest_ref)

    entries: list[tuple[Candidate, str, dict[str, float], frozenset[str]]] = []
    entries += [(c, "frontier", c.measures, frozenset()) for c in fr.frontier]
    entries += [(c, "beaten", c.measures, frozenset()) for c in fr.dominated]
    entries += [
        (pc.candidate, "provisional", pc.measures, pc.untrusted_keys)
        for pc in fr.provisional
    ]
    entries += [(c, "awaiting", c.measures, frozenset()) for c in fr.unevaluated]

    built: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for candidate, band, measures, untrusted in entries:
        atoms = _structure_atoms(store, candidate.ref_id)
        dopant, n_dopant, site = _dopant_and_site(candidate, slab_element, atoms)
        coads, h_count, o_count = _coads(atoms)
        pw_meta = _linked_pathway_meta(store, candidate.ref_id)
        row: dict[str, Any] = {
            "ref_id": candidate.ref_id,
            "handle": candidate.handle,
            "name": candidate.name,
            "band": band,
            "dopant": dopant,
            "n_dopant": n_dopant,
            "site": site,
            "coads": coads,
            "tier": str(candidate.flags.get("tier") or "-"),
            "seeds": _seeds_display(pw_meta),
            "barrier": _measure_str(measures, untrusted, "barrier"),
            "span_at_Uopt": _measure_str(measures, untrusted, "span_at_Uopt"),
            "U_L": _measure_str(measures, untrusted, "U_L"),
            "P_side": _measure_str(measures, untrusted, "P_side"),
            "trusted": _trusted_str(candidate),
            "rls": _rls_display(pw_meta),
            "blocker": _blocker_display(candidate),
            "worst_problem": str(candidate.flags.get("worst_problem") or "-"),
        }
        sort_key = (dopant, n_dopant, h_count, o_count, candidate.ref_id)
        built.append((sort_key, row))

    built.sort(key=lambda t: t[0])
    return [row for _key, row in built]


def _cell(col: str, row: dict[str, Any]) -> str:
    v = row.get(col, "")
    s = "" if v is None else str(v)
    limit = _COL_MAX_WIDTH.get(col)
    if limit and len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def _render_table(rows: list[dict[str, Any]]) -> str:
    widths = {col: len(col) for col in _COLUMNS}
    for r in rows:
        for col in _COLUMNS:
            widths[col] = max(widths[col], len(_cell(col, r)))
    header = "  ".join(col.ljust(widths[col]) for col in _COLUMNS)
    lines = [header]
    for r in rows:
        lines.append("  ".join(_cell(col, r).ljust(widths[col]) for col in _COLUMNS))
    return "\n".join(lines)


def render_results_table(
    rows: list[dict[str, Any]], *, token_budget: int = _RESULTS_TABLE_TOKEN_BUDGET
) -> str:
    """A fixed-width aligned text table over ``rows`` (:func:`build_results_rows`
    order — lineage, not band), for the tick prompt.

    Truncates from the END of the lineage order when over ``token_budget``
    (chars/4) — but never drops one of the newest :data:`_KEEP_NEWEST`
    candidates by ``ref_id``, since those are the ones most likely to be
    re-discovered — appending ``"(+K rows omitted)"`` when it does.
    """
    if not rows:
        return "(no candidates yet)"
    working, omitted = fit_rows_to_budget(rows, token_budget=token_budget)
    text = _render_table(working)
    if omitted:
        text += f"\n(+{omitted} rows omitted)"
    return text


def fit_rows_to_budget(
    rows: list[dict[str, Any]],
    *,
    token_budget: int,
    render: Callable[[list[dict[str, Any]]], str] = _render_table,
) -> tuple[list[dict[str, Any]], int]:
    """``(kept_rows, omitted)`` — drop rows from the END of ``rows`` (lineage
    order) until ``render(kept)`` fits ``token_budget`` (chars/4), never
    dropping one of the newest :data:`_KEEP_NEWEST` candidates by ``ref_id``
    (the ones most likely to be re-discovered). ``render`` is the text the
    budget is measured against — the tick's fixed-width table by default;
    the handler's ``view='results'`` passes its own TOON+text renderer so
    the budget covers what it actually returns (gr345353).
    """
    working = list(rows)
    keep_ids = {
        r["ref_id"]
        for r in sorted(rows, key=lambda r: r.get("ref_id", 0), reverse=True)[
            :_KEEP_NEWEST
        ]
    }
    omitted = 0
    text = render(working)
    while len(text) / _CHARS_PER_TOKEN > token_budget:
        drop_idx = None
        for i in range(len(working) - 1, -1, -1):
            if working[i].get("ref_id") not in keep_ids:
                drop_idx = i
                break
        if drop_idx is None:
            break  # everything left is a protected newest-10 row
        working.pop(drop_idx)
        omitted += 1
        text = render(working)
    return working, omitted


#: Public column order — the handler's ``view='results'`` TOON rendering
#: uses this as its explicit ``toon.dump`` schema (dropping the internal
#: ``ref_id`` ordering/truncation key every row also carries).
RESULTS_COLUMNS: tuple[str, ...] = _COLUMNS

__all__ = [
    "RESULTS_COLUMNS",
    "build_results_rows",
    "render_results_table",
]
