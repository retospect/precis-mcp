"""Text for a ``measures`` row, shared by ``kind='measure'`` and the quest
``view='measures'``. Pure formatting over the row dicts the measures store
returns; no DB.

A stored value is SI; this turns it into the unit a person expects
(:func:`precis.taxonomy.measure_units.display_numbers`): the caller's
``unit=``, else the measurand's ``display_unit``, else SI with an automatic
prefix.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from precis.errors import BadInput
from precis.taxonomy.measure_units import display_numbers, format_number
from precis.utils import handle_registry


def _numbers(
    row: dict[str, Any], values: Sequence[float], unit: str | None
) -> tuple[list[float], str]:
    """Display numbers for a row; an explicit ``unit`` that does not fit this
    row's measurand falls back to the row's own default rather than failing a
    whole result list."""
    try:
        return display_numbers(
            values,
            canonical_unit=row.get("canonical_unit"),
            display_unit=row.get("display_unit"),
            unit=unit,
        )
    except BadInput:
        return display_numbers(
            values,
            canonical_unit=row.get("canonical_unit"),
            display_unit=row.get("display_unit"),
        )


def _with(label: str, *parts: str) -> str:
    return " ".join([*parts, label]).strip() if label else " ".join(parts)


def value_text(row: dict[str, Any], *, unit: str | None = None) -> str:
    """The row's reading in the output unit: ``95 %``, ``1.4–2.1 Å``, ``< 5 mA/m²``,
    ``~3 V``, ``9.6 ± 1.7 mV``, a category as printed."""
    form = row.get("value_form")
    num, low, high = row.get("value_num"), row.get("value_low"), row.get("value_high")
    if row.get("value_bool") is not None and num is None:
        return "true" if row["value_bool"] else "false"
    if form == "interval" and low is not None and high is not None:
        (a, b), label = _numbers(row, [low, high], unit)
        return _with(label, f"{format_number(a)}–{format_number(b)}")
    if num is not None and form != "categorical":
        err = row.get("value_err")
        nums, label = _numbers(row, [num, num + err] if err else [num], unit)
        text = format_number(nums[0])
        if err:
            text += f" ± {format_number(abs(nums[1] - nums[0]))}"
        prefix = {"upper_bound": "< ", "lower_bound": "> ", "approximate_point": "~"}
        return _with(label, prefix.get(form or "", "") + text)
    return str(row.get("value_text") or row.get("literal") or "—")


def one_condition(c: dict[str, Any]) -> str:
    """``potential=-0.5 V vs RHE`` — the input's value in its own display unit."""
    text = value_text(c)
    if c.get("reference"):
        text += f" vs {c['reference']}"
    return f"{c.get('name') or c.get('measurand') or '?'}={text}"


def conditions_text(conditions: Sequence[dict[str, Any]]) -> str:
    return ", ".join(one_condition(c) for c in conditions) or "no conditions"


def ref_handle(kind: str | None, ref_id: int | None) -> str | None:
    if ref_id is None:
        return None
    return (
        handle_registry.try_format(kind, ref_id) if kind else None
    ) or f"ref {ref_id}"


def paper_handle(row: dict[str, Any]) -> str:
    """The source paper's handle (``pa5``), ``—`` for a row with no anchor."""
    return ref_handle(row.get("paper_kind"), row.get("source_ref_id")) or "—"


def subject_label(row: dict[str, Any]) -> str:
    """``Cu NWA`` (the paper-local label), else the subject ref's title."""
    return str(row.get("subject") or row.get("subject_title") or "?")


def flags(row: dict[str, Any]) -> list[str]:
    """Why a row would be left out of a ranking: superseded, ambiguous,
    escalated, anchor-lost."""
    out = []
    if row.get("superseded_by") is not None:
        out.append(f"superseded by {row['superseded_by']}")
    if row.get("measurand_status") == "ambiguous":
        out.append("ambiguous measurand")
    if (row.get("meta") or {}).get("escalation"):
        rules = [str(e.get("rule")) for e in row["meta"]["escalation"]]
        out.append("escalated: " + ", ".join(rules))
    if row.get("anchor_lost"):
        out.append("anchor lost")
    return out


def result_line(row: dict[str, Any], *, unit: str | None = None) -> str:
    """One line per row: id, subject, measurand and value, conditions, tier,
    paper handle (plus any flag, shown only for ``status='all'`` rows)."""
    mx = handle_registry.format_handle("measure", int(row["id"]))
    parts = [
        mx,
        subject_label(row),
        f"{row.get('measurand') or '?'}: {value_text(row, unit=unit)}"
        + (f" vs {row['reference']}" if row.get("reference") else "")
        + (f" [{row['normalization']}]" if row.get("normalization") else ""),
        conditions_text(row.get("conditions") or []),
        str(row.get("tier") or "tier unknown"),
        paper_handle(row),
    ]
    line = " | ".join(parts)
    if flagged := flags(row):
        line += f"  ({'; '.join(flagged)})"
    return line
