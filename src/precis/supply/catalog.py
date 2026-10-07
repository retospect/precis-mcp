"""Curated supplier category mappings and strict metric filter selection.

IDs come from supplier data, not geometry or keyword guesses. Parameter
value IDs are resolved against the API's current filter options: missing
thread/length values must refuse the filtered lookup rather than broaden
it into unrelated hardware. Product matches still carry keyword confidence.
"""

from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from importlib import resources
from typing import Any


class FilterUnavailable(RuntimeError):
    """Safe, locally generated diagnostic for an unresolved stock filter."""


@lru_cache(maxsize=1)
def series_mapping(series_id: str) -> dict[str, Any] | None:
    data = json.loads(
        resources.files("precis.data")
        .joinpath("digikey_series.json")
        .read_text(encoding="utf-8")
    )
    for group in data["categories"]:
        if series_id in group["series"]:
            return dict(group)
    return None


def validate_filters(
    category_id: int | None, parameters: dict[str, list[str]] | None
) -> None:
    if category_id is not None and (
        isinstance(category_id, bool)
        or not isinstance(category_id, int)
        or category_id <= 0
    ):
        raise ValueError("category_id must be a positive integer")
    if parameters is None:
        return
    if category_id is None:
        raise ValueError("parameters require category_id")
    if not isinstance(parameters, dict) or not parameters:
        raise ValueError("parameters must map parameter IDs to nonempty value-ID lists")
    for key, values in parameters.items():
        if not isinstance(key, str) or not key.isdecimal() or int(key) <= 0:
            raise ValueError("parameter IDs must be positive integer strings")
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) or not value.strip() for value in values)
        ):
            raise ValueError("parameter values must be nonempty string-ID lists")


def metric_parameters(
    options: list[dict[str, Any]],
    *,
    thread: str,
    pitch: float | None,
    length: float | None,
    category_id: int,
    length_parameter: str = "Length - Below Head",
) -> dict[str, list[str]]:
    """Resolve exact metric thread/length values; never fuzzy-match a size.

    Catalogue labels include e.g. M4, M4x0.7 and 0.472\" (12.00mm).
    A pitch-labelled thread must match the series pitch. Inch-only values
    are not silently converted, nor below-head length confused with overall.
    """
    wanted = {
        "Thread/Screw/Hole Size" if category_id == 571 else "Thread Size": "thread"
    }
    if length is not None:
        wanted[length_parameter] = "length"
    result: dict[str, list[str]] = {}
    for name, dimension in wanted.items():
        candidates = [
            option
            for option in options
            if option.get("ParameterName") == name
            and str((option.get("Category") or {}).get("Id")) == str(category_id)
        ]
        if len(candidates) != 1:
            raise FilterUnavailable(
                f"{dimension} filter unavailable for category {category_id}"
            )
        option = candidates[0]
        ids = []
        for value in option.get("FilterValues") or []:
            label = str(value.get("ValueName") or "")
            if dimension == "thread":
                match = re.fullmatch(
                    r"(M\d+(?:\.\d+)?)(?:[x×](\d+(?:\.\d+)?))?", label, re.I
                )
                selected = bool(
                    match
                    and match[1].upper() == thread.upper()
                    and (
                        match[2] is None
                        or (pitch is not None and math.isclose(float(match[2]), pitch))
                    )
                )
            else:
                match = re.search(r"(?:^|\()(\d+(?:\.\d+)?)\s*mm\)?$", label, re.I)
                selected = bool(
                    match
                    and length is not None
                    and math.isclose(float(match[1]), length)
                )
            if selected and value.get("ValueId") is not None:
                ids.append(str(value["ValueId"]))
        if not ids:
            raise FilterUnavailable(
                f"{dimension} value unavailable for category {category_id}"
            )
        result[str(option["ParameterId"])] = ids
    validate_filters(category_id, result)
    return result
