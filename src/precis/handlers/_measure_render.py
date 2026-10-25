"""Text for a ``measures`` row, shared by ``kind='measure'`` and the quest
``view='measures'``. Pure formatting over the row dicts the measures store
returns; no DB.

A stored value is SI; this turns it into the unit a person expects
(:func:`precis.taxonomy.measure_units.display_numbers`): the caller's
``unit=``, else the measurand's ``display_unit``, else SI with an automatic
prefix. A currency row is the exception: its stored number is already in
the reported currency, so only the remainder converts and the label carries
that currency and price year (``1000 EUR(2015)/t``; gr510555 S1).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from precis.errors import BadInput
from precis.taxonomy.measure_units import (
    display_numbers,
    format_number,
    make_converter,
    pint_unit_text,
)
from precis.taxonomy.money import row_currency, split_currency
from precis.utils import handle_registry


def _money_label(currency: str, price_year: int | None, remainder: str) -> str:
    """``EUR(2015)/t`` — the currency, its price year when known, the unit's
    non-money remainder."""
    text = currency + (f"({price_year})" if price_year is not None else "")
    if remainder:
        text += remainder if remainder.startswith("/") else f" {remainder}"
    return text


def _money_unit(currency: str, remainder: str) -> str:
    """``'EUR' + '/t'`` -> ``'EUR/t'`` — the unit text split_currency and
    make_converter read back."""
    if not remainder:
        return currency
    return (
        currency + remainder
        if remainder.startswith("/")
        else (f"{currency} {remainder}")
    )


def _money_numbers(
    row: dict[str, Any], values: Sequence[float], unit: str | None
) -> tuple[list[float], str] | None:
    """Numbers + label for a currency row, or None on a non-currency row.

    Slice 1 of measure-currency-units (gr510555): a stored value is in the
    *reported* currency (``measures.currency``, or the legacy token) with the
    remainder scaled to the canonical one. Currencies never convert, so the
    only thing pint scales here is the remainder — toward the requested
    ``unit``'s, else the display unit's, else the canonical remainder —
    and the label always carries the row's own currency
    (``1000 EUR(2015)/t``)."""
    currency, price_year = row_currency(row)
    if currency is None:
        return None
    try:
        canon_rem = split_currency(row.get("canonical_unit")).remainder
    except BadInput:
        canon_rem = ""
    rem = canon_rem
    target = (unit or "").strip() or (row.get("display_unit") or "").strip()
    if target:
        try:
            t = split_currency(target)
            if t.remainder:
                rem = t.remainder
        except BadInput:
            pass
    if rem != canon_rem:
        try:
            conv = make_converter(
                _money_unit(currency, canon_rem), _money_unit(currency, rem)
            )
        except BadInput:
            rem = canon_rem  # an unconvertible target keeps the stored remainder
        else:
            return [conv.value(v) for v in values], _money_label(
                currency, price_year, rem
            )
    return list(values), _money_label(currency, price_year, rem)


def stored_unit(row: dict[str, Any]) -> str:
    """The unit the stored numbers are in: the canonical unit for an SI row
    (``''`` when dimensionless), the row's currency + canonical remainder
    for a currency row (``EUR(2015)/t``)."""
    unit = (row.get("canonical_unit") or "").strip()
    currency, price_year = row_currency(row)
    if currency is not None:
        try:
            rem = split_currency(unit).remainder
        except BadInput:
            rem = ""
        return _money_label(currency, price_year, rem)
    return "" if pint_unit_text(unit)[0] == "1" else unit


def _numbers(
    row: dict[str, Any], values: Sequence[float], unit: str | None
) -> tuple[list[float], str]:
    """Display numbers for a row; an explicit ``unit`` that does not fit this
    row's measurand falls back to the row's own default rather than failing a
    whole result list."""
    if (money := _money_numbers(row, values, unit)) is not None:
        return money
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
    if num is None:
        # a bound written into low/high (the pilot's corrections) shows its
        # sign too — ">80" reads "> 80 %", not the bare number
        if form == "upper_bound":
            num = high
        elif form == "lower_bound":
            num = low
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
    escalated, anchor-lost (only a ``measured`` row is actually excluded
    over it — for other tiers the flag marks lost evidence, not a lost
    claim)."""
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
