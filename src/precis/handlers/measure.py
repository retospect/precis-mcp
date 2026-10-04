"""MeasureHandler — read and search the ``measures`` table, one sourced number
per row (``docs/backlog/measures-substrate.md``, pilot Build B).

A measure is a row, not a ref, so this is a handler-searched kind like ``tag``
and ``skill``: addressed by ``measures.id`` (``12`` or the handle ``mx12``),
read-only here (rows are written by ``Store.insert_measure``).

* ``get(kind='measure', id=12)`` — the row with everything a review needs: the
  literal and its value in the display unit (SI beside it), the measurand and
  its path, the subject, the run's input conditions, tier / attribution /
  statuses, the anchor, the ledger reviews (each current or stale) and the
  supersession chain.
* ``search(kind='measure', property=<taxon>, min=, max=, unit=, q=, status=)``
  — range-and-conditions search. ``property`` is a taxon handle or path and
  covers its ``specialises`` descendants; ``min``/``max`` are numbers in
  ``unit`` (else the taxon's ``display_unit``, else SI) converted to SI before
  they meet the stored values by interval overlap; ``q`` holds condition terms
  (``quant=Q4 potential<-0.5``) matched against the run's input rows, and any
  other word matches the subject label. One line per row.

A measure has no neighbourhood, so the fisheye ladder is ``Unsupported``.
"""

from __future__ import annotations

import re
import shlex
from datetime import UTC, datetime
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers import _measure_render as render
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store._measures_ops import ConditionFilter
from precis.taxonomy.measure_units import (
    format_number,
    is_unit,
    pint_unit_text,
    to_canonical,
)
from precis.utils import handle_registry

_DEFAULT_PAGE_SIZE = 20

_NUM = r"[-+−]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+−]?\d+)?"
_TERM_RE = re.compile(r"^([^\s=<>]+)(<=|>=|=|<|>)(.+)$")
_NUMBER_RE = re.compile(rf"^({_NUM})\s*(\S.*)?$")


def parse_query(q: str | None) -> tuple[list[ConditionFilter], str]:
    """Split ``q`` into condition filters and subject-label words.

    A term is ``name=value``, ``name<value``, ``name>value`` (also ``<=``,
    ``>=``); the value is a number, optionally with a unit (``-0.5V``, or the
    next word when pint reads it as a unit: ``temperature>300 K``), or text
    (``product=NH3``; quote a value with spaces: ``hardware="M2 Ultra"``).
    Every other word is part of the returned label text."""
    if not q or not q.strip():
        return [], ""
    try:
        tokens = shlex.split(q)
    except ValueError:
        tokens = q.split()
    conditions: list[ConditionFilter] = []
    words: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        m = _TERM_RE.match(tok)
        if m is None:
            words.append(tok)
            continue
        name, op, raw = m.group(1), m.group(2), m.group(3).strip()
        num = _NUMBER_RE.match(raw)
        if num is None:
            if op != "=":
                raise BadInput(
                    f"condition {tok!r}: < and > need a number, got {raw!r}",
                    next="q='potential<-0.5' (or = for a text value like product=NH3)",
                )
            conditions.append(ConditionFilter(name=name, op=op, text=raw))
            continue
        value = float(num.group(1).replace("−", "-"))
        unit = (num.group(2) or "").strip() or None
        if unit is None and i < len(tokens) and _TERM_RE.match(tokens[i]) is None:
            if is_unit(tokens[i]):
                unit, i = tokens[i], i + 1
        elif unit is not None and not is_unit(unit):
            if op != "=":
                raise BadInput(
                    f"condition {tok!r}: {unit!r} is not a unit",
                    next="q='potential<-0.5 V'",
                )
            conditions.append(ConditionFilter(name=name, op=op, text=raw))
            continue
        conditions.append(ConditionFilter(name=name, op=op, value=value, unit=unit))
    return conditions, " ".join(words)


def _utc(when: datetime | None) -> str:
    if when is None:
        return "?"
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")


class MeasureHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="measure",
        title="Measure",
        description=(
            "One sourced number per row, about any subject (a paper, a material, a "
            "model...): the printed literal, the value stored in SI and shown in "
            "the measurand's display unit, its input conditions, tier, anchor "
            "and reviews. get(kind='measure', id=12) reads one row; "
            "search(kind='measure', property='measurand/faradaic-efficiency', "
            "min=90, q='product=NH3 potential<-0.5 V', unit='%') filters by "
            "measurand, numeric range (in unit=) and the run's conditions. "
            "Read-only. See precis-measure-help."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=False,
        is_numeric=True,
        id_required=True,
        placement="stream",
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("measure: store required")
        self.store = hub.store
        self.hub = hub

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _coerce_id(raw: str | int) -> int:
        text = str(raw).strip()
        parsed = handle_registry.parse(text)
        if parsed is not None and parsed[0] == "measure" and not parsed[1]:
            return parsed[2]
        try:
            return int(text)
        except ValueError:
            raise BadInput(
                f"measure id must be an integer or an mx handle, got {raw!r}",
                next="get(kind='measure', id=12) — ids come from search(kind='measure', ...)",
            ) from None

    def _taxon(self, spec: str) -> tuple[int, dict[str, Any]]:
        """Resolve ``property=`` like a taxon address; returns ``(id, meta)``."""
        taxon_handler = self.hub.sibling("taxon")
        ref_id = taxon_handler.resolve_node(spec)
        ref = self.store.get_ref(kind="taxon", id=ref_id)
        if ref is None:
            raise NotFound(
                f"no taxon {spec!r}",
                next="search(kind='taxon', q='...') to find the measurand, then use its handle",
            )
        return ref_id, dict(ref.meta or {})

    # ── get ──────────────────────────────────────────────────────────

    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        if view is not None:
            from precis.utils.eye_render import RECALL_SUFFIX
            from precis.workers.working_set import Extent

            ladder = {e.label for e in Extent}
            if view in ladder or view.endswith(RECALL_SUFFIX):
                raise Unsupported(
                    "a measure is a row of a run, not a node: it has no neighbourhood "
                    "to fish-eye",
                    next="get(kind='measure', id=N) reads the row with its conditions "
                    "and anchor; fish-eye the paper or the quest instead",
                )
            raise BadInput(
                f"kind='measure' has no view {view!r}",
                next="get(kind='measure', id=N) — the one view",
            )
        if id is None or (isinstance(id, str) and not id.strip()):
            raise BadInput(
                "get(kind='measure') needs id=<measures.id>",
                next="search(kind='measure', property='<measurand>') lists rows with their ids",
            )
        return Response(
            body=self._render_row(self.store.measure_detail(self._coerce_id(id)))
        )

    def _render_row(self, r: dict[str, Any]) -> str:
        mx = handle_registry.format_handle("measure", int(r["id"]))
        out = [f"# {mx} — {render.subject_label(r)}: {r['measurand']}"]
        reported = (
            f" (reported unit: {r['reported_unit']})" if r["reported_unit"] else ""
        )
        out.append(f"literal: {r['literal']}{reported}")
        out.append(f"value: {render.value_text(r)}   SI: {self._si_text(r)}")
        extras = []
        if r["reference"]:
            extras.append(f"reference {r['reference']}")
        if r["normalization"]:
            extras.append(
                f"normalization {r['normalization']} ({r['normalization_status'] or '—'})"
            )
        if extras:
            out.append("basis: " + ", ".join(extras))

        taxon = handle_registry.format_handle("taxon", int(r["measurand_ref_id"]))
        try:
            path = self.hub.sibling("taxon").node_path(int(r["measurand_ref_id"]))
        except Exception:  # a path is a convenience; never fail the read on it
            path = r["measurand_slug"] or ""
        out.append(f"measurand: {taxon} {r['measurand']} ({path})")
        subj = render.ref_handle(r["subject_kind"], r["subject_ref_id"])
        out.append(f"subject: {subj} {r['subject_title']}")
        if r["subject"]:
            out.append(f"subject label: {r['subject']}")
        out.append(
            f"run: {r['run_key']}"
            + (f"   group: {r['subject_group']}" if r["subject_group"] else "")
            + f"   direction: {r['direction']}"
        )

        out.append("conditions:")
        if r["conditions"]:
            for c in r["conditions"]:
                role = f", {c['role']}" if c["role"] else ""
                live = "" if c["superseded_by"] is None else ", superseded"
                out.append(
                    f"  - {render.one_condition(c)}   [{c['literal']}{role}{live}] "
                    f"{handle_registry.format_handle('measure', int(c['id']))}"
                )
        else:
            out.append("  (none)")

        out.append(
            f"tier: {r['tier'] or 'unknown'} · attribution: "
            f"{r['source_attribution'] or '—'} · measurand status: "
            f"{r['measurand_status'] or '—'} · extraction: {r['extraction_status']}"
            f" · trusted: {'unassessed' if r['trusted'] is None else r['trusted']}"
        )
        if flagged := render.flags(r):
            out.append("flags: " + "; ".join(flagged))
        for entry in (r["meta"] or {}).get("escalation") or []:
            out.append(f"  escalation: {entry}")

        out.append(self._anchor_line(r))
        out.append(
            f"written: {_utc(r['created_at'])} by {r['actor']}"
            + (f" ({r['model']})" if r["model"] else "")
        )

        out.append("reviews:")
        if r["reviews"]:
            for rv in r["reviews"]:
                who = rv.actor + (f" / {rv.model}" if rv.model else "")
                state = "current" if rv.current else "STALE (a fixed field changed)"
                note = f" — {rv.note}" if rv.note else ""
                out.append(
                    f"  - {rv.verdict} by {who}, version {rv.version}, {_utc(rv.at)} {state}{note}"
                )
        else:
            out.append("  (none)")

        if len(r["chain"]) > 1:
            parts = [
                (f"[{e['id']}]" if e["id"] == r["id"] else str(e["id"]))
                + ("" if e["live"] else " (superseded)")
                for e in r["chain"]
            ]
            out.append("supersession: " + " -> ".join(parts) + "  (oldest first)")
        else:
            out.append("supersession: none")
        return "\n".join(out)

    @staticmethod
    def _si_text(r: dict[str, Any]) -> str:
        nums = [
            r[k] for k in ("value_num", "value_low", "value_high") if r[k] is not None
        ]
        if not nums:
            return "—"
        unit = (r["canonical_unit"] or "").strip()
        unit = "" if pint_unit_text(unit)[0] == "1" else unit
        return " ".join(f"{n:.6g}" for n in nums) + (f" {unit}" if unit else "")

    def _anchor_line(self, r: dict[str, Any]) -> str:
        a = r["anchor"]
        if a is None:
            if r["tier"] == "measured":
                return "anchor: LOST (the anchoring chunk or paper was deleted)"
            return "anchor: none"
        paper = handle_registry.format_handle(a["kind"], a["paper_ref_id"])
        chunk = (
            handle_registry.try_format(a["kind"], a["chunk_id"], chunk=True)
            or f"chunk {a['chunk_id']}"
        )
        span = f" {r['anchor_scheme']}: {r['span']}" if r["anchor_scheme"] else ""
        title = f" {a['title']}" if a["title"] else ""
        return f"anchor: {paper}{title} · {chunk} ·{span}"

    # ── search ───────────────────────────────────────────────────────

    def search(
        self,
        *,
        q: str | None = None,
        property: str | None = None,
        min: float | None = None,
        max: float | None = None,
        unit: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
        **_kw: Any,
    ) -> Response:
        if status not in (None, "all", "live"):
            raise BadInput(
                f"status {status!r} is not 'all' or 'live'",
                next="status='all' includes superseded, escalated, ambiguous and anchor-lost rows",
            )
        taxon_id: int | None = None
        taxon_meta: dict[str, Any] = {}
        if property is not None and str(property).strip():
            taxon_id, taxon_meta = self._taxon(str(property).strip())
        if (min is not None or max is not None) and taxon_id is None:
            raise BadInput(
                "min=/max= need property=: a bound is a number in the measurand's unit",
                next="search(kind='measure', property='measurand/bond-length', min=1.4, unit='Å')",
            )
        if min is not None and max is not None and min > max:
            raise BadInput(f"min={min} is above max={max}")
        conditions, text = parse_query(q)
        if taxon_id is None and not conditions and not text:
            raise BadInput(
                "search(kind='measure') needs property=, or q= with condition terms "
                "or subject words",
                next="search(kind='measure', property='measurand/faradaic-efficiency', "
                "min=90, unit='%', q='product=NH3')",
            )
        min_si = self._to_si(min, unit, taxon_meta)
        max_si = self._to_si(max, unit, taxon_meta)
        found = self.store.search_measures(
            taxon_id,
            min_si=min_si,
            max_si=max_si,
            conditions=conditions,
            text=text or None,
            include_all=status == "all",
        )
        rows = found.rows
        size = page_size if page_size > 0 else _DEFAULT_PAGE_SIZE
        page = page if page >= 1 else 1
        shown = rows[(page - 1) * size : page * size]
        head = self._header(len(rows), taxon_meta, min, max, unit, q, status)
        if not rows:
            return Response(body=head)
        lines = [head, *(render.result_line(r, unit=unit) for r in shown)]
        if len(rows) > page * size:
            lines.append(f"… {len(rows) - page * size} more: page={page + 1}")
        if found.truncated:
            lines.append(
                "(candidate scan stopped at its cap: narrow with property=, min=/max= or q=)"
            )
        return Response(body="\n".join(lines))

    @staticmethod
    def _to_si(
        bound: float | None, unit: str | None, taxon_meta: dict[str, Any]
    ) -> float | None:
        """A bound as SI: in ``unit``, else the taxon's display unit, else SI."""
        if bound is None:
            return None
        use = (unit or "").strip() or taxon_meta.get("display_unit")
        if not use:
            return float(bound)
        return to_canonical(float(bound), use, taxon_meta.get("canonical_unit"))

    @staticmethod
    def _header(
        n: int,
        taxon_meta: dict[str, Any],
        lo: float | None,
        hi: float | None,
        unit: str | None,
        q: str | None,
        status: str | None,
    ) -> str:
        bits = []
        if taxon_meta:
            bits.append(str(taxon_meta.get("name") or "measurand"))
        if lo is not None or hi is not None:
            use = unit or taxon_meta.get("display_unit") or "SI"
            lo_s = "" if lo is None else format_number(lo)
            hi_s = "" if hi is None else format_number(hi)
            bits.append(f"{lo_s}..{hi_s} {use}")
        if q and q.strip():
            bits.append(f"q={q.strip()!r}")
        if status == "all":
            bits.append("including superseded/escalated/anchor-lost")
        what = ", ".join(bits) or "all"
        if n == 0:
            return f"no measures match ({what})"
        return f"# {n} measure(s) ({what})"
