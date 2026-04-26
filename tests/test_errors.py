"""Exception hierarchy + breaking-hint field."""

from __future__ import annotations

from precis.errors import (
    BadInput,
    Internal,
    NotFound,
    PrecisError,
    RateLimited,
    Unsupported,
    Upstream,
)


def test_precis_error_carries_cause() -> None:
    e = PrecisError("bad")
    assert e.cause == "bad"
    assert e.next is None
    assert e.options is None
    assert str(e) == "bad"


def test_next_is_carried() -> None:
    e = BadInput("oops", next="try this")
    assert e.next == "try this"


def test_options_are_copied_not_aliased() -> None:
    src = ["a", "b"]
    e = BadInput("oops", options=src)
    assert e.options == ["a", "b"]
    src.append("c")
    assert e.options == ["a", "b"], "options must be a copy, not the caller's list"


def test_subclass_hierarchy() -> None:
    for cls in (NotFound, BadInput, Unsupported, Upstream, RateLimited, Internal):
        assert issubclass(cls, PrecisError)


def test_chain_via_from() -> None:
    src = ValueError("root")
    try:
        raise BadInput("wrapped") from src
    except BadInput as e:
        assert e.__cause__ is src
