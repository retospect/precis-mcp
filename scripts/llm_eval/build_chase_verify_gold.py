"""Build a ``chase:verify`` gold set from the route-log (read-only) for ``score_verdict``.

``chase:verify`` (``workers/_chase_llm._verify_support_with_caveats``) is ~96% of
MEDIUM-tier traffic. Its prompt is assembled inline from live chunk text, the
claim, neighbours and a source-identity block, so it cannot be rebuilt from stored
rows alone. It does not need to be: every dispatch is logged verbatim in
``llm_call_log`` (``request_hash`` -> ``llm_blob``: the serialized
``LlmRequest``, whose ``prompt`` IS the production prompt; ``response_hash`` ->
the incumbent's reply). This script samples already-verified calls, takes the
logged ``prompt`` byte-for-byte (the production function's output, so no
re-templating drift) and emits a ``GoldTask`` (axis ``tool-structured`` — the
nearest catalog axis for a strict-JSON verdict; scorer ``verdict``) whose
``prompt`` replays it against a candidate model.

Eligible rows: ``source = 'chase:verify'``, not errored, ``data_parsed`` true,
the incumbent model (``--model-like``, default ``%haiku%``), both blobs
present, and an incumbent reply whose ``supports`` is yes / partial / no.
Duplicate requests (same ``request_hash``) collapse to the latest call.

Read-only: one transaction opened with ``SET TRANSACTION READ ONLY``
(transaction-scoped; never a session-level SET, which would poison
pgbouncer's pooled server connections), plain SELECTs only. Pass 1 reads just
the (small) response blobs to label and stratify; pass 2 fetches the (large)
request blobs of the chosen rows.

Bias: ``expect.supports`` is the *incumbent's* stored verdict, not ground
truth. A candidate that disagrees with Haiku on a borderline chunk is scored
wrong even when it is right, so the score is agreement-with-incumbent. The
reply is parsed with the production parser (``claude_p._parse_last_json_block``).

Stratification (so minority verdicts are exercised rather than the dominant
verdict swamping the set): four strata with target shares yes 30%, partial 30%,
no+contradicts 15%, no-other 25% (n=40 -> 12/12/6/10). A stratum with too few
rows gives its slots to the others, largest-remaining first. The realised
split is printed to stderr and stored in each task's ``expect.stratum``.

The output holds real paper text: it defaults to ``gold_set/local/`` which is
gitignored (the repo is PUBLIC) — never commit it.

    uv run python scripts/llm_eval/build_chase_verify_gold.py --n 40 --seed 1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

AXIS = "tool-structured"
SCORER = "verdict"
SOURCE = "chase:verify"
DEFAULT_OUT = "scripts/llm_eval/gold_set/local/chase_verify_v1.json"

#: stratum -> share of ``n`` (sums to 1).
STRATA: dict[str, float] = {
    "yes": 0.30,
    "partial": 0.30,
    "no_contradicts": 0.15,
    "no_other": 0.25,
}
VERDICTS = ("yes", "partial", "no")

# Pass 1: labels only. Newest first per request_hash, capped so a busy week
# stays bounded.
_LABEL_SQL = """
    SELECT DISTINCT ON (l.request_hash)
           l.id, l.request_hash, l.ts, l.model, rb.text AS response
      FROM llm_call_log l
      JOIN llm_blob rb ON rb.hash = l.response_hash
     WHERE l.source = %(source)s
       AND NOT l.errored
       AND l.data_parsed
       AND l.model ILIKE %(model_like)s
       AND l.request_hash IS NOT NULL
       AND l.ts >= now() - make_interval(days => %(days)s)
     ORDER BY l.request_hash, l.ts DESC
     LIMIT %(cap)s
"""

_REQUEST_SQL = "SELECT hash, text FROM llm_blob WHERE hash = ANY(%(hashes)s)"


def parse_verdict(response: str) -> dict[str, Any] | None:
    """The incumbent reply's verdict dict via the production parser, else None."""
    from precis.utils.claude_p import _parse_last_json_block

    data = _parse_last_json_block(response or "")
    if not data or data.get("supports") not in VERDICTS:
        return None
    return data


def stratum_of(verdict: dict[str, Any]) -> str:
    if verdict["supports"] != "no":
        return str(verdict["supports"])
    return "no_contradicts" if verdict.get("contradicts") is True else "no_other"


def _rank(seed: str, key: str) -> str:
    return hashlib.md5(f"{key}{seed}".encode()).hexdigest()


def allocate(avail: dict[str, int], n: int) -> dict[str, int]:
    """Per-stratum quota: target shares, shortfalls refilled largest-remaining first."""
    quota = {s: min(avail.get(s, 0), round(n * share)) for s, share in STRATA.items()}
    while sum(quota.values()) > n:
        quota[max(quota, key=lambda s: quota[s])] -= 1
    while sum(quota.values()) < n:
        spare = {s: avail.get(s, 0) - quota[s] for s in STRATA}
        s = max(spare, key=lambda k: spare[k])
        if spare[s] <= 0:
            break
        quota[s] += 1
    return quota


def stratify(
    labelled: list[tuple[str, dict[str, Any]]], *, n: int, seed: str
) -> list[tuple[str, str, dict[str, Any]]]:
    """``[(request_hash, verdict)]`` -> ``[(stratum, request_hash, verdict)]``, seeded."""
    by: dict[str, list[tuple[str, dict[str, Any]]]] = {s: [] for s in STRATA}
    for h, v in labelled:
        by[stratum_of(v)].append((h, v))
    quota = allocate({s: len(r) for s, r in by.items()}, n)
    out: list[tuple[str, str, dict[str, Any]]] = []
    for s, rows in by.items():
        rows.sort(key=lambda r: _rank(seed, r[0]))
        out += [(s, h, v) for h, v in rows[: quota[s]]]
    return out


def row_to_task(
    request_blob: str, response: str, *, stratum: str, request_hash: str
) -> dict[str, Any]:
    """One logged call (request blob + incumbent reply) -> a ``GoldTask`` JSON object."""
    req = json.loads(request_blob)
    verdict = parse_verdict(response) or {}
    task: dict[str, Any] = {
        "task_id": f"cvfy-{request_hash[:12]}",
        "axis": AXIS,
        "scorer": SCORER,
        # The logged prompt IS the production prompt (messages was unset in prod,
        # so a single user turn is exactly what the local wire sends too).
        "prompt": str(req["prompt"]),
        "expect": {
            "supports": verdict.get("supports"),
            "contradicts": verdict.get("contradicts"),
            "stratum": stratum,
            "incumbent": response,
        },
    }
    if req.get("messages"):
        task["messages"] = req["messages"]
    if isinstance(req.get("max_tokens"), int) and req["max_tokens"] > 0:
        task["max_tokens"] = req["max_tokens"]
    return task


def sample(
    conn: Any, *, n: int, seed: str, days: int, model_like: str, cap: int
) -> list[dict[str, Any]]:
    """Two read-only passes: label + stratify on responses, then fetch the chosen requests."""
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _LABEL_SQL,
            {"source": SOURCE, "model_like": model_like, "days": days, "cap": cap},
        )
        rows = cur.fetchall()
    responses = {r["request_hash"]: r["response"] for r in rows}
    labelled = [
        (h, v)
        for h, resp in responses.items()
        if (v := parse_verdict(resp)) is not None
    ]
    chosen = stratify(labelled, n=n, seed=seed)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_REQUEST_SQL, {"hashes": [h for _, h, _ in chosen]})
        blobs = {r["hash"]: r["text"] for r in cur.fetchall()}
    return [
        row_to_task(blobs[h], responses[h], stratum=s, request_hash=h)
        for s, h, _ in chosen
        if h in blobs
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--database-url", default=None, help="else PRECIS_DATABASE_URL")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", default="1")
    ap.add_argument("--days", type=int, default=7, help="route-log lookback")
    ap.add_argument("--model-like", default="%haiku%", help="incumbent model ILIKE")
    ap.add_argument("--cap", type=int, default=20000, help="max rows read in pass 1")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    from precis.cli._common import resolve_dsn
    from precis.store.store import Store

    store = Store.connect(resolve_dsn(args.database_url))
    try:
        with store.pool.connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            tasks = sample(
                conn,
                n=args.n,
                seed=str(args.seed),
                days=args.days,
                model_like=args.model_like,
                cap=args.cap,
            )
            conn.rollback()
    finally:
        store.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(tasks, indent=1, ensure_ascii=False), encoding="utf-8")
    split = {s: sum(1 for t in tasks if t["expect"]["stratum"] == s) for s in STRATA}
    print(f"wrote {len(tasks)} tasks {split} -> {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
