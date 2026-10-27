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
from precis.extract.tables import ParsedTable, has_citation_marker
from precis.store._measures_ops import MeasureAnchor, MeasureSpec
from precis.taxonomy.measure_units import NeedsMolarMass, make_converter, parse_literal
from precis.taxonomy.money import split_currency
from precis.utils.text import markup_surface

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


def _label_names_measurand(label: str, entry: VocabEntry) -> bool:
    """The measurand's name or an alias appears in the series label —
    case-insensitive on the folded surface (``explicit`` evidence)."""
    hay = " ".join(markup_surface(label).split()).casefold()
    for key in (entry.name, *entry.aliases):
        if key and " ".join(markup_surface(key).split()).casefold() in hay:
            return True
    return False


def _unit_mismatch(unit: str, entry: VocabEntry) -> str | None:
    """``None`` when ``unit`` converts to the measurand's canonical unit
    (or is a currency token — the S1 currency path inside
    ``insert_measure`` owns those); else the
    ``unit_dimension_mismatch`` detail naming both units. A
    ``NeedsMolarMass`` bridge is not a mismatch — the store's own
    ``no_molar_mass`` escalation handles it at write."""
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
        make_converter(unit, canonical)
    except NeedsMolarMass:
        return None
    except BadInput as exc:
        return str(exc)
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
) -> tuple[list[RunProposal], list[dict[str, Any]]]:
    """Expand one recipe over the parsed grid.

    Returns ``(runs, drops)`` — a drop per refused element with
    ``reason``/``detail`` naming what went wrong, never silent.
    ``caption`` is the ``(caption_chunk_id, caption_text)`` pairing
    :func:`precis.extract.tables.assign_captions` produced; ``table`` is
    the logical table's 1-based ``T<n>`` number for ``meta.table``.
    ``chunk_text`` is the chunk's raw text for the anchor re-check; when
    omitted the cell's own text stands in (the ``Cell`` invariant makes
    them equal).
    """
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
    runs: list[RunProposal] = []

    def cell_at(r: int, c: int) -> Any:
        if 0 <= r < n_rows and 0 <= c < len(parsed.rows[r]):
            return parsed.rows[r][c]
        return None

    def series_label(spec: SeriesSpec) -> str:
        if orientation == "columns":
            return parsed.columns[spec.index].header
        row = parsed.rows[spec.index]
        return row[0].text if row else ""

    def cited_at(r: int, c: int, series: SeriesSpec) -> bool:
        """Decision 6 detection at one cell: the series axis's citation
        flag (column header for ``columns``, row label for ``rows``), or
        a citation marker on the subject cell of that cell's row /
        column."""
        if orientation == "columns":
            if parsed.columns[series.index].cited:
                return True
            subj = (
                cell_at(r, norm.subject_axis) if norm.subject_axis is not None else None
            )
            return bool(subj is not None and has_citation_marker(subj.text))
        if (
            series.index < len(parsed.row_label_cited)
            and parsed.row_label_cited[series.index]
        ):
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
        # one.
        if norm.subject_axis is not None:
            cell = cell_at(norm.subject_axis, c)
            if cell is not None:
                return _subject_surface(cell.text)
        if 0 <= c < n_cols:
            return _subject_surface(parsed.columns[c].header)
        return None

    def table_meta(
        r: int | None, c: int | None, label: str | None, footnote: str | None
    ) -> dict[str, Any]:
        return {
            "chunk": chunk_id,
            "table": table,
            "row": None if r is None else r + 1,
            "col": None if c is None else c + 1,
            "header": label,
            "row_span": (
                list(parsed.row_spans[r])
                if r is not None and r < len(parsed.row_spans)
                else None
            ),
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
                    paper, cap_id, "offsets", [cap_id, pos, pos + len(cond.literal)]
                )
            else:
                # Still anchored on the caption chunk — insert_measure's
                # own span check then flags anchor_mismatch; the
                # condition writes, never refuses.
                anchor = MeasureAnchor(
                    paper, cap_id, "offsets", [cap_id, 0, len(cap_text)]
                )
        else:
            assert cond.cell is not None
            cr, cc = cond.cell
            cell = cell_at(cr, cc)
            if cell is None:
                drops.append(_drop(loc, "bad_shape", f"cell {cond.cell} vanished"))
                continue
            anchor = MeasureAnchor(
                paper, chunk_id, "offsets", [chunk_id, cell.start, cell.end]
            )
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
                value_num=parsed_lit.num,
                value_low=parsed_lit.low,
                value_high=parsed_lit.high,
                value_err=parsed_lit.err,
                value_text=parsed_lit.text,
                value_bool=parsed_lit.flag,
                value_form=parsed_lit.form,
                direction="input",
                source_attribution=(
                    "cited_work" if cited_at(r, c, spec) else "own_work"
                ),
                anchor=MeasureAnchor(
                    paper, chunk_id, "offsets", [chunk_id, cell.start, cell.end]
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
            loc_cell = f"{loc} cell {r}:{c}"
            over = norm.overrides.get((r, c)) or {}
            out_entry = entry
            if isinstance(over.get("measurand"), str):
                resolved = resolve_measurand(over["measurand"], vocab)
                if resolved is None:
                    drops.append(
                        _drop(loc_cell, "unknown_measurand", repr(over["measurand"]))
                    )
                    continue
                out_entry = resolved
            literal = cell.text
            parsed_lit = parse_literal(cell.value_text)
            num, low, high = parsed_lit.num, parsed_lit.low, parsed_lit.high
            err, text_v, flag_v = parsed_lit.err, parsed_lit.text, parsed_lit.flag
            form = parsed_lit.form
            printed_value: float | None = None
            if spec.transform:
                if form in ("categorical", "boolean") or (
                    num is None and low is None and high is None
                ):
                    drops.append(
                        _drop(
                            loc_cell,
                            "bad_shape",
                            f"{spec.transform} on non-numeric {literal!r}",
                        )
                    )
                    continue
                printed_value = num
                tf = _transformed(parsed_lit, spec.transform)
                if tf is None or isinstance(tf, str):
                    drops.append(
                        _drop(
                            loc_cell,
                            tf if isinstance(tf, str) else "bad_shape",
                            f"{spec.transform} "
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
                    drops.append(_drop(loc_cell, "unit_dimension_mismatch", mismatch))
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
            flags: list[str] = []
            if claimed_unit is not None and claimed_unit != "1":
                # '1' is the dimensionless claim (reaching here only on a
                # canonical-'1' measurand — anything else dropped at
                # unit_dimension_mismatch): there is no printed unit token
                # to support, so the flag is always spurious (gr506762
                # slice-4: correct sticking coefficients flagged).
                support = " ".join(t for t in (label, unit_cell or "", cap_text) if t)
                if not _verify.unit_has_text_support(claimed_unit, support):
                    flags.append("unit_without_text_support")
            cited = cited_at(r, c, spec)
            attribution = (
                "cited_work" if cited else (spec.attribution or norm.attribution)
            )
            tier = "asserted" if cited else _tier(attribution, spec.tier or norm.tier)
            if spec.transform:
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
            if spec.transform:
                meta["transform"] = spec.transform
                if printed_value is not None:
                    meta["printed_value"] = printed_value
            output = MeasureSpec(
                measurand_ref_id=out_entry.ref_id,
                literal=literal,
                subject_ref_id=paper,
                reported_unit=unit,
                subject=subject_at(r, c),
                subject_group=norm.group,
                value_num=num,
                value_low=low,
                value_high=high,
                value_err=err,
                value_text=text_v,
                value_bool=flag_v,
                value_form=form,
                reference=(
                    over["reference"]
                    if isinstance(over.get("reference"), str)
                    else None
                ),
                tier=tier,
                source_attribution=attribution,
                measurand_status=status,
                normalization=(
                    over["normalization"]
                    if isinstance(over.get("normalization"), str)
                    else None
                ),
                normalization_status=(
                    "explicit" if isinstance(over.get("normalization"), str) else None
                ),
                anchor=MeasureAnchor(
                    paper, chunk_id, "offsets", [chunk_id, cell.start, cell.end]
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
