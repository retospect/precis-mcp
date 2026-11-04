"""Labelled memory searches through the ordinary dispatch path.

A batch preserves each query's ranking and error separately. Children run
sequentially: they need normal request/user scope and retry/error handling,
not a worker pool sharing the caller's store connection. All item shapes are
checked before the first search so malformed input has no partial execution.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any, NoReturn

from precis.errors import BadInput, ResultBudgetExceeded
from precis.response import Response

MAX_BATCH_ITEMS = 8
MAX_BATCH_PAGE_SIZE = 10
DEFAULT_BATCH_PAGE_SIZE = 5
_FILTERS = frozenset({"scope", "page_size", "mode", "view", "tags", "under", "depth"})
_ITEM_KEYS = _FILTERS | {"key", "q"}
_OUTER_KEYS = _FILTERS | {"kind", "q", "page", "__extras__"}
_VIEWS = frozenset({"index", "ids", "count"})
_NEXT = (
    "search(kind='memory', args={'batch': "
    "[{'key': 'jane', 'q': 'Jane'}, {'key': 'bill', 'q': 'Bill'}]})"
)


def _refuse(message: str) -> NoReturn:
    raise BadInput(message, next=_NEXT)


def _check_filter_types(values: dict[str, Any], where: str) -> None:
    for name in ("scope", "mode", "view"):
        value = values.get(name)
        if value is not None and not isinstance(value, str):
            _refuse(f"{where}.{name} must be a string")
    view = values.get("view")
    if view is not None and view not in _VIEWS:
        _refuse(f"{where}.view must be index, ids, count, or omitted in a memory batch")
    tags = values.get("tags")
    if tags is not None and (
        not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags)
    ):
        _refuse(f"{where}.tags must be a list of strings")
    size = values.get("page_size")
    if size is not None and (
        isinstance(size, bool) or not isinstance(size, int) or size < 1
    ):
        _refuse(f"{where}.page_size must be a positive integer")
    depth = values.get("depth")
    if depth is not None and (
        isinstance(depth, bool) or not isinstance(depth, int) or depth < 1
    ):
        _refuse(f"{where}.depth must be a positive integer")
    under = values.get("under")
    if under is not None and (
        isinstance(under, bool)
        or not isinstance(under, (str, int, list))
        or (
            isinstance(under, list)
            and any(not isinstance(value, str) for value in under)
        )
    ):
        _refuse(f"{where}.under must be a handle/string, integer, or list of strings")


def _children(args: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    if args.get("kind") != "memory":
        _refuse("args.batch requires kind='memory'")
    if args.get("q") is not None:
        _refuse("args.batch cannot combine with an outer q; put q in each item")
    unknown = sorted(str(key) for key in args if key not in _OUTER_KEYS)
    if unknown:
        _refuse(
            f"unsupported shared batch fields {unknown}; use only the documented query filters"
        )
    if isinstance(args.get("page"), bool) or args.get("page") not in (None, 1):
        _refuse("a memory batch supports only page=1")
    extras = copy.deepcopy(args.get("__extras__", {}))
    items = extras.pop("batch")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_BATCH_ITEMS:
        _refuse(f"args.batch must contain 1..{MAX_BATCH_ITEMS} search objects")
    unknown = sorted(str(key) for key in extras if key not in _FILTERS)
    if unknown:
        _refuse(
            f"unsupported shared batch args {unknown}; use only the documented query filters"
        )

    defaults = copy.deepcopy(args)
    defaults.pop("__extras__", None)
    # Canonicalize shared fields before an item overrides a subtree or mode.
    for name in _FILTERS:
        if name not in extras:
            continue
        value = extras.pop(name)
        if defaults.get(name) is not None and defaults[name] != value:
            _refuse(f"conflicting shared {name} values inside and outside args")
        defaults[name] = value
    _check_filter_types(defaults, "shared search")
    shared_tags = defaults.get("tags") or []
    children: list[tuple[str, dict[str, Any]]] = []
    keys: set[str] = set()
    for position, item in enumerate(items):
        where = f"args.batch[{position}]"
        if not isinstance(item, dict):
            _refuse(f"{where} must be a search object")
        unknown = sorted(str(key) for key in item if key not in _ITEM_KEYS)
        if unknown:
            _refuse(
                f"{where} has unsupported fields {unknown}; nested batches/args are not supported"
            )
        key, query = item.get("key"), item.get("q")
        if not isinstance(key, str) or not key.strip():
            _refuse(f"{where}.key must be a nonempty string")
        key = key.strip()
        if key in keys:
            _refuse(f"duplicate batch key {key!r}; use a unique key per search")
        keys.add(key)
        if not isinstance(query, str) or not query.strip():
            _refuse(f"{where}.q must be a nonempty string")
        _check_filter_types(item, where)
        child = copy.deepcopy(defaults)
        child.update({name: value for name, value in item.items() if name != "key"})
        child["tags"] = list(dict.fromkeys([*shared_tags, *(item.get("tags") or [])]))
        child["page_size"] = child.get("page_size") or DEFAULT_BATCH_PAGE_SIZE
        if child["page_size"] > MAX_BATCH_PAGE_SIZE:
            _refuse(
                f"{where}.page_size exceeds the batch maximum {MAX_BATCH_PAGE_SIZE}"
            )
        children.append((key, child))
    return children


def dispatch_memory_batch(
    args: dict[str, Any], run_search: Callable[[dict[str, Any]], tuple[str, bool]]
) -> Response:
    """Validate once, then return each ordinary search's body and error flag."""
    children = _children(args)
    results = []
    for key, child in children:
        body, is_error = run_search(child)
        results.append(
            {"key": key, "q": child["q"], "is_error": is_error, "body": body}
        )
    return Response(
        body=json.dumps({"results": results}, ensure_ascii=False, indent=2),
        result_count=len(results),
        result_budget_hint="repeat individual labelled searches, or use fewer items/page_size",
    )


def fit_memory_batch_body(body: str, *, context_limited: bool) -> str:
    """A labelled envelope is indivisible: return it whole or refuse its size."""
    from precis._result_budget import (
        DEFAULT_MAX_BODY_BYTES,
        _env_cap,
        result_context_cap,
    )

    cap = _env_cap("PRECIS_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES)
    if context_limited:
        cap = min(cap, result_context_cap())
    size = len(body.encode("utf-8"))
    if size > cap:
        raise ResultBudgetExceeded(
            f"memory search batch result is {size} bytes, above the {cap}-byte result budget; "
            "the complete labelled envelope cannot be truncated",
            next="retry with fewer batch items or page_size=1; alternatively "
            "search(kind='memory', q='<one original query>', page_size=5)",
        )
    return body
