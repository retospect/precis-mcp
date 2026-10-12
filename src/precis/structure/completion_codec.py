"""Bounded exact-number JSON transport for completion evidence.

Integers use ``{"$int":"..."}``; float/Decimal/Fraction values use reduced
``{"$rational":{"n":"...","d":"..."}}``. Booleans stay booleans. Rational
decode preserves value as Fraction, not the original numeric Python type.
Ordinary mappings cannot contain either reserved tag key. This is transport,
not receipt validation or a scientific identity hash: callers explicitly use
``canonical_number`` where scientific integers must normalize to rationals.

Limits apply to the wire tree, including tag keys/values: 4096 decimal digits
per numerator/denominator/integer, depth 64 (root zero), 100000 nodes including
mapping keys, and 16 MiB aggregate UTF-8 string/key bytes. Serialized text also
has a 16 MiB byte cap. Decimal coefficients and absolute exponents are bounded
before ratio expansion; zero normalizes without expanding its exponent.
JSON text is scanned for depth/items before parsing, duplicate keys are refused,
and no untagged JSON numbers are accepted. No backend, DB or network is used.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any

MAX_NUMERIC_DIGITS = 4096
MAX_DEPTH = 64
MAX_ITEMS = 100_000
MAX_TEXT_BYTES = 16 * 1024 * 1024
_INTEGER_LIMIT = 10**MAX_NUMERIC_DIGITS
_INTEGER = re.compile(r"(?:0|-?[1-9][0-9]*)\Z", re.ASCII)
_RESERVED = frozenset({"$int", "$rational"})


class CodecError(ValueError):
    """Malformed, unsupported or over-budget completion wire data."""


def _int_text(value: int) -> str:
    if not -_INTEGER_LIMIT < value < _INTEGER_LIMIT:
        raise CodecError("numeric digit limit exceeded")
    return str(value)


def canonical_number(value: object) -> Fraction:
    """Normalize an explicitly scientific number exactly; reject booleans.

    Decimal('0.1') becomes 1/10; float 0.1 retains its actual binary ratio.
    Integer identifiers should bypass this helper so their wire tag stays int.
    """
    if type(value) is int:
        _int_text(value)
        return Fraction(value)
    if type(value) is float:
        if not math.isfinite(value):
            raise CodecError("nonfinite number")
        numerator, denominator = value.as_integer_ratio()
    elif type(value) is Decimal:
        if not value.is_finite():
            raise CodecError("nonfinite number")
        if value.is_zero():
            return Fraction(0)
        parts = value.as_tuple()
        exponent = parts.exponent
        if (
            len(parts.digits) > MAX_NUMERIC_DIGITS
            or not isinstance(exponent, int)
            or abs(exponent) > MAX_NUMERIC_DIGITS
        ):
            raise CodecError("decimal expansion limit exceeded")
        numerator, denominator = value.as_integer_ratio()
    elif type(value) is Fraction:
        numerator, denominator = value.numerator, value.denominator
    else:
        raise CodecError("unsupported exact number")
    _int_text(numerator)
    _int_text(denominator)
    return Fraction(numerator, denominator)


def _text_size(value: str) -> int:
    if len(value) > MAX_TEXT_BYTES:
        raise CodecError("text byte limit exceeded")
    # Escaped lone surrogates are representable by JSON; preserve strings.
    return len(value.encode("utf-8", errors="surrogatepass"))


@dataclass
class _Budget:
    items: int = 0
    text_bytes: int = 0

    def node(self, depth: int, text: str | None = None) -> None:
        self.items += 1
        if depth > MAX_DEPTH:
            raise CodecError("depth limit exceeded")
        if self.items > MAX_ITEMS:
            raise CodecError("item limit exceeded")
        if text is not None:
            self.text_bytes += _text_size(text)
            if self.text_bytes > MAX_TEXT_BYTES:
                raise CodecError("text byte limit exceeded")


def _parse_int(value: object) -> int:
    if type(value) is not str:
        raise CodecError("integer tag requires a decimal string")
    if len(value.removeprefix("-")) > MAX_NUMERIC_DIGITS:
        raise CodecError("numeric digit limit exceeded")
    if not _INTEGER.fullmatch(value):
        raise CodecError("noncanonical integer string")
    return int(value)


def _tag(value: dict[str, Any], budget: _Budget, depth: int) -> int | Fraction:
    """Validate/count a tag; its root node has already been counted."""
    if set(value) == {"$int"}:
        result = _parse_int(value["$int"])
        budget.node(depth + 1, "$int")
        budget.node(depth + 1, value["$int"])
        return result
    if set(value) != {"$rational"}:
        raise CodecError("reserved numeric tag has extra keys")
    ratio = value["$rational"]
    if type(ratio) is not dict or set(ratio) != {"n", "d"}:
        raise CodecError("rational tag requires exactly n and d")
    n, d = _parse_int(ratio["n"]), _parse_int(ratio["d"])
    if d <= 0 or math.gcd(n, d) != 1:
        raise CodecError("rational must be reduced with positive denominator")
    budget.node(depth + 1, "$rational")
    budget.node(depth + 1)
    for key in ("n", "d"):
        budget.node(depth + 2, key)
        budget.node(depth + 2, ratio[key])
    return Fraction(n, d)


def _walk(value: object, budget: _Budget, depth: int, *, decode: bool) -> Any:
    budget.node(depth, value if type(value) is str else None)
    if value is None or type(value) in (bool, str):
        return value
    if not decode and type(value) in (int, float, Decimal, Fraction):
        if type(value) is int:
            tag: dict[str, Any] = {"$int": _int_text(value)}
        else:
            ratio = canonical_number(value)
            tag = {
                "$rational": {
                    "n": _int_text(ratio.numerator),
                    "d": _int_text(ratio.denominator),
                }
            }
        _tag(tag, budget, depth)
        return tag
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise CodecError("object keys must be strings")
        if _RESERVED.intersection(value):
            if decode:
                return _tag(value, budget, depth)
            raise CodecError("ordinary data contains a reserved numeric tag key")
        result = {}
        for key in sorted(value):
            budget.node(depth + 1, key)
            result[key] = _walk(value[key], budget, depth + 1, decode=decode)
        return result
    if type(value) is list or (not decode and type(value) is tuple):
        return [_walk(item, budget, depth + 1, decode=decode) for item in value]
    raise CodecError("unsupported value or untagged wire number")


def to_wire(value: object) -> Any:
    """Encode supported Python values as a bounded tagged JSON-compatible tree."""
    return _walk(value, _Budget(), 0, decode=False)


def from_wire(value: object) -> Any:
    """Decode a strict wire tree; raw numeric values and tuple arrays are refused."""
    return _walk(value, _Budget(), 0, decode=True)


def dumps_wire(value: object) -> str:
    """Encode deterministic compact JSON, independent of mapping insertion order."""
    text = json.dumps(to_wire(value), sort_keys=True, separators=(",", ":"))
    if _text_size(text) > MAX_TEXT_BYTES:
        raise CodecError("text byte limit exceeded")
    return text


def _scan_json(text: str) -> None:
    if _text_size(text) > MAX_TEXT_BYTES:
        raise CodecError("text byte limit exceeded")
    depth = items = 0
    quoted = escaped = primitive = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char.isspace() or char in ",:}]":
            primitive = False
            if char in "}]":
                depth -= 1
            continue
        if char in '[{"' or not primitive:
            items += 1
            if items > MAX_ITEMS:
                raise CodecError("item limit exceeded")
            if depth > MAX_DEPTH:
                raise CodecError("depth limit exceeded")
        if char in "[{":
            depth += 1
            primitive = False
        elif char == '"':
            quoted = True
            primitive = False
        else:
            primitive = True


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CodecError("duplicate JSON object key")
        result[key] = value
    return result


def _raw_number(_text: str) -> Any:
    raise CodecError("untagged JSON number or nonfinite constant")


def loads_wire(text: str) -> Any:
    """Parse strict, bounded JSON and decode tags without numeric precision loss."""
    if type(text) is not str:
        raise CodecError("wire JSON must be text")
    _scan_json(text)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_int=_raw_number,
            parse_float=_raw_number,
            parse_constant=_raw_number,
        )
    except json.JSONDecodeError as exc:
        raise CodecError("invalid JSON") from exc
    return from_wire(value)
