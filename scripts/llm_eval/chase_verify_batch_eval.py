"""chase:verify batch eval — replay logged verifies through the batch prompt.

MEASURE ONLY — the regression record for the chase:verify → castor switch
(gr499038; Reto 2026-10-21 triage Q5 ruled the op routes to castor regardless,
so this does not gate the flip). It answers: does the batch prompt
(``workers/_chase_llm._verify_supports_batch``, landed 7c769aebd) on the
candidate rung return the same per-item verdicts as the reference — the
FRONTIER rung (opus-class) — and as the stored MEDIUM verdicts?

Sampling (read-only, ``_prod_ro``): ``--n`` logged ``chase:verify`` calls from
``llm_call_log``/``llm_blob`` — the serialized ``LlmRequest``'s ``prompt`` IS
the production prompt, byte-for-byte. Each logged prompt is parsed back into
its shared claim block + per-candidate item blocks (both the single and the
batch prompt shapes round-trip). Items are then pooled and regrouped by
identical claim block into batches of ≤ ``VERIFY_BATCH_MAX_ITEMS`` — the same
grouping the production batcher makes — and the prompt rebuilt through the
real ``_verify_batch_prompt`` / ``_PROMPT_VERIFY`` templates, so a verdict
difference is the model's, not a re-templating artifact. A lone item replays
the single prompt, exactly as ``_verify_supports_batch`` does.

Arms:

* **candidate** — ``--model M --endpoint-url URL`` pins a single local rung
  (``local_url`` repoint, no slot reservation — no store is ever bound, so
  ``local_serving.acquire`` is dark) at ``reasoning_effort: low``; or
  ``--op-chain``, which reads rung 0 of the ``llm.op.chase:verify``
  app_settings row and, for a local rung, resolves its endpoint from the
  llm card's ``served_by`` (never printed or written — a LAN address).
* **reference** — ``Tier.FRONTIER`` rung 0, same rebuilt prompts.

Report: per-item candidate↔frontier and candidate↔incumbent agreement with a
Wilson 95% CI, a supports confusion matrix, unparseable counts, per-item
latency, and the raw arm replies (real paper text). Output defaults to the
gitignored ``gold_set/local/`` — never commit it.

    uv run python scripts/llm_eval/chase_verify_batch_eval.py --prod --n 200 \
        --model openai/gpt-oss-120b --endpoint-url http://<host>:<port>/v1
    uv run python scripts/llm_eval/chase_verify_batch_eval.py --prod --op-chain
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from precis.mesh_eval.stats import wilson_interval
from precis.utils.claude_p import _parse_last_json_block
from precis.utils.llm.router import LlmRequest, Tier, route

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _prod_ro import add_db_args, connect_ro  # noqa: E402

SOURCE = "chase:verify"

#: Env chain the candidate arm dispatches under (same seam medium_castor_eval
#: uses): one rung, the rung an operator flip would write.
CANDIDATE_CHAIN_ENV = "PRECIS_LLM_CHAIN_MEDIUM"

_OP_SQL = "SELECT value FROM app_settings WHERE key = %(key)s"
_CARD_SQL = """
    SELECT meta FROM refs
     WHERE kind = 'llm' AND meta->>'model_id' = %(model)s
     ORDER BY ref_id LIMIT 1
"""

#: Pass 1: dedup'd log rows + incumbent replies (small blobs); pass 2 fetches
#: only the chosen rows' (large) request blobs. Newest call per request_hash.
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

#: Anchors inside the production prompts (``_PROMPT_VERIFY`` /
#: ``_PROMPT_VERIFY_BATCH``): the claim block opens at ``CLAIM:``, the rubric
#: at this sentence, and items sit between the two.
_RUBRIC_MARK = "First decide the chunk's STANCE"
_CLAIM_RE = re.compile(r"\n\n(CLAIM:\n)")
_ITEM_START_RE = re.compile(r"\n\n(SOURCE: [^\n]*, chunk ord \d+, section )")
_BATCH_SEP_RE = re.compile(
    r"\n\n(\d+) candidate sources follow, each labelled ITEM <n>\. Judge EACH\n"
    "item independently against the claim — every item gets its own verdict\n"
    r"under the same contract\.\n\n"
)
_ITEM_SPLIT_RE = re.compile(r"(?:^|\n\n)ITEM \d+\n\n")
_SOURCE_KIND_RE = re.compile(r"^SOURCE: (\w+)")

_VERDICT_ENUM = ("yes", "partial", "no")


def _chase_llm() -> Any:
    """``workers/_chase_llm.py`` loaded by path: importing ``precis.workers``
    pulls the whole ref-pass registry; this module itself needs only the
    router. Same path-load seam ``_prod_ro`` uses for ``readonly_dsn``."""
    spec = importlib.util.spec_from_file_location(
        "_chase_llm_eval",
        HERE.parents[1] / "src" / "precis" / "workers" / "_chase_llm.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec: the module's dataclasses resolve their own
    # module in sys.modules while being created.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _rank(seed: str, key: str) -> str:
    return hashlib.md5(f"{key}{seed}".encode()).hexdigest()


def stratum_of(verdict: dict[str, Any] | None) -> str | None:
    """The incumbent verdict's stratum (same taxonomy as the gold builder)."""
    if not verdict:
        return None
    supports = verdict.get("supports")
    if supports not in _VERDICT_ENUM:
        return None
    if supports != "no":
        return str(supports)
    return "no_contradicts" if verdict.get("contradicts") is True else "no_other"


def _one_template_nl(s: str) -> str:
    """Drop the single template newline a block sits before (``{item_block}\\n``
    + rubric), keeping any trailing newline the block itself carries."""
    return s[:-1] if s.endswith("\n") else s


def parse_verdict(response: str | None) -> dict[str, Any] | None:
    """A single-call reply's verdict dict via the production parser."""
    data = _parse_last_json_block(response or "")
    if not data or data.get("supports") not in _VERDICT_ENUM:
        return None
    return data


def parse_logged_prompt(prompt: str) -> tuple[str, list[str]] | None:
    """One logged ``chase:verify`` prompt → ``(claim_block, item_blocks)``.

    Handles both shapes — the single verify (one item block, no ITEM
    markers) and the batch verify (``ITEM <n>``-labelled blocks). Returns
    ``None`` when a logged prompt doesn't match either template (older or
    drifted wording): the row is skipped, never guessed.
    """
    rubric = prompt.find(_RUBRIC_MARK)
    claim = _CLAIM_RE.search(prompt)
    if rubric < 0 or claim is None:
        return None
    claim_block = None
    batch = _BATCH_SEP_RE.search(prompt, claim.end())
    if batch is not None and batch.end() <= rubric:
        claim_block = prompt[claim.start() + 2 : batch.start()]
        region = prompt[batch.end() : rubric]
        parts = _ITEM_SPLIT_RE.split(region)
        # Middle blocks are verbatim (the split ate the ``\n\nITEM`` join,
        # leaving the block's own trailing newline); the LAST block is
        # followed by the template's single newline before the rubric.
        blocks = parts[1:]
        if blocks:
            blocks[-1] = _one_template_nl(blocks[-1])
        if not parts[0].strip() and len(blocks) == int(batch.group(1)) and all(blocks):
            return claim_block, blocks
        return None
    if "candidate sources follow" in prompt[:rubric]:
        return None  # batch-shaped but the separator wording drifted — skip
    item = _ITEM_START_RE.search(prompt, claim.end())
    if item is None or item.start() >= rubric:
        return None
    claim_block = prompt[claim.start() + 2 : item.start()]
    block = _one_template_nl(prompt[item.start() + 2 : rubric])
    return (claim_block, [block]) if block else None


def _source_kind(item_block: str) -> str:
    m = _SOURCE_KIND_RE.match(item_block)
    return m.group(1) if m else "paper"


def rebuild_prompt(
    claim_block: str, item_blocks: list[str], cl: Any
) -> tuple[str, int]:
    """The replay prompt for one group: the production batch template, or —
    for a lone item, as ``_verify_supports_batch`` prescribes — the single
    one. Returns ``(prompt, n_items)``."""
    if len(item_blocks) == 1:
        prompt = cl._PROMPT_VERIFY.format(
            source_kind=_source_kind(item_blocks[0]),
            claim_block=claim_block,
            item_block=item_blocks[0],
        )
    else:
        prompt = cl._verify_batch_prompt(claim_block, item_blocks)
    return prompt, len(item_blocks)


def arm_verdicts(
    prompt_kind: str, n_items: int, text: str | None, data: Any, cl: Any
) -> list[dict[str, Any] | None]:
    """Per-item verdicts from one arm's reply — batch parse for batch
    prompts (the production ``_parse_batch_verdicts``), the single verdict
    for lone-item replays. ``None`` = missing/malformed, never guessed."""
    if prompt_kind == "batch":
        parsed = data if isinstance(data, dict) else _parse_last_json_block(text or "")
        return list(cl._parse_batch_verdicts(parsed, n_items))
    verdict = (
        parse_verdict(text)
        if not isinstance(data, dict)
        else (data if data.get("supports") in _VERDICT_ENUM else None)
    )
    return [verdict]


def sample_items(
    conn: Any,
    cl: Any,
    *,
    n: int,
    seed: str,
    days: int,
    cap: int,
    model_like: str,
) -> list[dict[str, Any]]:
    """Two read-only passes → up to ``n`` items, each
    ``{request_hash, item_idx, claim_block, item_block, incumbent, model, ts}``.

    Seeded-uniform over the dedup'd log rows (no stratification — the
    agreement CI is a population estimate, not a stratum exercise). Pass 2
    fetches at most ``2n`` request blobs: singles dominate the log, so 2n
    rows covers parse-dropout; a shortfall reports the realised n.
    """
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _LABEL_SQL,
            {
                "source": SOURCE,
                "model_like": model_like,
                "days": days,
                "cap": cap,
            },
        )
        rows = cur.fetchall()
    rows.sort(key=lambda r: _rank(seed, r["request_hash"]))
    take = rows[: min(len(rows), 2 * n)]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_REQUEST_SQL, {"hashes": [r["request_hash"] for r in take]})
        blobs = {r["hash"]: r["text"] for r in cur.fetchall()}

    items: list[dict[str, Any]] = []
    skipped = 0
    for r in take:
        blob = blobs.get(r["request_hash"])
        if blob is None:
            skipped += 1
            continue
        try:
            req = json.loads(blob)
        except json.JSONDecodeError:
            skipped += 1
            continue
        parsed_prompt = parse_logged_prompt(str(req.get("prompt") or ""))
        if parsed_prompt is None:
            skipped += 1
            continue
        claim_block, item_blocks = parsed_prompt
        if len(item_blocks) > 1:
            verdicts = cl._parse_batch_verdicts(
                _parse_last_json_block(r["response"] or ""), len(item_blocks)
            )
        else:
            verdicts = [parse_verdict(r["response"])]
        for i, (block, verdict) in enumerate(zip(item_blocks, verdicts)):
            if verdict is None:
                skipped += 1
                continue
            items.append(
                {
                    "request_hash": r["request_hash"],
                    "item_idx": i,
                    "claim_key": hashlib.md5(claim_block.encode()).hexdigest(),
                    "claim_block": claim_block,
                    "item_block": block,
                    "incumbent": verdict,
                    "incumbent_model": r["model"],
                    "incumbent_ts": str(r["ts"]),
                }
            )
            if len(items) >= n:
                break
        if len(items) >= n:
            break
    print(
        f"sample: {len(items)} items from {len(rows)} log rows ({skipped} skipped)",
        file=sys.stderr,
    )
    return items


def regroup(
    items: list[dict[str, Any]], *, per_call: int, seed: str
) -> list[dict[str, Any]]:
    """Pool items, group by identical claim block, chunk ≤ ``per_call`` —
    deterministic under ``seed`` (item order inside a group is rank-sorted)."""
    by_claim: dict[str, list[dict[str, Any]]] = {}
    for it in sorted(
        items, key=lambda i: _rank(seed, f"{i['request_hash']}:{i['item_idx']}")
    ):
        by_claim.setdefault(it["claim_key"], []).append(it)
    groups: list[dict[str, Any]] = []
    for claim_key, members in by_claim.items():
        for i in range(0, len(members), per_call):
            chunk = members[i : i + per_call]
            groups.append(
                {
                    "claim_key": claim_key,
                    "claim_block": chunk[0]["claim_block"],
                    "items": chunk,
                }
            )
    groups.sort(key=lambda g: _rank(seed, g["claim_key"] + str(len(g["items"]))))
    return groups


def served_endpoint(conn: Any, model: str) -> str:
    """A local rung's ``served_by`` endpoint, read from the llm catalog card —
    the same read medium_castor_eval does. Held in memory only (LAN address,
    public repo)."""
    from urllib.parse import urlparse

    row = conn.execute(_CARD_SQL, {"model": model}).fetchone()
    if not row:
        raise SystemExit(f"no llm card with model_id={model!r}")
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
        return ep
    raise SystemExit(f"llm card {model!r} has no routable served_by endpoint")


def candidate_rung(
    args: argparse.Namespace, conn: Any
) -> tuple[dict[str, Any], str | None]:
    """The candidate's single rung + endpoint (``None`` for a cloud rung).

    ``--endpoint-url`` mode synthesises the proven local-rung shape
    (medium_castor_eval's LOCAL_RUNG: low reasoning effort, bounded
    ``max_tokens``). ``--op-chain`` takes rung 0 of ``llm.op.chase:verify``
    verbatim — the row the coordinator writes before the run — and resolves
    a local rung's endpoint from the card's ``served_by``.
    """
    if args.op_chain:
        row = conn.execute(_OP_SQL, {"key": f"llm.op.{SOURCE}"}).fetchone()
        if not row or not row[0]:
            raise SystemExit(
                f"no app_settings row for llm.op.{SOURCE} — the coordinator "
                "writes the castor row before this eval runs"
            )
        override = json.loads(row[0])
        chain = override.get("chain") if isinstance(override, dict) else None
        if not isinstance(chain, list) or not chain or not isinstance(chain[0], dict):
            raise SystemExit(f"llm.op.{SOURCE} has no usable chain[0]")
        rung = dict(chain[0])
        endpoint = (
            served_endpoint(conn, str(rung["model"]))
            if rung.get("placement") == "local"
            else None
        )
        return rung, endpoint
    rung = {
        "placement": "local",
        "model": args.model,
        "transport": "local",
        "max_tokens": args.rung_max_tokens,
        "timeout_s": args.rung_timeout_s,
        "reasoning_effort": "low",
    }
    return rung, args.endpoint_url


def _arm_result(res: Any, duration_s: float) -> dict[str, Any]:
    return {
        "error": res.error,
        "text": res.text or "",
        "data": res.data if isinstance(res.data, dict) else None,
        "cost_usd": res.cost_usd,
        "model": res.model,
        "placement": getattr(res, "placement", None),
        "duration_s": round(duration_s, 2),
    }


def run_group(
    group: dict[str, Any],
    *,
    arm: str,
    cl: Any,
    endpoint: str | None,
) -> dict[str, Any]:
    """One group's rebuilt prompt through one arm. ``arm`` is ``candidate``
    (Tier.MEDIUM under the env-pinned rung, ``local_url`` repoint — no slot
    reservation, no bound store → no prod writes) or ``reference``
    (Tier.FRONTIER rung 0)."""
    prompt, n_items = rebuild_prompt(group["claim_block"], group["item_blocks"], cl)
    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "log_call": False,
        "max_tokens": (cl._verify_batch_max_tokens(n_items) if n_items > 1 else None),
    }
    if arm == "candidate":
        # source=chase:verify — the registered op — so the env-pinned MEDIUM
        # chain is what prod's resolve_op path lands on.
        req = LlmRequest(tier=Tier.MEDIUM, source=SOURCE, local_url=endpoint, **kwargs)
    else:
        # NOT chase:verify: resolve_op force-remaps a registered source to
        # its op tier (MEDIUM) even with no store bound, which would quietly
        # rung the "reference" back onto medium. An unregistered source lets
        # the request's own FRONTIER tier stand.
        req = LlmRequest(tier=Tier.FRONTIER, source=f"{SOURCE}-eval:ref", **kwargs)
    t0 = time.monotonic()
    res = route(req)
    out = _arm_result(res, time.monotonic() - t0)
    kind = "batch" if n_items > 1 else "single"
    out["verdicts"] = arm_verdicts(kind, n_items, res.text, res.data, cl)
    return out


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    i = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
    return round(s[i], 1)


def agree_stats(items: list[dict[str, Any]], a: str, b: str) -> dict[str, Any]:
    """Wilson-CI agreement between two verdict arms over comparable items."""
    pairs = [
        (it[a], it[b])
        for it in items
        if it.get(a) is not None and it.get(b) is not None
    ]
    k = sum(1 for x, y in pairs if x == y)
    lo, hi = wilson_interval(k, len(pairs))
    return {
        "k": k,
        "n": len(pairs),
        "rate": round(k / len(pairs), 4) if pairs else None,
        "wilson95": [round(lo, 4), round(hi, 4)] if pairs else None,
    }


def summarize(groups: list[dict[str, Any]]) -> dict[str, Any]:
    items = [
        {
            "request_hash": it["request_hash"],
            "item_idx": it["item_idx"],
            "stratum": stratum_of(it["incumbent"]),
            "incumbent": (it["incumbent"] or {}).get("supports"),
            "candidate": (it.get("cand") or {}).get("supports")
            if it.get("cand")
            else None,
            "frontier": (it.get("ref") or {}).get("supports")
            if it.get("ref")
            else None,
        }
        for g in groups
        for it in g["items"]
    ]
    confusion: dict[str, dict[str, int]] = {}
    for it in items:
        ref = it["frontier"] or "none"
        row = confusion.setdefault(ref, {})
        cand = it["candidate"] or "none"
        row[cand] = row.get(cand, 0) + 1
    lat = {
        arm: [
            g[arm]["duration_s"] / len(g["items"])
            for g in groups
            if g.get(arm) and g[arm].get("duration_s")
        ]
        for arm in ("candidate", "reference")
    }
    return {
        "n_items": len(items),
        "n_groups": len(groups),
        "candidate_vs_frontier": agree_stats(items, "candidate", "frontier"),
        "candidate_vs_incumbent": agree_stats(items, "candidate", "incumbent"),
        "frontier_vs_incumbent": agree_stats(items, "frontier", "incumbent"),
        "confusion_frontier_x_candidate": confusion,
        "unparseable": {
            "candidate": sum(1 for i in items if i["candidate"] is None),
            "frontier": sum(1 for i in items if i["frontier"] is None),
        },
        "per_item_s": {
            arm: {
                "mean": round(statistics.mean(v), 2) if v else None,
                "p50": _pct(v, 0.50),
                "p95": _pct(v, 0.95),
            }
            for arm, v in lat.items()
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_db_args(ap)
    cand = ap.add_mutually_exclusive_group(required=True)
    cand.add_argument(
        "--op-chain",
        action="store_true",
        help="candidate = rung 0 of the llm.op.chase:verify app_settings row",
    )
    cand.add_argument("--model", help="candidate model id (with --endpoint-url)")
    ap.add_argument(
        "--endpoint-url", default=None, help="local transport URL for --model"
    )
    ap.add_argument("--rung-max-tokens", type=int, default=1600)
    ap.add_argument("--rung-timeout-s", type=int, default=300)
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--per-call", type=int, default=6, help="items per batch prompt")
    ap.add_argument("--seed", default="1")
    ap.add_argument("--days", type=int, default=30, help="route-log lookback")
    ap.add_argument("--model-like", default="%haiku%", help="incumbent model ILIKE")
    ap.add_argument("--log-cap", type=int, default=8000)
    ap.add_argument("--cap-usd", type=float, default=15.0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument(
        "--out",
        default=None,
        help="default: gold_set/local/chase_verify_batch_eval_<UTC date>.json",
    )
    ap.add_argument(
        "--sample-only", action="store_true", help="sample + group; no LLM calls"
    )
    args = ap.parse_args(argv)
    if args.model and not args.endpoint_url:
        ap.error("--model needs --endpoint-url")
    if args.op_chain and args.endpoint_url:
        ap.error(
            "--op-chain takes the rung from app_settings; --endpoint-url is ignored"
        )

    cl = _chase_llm()
    per_call = max(1, min(args.per_call, cl.VERIFY_BATCH_MAX_ITEMS))
    conn = connect_ro(args)
    try:
        rung, endpoint = candidate_rung(args, conn)
        items = sample_items(
            conn,
            cl,
            n=args.n,
            seed=str(args.seed),
            days=args.days,
            cap=args.log_cap,
            model_like=args.model_like,
        )
    finally:
        conn.rollback()
        conn.close()
    groups = regroup(items, per_call=per_call, seed=str(args.seed))
    for g in groups:
        g["item_blocks"] = [it["item_block"] for it in g["items"]]
    sizes = {len(g["items"]) for g in groups}
    print(
        f"eval: {len(items)} items in {len(groups)} groups "
        f"(sizes {sorted(sizes)}); candidate rung {rung}",
        file=sys.stderr,
    )
    if args.sample_only or not groups:
        return 0

    spent = 0.0
    # Candidate phase: the env-pinned single rung (popped right after — the
    # reference arm must see the fleet frontier chain, not this override).
    os.environ[CANDIDATE_CHAIN_ENV] = json.dumps([rung])
    try:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futures = {
                ex.submit(run_group, g, arm="candidate", cl=cl, endpoint=endpoint): g
                for g in groups
            }
            for fut in futures:
                g = futures[fut]
                try:
                    g["candidate"] = fut.result()
                except Exception as exc:
                    g["candidate"] = {"error": f"harness:{exc}", "verdicts": []}
                spent += g["candidate"].get("cost_usd") or 0.0
                if spent > args.cap_usd:
                    print(f"SPEND CAP at ${spent:.2f} — stopping", file=sys.stderr)
                    for f2 in futures:
                        f2.cancel()
                    break
    finally:
        os.environ.pop(CANDIDATE_CHAIN_ENV, None)
    todo = [g for g in groups if "candidate" in g]

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = {
            ex.submit(run_group, g, arm="reference", cl=cl, endpoint=None): g
            for g in todo
        }
        for fut in futures:
            g = futures[fut]
            try:
                g["reference"] = fut.result()
            except Exception as exc:
                g["reference"] = {"error": f"harness:{exc}", "verdicts": []}
            spent += g["reference"].get("cost_usd") or 0.0
            if spent > args.cap_usd:
                print(f"SPEND CAP at ${spent:.2f} — stopping", file=sys.stderr)
                for f2 in futures:
                    f2.cancel()
                break

    for g in todo:
        for arm_key, out_key in (("candidate", "cand"), ("reference", "ref")):
            arm = g.get(arm_key) or {}
            verdicts = arm.get("verdicts") or []
            for i, it in enumerate(g["items"]):
                it[out_key] = verdicts[i] if i < len(verdicts) else None

    report: dict[str, Any] = {
        "meta": {
            "run_date_utc": time.strftime("%Y-%m-%d", time.gmtime()),
            "source": SOURCE,
            "n_items": len(items),
            "n_groups": len(todo),
            "groups_dropped_spend_cap": len(groups) - len(todo),
            "seed": str(args.seed),
            "days": args.days,
            "model_like": args.model_like,
            "candidate": {
                "mode": "op-chain" if args.op_chain else "endpoint-url",
                "rung": rung,
                "endpoint": "served_by" if endpoint else None,
            },
            "reference": {"tier": Tier.FRONTIER.value},
            "spent_usd": round(spent, 4),
            "cap_usd": args.cap_usd,
        },
        "summary": summarize(todo),
        "groups": [
            {
                "claim_key": g["claim_key"],
                "n_items": len(g["items"]),
                "candidate": g.get("candidate"),
                "reference": g.get("reference"),
            }
            for g in todo
        ],
        "items": [
            {
                "request_hash": it["request_hash"],
                "item_idx": it["item_idx"],
                "claim_key": it["claim_key"],
                "stratum": stratum_of(it["incumbent"]),
                "incumbent": it["incumbent"],
                "incumbent_model": it["incumbent_model"],
                "candidate": it.get("cand"),
                "frontier": it.get("ref"),
            }
            for g in todo
            for it in g["items"]
        ],
    }
    out = Path(
        args.out
        or HERE
        / "gold_set"
        / "local"
        / f"chase_verify_batch_eval_{report['meta']['run_date_utc']}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    s = report["summary"]["candidate_vs_frontier"]
    print(
        f"agreement cand↔frontier {s['k']}/{s['n']} "
        f"(rate {s['rate']}, wilson95 {s['wilson95']}); spent ${spent:.3f}; "
        f"wrote {out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
