"""Deterministic per-lane scorers for the mesh evals.

Each scorer is a pure function ``(response_text, response_data, item,
ctx) -> LaneScore`` — ``ctx`` is the set header's ``scorer_ctx`` (e.g. the
campaign's measurand aliases for ``categorise``). A scorer never raises on
a malformed reply: the reply *is* the thing being measured, so a
non-answer scores 0 with the reason in ``detail``.

These measure *agreement with the frozen gold*, which for most lanes is a
frontier model's verdict — the reviewed bar. The auto-apply bar also needs
the human holdout (``local-mesh-upkeep.md`` §1). Where the spec wants a
frontier judge (finding's claim match, link-add's no-edge negatives), the
deterministic score is the cheap check and
:mod:`~precis.mesh_eval.runner`'s judge pass is the reference.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from precis.errors import BadInput
from precis.mesh_eval.sets import LANES, MeshItem


@dataclass(frozen=True, slots=True)
class LaneScore:
    """A scored item: ``score`` in [0, 1] plus lane-specific ``detail``."""

    score: float
    detail: dict[str, Any] = field(default_factory=dict)


def _last_json(text: str, kinds: tuple[type, ...]) -> Any | None:
    """Parse the LAST complete JSON value of a wanted kind in ``text``.

    Same scan as ``claude_p._parse_last_json_block`` (raw_decode from each
    candidate opener, string-aware), generalised to arrays: lanes ask for
    a JSON array reply, and a fenced or prose-prefixed one still parses.
    """
    if not text:
        return None
    decoder = json.JSONDecoder()
    last: Any = None
    i = 0
    while i < len(text):
        ch = text[i]
        if ch not in "[{":
            i += 1
            continue
        try:
            parsed, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(parsed, kinds):
            last = parsed
        i = max(end, i + 1)
    return last


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", _norm(s)) if len(t) > 2}


def _aliases_from_ctx(ctx: dict[str, Any]) -> Any:
    """Rebuild the campaign's MeasurandAliases from a set's scorer_ctx."""
    raw = ctx.get("aliases")
    if not isinstance(raw, dict):
        return None
    from precis.taxonomy.config import MeasurandAliases

    return MeasurandAliases(
        species={k: tuple(v) for k, v in (raw.get("species") or {}).items()},
        phrases={k: tuple(v) for k, v in (raw.get("phrases") or {}).items()},
    )


def _mentions_from_meta(item: MeshItem) -> list[Any]:
    from precis.taxonomy.types import Anchor, Mention

    out: list[Mention] = []
    for m in item.meta.get("mentions") or []:
        a = m["anchor"]
        out.append(
            Mention(
                anchor=Anchor(
                    source_ref_id=int(a["ref_id"]),
                    start=int(a["start"]),
                    end=int(a["end"]),
                    source_kind=a.get("source_kind", "ref_text"),
                    scheme=int(a.get("scheme", 1)),
                ),
                kind=m["kind"],
                literal=m["literal"],
                raw_unit=m.get("raw_unit"),
                context=m.get("context", ""),
                marker=m.get("marker"),
            )
        )
    return out


def score_categorise(
    response_text: str,
    response_data: dict[str, Any] | None,
    item: MeshItem,
    ctx: dict[str, Any],
) -> LaneScore:
    """categorise: share of gold mentions whose folded measurand key matches.

    ``gold = {"mentions": [{"index", "gold_key", "gold_measurand"}]}``;
    ``item.meta["mentions"]`` replays the prompt's mention list so the
    reply is parsed exactly as production's discovery stage parses it
    (:func:`precis.taxonomy.discovery.parse_response`). The model's
    measurand strings fold through the campaign aliases carried in
    ``ctx["aliases"]`` — the same fold the frontier's gold keys went
    through.
    """
    from precis.taxonomy.discovery import parse_response
    from precis.taxonomy.normalise import alias_key

    gold_mentions = (item.gold or {}).get("mentions") or []
    if not gold_mentions:
        return LaneScore(0.0, {"reason": "no gold mentions"})
    mentions = _mentions_from_meta(item)
    aliases = _aliases_from_ctx(ctx)
    terms, warnings = parse_response(response_text or "", mentions, "A")
    # A term carries the Mention it resolved to (``parse_response`` looked
    # the reply row's ``index`` up in ``mentions``), so anchor equality
    # maps it back to the prompt's numbering.
    anchor_to_idx = {m.anchor: i for i, m in enumerate(mentions)}
    answered: dict[int, str] = {}
    for t in terms:
        idx = anchor_to_idx.get(t.mention.anchor)
        if idx is not None:
            answered[idx] = t.measurand

    matched = 0
    diffs: list[dict[str, Any]] = []
    for g in gold_mentions:
        idx = int(g["index"])
        got = answered.get(idx)
        got_key = alias_key(got, aliases) if got else None
        if got_key is not None and got_key == g["gold_key"]:
            matched += 1
        else:
            diffs.append(
                {"index": idx, "gold": g["gold_key"], "got": got_key or "(none)"}
            )
    n = len(gold_mentions)
    return LaneScore(
        matched / n,
        {
            "n": n,
            "matched": matched,
            "answered": len(answered),
            "parse_warnings": list(warnings)[:5],
            "diffs": diffs[:10],
        },
    )


def score_link_add(
    response_text: str,
    response_data: dict[str, Any] | None,
    item: MeshItem,
    ctx: dict[str, Any],
) -> LaneScore:
    """link-add: F1 over proposed ``(dst, relation)`` pairs vs the gold edges.

    ``gold = {"edges": [{"dst_ref_id", "relation"}],
              "no_edge": [ref_id, ...]}``. A proposal naming a ``no_edge``
    neighbour counts as a false positive on top of missing the gold pair.
    Proposals for a node outside the item's candidate list count as
    ``wild`` — flagged separately because they usually mean the model
    invented an id rather than chose a wrong neighbour.
    """
    gold = item.gold or {}
    gold_pairs = {
        (int(e["dst_ref_id"]), _norm(str(e["relation"])))
        for e in gold.get("edges") or []
    }
    no_edge = {int(x) for x in gold.get("no_edge") or []}
    candidates = {int(x) for x in (item.meta.get("candidates") or [])}

    reply = response_data if isinstance(response_data, list) else None
    if reply is None:
        reply = _last_json(response_text or "", (list,))
    proposed: set[tuple[int, str]] = set()
    proposed_no_edge = 0
    wild = 0
    if isinstance(reply, list):
        for row in reply:
            if not isinstance(row, dict):
                continue
            dst_raw = row.get("dst")
            if not isinstance(dst_raw, (int, str)):
                continue
            try:
                dst = int(dst_raw)
            except (TypeError, ValueError):
                continue
            rel = _norm(str(row.get("relation") or ""))
            if not rel:
                continue
            if dst in no_edge:
                proposed_no_edge += 1
            elif dst not in candidates and dst not in {d for d, _ in gold_pairs}:
                wild += 1
                continue
            proposed.add((dst, rel))

    tp = len(proposed & gold_pairs)
    fp = len(proposed - gold_pairs) + proposed_no_edge
    fn = len(gold_pairs - proposed)
    precision = tp / (tp + fp) if tp + fp else (1.0 if not gold_pairs else 0.0)
    recall = tp / len(gold_pairs) if gold_pairs else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return LaneScore(
        f1,
        {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": precision,
            "recall": recall,
            "no_edge_proposed": proposed_no_edge,
            "wild": wild,
            "unparseable": not isinstance(reply, list),
        },
    )


def score_finding(
    response_text: str,
    response_data: dict[str, Any] | None,
    item: MeshItem,
    ctx: dict[str, Any],
) -> LaneScore:
    """finding: weighted coverage of the gold claim titles by the reply's claims.

    ``gold = {"claims": [{"hub_ref_id", "title", "weight"}]}``. The reply
    is a JSON array of claim statements. A gold claim is *matched* when
    its token overlap (|shared| / |gold tokens|) with the best reply claim
    reaches ``_CLAIM_MATCH`` — token overlap is the cheap deterministic
    stand-in for the frontier claim-match judge the spec grades on a
    sample; ``detail.extra`` counts reply claims matching no gold claim.
    """
    _CLAIM_MATCH = 0.6
    gold_claims = (item.gold or {}).get("claims") or []
    if not gold_claims:
        return LaneScore(0.0, {"reason": "no gold claims"})

    reply = response_data if isinstance(response_data, list) else None
    if reply is None:
        reply = _last_json(response_text or "", (list,))
    claims = [str(c) for c in reply] if isinstance(reply, list) else []
    claim_tokens = [_tokens(c) for c in claims]

    covered = 0.0
    total_w = 0.0
    missed: list[int] = []
    used: set[int] = set()
    for g in gold_claims:
        w = float(g.get("weight") or 1.0)
        total_w += w
        gt = _tokens(str(g.get("title") or ""))
        best_i, best = -1, 0.0
        for i, ct in enumerate(claim_tokens):
            if not ct or not gt:
                continue
            ov = len(gt & ct) / len(gt)
            if ov > best:
                best, best_i = ov, i
        if best >= _CLAIM_MATCH:
            covered += w
            used.add(best_i)
        else:
            missed.append(int(g["hub_ref_id"]))
    extra = len(claims) - len(used)
    return LaneScore(
        covered / total_w if total_w else 0.0,
        {
            "covered": covered,
            "total_weight": total_w,
            "n_reply_claims": len(claims),
            "extra": extra,
            "missed": missed,
            "unparseable": not isinstance(reply, list),
        },
    )


def score_merge(
    response_text: str,
    response_data: dict[str, Any] | None,
    item: MeshItem,
    ctx: dict[str, Any],
) -> LaneScore:
    """merge: 1.0 iff the reply's same/different verdict equals gold.

    ``gold = {"same": bool, "survivor": "a"|"b"|null}``. The reply's last
    JSON object is read for ``same`` (bool); a ``merge: yes/no`` string
    also parses. ``detail.false_merge`` marks the expensive error
    direction — saying *same* on a genuinely-different pair — which the
    spec wants counted apart.
    """
    gold_same = bool((item.gold or {}).get("same"))
    data = response_data if isinstance(response_data, dict) else None
    if data is None:
        data = _last_json(response_text or "", (dict,))
    pred: bool | None = None
    if isinstance(data, dict):
        if isinstance(data.get("same"), bool):
            pred = data["same"]
        else:
            m = str(data.get("merge") or "").strip().casefold()
            if m in ("yes", "true", "same"):
                pred = True
            elif m in ("no", "false", "different"):
                pred = False
    if pred is None:
        return LaneScore(0.0, {"unparseable": True})
    correct = pred == gold_same
    return LaneScore(
        1.0 if correct else 0.0,
        {
            "pred_same": pred,
            "gold_same": gold_same,
            "false_merge": pred and not gold_same,
            "missed_merge": (not pred) and gold_same,
        },
    )


def score_summarise(
    response_text: str,
    response_data: dict[str, Any] | None,
    item: MeshItem,
    ctx: dict[str, Any],
) -> LaneScore:
    """summarise: the production summariser's own gates (llm_eval's scorer)."""
    from precis.llm_eval.scorers import score_summary

    return LaneScore(
        score_summary(response_text or "", response_data, item.gold or {}),
        {},
    )


#: Lane → scorer. A scorer takes ``(response_text, response_data, item,
#: ctx)``; the runner looks the item's lane up here.
LANE_SCORERS = {
    "categorise": score_categorise,
    "link-add": score_link_add,
    "finding": score_finding,
    "merge": score_merge,
    "summarise": score_summarise,
}
assert set(LANE_SCORERS) == set(LANES), "mesh eval: scorer registry not total"


def score_item(
    item: MeshItem,
    response_text: str,
    response_data: dict[str, Any] | None = None,
    ctx: dict[str, Any] | None = None,
) -> LaneScore:
    """Score one item's reply with its lane's scorer."""
    if item.gold is None:
        raise BadInput(f"mesh eval: item {item.item_id} has no gold (holdout?)")
    return LANE_SCORERS[item.lane](response_text, response_data, item, ctx or {})


__all__ = ["LANE_SCORERS", "LaneScore", "score_item"]
