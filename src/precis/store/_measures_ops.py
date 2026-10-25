"""The measures substrate (migration 0187). Mixin on
:class:`precis.store.Store`.

Backs ``docs/backlog/measures-substrate.md`` (pilot Build A). ``measures`` is
one append-only sourced-number record for any subject ref; ``material_values``
and ``component_spec_values`` are compatibility views over it
(:mod:`precis.store._material_ops` / :mod:`precis.store._component_ops` keep
writing through them unchanged).

**A run.** :meth:`insert_measure` writes one *output* row and exactly its own
*input* rows (``direction='input'`` — set temperature, potential, product),
tied by one ``run_key``, in one transaction. "FE 95% at -0.5 V and 61% at
-0.9 V" is two runs: the caller calls it twice.

**Evidence edge.** A row with an ``anchor`` points (``primary_link_id``) at a
``quantifies`` link written through ``add_link``: paper (``src_chunk_id`` = the
anchoring chunk) -> measurand taxon, edge meta empty. The link is deduplicated,
so two measures on the same chunk and measurand share one edge. The anchor
itself — ``anchor_scheme`` and ``span`` — lives on the measure row, so each
measure keeps its own span. Further anchors (``extra_anchors``) each get their
own edge and are listed in ``meta.extra_anchors`` as
``{link_id, anchor_scheme, span}``.

**Computed on write, never asserted.** ``extraction_status`` is
``anchor_matched`` / ``anchor_mismatch`` by looking for the literal in the
anchored span (a mismatch flags the row, never rejects it) and ``unverified``
without an anchor.

**Flags, not refusals.** A missing required condition, or a mass-rate yield
whose product has no formula, lands in ``meta.escalation`` (a list of
``{"rule": ..., ...}``); ``best_measure`` excludes flagged rows. Two sanity
flags join them (needs-review, never a drop): ``implausible_value`` — the
canonical value sits ``_OUTLIER_DECADES`` or more orders of magnitude off the
measurand's live median, which is what a stripped ``µ`` looks like ("6 µm"
extracts as "6 m", gr228594) — and ``glyph_suspected`` — the source paper's
extracted text carries the ingest ``glyph_health`` flag (gr228652). The
required names come from the output measurand taxon's ``meta.required_conditions``
and those of every ancestor along ``specialises`` (union); an input row
satisfies a name when it has ``role='context'`` and its ``meta.condition``
label, its taxon's slug, name or alias matches. A condition with no
taxon of its own (``product``) still needs *some* taxon (``measurand_ref_id``
is NOT NULL): the caller mints a ``reaction product`` taxon and labels the row
``meta={'condition': 'product'}``; its formula goes in ``value_text``.

**Refusals.** ``tier='measured'`` without an anchor whose chunk is set;
``tier='measured'`` with ``source_attribution='cited_work'``; a live output
row that repeats another's measurand, anchor scheme, span and value (one
chunk value extracted under two findings is one measure, not two); a unit
with no
dimension match to the measurand's ``canonical_unit`` (naming both); a
number with a ``reported_unit`` on a taxon that has no ``canonical_unit``
(values are stored normalised to one unit per measurand); and (migration 0188,
Store SI) **a number with no ``reported_unit`` on a taxon that has a
``canonical_unit``**, naming the canonical and the display unit: "95" is
ambiguous between 95 % and 0.95, and a legacy "5" between 5 mm and 5 m, so
neither is guessed. A caller whose number really is in the canonical unit
(a table-recipe row) passes that unit as ``reported_unit``. Unitless numeric
rows are accepted only on a unit-less taxon.

**Re-point door (0234).** A row's ``measurand_ref_id`` is frozen like every
other column, so a duplicate-measurand merge whose loser carries rows was
unappliable. ``repoint_measurand_measures`` is the one door: inside a
transaction that sets ``precis.allow_measurand_repoint`` (the helper sets
it), ``measurand_ref_id`` — nothing else — may move onto a live taxon with
the same ``canonical_unit``/``dimension_kind``/``si_vector`` (checked in
the trigger). Live and superseded rows move together so the merge guard
counts zero. Reviews on the moved rows refuse the run — the measure sha
covers ``measurand_ref_id``, a re-point stales them.

**Legacy taxa.** A taxon seeded from a legacy registry (``meta.legacy_source``)
has a ``measure_unit_compat`` row: the unit the registry kept (``legacy_unit``),
its SI unit and the linear map. A ``reported_unit`` equal to the legacy unit
converts through that row (exact, the same factor the compatibility views use);
anything else goes through pint. ``measures_for`` and the Build B reads carry the
taxon's ``display_unit`` and convert at the edge.

**Reads (Build B).** :meth:`best_measure` (best live value per
``(measurand, reference, normalization)`` group over everything serving a
quest), :meth:`measures_census`, :meth:`search_measures` (measurand incl.
descendants, numeric interval overlap in SI, run-condition filters, subject
words) and :meth:`measure_detail` back ``kind='measure'`` and the quest
``view='measures'``. Values are stored in SI; the unit conversion for queries
and output lives in :mod:`precis.taxonomy.measure_units`.

Reviews of a measure go through the shared ledger
(``record_target_review('measure', id, ...)``); the sha covers the frozen
fields only (``precis_measure_sha``). Ranking excludes the newest current
review's rejection, not every historical rejection: a later approval can
restore eligibility, while a stale verdict says nothing about this version.
Unreviewed and proposed rows remain eligible under the same numeric ranking.
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from precis.errors import BadInput, NotFound
from precis.extract.accept import DEFAULT_BAR, DEFAULT_Z
from precis.store._taxon_ops import _EDGES
from precis.taxonomy.measure_units import (
    MINUS_FOLD,
    Conversion,
    NeedsMolarMass,
    basis_normalization,
    is_coherent_si,
    make_converter,
    molar_mass,
    parse_literal,
    si_form,
    to_canonical,
)
from precis.taxonomy.money import (
    check_price_year,
    effective_currency,
    row_currency,
    split_currency,
    taxon_is_currency,
)
from precis.taxonomy.nodes import slugify
from precis.utils.glyph_health import glyph_suspected
from precis.utils.text import markup_surface

log = logging.getLogger(__name__)

TIERS = ("measured", "computed", "derived", "asserted")
ATTRIBUTIONS = ("own_work", "cited_work", "not_established")
MEASURAND_STATUSES = ("explicit", "interpreted", "ambiguous")
NORMALIZATION_STATUSES = ("explicit", "inferred", "unresolved")
ROLES = ("context", "preparation", "model")

#: Slice-8 sanity flag: a new output value this far from the measurand's live
#: median (log10 decades) is needs-review — a stripped µ gives 6 (gr228594).
_OUTLIER_DECADES = 3.0
#: the median needs a distribution: fewer existing values, no flag
_OUTLIER_MIN_SAMPLE = 3
INPUT_DIRECTIONS = ("input", "covariate")


@dataclass(frozen=True, slots=True)
class MeasureAnchor:
    """Where a number is printed: ``chunk_id`` of ``paper_ref_id``, the span
    inside it (``scheme`` names how: sentence / range / atom / offsets;
    ``span`` is the string, or ``[chunk, start, end]`` for raw offsets)."""

    paper_ref_id: int
    chunk_id: int
    scheme: str
    span: Any


@dataclass(frozen=True, slots=True)
class MeasureSpec:
    """One row of a run. ``literal`` is the exact printed string; the parsed
    ``value_*`` fields default to :func:`parse_literal` of it when none is
    given. A caller-supplied ``value_num`` / ``value_low`` / ``value_high`` /
    ``value_err`` is in the REPORTED unit (the unit of ``reported_unit`` and the
    literal), never the canonical one: it is converted on write (literal
    ``500``, ``reported_unit='mV'``, ``value_num=500`` stores 0.5 V).
    ``reported_unit`` is the unit the number is printed in; it is
    required for a number on a taxon with a ``canonical_unit`` (a number
    already in the canonical unit says so by passing it)."""

    measurand_ref_id: int
    literal: str
    subject_ref_id: int
    reported_unit: str | None = None
    #: the year a currency figure is priced at (``EUR2015/t``, or the field);
    #: NULL means unknown — shown as reported, never converted (gr510555 S1)
    price_year: int | None = None
    subject: str | None = None
    subject_group: str | None = None
    value_num: float | None = None
    value_low: float | None = None
    value_high: float | None = None
    value_text: str | None = None
    value_bool: bool | None = None
    value_err: float | None = None
    value_form: str | None = None
    reference: str | None = None
    tier: str | None = None
    source_attribution: str | None = None
    measurand_status: str | None = None
    normalization: str | None = None
    normalization_status: str | None = None
    role: str | None = None
    direction: str | None = None
    anchor: MeasureAnchor | None = None
    extra_anchors: Sequence[MeasureAnchor] = ()
    derived_from: Sequence[int] | None = None
    supersedes: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    # the legacy fact-table columns (material / component / rxn writers); a
    # ``source_ref_id`` wins over the anchor's paper (a source need not be one)
    maturity: str | None = None
    method: str | None = None
    conditions: dict[str, Any] | None = None
    source_ref_id: int | None = None
    source_chunk: str | None = None
    source_url: str | None = None
    as_of: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class MeasureRun:
    """What :meth:`MeasuresMixin.insert_measure` wrote."""

    run_key: str
    output_id: int
    input_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ConditionFilter:
    """One condition term of a search: the run must have an input row named
    ``name`` (its own condition label, or its taxon's slug, name or alias)
    whose value satisfies ``op`` (``=`` ``<`` ``>`` ``<=`` ``>=``) against
    ``value`` or ``text``. A numeric ``value`` is in ``unit`` when given, else
    in the input taxon's ``display_unit``, else SI; a ``text`` value compares
    by slug with ``=`` only (``M2 Ultra`` equals ``M2-Ultra``)."""

    name: str
    op: str = "="
    value: float | None = None
    unit: str | None = None
    text: str | None = None


#: ``bound_currency`` year sentinel in :meth:`MeasuresMixin.search_measures`:
#: the bound's currency at *any* price year — a range without an explicit
#: ``unit=`` spans the taxon's currency across all years, each row still
#: labelling its own (gr510555 S1 ruling). An explicit ``unit=`` is an
#: exact ``(currency, price_year)`` group instead, NULL year included.
ANY_YEAR: Final = -1


@dataclass(frozen=True, slots=True)
class MeasureSearch:
    """What :meth:`MeasuresMixin.search_measures` found. ``truncated`` means
    the candidate scan hit its cap before the end. ``other_currency`` counts
    in-range candidates left out of a currency-bounded range because they
    are in another ``(currency, price_year)`` group (gr510555 S1: ranges
    compare within one group until conversion lands)."""

    rows: list[dict[str, Any]]
    truncated: bool = False
    other_currency: int = 0


#: The row that re-based this one (migration 0188 supersedes a legacy row L, whose
#: numbers are in the taxon's old unit, by a row N in SI whose ``meta`` says
#: ``rebased_from`` L and carries the ``conversion``). Joined from L, it lets a
#: reader show L in SI like every other row (:func:`_si_normalise`).
_REBASE_JOIN = (
    "LEFT JOIN measures sx ON sx.id = m.superseded_by "
    "AND sx.meta ->> 'rebased_from' = m.id::text"
)

_NUMERIC_COLS = ("value_num", "value_low", "value_high")


def _si_normalise(rows: Sequence[dict[str, Any]]) -> None:
    """Map the numbers of every row that carries a ``rebase`` (a superseded
    legacy row: stored in the old unit) to the taxon's SI unit, in place, so a
    superseded row reads like a live one and never off by the unit factor."""
    for r in rows:
        conv = r.get("rebase")
        if not isinstance(conv, dict):
            continue
        factor = Decimal(str(conv["factor"]))
        offset = Decimal(str(conv.get("offset") or 0))
        for col in _NUMERIC_COLS:
            if r.get(col) is not None:
                r[col] = float(Decimal(str(r[col])) * factor + offset)
        if r.get("value_err") is not None:
            r["value_err"] = float(Decimal(str(r["value_err"])) * factor)


def legacy_value(
    value: float | None,
    factor: float | Decimal | None,
    offset: float | Decimal | None,
) -> float | None:
    """A stored SI number back in the legacy unit, the same rule as the SQL
    ``precis_measure_legacy_value``: ``(si - offset) / factor`` in decimal,
    rounded to 15 significant digits (7 mm stored as 0.007 m reads back as 7).
    No factor means the taxon was never re-based."""
    if value is None or factor is None:
        return value
    if math.isinf(value) or math.isnan(value):
        return value
    shifted = Decimal(str(value)) - Decimal(str(offset or 0))
    return float(f"{float(shifted / Decimal(str(factor))):.15g}")


#: A bound matches a value that equals it to within float noise (1.4 Å stored
#: as 1.4e-10 m by one path and 0.14 nm by another must still meet at 1.4 Å).
_REL_TOL = 1e-9

#: ``anchor_lost``: the row can't produce an anchor — either it was written
#: with one (``anchor_scheme`` is stamped at insert exactly when ``anchor=``
#: was given) whose link is gone (the chunk or paper was deleted and the
#: foreign key set ``primary_link_id`` NULL), or it is a ``measured`` row
#: with no link at all (its anchor IS the claim, so a linkless measured row
#: is flaggable on its own — every ``best_measure``/``search_measures``
#: exclusion then carries this flag as its reason). Other tiers with a
#: never-recorded anchor are legal (``asserted`` needs none) and stay
#: unflagged; a lost anchor already stales their reviews through the sha.
_ANCHOR_LOST_SQL = (
    "(m.primary_link_id IS NULL AND (m.anchor_scheme IS NOT NULL "
    "OR coalesce(m.tier = 'measured', false)))"
)

_MEASURE_COLS = (
    "m.*, t.meta ->> 'name' AS measurand, t.meta ->> 'slug' AS measurand_slug, "
    "t.meta -> 'aliases' AS measurand_aliases, "
    "t.meta ->> 'canonical_unit' AS canonical_unit, "
    "t.meta ->> 'display_unit' AS display_unit, "
    "t.meta -> 'higher_is_better' AS higher_is_better, "
    "s.title AS subject_title, s.kind AS subject_kind, p.kind AS paper_kind, "
    + _ANCHOR_LOST_SQL
    + " AS anchor_lost, sx.meta -> 'conversion' AS rebase"
)
_MEASURE_FROM = (
    "FROM measures m JOIN refs t ON t.ref_id = m.measurand_ref_id "
    "JOIN refs s ON s.ref_id = m.subject_ref_id "
    "LEFT JOIN refs p ON p.ref_id = m.source_ref_id "
    f"{_REBASE_JOIN}"
)
#: A row's numeric extent as SQL: a point is [v, v], an interval [low, high], an
#: upper bound ``<x`` runs from -inf to x and a lower bound from x to +inf.
_LO_SQL = (
    "(CASE WHEN m.value_form = 'upper_bound' THEN '-Infinity'::float8 "
    "ELSE coalesce(m.value_low, m.value_num) END)"
)
_HI_SQL = (
    "(CASE WHEN m.value_form = 'lower_bound' THEN 'Infinity'::float8 "
    "ELSE coalesce(m.value_high, m.value_num) END)"
)


def register_legacy_unit(
    conn: Connection, table: str, key: str, unit: str | None
) -> None:
    """The legacy mint verbs (``material_property_mint``, ``component_spec_mint``,
    ``rxn_property_mint``) call this in the transaction that inserts the registry
    row. The registry keeps the unit it was given; when that unit converts to SI
    (``nm``, ``MPa``, ``%``, ``degC``, ...) a ``measure_unit_compat`` row is
    inserted for ``(table, key)``, so the taxon ``precis_measure_taxon`` mints
    later stores SI and shows this unit, and the compatibility views and their
    insert triggers convert exactly as for the rows 0188 seeded.

    The factor comes from an existing compat row with the same ``legacy_unit``
    when there is one (the seeded rows stay the one source), else from pint
    (:func:`~precis.taxonomy.measure_units.si_form`). A unit that is already
    coherent SI writes no row and stays quiet; a unit with no SI form (USD,
    HV, pH) writes no row either, but the skip is logged — counted and
    visible, never silent (the later conversion migration lists the same
    keys with an anti-join on ``measure_unit_compat``). It does not refuse:
    the legacy verbs must keep minting. Concurrent mints
    of one key serialise on an advisory lock, like ``precis_measure_taxon``."""
    text = (unit or "").strip()
    if not text:
        return
    row = conn.execute(
        "SELECT si_unit, factor, si_offset FROM measure_unit_compat "
        "WHERE legacy_unit = %s ORDER BY legacy_table, legacy_key LIMIT 1",
        (text,),
    ).fetchone()
    if row is not None:
        si_unit, factor, offset = row
    else:
        form = si_form(text)
        if form is None:
            if not is_coherent_si(text):
                log.warning(
                    "measure_unit_compat skip: %s %s keeps unit %r — it has "
                    "no SI form, so no conversion row was written",
                    table,
                    key,
                    text,
                )
            return
        si_unit, factor, offset = form.si_unit, form.factor, form.offset
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (f"precis_measure_compat:{table}:{key}",),
    )
    conn.execute(
        "INSERT INTO measure_unit_compat "
        "(legacy_table, legacy_key, legacy_unit, si_unit, factor, si_offset) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (table, key, text, si_unit, factor, offset),
    )


def _sense(value: Any) -> str | None:
    """``higher`` / ``lower`` from a caller's ``sense=`` or a taxon's
    ``higher_is_better`` (a bool); None when unset."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "higher" if value else "lower"
    word = str(value).strip().casefold()
    if word in ("higher", "max", "high", "true"):
        return "higher"
    if word in ("lower", "min", "low", "false"):
        return "lower"
    raise BadInput(
        f"sense {value!r} is not higher/max or lower/min",
        next="sense='max' (higher is better) or sense='min'",
    )


def row_interval(row: dict[str, Any]) -> tuple[float, float] | None:
    """A row's numeric extent ``(low, high)`` in canonical units, infinities
    for a one-sided bound; None when it carries no number (a category)."""
    form = row.get("value_form")
    num, low, high = row.get("value_num"), row.get("value_low"), row.get("value_high")
    if form in ("upper_bound", "lower_bound"):
        # the parse path puts a bound in num; a caller may write it into
        # high (<x) or low (>x) — the pilot's corrections did
        bound = num if num is not None else (high if form == "upper_bound" else low)
        if bound is not None:
            return (
                (float("-inf"), bound)
                if form == "upper_bound"
                else (bound, float("inf"))
            )
    lo = low if low is not None else num
    hi = high if high is not None else num
    if lo is None or hi is None:
        return None
    return (lo, hi)


def _condition_holds(f: ConditionFilter, conditions: Sequence[dict[str, Any]]) -> bool:
    """Does any input row of the run satisfy the filter?"""
    want = slugify(f.name)
    for c in conditions:
        if want not in c["names"]:
            continue
        if f.text is not None:
            shown = c.get("value_text") or c.get("literal") or ""
            if f.op == "=" and slugify(shown) == slugify(f.text):
                return True
            continue
        if f.value is None:
            return True
        extent = row_interval(c)
        if extent is None:
            continue
        canon, disp = c.get("canonical_unit"), c.get("display_unit")
        try:
            if f.unit:
                v = to_canonical(f.value, f.unit, canon)
            elif disp and canon:
                v = to_canonical(f.value, disp, canon)
            else:
                v = f.value
        except BadInput:
            continue  # a unit of another kind: this input is not the one meant
        lo, hi = extent
        near = (
            math.isclose(v, lo, rel_tol=_REL_TOL),
            math.isclose(v, hi, rel_tol=_REL_TOL),
        )
        if f.op == "=" and (lo <= v <= hi or any(near)):
            return True
        if f.op in ("<", "<=") and (lo < v or (f.op == "<=" and near[0])):
            return True
        if f.op in (">", ">=") and (hi > v or (f.op == ">=" and near[1])):
            return True
    return False


MEASURE_VALUE_FORMS = frozenset(
    {
        "point",
        "approximate_point",
        "upper_bound",
        "lower_bound",
        "interval",
        "categorical",
        "boolean",
        "not_established",
    }
)
VALUE_FORM_ALIASES = {"upper": "upper_bound", "lower": "lower_bound"}


def normalize_value_form(form: str) -> str:
    """A caller's value_form in the ``measures_value_form_check`` spelling:
    ``upper``/``lower`` become the bound forms; anything else outside the
    allowed set is refused by name."""
    form = VALUE_FORM_ALIASES.get(form, form)
    if form not in MEASURE_VALUE_FORMS:
        raise BadInput(
            f"value_form {form!r} is not one of "
            f"{', '.join(sorted(MEASURE_VALUE_FORMS))}",
            next="value_form='upper_bound' for '<5', 'lower_bound' for '>5', "
            "or omit it to infer from the literal",
        )
    return form


def _check_enum(name: str, value: str | None, allowed: Sequence[str]) -> None:
    if value is not None and value not in allowed:
        raise BadInput(f"{name} {value!r} is not one of {', '.join(allowed)}")


def _squash(text: str) -> str:
    """Markup-folded, whitespace-collapsed, minus-folded, case-folded
    form for the literal-in-span test — the same surface the
    extraction-time check sees, so a LaTeX/``<sup>`` span still stamps
    ``anchor_matched`` for the plain literal — incl. a sign-only table cell merged onto its
    number ("− | 1.93" reads "-1.93") (gr511458, gr511461)."""
    t = markup_surface(text).translate(MINUS_FOLD)
    t = t.replace("\u2009", " ").replace("\xa0", " ")

    return " ".join(t.split()).casefold()


def _literal_in(span_text: str, literal: str) -> bool:
    """Does the literal occur in the span text, on number boundaries? A digit
    run must not be preceded by a digit (or ``digit.``) nor followed by a digit
    (or ``.digit``): "5" and "95" are not in "1950"."""
    lit = _squash(literal)
    if not lit:
        return False
    pattern = re.escape(lit)
    if lit[0].isdigit():
        pattern = r"(?<!\d)(?<!\d\.)" + pattern
    if lit[-1].isdigit():
        pattern += r"(?!\d)(?!\.\d)"
    return re.search(pattern, _squash(span_text)) is not None


def _span_text(chunk_text: str, span: Any) -> str:
    """The text a span names. Raw offsets ``[chunk, start, end]`` slice the
    chunk; every other scheme falls back to the whole chunk (the span's
    sentence/atom ids are not resolvable here), a weaker test."""
    if (
        isinstance(span, (list, tuple))
        and len(span) == 3
        and all(isinstance(x, int) and not isinstance(x, bool) for x in span)
    ):
        return chunk_text[span[1] : span[2]]
    return chunk_text


class MeasuresMixin:
    """``measures`` writes and reads; needs ``self.pool`` / ``self.tx``."""

    pool: Any
    tx: Any
    add_link: Any
    ancestors: Any
    taxon_descendants: Any
    reviews_for: Any

    # -- helpers -----------------------------------------------------------

    def _measures_conn(self, conn: Connection | None) -> AbstractContextManager[Any]:
        return nullcontext(conn) if conn is not None else self.tx()

    def _measure_taxa(
        self, conn: Connection, ids: Sequence[int]
    ) -> dict[int, dict[str, Any]]:
        rows = conn.execute(
            "SELECT ref_id, kind, retired_at IS NOT NULL, meta "
            "FROM refs WHERE ref_id = ANY(%s)",
            (list(set(ids)),),
        ).fetchall()
        out: dict[int, dict[str, Any]] = {}
        for ref_id, kind, retired, meta in rows:
            if kind != "taxon" or retired:
                raise BadInput(
                    f"measurand ref {ref_id} is not a live taxon (kind={kind!r})",
                    next="mint the measurand: put(kind='taxon', text='<name> — <definition>', "
                    "meta={'canonical_unit': ..., 'dimension_kind': ...}) under 'measurand'",
                )
            out[int(ref_id)] = meta or {}
        missing = set(ids) - set(out)
        if missing:
            raise NotFound(
                f"measurand taxon ref {sorted(missing)[0]} not found",
                next="search(kind='taxon', q='...') for the node, then pass its id",
            )
        return out

    def _required_conditions(self, conn: Connection, measurand_id: int) -> list[str]:
        """Union of ``meta.required_conditions`` over the measurand and its
        ancestors (``specialises`` chain), nearest first. Runs on ``conn``."""
        walk = conn.execute(
            f"""
            WITH RECURSIVE {_EDGES},
            walk(ref_id, depth, path) AS (
              SELECT e.parent, 1, ARRAY[%(r)s::bigint, e.parent]
                FROM e WHERE e.child = %(r)s
              UNION ALL
              SELECT e.parent, w.depth + 1, w.path || e.parent
                FROM walk w JOIN e ON e.child = w.ref_id
               WHERE NOT e.parent = ANY(w.path)
            )
            SELECT ref_id FROM walk GROUP BY ref_id ORDER BY min(depth), ref_id
            """,
            {"r": measurand_id},
        ).fetchall()
        chain = [measurand_id] + [int(r[0]) for r in walk]
        rows = conn.execute(
            "SELECT ref_id, meta -> 'required_conditions' FROM refs "
            "WHERE ref_id = ANY(%s) AND kind = 'taxon'",
            (chain,),
        ).fetchall()
        by_id = {int(r[0]): r[1] for r in rows}
        out: list[str] = []
        for rid in chain:
            for name in by_id.get(rid) or []:
                if isinstance(name, str) and name.strip() and name not in out:
                    out.append(name)
        return out

    @staticmethod
    def _condition_names(spec: MeasureSpec, taxon_meta: dict[str, Any]) -> set[str]:
        names: set[str] = set()
        label = spec.meta.get("condition")
        if isinstance(label, str) and label.strip():
            names.add(slugify(label))
        for key in ("slug", "name"):
            val = taxon_meta.get(key)
            if isinstance(val, str) and val.strip():
                names.add(slugify(val))
        for val in taxon_meta.get("aliases") or []:
            if isinstance(val, str) and val.strip():
                names.add(slugify(val))
        return names

    def _anchor_chunk(self, conn: Connection, anchor: MeasureAnchor) -> tuple[int, str]:
        """``(ord, text)`` of the anchored chunk, checked to belong to the paper."""
        row = conn.execute(
            "SELECT ref_id, ord, text, retired_at IS NOT NULL FROM chunks "
            "WHERE chunk_id = %s",
            (anchor.chunk_id,),
        ).fetchone()
        if row is None:
            raise NotFound(
                f"anchor chunk {anchor.chunk_id} not found",
                next="anchor to a live chunk of the paper that prints the number",
            )
        if row[3]:
            raise BadInput(
                f"anchor chunk {anchor.chunk_id} is retired",
                next="anchor to a live chunk of the paper that prints the number",
            )
        if int(row[0]) != anchor.paper_ref_id:
            raise BadInput(
                f"anchor chunk {anchor.chunk_id} belongs to ref {row[0]}, "
                f"not paper {anchor.paper_ref_id}",
            )
        return int(row[1]), str(row[2] or "")

    def _anchor_link(
        self,
        conn: Connection,
        anchor: MeasureAnchor,
        measurand_ref_id: int,
        link_set_by: str,
    ) -> tuple[int, str]:
        """The shared ``quantifies`` edge for this chunk and measurand, and the
        chunk text. Deduplicated by ``add_link``; the span stays on the row."""
        ord_, text = self._anchor_chunk(conn, anchor)
        link = self.add_link(
            src_ref_id=anchor.paper_ref_id,
            src_pos=ord_,
            dst_ref_id=measurand_ref_id,
            relation="quantifies",
            set_by=link_set_by,
            conn=conn,
        )
        return int(link.id), text

    # -- write -------------------------------------------------------------

    @staticmethod
    def _compat_conversion(
        c: Connection, taxon: dict[str, Any], reported_unit: str, canonical: str
    ) -> Conversion | None:
        """The exact converter of a taxon's ``measure_unit_compat`` row when
        ``reported_unit`` is the unit the taxon was kept in before its SI
        re-base and the taxon stores the row's SI unit; None otherwise (pint
        decides). The row is keyed by ``legacy_source`` for a legacy taxon,
        else by ``('taxon', slug)`` (0236). The same factor the migrations and
        compatibility views use, so a number written through any door reads
        back the same."""
        src = taxon.get("legacy_source")
        if isinstance(src, dict):
            key = (src.get("table"), src.get("key"))
        elif taxon.get("slug"):
            key = ("taxon", taxon.get("slug"))
        else:
            return None
        row = c.execute(
            "SELECT legacy_unit, si_unit, factor, si_offset FROM measure_unit_compat "
            "WHERE legacy_table = %s AND legacy_key = %s",
            key,
        ).fetchone()
        if row is None or row[1] != canonical or reported_unit.strip() != row[0]:
            return None
        # exact decimals, like the SQL (precis_measure_si_value): 25.4 mm is
        # exactly 0.0254 m through the migration, the views and this path alike
        factor, offset = Decimal(row[2]), Decimal(row[3])
        return Conversion(
            value=lambda v: float(Decimal(str(v)) * factor + offset),
            scale=float(factor),
            label=None,
            error=lambda e: float(Decimal(str(e)) * factor),
        )

    def insert_measure(
        self,
        output: MeasureSpec,
        inputs: Sequence[MeasureSpec] = (),
        *,
        actor: str,
        model: str | None = None,
        run_key: str | None = None,
        link_set_by: str = "agent",
        conn: Connection | None = None,
    ) -> MeasureRun:
        """Write one run — an output row and its input rows — in one
        transaction. Returns the new ids. See the module docstring for the
        refusals and the flags. Caller-supplied ``value_*`` are in the REPORTED
        unit and are converted to the measurand's canonical unit.

        With a caller-supplied ``conn`` the run joins the caller's
        transaction: a refusal part-way leaves the earlier rows of the run
        (and their edges) in it, so the caller must roll back."""
        if not actor or not actor.strip():
            raise BadInput("a measure needs an actor (who wrote it)")
        if output.direction not in (None, "output"):
            raise BadInput("the first row of a run is the output (direction='output')")
        for spec in inputs:
            if spec.direction not in (None, *INPUT_DIRECTIONS):
                raise BadInput(
                    f"an input row's direction is input or covariate, not {spec.direction!r}"
                )
        key = run_key or f"run:{uuid.uuid4().hex[:16]}"

        with self._measures_conn(conn) as c:
            taxa = self._measure_taxa(
                c, [output.measurand_ref_id, *(i.measurand_ref_id for i in inputs)]
            )
            required = self._required_conditions(c, output.measurand_ref_id)
            have: set[str] = set()
            for key in output.conditions or {}:
                if isinstance(key, str) and key.strip():
                    have.add(slugify(key))
            for spec in inputs:
                if (spec.role or "context") == "context":
                    have |= self._condition_names(spec, taxa[spec.measurand_ref_id])
            run_flags: list[dict[str, Any]] = [
                {
                    "rule": "required_condition_missing",
                    "condition": name,
                    "measurand": taxa[output.measurand_ref_id].get("slug"),
                }
                for name in required
                if slugify(name) not in have
            ]
            product = self._product_formula(inputs, taxa)

            out_id = self._insert_one(
                c,
                output,
                taxa[output.measurand_ref_id],
                direction="output",
                run_key=key,
                actor=actor,
                model=model,
                link_set_by=link_set_by,
                flags=run_flags,
                product=product,
            )
            in_ids = tuple(
                self._insert_one(
                    c,
                    spec,
                    taxa[spec.measurand_ref_id],
                    direction=spec.direction or "input",
                    run_key=key,
                    actor=actor,
                    model=model,
                    link_set_by=link_set_by,
                    flags=[],
                    product=None,
                )
                for spec in inputs
            )
        return MeasureRun(run_key=key, output_id=out_id, input_ids=in_ids)

    @staticmethod
    def _product_formula(
        inputs: Sequence[MeasureSpec], taxa: dict[int, dict[str, Any]]
    ) -> str | None:
        for spec in inputs:
            names = MeasuresMixin._condition_names(spec, taxa[spec.measurand_ref_id])
            if "product" in names:
                return (spec.value_text or spec.literal).strip() or None
        return None

    @staticmethod
    def _refuse_duplicate_output(
        c: Connection,
        spec: MeasureSpec,
        anchor: MeasureAnchor,
        values: tuple[Any, ...],
    ) -> None:
        """One chunk value extracted under two findings is one measure, not
        two (the pilot's mx212/mx218 pair): a live output row with the same
        measurand, subject, anchor scheme and span, and the same literal or
        parsed value is refused, naming the row. The same span's number about
        a *different* subject ref is a different claim and lands; a
        supersession of that row is not a duplicate."""
        rows = c.execute(
            "SELECT m.id, m.literal, m.value_num, m.value_low, m.value_high, "
            "       m.value_text, m.value_bool "
            "FROM measures m JOIN links l ON l.link_id = m.primary_link_id "
            "WHERE m.direction = 'output' AND m.superseded_by IS NULL "
            "  AND m.measurand_ref_id = %s AND m.subject_ref_id = %s "
            "  AND l.src_chunk_id = %s "
            "  AND m.anchor_scheme = %s AND m.span = %s::jsonb",
            (
                spec.measurand_ref_id,
                spec.subject_ref_id,
                anchor.chunk_id,
                anchor.scheme,
                Jsonb(anchor.span),
            ),
        ).fetchall()
        lit = _squash(spec.literal)
        has_value = any(v is not None for v in values)
        for row in rows:
            if spec.supersedes is not None and int(row[0]) == spec.supersedes:
                continue
            if _squash(row[1] or "") == lit or (
                has_value and (row[2], row[3], row[4], row[5], row[6]) == values
            ):
                raise BadInput(
                    f"duplicate of measure {int(row[0])}: a live output row "
                    "already carries this measurand's value on the same "
                    "anchor span",
                    next="the number is already written: "
                    "get(kind='measure', id=…) reads it; to replace it, put "
                    "the corrected run with meta={'supersedes': …}",
                )

    def _sanity_escalations(
        self,
        c: Connection,
        spec: MeasureSpec,
        anchor: MeasureAnchor | None,
        values: tuple[float | None, ...],
        *,
        currency: str | None = None,
        price_year: int | None = None,
        currency_taxon: bool = False,
        canonical_unit: str | None = None,
    ) -> list[dict[str, Any]]:
        """Slice-8 sanity flags on an output row (canonical ``values`` are
        ``(value_num, value_low, value_high)``). Both are needs-review, never
        a refusal or a drop: ``implausible_value`` when a reading sits
        ``_OUTLIER_DECADES`` or more orders of magnitude from the measurand's
        live median, and ``glyph_suspected`` when the source paper carries the
        ingest glyph-health flag. The median is computed per effective
        ``(currency, price_year)`` group — a NULL year is its own group;
        ``currency``/``price_year`` are the new row's, a stored/legacy row's
        comes from its columns or the token in its reported/canonical unit —
        unconverted currencies and price years never mix (gr510555 S1). On a
        measurand that is not a currency measurand at all every live row is
        in the one group and no unit is parsed."""
        out: list[dict[str, Any]] = []
        nums = [float(v) for v in values if v is not None and v != 0]
        if nums:
            if currency is None and not currency_taxon:
                mags = sorted(
                    math.log10(abs(float(r[0])))
                    for r in c.execute(
                        "SELECT value_num FROM measures "
                        "WHERE direction = 'output' AND superseded_by IS NULL "
                        "AND measurand_ref_id = %s "
                        "AND value_num IS NOT NULL AND value_num <> 0",
                        (spec.measurand_ref_id,),
                    ).fetchall()
                )
            else:
                mags = sorted(
                    math.log10(abs(float(r[0])))
                    for r in c.execute(
                        "SELECT value_num, currency, price_year, reported_unit "
                        "FROM measures "
                        "WHERE direction = 'output' AND superseded_by IS NULL "
                        "AND measurand_ref_id = %s "
                        "AND value_num IS NOT NULL AND value_num <> 0",
                        (spec.measurand_ref_id,),
                    ).fetchall()
                    if effective_currency(r[1], r[2], r[3], canonical_unit)
                    == (currency, price_year)
                )
        if nums and len(mags) >= _OUTLIER_MIN_SAMPLE:
            n = len(mags)
            med = mags[n // 2] if n % 2 else (mags[n // 2 - 1] + mags[n // 2]) / 2
            for v in nums:
                decades = math.log10(abs(float(v))) - med
                if abs(decades) >= _OUTLIER_DECADES:
                    out.append(
                        {
                            "rule": "implausible_value",
                            "value": float(v),
                            "median": 10.0**med,
                            "decades": round(decades, 1),
                            "n": n,
                            "why": (
                                f"the value sits {abs(decades):.1f} orders of "
                                f"magnitude "
                                f"{'above' if decades > 0 else 'below'} the "
                                "measurand's live median — a stripped µ reads "
                                "'µm' as 'm' (gr228594)"
                            ),
                        }
                    )
                    break
        src = spec.source_ref_id or (anchor.paper_ref_id if anchor else None)
        if src is not None:
            row = c.execute(
                "SELECT kind, meta FROM refs WHERE ref_id = %s", (src,)
            ).fetchone()
            if row is not None and row[0] == "paper":
                if glyph_suspected(row[1]):
                    out.append(
                        {
                            "rule": "glyph_suspected",
                            "source_ref_id": int(src),
                            "why": (
                                "the source paper's extracted text carries the "
                                "glyph-health flag — a font whose encoding lies "
                                "can turn 'µm' into 'm' (gr228652, gr228594)"
                            ),
                        }
                    )
        return out

    def _insert_one(
        self,
        c: Connection,
        spec: MeasureSpec,
        taxon: dict[str, Any],
        *,
        direction: str,
        run_key: str,
        actor: str,
        model: str | None,
        link_set_by: str,
        flags: list[dict[str, Any]],
        product: str | None,
    ) -> int:
        _check_enum("tier", spec.tier, TIERS)
        _check_enum("source_attribution", spec.source_attribution, ATTRIBUTIONS)
        _check_enum("measurand_status", spec.measurand_status, MEASURAND_STATUSES)
        _check_enum(
            "normalization_status", spec.normalization_status, NORMALIZATION_STATUSES
        )
        _check_enum("role", spec.role, ROLES)
        if direction not in ("output", *INPUT_DIRECTIONS):
            raise BadInput(f"direction {direction!r} is not input, output or covariate")
        if not spec.literal.strip():
            raise BadInput("a measure needs its literal (the exact printed string)")

        anchor = spec.anchor
        if spec.tier == "measured":
            if anchor is None or not anchor.chunk_id:
                raise BadInput(
                    "tier='measured' needs an anchored primary edge: a quantifies "
                    "link from the paper with src_chunk_id set (give anchor=)",
                    next="anchor=MeasureAnchor(paper_ref_id, chunk_id, scheme, span), "
                    "or use tier='asserted'",
                )
            if spec.source_attribution == "cited_work":
                raise BadInput(
                    "source_attribution='cited_work' never gets tier='measured': a "
                    "paper restating another paper's number is 'asserted' on this subject",
                    next="use tier='asserted' and chase the primary source",
                )

        # parsed reading: caller-supplied values win; the form comes from the
        # literal when it parses as a number/bound/interval/boolean
        caller_vals = any(
            v is not None
            for v in (
                spec.value_num,
                spec.value_low,
                spec.value_high,
                spec.value_text,
                spec.value_bool,
            )
        )
        parsed = parse_literal(spec.literal)
        if caller_vals:
            num, low, high = spec.value_num, spec.value_low, spec.value_high
            text, flag = spec.value_text, spec.value_bool
        else:
            num, low, high = parsed.num, parsed.low, parsed.high
            text, flag = parsed.text, parsed.flag
        err = spec.value_err if spec.value_err is not None else parsed.err
        if spec.value_form is not None:
            form = normalize_value_form(spec.value_form)
        elif parsed.form != "categorical" or not caller_vals:
            form = parsed.form
        else:
            form = (
                "categorical"
                if text is not None
                else "boolean"
                if flag is not None
                else "point"
                if num is not None
                else "interval"
                if low is not None and high is not None
                else "not_established"
            )

        # unit -> canonical
        meta: dict[str, Any] = dict(spec.meta)
        # Caller-supplied ``meta.escalation`` entries (a checker upstream of
        # the store flagging a misaligned table row, gr512370) merge into the
        # run's own list rather than being clobbered by it.
        escalation: list[dict[str, Any]] = list(flags) + [
            e for e in (meta.pop("escalation", None) or []) if isinstance(e, dict)
        ]
        normalization = spec.normalization
        normalization_status = spec.normalization_status
        reported_unit = spec.reported_unit
        canonical = taxon.get("canonical_unit")
        has_numbers = any(v is not None for v in (num, low, high))
        conditions = dict(spec.conditions or {})

        # currency (gr510555 slice 1): the reported_unit's token is recorded,
        # never converted; the price year comes from the token or the field,
        # and a disagreement between the two is refused
        cur = split_currency(reported_unit)
        is_currency_taxon = taxon_is_currency(taxon, canonical)
        if (
            is_currency_taxon
            and reported_unit
            and reported_unit.strip()
            and cur.currency is None
        ):
            raise BadInput(
                f"measure of {taxon.get('name') or 'this measurand'!r} is a "
                f"currency measurand, but reported_unit {reported_unit!r} "
                "carries no currency token",
                next="reported_unit='EUR2015/t'",
            )
        if (
            spec.price_year is not None
            and cur.price_year is not None
            and spec.price_year != cur.price_year
        ):
            raise BadInput(
                f"price_year={spec.price_year} disagrees with the year in "
                f"reported_unit {reported_unit!r} ({cur.price_year})",
                next="give the price year once: in the unit ('EUR2015/t') "
                "or as price_year=2015",
            )
        price_year = spec.price_year if spec.price_year is not None else cur.price_year
        currency = cur.currency
        if price_year is not None:
            check_price_year(price_year)
            if currency is None:
                raise BadInput(
                    f"price_year={price_year} on a measure with no currency: "
                    "a price year belongs to a currency figure",
                    next="report the unit as a currency ('EUR2015/t'), or drop price_year=",
                )
        if canonical and has_numbers and not (reported_unit and reported_unit.strip()):
            # Store SI: the number's unit is never guessed (a silent factor of
            # 1000, or of 100 for a fraction, is the failure this guards)
            shown = taxon.get("display_unit")
            name = taxon.get("name") or "this measurand"
            if shown and shown != canonical:
                message = (
                    f"measure of {name!r} gives a number with no reported_unit, but "
                    f"this measurand is stored in {canonical!r} and shown in "
                    f"{shown!r}; {spec.literal!r} could be in either, so it is not "
                    "guessed"
                )
            else:
                message = (
                    f"no unit given for {spec.literal!r} on {name!r} "
                    f"(canonical {canonical!r}); state the unit"
                )
            raise BadInput(
                message,
                next=f"state the unit the number is printed in "
                f"(reported_unit={shown or canonical!r}), or "
                f"reported_unit={canonical!r} when it is already in the canonical unit",
            )
        if reported_unit and canonical:
            # The dimension check runs even when the literal carries no
            # numbers: '50 %' parses as categorical, and a relative change
            # filed under an absolute measurand (gr511460) is the same
            # refusal a numeric 'W mK^2' gets.
            try:
                conv = self._compat_conversion(
                    c, taxon, reported_unit, canonical
                ) or make_converter(
                    reported_unit, canonical, delta=bool(taxon.get("difference"))
                )
            except NeedsMolarMass as need:
                conv = None
                if has_numbers:
                    mm = molar_mass(product) if product else None
                    if mm is None:
                        escalation.append(
                            {
                                "rule": "no_molar_mass",
                                "product": product,
                                "reported_unit": reported_unit,
                                "canonical_unit": canonical,
                                "direction": need.direction,
                            }
                        )
                        num = low = high = err = None
                        form = "not_established"
                    else:
                        conv = make_converter(
                            reported_unit, canonical, molar_mass_g_mol=mm
                        )
                        meta["conversion"] = {
                            "from": reported_unit,
                            "to": canonical,
                            "product": product,
                            "molar_mass_g_mol": mm,
                        }
            if conv is not None and has_numbers:
                if "conversion" not in meta and conv.scale != 1.0:
                    meta["conversion"] = {"from": reported_unit, "to": canonical}
                num = None if num is None else conv.value(num)
                low = None if low is None else conv.value(low)
                high = None if high is None else conv.value(high)
                err = (
                    None
                    if err is None
                    else abs(conv.error(err) if conv.error else err * conv.scale)
                )
                if conv.label and normalization is None:
                    normalization = basis_normalization(conv.label)
                    normalization_status = normalization_status or "explicit"
                if conv.gas:
                    # 'Gt CO2 yr-1' is a rate per CO2 mass — the gas rides
                    # the conditions dict, not the unit (gr512368).
                    conditions.setdefault("gas", conv.gas)
        elif reported_unit and has_numbers and canonical is None:
            raise BadInput(
                f"measure of {taxon.get('name') or 'this measurand'!r} reports a "
                f"number in {reported_unit!r}, but values are stored normalised to "
                "one unit per measurand and this taxon has none",
                next="set the taxon's canonical_unit first (meta canonical_unit), "
                "or write the number without a reported_unit if it is unitless",
            )
        if taxon.get("sign_convention") == "reduction" and has_numbers:
            # A below-baseline measurand ("emission reduction potential,
            # fraction below baseline") reads the magnitude: '54% below
            # baseline' and a change table's '−54%' both mean a 54%
            # reduction — wording never flips the sign (gr512365).
            num = None if num is None else abs(num)
            if low is not None and high is not None:
                low, high = min(abs(low), abs(high)), max(abs(low), abs(high))
            else:
                low = None if low is None else abs(low)
                high = None if high is None else abs(high)
        if direction == "output":
            escalation.extend(
                self._sanity_escalations(
                    c,
                    spec,
                    anchor,
                    (num, low, high),
                    currency=currency,
                    price_year=price_year,
                    currency_taxon=is_currency_taxon,
                    canonical_unit=canonical,
                )
            )
        if escalation:
            meta["escalation"] = escalation

        if direction == "output" and anchor is not None:
            self._refuse_duplicate_output(c, spec, anchor, (num, low, high, text, flag))

        # evidence edge + computed extraction status
        link_id: int | None = None
        status = "unverified"
        if anchor is not None:
            link_id, chunk_text = self._anchor_link(
                c, anchor, spec.measurand_ref_id, link_set_by
            )
            status = (
                "anchor_matched"
                if _literal_in(_span_text(chunk_text, anchor.span), spec.literal)
                else "anchor_mismatch"
            )
        if spec.extra_anchors:
            if anchor is None:
                raise BadInput("extra_anchors need a primary anchor= first")
            meta["extra_anchors"] = [
                {
                    "link_id": self._anchor_link(
                        c, extra, spec.measurand_ref_id, link_set_by
                    )[0],
                    "anchor_scheme": extra.scheme,
                    "span": extra.span,
                }
                for extra in spec.extra_anchors
            ]

        if spec.supersedes is not None:
            old = c.execute(
                "SELECT superseded_by, measurand_ref_id, subject_ref_id, direction, subject "
                "FROM measures WHERE id = %s FOR UPDATE",
                (spec.supersedes,),
            ).fetchone()
            if old is None:
                raise NotFound(f"measure {spec.supersedes} not found")
            if old[0] is not None:
                raise BadInput(
                    f"measure {spec.supersedes} is already superseded by {old[0]}"
                )
            if (old[1], old[2], old[3], old[4]) != (
                spec.measurand_ref_id,
                spec.subject_ref_id,
                direction,
                spec.subject,
            ):
                raise BadInput(
                    f"measure {spec.supersedes} has a different measurand, subject, "
                    "subject label or direction: a supersession replaces the same number",
                    next="write the new number as its own run instead",
                )

        try:
            row = c.execute(
                "INSERT INTO measures "
                "(subject_ref_id, measurand_ref_id, literal, reported_unit, "
                " currency, price_year, "
                " value_num, value_low, value_high, value_text, value_bool, "
                " value_err, value_form, reference, tier, extraction_status, "
                " normalization, normalization_status, source_attribution, "
                " measurand_status, direction, role, run_key, subject, "
                " subject_group, derived_from, primary_link_id, anchor_scheme, span, "
                " supersedes, source_ref_id, actor, model, meta, conditions, maturity, "
                " method, source_chunk, source_url, as_of, notes) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "        coalesce(%s, '{}'::jsonb), coalesce(%s, 'lab'), %s, %s, %s, %s, %s) "
                "RETURNING id",
                (
                    spec.subject_ref_id,
                    spec.measurand_ref_id,
                    spec.literal,
                    reported_unit,
                    currency,
                    price_year,
                    num,
                    low,
                    high,
                    text,
                    flag,
                    err,
                    form,
                    spec.reference,
                    spec.tier,
                    status,
                    normalization,
                    normalization_status,
                    spec.source_attribution,
                    spec.measurand_status,
                    direction,
                    spec.role or ("context" if direction == "input" else None),
                    run_key,
                    spec.subject,
                    spec.subject_group,
                    list(spec.derived_from) if spec.derived_from else None,
                    link_id,
                    anchor.scheme if anchor is not None else None,
                    Jsonb(anchor.span) if anchor is not None else None,
                    spec.supersedes,
                    spec.source_ref_id
                    if spec.source_ref_id is not None
                    else (anchor.paper_ref_id if anchor is not None else None),
                    actor,
                    model,
                    Jsonb(meta),
                    Jsonb(conditions) if conditions else None,
                    spec.maturity,
                    spec.method,
                    spec.source_chunk,
                    spec.source_url,
                    spec.as_of,
                    spec.notes,
                ),
            ).fetchone()
        except psycopg.errors.CheckViolation as exc:
            raise BadInput(
                f"measure refused by the database: {exc.diag.message_primary}",
                next="fix the field the check names; value_form is one of "
                + ", ".join(sorted(MEASURE_VALUE_FORMS)),
            ) from exc
        assert row is not None
        new_id = int(row[0])
        if spec.supersedes is not None:
            c.execute(
                "UPDATE measures SET superseded_by = %s, superseded_at = now() "
                "WHERE id = %s AND superseded_by IS NULL",
                (new_id, spec.supersedes),
            )
        return new_id

    def repoint_measurand_measures(
        self,
        src_ref_id: int,
        dst_ref_id: int,
        *,
        conn: Connection,
        actor: str,
        at: str | None = None,
    ) -> dict[str, Any]:
        """Move every measure row off one measurand taxon onto another —
        the canonical_unit-safe re-point (ontology-v2, Reto's Q8 ruling).

        The only sanctioned way a row changes ``measurand_ref_id``: the 0234
        door in ``precis_measures_frozen``, opened here by the
        transaction-local ``precis.allow_measurand_repoint`` setting, lets
        that column move *only* onto a live taxon with the same
        ``canonical_unit``, ``dimension_kind`` and ``si_vector`` — the stored
        SI numbers then mean the same under the new measurand, so the update
        is a re-label, not a re-base. Live and superseded rows alike move
        (the garden merge guard counts both), while ``subject_ref_id`` and
        ``primary_link_id`` references are *not* covered — a row pointing at
        ``src`` through one of those refuses the run by name.

        Guards beyond the trigger's: both refs must exist and be taxa, the
        target must be live (the trigger checks this too — a clearer error
        here), the dimension contract is compared in Python first so a
        mismatch names both units, and a measure carrying a *current*
        review refuses — ``precis_measure_sha`` covers ``measurand_ref_id``,
        so a re-point stales its reviews. The repoint is recorded on the
        source's ``meta.measure_repoint`` (survives the loser's retirement
        or merge) inside the same transaction.

        Returns a small report ``{moved, ids, reviews}``; ``moved`` counts
        every updated row. Caller supplies the transaction via ``conn`` so
        the re-point can commit atomically with a following merge/retire
        where the door allows (the garden apply path takes its own tx).
        """
        if src_ref_id == dst_ref_id:
            raise BadInput("re-point needs two distinct measurand taxa")
        taxa = conn.execute(
            "SELECT ref_id, kind, title, retired_at IS NOT NULL, meta "
            "FROM refs WHERE ref_id = ANY(%s) FOR UPDATE",
            ([src_ref_id, dst_ref_id],),
        ).fetchall()
        by_id = {int(r[0]): r for r in taxa}
        for rid in (src_ref_id, dst_ref_id):
            row = by_id.get(rid)
            if row is None:
                raise NotFound(f"measurand taxon {rid} not found")
            if row[1] != "taxon":
                raise BadInput(
                    f"re-point endpoint {rid} is a {row[1]!r}, not a taxon",
                    next="re-point runs measurand taxon -> measurand taxon only",
                )
        if by_id[dst_ref_id][3]:
            raise BadInput(
                f"target taxon {dst_ref_id} is retired — re-pointing onto it "
                "would strand the rows again",
                next="restore it, or pick a live measurand",
            )
        s_meta, d_meta = by_id[src_ref_id][4] or {}, by_id[dst_ref_id][4] or {}
        diff = [
            key
            for key in ("canonical_unit", "dimension_kind", "si_vector")
            if s_meta.get(key) != d_meta.get(key)
        ]
        if diff:
            raise BadInput(
                f"measurand {src_ref_id} -> {dst_ref_id} is not "
                f"canonical_unit-safe: {', '.join(diff)} differ "
                f"({src_ref_id} { {k: s_meta.get(k) for k in diff} } vs "
                f"{dst_ref_id} { {k: d_meta.get(k) for k in diff} })",
                next="re-point only between taxa storing the same unit; a "
                "unit change is a supersede-per-row re-base (not built)",
            )

        stray = conn.execute(
            "SELECT count(*) FROM measures WHERE subject_ref_id = %(r)s "
            "   OR primary_link_id IN (SELECT link_id FROM links "
            "        WHERE src_ref_id = %(r)s OR dst_ref_id = %(r)s)",
            {"r": src_ref_id},
        ).fetchone()
        if stray and int(stray[0]):
            raise BadInput(
                f"{int(stray[0])} measure(s) reference taxon {src_ref_id} as "
                "subject or through an anchored link — the re-point only "
                "covers measurand_ref_id rows",
                next="resolve those rows by hand first; the merge guard "
                "counts them too",
            )
        reviews = [
            int(r[0])
            for r in conn.execute(
                "SELECT r.review_id FROM reviews r JOIN measures m "
                "  ON m.id = r.target_id "
                "WHERE r.target_kind = 'measure' "
                "  AND m.measurand_ref_id = %s "
                "  AND r.content_sha = precis_target_sha('measure', m.id)",
                (src_ref_id,),
            ).fetchall()
        ]
        if reviews:
            raise BadInput(
                f"{len(reviews)} live measure review(s) pin rows on "
                f"{src_ref_id} ({reviews[:5]}) — a re-point changes "
                "measurand_ref_id, which precis_measure_sha covers: every "
                "review goes stale",
                next="get a ruling on the reviews first (they re-review "
                "against the re-labelled rows)",
            )

        ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT id FROM measures WHERE measurand_ref_id = %s ORDER BY id",
                (src_ref_id,),
            ).fetchall()
        ]
        if ids:
            conn.execute(
                "SELECT set_config('precis.allow_measurand_repoint', 'on', true)"
            )
            conn.execute(
                "UPDATE measures SET measurand_ref_id = %s WHERE measurand_ref_id = %s",
                (dst_ref_id, src_ref_id),
            )
        stamp = {
            "at": at or f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}",
            "actor": actor,
            "dst": dst_ref_id,
            "moved": len(ids),
            "ids": ids,
        }
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (Jsonb({"measure_repoint": stamp}), src_ref_id),
        )
        return {"moved": len(ids), "ids": ids, "reviews": len(reviews)}

    # -- read --------------------------------------------------------------

    def measures_for(
        self, subject_ref_id: int, *, include_superseded: bool = False
    ) -> list[dict[str, Any]]:
        """Every measure of one subject ref (live rows unless
        ``include_superseded``), grouped by run, outputs before inputs,
        oldest run first. Each row carries the measurand's name
        (``measurand``) and canonical unit, and ``anchor_lost``: the row
        can't produce an anchor — either it recorded one (``anchor_scheme``)
        whose link is gone (the chunk or paper was deleted and the foreign
        key set ``primary_link_id`` NULL), or it is a ``measured`` row with
        no link at all. Numbers are stored
        SI (the taxon's ``canonical_unit``); the row also carries the taxon's
        ``display_unit``, its ``legacy_source`` and, for a legacy taxon, the
        ``measure_unit_compat`` row (``legacy_unit``, ``legacy_factor``,
        ``legacy_offset``) so a caller can convert at the edge. ``best_measure``
        excludes the ``measured`` ones only; re-anchoring is a follow-up. The ids
        in ``meta.extra_anchors`` are not foreign keys and may dangle the same
        way."""
        sql = (
            "SELECT m.*, t.meta ->> 'name' AS measurand, "
            "       t.meta ->> 'canonical_unit' AS canonical_unit, "
            "       t.meta ->> 'display_unit' AS display_unit, "
            "       t.meta -> 'legacy_source' AS legacy_source, "
            "       c.legacy_unit, c.factor AS legacy_factor, "
            "       c.si_offset AS legacy_offset, sr.kind AS source_kind, "
            "       s.kind AS subject_kind, sx.meta -> 'conversion' AS rebase, "
            f"       {_ANCHOR_LOST_SQL} AS anchor_lost "
            "FROM measures m JOIN refs t ON t.ref_id = m.measurand_ref_id "
            "LEFT JOIN measure_unit_compat c "
            "  ON c.legacy_table = t.meta -> 'legacy_source' ->> 'table' "
            " AND c.legacy_key = t.meta -> 'legacy_source' ->> 'key' "
            " AND c.si_unit = t.meta ->> 'canonical_unit' "
            "LEFT JOIN refs sr ON sr.ref_id = m.source_ref_id "
            "LEFT JOIN refs s ON s.ref_id = m.subject_ref_id "
            f"{_REBASE_JOIN} "
            "WHERE m.subject_ref_id = %s "
            + ("" if include_superseded else "AND m.superseded_by IS NULL ")
            + "ORDER BY m.created_at, m.run_key, (m.direction <> 'output'), m.id"
        )
        with self.pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, (subject_ref_id,))
                rows = list(cur.fetchall())
        _si_normalise(rows)
        return rows

    def accepted_runs(
        self, tokens: str | Sequence[str]
    ) -> dict[str, dict[int, dict[str, Any]]]:
        """The governing verdict per covered paper for each extraction-run
        token: ``{token: {subject_ref_id: meta.acceptance}}`` — a token with
        no counting verdict maps to an empty dict.

        Read-only — the verdict stays on the job ref because a measure row's
        ``meta`` is frozen, so acceptance can never be stamped on the rows it
        covers (docs/backlog/corpus-quantitative-extraction.md, finding 2).

        A finished ``measure_accept`` verdict records its coverage:
        ``papers`` (the papers in the sampling frame) and ``max_id`` (the
        frame's row-id high-water mark, frozen at the draw). For each
        covered paper the latest *counting* verdict governs; a verdict
        counts only when it carries both coverage fields, its ``bar`` is
        at or under ``DEFAULT_BAR`` and its ``z`` at or above
        ``DEFAULT_Z`` — a loose-bar or loose-z re-accept can neither
        grant nor overturn — while a pre-coverage verdict grants nothing
        (fail closed) and a job that finished before reaching a verdict
        says nothing at all. Paper-scoped accept children each cover
        their paper; a run-level verdict covers every paper in the
        frame, so the run's coverage is the union — every paper needs a
        passing verdict.
        """
        toks = [tokens] if isinstance(tokens, str) else [str(t) for t in tokens]
        out: dict[str, dict[int, dict[str, Any]]] = {t: {} for t in toks}
        if not toks:
            return out
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT j.meta -> 'acceptance' ->> 'run_token' AS tok, "
                "       j.meta -> 'acceptance' AS acc "
                "  FROM refs j "
                " WHERE j.kind = 'job' AND j.retired_at IS NULL "
                "   AND j.meta ->> 'job_type' = 'measure_accept' "
                "   AND j.meta ? 'finished_at' "
                "   AND j.meta -> 'acceptance' ->> 'run_token' = ANY(%s) "
                " ORDER BY j.meta ->> 'finished_at' DESC, j.ref_id DESC",
                (toks,),
            ).fetchall()
        for tok, acc in rows:
            if not isinstance(acc, dict):
                continue
            papers, max_id, bar, z = (
                acc.get("papers"),
                acc.get("max_id"),
                acc.get("bar"),
                acc.get("z"),
            )
            if (
                not isinstance(papers, list)
                or not isinstance(max_id, int)
                or isinstance(max_id, bool)
                or not isinstance(bar, (int, float))
                or isinstance(bar, bool)
                or bar > DEFAULT_BAR
                or not isinstance(z, (int, float))
                or isinstance(z, bool)
                or z < DEFAULT_Z
            ):
                continue  # not a counting verdict: grants nothing, revokes nothing
            cov = out.setdefault(str(tok), {})
            for pid in papers:  # latest first — the first sighting governs
                if isinstance(pid, int) and not isinstance(pid, bool):
                    cov.setdefault(pid, dict(acc))
        return out

    # -- ranking and search (Build B) --------------------------------------

    def _serving_subjects(self, serving: int) -> set[int]:
        """The quest and every ref that reaches it along ``serves``, at any
        depth (papers serve a sub-quest that serves the quest)."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT kind FROM refs WHERE ref_id = %s AND retired_at IS NULL",
                (serving,),
            ).fetchone()
        if row is None:
            raise NotFound(f"quest {serving} not found")
        if row[0] != "quest":
            raise BadInput(
                f"serving= names a {row[0]!r} ref, not a quest",
                next="serving=<quest ref id>, e.g. the number in qu202467",
            )
        return {serving} | set(self.ancestors("serves", serving))

    def _measurand_cover(self, measurand: int) -> list[int]:
        """The taxon and its ``specialises`` descendants."""
        found = {rid for rid, _depth, _axis in self.taxon_descendants(measurand)}
        return [measurand, *sorted(found - {measurand})]

    def _required_conditions_map(self, measurand_ids: set[int]) -> dict[int, list[str]]:
        """``_required_conditions`` per measurand, on one connection."""
        if not measurand_ids:
            return {}
        with self.pool.connection() as conn:
            return {mid: self._required_conditions(conn, mid) for mid in measurand_ids}

    @staticmethod
    def _group_partition(
        required: Sequence[str], conditions: Sequence[dict[str, Any]]
    ) -> tuple[tuple[str, str], ...]:
        """The run's categorical required-condition values, as a sorted
        ``(name, value)`` tuple that joins the ``best_measure`` group key:
        "FE 98%" toward NH3 never ranks against FE toward NO2. A required
        name whose input is numeric or absent adds nothing."""
        out: dict[str, str] = {}
        for name in required:
            want = slugify(name)
            for c in conditions:
                if want not in c["names"] or c["value_form"] != "categorical":
                    continue
                value = (c["value_text"] or c["literal"] or "").strip()
                if value:
                    out[want] = value
                break
        return tuple(sorted(out.items()))

    def _measure_rows(
        self, where: str, params: dict[str, Any], *, with_review: bool = False
    ) -> list[dict[str, Any]]:
        cols = _MEASURE_COLS
        if with_review:
            cols += (
                ", coalesce((SELECT r.verdict FROM reviews r "
                "WHERE r.target_kind = 'measure' AND r.target_id = m.id "
                "AND r.content_sha = precis_target_sha('measure', m.id) "
                "ORDER BY r.at DESC, r.review_id DESC LIMIT 1), "
                "'unreviewed') AS review_state"
            )
        sql = f"SELECT {cols} {_MEASURE_FROM} WHERE {where} ORDER BY t.ref_id, m.id"
        with self.pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                rows = list(cur.fetchall())
        _si_normalise(rows)
        return rows

    def _conditions_by_run(
        self, run_keys: Sequence[str], *, live_only: bool = True
    ) -> dict[str, list[dict[str, Any]]]:
        """The input rows of each run, in write order. Each carries its display
        ``name`` (the row's own condition label, else its taxon's name) and the
        slugs a query may match it by (``names``)."""
        out: dict[str, list[dict[str, Any]]] = {k: [] for k in run_keys}
        if not out:
            return out
        sql = (
            f"SELECT {_MEASURE_COLS} {_MEASURE_FROM} "
            "WHERE m.run_key = ANY(%(keys)s) AND m.direction <> 'output' "
            + ("AND m.superseded_by IS NULL " if live_only else "")
            + "ORDER BY m.run_key, m.id"
        )
        with self.pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, {"keys": list(out)})
                rows = list(cur.fetchall())
        _si_normalise(rows)
        for r in rows:
            label = (r["meta"] or {}).get("condition")
            r["name"] = (
                label.strip()
                if isinstance(label, str) and label.strip()
                else r["measurand"]
            )
            names = {slugify(r["name"] or "")}
            for val in (
                r["measurand_slug"],
                r["measurand"],
                *(r["measurand_aliases"] or []),
            ):
                if isinstance(val, str) and val.strip():
                    names.add(slugify(val))
            names.discard("")
            r["names"] = names
            out[r["run_key"]].append(r)
        return out

    def best_measure(
        self,
        measurand: int | None = None,
        *,
        serving: int,
        sense: str | None = None,
        reference: str | None = None,
        accepted_only: bool = False,
    ) -> list[dict[str, Any]]:
        """The best live value per comparable group over everything serving
        the quest ``serving``.

        Candidates are live *output* rows whose subject is the quest or reaches
        it along ``serves`` at any depth, measuring ``measurand`` or any
        ``specialises`` descendant of it (every measurand when ``measurand``
        is None). Left out: ``measurand_status='ambiguous'``, a row flagged in
        ``meta.escalation`` (a missing required condition, no molar mass), an
        anchor-lost ``measured`` row, ``trusted = false``, a newest current
        review of ``rejected``, and anything without a single numeric reading
        (an interval, a bound, a category). The anchor filter stops at
        ``measured`` on purpose: there the anchor IS the claim, while an
        asserted/computed/derived row keeps its claim when the link dies —
        it stays ranked, marked ``anchor_lost``, and its anchor-covered
        reviews go stale (gr515817).
        ``reference`` keeps only rows stated against that reference; a
        conversion between references is not built, so a SHE row never stands
        in for an RHE one.

        Rows group by ``(measurand, reference, normalization, currency,
        price_year)`` — a NULL price year is its own group, so EUR 2010
        never ranks against EUR 2023 — plus the
        values of the measurand's *categorical* required conditions — a
        Faradaic efficiency is ranked per product, so a group reads "FE ·
        product=NH3" — and are never compared across groups: each group
        answers for itself. ``sense``
        (``higher``/``max`` or ``lower``/``min``) overrides the taxon's
        ``higher_is_better``; a group with neither has ``best=None`` and says
        how many rows it holds. Each result is ``{measurand_ref_id, measurand,
        reference, normalization, currency, price_year, by, sense, n, best}``,
        ``by`` the
        ``{name: value}`` partition (``{'product': 'NH3'}``), ``best`` a row
        dict with
        its run's ``conditions`` and ``review_state`` (newest current verdict,
        else ``unreviewed``); ordered by measurand name, reference,
        normalization, partition.

        ``accepted_only`` keeps a row stamped with an ``extraction_run``
        token only when its paper's governing ``measure_accept`` verdict
        (:meth:`accepted_runs`) passed, the row id sits at or under that
        verdict's ``max_id`` high-water mark, and the row still carries its
        anchor (``primary_link_id``) — the batch gate from corpus finding 2,
        scoped so a batch row the sampler never saw stays out. A row with no
        token (hand-written, e.g. the pilot's) is not an extraction row and
        stays; its own reviews already govern it."""
        override = _sense(sense)
        subjects = self._serving_subjects(serving)
        where = [
            "m.direction = 'output'",
            "m.superseded_by IS NULL",
            "m.subject_ref_id = ANY(%(subjects)s)",
            "m.measurand_status IS DISTINCT FROM 'ambiguous'",
            "NOT (m.meta ? 'escalation')",
            "NOT (coalesce(m.tier = 'measured', false) AND m.primary_link_id IS NULL)",
            "m.trusted IS NOT FALSE",
            "m.value_form IN ('point', 'approximate_point')",
            "m.value_num IS NOT NULL",
        ]
        params: dict[str, Any] = {"subjects": sorted(subjects)}
        if measurand is not None:
            where.append("m.measurand_ref_id = ANY(%(cover)s)")
            params["cover"] = self._measurand_cover(measurand)
        if reference is not None:
            where.append("m.reference = %(reference)s")
            params["reference"] = reference
        groups: dict[
            tuple[
                int,
                str | None,
                str | None,
                tuple[tuple[str, str], ...],
                str | None,
                int | None,
            ],
            list[dict[str, Any]],
        ] = {}
        rows = [
            r
            for r in self._measure_rows(" AND ".join(where), params, with_review=True)
            if r["review_state"] != "rejected"
        ]
        if accepted_only:
            verdicts = self.accepted_runs(
                [
                    str(tok)
                    for r in rows
                    if (tok := (r.get("meta") or {}).get("extraction_run")) is not None
                ]
            )
            rows = [
                r
                for r in rows
                if (tok := (r.get("meta") or {}).get("extraction_run")) is None
                or (
                    r.get("primary_link_id") is not None
                    and (v := (verdicts.get(str(tok)) or {}).get(r["subject_ref_id"]))
                    is not None
                    and bool(v.get("accepted"))
                    and int(r["id"]) <= int(v["max_id"])
                )
            ]
        by_run = self._conditions_by_run([r["run_key"] for r in rows])
        required = self._required_conditions_map({r["measurand_ref_id"] for r in rows})
        for r in rows:
            part = self._group_partition(
                required.get(r["measurand_ref_id"], ()),
                by_run.get(r["run_key"], []),
            )
            currency, year = row_currency(r)
            groups.setdefault(
                (
                    r["measurand_ref_id"],
                    r["reference"],
                    r["normalization"],
                    part,
                    currency,
                    year,
                ),
                [],
            ).append(r)
        results: list[dict[str, Any]] = []
        for (mid, ref, norm, part, currency, year), members in groups.items():
            direction = override or _sense(members[0]["higher_is_better"])
            best: dict[str, Any] | None = None
            if direction == "higher":
                best = max(members, key=lambda r: (r["value_num"], -r["id"]))
            elif direction == "lower":
                best = min(members, key=lambda r: (r["value_num"], r["id"]))
            results.append(
                {
                    "measurand_ref_id": mid,
                    "measurand": members[0]["measurand"],
                    "reference": ref,
                    "normalization": norm,
                    "currency": currency,
                    "price_year": year,
                    "by": dict(part),
                    "sense": direction,
                    "n": len(members),
                    "best": best,
                }
            )
        results.sort(
            key=lambda g: (
                g["measurand"] or "",
                g["reference"] or "",
                g["normalization"] or "",
                g["currency"] or "",
                g["price_year"] or 0,
                sorted(g["by"].items()),
            )
        )
        for g in results:
            if g["best"] is not None:
                g["best"]["conditions"] = by_run.get(g["best"]["run_key"], [])
        return results

    def measures_census(self, measurand: int) -> list[dict[str, Any]]:
        """Live rows of ``measurand`` and its ``specialises`` descendants,
        counted by ``(tier, reference, normalization)`` as
        ``{tier, reference, normalization, n}`` (a NULL stays None), largest
        first. Every direction counts: a potential is usually written as an
        input, and "how many state a reference" is the question."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT tier, reference, normalization, count(*) FROM measures "
                "WHERE superseded_by IS NULL AND measurand_ref_id = ANY(%s) "
                "GROUP BY tier, reference, normalization "
                "ORDER BY count(*) DESC, tier NULLS LAST, reference NULLS FIRST, "
                "         normalization NULLS FIRST",
                (self._measurand_cover(measurand),),
            ).fetchall()
        return [
            {"tier": r[0], "reference": r[1], "normalization": r[2], "n": int(r[3])}
            for r in rows
        ]

    def live_measure_count(self, measurand_ref_id: int) -> int:
        """Live (unsuperseded) rows keyed on this one measurand — the
        pin count behind the taxon unit guard (ontology-v2 slice 0b): a
        ``canonical_unit``/``display_unit`` edit is refused while rows
        would be silently re-interpreted by it."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM measures WHERE measurand_ref_id = %s "
                "AND superseded_by IS NULL",
                (measurand_ref_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def search_measures(
        self,
        measurand: int | None = None,
        *,
        min_si: float | None = None,
        max_si: float | None = None,
        conditions: Sequence[ConditionFilter] = (),
        text: str | None = None,
        include_all: bool = False,
        scan_cap: int = 5000,
        bound_currency: tuple[str, int | None] | None = None,
    ) -> MeasureSearch:
        """Output rows matching all of: the measurand and its descendants; a
        numeric range by interval overlap (``min_si``/``max_si`` are SI
        bounds; a point is a degenerate interval, ``<x`` reaches down from x,
        ``>x`` up); every condition filter against the run's input rows;
        every word of ``text`` in the subject label. Without ``include_all``
        superseded, ambiguous, flagged and anchor-lost ``measured`` rows are
        left out (only ``measured`` drops its claim with the link — see
        :meth:`best_measure`; other tiers stay, marked ``anchor_lost``).
        Scans at most ``scan_cap`` candidate rows (``truncated`` says so).

        ``bound_currency`` is the ``(currency, price_year)`` group the bounds
        are stated in: an explicit ``unit=`` token, exact to the year (a
        NULL year is its own group), or the taxon's currency paired with
        :data:`ANY_YEAR` — a range with no ``unit=`` compares the taxon's
        currency across every price year. The range itself still runs in
        SQL; until conversion lands only the group filter runs in Python: a
        row outside the bound group never mixes, and ``other_currency``
        counts them for the search footer (gr510555 S1)."""
        where = ["m.direction = 'output'"]
        params: dict[str, Any] = {"cap": scan_cap + 1}
        if not include_all:
            where += [
                "m.superseded_by IS NULL",
                "m.measurand_status IS DISTINCT FROM 'ambiguous'",
                "NOT (m.meta ? 'escalation')",
                "NOT (coalesce(m.tier = 'measured', false) AND m.primary_link_id IS NULL)",
            ]
        if measurand is not None:
            where.append("m.measurand_ref_id = ANY(%(cover)s)")
            params["cover"] = self._measurand_cover(measurand)
        if include_all and (min_si is not None or max_si is not None):
            # a re-based legacy row's stored numbers are in the old unit (see
            # _si_normalise); the SQL range test would compare them as SI. Its
            # live successor carries the same number in SI and is searched.
            where.append("sx.id IS NULL")
        if min_si is not None:
            where.append(f"{_HI_SQL} >= %(min)s")
            params["min"] = min_si - abs(min_si) * _REL_TOL
        if max_si is not None:
            where.append(f"{_LO_SQL} <= %(max)s")
            params["max"] = max_si + abs(max_si) * _REL_TOL
        from_ = _MEASURE_FROM
        for i, word in enumerate((text or "").split()):
            esc = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            # gr489581: a free-text query ("ammonia faradaic efficiency")
            # names the measurand or the anchored passage, not only the
            # subject label — each word may match any of them.
            where.append(
                f"(m.subject ILIKE %(w{i})s OR m.literal ILIKE %(w{i})s "
                f"OR t.meta ->> 'name' ILIKE %(w{i})s "
                f"OR at_.text ILIKE %(w{i})s)"
            )
            params[f"w{i}"] = f"%{esc}%"
        if (text or "").strip():
            # Anchor text = the chunk the measure was quantified by
            # (primary_link_id → links.src_chunk_id → chunks).
            from_ += (
                " LEFT JOIN links al_ ON al_.link_id = m.primary_link_id"
                " LEFT JOIN chunks at_ ON at_.chunk_id = al_.src_chunk_id"
            )
        sql = (
            f"SELECT {_MEASURE_COLS} {from_} WHERE {' AND '.join(where)} "
            "ORDER BY t.ref_id, m.id LIMIT %(cap)s"
        )
        with self.pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                rows = list(cur.fetchall())
        truncated = len(rows) > scan_cap
        rows = rows[:scan_cap]
        _si_normalise(rows)
        by_run = self._conditions_by_run([r["run_key"] for r in rows])
        bound = bound_currency if min_si is not None or max_si is not None else None
        out = []
        other_currency = 0
        for r in rows:
            r["conditions"] = by_run.get(r["run_key"], [])
            if not all(_condition_holds(f, r["conditions"]) for f in conditions):
                continue
            if bound is not None:
                currency, year = row_currency(r)
                if currency != bound[0] or (bound[1] != ANY_YEAR and year != bound[1]):
                    other_currency += 1
                    continue
            out.append(r)
        return MeasureSearch(
            rows=out, truncated=truncated, other_currency=other_currency
        )

    def measure_detail(self, measure_id: int) -> dict[str, Any]:
        """One row, any liveness, with what a review needs: its run's
        ``conditions``, the ``anchor`` (paper and chunk of the anchoring link),
        the supersession ``chain`` (oldest first) and the ledger ``reviews``
        (newest first, each ``current`` or stale)."""
        rows = self._measure_rows("m.id = %(id)s", {"id": measure_id})
        if not rows:
            raise NotFound(
                f"measure {measure_id} not found",
                next="search(kind='measure', property='<measurand>') to find one",
            )
        r = rows[0]
        r["conditions"] = self._conditions_by_run([r["run_key"]], live_only=False)[
            r["run_key"]
        ]
        r["anchor"] = None
        with self.pool.connection() as conn:
            if r["primary_link_id"] is not None:
                a = conn.execute(
                    "SELECT l.src_ref_id, l.src_chunk_id, p.kind, p.title "
                    "FROM links l JOIN refs p ON p.ref_id = l.src_ref_id "
                    "WHERE l.link_id = %s",
                    (r["primary_link_id"],),
                ).fetchone()
                if a is not None:
                    r["anchor"] = {
                        "paper_ref_id": int(a[0]),
                        "chunk_id": None if a[1] is None else int(a[1]),
                        "kind": a[2],
                        "title": a[3],
                    }
            earlier: list[dict[str, Any]] = []
            later: list[dict[str, Any]] = []
            seen = {measure_id}
            for start, step, bucket in (
                (r["supersedes"], "supersedes", earlier),
                (r["superseded_by"], "superseded_by", later),
            ):
                cursor = start
                while cursor is not None and cursor not in seen and len(bucket) < 50:
                    seen.add(cursor)
                    row = conn.execute(
                        "SELECT id, literal, supersedes, superseded_by FROM measures "
                        "WHERE id = %s",
                        (cursor,),
                    ).fetchone()
                    if row is None:
                        break
                    bucket.append(
                        {"id": int(row[0]), "literal": row[1], "live": row[3] is None}
                    )
                    cursor = row[2] if step == "supersedes" else row[3]
        r["chain"] = [
            *reversed(earlier),
            {
                "id": measure_id,
                "literal": r["literal"],
                "live": r["superseded_by"] is None,
            },
            *later,
        ]
        r["reviews"] = self.reviews_for("measure", measure_id)
        return r
