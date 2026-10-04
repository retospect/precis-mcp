"""Write-side parsing for ``kind='measure'`` (pilot Build C,
``docs/backlog/measures-substrate.md``).

``put(kind='measure', text=<output literal>, meta={...}, items=[...])`` carries
a run as plain dicts; this module turns them into the
:class:`~precis.store._measures_ops.MeasureSpec` rows ``Store.insert_measure``
takes, naming the field of every refusal. No DB writes here: handles and
anchors are resolved (read-only) so a bad run fails before the transaction
opens.

A row dict (the output's ``meta``, or one entry of ``items``) holds:

* ``measurand`` (taxon handle, path or id; required), ``subject`` (ref handle or
  id; an input defaults to the output's);
* ``literal`` (inputs only; the output's literal is ``text=``), ``reported_unit``;
* ``subject_label``, ``subject_group``, ``value_num`` / ``value_low`` /
  ``value_high`` / ``value_err`` / ``value_text`` / ``value_bool`` /
  ``value_form``, ``reference``, ``normalization``, ``normalization_status``,
  ``tier``, ``source_attribution``, ``measurand_status``, ``role``,
  ``direction`` (inputs: ``input`` | ``covariate``), ``condition`` (an input's
  label, as ``meta.condition``), ``note`` (free text, <= 500 characters, as
  ``meta.note``; written at insert only, since ``meta`` is frozen after);
* ``anchor`` = ``{chunk, anchor_scheme, span}`` (an input inherits the
  output's), ``extra_anchors`` (a list of the same), ``supersedes`` (a measure
  id or ``mx`` handle), ``derived_from`` (a list of the same).

The output's ``meta`` also takes ``run_key`` and ``model`` (the writing model).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from precis.errors import BadInput
from precis.store import Store
from precis.store._measures_ops import MeasureAnchor, MeasureSpec
from precis.utils import handle_registry

_ROW_FIELDS = frozenset(
    {
        "measurand",
        "subject",
        "literal",
        "reported_unit",
        "subject_label",
        "subject_group",
        "value_num",
        "value_low",
        "value_high",
        "value_err",
        "value_text",
        "value_bool",
        "value_form",
        "reference",
        "normalization",
        "normalization_status",
        "tier",
        "source_attribution",
        "measurand_status",
        "role",
        "direction",
        "condition",
        "note",
        "anchor",
        "extra_anchors",
        "supersedes",
        "derived_from",
    }
)
_OUTPUT_ONLY = frozenset({"run_key", "model"})
_NUMBERS = ("value_num", "value_low", "value_high", "value_err")
_STRINGS = (
    "reported_unit",
    "subject_label",
    "subject_group",
    "value_text",
    "value_form",
    "reference",
    "normalization",
    "normalization_status",
    "tier",
    "source_attribution",
    "measurand_status",
    "role",
    "direction",
    "condition",
)
_NOTE_MAX = 500
_PUT_NEXT = (
    "put(kind='measure', text='95', meta={'measurand': 'measurand/faradaic-efficiency', "
    "'subject': 'pa12', 'reported_unit': '%', 'tier': 'measured', 'anchor': "
    "{'chunk': 'pc345', 'anchor_scheme': 'sentence', 'span': 's3'}}, "
    "items=[{'measurand': 'measurand/potential', 'literal': '-0.5', 'reported_unit': 'V'}]) "
    "— get(kind='skill', id='precis-measure-help')"
)


@dataclass(frozen=True, slots=True)
class ParsedRun:
    output: MeasureSpec
    inputs: list[MeasureSpec]
    run_key: str | None
    model: str | None


def _where(index: int | None) -> str:
    return "meta" if index is None else f"items[{index}]"


def _bad(where: str, field: str, problem: str) -> BadInput:
    return BadInput(f"{where}.{field}: {problem}", next=_PUT_NEXT)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _measure_id(raw: Any, where: str, field: str) -> int:
    """A measure id from an int, a digit string or an ``mx`` handle."""
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        parsed = handle_registry.parse(text)
        if parsed is not None and parsed[0] == "measure" and not parsed[1]:
            return parsed[2]
        if text.isdigit() and int(text) > 0:
            return int(text)
    raise _bad(where, field, f"{raw!r} is not a measure id or mx handle")


class RunParser:
    """Resolves one put's handles and builds its specs."""

    def __init__(self, store: Store, resolve_taxon: Callable[[str], int]) -> None:
        self.store = store
        self.resolve_taxon = resolve_taxon

    # -- handles ---------------------------------------------------------

    def _ref_id(self, raw: Any, where: str, field: str) -> int:
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise _bad(where, field, f"{raw!r} is not a ref handle or id")
        text = str(raw).strip()
        if text.isdigit():
            with self.store.pool.connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM refs WHERE ref_id = %s AND retired_at IS NULL",
                    (int(text),),
                ).fetchone()
            if row is None:
                raise _bad(where, field, f"no live ref {text}")
            return int(text)
        resolved = self.store.resolve_handle(text)
        if resolved is None:
            raise _bad(where, field, f"{raw!r} is not a live ref handle (like pa12)")
        return int(resolved.ref_id)

    def _anchor(self, raw: Any, where: str, field: str) -> MeasureAnchor:
        if not isinstance(raw, dict):
            raise _bad(
                where,
                field,
                "must be {'chunk': <chunk handle or id>, 'anchor_scheme': ..., 'span': ...}",
            )
        unknown = sorted(set(raw) - {"chunk", "anchor_scheme", "span"})
        if unknown:
            raise _bad(
                where,
                field,
                f"unknown key(s) {unknown}; the keys are chunk, anchor_scheme, span",
            )
        for key in ("chunk", "anchor_scheme", "span"):
            if raw.get(key) in (None, ""):
                raise _bad(where, f"{field}.{key}", "is required")
        chunk = raw["chunk"]
        if isinstance(chunk, bool) or not isinstance(chunk, (int, str)):
            raise _bad(
                where, f"{field}.chunk", f"{chunk!r} is not a chunk handle or id"
            )
        text = str(chunk).strip()
        if text.isdigit():
            chunk_id = int(text)
            with self.store.pool.connection() as conn:
                row = conn.execute(
                    "SELECT ref_id FROM chunks WHERE chunk_id = %s", (chunk_id,)
                ).fetchone()
            if row is None:
                raise _bad(where, f"{field}.chunk", f"no chunk {chunk_id}")
            paper = int(row[0])
        else:
            resolved = self.store.resolve_handle(text)
            if resolved is None or resolved.chunk_id is None:
                raise _bad(
                    where,
                    f"{field}.chunk",
                    f"{chunk!r} is not a chunk handle (like pc345)",
                )
            chunk_id, paper = int(resolved.chunk_id), int(resolved.ref_id)
        scheme = raw["anchor_scheme"]
        if not isinstance(scheme, str):
            raise _bad(where, f"{field}.anchor_scheme", "must be a string")
        return MeasureAnchor(paper, chunk_id, scheme, raw["span"])

    # -- rows ------------------------------------------------------------

    def row(
        self,
        raw: Any,
        *,
        index: int | None,
        literal: str | None,
        defaults: MeasureSpec | None = None,
    ) -> MeasureSpec:
        """One spec. ``defaults`` is the output when parsing an input: its
        subject, group and anchor are inherited."""
        where = _where(index)
        if not isinstance(raw, dict):
            raise BadInput(f"{where} must be an object", next=_PUT_NEXT)
        allowed = _ROW_FIELDS | (_OUTPUT_ONLY if index is None else frozenset({"text"}))
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise BadInput(
                f"{where}: unknown field(s) {unknown}",
                options=sorted(allowed),
                next=_PUT_NEXT,
            )
        if index is not None:
            lit = raw.get("literal", raw.get("text"))
            if "literal" in raw and "text" in raw and raw["literal"] != raw["text"]:
                raise _bad(where, "literal", "and text disagree; give one")
            literal = lit if isinstance(lit, str) else None
            if lit is not None and literal is None:
                raise _bad(where, "literal", "must be the printed string, as text")
        if literal is None or not literal.strip():
            raise BadInput(
                "a measure needs its literal: the exact printed string"
                + (" (put text=)" if index is None else f" ({where}.literal)"),
                next=_PUT_NEXT,
            )

        if raw.get("measurand") in (None, ""):
            raise _bad(where, "measurand", "is required (a taxon handle or path)")
        measurand = self.resolve_taxon(str(raw["measurand"]).strip())

        if raw.get("subject") not in (None, ""):
            subject = self._ref_id(raw["subject"], where, "subject")
        elif defaults is not None:
            subject = defaults.subject_ref_id
        else:
            raise _bad(where, "subject", "is required (the ref the number is about)")

        for name in _NUMBERS:
            if raw.get(name) is not None and not _is_number(raw[name]):
                raise _bad(where, name, f"{raw[name]!r} is not a number")
        for name in _STRINGS:
            if raw.get(name) is not None and not isinstance(raw[name], str):
                raise _bad(where, name, f"{raw[name]!r} is not a string")
        if raw.get("value_bool") is not None and not isinstance(
            raw["value_bool"], bool
        ):
            raise _bad(where, "value_bool", f"{raw['value_bool']!r} is not true/false")

        anchor = (
            self._anchor(raw["anchor"], where, "anchor")
            if raw.get("anchor") is not None
            else (defaults.anchor if defaults is not None else None)
        )
        extras_raw = raw.get("extra_anchors")
        if extras_raw is not None and not isinstance(extras_raw, list):
            raise _bad(where, "extra_anchors", "must be a list of anchors")
        extras = [
            self._anchor(a, where, f"extra_anchors[{i}]")
            for i, a in enumerate(extras_raw or [])
        ]
        derived = raw.get("derived_from")
        if derived is not None and not isinstance(derived, list):
            raise _bad(where, "derived_from", "must be a list of measure ids")
        meta: dict[str, Any] = {}
        if raw.get("condition"):
            meta["condition"] = raw["condition"].strip()
        note = raw.get("note")
        if note is not None:
            if not isinstance(note, str):
                raise _bad(where, "note", f"{note!r} is not a string")
            if len(note) > _NOTE_MAX:
                raise _bad(
                    where,
                    "note",
                    f"is {len(note)} characters; the cap is {_NOTE_MAX}",
                )
            if note.strip():
                meta["note"] = note.strip()
        group = raw.get("subject_group")
        if group is None and defaults is not None:
            group = defaults.subject_group
        return MeasureSpec(
            measurand_ref_id=measurand,
            literal=literal,
            subject_ref_id=subject,
            reported_unit=raw.get("reported_unit"),
            subject=raw.get("subject_label"),
            subject_group=group,
            value_num=raw.get("value_num"),
            value_low=raw.get("value_low"),
            value_high=raw.get("value_high"),
            value_text=raw.get("value_text"),
            value_bool=raw.get("value_bool"),
            value_err=raw.get("value_err"),
            value_form=raw.get("value_form"),
            reference=raw.get("reference"),
            tier=raw.get("tier"),
            source_attribution=raw.get("source_attribution"),
            measurand_status=raw.get("measurand_status"),
            normalization=raw.get("normalization"),
            normalization_status=raw.get("normalization_status"),
            role=raw.get("role"),
            direction=raw.get("direction"),
            anchor=anchor,
            extra_anchors=extras,
            derived_from=(
                [_measure_id(d, where, "derived_from") for d in derived]
                if derived
                else None
            ),
            supersedes=(
                _measure_id(raw["supersedes"], where, "supersedes")
                if raw.get("supersedes") is not None
                else None
            ),
            meta=meta,
        )

    def run(
        self, text: Any, meta: dict[str, Any] | None, items: list[Any] | None
    ) -> ParsedRun:
        if not isinstance(text, str) or not text.strip():
            raise BadInput(
                "put(kind='measure') needs text=<the output's printed literal>",
                next=_PUT_NEXT,
            )
        if not isinstance(meta, dict) or not meta:
            raise BadInput(
                "put(kind='measure') needs meta={measurand, subject, ...}",
                next=_PUT_NEXT,
            )
        if items is not None and not isinstance(items, list):
            raise BadInput(
                "items= must be a list of input rows, each {measurand, literal, ...}",
                next=_PUT_NEXT,
            )
        output = self.row(meta, index=None, literal=text)
        inputs = [
            self.row(item, index=i, literal=None, defaults=output)
            for i, item in enumerate(items or [])
        ]
        run_key = meta.get("run_key")
        if run_key is not None and (
            not isinstance(run_key, str) or not run_key.strip()
        ):
            raise _bad("meta", "run_key", "must be a non-empty string")
        model = meta.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise _bad("meta", "model", "must be a non-empty string")
        return ParsedRun(output, inputs, run_key.strip() if run_key else None, model)


def flag_lines(rows: dict[int, dict[str, Any]]) -> list[str]:
    """One line per flagged row (id -> ``measure_detail`` dict): an escalation
    entry, or a literal not found in its anchored span."""
    out: list[str] = []
    for mid, r in rows.items():
        mx = handle_registry.format_handle("measure", mid)
        for entry in (r.get("meta") or {}).get("escalation") or []:
            rule = entry.get("rule", "flag") if isinstance(entry, dict) else str(entry)
            detail = (
                ", ".join(f"{k}={v}" for k, v in entry.items() if k != "rule")
                if isinstance(entry, dict)
                else ""
            )
            out.append(
                f"{mx} escalation {rule}"
                + (f" ({detail})" if detail else "")
                + " — excluded from best_measure until resolved"
            )
        if r.get("extraction_status") == "anchor_mismatch":
            out.append(
                f"{mx} anchor_mismatch — the literal {r['literal']!r} was not found "
                "in the anchored span; check the chunk and span"
            )
    return out
