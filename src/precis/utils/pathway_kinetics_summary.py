"""Stored kinetics census shared by the pathway plugin and the quest handler.

One query projects small fields, never result/graph blobs. Each axis is a
partition, so a poisoned chain fallback appears once in the failure table:
diagnosis first, then a classified TOF sign, non-positive TOF, chain fallback,
other failed MF, and finally none/unknown. Rung selection matches the readers'
KMC (>0), finite MF, finite chain precedence. No warning/reason prose is parsed.

The writers currently stamp ``kinetics_trusted`` on jobs, not pathways. A job
link alone cannot prove its verdict belongs to the current recomputed record;
absent a verdict on the pathway, report ``not recorded`` rather than borrowing
one or recalculating the trust gate from warnings. Environment basis is the
stored hash, never a hash inferred from config. Missing v2 records get their
own row on every axis, including pathways with only v1 kinetics.

Default tables show ten values plus the no-v2 row and exact remainder counts
so distinct basis hashes cannot exhaust the transport budget. Axis pages retain
full-value grouping and stable count/value ordering; only basis display is abbreviated.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from precis.errors import BadInput
from precis.store import Store

_NO_V2 = "no v2 solve"
_MISSING = "not recorded"
_SUMMARY_VALUES = 10
_VALUES: dict[str, tuple[str, ...]] = {
    "kinetics_method": ("chain", "mean-field", "kMC", "none"),
    "failed_observables": ("yes", "no", _MISSING),
    "failure_class": (
        "none",
        "no positive steady state",
        "chain fallback",
        "mean-field failed",
        _MISSING,
    ),
    "bep_flux_share": ("0", "up to 0.1", "up to 0.5", "above 0.5", "none"),
    "kinetics_trusted": ("true", "false", _MISSING),
    "environment_basis": (_MISSING,),
}

# EXISTS avoids duplicate pathway rows when several candidates link one run.
# candidate_ref covers pre-harvest runs; related-to covers shared results.
_QUEST_SCOPE = """
AND (
    EXISTS (SELECT 1 FROM links direct
            WHERE direct.src_ref_id = p.ref_id
              AND direct.dst_ref_id = %(quest_id)s AND direct.relation = 'serves')
    OR EXISTS (
        SELECT 1 FROM links serves
        JOIN refs candidate ON candidate.ref_id = serves.src_ref_id
        WHERE serves.dst_ref_id = %(quest_id)s AND serves.relation = 'serves'
          AND candidate.kind = 'structure' AND candidate.retired_at IS NULL
          AND (
              p.meta->>'candidate_ref' = candidate.ref_id::text
              OR EXISTS (SELECT 1 FROM links related
                         WHERE related.relation = 'related-to'
                           AND ((related.src_ref_id = candidate.ref_id
                                 AND related.dst_ref_id = p.ref_id)
                                OR (related.dst_ref_id = candidate.ref_id
                                    AND related.src_ref_id = p.ref_id)))
          )
    )
)
"""

_PROJECTION = """
WITH records AS (
    SELECT p.ref_id,
           CASE WHEN jsonb_typeof(p.meta #> '{{results,kinetics_v2}}') = 'object'
                THEN p.meta #> '{{results,kinetics_v2}}'
                WHEN jsonb_typeof(p.meta->'kinetics_v2') = 'object'
                THEN p.meta->'kinetics_v2' END AS v2,
           p.meta->'kinetics_trusted' AS trusted,
           p.meta #>> '{{results,kinetics,environment_basis}}' AS basis
    FROM refs p
    WHERE p.kind = 'pathway' AND p.retired_at IS NULL
    {scope}
)
SELECT ref_id, v2 IS NOT NULL AND v2 <> '{{}}'::jsonb AS has_v2,
       v2 #> '{{tof_kmc,median}}' AS kmc,
       v2->'tof_mf' AS mf, v2->'tof_chain' AS chain,
       v2 #> '{{mf_status,ok}}' AS mf_ok,
       CASE WHEN jsonb_typeof(v2 #> '{{mf_status,failed_observables}}') = 'array'
            THEN jsonb_array_length(v2 #> '{{mf_status,failed_observables}}')
            END AS failed_observables,
       v2 #>> '{{mf_status,diagnosis,kind}}' AS diagnosis,
       v2 #> '{{mf_status,diagnosis,covering}}' AS covering,
       v2 #>> '{{mf_status,tof_sign,class}}' AS tof_sign,
       v2->'bep_flux_share' AS bep_flux_share,
       trusted, basis
FROM records ORDER BY ref_id
"""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _classes(row: dict[str, Any]) -> dict[str, str]:
    if not row["has_v2"]:
        return dict.fromkeys(_VALUES, _NO_V2)
    kmc, mf, chain = (_number(row[k]) for k in ("kmc", "mf", "chain"))
    if kmc is not None and kmc > 0:
        method, tof = "kMC", kmc
    elif mf is not None:
        method, tof = "mean-field", mf
    elif chain is not None:
        method, tof = "chain", chain
    else:
        method, tof = "none", None

    diagnosis = row["diagnosis"]
    if diagnosis == "poisoned_surface":
        covering = row["covering"]
        species = (
            sorted({s for s in covering if isinstance(s, str) and s})
            if isinstance(covering, list)
            else []
        )
        failure = "poisoned surface: " + (", ".join(species) or _MISSING)
    elif diagnosis:
        failure = "diagnosis: " + diagnosis
    elif tof is not None and tof <= 0 and row["tof_sign"]:
        failure = "tof_sign: " + row["tof_sign"]
    elif tof is not None and tof <= 0:
        failure = "no positive steady state"
    elif row["mf_ok"] is False:
        failure = "chain fallback" if method == "chain" else "mean-field failed"
    else:
        failure = "none" if row["mf_ok"] is True else _MISSING

    share = _number(row["bep_flux_share"])
    band = "none"
    if share is not None and share >= 0:
        if share == 0:
            band = "0"
        elif share <= 0.1:
            band = "up to 0.1"
        elif share <= 0.5:
            band = "up to 0.5"
        else:
            band = "above 0.5"
    failed = row["failed_observables"]
    trusted = row["trusted"]
    return {
        "kinetics_method": method,
        "failed_observables": _MISSING if failed is None else "yes" if failed else "no",
        "failure_class": failure,
        "bep_flux_share": band,
        "kinetics_trusted": (
            "true" if trusted is True else "false" if trusted is False else _MISSING
        ),
        "environment_basis": row["basis"] or _MISSING,
    }


def _page_arg(args: dict[str, Any], name: str, default: int, maximum: int) -> int:
    value = args.get(name, default)
    minimum = 0 if name == "offset" else 1
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise BadInput(
            f"kinetics-summary {name} must be an integer from {minimum} to {maximum}",
            next=f"args={{{name!r}: {default}}}",
        )
    return value


def _label(axis: str, value: str) -> str:
    if axis == "environment_basis" and value not in {_MISSING, _NO_V2}:
        return value[:12]
    return value


def kinetics_summary(
    store: Store, *, quest_id: int | None = None, args: dict[str, Any] | None = None
) -> str:
    """Count live pathways, page an axis's values, or list a selected class.

    The class selector accepts two spellings (gr592637): the legacy
    ``args={'class': '<axis>=<value>'}`` (pathway-side callers) and the
    table-lens ``args={'where': {'<axis>': '<value>'|[<values>]}}`` — the
    same selection, ANDed across axes when several are given."""
    args = dict(args or {})
    unknown = sorted(set(args) - {"axis", "class", "where", "page_size", "offset"})
    if unknown:
        raise BadInput(
            f"kinetics-summary does not accept args keys: {', '.join(unknown)}",
            options=["axis", "class", "where", "page_size", "offset"],
            next="use args={'where': {'failed_observables': 'yes'}, 'page_size': 20, 'offset': 0}",
        )
    page_size = _page_arg(args, "page_size", 20, 100)
    offset = _page_arg(args, "offset", 0, 2**63 - 1)
    selector = args.get("class")
    where = args.get("where")
    if where is not None and not isinstance(where, dict):
        raise BadInput(
            f"kinetics-summary where must be a dict {{axis: value|[values]}}, "
            f"got {where!r}",
            next="use args={'where': {'failed_observables': 'yes'}}",
        )
    axis_arg = args.get("axis")
    if "axis" in args:
        if not isinstance(axis_arg, str) or axis_arg not in _VALUES:
            raise BadInput(
                f"unknown kinetics-summary axis {axis_arg!r}",
                options=list(_VALUES),
                next="use args={'axis': 'environment_basis', 'offset': 0}",
            )
        if "class" in args or "where" in args:
            raise BadInput(
                "kinetics-summary axis and class/where are mutually exclusive",
                next="use args={'axis': 'environment_basis', 'offset': 0}",
            )
    if "class" in args and "where" in args:
        raise BadInput(
            "kinetics-summary class and where are mutually exclusive",
            next="use args={'where': {'failed_observables': 'yes'}}",
        )
    with store.pool.connection() as conn:
        cursor = conn.execute(
            _PROJECTION.format(scope=_QUEST_SCOPE if quest_id is not None else ""),
            {"quest_id": quest_id},
        )
        columns = [column.name for column in cursor.description or ()]
        rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    classified = [(row["ref_id"], _classes(row)) for row in rows]
    counts = {
        axis: Counter({_NO_V2: 0, **dict.fromkeys(values, 0)})
        for axis, values in _VALUES.items()
    }
    for _, classes in classified:
        for axis, value in classes.items():
            counts[axis][value] += 1
    valid = sorted(
        f"{axis}={value}" for axis, values in counts.items() for value in values
    )
    scope = f"quest qu{quest_id}" if quest_id is not None else "all live pathways"
    header = f"Kinetics summary: {len(rows)} pathways ({scope})"
    call = (
        f"get(kind='quest', id='qu{quest_id}', view='kinetics-summary'"
        if quest_id is not None
        else "get(kind='pathway', view='kinetics-summary'"
    )
    if "class" in args or "where" in args:
        # selector_map: {axis: {accepted values}} — the legacy class string
        # is one axis=value; the lens where is {axis: value|[values]}
        # ANDed (gr592637).
        selector_map: dict[str, set[str]] = {}
        label = ""
        if "class" in args:
            axis, value = "", ""
            if isinstance(selector, str):
                axis, _, value = selector.partition("=")
            selected: set[str] = set()
            if selector in valid:
                selected.add(value)
            elif axis == "environment_basis" and len(value) == 12:
                selected.update(
                    basis
                    for basis in counts[axis]
                    if basis not in {_MISSING, _NO_V2} and basis.startswith(value)
                )
            selector_map[axis] = selected
            label = str(selector)
        else:
            for w_axis, cond in (where or {}).items():
                vals = (
                    list(cond)
                    if isinstance(cond, (list, tuple, set, frozenset))
                    else [cond]
                )
                selected = set()
                for v in vals:
                    if f"{w_axis}={v}" in valid:
                        selected.add(str(v))
                    elif w_axis == "environment_basis":
                        selected.update(
                            basis
                            for basis in counts[w_axis]
                            if basis not in {_MISSING, _NO_V2}
                            and basis.startswith(str(v))
                        )
                selector_map[str(w_axis)] = selected
            label = f"where={where!r}"
        empty = [a for a, vs in selector_map.items() if not vs]
        if empty:
            raise BadInput(
                f"unknown kinetics-summary selector {label!r} — no valid "
                f"values on axis {empty[0]!r}; valid axis=value pairs: "
                + ", ".join(valid),
                options=valid,
                next=f"{call}, args={{'where': {{'failed_observables': 'yes'}}}})",
            )
        matches = [
            ref_id
            for ref_id, classes in classified
            if all(classes.get(ax) in vs for ax, vs in selector_map.items())
        ]
        page = matches[offset : offset + page_size]
        lines = [
            header,
            f"{label}: {len(matches)} total; {len(page)} shown; offset {offset}",
            "",
            "pathway",
        ]
        lines.extend(f"pw{ref_id}" for ref_id in page)
        if offset + len(page) < len(matches):
            next_args = dict(
                {"class": selector} if "class" in args else {"where": where}
            )
            next_args.update({"page_size": page_size, "offset": offset + page_size})
            lines += ["", f"next: {call}, args={next_args!r})"]
        return "\n".join(lines)

    if isinstance(axis_arg, str):
        ranked_values = sorted(
            counts[axis_arg].items(), key=lambda item: (-item[1], item[0])
        )
        page_values = ranked_values[offset : offset + page_size]
        lines = [
            header,
            f"{axis_arg}: {len(ranked_values)} values; {len(page_values)} shown; offset {offset}",
            "",
            f"## {axis_arg}",
            "class | count",
        ]
        lines.extend(
            f"{axis_arg}={_label(axis_arg, value)} | {count}"
            for value, count in page_values
        )
        if offset + len(page_values) < len(ranked_values):
            next_args = {
                "axis": axis_arg,
                "page_size": page_size,
                "offset": offset + page_size,
            }
            lines += ["", f"next: {call}, args={next_args!r})"]
        return "\n".join(lines)

    lines = [
        header,
        "Each axis totals the population; copy a value into args={'where': {'<axis>': '<value>'}}.",
    ]
    for axis, values in counts.items():
        ranked = sorted(values.items(), key=lambda item: (-item[1], item[0]))
        limited = [item for item in ranked if item[0] != _NO_V2]
        shown = {value for value, _ in limited[:_SUMMARY_VALUES]} | {_NO_V2}
        lines += ["", f"## {axis}", "class | count"]
        lines.extend(
            f"{axis}={_label(axis, value)} | {count}"
            for value, count in ranked
            if value in shown
        )
        remaining = limited[_SUMMARY_VALUES:]
        if remaining:
            axis_args = {"axis": axis, "offset": ranked.index(remaining[0])}
            lines.append(
                f"other: {len(remaining)} values, {sum(count for _, count in remaining)} pathways; "
                f"{call}, args={axis_args!r})"
            )
    lines += [
        "",
        "Failure precedence: stored diagnosis, TOF sign, non-positive TOF, chain fallback, other failed MF.",
        "Unresolved slow mode omitted: no structured class found; warning/reason prose is not parsed.",
        "kinetics_trusted reads the pathway's stored verdict only; job verdicts cannot be tied reliably to the current solve.",
        "Environment basis shows 12 characters of the stored v1 hash; class accepts the full hash or prefix (all matches).",
        "Absent hashes and observables are not recorded, not clean/default.",
    ]
    return "\n".join(lines)
