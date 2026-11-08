"""Plain-language job rows — one sentence per job (td573331, spec me573285 §W6).

Every job row in the UI and the ``get(kind='job')`` card should read as a
sentence — which quest it serves and what it did — instead of the stored
title's ``quest_tick (unlinked)`` / ``derived_drain (materializer 33/33)``.
Stored titles are never read here (they are minted once at put time and rot);
the sentence is rebuilt per read from ``meta.params``, ``meta`` itself, and
the latest ``job_summary`` / ``job_event`` chunk text the caller supplies.

Shape:

* :func:`job_sentence` — pure function, no DB. Dispatches on ``job_type``
  through :data:`DESCRIBERS`; unlisted types get a generic fallback
  ("<job_type as words>: <summary or last_event or status>").
* :data:`PLATFORM_UPKEEP` — the materialized/scheduled platform passes that
  collapse into one "platform upkeep" line per type on /status?tab=now
  (running count, queued count, oldest wait) under qu540835. Membership:
  the explicitly named recurring passes plus types judged platform-shaped
  by ``dedupe_pending``/``queue_ttl_exempt`` and their module docstrings
  (plan_dispatch, pr_poll, ci_verdict_poll). Product content producers
  (meditation, briefing, reading_brief, card_forge, remarkable_*),
  diagnose_gripe and one-off backfills (elsevier_abstract_backfill) stay
  out — they are quest/user-facing work, not upkeep.
* :func:`quest_id_from_meta` — the meta-only half of "which quest does this
  job serve": ``params.quest_id`` (quest_tick, struct_search, …), then
  ``coordinator_state.quest_id``, then ``params.structure_ref_id`` → that
  structure ref's open ``quest:<id>`` tag (the tag
  :func:`precis.quest.tagging.quest_tag_value` writes), resolved through a
  precomputed ``{structure_ref_id: quest_id}`` map — never a query per
  row; page callers build it once with
  :func:`quest_ids_for_structures` on the connection they already hold.
  No todo-parent chain — sibling W5 (td573299) builds that resolver and
  calls this one as its fallback.
* :func:`collapse_upkeep` — group the upkeep rows of a live-lane snapshot
  into one synthetic line per job type.
* :func:`job_row_sentence` — the store-touching convenience wrapper for
  single-job renders (the MCP card): scans chunk blocks for the latest
  summary/event and resolves the quest headline through
  :func:`quest_id_from_meta` + one ``kind='quest'`` read.

Handles: quest ``qu<id>``, structure ``st<id>``, job ``jo<id>``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from precis.utils.secret_scan import mask_secrets

log = logging.getLogger(__name__)

__all__ = [
    "DESCRIBERS",
    "PLATFORM_UPKEEP",
    "UPKEEP_QUEST_HANDLE",
    "collapse_upkeep",
    "job_row_sentence",
    "job_sentence",
    "quest_id_from_meta",
    "quest_ids_for_structures",
    "structure_ref_id_from_meta",
]

#: Job types whose live rows collapse into one "platform upkeep" line on
#: /status?tab=now — see the module docstring for the membership rule.
PLATFORM_UPKEEP: frozenset[str] = frozenset(
    {
        "ci_verdict_poll",
        "conflict_sweep",
        "derived_drain",
        "doctor_tick",
        "draft_refresh",
        "embed_batch",
        "graph_garden",
        "gripe_validate",
        "lane_health",
        "money_refresh",
        "news_poll",
        "plan_dispatch",
        "factory_rescue_tick",
        "pr_poll",
        "secret_status",
        "taproot_backfill",
    }
)

#: The quest every collapsed "platform upkeep" line is filed under.
UPKEEP_QUEST_HANDLE = "qu540835"


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    """``str`` passthrough, ``None`` passthrough, anything else ``str()`` —
    a ``meta.error`` that is a dict (handlers/job.py already guards this
    shape) must not crash ``_first_line``'s ``.split``."""
    return value if isinstance(value, str) or value is None else str(value)


def _short_wait(secs: float | None) -> str:
    """Bare-magnitude age ("40m", "2h") for an upkeep line's "oldest" field.

    Mirrors ``precis_web.timefmt._bucket`` — duplicated here rather than
    imported because the workers layer may not import ``precis_web``.
    """
    if secs is None:
        return ""
    secs = max(0.0, secs)
    if secs < 90:
        return f"{int(secs)}s"
    if secs < 5400:
        return f"{int(secs / 60)}m"
    if secs < 172800:
        return f"{int(secs / 3600)}h"
    return f"{int(secs / 86400)}d"


def _quest_label(quest_headline: str | None, qid: int | None) -> str:
    """``"<headline> (qu<id>)"`` — the quest handle always survives so a
    failed row still names its quest when only the headline was passed."""
    handle = f"qu{qid}" if qid is not None else None
    if quest_headline:
        if handle and handle not in quest_headline:
            return f"{quest_headline} ({handle})"
        return quest_headline
    return handle or "quest"


def _first_line(text: str | None) -> str:
    return (text or "").split("\n", 1)[0].strip()


#: The tick event line quest_tick writes per slice (quest_tick.py):
#: ``tick #N: <status> — C candidate(s), S sim(s), H harvested, G
#: graduated, X search(es) (+Y papers); <note>``.
_TICK_EVENT_RE = re.compile(
    r"tick #(\d+): .*?— (\d+) candidate\(s\), (\d+) sim\(s\), "
    r"(\d+) harvested, (\d+) graduated, (\d+) search\(es\)"
)

#: Terminal rest summaries: ``quest <id> loop resting after N consecutive
#: <flavor> (last: <status> — <note>). Re-armed …`` — plus the
#: cli_unavailable form ``loop resting — this host's claude CLI is
#: unavailable (<note>).``.
_REST_RE = re.compile(r"resting after (\d+) consecutive ([^(]+?)\s*\(last: (.*)\)")


def _d_quest_tick(
    *,
    params: dict[str, Any],
    meta: dict[str, Any],
    status: str | None,
    summary: str | None,
    last_event: str | None,
    quest_headline: str | None,
    error: str | None,
) -> str:
    state = meta.get("coordinator_state") or {}
    qid = _as_int(params.get("quest_id")) or _as_int(state.get("quest_id"))
    label = _quest_label(quest_headline, qid)

    if status == "failed" or "loop resting" in (summary or ""):
        m = _REST_RE.search(summary or "")
        if m:
            n, flavor, last = m.group(1), m.group(2).strip(), m.group(3).strip()
            return f"{label}: resting after {n} {flavor}: {last}"
        reason = error or _first_line(summary) or "unknown error"
        n = (
            _as_int(state.get("tick_failures"))
            or _as_int(state.get("dry_ticks"))
            or _as_int(state.get("punt_ticks"))
        )
        if n:
            return f"{label}: resting after {n} failed ticks: {reason}"
        return f"{label}: {status or 'failed'}: {reason}"

    m = _TICK_EVENT_RE.search(last_event or "")
    if m:
        n, c, s, h, _g, _x = m.groups()
        return (
            f"{label}: tick {n}, proposed {c} candidate(s), "
            f"dispatched {s} sim(s), harvested {h}"
        )
    if last_event:
        return f"{label}: {_first_line(last_event)}"
    if summary:
        return f"{label}: {_first_line(summary)}"
    n = _as_int(state.get("slice_count"))
    return (
        f"{label}: tick {n}"
        if n is not None
        else f"{label}: quest loop {status or 'pending'}"
    )


def _d_struct_relax(
    *,
    params: dict[str, Any],
    meta: dict[str, Any],
    status: str | None,
    summary: str | None,
    last_event: str | None,
    quest_headline: str | None,
    error: str | None,
) -> str:
    formula = _first_line(params.get("poscar"))
    sid = _as_int(params.get("structure_ref_id"))
    what = formula or (f"st{sid}" if sid is not None else "a structure")
    if what == "a structure" and (summary or last_event):
        # Nothing names the structure — the job's own line carries the
        # verdict, energy and run-cube id better than a generic compose.
        return f"structure relax: {_first_line(summary or last_event)}"
    model = params.get("model") or params.get("fidelity")
    host = meta.get("node") or meta.get("lease_host") or params.get("target_node")
    wall = meta.get("wall_s")
    converged: bool | None = None
    m = re.search(r"relax\[[^\]]+\] (converged|did NOT converge)", summary or "")
    if m:
        converged = m.group(1) == "converged"

    quest = quest_headline or ""
    bits = "relaxed" if converged is not None else "relaxing"
    bits += f" {what}"
    if quest:
        bits += f" for {quest}"
    if model:
        bits += f" with {model}"
    if host:
        bits += f" on {host}"
    if converged is True:
        wall_s = _as_int(wall)
        bits += f", converged in {wall_s} s" if wall_s is not None else ", converged"
    elif converged is False:
        bits += ", did not converge"
    elif status in ("running", "queued"):
        bits += f" ({status})"
    return bits


def _d_derived_drain(
    *,
    params: dict[str, Any],
    meta: dict[str, Any],
    status: str | None,
    summary: str | None,
    last_event: str | None,
    quest_headline: str | None,
    error: str | None,
) -> str:
    # Terminal: the drain's own summary reads better than the generic line
    # ("derived_drain[llm_summarize]: drained 33 chunk(s) (33 ok, 0 …)").
    if status in ("succeeded", "failed", "cancelled") and summary:
        return re.sub(r"^derived_drain\[[^\]]*\]:\s*", "", _first_line(summary))
    verb = {"llm_summarize": "summarising", "classify": "classifying"}.get(
        str(params.get("pass") or ""), "summarising and classifying"
    )
    s = f"{verb} ingested text"
    # Callers with queue depth (the collapsed upkeep line) inject counts as
    # params['queued']/params['running'] — "N queued behind M running".
    q, r = _as_int(params.get("queued")), _as_int(params.get("running"))
    if q is not None and r is not None:
        s += f", {q} queued behind {r} running"
    return s


def _d_embed_batch(
    *,
    params: dict[str, Any],
    meta: dict[str, Any],
    status: str | None,
    summary: str | None,
    last_event: str | None,
    quest_headline: str | None,
    error: str | None,
) -> str:
    if status in ("succeeded", "failed", "cancelled") and summary:
        return _first_line(summary)
    return "embedding new text chunks"


def _d_gripe_validate(
    *,
    params: dict[str, Any],
    meta: dict[str, Any],
    status: str | None,
    summary: str | None,
    last_event: str | None,
    quest_headline: str | None,
    error: str | None,
) -> str:
    s = _first_line(summary)
    # "gripe_validate (prod head <sha9>): verify=V live=L … held=H user=U …"
    m = re.search(r"verify=(\d+).*?held=(\d+)", s)
    if m:
        return (
            f"checked {m.group(1)} shipped gripe fixes against prod, {m.group(2)} held"
        )
    if s:
        return s
    return "checking shipped gripe fixes against prod"


def _prompt_head(params: dict[str, Any], *, limit: int = 72) -> str:
    head = _first_line(str(params.get("prompt") or ""))
    return f"{head[: limit - 1]}…" if len(head) > limit else head


def _d_fix_gripe(
    *,
    params: dict[str, Any],
    meta: dict[str, Any],
    status: str | None,
    summary: str | None,
    last_event: str | None,
    quest_headline: str | None,
    error: str | None,
) -> str:
    gid = _as_int(params.get("gripe_id"))
    what = f"fixing gr{gid}" if gid is not None else "fixing a gripe"
    if status == "failed" and error:
        return f"{what}: failed — {_first_line(error)}"
    if status in ("succeeded", "failed", "cancelled") and summary:
        return _first_line(summary)
    return what


def _d_run_slice(
    *,
    params: dict[str, Any],
    meta: dict[str, Any],
    status: str | None,
    summary: str | None,
    last_event: str | None,
    quest_headline: str | None,
    error: str | None,
) -> str:
    tid = _as_int(params.get("todo_id"))
    lane = params.get("lane")
    what = f"running slice td{tid}" if tid is not None else "running a slice"
    if lane:
        what += f" on {lane}"
    if status == "failed" and error:
        return f"{what}: failed — {_first_line(error)}"
    if status in ("succeeded", "failed", "cancelled") and summary:
        return _first_line(summary)
    return what


def _d_land_slice(
    *,
    params: dict[str, Any],
    meta: dict[str, Any],
    status: str | None,
    summary: str | None,
    last_event: str | None,
    quest_headline: str | None,
    error: str | None,
) -> str:
    tid = _as_int(params.get("todo_id"))
    what = f"landing slice td{tid}" if tid is not None else "landing a slice"
    if status == "failed" and error:
        return f"{what}: failed — {_first_line(error)}"
    if status in ("succeeded", "failed", "cancelled") and summary:
        return _first_line(summary)
    return what


def _d_sandbox_run(
    *,
    params: dict[str, Any],
    meta: dict[str, Any],
    status: str | None,
    summary: str | None,
    last_event: str | None,
    quest_headline: str | None,
    error: str | None,
) -> str:
    mode = params.get("mode") or "run"
    head = _prompt_head(params)
    s = f"sandbox {mode}: {head}" if head else f"sandbox {mode}"
    if status == "failed" and error:
        return f"{s}: failed — {_first_line(error)}"
    return s


def _d_devin_task(
    *,
    params: dict[str, Any],
    meta: dict[str, Any],
    status: str | None,
    summary: str | None,
    last_event: str | None,
    quest_headline: str | None,
    error: str | None,
) -> str:
    head = _prompt_head(params)
    s = f"devin task: {head}" if head else "devin task"
    if status == "failed" and error:
        return f"{s}: failed — {_first_line(error)}"
    if status in ("succeeded", "failed", "cancelled") and summary:
        return _first_line(summary)
    return s


def _d_paper_pull(
    *,
    params: dict[str, Any],
    meta: dict[str, Any],
    status: str | None,
    summary: str | None,
    last_event: str | None,
    quest_headline: str | None,
    error: str | None,
) -> str:
    rid = _as_int(params.get("ref_id"))
    what = (
        f"fetching the PDF for pa{rid}" if rid is not None else "fetching a paper PDF"
    )
    if status in ("succeeded", "failed", "cancelled") and summary:
        return _first_line(summary)
    if status == "failed" and error:
        return f"{what}: failed — {_first_line(error)}"
    return what


def _d_plan_tick(
    *,
    params: dict[str, Any],
    meta: dict[str, Any],
    status: str | None,
    summary: str | None,
    last_event: str | None,
    quest_headline: str | None,
    error: str | None,
) -> str:
    model = params.get("model")
    s = f"planner tick ({model})" if model else "planner tick on its parent todo"
    if status == "failed" and error:
        return f"{s}: failed — {_first_line(error)}"
    if status in ("succeeded", "failed", "cancelled") and summary:
        return _first_line(summary)
    return s


def _const(sentence: str) -> Callable[..., str]:
    """An upkeep-type describer: one fixed activity phrase while live, the
    job's own summary once terminal (those summaries already read as
    sentences — ``news_poll: 3 feeds, 12 new articles, 0 failed``)."""

    def _d(
        *,
        params: dict[str, Any],
        meta: dict[str, Any],
        status: str | None,
        summary: str | None,
        last_event: str | None,
        quest_headline: str | None,
        error: str | None,
    ) -> str:
        if status == "failed" and error:
            return f"{sentence}: failed — {_first_line(error)}"
        if status in ("succeeded", "failed", "cancelled") and summary:
            return _first_line(summary)
        return sentence

    return _d


#: Per-job-type sentence builders. Each takes the same kwargs
#: :func:`job_sentence` accepts (minus ``job_type``) and must never raise
#: on absent fields — a row mid-flight carries far less than a done one.
DESCRIBERS: dict[str, Callable[..., str]] = {
    "quest_tick": _d_quest_tick,
    "struct_relax": _d_struct_relax,
    "derived_drain": _d_derived_drain,
    "embed_batch": _d_embed_batch,
    "gripe_validate": _d_gripe_validate,
    "fix_gripe": _d_fix_gripe,
    "run_slice": _d_run_slice,
    "land_slice": _d_land_slice,
    "sandbox_run": _d_sandbox_run,
    "devin_task": _d_devin_task,
    "paper_pull": _d_paper_pull,
    "plan_tick": _d_plan_tick,
    "doctor_tick": _const("checking fleet health"),
    "conflict_sweep": _const("sweeping a claim hub for contradictions"),
    "taproot_backfill": _const("backfilling claim taproots"),
    "news_poll": _const("polling RSS feeds"),
    "money_refresh": _const("refreshing FX/CPI rates"),
    "secret_status": _const("checking stored credentials"),
    "lane_health": _const("probing lane credentials"),
    "draft_refresh": _const("refreshing a living draft"),
    "graph_garden": _const("gardening the memory graph"),
    "plan_dispatch": _const("dispatching dark-factory work"),
    "factory_rescue_tick": _const("watching dark-factory nodes for stalls"),
    "pr_poll": _const("polling inbound pull requests"),
    "ci_verdict_poll": _const("polling the release gate"),
    "ci_rerun": _const("re-firing the red run's failed jobs"),
}


#: Job types whose describer already logged a failure — the warning fires
#: once per type per process, not a traceback per row per render.
_DESCRIBER_FAILED: set[str] = set()


def job_sentence(
    job_type: str,
    *,
    params: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    status: str | None = None,
    summary: str | None = None,
    last_event: str | None = None,
    quest_headline: str | None = None,
    error: str | None = None,
) -> str:
    """One plain-language sentence for a job row. Pure — every fact comes
    in through the arguments; the caller decides what a DB read costs.

    ``params`` is ``meta['params']`` on a job ref; ``summary`` /
    ``last_event`` the newest ``job_summary`` / ``job_event`` chunk text;
    ``quest_headline`` a preformatted quest name (title or ``qu<id>``);
    ``error`` the failure reason when ``status == 'failed'``.

    Never raises: non-dict ``params``/``meta`` degrade to empty, the
    rendered free-text fields are coerced to ``str``, and a describer
    that still throws falls back to the generic sentence — a page or
    card must survive one malformed row (td573331 fix round).
    """
    params = dict(params) if isinstance(params, dict) else {}
    meta = dict(meta) if isinstance(meta, dict) else {}
    if "poscar" in params:
        params["poscar"] = _as_str(params["poscar"])
    # Free text shown verbatim goes through the secret mask before it is
    # rendered — a pasted credential in a prompt or an error trace must
    # not echo onto a page or card (td573331 fix round).
    if "prompt" in params:
        params["prompt"] = mask_secrets(_as_str(params["prompt"]), warn=False)
    summary = mask_secrets(_as_str(summary), warn=False)
    last_event = mask_secrets(_as_str(last_event), warn=False)
    error = mask_secrets(_as_str(error), warn=False)
    fn = DESCRIBERS.get(job_type)
    if fn is not None:
        try:
            return fn(
                params=params,
                meta=meta,
                status=status,
                summary=summary,
                last_event=last_event,
                quest_headline=quest_headline,
                error=error,
            )
        except Exception:
            if job_type not in _DESCRIBER_FAILED:
                _DESCRIBER_FAILED.add(job_type)
                log.warning(
                    "job_describe: %s describer failed, using fallback",
                    job_type,
                    exc_info=True,
                )
    detail = _first_line(summary) or _first_line(last_event) or status or "pending"
    return f"{job_type.replace('_', ' ')}: {detail}"


_QUEST_TAG_RE = re.compile(r"^quest:(\d+)$")


def structure_ref_id_from_meta(meta: dict[str, Any] | None) -> int | None:
    """``params.structure_ref_id`` as an int, or ``None`` — the one place
    that knows where a job's structure link lives."""
    params = (meta or {}).get("params") or {}
    if not isinstance(params, dict):
        return None
    return _as_int(params.get("structure_ref_id"))


def _quest_id_in_tags(tags: Iterable[Any]) -> int | None:
    for t in tags:
        m = _QUEST_TAG_RE.match(str(t))
        if m:
            return int(m.group(1))
    return None


def quest_ids_for_structures(conn: Any, structure_ids: Iterable[int]) -> dict[int, int]:
    """``{structure_ref_id: quest_id}`` from the open ``quest:<id>`` tags —
    ONE bulk read for a whole page of jobs, never one ``tags_for`` per
    row (a sweep queues dozens; td573331 fix round).

    ``conn`` is the caller's already-held connection — /status?tab=now
    shares one request connection and must not check out a second pooled
    one while it is held.
    """
    ids = sorted({int(i) for i in structure_ids})
    if not ids:
        return {}
    out: dict[int, int] = {}
    for sid, value in conn.execute(
        "SELECT rt.ref_id, t.value FROM ref_tags rt "
        "JOIN tags t ON t.tag_id = rt.tag_id "
        "WHERE rt.ref_id = ANY(%s) AND t.namespace = 'OPEN' "
        "AND t.value LIKE 'quest:%%'",
        (ids,),
    ).fetchall():
        m = _QUEST_TAG_RE.match(str(value))
        if m:
            out.setdefault(int(sid), int(m.group(1)))
    return out


def quest_id_from_meta(
    meta: dict[str, Any] | None,
    structure_quests: Mapping[int, int] | None = None,
) -> int | None:
    """Resolve a job's quest from ``meta`` alone. Returns the quest's ref_id.

    Order: ``params.quest_id`` (quest_tick — schema-required — and any
    other type that carries it, e.g. struct_search), then
    ``coordinator_state.quest_id``, then ``params.structure_ref_id`` →
    the open ``quest:<id>`` tag on that structure ref
    (:func:`precis.quest.tagging.quest_tag_value`'s stamp; struct_relax),
    looked up in the precomputed ``structure_quests`` map. Pure — the
    query belongs to the caller's page read, not to this resolver.

    This is deliberately the meta-only half of quest resolution: it does
    NOT walk the job's ``refs.parent_id`` todo chain — sibling slice W5
    (td573299) builds that resolver and calls this one as its fallback.
    """
    meta = meta or {}
    params = meta.get("params") or {}
    qid = _as_int(params.get("quest_id")) if isinstance(params, dict) else None
    if qid is None:
        state = meta.get("coordinator_state") or {}
        qid = _as_int(state.get("quest_id")) if isinstance(state, dict) else None
    if qid is not None:
        return qid
    sid = structure_ref_id_from_meta(meta)
    if sid is None:
        return None
    return (structure_quests or {}).get(sid)


def collapse_upkeep(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse live upkeep job rows into one synthetic line per job type.

    ``rows`` are live-lane dicts with keys ``ref_id``, ``job_type``,
    ``status`` (``'running'``/``'queued'``), ``age_s`` (seconds the row has
    sat in its lane), ``params``, ``meta``, ``summary`` and ``last_event``
    — the shape /status?tab=now builds. Rows whose ``job_type`` is not in
    :data:`PLATFORM_UPKEEP` are ignored.

    Each output dict carries ``upkeep=True``, ``job_type``, ``ref_id`` (a
    representative row — first running, else first queued), ``running`` /
    ``queued`` counts, ``oldest_ago`` (``_short_wait`` of the oldest
    ``age_s``), ``quest`` (:data:`UPKEEP_QUEST_HANDLE`) and ``sentence``:
    ``"platform upkeep: <base sentence>, M running, N queued, oldest T"``.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        jt = row.get("job_type")
        if jt in PLATFORM_UPKEEP:
            groups.setdefault(jt, []).append(row)
    out: list[dict[str, Any]] = []
    for jt in sorted(groups):
        group = groups[jt]
        running_n = sum(1 for r in group if r.get("status") == "running")
        queued_n = sum(1 for r in group if r.get("status") != "running")
        ages = [
            float(r["age_s"]) for r in group if isinstance(r.get("age_s"), (int, float))
        ]
        oldest = max(ages) if ages else None
        rep = next((r for r in group if r.get("status") == "running"), group[0])
        base = job_sentence(
            jt,
            params=rep.get("params"),
            meta=rep.get("meta"),
            status=rep.get("status"),
            summary=rep.get("summary"),
            last_event=rep.get("last_event"),
            quest_headline=UPKEEP_QUEST_HANDLE,
            error=None,
        )
        counts = f"{running_n} running, {queued_n} queued"
        if oldest is not None:
            counts += f", oldest {_short_wait(oldest)}"
        out.append(
            {
                "upkeep": True,
                "job_type": jt,
                "ref_id": rep.get("ref_id"),
                "running": running_n,
                "queued": queued_n,
                "oldest_ago": _short_wait(oldest),
                "quest": UPKEEP_QUEST_HANDLE,
                "sentence": f"platform upkeep: {base}, {counts}",
            }
        )
    return out


def job_row_sentence(
    store: Any,
    *,
    meta: dict[str, Any] | None,
    status: str | None,
    blocks: list[Any] | None = None,
) -> str:
    """Store-touching convenience for single-job renders (the MCP card):
    pulls the newest ``job_summary`` / ``job_event`` text off ``blocks``
    (``ChunkRow``-likes with ``.chunk_kind`` / ``.text``) and resolves the
    quest headline through :func:`quest_id_from_meta` plus one
    ``kind='quest'`` read. Never raises on a thin row.
    """
    meta = meta or {}
    summary = last_event = None
    for b in reversed(blocks or []):
        kind = getattr(b, "chunk_kind", None)
        if kind == "job_summary" and summary is None:
            summary = getattr(b, "text", None)
        elif kind == "job_event" and last_event is None:
            last_event = getattr(b, "text", None)
        if summary is not None and last_event is not None:
            break
    sid = structure_ref_id_from_meta(meta)
    structure_quests: dict[int, int] = {}
    if sid is not None and store is not None:
        try:
            q = _quest_id_in_tags(store.tags_for(sid))
        except Exception:
            q = None
        if q is not None:
            structure_quests[sid] = q
    qid = quest_id_from_meta(meta, structure_quests)
    headline = None
    if qid is not None and store is not None:
        try:
            qref = store.get_ref(kind="quest", id=qid)
        except Exception:
            qref = None
        headline = getattr(qref, "title", None) or f"qu{qid}"
    return job_sentence(
        str(meta.get("job_type") or "job"),
        params=meta.get("params"),
        meta=meta,
        status=status,
        summary=summary,
        last_event=last_event,
        quest_headline=headline,
        error=meta.get("error"),
    )
