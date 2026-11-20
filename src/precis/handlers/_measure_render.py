"""Text for a ``measures`` row, shared by ``kind='measure'`` and the quest
``view='measures'``. Pure formatting over the row dicts the measures store
returns; no DB.

A stored value is SI; this turns it into the unit a person expects
(:func:`precis.taxonomy.measure_units.display_numbers`): the caller's
``unit=``, else the measurand's ``display_unit``, else SI with an automatic
prefix. A currency row is the exception: its stored number is already in
the reported currency, so only the remainder converts and the label carries
that currency and price year (``1000 EUR(2015)/t``; gr510555 S1).

Convert-on-read (gr510555 slice 3, ruling 2026-10-28): when the caller
passes ``money`` — the :class:`~precis.taxonomy.money.MoneyTables` the
store loads from the ``fx_rates``/``price_index`` caches — a currency value
converts to EUR at the target year (the latest year with CPI in the cache,
capped at the current UTC year — the series lags and is never extrapolated)
and renders as
``1277 EUR(2026)/t (1300 USD/t, 2019, FX: ECB annual mean 2019)``: the
converted value with its year, the original figure and the FX basis beside
it. A missing (or too thin) rate or index renders the original with a note
naming it; a rate is never guessed.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Final

from precis.computed_sources import contract_fingerprint
from precis.errors import BadInput
from precis.taxonomy.measure_units import (
    display_numbers,
    format_number,
    make_converter,
    pint_unit_text,
)
from precis.taxonomy.money import (
    CANONICAL_CURRENCY,
    MoneyRefusal,
    MoneyTables,
    convert_money,
    default_target_year,
    row_currency,
    split_currency,
)
from precis.utils import handle_registry
from precis.utils.text import markup_surface


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


def _figure_text(row: dict[str, Any], values: Sequence[float]) -> str:
    """The original figure for a converted value's bracket: mirrors
    :func:`value_text`'s composition (interval, ``±`` err, plain) over the
    unconverted display numbers."""
    if row.get("value_form") == "interval" and len(values) >= 2:
        return f"{format_number(values[0])}–{format_number(values[1])}"
    if row.get("value_err") and len(values) >= 2:
        return (
            f"{format_number(values[0])} ± {format_number(abs(values[1] - values[0]))}"
        )
    return format_number(values[0])


def _money_numbers(
    row: dict[str, Any],
    values: Sequence[float],
    unit: str | None,
    money: MoneyTables | None,
) -> tuple[list[float], str] | None:
    """Numbers + label for a currency row, or None on a non-currency row.

    Slice 1 of measure-currency-units (gr510555): a stored value is in the
    *reported* currency (``measures.currency``, or the legacy token) with the
    remainder scaled to the canonical one, so the only thing pint scales here
    is the remainder — toward the requested ``unit``'s, else the display
    unit's, else the canonical remainder.

    Slice 3 (``money`` given): the remainder-scaled value then converts to
    EUR at the target year — the latest year with CPI in the cache, capped
    at the current UTC year — and the label becomes
    ``EUR(2026)/t (1300 USD/t, 2019, FX: ECB annual mean 2019)``. A refusal
    renders the original label plus the reason."""
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
    out = list(values)
    if rem != canon_rem:
        try:
            conv = make_converter(
                _money_unit(currency, canon_rem), _money_unit(currency, rem)
            )
        except BadInput:
            rem = canon_rem  # an unconvertible target keeps the stored remainder
        else:
            out = [conv.value(v) for v in out]
    label = _money_label(currency, price_year, rem)
    if money is None:
        return out, label
    target_year, latest_cpi = default_target_year(money)
    done = convert_money(
        money,
        currency=currency,
        price_year=price_year,
        target_year=target_year,
    )
    if isinstance(done, MoneyRefusal):
        return out, f"{label} (not converted: {done.reason})"
    note = f"{_figure_text(row, out)} {_money_label(currency, None, rem)}, {price_year}"
    if done.per_eur is not None:
        note += f", FX: ECB annual mean {price_year}"
    if latest_cpi:
        note += ", latest CPI year"
    return [v * done.factor for v in out], (
        f"{_money_label(CANONICAL_CURRENCY, target_year, rem)} ({note})"
    )


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
    row: dict[str, Any],
    values: Sequence[float],
    unit: str | None,
    money: MoneyTables | None,
) -> tuple[list[float], str]:
    """Display numbers for a row; an explicit ``unit`` that does not fit this
    row's measurand falls back to the row's own default rather than failing a
    whole result list."""
    if (money_out := _money_numbers(row, values, unit, money)) is not None:
        return money_out
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


#: A bound's strictness lives only in the printed literal — ``value_form``
#: folds ``<``, ``<=`` and ``≤`` (and the ``>`` family) into one
#: ``upper_bound``/``lower_bound``. The rendered sign is read back from the
#: literal: a strict marker or strict phrase prints ``<``/``>``; an
#: inclusive marker or phrase (``≤``, ``<=``, 'at most', 'by') prints
#: ``≤``/``≥``. A literal with no evidence keeps the long-standing strict
#: rendering — flipping an evidence-less bound the other way would change
#: its endpoint just as reading 'by 350 K' as ``< 350 K`` does (gr716878).
_BOUND_STRICT_UPPER_RE: Final[re.Pattern[str]] = re.compile(
    r"<(?![=])|\bless than\b|\bfewer than\b|\bunder\b|\bbelow\b", re.IGNORECASE
)
_BOUND_STRICT_LOWER_RE: Final[re.Pattern[str]] = re.compile(
    r">(?![=])|\bmore than\b|\bgreater than\b|\babove\b|\bover\b|"
    r"\bexceed\w*|\bsurpass\w*",
    re.IGNORECASE,
)
_BOUND_INCLUSIVE_UPPER_RE: Final[re.Pattern[str]] = re.compile(
    r"≤|<=|at most|no more than|not exceed\w*|never exceed\w*|up to|"
    r"no greater than|\bwithin\b|maximum of|at maximum|\bby\b",
    re.IGNORECASE,
)
_BOUND_INCLUSIVE_LOWER_RE: Final[re.Pattern[str]] = re.compile(
    r"≥|>=|at least|no less than|no fewer than|minimum of|at minimum",
    re.IGNORECASE,
)


def bound_sign(row: dict[str, Any]) -> str:
    """The sign a bound row's value opens with: ``'< '``/``'> '`` when the
    stored literal prints a strict reading, ``'≤ '``/``'≥ '`` when it prints
    an inclusive one; ``'~'`` for an approximate point, '' otherwise.

    The literal decides because the store keeps no inclusivity flag:
    ``value_form='upper_bound'`` covers ``<5``, ``≤5`` and a phrase-cued
    'at most 5' alike, so the sign is recovered from the only evidence
    kept — the literal itself. An evidence-less literal keeps the strict
    default (gr716878: 'by 350 K' is a ceiling that includes 350, rendered
    ``≤ 350 K``, not ``< 350 K``)."""
    form = row.get("value_form")
    if form not in ("upper_bound", "lower_bound"):
        return "~" if form == "approximate_point" else ""
    lit = markup_surface(str(row.get("literal") or ""))
    if form == "upper_bound":
        if _BOUND_INCLUSIVE_UPPER_RE.search(lit) and not _BOUND_STRICT_UPPER_RE.search(
            lit
        ):
            return "≤ "
        return "< "
    if _BOUND_INCLUSIVE_LOWER_RE.search(lit) and not _BOUND_STRICT_LOWER_RE.search(lit):
        return "≥ "
    return "> "


def value_text(
    row: dict[str, Any], *, unit: str | None = None, money: MoneyTables | None = None
) -> str:
    """The row's reading in the output unit: ``95 %``, ``1.4–2.1 Å``, ``< 5 mA/m²``,
    ``~3 V``, ``9.6 ± 1.7 mV``, a category as printed. A bound shows ``≤``/``≥``
    when the stored literal prints an inclusive reading (see :func:`bound_sign`)."""
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
        (a, b), label = _numbers(row, [low, high], unit, money)
        return _with(label, f"{format_number(a)}–{format_number(b)}")
    if num is not None and form != "categorical":
        err = row.get("value_err")
        nums, label = _numbers(row, [num, num + err] if err else [num], unit, money)
        text = format_number(nums[0])
        if err:
            text += f" ± {format_number(abs(nums[1] - nums[0]))}"
        return _with(label, bound_sign(row) + text)
    return str(row.get("value_text") or row.get("literal") or "—")


def one_condition(c: dict[str, Any], *, money: MoneyTables | None = None) -> str:
    """``potential=-0.5 V vs RHE`` — the input's value in its own display unit."""
    text = value_text(c, money=money)
    if c.get("reference"):
        text += f" vs {c['reference']}"
    return f"{c.get('name') or c.get('measurand') or '?'}={text}"


def conditions_text(
    conditions: Sequence[dict[str, Any]], *, money: MoneyTables | None = None
) -> str:
    return (
        ", ".join(one_condition(c, money=money) for c in conditions) or "no conditions"
    )


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


def result_line(
    row: dict[str, Any], *, unit: str | None = None, money: MoneyTables | None = None
) -> str:
    """One line per row: id, subject, measurand and value, conditions, tier,
    paper handle, plus any flags."""
    mx = handle_registry.format_handle("measure", int(row["id"]))
    parts = [
        mx,
        subject_label(row),
        f"{row.get('measurand') or '?'}: {value_text(row, unit=unit, money=money)}"
        + (f" vs {row['reference']}" if row.get("reference") else "")
        + (f" [{row['normalization']}]" if row.get("normalization") else ""),
        conditions_text(row.get("conditions") or [], money=money),
        str(row.get("tier") or "tier unknown")
        + (f" · {row['origin']}" if row.get("origin") else ""),
        paper_handle(row),
    ]
    line = " | ".join(parts)
    if flagged := flags(row):
        line += f"  ({'; '.join(flagged)})"
    return line


def computed_source_line(source: dict[str, Any]) -> str:
    """One line for a row's ``meta['computed_source']`` contract: the
    producer, the pinned output + selector, and the contract's content
    fingerprint, so a reviewer sees the value's evidence chain (and can spot
    an edited contract) without expanding the whole JSON."""
    producer = source.get("producer") or {}
    output = source.get("output") or {}
    value = source.get("value") or {}
    adapter = producer.get("adapter") or "?"
    if adapter == "pathway_seed":
        who = f"job {producer.get('job_id')} · pathway {producer.get('pathway_ref_id')}"
    else:
        who = f"{producer.get('name')} · run {producer.get('run_key')}"
    where = f"{output.get('path')} · selector {output.get('selector')}"
    if output.get("encoding") == "csv" and output.get("row") is not None:
        where += f" · row {output['row']}"
    number = value.get("number")
    reading = (
        f"{number:g} {value.get('unit')}"
        if isinstance(number, (int, float)) and not isinstance(number, bool)
        else f"{value.get('text')!r} ({value.get('unit')})"
    )
    try:
        fingerprint = contract_fingerprint(source)[:12]
    except (TypeError, ValueError):
        fingerprint = "malformed"
    return (
        f"computed source: {adapter} · {who} · {where} · {reading} · fp {fingerprint}"
    )
