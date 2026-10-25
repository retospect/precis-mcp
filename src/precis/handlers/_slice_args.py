"""The one canonical slice-arg pair — ``page``/``page_size`` — with the
old spellings as one-release aliases (gr514469).

Args-facing slicing was split three ways: ``limit`` (se ``view='sites'``
/``view='ops'``, job ``/logs``, draft outline/history, pcb review),
``page`` (finding/gripe/youtube/tag listings and ``search``), and
``offset`` (se ``view='ops'``, draft outline — everywhere else an
internal name only). The canonical pair is ``page`` (1-based) +
``page_size``: the names ``search`` already advertises on every kind
and precis-search-help calls canonical. ``limit`` reads as
``page_size``, ``offset`` as an absolute 0-based row index; both still
work for one release and say so on an ``interpreted:`` first line
(:mod:`precis.handlers._interpret`). ``from`` is deliberately NOT a
slice name — on ``time`` it is a timezone and on se ``view='ray'`` a
parsed quantity, not an index.

:func:`slice_window` resolves the four spellings to ``(offset, size,
readings)``. Range vetting stays with the call site — one view's "0 =
every row" is another's "must be ≥ 1" — the helper only guarantees a
whole non-negative number under exactly one effective spelling.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from precis.errors import BadInput


def _whole(raw: Any, *, name: str, where: str, coerce: bool) -> int:
    """``raw`` as a row count/index: a real ``int``, or (``coerce``) a
    digit string — bools and floats refuse, never truncate."""
    val: int | None = (
        raw if isinstance(raw, int) and not isinstance(raw, bool) else None
    )
    if val is None and coerce and isinstance(raw, str):
        try:
            val = int(raw)
        except ValueError:
            val = None
    if val is None:
        raise BadInput(
            f"{where} '{name}' must be a whole number; got {raw!r}",
            next=f"{where} slices with args={{'page': 1, 'page_size': 50}}",
        )
    return val


def slice_window(
    args: Mapping[str, Any] | None,
    *,
    where: str,
    default_size: int | None = None,
    coerce: bool = False,
) -> tuple[int, int | None, list[str]]:
    """Resolve ``page``/``page_size`` (+ ``limit``/``offset`` aliases) in
    ``args`` to ``(offset, size, readings)``.

    ``offset`` is the 0-based start index; ``size`` the requested row
    cap — ``None`` when neither ``page_size`` nor ``limit`` was given,
    so the site applies its own default (and 0 keeps the per-site "every
    row" meaning where it has one). ``readings`` names each deprecated
    spelling actually used, for an ``interpreted:`` line.

    ``default_size`` sizes ``page=`` arithmetic only (``offset =
    (page-1) * size``): pass the site's fallback cap so a bare
    ``page=2`` still means something. ``coerce`` accepts ``"40"`` for
    sites whose args arrive through a query string.
    """
    a = args or {}
    readings: list[str] = []

    given = [n for n in ("page_size", "limit") if a.get(n) is not None]
    sizes = {n: _whole(a[n], name=n, where=where, coerce=coerce) for n in given}
    if len(set(sizes.values())) > 1:
        raise BadInput(
            f"{where} got conflicting slice sizes: "
            + ", ".join(f"{n}={v}" for n, v in sorted(sizes.items())),
            next="pass one — 'page_size' is the canonical name",
        )
    size = next(iter(sizes.values()), None)
    if size is not None and size < 0:
        raise BadInput(
            f"{where} '{given[0]}' must be >= 0; got {size}",
            next=f"{where} slices with args={{'page': 1, 'page_size': 50}}",
        )
    if "limit" in sizes:
        readings.append(
            "'limit' read as 'page_size' — the deprecated alias; "
            "'page_size' is canonical"
        )

    page_raw, off_raw = a.get("page"), a.get("offset")
    if page_raw is not None and off_raw is not None:
        raise BadInput(
            f"{where} got both 'page' and 'offset' — pass one ('page' is canonical)",
            next=f"{where} slices with args={{'page': 2, 'page_size': 50}}",
        )
    offset = 0
    if off_raw is not None:
        offset = _whole(off_raw, name="offset", where=where, coerce=coerce)
        if offset < 0:
            raise BadInput(
                f"{where} 'offset' must be >= 0; got {offset}",
                next=f"{where} slices with args={{'page': 2, 'page_size': 50}}",
            )
        readings.append(
            "'offset' read as an absolute row index — the deprecated "
            "alias; 'page' is canonical"
        )
    if page_raw is not None:
        page = _whole(page_raw, name="page", where=where, coerce=coerce)
        if page < 1:
            raise BadInput(
                f"{where} 'page' must be >= 1; got {page}",
                next=f"{where} slices with args={{'page': 1, 'page_size': 50}}",
            )
        eff = size if size is not None else default_size
        if not eff:
            raise BadInput(
                f"{where} 'page' has no window without a page size",
                next=f"{where} slices with args={{'page': {page}, 'page_size': 50}}}}",
            )
        offset = (page - 1) * eff
    return offset, size, readings
