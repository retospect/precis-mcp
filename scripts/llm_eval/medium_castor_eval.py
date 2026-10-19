"""Castor MEDIUM-tier load eval — replay real MEDIUM calls through both routes.

MEASURE ONLY — no routing change. For the "castor load" question (gr489474):
would the MEDIUM tier's real workload hold up on castor's served
``openai/gpt-oss-120b`` instead of the current cloud rung? This script answers
with production traffic, not synthetic tasks: it samples logged calls from
``llm_call_log``/``llm_blob`` (the serialized ``LlmRequest`` IS the production
prompt, byte-for-byte — no re-templating drift), then replays each through

* **cloud** — the current prod rung 0: ``route()``'s default MEDIUM chain,
  ``claude_p`` → ``claude-haiku-4-5-20251001`` (the same ``claude -p`` OAuth
  lane prod runs; the chain's hosted-OSS/claude_agent fallback rungs are not
  exercised — an error is recorded, not covered up);
* **local** — the candidate route: ``route()`` with a single-rung
  ``PRECIS_LLM_CHAIN_MEDIUM`` local override, exactly the chain shape an
  operator flip would write (``transport: local``, ``reasoning_effort: low``,
  the proven ``llm.op.llm_summarize`` rung), dispatched to the card's
  ``served_by`` endpoint via ``LlmRequest.local_url`` — the same slot-pin
  mechanism prod uses, minus the ``resource_slots`` reservation (no prod
  writes: the endpoint is READ from the card, never hard-coded).

Both arms run through the real router seam, so rung semantics
(``_empty_length_is_failure``, per-rung ``max_tokens``/``timeout_s``) match a
real flip byte-for-byte.

Scoring: (1) decision agreement between the two arms — ``supports`` for
``chase:verify``, ``verdict`` for ``taproot:reground-judge``, the effective
pick for ``chase:locate``, paraphrase-count parity only for
``conflict_search:negate`` (no exact-match decision exists — the judge carries
it); (2) a blind frontier-model judge (``claude -p`` opus) on a seeded sample
of pairs, disagreements first. Latency is per-call wall clock; the local arm
runs serially by default so "GPU-seconds" ≈ server busy time.

Spend: ``cost_usd`` is summed over every paid call (cloud arm + judge) and the
run stops early at ``--cap-usd`` (default 15). The claude_p lane is OAuth, so
the figure is the reported equivalent — counted anyway.

Read-only on prod: ``SET TRANSACTION READ ONLY`` + plain SELECTs; the only
network writes are the LLM dispatches themselves. Raw output (real paper
text + answers) lands in the gitignored ``gold_set/local/`` dir — never
commit it. The endpoint URL is held in memory only, never written anywhere.

    uv run python scripts/llm_eval/medium_castor_eval.py --prod --n 50
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from precis.utils.claude_p import _parse_last_json_block
from precis.utils.llm.router import LlmRequest, Tier, route

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _prod_ro import add_db_args, connect_ro  # noqa: E402

#: Card + wire id of the candidate local model (the ``served_by`` card's
#: ``model_id``; the served name on the wire is the same id on this deploy).
LOCAL_MODEL = "openai/gpt-oss-120b"

#: The local rung an operator MEDIUM flip would write — mirrors the proven
#: ``llm.op.llm_summarize`` shape (gr489474 comment 4/6): low reasoning effort
#: bounds the thinking channel, max_tokens leaves room for the JSON answer,
#: timeout_s is the big-model wall clock the SMALL-tier cap lacked.
LOCAL_RUNG: dict[str, Any] = {
    "placement": "local",
    "model": LOCAL_MODEL,
    "transport": "local",
    "max_tokens": 2400,
    "timeout_s": 300,
    "reasoning_effort": "low",
}
LOCAL_CHAIN_ENV = "PRECIS_LLM_CHAIN_MEDIUM"

#: Judge model + per-call budget. FRONTIER resolves ``claude -p``; pinned so a
#: local ``llm.model.frontier``/env drift can't move the yardstick.
JUDGE_MODEL = "claude-opus-5"
JUDGE_MAX_USD = 1.0


#: One call site to evaluate: the route-log ``source`` it dispatches under,
#: the owning worker(s), a per-stratum share for sampling (so a dominant
#: verdict can't swamp the set), and a decision-field extractor for agreement.
class Site:
    def __init__(
        self,
        *,
        key: str,
        source: str,
        sites: str,
        n: int,
        strata: dict[str, float],
    ) -> None:
        self.key = key
        self.source = source
        self.sites = sites
        self.n = n
        self.strata = strata


def _verify_stratum(data: dict[str, Any]) -> str | None:
    supports = data.get("supports")
    if supports not in ("yes", "partial", "no"):
        return None
    if supports != "no":
        return str(supports)
    return "no_contradicts" if data.get("contradicts") is True else "no_other"


def _judge_stratum(data: dict[str, Any]) -> str | None:
    verdict = data.get("verdict")
    return str(verdict) if verdict in ("KEEP", "PRUNE", "CONTRADICTS") else None


def _locate_stratum(data: dict[str, Any]) -> str | None:
    if data.get("ok") is True:
        return "ok"
    if data.get("ok") is False:
        return "alt" if data.get("alternative_ord") is not None else "none"
    return None


def _any_stratum(data: dict[str, Any]) -> str | None:
    return "any" if isinstance(data.get("paraphrases"), list) else None


SITES: list[Site] = [
    Site(
        key="chase:verify",
        source="chase:verify",
        sites="inbound_chase + inbound_ground (shared verify hook)",
        n=24,
        strata={"yes": 0.30, "partial": 0.30, "no_contradicts": 0.15, "no_other": 0.25},
    ),
    Site(
        key="reground-judge",
        source="taproot:reground-judge",
        sites="hub_refine (judge_edge_strict)",
        n=14,
        strata={"KEEP": 0.40, "PRUNE": 0.40, "CONTRADICTS": 0.20},
    ),
    Site(
        key="negate",
        source="conflict_search:negate",
        sites="conflict_search (negate_claim)",
        n=8,
        strata={"any": 1.0},
    ),
    Site(
        key="locate",
        source="chase:locate",
        sites="inbound_chase (locate_chunk_in_target)",
        n=4,
        strata={"ok": 0.5, "alt": 0.25, "none": 0.25},
    ),
]

STRATUM_FNS: dict[str, Any] = {
    "chase:verify": _verify_stratum,
    "taproot:reground-judge": _judge_stratum,
    "conflict_search:negate": _any_stratum,
    "chase:locate": _locate_stratum,
}

#: Pass 1: label + stratify on incumbent replies only (the small blobs); the
#: chosen rows' request blobs come in pass 2. Distinct requests, newest first.
_LABEL_SQL = """
    SELECT DISTINCT ON (l.request_hash)
           l.id, l.request_hash, l.ts, l.model, rb.text AS response
      FROM llm_call_log l
      JOIN llm_blob rb ON rb.hash = l.response_hash
     WHERE l.source = %(source)s
       AND NOT l.errored
       AND l.data_parsed
       AND l.request_hash IS NOT NULL
       AND l.ts >= now() - make_interval(days => %(days)s)
     ORDER BY l.request_hash, l.ts DESC
     LIMIT %(cap)s
"""

_REQUEST_SQL = "SELECT hash, text FROM llm_blob WHERE hash = ANY(%(hashes)s)"

_CARD_SQL = """
    SELECT meta FROM refs
     WHERE kind = 'llm' AND meta->>'model_id' = %(model)s
     ORDER BY ref_id LIMIT 1
"""


def _rank(seed: str, key: str) -> str:
    return hashlib.md5(f"{key}{seed}".encode()).hexdigest()


def served_endpoint(conn: Any) -> tuple[str, str]:
    """The candidate's ``served_by`` endpoint + wire model name, read from the
    llm catalog card — the same config the slot machinery consults. Never
    printed or written by this script (the repo is public; the endpoint is a
    LAN address)."""
    from urllib.parse import urlparse

    row = conn.execute(_CARD_SQL, {"model": LOCAL_MODEL}).fetchone()
    if not row:
        raise SystemExit(f"no llm card with model_id={LOCAL_MODEL!r}")
    meta = row[0] or {}
    for entry in meta.get("served_by") or []:
        if not isinstance(entry, dict):
            continue
        ep = entry.get("endpoint")
        if not isinstance(ep, str) or not ep:
            continue
        host = urlparse(ep).hostname or ""
        if host in {"", "localhost", "127.0.0.1", "::1", "0.0.0.0"}:
            continue  # host-private — the cluster-scoped rule in local_serving
        return ep, str(entry.get("model") or LOCAL_MODEL)
    raise SystemExit(f"llm card {LOCAL_MODEL!r} has no routable served_by endpoint")


def parse_reply(text: str | None, data: Any) -> dict[str, Any] | None:
    """A reply's JSON object — the transport's parsed data first, else the
    production last-JSON-block parser (same one ``claude_p`` uses)."""
    if isinstance(data, dict):
        return data
    return _parse_last_json_block(text or "")


def sample_site(
    conn: Any, site: Site, *, seed: str, days: int, cap: int
) -> list[dict[str, Any]]:
    """Stratified sample of one source's logged calls → replay records."""
    from psycopg.rows import dict_row

    label = STRATUM_FNS[site.source]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _LABEL_SQL,
            {"source": site.source, "days": days, "cap": cap},
        )
        rows = cur.fetchall()
    labelled: list[tuple[str, str, dict[str, Any]]] = []  # (stratum, hash, row)
    for r in rows:
        v = parse_reply(r["response"], None)
        if v is None:
            continue
        s = label(v)
        if s is not None:
            labelled.append((s, r["request_hash"], r))

    by: dict[str, list[tuple[str, dict[str, Any]]]] = {s: [] for s in site.strata}
    for s, h, r in labelled:
        by.setdefault(s, []).append((h, r))
    quota = {
        s: min(len(by.get(s) or []), round(site.n * share))
        for s, share in site.strata.items()
    }
    while sum(quota.values()) > site.n:
        quota[max(quota, key=lambda k: quota[k])] -= 1
    while sum(quota.values()) < site.n:
        spare = {s: len(by.get(s) or []) - quota.get(s, 0) for s in site.strata}
        best = max(spare, key=lambda k: spare[k])
        if spare[best] <= 0:
            break
        quota[best] = quota.get(best, 0) + 1
    chosen: list[tuple[str, str, dict[str, Any]]] = []
    for s, q in quota.items():
        pool = sorted(by.get(s) or [], key=lambda hr: _rank(seed, hr[0]))
        chosen += [(s, h, r) for h, r in pool[:q]]

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_REQUEST_SQL, {"hashes": [h for _, h, _ in chosen]})
        blobs = {r["hash"]: r["text"] for r in cur.fetchall()}
    out = []
    for s, h, r in chosen:
        if h not in blobs:
            continue
        out.append(
            {
                "site": site.key,
                "source": site.source,
                "call_sites": site.sites,
                "stratum": s,
                "request_hash": h,
                "log_id": r["id"],
                "incumbent_model": r["model"],
                "incumbent_ts": str(r["ts"]),
                "request": json.loads(blobs[h]),
                "incumbent_response": r["response"],
            }
        )
    return out


def replay_fields(request_blob: dict[str, Any]) -> dict[str, Any]:
    """The ``LlmRequest`` fields a replay carries — the serialized request's
    own values, minus fields that don't exist on dispatch (``tier`` is pinned
    MEDIUM by this eval; ``timeout_s`` isn't serialized so it stays unset,
    matching every logged call but ``conflict_search``'s)."""
    req = request_blob
    return {
        "prompt": str(req.get("prompt") or ""),
        "messages": req.get("messages") or None,
        "source": str(req.get("source") or ""),
        "tools_needed": bool(req.get("tools_needed")),
        "max_tokens": req.get("max_tokens") or None,
        "thinking": req.get("thinking"),
        "temperature": req.get("temperature"),
        "log_call": False,
    }


def _arm_result(res: Any, duration_s: float) -> dict[str, Any]:
    return {
        "error": res.error,
        "text": res.text or "",
        "data": res.data if isinstance(res.data, dict) else None,
        "cost_usd": res.cost_usd,
        "model": res.model,
        "placement": res.placement,
        "duration_s": round(duration_s, 2),
        "stop_reason": getattr(res, "stop_reason", None),
        "input_tokens": getattr(res, "input_tokens", None),
        "output_tokens": getattr(res, "output_tokens", None),
    }


def run_cloud(call: dict[str, Any]) -> dict[str, Any]:
    """The incumbent route: prod MEDIUM rung 0 (claude_p → haiku)."""
    req = LlmRequest(tier=Tier.MEDIUM, **replay_fields(call["request"]))
    t0 = time.monotonic()
    res = route(req)
    return _arm_result(res, time.monotonic() - t0)


def run_local(call: dict[str, Any], endpoint: str) -> dict[str, Any]:
    """The candidate route: a single local rung on the card's served_by
    endpoint — ``route()`` with the operator env-chain shape, ``local_url``
    doing the slot-pin repoint (no slot reservation: it writes prod)."""
    os.environ[LOCAL_CHAIN_ENV] = json.dumps([LOCAL_RUNG])
    try:
        fields = replay_fields(call["request"])
        req = LlmRequest(tier=Tier.MEDIUM, local_url=endpoint, **fields)
        t0 = time.monotonic()
        res = route(req)
    finally:
        os.environ.pop(LOCAL_CHAIN_ENV, None)
    return _arm_result(res, time.monotonic() - t0)


def decision_of(source: str, data: dict[str, Any] | None) -> Any:
    """The agreement-comparable decision for one call site, or ``None`` when
    the reply is unparseable / out of contract."""
    if not data:
        return None
    if source == "chase:verify":
        s = data.get("supports")
        return ("supports", s) if s in ("yes", "partial", "no") else None
    if source == "taproot:reground-judge":
        v = data.get("verdict")
        return ("verdict", v) if v in ("KEEP", "PRUNE", "CONTRADICTS") else None
    if source == "chase:locate":
        if data.get("ok") is True:
            return ("pick", "main")
        if data.get("ok") is False:
            alt = data.get("alternative_ord")
            return ("pick", "none" if alt is None else f"alt:{alt}")
        return None
    if source == "conflict_search:negate":
        ps = data.get("paraphrases")
        if isinstance(ps, list):
            return ("n_paraphrases", len([p for p in ps if str(p).strip()]))
        return None
    return None


_JUDGE_PROMPT = """\
You are judging two answers to the SAME production LLM prompt from a
scientific-knowledge-graph pipeline. The call site is `{source}` — it must
return exactly one JSON object obeying the contract stated in the prompt.

PROMPT (verbatim, possibly truncated):
{prompt}

ANSWER A:
{a}

ANSWER B:
{b}

Judge which answer better satisfies the prompt's contract — the correct
verdict and faithful fields (or, for a generation task, the more useful
output). A malformed or empty answer loses. If both are equally good or
equally bad, say so.

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "winner": "A" | "B" | "tie" | "both_wrong",
  "reason": "<one sentence>"
}}
"""


def judge_pair(
    call: dict[str, Any], *, flip: bool, max_prompt_chars: int
) -> dict[str, Any]:
    """Blind A/B judge of one pair via FRONTIER ``claude -p``. Returns the
    judge's verdict mapped back onto (cloud, local) plus its cost."""
    cloud_text = call["cloud"]["text"] or f"(error: {call['cloud']['error']})"
    local_text = call["local"]["text"] or f"(error: {call['local']['error']})"
    a, b = (local_text, cloud_text) if flip else (cloud_text, local_text)
    prompt = _JUDGE_PROMPT.format(
        source=call["source"],
        prompt=str(call["request"].get("prompt") or "")[:max_prompt_chars],
        a=a[:6000],
        b=b[:6000],
    )
    req = LlmRequest(
        tier=Tier.FRONTIER,
        model=JUDGE_MODEL,
        prompt=prompt,
        max_usd=JUDGE_MAX_USD,
        source="castor-eval:judge",
        log_call=False,
    )
    t0 = time.monotonic()
    res = route(req)
    out = _arm_result(res, time.monotonic() - t0)
    verdict = parse_reply(res.text, res.data)
    winner = (verdict or {}).get("winner")
    if flip and winner in ("A", "B"):
        winner = "B" if winner == "A" else "A"
    out["judge"] = {
        "winner_raw": (verdict or {}).get("winner"),
        "winner": (
            {"A": "cloud", "B": "local"}.get(winner, winner) if winner else None
        ),
        "reason": (verdict or {}).get("reason"),
        "flipped": flip,
    }
    return out


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    i = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
    return round(s[i], 1)


def report_markdown(payload: dict[str, Any]) -> str:
    """The committed results doc — numbers only (no endpoint, no prompt text)."""
    lines = [
        "# Castor MEDIUM-tier load eval",
        "",
        payload["blurb"],
        "",
        "| site (route-log source) | n | agree% | judge L/C/tie/bad | cloud p50/p95 s | local p50/p95 s | cloud $/call | local GPU-s |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in payload["per_site"]:
        lines.append(
            f"| {row['site']} ({row['source']}) | {row['n']} | {row['agree']} | "
            f"{row['judge']} | {row['cloud_p50']}/{row['cloud_p95']} | "
            f"{row['local_p50']}/{row['local_p95']} | {row['cloud_cost']} | "
            f"{row['local_gpu_s']} |"
        )
    lines += ["", "## Notes", ""]
    lines += [f"- {n}" for n in payload["notes"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_db_args(ap)
    ap.add_argument("--n", type=int, default=50, help="total pairs (~per-site quotas)")
    ap.add_argument(
        "--judge", type=int, default=12, help="pairs for the frontier judge"
    )
    ap.add_argument("--seed", default="1")
    ap.add_argument("--days", type=int, default=30, help="route-log lookback")
    ap.add_argument(
        "--log-cap", type=int, default=4000, help="log rows read per source"
    )
    ap.add_argument("--cap-usd", type=float, default=15.0, help="paid spend ceiling")
    ap.add_argument("--cloud-workers", type=int, default=4)
    ap.add_argument("--local-workers", type=int, default=1)
    ap.add_argument("--judge-workers", type=int, default=3)
    ap.add_argument("--judge-prompt-chars", type=int, default=12000)
    ap.add_argument(
        "--out",
        default=str(HERE / "gold_set" / "local" / "medium_castor_eval.jsonl"),
        help="raw JSONL (gitignored default; holds real text — never commit)",
    )
    ap.add_argument(
        "--doc", default=None, help="optional markdown results doc (numbers only)"
    )
    ap.add_argument(
        "--sample-only",
        action="store_true",
        help="sample + report the plan; make no LLM calls",
    )
    args = ap.parse_args(argv)

    conn = connect_ro(args)
    try:
        endpoint, served_model = served_endpoint(conn)
        calls: list[dict[str, Any]] = []
        scale = args.n / sum(s.n for s in SITES)
        for site in SITES:
            n = max(1, round(site.n * scale))
            site_calls = sample_site(
                conn, site, seed=str(args.seed), days=args.days, cap=args.log_cap
            )[:n]
            calls += site_calls
            split: dict[str, int] = {}
            for c in site_calls:
                split[c["stratum"]] = split.get(c["stratum"], 0) + 1
            print(f"{site.source}: {len(site_calls)} sampled {split}", file=sys.stderr)
    finally:
        conn.rollback()
        conn.close()

    print(
        f"eval: {len(calls)} pairs over {len(SITES)} sources; "
        f"served_model={served_model} via card served_by endpoint",
        file=sys.stderr,
    )
    if args.sample_only or not calls:
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    judged_done: dict[str, Any] = {}
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "cloud" in r and "local" in r:
                done.add(r["request_hash"])
            if r.get("judge"):
                judged_done[r["request_hash"]] = r["judge"]
        calls = [c for c in calls if c["request_hash"] not in done]
        print(f"resume: {len(done)} done, {len(calls)} left", file=sys.stderr)

    spent = 0.0

    with open(out_path, "a", encoding="utf-8") as lock_out:

        def emit(rec: dict[str, Any]) -> None:
            lock_out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            lock_out.flush()

        # ── arm 1: cloud (prod rung 0) ──────────────────────────────────
        os.environ.pop(LOCAL_CHAIN_ENV, None)
        with ThreadPoolExecutor(max_workers=max(1, args.cloud_workers)) as ex:
            futures = {ex.submit(run_cloud, c): c for c in calls}
            for fut in futures:
                c = futures[fut]
                try:
                    c["cloud"] = fut.result()
                except Exception as exc:  # a crashed worker must not lose the pair
                    c["cloud"] = {
                        "error": f"harness:{exc}",
                        "text": "",
                        "duration_s": None,
                    }
                spent += c["cloud"].get("cost_usd") or 0.0
                if spent > args.cap_usd:
                    print(f"SPEND CAP hit at ${spent:.2f} — stopping", file=sys.stderr)
                    for f2 in futures:
                        f2.cancel()
                    break
        print(f"cloud arm done, spent ${spent:.3f}", file=sys.stderr)

        # ── arm 2: local (the candidate rung) ───────────────────────────
        local_todo = [c for c in calls if "cloud" in c]
        if args.local_workers <= 1:
            for i, c in enumerate(local_todo, 1):
                try:
                    c["local"] = run_local(c, endpoint)
                except Exception as exc:
                    c["local"] = {
                        "error": f"harness:{exc}",
                        "text": "",
                        "duration_s": None,
                    }
                print(f"local {i}/{len(local_todo)}", file=sys.stderr)
        else:
            with ThreadPoolExecutor(max_workers=args.local_workers) as ex:
                for c, res in zip(
                    local_todo, ex.map(lambda c: run_local(c, endpoint), local_todo)
                ):
                    c["local"] = res

        for c in local_todo:
            cloud_d = parse_reply(c["cloud"].get("text"), c["cloud"].get("data"))
            local_d = parse_reply(c["local"].get("text"), c["local"].get("data"))
            c["cloud_data"] = cloud_d
            c["local_data"] = local_d
            cd = decision_of(c["source"], cloud_d)
            ld = decision_of(c["source"], local_d)
            c["cloud_decision"] = cd
            c["local_decision"] = ld
            c["agreement"] = None if cd is None or ld is None else cd == ld
            # Emit a copy minus the request blob (reproducible from the log); the
            # judge below still needs the prompt text in memory.
            emit({k: v for k, v in c.items() if k != "request"})

        # ── judge: disagreements first, then fill per site ─────────────
        os.environ.pop(LOCAL_CHAIN_ENV, None)
        pairs = [
            c
            for c in local_todo
            if not c["cloud"].get("error") and not c["local"].get("error")
        ]
        disagree = [c for c in pairs if c["agreement"] is False]
        rest = [c for c in pairs if c["agreement"] is not False]
        disagree.sort(key=lambda c: _rank(str(args.seed) + "d", c["request_hash"]))
        rest.sort(key=lambda c: _rank(str(args.seed) + "a", c["request_hash"]))
        judge_set = disagree[: args.judge] + rest[: max(0, args.judge - len(disagree))]
        judged: list[dict[str, Any]] = list(judged_done.values())
        with ThreadPoolExecutor(max_workers=max(1, args.judge_workers)) as ex:
            futures = {
                ex.submit(
                    judge_pair,
                    c,
                    flip=bool(int(_rank(str(args.seed), c["request_hash"]), 16) % 2),
                    max_prompt_chars=args.judge_prompt_chars,
                ): c
                for c in judge_set
                if c["request_hash"] not in judged_done
            }
            for fut in futures:
                c = futures[fut]
                try:
                    j = fut.result()
                except Exception as exc:
                    j = {"error": f"harness:{exc}", "judge": {"winner": None}}
                spent += j.get("cost_usd") or 0.0
                rec = {"request_hash": c["request_hash"], "site": c["site"], **j}
                judged.append(rec)
                emit(rec)
                if spent > args.cap_usd:
                    print(f"SPEND CAP hit at ${spent:.2f} — stopping", file=sys.stderr)
                    for f2 in futures:
                        f2.cancel()
                    break
    judge_by_hash = {j["request_hash"]: j["judge"] for j in judged if j.get("judge")}
    for c in local_todo:
        c["judge"] = judge_by_hash.get(c["request_hash"])
    print(f"judge done ({len(judged)}), total spent ${spent:.3f}", file=sys.stderr)

    # ── report ──────────────────────────────────────────────────────────
    per_site = []
    for site in SITES:
        rows = [c for c in local_todo if c["source"] == site.source]
        if not rows:
            continue
        agr = [c["agreement"] for c in rows if c["agreement"] is not None]
        jv = [
            j["judge"]["winner"]
            for j in judged
            if j["site"] == site.key and j.get("judge")
        ]
        jw = {w: jv.count(w) for w in ("local", "cloud", "tie", "both_wrong")}
        cl = [c["cloud"]["duration_s"] for c in rows if c["cloud"].get("duration_s")]
        ll = [c["local"]["duration_s"] for c in rows if c["local"].get("duration_s")]
        cost = [c["cloud"].get("cost_usd") or 0.0 for c in rows]
        n_err = sum(
            1 for c in rows if c["cloud"].get("error") or c["local"].get("error")
        )
        per_site.append(
            {
                "site": site.key,
                "source": site.source,
                "call_sites": site.sites,
                "n": len(rows),
                "errors": n_err,
                "agree": f"{100 * sum(agr) / len(agr):.0f}% ({sum(agr)}/{len(agr)})"
                if agr
                else "n/a",
                "judge": f"{jw['local']}/{jw['cloud']}/{jw['tie']}/{jw['both_wrong']}",
                "cloud_p50": _pct(cl, 0.50),
                "cloud_p95": _pct(cl, 0.95),
                "local_p50": _pct(ll, 0.50),
                "local_p95": _pct(ll, 0.95),
                "cloud_cost": f"${sum(cost) / len(cost):.4f}" if cost else "n/a",
                "local_gpu_s": f"{sum(ll):.0f} ({statistics.mean(ll):.1f}/call)"
                if ll
                else "n/a",
            }
        )
    payload = {
        "blurb": (
            f"{len(local_todo)} real MEDIUM calls replayed side-by-side: the prod "
            "cloud rung (claude_p → haiku) vs the local rung (gpt-oss-120b on the "
            "served endpoint). judge = local/cloud/tie/both-wrong wins on the "
            "frontier-judged sample. GPU-s = sum of per-call wall times on the "
            f"local arm (serial={args.local_workers == 1}). Run "
            f"{time.strftime('%Y-%m-%d', time.gmtime())}."
        ),
        "per_site": per_site,
        "notes": [
            f"Total paid-equivalent spend ${spent:.3f} (cap ${args.cap_usd}).",
            "Cloud $/call is the claude -p OAuth envelope's reported cost_usd "
            "(subscription-equivalent, not metered API spend).",
            "Local arm = single-rung local chain + served_by endpoint via "
            "LlmRequest.local_url (no slot reservation — no prod writes); "
            f"rung shape {LOCAL_RUNG}.",
            "Cloud arm = prod MEDIUM rung 0 only; the chain's hosted-OSS/"
            "claude_agent fallbacks are recorded as errors, not exercised.",
            "Agreement compares the site's decision field (supports / verdict / "
            "pick / paraphrase count); None = unparseable either side.",
        ],
    }
    doc = report_markdown(payload)
    if args.doc:
        Path(args.doc).write_text(doc, encoding="utf-8")
        print(f"wrote {args.doc}", file=sys.stderr)
    print("\n" + doc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
