"""EDGAR full-text-search query assembly for the ``edgar`` kind.

The handler exposes the cross-kind ``search(q=, tags=, scope=,
page_size=)`` surface and translates it into the SEC EDGAR full-text
search (FTS) API's structured query params on the remote leg.

Unlike ``_patent_cql.py`` (which builds a CQL *string*), the EDGAR
FTS endpoint takes structured params, so the "lift" builds a **param
dict** instead:

* ``q=`` — free text, passed to the ``q`` FTS param verbatim. EDGAR
  FTS is already a keyword engine, so no auto-promote gymnastics are
  needed (contrast the patent ``ti/ab`` disjunction).
* ``tags=`` — the closed translation table maps EDGAR-specific open
  lowercase prefixes to FTS params:

  =============  ==============  ==============================
  Open prefix    FTS param       Example
  =============  ==============  ==============================
  ``form:``      ``forms``       ``form:10-k`` → ``forms=10-K``
  ``cik:``       ``ciks``        ``cik:320193`` → ``ciks=0000320193``
  ``ticker:``    ``ciks``        resolved via a ticker→CIK map
  =============  ==============  ==============================

Open prefixes with no FTS equivalent (``topic:``, ``project:``) are
silently skipped — they only narrow the local SQL leg, same rule as
patent. ``dateRange`` / ``startdt`` / ``enddt`` are deferred to
search-future-filters.

Tag values are stored lowercased (precis convention via
``Tag.open()``). The lift re-uppercases form codes and zero-pads
CIKs to the canonical 10-digit form the FTS API expects.
"""

from __future__ import annotations

from typing import Protocol

from precis.errors import BadInput


class TickerResolver(Protocol):
    """Resolves a ticker symbol to a CIK. Implemented by the client.

    Returns the CIK as a plain digit string (leading zeros optional —
    the lift zero-pads) or ``None`` when the ticker is unknown.
    """

    def resolve_ticker(self, ticker: str) -> str | None: ...


# Open tag prefixes that lift to an FTS param. Everything else narrows
# only the local SQL leg.
_TAG_TO_FTS: dict[str, str] = {
    "form": "forms",
    "cik": "ciks",
    "ticker": "ciks",
}


def build_fts_params(
    *,
    q: str | None,
    tags: list[str] | None,
    resolver: TickerResolver | None = None,
) -> dict[str, str]:
    """Assemble the EDGAR FTS query-param dict for the remote leg.

    Args:
        q:        Free text for the ``q`` FTS param. ``None`` / empty
                  is allowed iff at least one tag lifts to a param.
        tags:     Lowercased ``"prefix:value"`` strings as stored in
                  ``ref_open_tags``. Open prefixes without an FTS
                  equivalent are silently skipped.
        resolver: Resolves ``ticker:`` tags to CIKs. Optional — a
                  ``ticker:`` tag is skipped when no resolver is given
                  or the symbol is unknown.

    Raises:
        BadInput: neither ``q=`` nor any liftable tag was provided.
    """
    params: dict[str, str] = {}
    forms: list[str] = []
    ciks: list[str] = []

    if q is not None and q.strip():
        params["q"] = q.strip()

    for tag in tags or []:
        if ":" not in tag:
            continue
        prefix, _, value = tag.partition(":")
        if prefix not in _TAG_TO_FTS or not value:
            continue
        if prefix == "form":
            _append_unique(forms, _canonical_form(value))
        elif prefix == "cik":
            _append_unique(ciks, _canonical_cik(value))
        elif prefix == "ticker":
            cik = resolver.resolve_ticker(value) if resolver is not None else None
            if cik:
                _append_unique(ciks, _canonical_cik(cik))

    if forms:
        params["forms"] = ",".join(forms)
    if ciks:
        params["ciks"] = ",".join(ciks)

    if not params:
        raise BadInput(
            "search requires q= or an FTS-liftable tag",
            next=(
                "search(kind='edgar', q='climate risk') or "
                "search(kind='edgar', tags=['form:10-k'])"
            ),
        )

    return params


def _append_unique(bag: list[str], value: str) -> None:
    """Append ``value`` to ``bag`` preserving order, skipping dups/blanks."""
    if value and value not in bag:
        bag.append(value)


def _canonical_form(slug: str) -> str:
    """Lowercased ``form:`` tag value → canonical SEC form code.

    Storage rule: tag values are lowercased on insert. SEC form codes
    are uppercase (``10-K``, ``8-K``, ``S-1``, ``DEFM14A``); the
    canonical render makes FTS hits match the filed form exactly.
    """
    return slug.strip().upper()


def _canonical_cik(value: str) -> str:
    """CIK digits → zero-padded 10-digit form the FTS ``ciks`` param wants.

    Accepts already-padded, unpadded, or ticker-resolver output. Any
    non-digit characters are stripped first; an all-empty result
    returns the empty string (skipped by :func:`_append_unique`).
    """
    digits = "".join(c for c in value if c.isdigit())
    if not digits:
        return ""
    return str(int(digits)).zfill(10)


__all__ = ["TickerResolver", "build_fts_params"]
