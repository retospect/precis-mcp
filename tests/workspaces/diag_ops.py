"""Traced library calls outside the conformance scripts' work root."""

from precis_geom.trace import traced


@traced
def inner(n: int) -> int:
    if n < 1:
        raise ValueError("n must be >= 1")
    return n


@traced
def outer(n: int) -> int:
    return inner(n - 1)
