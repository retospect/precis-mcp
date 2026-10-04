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
``{"rule": ..., ...}``); ``best_measure`` excludes flagged rows. The
required names come from the output measurand taxon's ``meta.required_conditions``
and those of every ancestor along ``specialises`` (union); an input row
satisfies a name when it has ``role='context'`` and its ``meta.condition``
label, its taxon's slug or its taxon's name matches. A condition with no
taxon of its own (``product``) still needs *some* taxon (``measurand_ref_id``
is NOT NULL): the caller mints a ``reaction product`` taxon and labels the row
``meta={'condition': 'product'}``; its formula goes in ``value_text``.

**Refusals.** ``tier='measured'`` without an anchor whose chunk is set;
``tier='measured'`` with ``source_attribution='cited_work'``; a unit with no
dimension match to the measurand's ``canonical_unit`` (naming both); a
number with a ``reported_unit`` on a taxon that has no ``canonical_unit``
(values are stored normalised to one unit per measurand); and (migration 0188,
Store SI) **a number with no ``reported_unit`` on a taxon that has a
``canonical_unit``**, naming the canonical and the display unit: "95" is
ambiguous between 95 % and 0.95, and a legacy "5" between 5 mm and 5 m, so
neither is guessed. A caller whose number really is in the canonical unit
(a table-recipe row) passes that unit as ``reported_unit``. Unitless numeric
rows are accepted only on a unit-less taxon.

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

import math
import re
import uuid
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from precis.errors import BadInput, NotFound
from precis.store._taxon_ops import _EDGES
from precis.taxonomy.measure_units import (
    Conversion,
    NeedsMolarMass,
    basis_normalization,
    make_converter,
    molar_mass,
    parse_literal,
    si_form,
    to_canonical,
)
from precis.taxonomy.nodes import slugify

TIERS = ("measured", "computed", "derived", "asserted")
ATTRIBUTIONS = ("own_work", "cited_work", "not_established")
MEASURAND_STATUSES = ("explicit", "interpreted", "ambiguous")
NORMALIZATION_STATUSES = ("explicit", "inferred", "unresolved")
ROLES = ("context", "preparation", "model")
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


@dataclass(frozen=True, slots=True)
class MeasureSearch:
    """What :meth:`MeasuresMixin.search_measures` found. ``truncated`` means
    the candidate scan hit its cap before the end."""

    rows: list[dict[str, Any]]
    truncated: bool = False


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

_MEASURE_COLS = (
    "m.*, t.meta ->> 'name' AS measurand, t.meta ->> 'slug' AS measurand_slug, "
    "t.meta -> 'aliases' AS measurand_aliases, "
    "t.meta ->> 'canonical_unit' AS canonical_unit, "
    "t.meta ->> 'display_unit' AS display_unit, "
    "t.meta -> 'higher_is_better' AS higher_is_better, "
    "s.title AS subject_title, s.kind AS subject_kind, p.kind AS paper_kind, "
    "(coalesce(m.tier = 'measured', false) AND m.primary_link_id IS NULL) AS anchor_lost, "
    "sx.meta -> 'conversion' AS rebase"
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
    coherent SI, or has no SI form (USD, HV, pH), writes no row. Concurrent mints
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
    if form == "upper_bound" and num is not None:
        return (float("-inf"), num)
    if form == "lower_bound" and num is not None:
        return (num, float("inf"))
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


def _check_enum(name: str, value: str | None, allowed: Sequence[str]) -> None:
    if value is not None and value not in allowed:
        raise BadInput(f"{name} {value!r} is not one of {', '.join(allowed)}")


def _squash(text: str) -> str:
    """Whitespace-collapsed, minus-folded, case-folded form for the
    literal-in-span test."""
    t = text.replace("−", "-").replace(" ", " ").replace(" ", " ")
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
        """The exact converter of a legacy taxon's ``measure_unit_compat`` row
        when ``reported_unit`` is the unit the legacy registry kept and the
        taxon stores the row's SI unit; None otherwise (pint decides). The same
        factor the compatibility views use, so a number written through either
        door reads back the same."""
        src = taxon.get("legacy_source")
        if not isinstance(src, dict):
            return None
        row = c.execute(
            "SELECT legacy_unit, si_unit, factor, si_offset FROM measure_unit_compat "
            "WHERE legacy_table = %s AND legacy_key = %s",
            (src.get("table"), src.get("key")),
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
            form = spec.value_form
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
        escalation: list[dict[str, Any]] = list(flags)
        normalization = spec.normalization
        normalization_status = spec.normalization_status
        reported_unit = spec.reported_unit
        canonical = taxon.get("canonical_unit")
        has_numbers = any(v is not None for v in (num, low, high))
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
        if reported_unit and canonical and has_numbers:
            try:
                conv = self._compat_conversion(
                    c, taxon, reported_unit, canonical
                ) or make_converter(reported_unit, canonical)
            except NeedsMolarMass as need:
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
                    conv = None
                else:
                    conv = make_converter(reported_unit, canonical, molar_mass_g_mol=mm)
                    meta["conversion"] = {
                        "from": reported_unit,
                        "to": canonical,
                        "product": product,
                        "molar_mass_g_mol": mm,
                    }
            if conv is not None:
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
        elif reported_unit and has_numbers and canonical is None:
            raise BadInput(
                f"measure of {taxon.get('name') or 'this measurand'!r} reports a "
                f"number in {reported_unit!r}, but values are stored normalised to "
                "one unit per measurand and this taxon has none",
                next="set the taxon's canonical_unit first (meta canonical_unit), "
                "or write the number without a reported_unit if it is unitless",
            )
        if escalation:
            meta["escalation"] = escalation

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

        row = c.execute(
            "INSERT INTO measures "
            "(subject_ref_id, measurand_ref_id, literal, reported_unit, "
            " value_num, value_low, value_high, value_text, value_bool, "
            " value_err, value_form, reference, tier, extraction_status, "
            " normalization, normalization_status, source_attribution, "
            " measurand_status, direction, role, run_key, subject, "
            " subject_group, derived_from, primary_link_id, anchor_scheme, span, "
            " supersedes, source_ref_id, actor, model, meta, conditions, maturity, "
            " method, source_chunk, source_url, as_of, notes) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "        coalesce(%s, '{}'::jsonb), coalesce(%s, 'lab'), %s, %s, %s, %s, %s) "
            "RETURNING id",
            (
                spec.subject_ref_id,
                spec.measurand_ref_id,
                spec.literal,
                reported_unit,
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
                Jsonb(spec.conditions) if spec.conditions is not None else None,
                spec.maturity,
                spec.method,
                spec.source_chunk,
                spec.source_url,
                spec.as_of,
                spec.notes,
            ),
        ).fetchone()
        assert row is not None
        new_id = int(row[0])
        if spec.supersedes is not None:
            c.execute(
                "UPDATE measures SET superseded_by = %s, superseded_at = now() "
                "WHERE id = %s AND superseded_by IS NULL",
                (new_id, spec.supersedes),
            )
        return new_id

    # -- read --------------------------------------------------------------

    def measures_for(
        self, subject_ref_id: int, *, include_superseded: bool = False
    ) -> list[dict[str, Any]]:
        """Every measure of one subject ref (live rows unless
        ``include_superseded``), grouped by run, outputs before inputs,
        oldest run first. Each row carries the measurand's name
        (``measurand``) and canonical unit, and ``anchor_lost``: a
        ``tier='measured'`` row whose anchoring link is gone (the chunk or
        paper was deleted; the foreign key set it NULL). Numbers are stored
        SI (the taxon's ``canonical_unit``); the row also carries the taxon's
        ``display_unit``, its ``legacy_source`` and, for a legacy taxon, the
        ``measure_unit_compat`` row (``legacy_unit``, ``legacy_factor``,
        ``legacy_offset``) so a caller can convert at the edge. ``best_measure``
        excludes those rows; re-anchoring is a follow-up. The ids
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
            "       (m.tier = 'measured' AND m.primary_link_id IS NULL) AS anchor_lost "
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
        (an interval, a bound, a category).
        ``reference`` keeps only rows stated against that reference; a
        conversion between references is not built, so a SHE row never stands
        in for an RHE one.

        Rows group by ``(measurand, reference, normalization)`` and are never
        compared across groups: each group answers for itself. ``sense``
        (``higher``/``max`` or ``lower``/``min``) overrides the taxon's
        ``higher_is_better``; a group with neither has ``best=None`` and says
        how many rows it holds. Each result is ``{measurand_ref_id, measurand,
        reference, normalization, sense, n, best}``, ``best`` a row dict with
        its run's ``conditions`` and ``review_state`` (newest current verdict,
        else ``unreviewed``); ordered by measurand name, reference,
        normalization."""
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
        groups: dict[tuple[int, str | None, str | None], list[dict[str, Any]]] = {}
        for r in self._measure_rows(" AND ".join(where), params, with_review=True):
            if r["review_state"] == "rejected":
                continue
            key = (r["measurand_ref_id"], r["reference"], r["normalization"])
            groups.setdefault(key, []).append(r)
        results: list[dict[str, Any]] = []
        for (mid, ref, norm), members in groups.items():
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
            )
        )
        conds = self._conditions_by_run(
            [g["best"]["run_key"] for g in results if g["best"] is not None]
        )
        for g in results:
            if g["best"] is not None:
                g["best"]["conditions"] = conds.get(g["best"]["run_key"], [])
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
    ) -> MeasureSearch:
        """Output rows matching all of: the measurand and its descendants; a
        numeric range by interval overlap (``min_si``/``max_si`` are SI
        bounds; a point is a degenerate interval, ``<x`` reaches down from x,
        ``>x`` up); every condition filter against the run's input rows;
        every word of ``text`` in the subject label. Without ``include_all``
        superseded, ambiguous, flagged and anchor-lost rows are left out.
        Scans at most ``scan_cap`` candidate rows (``truncated`` says so)."""
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
        for i, word in enumerate((text or "").split()):
            esc = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append(f"m.subject ILIKE %(w{i})s")
            params[f"w{i}"] = f"%{esc}%"
        sql = (
            f"SELECT {_MEASURE_COLS} {_MEASURE_FROM} WHERE {' AND '.join(where)} "
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
        out = []
        for r in rows:
            r["conditions"] = by_run.get(r["run_key"], [])
            if all(_condition_holds(f, r["conditions"]) for f in conditions):
                out.append(r)
        return MeasureSearch(rows=out, truncated=truncated)

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
