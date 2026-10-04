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
``{"rule": ..., ...}``); ``best_measure`` (Build B) excludes flagged rows. The
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
(values are stored normalised to one unit per measurand). Unitless numeric rows
(no ``reported_unit``) are accepted on a unit-less taxon.

Reviews of a measure go through the shared ledger
(``record_target_review('measure', id, ...)``); the sha covers the frozen
fields only (``precis_measure_sha``).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from precis.errors import BadInput, NotFound
from precis.store._taxon_ops import _EDGES
from precis.taxonomy.measure_units import (
    NeedsMolarMass,
    basis_normalization,
    make_converter,
    molar_mass,
    parse_literal,
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
    given. ``reported_unit`` None means the number is already in the
    measurand's canonical unit (a table-recipe row)."""

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


@dataclass(frozen=True, slots=True)
class MeasureRun:
    """What :meth:`MeasuresMixin.insert_measure` wrote."""

    run_key: str
    output_id: int
    input_ids: tuple[int, ...]


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
        refusals and the flags.

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
        if reported_unit and canonical and has_numbers:
            try:
                conv = make_converter(reported_unit, canonical)
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
                err = None if err is None else abs(err * conv.scale)
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
            " supersedes, source_ref_id, actor, model, meta) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
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
                anchor.paper_ref_id if anchor is not None else None,
                actor,
                model,
                Jsonb(meta),
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
        paper was deleted; the foreign key set it NULL). ``best_measure``
        (Build B) excludes those rows; re-anchoring is a follow-up. The ids
        in ``meta.extra_anchors`` are not foreign keys and may dangle the same
        way."""
        sql = (
            "SELECT m.*, t.meta ->> 'name' AS measurand, "
            "       t.meta ->> 'canonical_unit' AS canonical_unit, "
            "       (m.tier = 'measured' AND m.primary_link_id IS NULL) AS anchor_lost "
            "FROM measures m JOIN refs t ON t.ref_id = m.measurand_ref_id "
            "WHERE m.subject_ref_id = %s "
            + ("" if include_superseded else "AND m.superseded_by IS NULL ")
            + "ORDER BY m.created_at, m.run_key, (m.direction <> 'output'), m.id"
        )
        with self.pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, (subject_ref_id,))
                return list(cur.fetchall())
