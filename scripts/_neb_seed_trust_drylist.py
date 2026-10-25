"""neb-seed-trust-drylist — read-only list of stored pathways whose barrier
would flip trusted→untrusted under the seed-independence gate (gr517804).

Pages live ``pathway`` refs from prod via ``scripts/prod-psql --ro`` (CSV over
``COPY``), replays the ruling — a neb-tier run needs ≥2 DISTINCT
same-``autocatpath_version`` seeds in the UNION pooled across its ready
siblings (same ``candidate_ref`` + engine + tier + IS-geometry + route/config
— ``trust_gate.sibling_key``, the one function shared with harvest and
``view='trust'``), where a run that never completed contributes nothing and
an unknowable seed set is ``seeds:unknown``, AND replays endpoint agreement
over the union (round-2 R1 — ``endpoint:union-not-run`` /
``endpoint:union-disagree`` via ``trust_gate.endpoint_union_blocker``) — and
prints each pathway that
would fire, flagging a real ``trusted->untrusted`` flip only when the firing
pathway is the one the candidate's barrier was harvested from (the latest
ready pathway at the candidate's ``barrier_fidelity`` tier). SELECT only;
stamps nothing.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PROD_PSQL = Path(__file__).resolve().parent / "prod-psql"
PAGE = 500
# --ref mode re-fetches every pathway row sharing the named rows'
# candidate_refs so the sibling pool is complete; bound it anyway.
_SIBLING_FETCH_LIMIT = 10000

# Columns: 0 ref_id, 1 candidate_ref, 2 engine, 3 declared seeds (jsonb),
# 4 non-stale record seeds (jsonb), 5 ALL record seeds (jsonb), 6
# jsonb_typeof(results.trust), 7 tier stamp, 8-10 screening/template/
# neb_schedule (the _pathway_tier fallback inputs), 11 status, 12
# reaction_key, 13 config (jsonb — the pre-stamp route fallback), 14
# validation_manifest slab sha, 15 structure_refs (jsonb — the older
# geometry identity), 16 candidate barrier_trusted, 17 barrier_fidelity,
# 18 route_steps (jsonb — the union replay's route scope), 19 non-stale
# endpoint_identity records (jsonb — pooled for the union agreement
# replay), 20 stamped endpoint tol_eV. The record aggregates are
# CASE-guarded on jsonb_typeof='array': a row storing results.trust as an
# object would otherwise abort the scan.
_SELECT = (
    "COPY (SELECT r.ref_id, r.meta->>'candidate_ref', "
    "r.meta->>'autocatpath_version', "
    "r.meta->'results'->'seeds', "
    "CASE WHEN jsonb_typeof(r.meta->'results'->'trust') = 'array' THEN "
    "(SELECT jsonb_agg(DISTINCT (rec->>'seed')::int) "
    "FROM jsonb_array_elements(r.meta->'results'->'trust') rec "
    "WHERE jsonb_typeof(rec->'seed') = 'number' "
    "AND rec->>'seed' ~ '^-?[0-9]+$' "
    "AND (r.meta->>'autocatpath_version' IS NULL OR "
    "COALESCE(rec->>'engine', rec->>'autocatpath_version', "
    "r.meta->>'autocatpath_version') = r.meta->>'autocatpath_version')) END, "
    "CASE WHEN jsonb_typeof(r.meta->'results'->'trust') = 'array' THEN "
    "(SELECT jsonb_agg(DISTINCT (rec->>'seed')::int) "
    "FROM jsonb_array_elements(r.meta->'results'->'trust') rec "
    "WHERE jsonb_typeof(rec->'seed') = 'number' "
    "AND rec->>'seed' ~ '^-?[0-9]+$') END, "
    "jsonb_typeof(r.meta->'results'->'trust'), "
    "r.meta->>'tier', "
    "COALESCE(r.meta->'results'->>'screening', ''), "
    "COALESCE(r.meta->'results'->>'template', ''), "
    "r.meta->'results' ? 'neb_schedule', "
    "r.meta->>'status', "
    "r.meta->>'reaction_key', "
    "r.meta->'config', "
    "r.meta->'validation_manifest'->'inputs'->>'slab_extxyz_sha256', "
    "r.meta->'structure_refs', "
    "COALESCE(c.meta->>'barrier_trusted', ''), "
    "COALESCE(c.meta->>'barrier_fidelity', ''), "
    "r.meta->'results'->'route_steps', "
    "CASE WHEN jsonb_typeof(r.meta->'results'->'trust') = 'array' THEN "
    "(SELECT jsonb_agg(rec) "
    "FROM jsonb_array_elements(r.meta->'results'->'trust') rec "
    "WHERE rec->>'check' = 'endpoint_identity' "
    "AND (r.meta->>'autocatpath_version' IS NULL OR "
    "COALESCE(rec->>'engine', rec->>'autocatpath_version', "
    "r.meta->>'autocatpath_version') = r.meta->>'autocatpath_version')) END, "
    "CASE WHEN jsonb_typeof(r.meta->'results'->'trust') = 'array' THEN "
    "(SELECT (rec->'evidence'->>'tol_eV')::float8 "
    "FROM jsonb_array_elements(r.meta->'results'->'trust') rec "
    "WHERE rec->>'check' = 'endpoint_agreement' "
    "AND jsonb_typeof(rec->'evidence'->'tol_eV') = 'number' LIMIT 1) END "
    "FROM refs r "
    "LEFT JOIN refs c ON c.ref_id::text = r.meta->>'candidate_ref' "
    "WHERE r.kind = 'pathway' AND r.retired_at IS NULL AND %s "
    "ORDER BY r.ref_id LIMIT %d) TO STDOUT WITH CSV;"
)


def _psql(sql: str) -> str:
    env = {**os.environ, "PRECIS_PROD_PSQL_OPTS": "-q"}
    res = subprocess.run(
        [str(PROD_PSQL), "--ro", sql],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=600,
    )
    if res.returncode != 0:
        sys.exit(f"prod-psql failed ({res.returncode}): {res.stderr.strip()}")
    return res.stdout


def _seed_set(text: str) -> set[int] | None:
    """A jsonb seed-array column — the int set (possibly empty);
    ``None`` when absent, empty, or not a JSON array."""
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, list):
        return None
    try:
        return {int(s) for s in parsed}
    except (TypeError, ValueError):
        return None


def _json_obj(text: str) -> dict[str, Any] | None:
    """A jsonb object column (config / structure_refs) — ``None`` when
    absent or not an object."""
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _row_meta(row: list[str]) -> dict[str, Any]:
    """Rebuild the meta shape ``trust_gate.sibling_key`` and
    ``_pathway_tier`` read — the shared grouping code (item 3: one
    function, an empty candidate_ref or engine pools nothing)."""
    results: dict[str, Any] = {}
    if row[8] == "true":
        results["screening"] = True
    if row[9]:
        results["template"] = row[9]
    if row[10] == "t":
        results["neb_schedule"] = {}
    meta: dict[str, Any] = {
        "candidate_ref": (
            int(row[1]) if row[1].lstrip("-").isdigit() else (row[1] or None)
        ),
        "autocatpath_version": row[2] or None,
        "tier": row[7] or None,
        "status": row[11] or None,
        "reaction_key": row[12] or None,
        "config": _json_obj(row[13]),
        "structure_refs": _json_obj(row[15]),
        "results": results,
    }
    if row[14]:
        meta["validation_manifest"] = {"inputs": {"slab_extxyz_sha256": row[14]}}
    return meta


def _run(row: list[str]) -> set[int] | None:
    """``trust_gate.run_seeds`` semantics on the fetched columns: live
    (non-stale) record seeds win; seeded-but-all-stale records mean the
    run sampled nothing at this engine → empty; the declared list counts
    only when NO record carries a seed (a non-array ``results.trust``
    counts as no records — item 11). ``None`` = unknowable."""
    all_rec = _seed_set(row[5])
    if all_rec is not None:
        return _seed_set(row[4]) or set()
    return _seed_set(row[3])


def _tier(meta: dict[str, Any]) -> str:
    """``_pathway_tier`` over the rebuilt meta — the same ladder-tier
    read the harvest uses."""
    from precis.quest.compute import _pathway_tier

    return _pathway_tier(meta)


def _rows(where: str, limit: int) -> list[list[str]]:
    return list(csv.reader(io.StringIO(_psql(_SELECT % (where, limit)))))


def _json_list(text: str) -> list[Any] | None:
    """A jsonb array column (route_steps / endpoint_identity records) —
    the parsed list, ``None`` when absent or not an array."""
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, list) else None


def _identity(row: list[str]) -> list[dict[str, Any]]:
    """The row's non-stale ``endpoint_identity`` records (column 19) —
    the record dicts the union agreement replay consumes."""
    return [r for r in (_json_list(row[19]) or []) if isinstance(r, dict)]


def _evaluate(
    rows: list[list[str]],
    *,
    named: set[int] | None = None,
    all_fires: bool = False,
) -> tuple[list[str], int, int, int, int]:
    """The gate replay over fetched rows — pure, so tests feed fixture
    rows without prod. Returns ``(lines, fired, flips, scanned,
    skipped_trust)`` where ``skipped_trust`` counts rows storing
    ``results.trust`` as a non-array (skipped for seed evidence, item
    11). Only READY rows contribute seeds — and their current-engine
    ``endpoint_identity`` records — to a row's sibling pool; every row's
    union is its own run plus its key's pool, and endpoint agreement is
    replayed over the union exactly like the harvest (R1).
    ``named`` filters the printed lines (``--ref`` mode scans the
    siblings too but reports on the named rows)."""
    from precis.utils import trust_gate

    metas = [_row_meta(r) for r in rows]
    keys = [trust_gate.sibling_key(m) for m in metas]
    runs = [_run(r) for r in rows]
    idents = [_identity(r) for r in rows]
    scopes = [trust_gate.route_scope({"route_steps": _json_list(r[18])}) for r in rows]
    # Per-row sibling context: the OTHER ready same-key rows' seeds and
    # endpoint_identity records (restricted to seeds those rows ran —
    # the mirror of trust_gate.sibling_evidence).
    by_key: dict[tuple[str, str, str, str, str], list[int]] = {}
    for i in range(len(rows)):
        if keys[i] is None or metas[i].get("status") != "ready" or not runs[i]:
            continue
        by_key.setdefault(keys[i], []).append(i)
    sib_seeds: list[set[int]] = [set() for _ in rows]
    sib_ident: list[list[dict[str, Any]]] = [[] for _ in rows]
    for i in range(len(rows)):
        if keys[i] is None:
            continue
        for j in by_key.get(keys[i], []):
            if j == i:
                continue
            rj = runs[j]
            if not rj:
                continue
            sib_seeds[i] |= rj
            sib_ident[i] += [rec for rec in idents[j] if rec.get("seed") in rj]
    # The candidate's barrier came from its LATEST ready pathway at the
    # barrier_fidelity tier (harvest lands oldest-first; canonicalization
    # keeps the stamped fidelity). A firing pathway that isn't that
    # source never counts as a trusted->untrusted flip (item 9).
    tiers = [_tier(m) for m in metas]
    source: dict[str, int] = {}
    for i, r in enumerate(rows):
        cand = r[1]
        if not cand or metas[i].get("status") != "ready":
            continue
        fidelity = r[17] or "neb"
        if tiers[i] != fidelity:
            continue
        source[cand] = max(source.get(cand, -1), int(r[0]))

    lines: list[str] = []
    fired = flips = skipped_trust = 0
    for i, row in enumerate(rows):
        if row[6] and row[6] != "array":
            skipped_trust += 1
        if tiers[i] != "neb":
            continue
        run = runs[i]
        union = (run or set()) | sib_seeds[i]
        if run is None:
            blocker: str | None = "seeds:unknown"
        elif len(union) >= 2:
            blocker = None
            if sib_seeds[i]:
                # R1: pooled seeds must also AGREE at the endpoints —
                # the same replay harvest and view='trust' run.
                tol = float(row[20]) if row[20] else trust_gate.endpoint_tol_ev()
                blocker = trust_gate.endpoint_union_blocker(
                    idents[i],
                    sib_ident[i],
                    scopes[i],
                    run=run,
                    union=union,
                    engine=row[2] or None,
                    tol_eV=tol,
                )[0]
        else:
            blocker = f"seeds:{len(union)}-independent"
        if blocker is None:
            continue
        fired += 1
        ref_id = int(row[0])
        trusted = row[16] == "true"
        flip = trusted and source.get(row[1]) == ref_id
        if flip:
            flips += 1
        if named is not None and ref_id not in named:
            continue
        if not all_fires and not trusted:
            continue
        mark = "  <-- trusted->untrusted" if flip else ""
        lines.append(
            f"pw{ref_id}  st{row[1] or '?'}  engine {row[2] or '?'}  "
            f"tier {tiers[i]}  run {sorted(run) if run is not None else '?'}  "
            f"union {sorted(union)}  blocker {blocker}  "
            f"barrier_trusted={row[16] or '?'}{mark}"
        )
    return lines, fired, flips, len(rows), skipped_trust


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="max pathways scanned — the sibling pool covers only the "
        "scanned prefix, so tail rows can over-fire (their later "
        "siblings are out of scope)",
    )
    ap.add_argument(
        "--ref", type=int, action="append", default=[], help="pathway ref_id"
    )
    ap.add_argument(
        "--all-fires",
        action="store_true",
        help="list every pathway the gate would fire on, not just the "
        "currently-trusted ones",
    )
    args = ap.parse_args()

    named: set[int] | None = None
    if args.ref:
        # --ref needs the candidates' SIBLINGS too — the pooled union is
        # only computable with them in scope (item 2).
        named = {int(i) for i in args.ref}
        ids = ",".join(str(i) for i in sorted(named))
        first = _rows(f"r.ref_id = ANY(ARRAY[{ids}]::bigint[])", len(named))
        cands = sorted({r[1] for r in first if r[1] and r[1].isdigit()})
        by_id = {int(r[0]): r for r in first}
        if cands:
            cand_ids = ",".join(f"'{c}'" for c in cands)
            for r in _rows(
                f"r.meta->>'candidate_ref' = ANY(ARRAY[{cand_ids}]::text[])",
                _SIBLING_FETCH_LIMIT,
            ):
                by_id[int(r[0])] = r
        rows = [by_id[k] for k in sorted(by_id)]
    else:
        rows = []
        after = 0
        while args.limit is None or len(rows) < args.limit:
            take = PAGE if args.limit is None else min(PAGE, args.limit - len(rows))
            page = _rows(f"r.ref_id > {after}", take)
            if not page:
                break
            rows.extend(page)
            after = int(page[-1][0])

    lines, fired, flips, scanned, skipped_trust = _evaluate(
        rows, named=named, all_fires=args.all_fires
    )
    for line in lines:
        print(line)
    scope = f"{scanned} pathways"
    if named is not None:
        scope += f" ({len(named)} named; the rest are sibling context)"
    if skipped_trust:
        scope += f"; {skipped_trust} carried non-array results.trust (skipped)"
    print(f"scanned {scope}; gate fires on {fired}; {flips} flip trusted->untrusted")


if __name__ == "__main__":
    main()
