"""Bounded structural checks for trusted completion types, never trust creation.

Schemas are local Python types; no schema or verifier is selected from input.
Passing this check says nothing about producer provenance. The v1 DB reader
still constructs no VerifiedReceipt from raw JSON.
"""

from __future__ import annotations

import types
from functools import lru_cache
from typing import (
    Any,
    Literal,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)


@lru_cache(maxsize=128)
def _fields(schema: Any) -> dict[str, Any]:
    return get_type_hints(schema)


def valid_record(value: Any, schema: Any) -> bool:
    nodes = 0

    def valid(item: Any, annotation: Any, depth: int) -> bool:
        nonlocal nodes
        nodes += 1
        if nodes > 100_000 or depth > 64:
            return False
        if annotation is Any or annotation is object:
            return True
        origin, args = get_origin(annotation), get_args(annotation)
        if is_typeddict(annotation):
            fields = _fields(annotation)
            return (
                type(item) is dict
                and set(item) == set(fields)
                and all(valid(item[key], sub, depth + 1) for key, sub in fields.items())
            )
        if origin in (Union, types.UnionType):
            return any(valid(item, choice, depth + 1) for choice in args)
        if origin is Literal:
            return any(type(item) is type(choice) and item == choice for choice in args)
        if origin is list:
            return type(item) is list and all(
                valid(v, args[0], depth + 1) for v in item
            )
        if origin is tuple:
            return (
                type(item) in (list, tuple)
                and len(item) == len(args)
                and all(
                    valid(v, sub, depth + 1) for v, sub in zip(item, args, strict=True)
                )
            )
        if origin is dict:
            return type(item) is dict and all(
                valid(k, args[0], depth + 1) and valid(v, args[1], depth + 1)
                for k, v in item.items()
            )
        return type(item) is annotation

    return valid(value, schema, 0)
