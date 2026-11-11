"""Table recipes into measure runs — the deterministic expander behind
the ``survey_extract:table_recipe`` op (gr506762 slice 2, absorbing
corpus-quantitative-extraction T1).

The model's job is *mapping only*: one recipe per parsed grid
(:func:`precis.extract.tables.parse_table`) names which column (or row)
carries which measurand, the printed unit, the subject axis and the
table-level conditions. :func:`expand_recipe` turns that recipe into
:class:`~precis.extract.proposals.RunProposal` runs — every literal is
the verbatim cell text, every anchor is the cell's exact
``[chunk, start, end]`` span (decision 5: a row anchor would collapse
two same-literal cells of one row into one dup-refused write), and
every value is parsed by ``parse_literal``, never read off the model's
reply.

Coordinate spaces — the recipe speaks ZERO-based indices throughout:
``subject_axis``, ``series.index``, ``series.unit_index`` and the
``"r:c"`` keys of ``skip_cells``/``overrides``/``cell`` all index
``parsed.columns`` / ``parsed.rows`` directly. ``meta.table`` rows and
cols are written ONE-based to match the ``view='tables'`` display and
its ``row R col C`` cell cites.

Determinism beats the model's say-so where the grid itself is evidence
(decision 6): a series whose column header (or, for ``"rows"``
orientation, whose row label) carries a citation marker — ``et al.``,
``<sup>56</sup>``, ``[12]``, ``ref.`` — writes ``cited_work`` /
``asserted`` whatever the recipe claims, and so does a cell whose
subject label carries one. A condition anchored ``source: "caption"``
on a table :func:`assign_captions` left unpaired drops
(``caption_missing``) — an ambiguous pairing is worse than none.

The same rule covers log scales (gr506762 slice-4: mx1410/mx1424 stored
``13.1 s^-1`` off a ``log(ν/s⁻¹)`` column the recipe gave no
``transform``): an output series whose label prints ``log``/``log10``/
``lg`` (base 10 — bare ``log`` is the kinetics convention) or
``ln``/``log_e`` stores ``base**cell``; a label naming another base, or a
declared ``transform`` that disagrees with the label, drops
(``log_base_unclear``) rather than guessing. A claimed unit the table
never prints stays a flag — ``unit_unconfirmed`` when no unit appears
anywhere (the unit often lives only in the body text), else
``unit_without_text_support``; ``unit_header_mismatch`` refuses only on
a printed contradiction — a header, caption or notes unit the claim
can't convert to (subset-5: a unitless ``E_b`` column takes its eV
from the caption or a table note).

Subset-2 review (gr506762, jo550561) added three more deterministic
refusals on the same "the grid itself is evidence" footing:

* an output series whose cells are a consecutive integer run ``0..n``
  or ``1..n`` sitting where an index axis sits — first position of its
  axis, or spanning the whole cross axis — is a printed enumeration,
  not a measurement (pa452964's ``m = 0..12`` index row written as 13
  sticking coefficients): the series drops ``index_enumeration`` unless
  its label names the measurand (``cluster size m`` IS the data);
* a series label naming a different quantity than the claimed measurand
  — the small :data:`_LABEL_QUANTITY` table — drops
  ``label_measurand_mismatch`` (``Experimental formation energy`` read
  as adsorption energy; an equilibrium constant read as a saturation
  coverage);
* an output whose subject appears in none of the spans the reviewer
  sees — the cited row's own line, the column header, the caption —
  drops ``subject_not_in_span`` (a label-row subject is another row's
  text and never evidence for this cell).

Subset-3 review (gr506762, jo551208) hardened the evidence the reviewer
is shown and three more silent wrongs:

* every emitted cell's column must print a header the cited span can
  show — a headerless value column drops ``no_header_in_span`` (a
  single-row span with no header or caption reads 'undecidable').
  ``meta.table`` carries the chunk's ``header_span`` and the caption
  text (or the chunk's own leading title when no caption paired), so
  the reviewed span is header cells + data row + title; a
  continuation's header lives in the run head's chunk — its offsets
  can't slice this chunk's text, so ``meta.table`` carries those lines
  as ``header_text`` instead (subset-3 r2);
* a label marking a correction, difference or delta row — the small
  :data:`_CORRECTION_RE` term list — drops ``correction_row``, on its
  series or on the data row whose label marks it (a ``ZPE correction``
  row read as adsorption energies);
* an exact ``0`` on an energy measurand (:func:`_energy_unit`: converts
  to ``J`` or ``J/mol``) whose row label or caption marks it as the
  reference/zero/standard state — or which is the column's sole zero on
  a row labelled a reference phase — drops ``reference_zero`` (graphite
  ``0 eV`` stored as an adsorption energy, mx2188);
* several axis elements naming one measurand emit one run each — the
  recipe's mapped index plus every other column/row whose label names
  it — and each run records the column (or row) it took as a condition
  on the measure, so a wide same-measurand table can no longer silently
  pick ``E_top`` for ``E_fcc`` (mx2224-2228). An auto-matched element
  passes the same series-level checks on its own label and cells —
  the group inherits the measurand, not the mapped column's clean bill
  (subset-3 r2: a ``ΔE_ads correction`` or ``E_ads (kJ/mol)`` column
  next to ``E_ads (eV)`` emitted unchecked, and kJ/mol numbers stored
  as eV); its transform is re-derived from its own label and its unit
  from its own header.

Subset-4 review (gr506762, tables-4: 12 of 12 sampled rows wrong on
pa1820's ``Number of oxide layers`` column stored as saturation
coverages, a sub-header row stored as a value, and the off-by-one
cells mx1505/mx1721/mx1727) extends the same footing:

* a header naming a count — ``Number of``, ``No. of``, ``#``,
  ``count``, ``layers``, a bare ``N`` — pins its axis to a
  dimensionless count (:func:`_count_label` folds into
  :func:`_label_quantity`), never a coverage, energy, rate or barrier
  measurand: ``label_measurand_mismatch`` refuses it. The check fires
  only when the header prints no unit — a ``count rate (s^-1)`` is a
  rate, a ``layers (nm)`` column a thickness. This was the
  incompatibility table's blind spot: ``_LABEL_QUANTITY`` is a term
  list and a count is a category, so the table never matched;
* a data row that is a printed sub-header — non-empty cells mostly
  text or empty, or cells mostly repeating the column headers — never
  emits as a value (``sub_header_row``);
* at review and recheck time the row's literal must sit in the cell at
  the intersection of the claimed row label and column header, not
  just anywhere in the anchor span — :func:`anchor_cell_mismatch`
  names the divergence (``anchor_cell_mismatch``). The same guards
  re-run on stored rows through :func:`stored_row_reasons`, the
  ``put(kind='measure', mode='recheck')`` dry-run's engine.

Subset-5 review (gr506762 round 5 — a prod dry run showed the same
guards dropping valid rows, 360 of 576 sampled rows refused, mostly
false positives):

* a caption-like spanning line as the only detected header row is a
  title, not the column labels — ``parse_table`` pulls the
  column-label row it spans (the ``m = 0..12`` axis) into the header
  for the span check and the review context alike; the index row
  never reaches the data region while ``index_enumeration`` still
  guards a printed index sitting in it (pa452964);
* ``anchor_cell_mismatch`` also compares the located cell's
  coordinates against a partial ``row``/``col`` claim (the mx1505
  literal one column off silently passed) and flags an anchor landing
  in the header region;
* the ``Δ``/``δ`` correction exemption covers the barrier symbols
  ``ΔE‡``/``ΔG‡``/``ΔH‡``/``ΔS‡``/``ΔE_a`` (``Ea``) alongside
  ``ΔE_ads``/``ΔH_ads`` — a barrier column is a reading, not a
  correction row (pa4750's ΔE‡ column, mx1821-1845).

Subset-5b review (gr506762 — review of the subset-5 commit):

* a stored value whose anchor lands in the header region — an index
  row a stacked-header parse pulled up — refuses as
  ``value_in_header_row``: a deterministic verdict, not parser drift
  (mx2049/mx2051-2061 sat in the recheck's review-only bucket instead
  of refused);
* the recheck's ``printed_unit`` caption sniffing ignores
  single-letter tokens — ``in a`` is not a year, ``(b)`` is not a
  barn — except ``K`` and ``s``;
* a printed unit the claim can't spell but that converts is taken as
  the row's unit and converted to canonical at write — the subset-3
  r2 auto-match rule extended to claims — refusing
  ``unit_needs_molar_mass`` when the conversion needs a molar mass;
* ``anchor_cell_mismatch`` also runs at extraction — a model-claimed
  condition literal sitting in a different cell than the claimed
  ``r:c`` is the mx1505 off-by-one, deterministic on the same grid.

Subset-5c review (gr506762 — review of the subset-5b commit) narrowed
the unit rules back out and closed two more silent wrongs:

* a single-letter unit in a COLUMN HEADER is always read —
  ``Overpotential (V)`` prints volts (the 5b filter had left an mV
  claim ``unit_unconfirmed`` and stored it 1000x off). In prose the
  rule holds only against the real noise: a parenthesised lowercase
  ``a``–``h`` is a panel label and an ``in a``/``in an <word>``
  phrase is the article, while ``rates in s`` and ``(V)`` still read;
* ``kJ/mol``/``kcal/mol`` against an energy claim converts through
  Avogadro's number (96.485 kJ/mol = 1 eV) inside
  :func:`precis.taxonomy.measure_units.make_converter` — the
  per-amount bridge is a constant, so ``mg``↔``mol`` stays the only
  ``unit_needs_molar_mass`` case, and then only when the column's own
  header prints it;
* a caption/notes unit is taken for a column only when its dimension
  is compatible with the claimed measurand — a unit naming a
  different column's quantity leaves the claim flagged, as before
  5b.

Subset-5d review (gr506762 — review of the subset-5c commit) narrowed
all three again:

* the Avogadro bridge left ``make_converter``'s default — it fired for
  ANY energy-per-X against ANY energy unit (``150 kJ/mol`` under a
  total-energy ``kJ`` canonical stored 1/N_A off, ``J/(mol K)`` under
  ``J/K`` likewise). It now fires only on the ``molar_bridge`` opt-in,
  only for a unit that IS energy-per-amount against an ``eV``/``meV``
  canonical; the store stays strict, and each spec that needs the
  bridge carries the opt-in as ``MeasureSpec.molar_bridge``
  (:func:`_needs_molar_bridge`);
* a parenthesised lowercase ``a``–``f`` is a footnote marker even in a
  column header — ``E_ads (b)`` prints no unit — while ``(V)``,
  ``(g)``, ``(h)``, ``(s)``, ``(m)`` and the uppercase letters stay
  header units;
* the column's own header unit is the strongest evidence and is TAKEN
  over a differing claim — ``Overpotential (V)`` under an mV claim
  stores 0.523 V; a caption/notes unit is taken second, and only a
  header unit needing a molar mass still refuses
  ``unit_needs_molar_mass``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from typing import Any, TypeGuard

from precis.errors import BadInput
from precis.extract import anchors as _anchors
from precis.extract import verify as _verify
from precis.extract.proposals import (
    _ATTRIBUTIONS,
    _ROLES,
    _TIERS,
    RunProposal,
    VocabEntry,
    _tier,
    resolve_measurand,
)
from precis.extract.tables import (
    _DIMENSIONLESS_HINTS,
    ParsedTable,
    _enumeration_run,
    _int_point,
    has_citation_marker,
    header_unit,
    printed_unit,
)
from precis.store._measures_ops import MeasureAnchor, MeasureSpec
from precis.taxonomy.measure_units import (
    NeedsMolarMass,
    is_potential_unit,
    make_converter,
    parse_literal,
    printed_reference,
)
from precis.taxonomy.money import split_currency
from precis.utils.text import markup_surface, slugify

#: The reader-contract generation stamped on recipe-expanded runs —
#: the recipe op is a different contract from ``build_runs``' per-chunk
#: reader, so rows stay distinguishable.
RECIPE_PROMPT_VERSION = "table-r1"

_TRANSFORMS = {"log10": 10.0, "ln": math.e}
_ORIENTATIONS = {"columns", "rows"}
_SERIES_ROLES = {"output", "input"}
_COND_SOURCES = {"caption", "table"}
_OVERRIDE_FIELDS = {
    "measurand",
    "measurand_status",
    "reference",
    "normalization",
    "unit",
}
_MEASURAND_STATUSES = {"explicit", "interpreted", "ambiguous"}
_TABLE_FIELDS = {
    "orientation",
    "subject_axis",
    "group",
    "attribution",
    "tier",
    "conditions",
    "series",
    "skip_cells",
    "overrides",
}
_SERIES_FIELDS = {
    "index",
    "measurand",
    "unit",
    "unit_index",
    "role",
    "condition",
    "transform",
    "attribution",
    "tier",
}
_CONDITION_FIELDS = {
    "condition",
    "measurand",
    "literal",
    "unit",
    "role",
    "source",
    "cell",
}

#: A cell whose whole text is one of these is a placeholder, not a
#: reading — skipped silently, never a run.
_EMPTY_CELL = frozenset({"", "-", "–", "—", "−", "n/a", "na", "nd", "n.a.", "n.d."})

_CELL_REF_RE = re.compile(r"^(\d+):(\d+)$")

#: Citation spans stripped from a subject label — ``<sup>56</sup>``,
#: ``^{44}``, ``[12,13]``. ``et al.`` stays: it is part of the name.
_CITE_SPAN_RES = (
    re.compile(r"<sup>\s*\d+(?:\s*,\s*\d+)*\s*</sup>"),
    re.compile(r"\^\{?\d+(?:\s*,\s*\d+)*\}?"),
    re.compile(r"\[\d+(?:\s*,\s*\d+)*\]"),
)


@dataclass(frozen=True, slots=True)
class SeriesSpec:
    """One normalized ``series[]`` entry — a column (``columns``) or a
    row (``rows``) the recipe maps to a measurand."""

    index: int
    measurand: str | None
    unit: str | None
    unit_index: int | None
    role: str
    condition: str | None
    transform: str | None
    attribution: str | None
    tier: str | None


@dataclass(frozen=True, slots=True)
class ConditionSpec:
    """One normalized ``conditions[]`` entry — a table-level input row."""

    condition: str
    measurand: str | None
    literal: str
    unit: str | None
    role: str
    source: str  # "caption" | "table"
    cell: tuple[int, int] | None  # 0-based (row, col)


@dataclass(frozen=True, slots=True)
class TableRecipe:
    """The validated recipe. ``no_results`` carries the ``reason`` of a
    ``{"table": null}`` reply — the table writes nothing on purpose."""

    orientation: str | None = None
    subject_axis: int | None = None
    group: str | None = None
    attribution: str = "not_established"
    tier: str | None = None
    conditions: tuple[ConditionSpec, ...] = ()
    series: tuple[SeriesSpec, ...] = ()
    skip_cells: frozenset[tuple[int, int]] = frozenset()
    overrides: dict[tuple[int, int], dict[str, Any]] = field(default_factory=dict)
    no_results: str | None = None


def _drop(location: str, reason: str, detail: object) -> dict[str, Any]:
    return {"location": location, "reason": reason, "detail": str(detail)[:200]}


def _is_int(raw: object) -> TypeGuard[int]:
    return isinstance(raw, int) and not isinstance(raw, bool)


def _cell_ref(raw: object, n_rows: int, n_cols: int) -> tuple[int, int] | None:
    """Parse a ``"r:c"`` cell key to its 0-based ``(row, col)``; ``None``
    when malformed or outside the grid's data rows."""
    if not isinstance(raw, str):
        return None
    m = _CELL_REF_RE.match(raw.strip())
    if m is None:
        return None
    r, c = int(m.group(1)), int(m.group(2))
    if 0 <= r < n_rows and 0 <= c < n_cols:
        return r, c
    return None


def validate_recipe(
    parsed: ParsedTable, recipe: dict[str, Any]
) -> tuple[TableRecipe | None, list[dict[str, Any]]]:
    """Shape-check the model's ``{"table": …}`` reply into a
    :class:`TableRecipe`, collecting one ``bad_shape`` drop per
    malformed element. Returns ``(None, drops)`` when the recipe is
    unusable whole — ``table`` not an object, a bad ``orientation``, or
    a ``subject_axis`` naming no axis — since a recipe that maps nothing
    correctly expands to nothing rather than to wrong rows. Index
    fields are 0-based on ``parsed.columns``/``parsed.rows``; ``"r:c"``
    cell keys are 0-based ``row:col`` into the grid's data rows."""
    drops: list[dict[str, Any]] = []
    n_cols = len(parsed.columns)
    n_rows = len(parsed.rows)
    if not isinstance(recipe, dict) or "table" not in recipe:
        return None, [_drop("recipe", "bad_shape", "reply carries no 'table' key")]
    raw_table = recipe["table"]
    if raw_table is None:
        reason = recipe.get("reason")
        return (
            TableRecipe(
                no_results=(
                    str(reason)
                    if isinstance(reason, str) and reason.strip()
                    else "no results"
                )
            ),
            drops,
        )
    if not isinstance(raw_table, dict):
        return None, [
            _drop("table", "bad_shape", f"table is {type(raw_table).__name__}")
        ]
    unknown_top = sorted(set(raw_table) - _TABLE_FIELDS)
    if unknown_top:
        drops.append(_drop("table", "bad_shape", f"unknown field(s) {unknown_top}"))
    orientation = raw_table.get("orientation")
    if orientation not in _ORIENTATIONS:
        return None, drops + [
            _drop("table", "bad_shape", f"orientation {orientation!r}")
        ]
    subject_axis = raw_table.get("subject_axis")
    axis_len = n_cols if orientation == "columns" else n_rows
    if subject_axis is not None:
        if not _is_int(subject_axis) or not 0 <= int(subject_axis) < max(axis_len, 1):
            return None, drops + [
                _drop("table", "bad_shape", f"subject_axis {subject_axis!r}")
            ]
        subject_axis = int(subject_axis)
    group = raw_table.get("group")
    attribution = (
        raw_table["attribution"]
        if raw_table.get("attribution") in _ATTRIBUTIONS
        else "not_established"
    )
    tier = raw_table["tier"] if raw_table.get("tier") in _TIERS else None

    conditions: list[ConditionSpec] = []
    raw_conds = raw_table.get("conditions") or []
    if not isinstance(raw_conds, list):
        drops.append(_drop("table.conditions", "bad_shape", "not a list"))
        raw_conds = []
    for i, raw_c in enumerate(raw_conds):
        loc = f"conditions[{i}]"
        if not isinstance(raw_c, dict):
            drops.append(_drop(loc, "bad_shape", "not an object"))
            continue
        unknown = sorted(set(raw_c) - _CONDITION_FIELDS)
        if unknown:
            drops.append(_drop(loc, "bad_shape", f"unknown field(s) {unknown}"))
            continue
        literal = raw_c.get("literal")
        if not isinstance(literal, str) or not literal.strip():
            drops.append(_drop(loc, "bad_shape", "missing literal"))
            continue
        name = raw_c.get("condition")
        if not isinstance(name, str) or not name.strip():
            drops.append(_drop(loc, "bad_shape", "missing condition name"))
            continue
        cell = _cell_ref(raw_c.get("cell"), n_rows, n_cols)
        if raw_c.get("cell") is not None and cell is None:
            drops.append(
                _drop(
                    loc,
                    "bad_shape",
                    f"cell {raw_c.get('cell')!r} is off the grid",
                )
            )
            continue
        source = raw_c.get("source")
        if source is None:
            source = "table" if cell is not None else "caption"
        if source not in _COND_SOURCES:
            drops.append(_drop(loc, "bad_shape", f"source {source!r}"))
            continue
        if source == "table" and cell is None:
            drops.append(_drop(loc, "bad_shape", "source 'table' needs a 'r:c' cell"))
            continue
        conditions.append(
            ConditionSpec(
                condition=name.strip(),
                measurand=(
                    raw_c["measurand"]
                    if isinstance(raw_c.get("measurand"), str)
                    else None
                ),
                literal=literal.strip(),
                unit=(
                    raw_c["unit"].strip()
                    if isinstance(raw_c.get("unit"), str) and raw_c["unit"].strip()
                    else None
                ),
                role=raw_c["role"] if raw_c.get("role") in _ROLES else "context",
                source=source,
                cell=cell,
            )
        )

    series: list[SeriesSpec] = []
    raw_series = raw_table.get("series") or []
    if not isinstance(raw_series, list):
        drops.append(_drop("table.series", "bad_shape", "not a list"))
        raw_series = []
    axis_n = n_cols if orientation == "columns" else n_rows
    for i, raw_s in enumerate(raw_series):
        loc = f"series[{i}]"
        if not isinstance(raw_s, dict):
            drops.append(_drop(loc, "bad_shape", "not an object"))
            continue
        unknown = sorted(set(raw_s) - _SERIES_FIELDS)
        if unknown:
            drops.append(_drop(loc, "bad_shape", f"unknown field(s) {unknown}"))
            continue
        index = raw_s.get("index")
        if not _is_int(index) or not 0 <= int(index) < axis_n:
            drops.append(_drop(loc, "bad_shape", f"index {index!r} is off the axis"))
            continue
        if subject_axis is not None and int(index) == subject_axis:
            drops.append(
                _drop(
                    loc,
                    "bad_shape",
                    "index names the subject axis — those cells are labels, "
                    "not readings",
                )
            )
            continue
        transform = raw_s.get("transform")
        if transform is not None and transform not in _TRANSFORMS:
            drops.append(_drop(loc, "bad_shape", f"transform {transform!r}"))
            continue
        unit_index = raw_s.get("unit_index")
        if unit_index is not None and (
            not _is_int(unit_index) or not 0 <= int(unit_index) < axis_n
        ):
            drops.append(
                _drop(loc, "bad_shape", f"unit_index {unit_index!r} is off the axis")
            )
            continue
        role = raw_s.get("role") or "output"
        if role not in _SERIES_ROLES:
            drops.append(_drop(loc, "bad_shape", f"role {role!r}"))
            continue
        series.append(
            SeriesSpec(
                index=int(index),
                measurand=(
                    raw_s["measurand"]
                    if isinstance(raw_s.get("measurand"), str)
                    else None
                ),
                unit=(
                    raw_s["unit"].strip()
                    if isinstance(raw_s.get("unit"), str) and raw_s["unit"].strip()
                    else None
                ),
                unit_index=(int(unit_index) if unit_index is not None else None),
                role=role,
                condition=(
                    raw_s["condition"].strip()
                    if isinstance(raw_s.get("condition"), str)
                    and raw_s["condition"].strip()
                    else None
                ),
                transform=transform,
                attribution=(
                    raw_s["attribution"]
                    if raw_s.get("attribution") in _ATTRIBUTIONS
                    else None
                ),
                tier=raw_s["tier"] if raw_s.get("tier") in _TIERS else None,
            )
        )

    skip_cells: set[tuple[int, int]] = set()
    raw_skip = raw_table.get("skip_cells") or []
    if not isinstance(raw_skip, list):
        drops.append(_drop("table.skip_cells", "bad_shape", "not a list"))
        raw_skip = []
    for raw_cell in raw_skip:
        cell = _cell_ref(raw_cell, n_rows, n_cols)
        if cell is None:
            drops.append(
                _drop(
                    "table.skip_cells",
                    "bad_shape",
                    f"cell {raw_cell!r} is off the grid",
                )
            )
            continue
        skip_cells.add(cell)

    overrides: dict[tuple[int, int], dict[str, Any]] = {}
    raw_over = raw_table.get("overrides") or {}
    if not isinstance(raw_over, dict):
        drops.append(_drop("table.overrides", "bad_shape", "not an object"))
        raw_over = {}
    for key, raw_o in raw_over.items():
        cell = _cell_ref(key, n_rows, n_cols)
        if cell is None:
            drops.append(
                _drop(f"overrides[{key}]", "bad_shape", "cell is off the grid")
            )
            continue
        if not isinstance(raw_o, dict) or not set(raw_o) <= _OVERRIDE_FIELDS:
            drops.append(
                _drop(
                    f"overrides[{key}]",
                    "bad_shape",
                    "an override may only change measurand, measurand_status, "
                    "reference, normalization and unit — never literal",
                )
            )
            continue
        overrides[cell] = raw_o

    return (
        TableRecipe(
            orientation=orientation,
            subject_axis=subject_axis,
            group=str(group) if isinstance(group, str) and group.strip() else None,
            attribution=attribution,
            tier=tier,
            conditions=tuple(conditions),
            series=tuple(series),
            skip_cells=frozenset(skip_cells),
            overrides=overrides,
        ),
        drops,
    )


def _subject_surface(text: str) -> str:
    """A cell or header as a subject label: markup-folded, citation
    spans stripped, separators collapsed."""
    out = markup_surface(text)
    for rx in _CITE_SPAN_RES:
        out = rx.sub("", out)
    return " ".join(out.split()).strip(" /").strip()


#: Measurands whose standard symbol is a bare ``n``/``N`` — a
#: ``| n |`` column under one of these is the measurand, not a count
#: (subset-4c: no seed writes a ``symbol`` meta key, so the exemption
#: keys on the slug/name the seeds actually write; ``entry.symbol``
#: is still honoured when a taxon declares it).
_N_SYMBOL_SLUGS = frozenset({"reaction-order", "refractive-index"})
_N_SYMBOL_NAMES = frozenset({"reaction order", "refractive index"})


def _label_names_measurand(label: str, entry: VocabEntry) -> bool:
    """The measurand's name or an alias appears in the series label —
    case-insensitive on the folded surface (``explicit`` evidence). A
    label that IS the measurand's declared symbol counts too: a bare
    ``n`` on a reaction-order column names the measurand, not a count
    (subset-4b/4c)."""
    hay = " ".join(markup_surface(label).split()).casefold()
    if (
        entry.symbol
        and hay == " ".join(markup_surface(entry.symbol).split()).casefold()
    ):
        return True
    if re.fullmatch(r"[Nn]", hay) and (
        slugify(entry.slug) in _N_SYMBOL_SLUGS
        or " ".join(markup_surface(entry.name).split()).casefold() in _N_SYMBOL_NAMES
    ):
        return True
    for key in (entry.name, *entry.aliases):
        if key and " ".join(markup_surface(key).split()).casefold() in hay:
            return True
    return False


def _entry_categorical(entry: VocabEntry) -> bool:
    """The measurand's declared value type is categorical — its value
    cells are labels, not numbers (the seed meta's ``value_type`` /
    ``dimension_kind``, subset-4c)."""
    return entry.value_type == "categorical" or entry.dimension_kind == "categorical"


#: A log token in a series label: ``log``/``Log``/``LOG`` with an
#: optional ``10``/``_10``/``e``/``_e`` base, ``lg`` or ``ln`` (lowercase
#: only — ``Ln`` is the lanthanide symbol). Braces are stripped first so
#: ``\log_{10}`` reads as ``log_10``.
_LOG_LABEL_RE = re.compile(
    r"(?<![A-Za-z])(?:(?:[Ll]og|LOG)(?:_?(?P<base>\d+|e)(?![A-Za-z0-9]))?"
    r"|(?P<lg>lg)|(?P<ln>ln))(?![A-Za-z])"
)

#: Units a header-only claim may carry only when the text prints them or
#: the label names the measurand (plain-surface, whitespace-free).
_GUESSED_UNITS = frozenset({"eV", "%", "s-1", "1/s", "/s"})


def _label_log_transform(label: str) -> str | None:
    """The transform a series label's log token implies: ``'log10'``,
    ``'ln'``, ``None`` when the label prints no log, ``'unclear'`` for
    any other base or a label mixing two bases."""
    found: set[str] = set()
    for m in _LOG_LABEL_RE.finditer(label.replace("{", "").replace("}", "")):
        base = m.group("base")
        if m.group("ln") or base == "e":
            found.add("ln")
        elif m.group("lg") or base in (None, "10"):
            found.add("log10")
        else:
            found.add("unclear")
    if not found:
        return None
    return found.pop() if len(found) == 1 else "unclear"


def _guessed_unit(unit: str) -> bool:
    return re.sub(r"\s+", "", _verify.unit_surface(unit)) in _GUESSED_UNITS


#: Label terms that pin an axis to a named quantity. Kept small and
#: explicit on purpose — the observed subset-2 misreads are
#: ``Experimental formation energy`` taken for adsorption energy
#: (mx2035-2039) and an equilibrium constant taken for a saturation
#: coverage (mx2042-2046). Deliberate near-synonyms stay out (``binding
#: energy`` is how catalysis papers print adsorption energy — listing
#: it would refuse correct rows).
_LABEL_QUANTITY: dict[str, str] = {
    "formation energy": "formation energy",
    "formation enthalpy": "formation energy",
    "heat of formation": "formation energy",
    "equilibrium constant": "equilibrium constant",
    # subset-3 (mx2216): "absorption energy" is not adsorption energy —
    # E_abs is a different printed symbol, not an alias (the terms match
    # the folded surface, where ``E_abs`` reads ``Eabs``).
    "absorption energy": "absorption energy",
    "eabs": "absorption energy",
}


#: A label opening ``number of`` / ``no. of`` / ``nr. of`` pins its
#: axis to a count of whatever follows — a count always wins over the
#: term table (``number of equilibrium constants`` IS a count).
_COUNT_OPENER_RE = re.compile(r"^\s*(?:number|no|nr|num)\.?\s+of\b", re.IGNORECASE)

#: The trailing count markers — word-boundary ``count``/``counts`` and
#: a standalone ``#``. Checked only when the header prints no unit: a
#: ``count rate (s^-1)`` is a rate, and the unit — not the word —
#: decides (subset-4).
_COUNT_WORD_RE = re.compile(
    r"(?<![A-Za-z0-9])counts?(?![A-Za-z0-9])|(?<!\S)#(?!\S)",
    re.IGNORECASE,
)

#: ``layer``/``layers`` as a count — only when preceded by a count
#: opener (``number of``, ``no. of``, ``n of``, ``#``) or, for the
#: plural, ending the label / followed only by a parenthetical:
#: ``oxide layers`` and ``no. of layers`` are counts, but
#: ``E_ads, top layer`` and ``Layer 1`` name a position, not a count
#: (subset-4b).
_LAYER_COUNT_RE = re.compile(
    r"(?:"
    r"(?<![A-Za-z0-9])(?:number|no|nr|num|n)\.?\s+of\b.*?\blayers?\b"
    r"|(?<!\S)#\s*\S*\s*layers?\b"
    r"|\blayers\b\s*(?:\([^)]*\))?\s*$"
    r")",
    re.IGNORECASE,
)


def _count_label(label: str) -> str | None:
    """``'count'`` when the label pins the axis to a dimensionless
    count — a header naming a number of things can never map to a
    coverage, energy, rate or barrier measurand (gr506762 subset-4:
    pa1820's ``Number of oxide layers`` written as 12 saturation
    coverages while ``_LABEL_QUANTITY`` matched nothing — a count is a
    category, not a term). ``None`` for a unit-carrying label that
    opens on something other than ``number of`` — the unit decides.
    A bare ``n``/``N`` counts only against a measurand that doesn't
    claim that symbol — :func:`_label_names_measurand` exempts it."""
    surf = " ".join(markup_surface(label).split())
    if not surf:
        return None
    if re.fullmatch(r"[Nn]", surf) or _COUNT_OPENER_RE.match(surf):
        return "count"
    if header_unit(label) is not None:
        return None
    if _COUNT_WORD_RE.search(surf) or _LAYER_COUNT_RE.search(surf):
        return "count"
    return None


#: Label terms marking a correction, difference or delta row — never a
#: reading of the measurand (subset-3: a ``ZPE correction`` / ``ΔE``
#: row stored as adsorption energies, mx2224-2228). Kept to the
#: observed misreads; word-boundary tokens so ``diffusion`` and
#: ``errors`` stay out. The symbols are narrower than the words
#: (subset-3 r2): ``Δ``/``δ`` names a correction term — ``ΔZPE``,
#: ``Δ(CCSD-PBE)`` — but ``ΔE_ads``/``ΔH_ads``/``ΔG_ads``/``ΔS_ads``
#: (folded: ``ΔEads`` …) is the measurand's own symbol, the barrier
#: symbols ``ΔE‡``/``ΔG‡``/``ΔH‡``/``ΔS‡``/``ΔE_a`` (folded: ``ΔE‡``
#: …, ``ΔEa``) are genuine barriers not corrections (subset-5,
#: pa4750's ΔE‡ column read as a delta row). Reaction energies
#: ``ΔE_r``/``ΔG_r`` (folded: ``ΔEr``/``ΔGr``) likewise name the
#: measurand itself, with a non-letter boundary to exclude longer words.
#: ``zpe`` counts only as the term itself — a ``ZPE-corrected`` or
#: ``corrected for ZPE`` energy IS the corrected measurand.
_CORRECTION_RE = re.compile(
    r"[Δδ](?!\s*(?:[EGHS]\s*(?:ads|des)(?:orption)?\b"
    r"|[EGHS]\s*‡|Ea(?![A-Za-z])|[EG]\s*r(?![A-Za-z])))"
    r"|(?<![A-Za-z0-9])(?:correction|delta|difference|diff|error)"
    r"(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])(?<!corrected )(?<!corrected for )zpe"
    r"(?![A-Za-z0-9])(?!\s*-?\s*correct)",
    re.IGNORECASE,
)


def _correction_label(label: str) -> str | None:
    """The correction/delta term the label prints, else ``None``."""
    m = _CORRECTION_RE.search(" ".join(markup_surface(label).split()))
    return m.group(0) if m is not None else None


#: Words marking the zero of an energy scale — on a row label or a
#: caption, an exact-0 cell they name is the reference point, never a
#: measured adsorption/desorption energy (subset-3, mx2188).
_REFERENCE_MARK_RE = re.compile(
    r"(?<![A-Za-z])(?:reference|standard state|zero|baseline)(?![A-Za-z])",
    re.IGNORECASE,
)

#: Row labels naming a phase that conventionally IS the reference —
#: only fires on a column's sole zero, the narrower half of the rule.
_REFERENCE_PHASE_RE = re.compile(
    r"(?<![A-Za-z])(?:graphite|bulk|clean surface|pristine|bare surface"
    r"|vacuum|gas[- ]phase)(?![A-Za-z])",
    re.IGNORECASE,
)


def _energy_unit(unit: str | None) -> bool:
    """The measurand's canonical unit is an energy: converts to ``J``
    (per particle — eV, meV, Ha) or to ``J/mol`` (molar — kJ/mol)."""
    if not unit:
        return False
    for target in ("J", "J/mol"):
        try:
            make_converter(unit, target)
        except (BadInput, NeedsMolarMass):
            continue
        return True
    return False


def _table_title(text: str | None) -> str:
    """The chunk's leading non-pipe text — the nearest table title when
    ``assign_captions`` paired no caption (a table chunk opening on its
    own ``Table N`` line)."""
    if not text:
        return ""
    lines: list[str] = []
    for ln in text.split("\n"):
        if ln.lstrip().startswith("|"):
            break
        lines.append(ln)
    return " ".join(" ".join(lines).split()).strip()[:300]


def _label_quantity(label: str) -> str | None:
    """The quantity the label's terms pin it to, else ``None`` —
    the ``_LABEL_QUANTITY`` terms, then the dimensionless-count
    category (:func:`_count_label`, subset-4)."""
    if _COUNT_OPENER_RE.match(" ".join(markup_surface(label).split())):
        return "count"
    hay = " ".join(markup_surface(label).split()).casefold()
    for term, quantity in _LABEL_QUANTITY.items():
        if term in hay:
            return quantity
    return _count_label(label)


def _unit_mismatch(unit: str, entry: VocabEntry) -> str | None:
    """``None`` when ``unit`` converts to the measurand's canonical unit
    (or is a currency token — the S1 currency path inside
    ``insert_measure`` owns those); else the
    ``unit_dimension_mismatch`` detail naming both units. A
    ``NeedsMolarMass`` bridge is not a mismatch — the store's own
    ``no_molar_mass`` escalation handles it at write. The table path
    opts in to the Avogadro bridge (``molar_bridge=True``): a
    ``kJ/mol`` unit against an ``eV``/``meV`` canonical converts at
    write, while ``kJ/mol`` against ``kJ`` or ``J/(mol K)`` against
    ``J/K`` still refuses (subset-5d)."""
    canonical = entry.canonical_unit
    if not canonical:
        return (
            None
            if not unit.strip()
            else f"unit {unit!r} on a measurand with no canonical unit"
        )
    try:
        if (
            split_currency(unit).currency is not None
            and split_currency(canonical).currency is None
        ):
            return (
                f"currency unit {unit!r} has no dimension match with the "
                f"measurand's canonical unit {canonical!r}"
            )
    except BadInput as exc:
        return str(exc)
    try:
        make_converter(unit, canonical, molar_bridge=True)
    except NeedsMolarMass:
        return None
    except BadInput as exc:
        return str(exc)
    return None


def _needs_molar_bridge(unit: str | None, canonical: str | None) -> bool:
    """``unit`` reaches ``canonical`` only through the Avogadro bridge
    (an ``<energy>/mol`` unit against an ``eV``/``meV`` canonical) —
    the spec's ``molar_bridge`` opt-in the store's strict
    ``make_converter`` needs to write the row (subset-5d)."""
    if not unit or not canonical:
        return False
    try:
        make_converter(unit, canonical)
    except NeedsMolarMass:
        return False
    except BadInput:
        pass
    else:
        return False
    try:
        make_converter(unit, canonical, molar_bridge=True)
    except (BadInput, NeedsMolarMass):
        return False
    return True


def _notes_text(chunk_text: str | None) -> str:
    """The chunk's non-pipe text — the leading title and the trailing
    footnote/notes lines where a unit the header doesn't print still
    shows up (gr506762 subset-5: pa255303's E_b column takes its eV
    from a table note)."""
    if not chunk_text:
        return ""
    return " ".join(
        ln.strip() for ln in chunk_text.split("\n") if not ln.lstrip().startswith("|")
    )


def _unit_conflicts(printed: str, claimed: str) -> bool:
    """A unit the table prints contradicts the claimed unit — different
    dimensions (``(K)`` under an ``eV`` claim). A convertible spelling
    (``J`` vs ``eV``) is not a contradiction — nor is ``kJ/mol`` against
    an ``eV`` claim (the table path's ``molar_bridge`` opt-in,
    subset-5d) — and a dimensionless hint (``ML``/``%``) agrees with a
    dimensionless claim."""
    if printed == claimed:
        return False
    try:
        make_converter(printed, claimed, molar_bridge=True)
    except NeedsMolarMass:
        return False
    except BadInput:
        pass
    else:
        return False
    if printed in _DIMENSIONLESS_HINTS:
        try:
            make_converter(claimed, "1")
        except (BadInput, NeedsMolarMass):
            return True
        return False
    return True


def _conflicting_unit(claimed: str, label: str | None, *pieces: str) -> str | None:
    """The first printed unit contradicting ``claimed`` — checked per
    evidence piece so a line that itself spells the claimed unit can't
    conflict. ``label`` is the series's own axis label and reads under
    header rules (:func:`header_unit` — a header's single letter is a
    unit, ``(V)`` volts, except the ``(a)``–``(f)`` footnote markers);
    ``pieces`` are the caption and table notes, prose
    (:func:`printed_unit` — ``(b)`` is a panel label)."""
    candidates: list[tuple[str, str]] = []
    if label and (u := header_unit(label)) is not None:
        candidates.append((u, label))
    for piece in pieces:
        if piece and (u := printed_unit(piece)) is not None:
            candidates.append((u, piece))
    for printed, piece in candidates:
        if _unit_conflicts(printed, claimed) and not _verify.unit_has_text_support(
            claimed, piece
        ):
            return printed
    return None


def _sub_header_row(
    parsed: ParsedTable,
    r: int,
    label_col: int = 0,
    value_cols: set[int] | frozenset[int] = frozenset(),
    categorical: bool = False,
) -> bool:
    """A data row that is a printed sub-header, not a reading row —
    subset-4's mx2313 stored as a value. Narrowed in subset-4b after
    the numeric-share rule dropped ordinary data rows
    (``Pt(111) | top | -0.45 | PBE`` — text in a site/method column is
    data, not a divider): the row is a sub-header only when *every*
    cell in the ``value_cols`` the extraction reads is empty or
    non-numeric, AND its shape says divider — the non-empty cells
    mostly repeat their column headers (a second header line eaten as
    data), text sits only in the label cell, or one lone non-numeric
    cell floats among empties (a span/merge). A numeric value cell
    ends the question — never judge by the share of numeric cells
    across the whole row."""
    if not 0 <= r < len(parsed.rows):
        return False
    row = parsed.rows[r]
    vals = [
        row[c]
        for c in value_cols
        if c != label_col
        and 0 <= c < len(row)
        and row[c].value_text.strip().casefold() not in _EMPTY_CELL
    ]
    if any(c.numeric for c in vals):
        return False
    nonempty = [
        (j, c)
        for j, c in enumerate(row)
        if c.value_text.strip().casefold() not in _EMPTY_CELL
    ]
    if not nonempty:
        return False
    echo = sum(
        1
        for j, c in nonempty
        if j < len(parsed.columns)
        and (h := " ".join(markup_surface(parsed.columns[j].header).split()).casefold())
        and len(cs := " ".join(markup_surface(c.text).split()).casefold()) >= 3
        and (cs in h or h in cs)
    )
    if echo * 2 > len(nonempty):
        return True
    if all(j == label_col for j, _c in nonempty):
        return True
    nonlabel = [(j, c) for j, c in nonempty if j != label_col]
    if not (
        len(nonlabel) == 1 and not nonlabel[0][1].numeric and len(nonempty) < len(row)
    ):
        return False
    # A lone text cell amid empties reads as a span/merge — unless it
    # IS the value a categorical measurand reads
    # (``Pt(111) | top | --`` for an adsorption-site measurand,
    # subset-4c): then the text is the reading, not a divider.
    j, _cell = nonlabel[0]
    return not (categorical and j in value_cols)


def _offset_cell(parsed: ParsedTable, start: int, end: int) -> tuple[int, int] | None:
    """The grid cell covering ``[start, end)``: exact containment wins;
    else the cell the anchor most overlaps. ``None`` when no cell
    overlaps at all."""
    hit: tuple[int, int] | None = None
    best = 0
    for r, row in enumerate(parsed.rows):
        for c, cell in enumerate(row):
            if start < end and cell.start <= start and end <= cell.end:
                return r, c
            overlap = min(end, cell.end) - max(start, cell.start)
            if overlap > best:
                hit, best = (r, c), overlap
    return hit


def _literal_cell(
    parsed: ParsedTable,
    start: int,
    end: int,
    literal: str,
    chunk_text: str,
) -> tuple[int, int] | None:
    """The grid cell the ``[start, end)`` anchor belongs to — for an
    old row-level span covering several cells, the one whose own text
    supports ``literal`` (subset-4b: the largest-overlap fallback could
    name the wrong cell, poisoning the review context and the
    anchor-cell check). ``None`` when the span reaches no cell, or when
    zero or several overlapped cells carry the literal — ambiguity is
    'unlocated', never a guess."""
    hit: tuple[int, int] | None = None
    cells: list[tuple[int, int]] = []
    for r, row in enumerate(parsed.rows):
        for c, cell in enumerate(row):
            if start < end and cell.start <= start and end <= cell.end:
                return r, c
            if min(end, cell.end) - max(start, cell.start) > 0:
                cells.append((r, c))
    if len(cells) == 1:
        hit = cells[0]
    elif cells:
        matches = [
            (r, c)
            for r, c in cells
            if _verify.literal_supported(
                literal,
                chunk_text[parsed.rows[r][c].start : parsed.rows[r][c].end],
                chunk_text,
                (parsed.rows[r][c].start, parsed.rows[r][c].end),
                numeric_required=False,
            )
        ]
        hit = matches[0] if len(matches) == 1 else None
    return hit


def _span_offsets(span: Any) -> tuple[int, int] | None:
    """The ``[chunk, start, end]`` anchor's int offsets, else ``None``."""
    if not isinstance(span, (list, tuple)) or len(span) != 3:
        return None
    start, end = span[1], span[2]
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in (start, end)):
        return None
    return start, end


def _in_header(parsed: ParsedTable, start: int, end: int) -> bool:
    """The ``[start, end)`` anchor overlaps the chunk's header region
    (``header_span``) — a header/index line, not a data cell."""
    return (
        parsed.header_span is not None
        and start < parsed.header_span[1]
        and end > parsed.header_span[0]
    )


def _label_at(parsed: ParsedTable, r: int, c: int, orientation: str) -> str:
    """The emitting axis's label for cell ``(r, c)`` — its column
    header (``columns``) or its row's label cell (``rows``)."""
    if orientation == "columns":
        return parsed.columns[c].header if 0 <= c < len(parsed.columns) else ""
    row = parsed.rows[r] if 0 <= r < len(parsed.rows) else []
    return row[0].text if row else ""


def _cross_label_at(parsed: ParsedTable, r: int, c: int, orientation: str) -> str:
    """The other axis's label for cell ``(r, c)`` — the row's label
    cell (``columns``) or the column header (``rows``)."""
    if orientation == "columns":
        row = parsed.rows[r] if 0 <= r < len(parsed.rows) else []
        return row[0].text if row else ""
    return parsed.columns[c].header if 0 <= c < len(parsed.columns) else ""


def _row_has_label(parsed: ParsedTable, r: int, label: str, skip: int) -> bool:
    """The folded ``label`` occurs in a cell of row ``r`` other than
    column ``skip``."""
    want = " ".join(markup_surface(label).split()).casefold()
    if not want or not 0 <= r < len(parsed.rows):
        return False
    for j, cell in enumerate(parsed.rows[r]):
        if j == skip:
            continue
        if " ".join(markup_surface(cell.text).split()).casefold() == want:
            return True
    return False


def _row_label_ok(parsed: ParsedTable, r: int, c: int, claim: str) -> bool:
    """The claimed row label sits in row ``r`` — in a cell other than
    ``c``, or in ``c`` itself when ``c`` is the label's own axis: a
    condition or output anchored on the subject column carries the
    claimed label AS its cell text, not in a sibling cell (gr506762
    subset-5c)."""
    if _row_has_label(parsed, r, claim, c):
        return True
    if not (0 <= r < len(parsed.rows) and 0 <= c < len(parsed.rows[r])):
        return False
    want = " ".join(markup_surface(claim).split()).casefold()
    got = " ".join(markup_surface(parsed.rows[r][c].text).split()).casefold()
    return bool(want) and got == want


def _claim_coord(meta: dict[str, Any], key: str) -> int | None:
    """``meta.table``'s 1-based ``row``/``col`` as a 0-based index."""
    raw = meta.get(key)
    return raw - 1 if isinstance(raw, int) and not isinstance(raw, bool) else None


def anchor_cell_mismatch(
    parsed: ParsedTable,
    *,
    span: Any,
    literal: str,
    table_meta: dict[str, Any] | None,
    chunk_text: str,
) -> str | None:
    """``detail`` when the stored row's literal does not sit in the
    cell at the intersection of the claimed row label and column
    header — the off-by-one-column cases (gr506762 subset-4,
    mx1505/mx1721/mx1727). ``None`` when the anchor resolves
    consistently, or too little is claimed to check.

    ``span`` is the row's ``[chunk, start, end]`` anchor; the claims
    are ``meta.table``'s 1-based ``row``/``col`` and the
    ``col_header``/``row_label``/``header`` labels."""
    meta = table_meta or {}
    if not isinstance(span, (list, tuple)) or len(span) != 3:
        return None
    _cid, start, end = span
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in (start, end)):
        return None
    orientation = meta.get("orientation") or "columns"
    claimed_r = _claim_coord(meta, "row")
    claimed_c = _claim_coord(meta, "col")
    claim_col = meta.get("col_header") or (
        meta.get("header") if orientation == "columns" else None
    )
    claim_row = meta.get("row_label") or (
        meta.get("header") if orientation == "rows" else None
    )
    n_cols = len(parsed.columns)

    def cell_ok(r: int, c: int) -> bool:
        cell = parsed.rows[r][c]
        return _verify.literal_supported(
            literal,
            chunk_text[cell.start : cell.end],
            chunk_text,
            (cell.start, cell.end),
            numeric_required=False,
        )

    if claimed_r is not None and claimed_c is not None:
        if not (
            0 <= claimed_r < len(parsed.rows)
            and claimed_c < len(parsed.rows[claimed_r])
        ):
            return (
                f"claimed cell row {claimed_r + 1} col {claimed_c + 1} is off the grid"
            )
        cell = parsed.rows[claimed_r][claimed_c]
        if min(end, cell.end) - max(start, cell.start) <= 0:
            located = _literal_cell(parsed, start, end, literal, chunk_text)
            if located is not None:
                where = f"row {located[0] + 1} col {located[1] + 1}"
            elif _in_header(parsed, start, end):
                where = "the header rows"
            else:
                where = "outside the grid"
            return (
                f"the anchor sits in {where}, not the claimed "
                f"row {claimed_r + 1} col {claimed_c + 1}"
            )
        if not cell_ok(claimed_r, claimed_c):
            return (
                f"literal {literal!r} is not in the claimed cell "
                f"(row {claimed_r + 1} col {claimed_c + 1}) "
                f"{cell.text[:80]!r}"
            )
        if (
            claim_col
            and 0 <= claimed_c < n_cols
            and (
                " ".join(
                    markup_surface(parsed.columns[claimed_c].header).split()
                ).casefold()
                != " ".join(markup_surface(str(claim_col)).split()).casefold()
            )
        ):
            return (
                f"claimed column header {claim_col!r} vs the grid's "
                f"{parsed.columns[claimed_c].header!r}"
            )
        if claim_row and not _row_label_ok(
            parsed, claimed_r, claimed_c, str(claim_row)
        ):
            return f"claimed row label {claim_row!r} is not in the anchor row"
        return None
    located = _literal_cell(parsed, start, end, literal, chunk_text)
    if located is None:
        # The anchor lands in the header region — a header/index line
        # stored as a value stays refused (subset-5: pa452964's
        # ``m = 0..12`` row under its promoted stacked header).
        if _in_header(parsed, start, end):
            return (
                "the anchor sits in the header rows — a header/index "
                "line, not a data cell"
            )
        return None
    r, c = located
    # A partial coordinate claim is still a claim: the located cell's
    # column (or row) must agree with it — the mx1505 off-by-one, where
    # the literal resolved one column off and silently passed (subset-5).
    if claimed_c is not None and c != claimed_c:
        return f"the literal sits in col {c + 1}, not the claimed col {claimed_c + 1}"
    if claimed_r is not None and r != claimed_r:
        return f"the literal sits in row {r + 1}, not the claimed row {claimed_r + 1}"
    if (
        claim_col
        and 0 <= c < n_cols
        and (
            " ".join(markup_surface(parsed.columns[c].header).split()).casefold()
            != " ".join(markup_surface(str(claim_col)).split()).casefold()
        )
    ):
        return (
            f"the anchor cell sits under {parsed.columns[c].header!r}, "
            f"not the claimed {claim_col!r}"
        )
    if claim_row and not _row_label_ok(parsed, r, c, str(claim_row)):
        return f"claimed row label {claim_row!r} is not in the anchor row"
    if not cell_ok(r, c):
        return f"literal {literal!r} is not in the anchor cell's text"
    return None


def _pow(base: float, exponent: float) -> float | None:
    """``base**exponent`` or ``None`` when the power can't land as a
    finite, nonzero float — ``10**500`` raises ``OverflowError`` and a
    deep negative underflows to ``0.0``, and either stores a corrupted
    value, so the cell drops instead (``base > 1`` never yields a
    legitimate 0)."""
    try:
        value = base**exponent
    except OverflowError:
        return None
    if not math.isfinite(value) or value == 0.0:
        return None
    return value


def _transformed(
    parsed_lit: Any, transform: str
) -> tuple[float | None, float | None, float | None, float | None, str] | str | None:
    """``(num, low, high, err, form)`` of a cell under ``log10``/``ln``,
    or a failure the caller drops: ``None`` when the value's own
    exponent overflows/underflows — a ``log10`` column with a stray
    ``400`` is garbage a drop names, never a crash nor a stored inf —
    and ``'uncertainty_overflow'`` when the value landed but a ``±``
    edge did not (``10**(307±3)``): the interval can't be written
    edgeless, so the cell drops under that named reason (Opus r2 E).
    The stored number is the value, not its exponent; an uncertainty
    folds into the interval edges (``10**(x±e)``) and the form reads
    ``interval``."""
    base = _TRANSFORMS[transform]
    num = None if parsed_lit.num is None else _pow(base, parsed_lit.num)
    low = None if parsed_lit.low is None else _pow(base, parsed_lit.low)
    high = None if parsed_lit.high is None else _pow(base, parsed_lit.high)
    err = parsed_lit.err
    form = parsed_lit.form
    if err is not None and parsed_lit.num is not None and num is not None:
        low = _pow(base, parsed_lit.num - err)
        high = _pow(base, parsed_lit.num + err)
        if low is None or high is None:
            return "uncertainty_overflow"
        err = None
        form = "interval"
    if (
        (parsed_lit.num is not None and num is None)
        or (parsed_lit.low is not None and low is None)
        or (parsed_lit.high is not None and high is None)
    ):
        return None
    return num, low, high, err, form


def expand_recipe(
    parsed: ParsedTable,
    recipe: dict[str, Any],
    *,
    chunk_id: int,
    caption: tuple[int, str] | None,
    vocab: dict[int, VocabEntry],
    paper: int,
    table: int | None = None,
    chunk_text: str | None = None,
    extraction_run: str | None = None,
    model: str | None = None,
    anchor_ref_id: int | None = None,
) -> tuple[list[RunProposal], list[dict[str, Any]]]:
    """Expand one recipe over the parsed grid.

    Returns ``(runs, drops)`` — a drop per refused element with
    ``reason``/``detail`` naming what went wrong, never silent.
    ``caption`` is the ``(caption_chunk_id, caption_text)`` pairing
    :func:`precis.extract.tables.assign_captions` produced; ``table`` is
    the logical table's 1-based ``T<n>`` number for ``meta.table``.
    ``chunk_text`` is the chunk's raw text for the anchor re-check; when
    omitted the cell's own text stands in (the ``Cell`` invariant makes
    them equal). ``paper`` is the run's subject ref; ``anchor_ref_id``
    is the ref the chunk belongs to — it differs when the grid comes
    from the paper's supplement (gr632957).
    """
    anchor_ref = paper if anchor_ref_id is None else anchor_ref_id
    if parsed.ragged_rows:
        return [], [
            _drop(
                "table",
                "ragged_table",
                f"rows {parsed.ragged_rows} have a cell count other than "
                f"{len(parsed.columns)} — column alignment is unprovable",
            )
        ]
    norm, drops = validate_recipe(parsed, recipe)
    if norm is None:
        return [], drops
    if norm.no_results is not None:
        drops.append(_drop("table", "no_results", norm.no_results))
        return [], drops
    orientation = norm.orientation or "columns"
    n_cols = len(parsed.columns)
    n_rows = len(parsed.rows)
    cap_id, cap_text = caption if caption is not None else (None, "")
    # The title the reviewer sees when no caption paired — the chunk's
    # own leading line (a table chunk opening on "Table N. …").
    cap_or_title = cap_text or _table_title(chunk_text)
    # The chunk's footnote/notes lines (and its title when no caption
    # paired) — where a unit the header doesn't print still shows up.
    notes_text = _notes_text(chunk_text)
    runs: list[RunProposal] = []
    label_col = (
        norm.subject_axis
        if orientation == "columns" and norm.subject_axis is not None
        else 0
    )

    def cell_at(r: int, c: int) -> Any:
        if 0 <= r < n_rows and 0 <= c < len(parsed.rows[r]):
            return parsed.rows[r][c]
        return None

    def series_label(spec: SeriesSpec) -> str:
        if orientation == "columns":
            return parsed.columns[spec.index].header
        row = parsed.rows[spec.index]
        return row[0].text if row else ""

    def cited_at(r: int, c: int, axis_idx: int) -> bool:
        """Decision 6 detection at one cell: the series axis's citation
        flag (column header for ``columns``, row label for ``rows``), or
        a citation marker on the subject cell of that cell's row /
        column. ``axis_idx`` is the emitted axis element — it can differ
        from the recipe's mapped index when a measurand-matched column
        joins the series (subset-3)."""
        if orientation == "columns":
            if parsed.columns[axis_idx].cited:
                return True
            subj = (
                cell_at(r, norm.subject_axis) if norm.subject_axis is not None else None
            )
            return bool(subj is not None and has_citation_marker(subj.text))
        if axis_idx < len(parsed.row_label_cited) and parsed.row_label_cited[axis_idx]:
            return True
        return bool(0 <= c < n_cols and parsed.columns[c].cited)

    def subject_at(r: int, c: int) -> str | None:
        if orientation == "columns":
            if norm.subject_axis is None:
                return None
            cell = cell_at(r, norm.subject_axis)
            return _subject_surface(cell.text) if cell is not None else None
        # rows: each subject is a column — its label is the column
        # header, or a designated label row's cell when the recipe names
        # one and that cell prints a label (an empty one defers to the
        # header — the only other subject evidence the reviewer sees).
        if norm.subject_axis is not None:
            cell = cell_at(norm.subject_axis, c)
            if cell is not None and (surf := _subject_surface(cell.text)):
                return surf
        if 0 <= c < n_cols:
            return _subject_surface(parsed.columns[c].header)
        return None

    def row_label(r: int | None) -> str | None:
        """The row's label cell: the subject column for a ``columns``
        table that names one, else the leftmost cell."""
        if r is None:
            return None
        axis = (
            norm.subject_axis
            if orientation == "columns" and norm.subject_axis is not None
            else 0
        )
        cell = cell_at(r, axis)
        return cell.text if cell is not None else None

    def table_meta(
        r: int | None, c: int | None, label: str | None, footnote: str | None
    ) -> dict[str, Any]:
        # col_header / row_label / header_unit are the reviewer's context
        # (gr506762 slice-4): ``header`` is only the series axis's label;
        # the cross axis's label (e.g. a per-column cluster size) was lost.
        return {
            "chunk": chunk_id,
            "table": table,
            "row": None if r is None else r + 1,
            "col": None if c is None else c + 1,
            "header": label,
            "col_header": (
                parsed.columns[c].header if c is not None and 0 <= c < n_cols else None
            ),
            "row_label": row_label(r),
            "header_unit": header_unit(label) if label else None,
            "row_span": (
                list(parsed.row_spans[r])
                if r is not None and r < len(parsed.row_spans)
                else None
            ),
            # subset-3: the header cells + the data row are the cited
            # span's evidence; ``caption`` is the paired caption's text
            # or the chunk's own leading title.
            "header_span": (
                list(parsed.header_span) if parsed.header_span is not None else None
            ),
            # A continuation's header lives in the run head's chunk —
            # its offsets can't slice this chunk's text, so the lines
            # ride as text (the cited span's head falls back to them).
            "header_text": (
                "\n".join(parsed.header_lines)
                if parsed.header_span is None and parsed.header_lines
                else None
            ),
            "caption": cap_or_title or None,
            "caption_chunk": cap_id,
            "footnote": footnote,
            "orientation": orientation,
        }

    def base_meta() -> dict[str, Any]:
        return {
            "extraction_run": extraction_run,
            "anchor_scheme": _anchors.ANCHOR_SCHEME,
            "extraction": {
                "chunk": chunk_id,
                "model": model,
                "prompt": RECIPE_PROMPT_VERSION,
            },
        }

    def cell_literal_ok(cell: Any, literal: str, categorical: bool) -> bool:
        """The anchor re-check: the literal is verbatim inside the cell's
        own span (an ``expand``-time ``literal_not_in_span`` is a
        ``parse_table`` offset bug, never a model miss)."""
        source = chunk_text if chunk_text is not None else cell.text
        span = (cell.start, cell.end) if chunk_text is not None else (0, len(cell.text))
        return _verify.literal_supported(
            literal,
            source[span[0] : span[1]],
            source,
            span,
            numeric_required=not categorical,
        )

    def subject_evidenced(subject: str, r: int, c: int) -> bool:
        """The stored subject appears in a span the reviewer sees: the
        cited row's own line (the cell and its row label ride in it),
        the column header, or the caption. A ``columns`` subject sits in
        its own row by construction; a ``rows`` subject sits in the
        column header — the label row a ``subject_axis`` names is a
        *different* row's line, never this cell's evidence, so a
        label-row subject that surfaces nowhere else drops
        (``subject_not_in_span``, gr506762 subset-2)."""
        parts = [cap_text, row_label(r) or ""]
        if 0 <= c < n_cols:
            parts.append(parsed.columns[c].header)
        if chunk_text is not None and r < len(parsed.row_spans):
            s, e = parsed.row_spans[r]
            parts.append(chunk_text[s:e])
        elif r < n_rows:
            parts.extend(cl.text for cl in parsed.rows[r])
        hay = _subject_surface(" ".join(parts)).casefold()
        return subject.casefold() in hay

    def axis_label(i: int) -> str:
        """The label axis element ``i`` prints — a column header
        (``columns``) or the row's label cell (``rows``)."""
        if orientation == "columns":
            return parsed.columns[i].header
        row = parsed.rows[i]
        return row[0].text if row else ""

    def cross_label(r: int, c: int) -> str:
        """The other axis's label for cell ``(r, c)``: the data row's
        label cell for ``columns``, the column header for ``rows`` —
        the label that can mark the row/column a correction or a
        reference phase (subset-3)."""
        if orientation == "columns":
            return row_label(r) or ""
        return parsed.columns[c].header if 0 <= c < n_cols else ""

    def _zero_cells_in_column(col: int) -> int:
        """Data cells in column ``col`` parsing as an exact-0 point —
        the sole-zero test of the ``reference_zero`` rule."""
        n = 0
        for row in parsed.rows:
            if col < len(row):
                lit = parse_literal(row[col].value_text)
                if lit.form == "point" and lit.num == 0:
                    n += 1
        return n

    # ── table-level conditions → one input spec each, shared by every
    # run of the table ────────────────────────────────────────────────
    shared_inputs: list[MeasureSpec] = []
    for i, cond in enumerate(norm.conditions):
        loc = f"conditions[{i}]"
        entry = resolve_measurand(cond.measurand, vocab)
        if entry is None:
            drops.append(_drop(loc, "unknown_measurand", repr(cond.measurand)))
            continue
        anchor: MeasureAnchor | None = None
        if cond.source == "caption":
            if cap_id is None:
                drops.append(
                    _drop(
                        loc,
                        "caption_missing",
                        f"condition {cond.condition!r} asks for the caption, "
                        "but assign_captions left this table unpaired",
                    )
                )
                continue
            pos = cap_text.find(cond.literal)
            if pos >= 0:
                anchor = MeasureAnchor(
                    anchor_ref,
                    cap_id,
                    "offsets",
                    [cap_id, pos, pos + len(cond.literal)],
                )
            else:
                # Still anchored on the caption chunk — insert_measure's
                # own span check then flags anchor_mismatch; the
                # condition writes, never refuses.
                anchor = MeasureAnchor(
                    anchor_ref, cap_id, "offsets", [cap_id, 0, len(cap_text)]
                )
        else:
            assert cond.cell is not None
            cr, cc = cond.cell
            cell = cell_at(cr, cc)
            if cell is None:
                drops.append(_drop(loc, "bad_shape", f"cell {cond.cell} vanished"))
                continue
            anchor = MeasureAnchor(
                anchor_ref, chunk_id, "offsets", [chunk_id, cell.start, cell.end]
            )
            if chunk_text is not None:
                # The literal+``r:c`` pair is model-claimed — the one
                # extraction shape where the claimed cell and the
                # literal's cell can diverge (the mx1505 off-by-one).
                # Same grid → deterministic → refuse, never flag.
                detail = anchor_cell_mismatch(
                    parsed,
                    span=[chunk_id, cell.start, cell.end],
                    literal=cond.literal,
                    table_meta=table_meta(cr, cc, cond.condition, None),
                    chunk_text=chunk_text,
                )
                if detail is not None:
                    drops.append(_drop(loc, "anchor_cell_mismatch", detail))
                    continue
        in_meta = base_meta()
        in_meta["condition"] = cond.condition
        in_meta["table"] = (
            table_meta(cond.cell[0], cond.cell[1], cond.condition, None)
            if cond.cell is not None
            else table_meta(None, None, cond.condition, None)
        )
        shared_inputs.append(
            MeasureSpec(
                measurand_ref_id=entry.ref_id,
                literal=cond.literal,
                subject_ref_id=paper,
                reported_unit=cond.unit,
                molar_bridge=_needs_molar_bridge(cond.unit, entry.canonical_unit),
                role=cond.role,
                direction="input",
                source_attribution="own_work",
                anchor=anchor,
                meta=in_meta,
            )
        )

    # ── input series → per-cell input specs, attached to every output
    # on the same row (columns) or column (rows) ──────────────────────
    input_cells: dict[tuple[int, int], MeasureSpec] = {}
    for si, spec in enumerate(norm.series):
        if spec.role != "input":
            continue
        loc = f"series[{si}]"
        entry = resolve_measurand(spec.measurand, vocab)
        if entry is None:
            drops.append(_drop(loc, "unknown_measurand", repr(spec.measurand)))
            continue
        label = series_label(spec)
        quantity = _label_quantity(label)
        if quantity is not None and not _label_names_measurand(label, entry):
            drops.append(
                _drop(
                    loc,
                    "label_measurand_mismatch",
                    f"label {label!r} names {quantity!r}, not {entry.name!r}",
                )
            )
            continue
        # A rows-orientation series spans ``range(1, n_cols)`` — column
        # 0 is the row-label axis ``series_label`` reads, never a
        # reading, whether or not the model listed it in skip_cells.
        coords = (
            ((r, spec.index) for r in range(n_rows))
            if orientation == "columns"
            else ((spec.index, c) for c in range(1, n_cols))
        )
        for r, c in coords:
            cell = cell_at(r, c)
            if cell is None or (r, c) in norm.skip_cells:
                continue
            if cell.value_text.strip().casefold() in _EMPTY_CELL:
                continue
            value_cols = (
                {spec.index} if orientation == "columns" else set(range(1, n_cols))
            )
            if _sub_header_row(
                parsed, r, label_col, value_cols, _entry_categorical(entry)
            ):
                drops.append(
                    _drop(
                        f"{loc} cell {r}:{c}",
                        "sub_header_row",
                        f"row {r + 1} is a sub-header (header echoes, "
                        "label-only text, or a lone text cell amid "
                        "empties), not a reading row",
                    )
                )
                continue
            unit = spec.unit
            if spec.unit_index is not None:
                uc = (
                    cell_at(r, spec.unit_index)
                    if orientation == "columns"
                    else cell_at(spec.unit_index, c)
                )
                unit = uc.value_text.strip() if uc is not None else None
            if unit is not None:
                mismatch = _unit_mismatch(unit, entry)
                if mismatch is not None:
                    drops.append(
                        _drop(
                            f"{loc} cell {r}:{c}", "unit_dimension_mismatch", mismatch
                        )
                    )
                    continue
            parsed_lit = parse_literal(cell.value_text)
            in_meta = base_meta()
            if spec.condition:
                in_meta["condition"] = spec.condition
            in_meta["table"] = table_meta(r, c, label, cell.footnote)
            input_cells[(r, c)] = MeasureSpec(
                measurand_ref_id=entry.ref_id,
                literal=cell.text,
                subject_ref_id=paper,
                reported_unit=unit,
                molar_bridge=_needs_molar_bridge(unit, entry.canonical_unit),
                value_num=parsed_lit.num,
                value_low=parsed_lit.low,
                value_high=parsed_lit.high,
                value_err=parsed_lit.err,
                value_text=parsed_lit.text,
                value_bool=parsed_lit.flag,
                value_form=parsed_lit.form,
                direction="input",
                source_attribution=(
                    "cited_work" if cited_at(r, c, spec.index) else "own_work"
                ),
                anchor=MeasureAnchor(
                    anchor_ref, chunk_id, "offsets", [chunk_id, cell.start, cell.end]
                ),
                meta=in_meta,
            )

    def inputs_for(r: int, c: int) -> list[MeasureSpec]:
        """The output cell's run inputs: its row's (columns) or column's
        (rows) input cells, then the table-level conditions."""
        own = [
            spec
            for (ir, ic), spec in input_cells.items()
            if (ir == r if orientation == "columns" else ic == c)
        ]
        return own + shared_inputs

    # ── output series → one run per non-empty cell ───────────────────
    # Pass 1: per-series checks and grouping by measurand. Pass 2 emits
    # each group's axis union — the recipe's indices plus every other
    # axis element whose label names the measurand (subset-3: a wide
    # same-measurand table emits one run per matching column, each
    # recording the column it took, never a silent pick).
    out_groups: dict[int, list[tuple[int, SeriesSpec, SeriesSpec, VocabEntry]]] = {}
    group_order: list[int] = []
    for si, spec in enumerate(norm.series):
        if spec.role != "output":
            continue
        loc = f"series[{si}]"
        entry = resolve_measurand(spec.measurand, vocab)
        if entry is None:
            drops.append(_drop(loc, "unknown_measurand", repr(spec.measurand)))
            continue
        eff_spec = spec
        if eff_spec.unit is None and entry.canonical_unit == "1":
            # `unit: null` is legal only on a canonical-'1' measurand —
            # store '1' so insert_measure's no-unit refusal never fires.
            eff_spec = replace(spec, unit="1")
        if eff_spec.unit is not None:
            mismatch = _unit_mismatch(eff_spec.unit, entry)
            if mismatch is not None:
                drops.append(_drop(loc, "unit_dimension_mismatch", mismatch))
                continue
        label = series_label(spec)
        label_tf = _label_log_transform(label)
        if label_tf == "unclear" or (
            label_tf is not None
            and spec.transform is not None
            and spec.transform != label_tf
        ):
            drops.append(
                _drop(
                    loc,
                    "log_base_unclear",
                    f"label {label!r} vs transform {spec.transform!r}",
                )
            )
            continue
        if label_tf is not None:
            eff_spec = replace(eff_spec, transform=label_tf)
        quantity = _label_quantity(label)
        if quantity is not None and not _label_names_measurand(label, entry):
            drops.append(
                _drop(
                    loc,
                    "label_measurand_mismatch",
                    f"label {label!r} names {quantity!r}, not {entry.name!r}",
                )
            )
            continue
        if (corr := _correction_label(label)) is not None:
            drops.append(
                _drop(
                    loc,
                    "correction_row",
                    f"label {label!r} prints {corr!r} — a correction/delta "
                    "series, not a reading",
                )
            )
            continue
        axis_coords = [
            (r, c)
            for r, c in (
                ((r, spec.index) for r in range(n_rows))
                if orientation == "columns"
                else ((spec.index, c) for c in range(1, n_cols))
            )
        ]
        nums: list[int] = []
        axis_span = n_rows if orientation == "columns" else n_cols - 1
        for r, c in axis_coords:
            cell = cell_at(r, c)
            if cell is None or (r, c) in norm.skip_cells:
                continue
            if cell.value_text.strip().casefold() in _EMPTY_CELL:
                continue
            v = _int_point(cell.value_text)
            if v is None:
                break
            nums.append(v)
        else:
            # Every emitted cell is an integer point. A consecutive run
            # 0../1.. filling the whole cross axis (a column per index)
            # or sitting in the axis's first position is a printed
            # enumeration — a label axis, never a measurement — unless
            # the label names the measurand (then it IS the data).
            if (
                _enumeration_run(nums)
                and (spec.index == 0 or len(nums) == axis_span)
                and not _label_names_measurand(label, entry)
            ):
                drops.append(
                    _drop(
                        loc,
                        "index_enumeration",
                        f"cells are an integer run {nums[0]}..{nums[-1]} — "
                        f"an index axis, not {entry.name!r} readings",
                    )
                )
                continue
        if entry.ref_id not in out_groups:
            group_order.append(entry.ref_id)
        out_groups.setdefault(entry.ref_id, []).append((si, spec, eff_spec, entry))

    def auto_axis(
        idx: int, group: list[tuple[int, SeriesSpec, SeriesSpec, VocabEntry]]
    ) -> tuple[int, SeriesSpec, SeriesSpec, VocabEntry] | None:
        """The emitting spec for an auto-matched axis element — one the
        recipe never mapped but whose label names the group's measurand.

        It passes the same checks a mapped series passes in pass 1, on
        ITS OWN label and cells: the group inherits the measurand, not
        the mapped column's clean bill (subset-3 r2 — a
        ``ΔE_ads correction`` or ``E_ads (kJ/mol)`` column auto-matched
        next to ``E_ads (eV)`` emitted unchecked). The transform is
        re-derived from its own label (a sibling's ``log10`` claim never
        exponentiates plain numbers) and the unit from its own header
        (a printed kJ/mol never stores under an inherited eV claim —
        when it can't convert, the element drops
        ``unit_dimension_mismatch``)."""
        si, spec, _eff, entry = group[0]
        loc = f"series[{si}] axis {idx}"
        label = axis_label(idx)
        label_tf = _label_log_transform(label)
        if label_tf == "unclear" or (
            label_tf is not None
            and spec.transform is not None
            and spec.transform != label_tf
        ):
            drops.append(
                _drop(
                    loc,
                    "log_base_unclear",
                    f"label {label!r} vs transform {spec.transform!r}",
                )
            )
            return None
        eff_spec = replace(
            spec,
            transform=label_tf,
            unit=(
                spec.unit
                if spec.unit is not None or entry.canonical_unit != "1"
                else "1"
            ),
        )
        quantity = _label_quantity(label)
        if quantity is not None and not _label_names_measurand(label, entry):
            drops.append(
                _drop(
                    loc,
                    "label_measurand_mismatch",
                    f"label {label!r} names {quantity!r}, not {entry.name!r}",
                )
            )
            return None
        if (corr := _correction_label(label)) is not None:
            drops.append(
                _drop(
                    loc,
                    "correction_row",
                    f"label {label!r} prints {corr!r} — a correction/delta "
                    "series, not a reading",
                )
            )
            return None
        printed = (
            parsed.columns[idx].header_unit
            if orientation == "columns"
            else header_unit(label)
        )
        if printed is not None and printed != eff_spec.unit:
            mismatch = _unit_mismatch(printed, entry)
            if mismatch is not None:
                drops.append(_drop(loc, "unit_dimension_mismatch", mismatch))
                return None
            eff_spec = replace(eff_spec, unit=printed)
        nums: list[int] = []
        axis_span = n_rows if orientation == "columns" else n_cols - 1
        for r, c in (
            ((r, idx) for r in range(n_rows))
            if orientation == "columns"
            else ((idx, c) for c in range(1, n_cols))
        ):
            cell = cell_at(r, c)
            if cell is None or (r, c) in norm.skip_cells:
                continue
            if cell.value_text.strip().casefold() in _EMPTY_CELL:
                continue
            v = _int_point(cell.value_text)
            if v is None:
                break
            nums.append(v)
        else:
            if (
                _enumeration_run(nums)
                and (idx == 0 or len(nums) == axis_span)
                and not _label_names_measurand(label, entry)
            ):
                drops.append(
                    _drop(
                        loc,
                        "index_enumeration",
                        f"cells are an integer run {nums[0]}..{nums[-1]} — "
                        f"an index axis, not {entry.name!r} readings",
                    )
                )
                return None
        return si, spec, eff_spec, entry

    for ref_id in group_order:
        group = out_groups[ref_id]
        explicit: dict[int, tuple[int, SeriesSpec, SeriesSpec, VocabEntry]] = {}
        for item in group:
            explicit.setdefault(item[1].index, item)
        # An index a series outside this group names — subject axis or an
        # explicit mapping to anything — stays out of the auto-match: the
        # recipe's own claim on it wins.
        foreign = {s.index for s in norm.series} - set(explicit)
        if norm.subject_axis is not None:
            foreign.add(norm.subject_axis)
        entry0 = group[0][3]
        axes = set(explicit)
        for i in range(n_cols if orientation == "columns" else n_rows):
            if i in axes or i in foreign:
                continue
            if _label_names_measurand(axis_label(i), entry0):
                axes.add(i)
        # Several axis elements naming the measurand: every emitted run
        # records which one it took as a condition on the measure.
        multi = len(axes) > 1
        for idx in sorted(axes):
            axis_item = explicit.get(idx) or auto_axis(idx, group)
            if axis_item is None:
                continue
            si, spec, eff_spec, entry = axis_item
            loc = f"series[{si}]"
            label = axis_label(idx)
            cond_name = spec.condition or (
                "column" if orientation == "columns" else "row"
            )
            coords = (
                ((r, idx) for r in range(n_rows))
                if orientation == "columns"
                else ((idx, c) for c in range(1, n_cols))
            )
            for r, c in coords:
                cell = cell_at(r, c)
                if cell is None or (r, c) in norm.skip_cells:
                    continue
                if cell.value_text.strip().casefold() in _EMPTY_CELL:
                    continue
                loc_cell = f"{loc} cell {r}:{c}"
                value_cols = (
                    {idx} if orientation == "columns" else set(range(1, n_cols))
                )
                if _sub_header_row(
                    parsed, r, label_col, value_cols, _entry_categorical(entry)
                ):
                    drops.append(
                        _drop(
                            loc_cell,
                            "sub_header_row",
                            f"row {r + 1} is a sub-header (header echoes, "
                            "label-only text, or a lone text cell amid "
                            "empties), not a reading row",
                        )
                    )
                    continue
                if not (0 <= c < n_cols and _subject_surface(parsed.columns[c].header)):
                    drops.append(
                        _drop(
                            loc_cell,
                            "no_header_in_span",
                            f"column {c + 1} prints no header — the cited "
                            "span cannot show which column the value is",
                        )
                    )
                    continue
                if (corr := _correction_label(cross_label(r, c))) is not None:
                    drops.append(
                        _drop(
                            loc_cell,
                            "correction_row",
                            f"label {cross_label(r, c)!r} prints {corr!r} — "
                            "a correction/delta row, not a reading",
                        )
                    )
                    continue
                over = norm.overrides.get((r, c)) or {}
                out_entry = entry
                if isinstance(over.get("measurand"), str):
                    resolved = resolve_measurand(over["measurand"], vocab)
                    if resolved is None:
                        drops.append(
                            _drop(
                                loc_cell,
                                "unknown_measurand",
                                repr(over["measurand"]),
                            )
                        )
                        continue
                    out_entry = resolved
                literal = cell.text
                parsed_lit = parse_literal(cell.value_text)
                num, low, high = parsed_lit.num, parsed_lit.low, parsed_lit.high
                err, text_v, flag_v = (
                    parsed_lit.err,
                    parsed_lit.text,
                    parsed_lit.flag,
                )
                form = parsed_lit.form
                printed_value: float | None = None
                if eff_spec.transform:
                    if form in ("categorical", "boolean") or (
                        num is None and low is None and high is None
                    ):
                        drops.append(
                            _drop(
                                loc_cell,
                                "bad_shape",
                                f"{eff_spec.transform} on non-numeric {literal!r}",
                            )
                        )
                        continue
                    printed_value = num
                    tf = _transformed(parsed_lit, eff_spec.transform)
                    if tf is None or isinstance(tf, str):
                        drops.append(
                            _drop(
                                loc_cell,
                                tf if isinstance(tf, str) else "bad_shape",
                                f"{eff_spec.transform} "
                                + (
                                    "uncertainty edges overflow"
                                    if isinstance(tf, str)
                                    else "overflow"
                                )
                                + f" on {literal!r}",
                            )
                        )
                        continue
                    num, low, high, err, form = tf
                unit = eff_spec.unit
                claimed_unit = unit
                unit_cell: str | None = None
                over_unit = over.get("unit")
                if isinstance(over_unit, str) and over_unit.strip():
                    unit, claimed_unit = over_unit.strip(), over_unit.strip()
                elif spec.unit_index is not None:
                    uc = (
                        cell_at(r, spec.unit_index)
                        if orientation == "columns"
                        else cell_at(spec.unit_index, c)
                    )
                    unit = uc.value_text.strip() if uc is not None else None
                    unit_cell = uc.value_text if uc is not None else None
                    claimed_unit = None  # printed in its own cell — self-supported
                if unit is not None:
                    mismatch = _unit_mismatch(unit, out_entry)
                    if mismatch is not None:
                        drops.append(
                            _drop(loc_cell, "unit_dimension_mismatch", mismatch)
                        )
                        continue
                categorical = form == "categorical" and text_v is not None
                if not cell_literal_ok(cell, literal, categorical):
                    drops.append(
                        _drop(
                            loc_cell,
                            "literal_not_in_span",
                            f"{literal!r} vs cell span {cell.text[:120]!r} — a "
                            "parse_table offset bug",
                        )
                    )
                    continue
                subject = subject_at(r, c)
                if subject and not subject_evidenced(subject, r, c):
                    drops.append(
                        _drop(
                            loc_cell,
                            "subject_not_in_span",
                            f"subject {subject!r} is in neither the cited row, "
                            "the column header nor the caption",
                        )
                    )
                    continue
                if (
                    form == "point"
                    and num == 0.0
                    and _energy_unit(out_entry.canonical_unit)
                ):
                    # An exact 0 on an energy measurand is the scale's
                    # reference point when a label says so — never a
                    # measured adsorption energy (subset-3, mx2188).
                    marked = _REFERENCE_MARK_RE.search(
                        f"{row_label(r) or ''} {cap_text}"
                    )
                    if marked is not None or (
                        _REFERENCE_PHASE_RE.search(cross_label(r, c)) is not None
                        and _zero_cells_in_column(c) == 1
                    ):
                        drops.append(
                            _drop(
                                loc_cell,
                                "reference_zero",
                                "an exact 0 on an energy measurand marked as "
                                "the reference/zero of the scale"
                                + (
                                    f" ({marked.group(0)!r})"
                                    if marked is not None
                                    else ""
                                ),
                            )
                        )
                        continue
                flags: list[str] = []
                if claimed_unit is not None and claimed_unit != "1":
                    # '1' is the dimensionless claim (reaching here only on a
                    # canonical-'1' measurand — anything else dropped at
                    # unit_dimension_mismatch): there is no printed unit token
                    # to support, so the flag is always spurious (gr506762
                    # slice-4: correct sticking coefficients flagged).
                    support = " ".join(
                        t for t in (label, unit_cell or "", cap_text, notes_text) if t
                    )
                    if not _verify.unit_has_text_support(claimed_unit, support):
                        # subset-5: refuse only on a printed
                        # contradiction — a header/caption/notes unit
                        # the claim can't convert to. Nothing printed
                        # anywhere flags ``unit_unconfirmed`` for the
                        # reviewer instead of refusing (the unit often
                        # lives only in the body text).
                        printed = _conflicting_unit(
                            claimed_unit, label, cap_text, notes_text
                        )
                        if printed is not None:
                            drops.append(
                                _drop(
                                    loc_cell,
                                    "unit_header_mismatch",
                                    f"the table prints unit {printed!r} — "
                                    f"the claimed {claimed_unit!r} conflicts",
                                )
                            )
                            continue
                        # subset-5b/5c/5d: a printed unit that differs
                        # from the claim but converts to the measurand's
                        # unit is the row's unit — take it and let
                        # insert_measure convert at write (the subset-3
                        # r2 auto-match rule on claims). The column's
                        # own header is the strongest evidence and wins
                        # first (subset-5d: ``Overpotential (V)`` under
                        # an mV claim stores volts, not 1000x off); a
                        # caption/notes unit is taken next, only when it
                        # could describe THIS column — a caption may
                        # name another column's quantity, so a
                        # dimension-incompatible (or molar-mass-needing)
                        # unit leaves the claim flagged, as before 5b.
                        # A header unit needing a molar mass refuses
                        # ``unit_needs_molar_mass``: it certainly names
                        # this column and the recipe cannot supply the
                        # bridge.
                        taken = False
                        header_u = header_unit(label) if label else None
                        if header_u is not None:
                            try:
                                make_converter(
                                    header_u,
                                    out_entry.canonical_unit or header_u,
                                    molar_bridge=True,
                                )
                            except NeedsMolarMass:
                                drops.append(
                                    _drop(
                                        loc_cell,
                                        "unit_needs_molar_mass",
                                        f"the header prints unit "
                                        f"{header_u!r} — converting to "
                                        "the measurand's unit needs a "
                                        "molar mass the recipe does not "
                                        "have",
                                    )
                                )
                                continue
                            except BadInput:
                                pass
                            else:
                                unit = header_u
                                taken = True
                        if not taken:
                            for piece in (cap_text, notes_text):
                                u = printed_unit(piece) if piece else None
                                if u is None:
                                    continue
                                try:
                                    make_converter(
                                        u,
                                        out_entry.canonical_unit or u,
                                        molar_bridge=True,
                                    )
                                except (BadInput, NeedsMolarMass):
                                    continue
                                unit = u
                                taken = True
                                break
                        if not taken:
                            flags.append(
                                "unit_without_text_support"
                                if header_u is not None
                                or any(
                                    printed_unit(p) for p in (cap_text, notes_text) if p
                                )
                                else "unit_unconfirmed"
                            )
                cited = cited_at(r, c, idx)
                attribution = (
                    "cited_work" if cited else (spec.attribution or norm.attribution)
                )
                tier = (
                    "asserted" if cited else _tier(attribution, spec.tier or norm.tier)
                )
                if eff_spec.transform:
                    status = "interpreted"
                else:
                    status = (
                        "explicit"
                        if _label_names_measurand(label, out_entry)
                        else "interpreted"
                    )
                if over.get("measurand_status") in _MEASURAND_STATUSES:
                    status = over["measurand_status"]
                meta = base_meta()
                meta["table"] = table_meta(r, c, label, cell.footnote)
                if chunk_text is not None:
                    # Deterministic on the same grid — a claim/anchor
                    # divergence at extraction is a bug, never a flag
                    # (subset-5b: the mx1505 off-by-one shape).
                    detail = anchor_cell_mismatch(
                        parsed,
                        span=[chunk_id, cell.start, cell.end],
                        literal=literal,
                        table_meta=meta["table"],
                        chunk_text=chunk_text,
                    )
                    if detail is not None:
                        drops.append(_drop(loc_cell, "anchor_cell_mismatch", detail))
                        continue
                if eff_spec.transform:
                    meta["transform"] = eff_spec.transform
                    if printed_value is not None:
                        meta["printed_value"] = printed_value
                # gr632956: an electrode reference printed in the column
                # label, caption or table notes ('(V vs RHE)', 'potentials
                # are vs NHE') fills the reference a potential series never
                # carries per cell — only for a potential measurand, and an
                # override always wins.
                reference = (
                    over["reference"]
                    if isinstance(over.get("reference"), str)
                    else None
                )
                if reference is None and is_potential_unit(out_entry.canonical_unit):
                    reference = printed_reference(
                        " ".join(p for p in (label, cap_text, notes_text) if p)
                    )
                output = MeasureSpec(
                    measurand_ref_id=out_entry.ref_id,
                    literal=literal,
                    subject_ref_id=paper,
                    reported_unit=unit,
                    molar_bridge=_needs_molar_bridge(unit, out_entry.canonical_unit),
                    subject=subject,
                    subject_group=norm.group,
                    value_num=num,
                    value_low=low,
                    value_high=high,
                    value_err=err,
                    value_text=text_v,
                    value_bool=flag_v,
                    value_form=form,
                    reference=reference,
                    tier=tier,
                    source_attribution=attribution,
                    measurand_status=status,
                    normalization=(
                        over["normalization"]
                        if isinstance(over.get("normalization"), str)
                        else None
                    ),
                    normalization_status=(
                        "explicit"
                        if isinstance(over.get("normalization"), str)
                        else None
                    ),
                    conditions=(
                        {cond_name: " ".join(label.split()) or label} if multi else None
                    ),
                    anchor=MeasureAnchor(
                        anchor_ref,
                        chunk_id,
                        "offsets",
                        [chunk_id, cell.start, cell.end],
                    ),
                    meta=meta,
                )
                runs.append(
                    RunProposal(
                        output=output,
                        inputs=inputs_for(r, c),
                        location=f"table cell {r}:{c}",
                        flags=flags,
                    )
                )
    return runs, drops


def stored_row_reasons(
    parsed: ParsedTable,
    *,
    cell: tuple[int, int] | None,
    literal: str,
    unit: str | None,
    subject: str | None,
    value_form: str | None,
    value_num: float | None,
    entry: VocabEntry,
    table_meta: dict[str, Any],
    transform: str | None,
    caption_text: str,
    chunk_text: str,
    span: Any,
) -> list[tuple[str, str]]:
    """The extraction guards re-run on a stored table_recipe row —
    the recheck pass's per-row verdicts (gr506762 subset-4).

    ``cell`` is the anchor's located ``(row, col)`` in the re-parsed
    grid (:func:`precis.extract.recheck`); ``table_meta`` is the row's
    stored ``meta.table`` — its claims fill in what the grid can't
    re-derive (the series label, claimed coords). Returns
    ``(reason, detail)`` pairs; an empty list means the row survives
    every current guard."""
    reasons: list[tuple[str, str]] = []
    meta = table_meta or {}
    orientation = meta.get("orientation") or "columns"
    label = str(meta.get("header") or "")
    n_cols = len(parsed.columns)
    n_rows = len(parsed.rows)
    r, c = cell if cell is not None else (None, None)

    def cell_at(rr: int, cc: int) -> Any:
        if 0 <= rr < n_rows and 0 <= cc < len(parsed.rows[rr]):
            return parsed.rows[rr][cc]
        return None

    # ── series-label guards (no located cell needed) ────────────────
    quantity = _label_quantity(label)
    if quantity is not None and not _label_names_measurand(label, entry):
        reasons.append(
            (
                "label_measurand_mismatch",
                f"label {label!r} names {quantity!r}, not {entry.name!r}",
            )
        )
    if (corr := _correction_label(label)) is not None:
        reasons.append(
            (
                "correction_row",
                f"label {label!r} prints {corr!r} — a correction/delta "
                "series, not a reading",
            )
        )
    label_tf = _label_log_transform(label)
    if label_tf == "unclear" or (
        label_tf is not None and transform is not None and transform != label_tf
    ):
        reasons.append(
            ("log_base_unclear", f"label {label!r} vs transform {transform!r}")
        )
    if unit is not None:
        mismatch = _unit_mismatch(unit, entry)
        if mismatch is not None:
            reasons.append(("unit_dimension_mismatch", mismatch))

    # ── cell-level guards ───────────────────────────────────────────
    # A stored value whose anchor sits in the header region — an index
    # row a stacked-header parse pulled into the header (subset-5b,
    # mx2049/mx2051-2061) — is a header cell, not parser drift: its own
    # deterministic reason, never the recheck's review-only bucket.
    off = _span_offsets(span)
    if off is not None and _in_header(parsed, *off):
        reasons.append(
            (
                "value_in_header_row",
                "the anchor sits in the header rows — the claimed value "
                "is a header/index cell, not a data cell",
            )
        )
    else:
        # The anchor check runs whether or not a cell was located.
        detail = anchor_cell_mismatch(
            parsed, span=span, literal=literal, table_meta=meta, chunk_text=chunk_text
        )
        if detail is not None:
            reasons.append(("anchor_cell_mismatch", detail))
    if r is None or c is None:
        return reasons
    row_label = _cross_label_at(parsed, r, c, "columns")
    cross = _cross_label_at(parsed, r, c, orientation)
    # The label column expand_recipe used (subject_axis) isn't stored —
    # recover it from the stored ``row_label`` claim when it folds onto
    # exactly one cell of the row, else fall back to column 0, the same
    # axis ``row_label()`` reads when no subject_axis was claimed.
    claim_row_label = meta.get("row_label")
    label_col = 0
    if claim_row_label:
        hits = [
            j
            for j, cj in enumerate(parsed.rows[r])
            if j != c
            and " ".join(markup_surface(cj.text).split()).casefold()
            == " ".join(markup_surface(str(claim_row_label)).split()).casefold()
        ]
        if len(hits) == 1:
            label_col = hits[0]
    if _sub_header_row(parsed, r, label_col, {c}, _entry_categorical(entry)):
        reasons.append(
            (
                "sub_header_row",
                f"row {r + 1} is a sub-header (mostly text or empty "
                "cells, or header echoes), not a reading row",
            )
        )
    if not (0 <= c < n_cols and _subject_surface(parsed.columns[c].header)):
        reasons.append(
            (
                "no_header_in_span",
                f"column {c + 1} prints no header — the cited span "
                "cannot show which column the value is",
            )
        )
    if (corr := _correction_label(cross)) is not None:
        reasons.append(
            (
                "correction_row",
                f"label {cross!r} prints {corr!r} — a correction/delta "
                "row, not a reading",
            )
        )
    # The emitting axis's enumeration check — the located cell's own
    # column (``columns``) or row (``rows``), same shape as expand's.
    nums: list[int] = []
    axis_span = n_rows if orientation == "columns" else n_cols - 1
    axis_idx = c if orientation == "columns" else r
    for ac in (
        (cell_at(rr, c) for rr in range(n_rows))
        if orientation == "columns"
        else (cell_at(r, cc) for cc in range(1, n_cols))
    ):
        if ac is None or ac.value_text.strip().casefold() in _EMPTY_CELL:
            continue
        v = _int_point(ac.value_text)
        if v is None:
            break
        nums.append(v)
    else:
        if (
            _enumeration_run(nums)
            and (axis_idx == 0 or len(nums) == axis_span)
            and not _label_names_measurand(label, entry)
        ):
            reasons.append(
                (
                    "index_enumeration",
                    f"cells are an integer run {nums[0]}..{nums[-1]} — "
                    f"an index axis, not {entry.name!r} readings",
                )
            )
    if subject:
        parts = [caption_text, row_label or ""]
        if 0 <= c < n_cols:
            parts.append(parsed.columns[c].header)
        if r < len(parsed.row_spans):
            s, e = parsed.row_spans[r]
            parts.append(chunk_text[s:e])
        hay = _subject_surface(" ".join(parts)).casefold()
        if subject.casefold() not in hay:
            reasons.append(
                (
                    "subject_not_in_span",
                    f"subject {subject!r} is in neither the cited row, "
                    "the column header nor the caption",
                )
            )
    if value_form == "point" and value_num == 0 and _energy_unit(entry.canonical_unit):
        marked = _REFERENCE_MARK_RE.search(f"{row_label or ''} {caption_text}")
        sole_zero = (
            _REFERENCE_PHASE_RE.search(cross) is not None
            and sum(
                1
                for row in parsed.rows
                if c < len(row)
                and (lit := parse_literal(row[c].value_text)).form == "point"
                and lit.num == 0
            )
            == 1
        )
        if marked is not None or sole_zero:
            reasons.append(
                (
                    "reference_zero",
                    "an exact 0 on an energy measurand marked as the "
                    "reference/zero of the scale",
                )
            )
    if unit is not None and unit != "1":
        # subset-5: refuse only on a printed contradiction — the
        # header, caption or table notes naming a unit the claim can't
        # convert to. Nothing printed anywhere keeps the row's
        # ``unit_unconfirmed`` flag for the reviewer, never a refusal.
        notes_text = _notes_text(chunk_text)
        support = " ".join(t for t in (label, caption_text, notes_text) if t)
        if not _verify.unit_has_text_support(unit, support):
            printed = _conflicting_unit(unit, label, caption_text, notes_text)
            if printed is not None:
                reasons.append(
                    (
                        "unit_header_mismatch",
                        f"the table prints unit {printed!r} — the stored "
                        f"unit {unit!r} conflicts",
                    )
                )
    return reasons
