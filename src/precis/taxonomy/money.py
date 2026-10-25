"""Currency tokens in measure units — slice 1 of
``docs/backlog/measure-currency-units.md`` (gr510555).

:func:`split_currency` reads the currency part of a printed unit: an ISO code
the ECB euro reference rates quote (:data:`ECB_CURRENCIES`, plus ``EUR``
itself) or one of the symbols ``$`` / ``US$`` / ``€`` / ``£`` — ``¥``, ``A$``
and ``C$`` are ambiguous and refused. Balanced ``$...$`` pairs whose
contents carry TeX are LaTeX math delimiters, not dollars, and fold away
first (``mA$^{-1}$``, ``$\\mu m$``); a lone ``$`` still reads as USD, and a
plain pair like ``$/t (2015$)`` is two dollars — the second token merges
back (USD 2015). An optional 4-digit price year in
1900..2100 may sit next to the code (``USD2015``, ``USD 2015``,
``USD(2015)``, ``2015 USD``, ``2015USD``); a 4-digit number outside that
range is refused as an unsupported scale factor (``1000 USD/yr``).
Everything else is the *remainder* — ``/t``, ``/yr``, ``/nozzle`` or empty.

The pint side lives with the shared registry
(:func:`precis.taxonomy.measure_units._registry` applies
:data:`MONEY_DEFINITION`): the converter swaps the token for ``money`` and
lets pint scale the remainder, so ``EUR/kg`` on a ``USD/t`` measurand stores
x1000 and keeps ``currency='EUR'``. Slice 1 records the currency *as
reported* — there is no FX conversion anywhere yet.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from precis.errors import BadInput

#: Every currency the ECB ``eurofxref-hist`` file quotes (from 1999-01-04):
#: the live daily set plus the codes retired along the way (legacy euro-area
#: members, HRK, ROL/TRL, RUB). ``EUR`` itself is the file's numeraire, so it
#: is not one of its columns — callers use :data:`CURRENCIES`.
ECB_CURRENCIES: Final[frozenset[str]] = frozenset(
    {
        "AUD",
        "BGN",
        "BRL",
        "CAD",
        "CHF",
        "CNY",
        "CYP",
        "CZK",
        "DKK",
        "EEK",
        "GBP",
        "HKD",
        "HRK",
        "HUF",
        "IDR",
        "ILS",
        "INR",
        "ISK",
        "JPY",
        "KRW",
        "LTL",
        "LVL",
        "MTL",
        "MXN",
        "MYR",
        "NOK",
        "NZD",
        "PHP",
        "PLN",
        "ROL",
        "RON",
        "RUB",
        "SEK",
        "SGD",
        "SIT",
        "SKK",
        "THB",
        "TRL",
        "TRY",
        "USD",
        "ZAR",
    }
)

#: The codes :func:`split_currency` accepts: the ECB set plus EUR.
CURRENCIES: Final[frozenset[str]] = ECB_CURRENCIES | {"EUR"}

#: Unambiguous symbols -> ISO code. ``US$`` is matched before ``$``; a ``$``
#: not preceded by a letter reads as USD (``A$``/``C$`` are refused first).
_SYMBOLS: Final[tuple[tuple[str, str], ...]] = (
    ("US$", "USD"),
    ("€", "EUR"),
    ("£", "GBP"),
    ("$", "USD"),
)

#: Symbols refused as ambiguous: each maps to the ISO code(s) it might mean.
_AMBIGUOUS: Final[dict[str, str]] = {
    "¥": "JPY or CNY",
    "A$": "AUD",
    "C$": "CAD",
}

#: The pint definition the shared measures registry applies. ``money`` is a
#: stand-in the currency token swaps for, so pint scales only the remainder.
MONEY_DEFINITION: Final[str] = "money = [currency]"

#: A code is bounded by non-letters on both sides (digits allowed, so the
#: glued year forms ``USD2015`` / ``2015USD`` still match).
_CODE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z])(" + "|".join(sorted(CURRENCIES)) + r")(?![A-Za-z])"
)
_YEAR_BEFORE_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\d)(\d{4})\s*$")
_YEAR_AFTER_RES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^\s*\(\s*(\d{4})\s*\)"),  # USD(2015)
    re.compile(r"^\s*(\d{4})(?!\d)"),  # USD2015, USD 2015
)
_MIN_YEAR = 1900
#: A 4-digit number beside the code is read as a price year only inside
#: 1900..2100; outside it is a thousands/millions scale (``1000 USD/yr``),
#: which is refused, not silently folded into the remainder.
_MAX_YEAR_TOKEN = 2100

#: A balanced ``$...$`` pair whose contents carry TeX is LaTeX math markup,
#: not a dollar sign (``K$^{-1}$``, ``mA$^{-1}$``, ``$\\mu m$``). The
#: delimiters fold away — contents kept, the ``markup_unit`` rule — before
#: any symbol or ambiguity check; a lone, unpaired ``$`` still reads as USD
#: (gr510555 review).
_MATH_DOLLARS_RE: Final[re.Pattern[str]] = re.compile(r"\$([^$]*)\$")
#: What makes a ``$...$`` pair math rather than two dollar signs: a TeX
#: mark inside — a command, an exponent, a subscript or a group. A plain
#: pair is currency twice over: ``$/kWh ($2015)`` is USD 2015, ``m$^2$``
#: is math.
_TEX_MARK_RE: Final[re.Pattern[str]] = re.compile(r"[\\^_{]")
#: A lone ``$`` that opens an unclosed math span — the next character is a
#: TeX command (``$\\mu m``). ``$/t`` still reads as USD.
_LONE_DOLLAR_MATH_RE: Final[re.Pattern[str]] = re.compile(r"\$(?=\\)")


def _fold_math_dollars(text: str) -> str:
    """Drop balanced TeX-carrying ``$...$`` math delimiters — and a lone
    ``$`` that opens an unclosed span before a TeX command — keeping their
    contents. A pair whose contents hold no TeX mark stays: two dollars."""
    folded = _MATH_DOLLARS_RE.sub(
        lambda m: m.group(1) if _TEX_MARK_RE.search(m.group(1)) else m.group(0),
        text,
    )
    return _LONE_DOLLAR_MATH_RE.sub("", folded)


@dataclass(frozen=True, slots=True)
class CurrencyUnit:
    """The currency part of a printed unit. ``currency`` is None when the
    unit carries no token; ``remainder`` is everything else (``/t``, ``yr``,
    ``/nozzle`` or empty)."""

    currency: str | None
    price_year: int | None
    remainder: str


def check_price_year(year: int) -> int:
    """A price year in 1900..the current UTC year; refuses otherwise."""
    current = datetime.now(UTC).year
    if not _MIN_YEAR <= year <= current:
        raise BadInput(
            f"price year {year} is outside {_MIN_YEAR}..{current}",
            next="state the price year as a 4-digit year, e.g. reported_unit='USD2015/t' "
            "or price_year=2015",
        )
    return year


def _dollar(text: str) -> int:
    """Position of a ``$`` read as USD: not preceded by a letter (``A$`` and
    ``C$`` are refused earlier; ``HK$`` never matches)."""
    for m in re.finditer(r"\$", text):
        if m.start() == 0 or not text[m.start() - 1].isalpha():
            return m.start()
    return -1


def _year_token(n: int, unit: str | None) -> int:
    """A 4-digit number beside the code: a price year inside
    ``1900..2100``; anything else is a scale factor (``1000 USD/yr``,
    ``USD 10000`` is not reachable — five digits never match), refused
    with ``next=`` rather than read as a year."""
    if not _MIN_YEAR <= n <= _MAX_YEAR_TOKEN:
        raise BadInput(
            f"the number {n} beside the currency in unit {unit!r} is not a "
            f"price year ({_MIN_YEAR}..{_MAX_YEAR_TOKEN}) and a scale factor "
            "on a currency is not supported",
            next="state the plain unit (e.g. 'USD/t') and give the number "
            "already multiplied by the scale",
        )
    return n


def split_currency(unit: str | None) -> CurrencyUnit:
    """Split a printed unit into (currency, price_year, remainder).

    ``'EUR/kg'`` -> ``('EUR', None, '/kg')``; ``'USD2015/t'`` ->
    ``('USD', 2015, '/t')``; ``'kg'`` -> ``(None, None, 'kg')``. Ambiguous
    symbols, a second currency token, a conflicting pair of years, a year
    outside 1900..now and an out-of-range 4-digit scale factor are
    refused."""
    text = _fold_math_dollars((unit or "").strip())
    if not text:
        return CurrencyUnit(None, None, "")
    for sym, codes in _AMBIGUOUS.items():
        if sym in text:
            raise BadInput(
                f"currency symbol {sym!r} in unit {unit!r} is ambiguous",
                next=f"state the ISO code instead ({codes})",
            )
    code: str | None = None
    start = end = 0
    m = _CODE_RE.search(text)
    if m is not None:
        code, start, end = m.group(1), m.start(), m.end()
    else:
        for sym, iso in _SYMBOLS:
            pos = _dollar(text) if sym == "$" else text.find(sym)
            if pos >= 0:
                code, start, end = iso, pos, pos + len(sym)
                break
    if code is None:
        return CurrencyUnit(None, None, text)

    before, after = text[:start], text[end:]
    year: int | None = None
    if (m := _YEAR_BEFORE_RE.search(before)) is not None:
        year = _year_token(int(m.group(1)), unit)
        before = before[: m.start()]
    for rex in _YEAR_AFTER_RES:
        if (m := rex.match(after)) is not None:
            other = _year_token(int(m.group(1)), unit)
            if year is not None and year != other:
                raise BadInput(
                    f"unit {unit!r} carries two different price years "
                    f"({year} and {other})",
                    next="give the price year once: 'USD2015/t' or price_year=2015",
                )
            year = other
            after = after[m.end() :]
            break
    if year is not None:
        check_price_year(year)
    remainder = re.sub(
        r"\s*/\s*", "/", re.sub(r"\s+", " ", f"{before} {after}".strip())
    )
    remainder = re.sub(r"\(\s*\)", "", remainder).strip()
    if _CODE_RE.search(remainder) or any(sym in remainder for sym in "$€£"):
        # a second token: the same currency stated twice merges back (the
        # parenthesised year in ``$/t ($2015)``); a different one refuses
        rest = split_currency(remainder)
        if rest.currency is not None:
            if rest.currency != code:
                raise BadInput(
                    f"unit {unit!r} carries more than one currency",
                    next="state one currency code with an optional price year, "
                    "e.g. 'EUR2015/t'",
                )
            if (
                year is not None
                and rest.price_year is not None
                and rest.price_year != year
            ):
                raise BadInput(
                    f"unit {unit!r} carries two different price years "
                    f"({year} and {rest.price_year})",
                    next="give the price year once: 'USD2015/t' or price_year=2015",
                )
            year = year if year is not None else rest.price_year
            remainder = rest.remainder
            if remainder in ("", "/"):
                raise BadInput(
                    f"unit {unit!r} carries only the currency, twice",
                    next="state the currency once with its unit, e.g. 'USD/t'",
                )
    return CurrencyUnit(code, year, remainder)


def money_unit_text(cur: CurrencyUnit) -> str:
    """The unit text pint sees after the token swap: ``'money/t'`` for
    ``EUR/t``, ``'money'`` for a bare code."""
    rem = cur.remainder
    if not rem:
        return "money"
    return "money" + rem if rem.startswith("/") else f"money {rem}"


def taxon_is_currency(meta: Mapping[str, Any], canonical_unit: str | None) -> bool:
    """A currency measurand: ``meta.dimension_kind == 'currency'``, or a
    ``canonical_unit`` carrying a currency token."""
    if meta.get("dimension_kind") == "currency":
        return True
    try:
        return split_currency(canonical_unit).currency is not None
    except BadInput:
        return False


def effective_currency(
    currency: str | None,
    price_year: int | None,
    reported_unit: str | None,
    canonical_unit: str | None,
) -> tuple[str | None, int | None]:
    """A row's effective ``(currency, price_year)``: its stored columns, else
    the token in ``reported_unit``, else the token in ``canonical_unit`` —
    the legacy-row reading for a NULL-currency row written before the
    columns existed."""
    if currency:
        return currency, price_year
    for text in (reported_unit, canonical_unit):
        try:
            cur = split_currency(text)
        except BadInput:
            continue
        if cur.currency is not None:
            return cur.currency, cur.price_year
    return None, None


def row_currency(row: Mapping[str, Any]) -> tuple[str | None, int | None]:
    """:func:`effective_currency` over a measure row dict."""
    return effective_currency(
        row.get("currency"),
        row.get("price_year"),
        row.get("reported_unit"),
        row.get("canonical_unit"),
    )
