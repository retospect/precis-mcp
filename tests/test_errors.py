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


def test_bad_input_is_also_value_error() -> None:
    # gr514202: scripts wrapping pga.geom in `except ValueError` (hexfold
    # raises plain ValueError) must also catch the BadInput it raises.
    assert issubclass(BadInput, ValueError)
    try:
        raise BadInput("bad arg", next="fix the arg")
    except ValueError as e:
        assert isinstance(e, BadInput)
        assert isinstance(e, PrecisError)


def test_only_bad_input_is_value_error() -> None:
    # The ValueError door is BadInput-specific; sibling typed errors stay
    # PrecisError-only so `except ValueError` doesn't swallow e.g. Gone.
    for cls in (NotFound, Unsupported, Upstream, RateLimited, Internal):
        assert not issubclass(cls, ValueError)


def test_chain_via_from() -> None:
    src = ValueError("root")
    try:
        raise BadInput("wrapped") from src
    except BadInput as e:
        assert e.__cause__ is src
