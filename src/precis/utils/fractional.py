"""Fractional indexing for sibling-scoped chunk ordering (ADR 0033 §2).

A draft chunk's reading position among its siblings is a string `pos`
over a base-62, ASCII-ordered alphabet, compared lexicographically.
``key_between(a, b)`` returns a key strictly between its neighbours —
either bound may be ``None`` for an open end — so insert / reorder is a
single-row write and never renumbers. Repeated insertion into one gap
lengthens keys; a periodic per-sibling-group rebalance (``even_keys``)
re-stamps them short.

The alphabet is ASCII-ordered (``0-9`` < ``A-Z`` < ``a-z``) so Python /
SQL string comparison *is* the key order — no custom collation needed.
``pos`` is internal plumbing (never surfaced to agents), so base-62 is
fine; it needs no no-ambiguous-chars treatment.
"""

from __future__ import annotations

DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE = len(DIGITS)  # 62
_VAL = {c: i for i, c in enumerate(DIGITS)}


def _digit(s: str, i: int, default: int) -> int:
    return _VAL[s[i]] if i < len(s) else default


def key_between(a: str | None, b: str | None) -> str:
    """A key ``c`` with ``a < c < b`` lexicographically.

    ``a is None`` means "before everything", ``b is None`` means "after
    everything". Raises ``ValueError`` if ``a >= b`` when both are given.
    """
    if a is not None and b is not None and a >= b:
        raise ValueError(f"keys out of order: {a!r} is not < {b!r}")

    lo = a or ""  # None / "" → smallest
    out: list[str] = []
    i = 0
    while True:
        da = _digit(lo, i, 0)
        db = _digit(b, i, BASE) if b is not None else BASE
        if da == db:
            out.append(DIGITS[da])
            i += 1
            continue
        # da < db is guaranteed (lo < b, shared prefix so far)
        mid = (da + db) // 2
        if mid > da:
            out.append(DIGITS[mid])
            return "".join(out)
        # adjacent digits (db == da + 1): take da; b no longer constrains
        # (out[-1] < b's digit here), so descend against lo with b open.
        out.append(DIGITS[da])
        i += 1
        b = None


def even_keys(n: int) -> list[str]:
    """``n`` evenly-spaced keys in ascending order — for an initial run or
    a per-sibling-group rebalance. Single base-62 digits when ``n`` fits.
    """
    if n <= 0:
        return []
    if n <= BASE - 1:
        step = BASE // (n + 1)
        return [DIGITS[step * (k + 1)] for k in range(n)]
    # fall back to sequential between() for large groups
    keys: list[str] = []
    prev: str | None = None
    for _ in range(n):
        prev = key_between(prev, None)
        keys.append(prev)
    return keys


def n_keys_between(a: str | None, b: str | None, n: int) -> list[str]:
    """``n`` keys in ascending order, all strictly between ``a`` and ``b``
    (e.g. splitting a multi-paragraph ``put`` into ordered chunks).
    """
    if n <= 0:
        return []
    if n == 1:
        return [key_between(a, b)]
    mid = key_between(a, b)
    left = n // 2
    right = n - left - 1
    return n_keys_between(a, mid, left) + [mid] + n_keys_between(mid, b, right)
