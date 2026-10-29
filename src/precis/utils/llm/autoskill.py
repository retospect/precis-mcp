"""Autoskill — router-side warm start from similar sessions.

Spec: ``docs/backlog/autoskill.md`` (supersedes the "bimodal only" rule of
``mcp-surface-economy.md`` §2 for router-routed agent calls).

Every tool-using :func:`precis.utils.llm.router.route` call is embedded once
(:func:`prepare`); the nearest past routed sessions in ``autoskill_sessions``
(migration 0264) vote on which skills and memories this call is likely to
need, and :func:`apply` appends a block to the END of the user prompt:

* **core skills** — in full, when ≥80% of the weighted neighbours used them;
* **tail pointers** — slug + one-line summary for the next skills;
* **memory pointers** — id + live title for the memories fetched by at
  least 3 distinct neighbour sessions (a memory must be requested, so a
  pointer's fetch rate is an exact usefulness measurement);
* the **gripe stanza**, always (two lines, not the 26 KB skill).

After the call returns, :func:`record` snapshots what the session actually
used (from ``tool_calls``, joined on the agentlog id) and inserts the row, so
the history builds itself. 5% of calls are hold-outs: recorded, no block, no
bimodal fallback — the control group for the comparison.

**Never fails a call.** Every fault (no store, no embedder, DB down, bad row)
logs and degrades to "route the request unchanged"; the store is the
process-bound one :mod:`precis.route_log` already carries, so a DB-free
caller (or a test with nothing bound) is a silent no-op. Config is read raw
at the call site (Tier 2, ``docs/conventions/env-vars.md``): opt-OUT
``PRECIS_AUTOSKILL=off``, hold-out fraction ``PRECIS_AUTOSKILL_HOLDOUT``.

This module must not import :mod:`precis.utils.llm.router` at module level —
the router imports it.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
from dataclasses import dataclass, field
from dataclasses import replace as _replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from precis import route_log

if TYPE_CHECKING:
    from precis.store import Store
    from precis.utils.llm.router import LlmRequest, LlmResult

log = logging.getLogger(__name__)

_ENABLE_ENV = "PRECIS_AUTOSKILL"
_HOLDOUT_ENV = "PRECIS_AUTOSKILL_HOLDOUT"
_DEFAULT_HOLDOUT = 0.05

#: Neighbour lookup: top-N by cosine, similarity floor, history window.
_NEIGHBOURS = 50
_MIN_SIM = 0.5
_WINDOW_DAYS = route_log.AUTOSKILL_RETENTION_DAYS
#: Below this many neighbours the history is too thin to vote — fall back
#: to today's bimodal :func:`~precis.skill_index.injection.match_skill`.
_MIN_NEIGHBOURS = 20
#: Recency weight: ``sim × 0.5^(age_days / half-life)``.
_HALF_LIFE_DAYS = 14.0

#: Selection thresholds (weighted share of neighbours).
_CORE_SHARE = 0.80
_CORE_CHARS = 24_000
_TAIL_SHARE = 0.10
_TAIL_CAP = 8
#: A memory pointer needs this many DISTINCT neighbour sessions that fetched
#: it (a count, so it does not scale with the neighbour count the way a
#: share threshold does); ranked by weighted share, at most ``_MEMORY_CAP``.
_MEMORY_MIN_SESSIONS = 3
_MEMORY_CAP = 5

#: Query text cap (chars) — the embedder's window is larger, but the task
#: statement leads the prompt and a remote embedder pays per byte.
_QUERY_CHARS = 8_000
#: ``autoskill_sessions.embedding`` is ``vector(1024)``.
_EMBED_DIM = 1024

_GRIPE_STANZA = (
    "Something broken, missing or confusing in precis itself? File it: "
    "put(kind='gripe', text='<what you tried, what happened, what you expected>').\n"
    "Full guide: get(kind='skill', id='precis-gripe-help')."
)


# ── enablement / config ──────────────────────────────────────────────────


def enabled() -> bool:
    """``PRECIS_AUTOSKILL=off`` disables autoskill; any other value
    (including unset) leaves it on — an opt-OUT knob, like
    :func:`precis.skill_index.injection._enabled`."""
    return os.environ.get(_ENABLE_ENV, "").strip().lower() != "off"


def active() -> bool:
    """Enabled AND a store is bound, i.e. :func:`route` will actually run
    autoskill in this process. The bimodal prompt-assembly sites key their
    skip on this, not :func:`enabled` alone — in a process with no bound
    store autoskill never injects, so skipping the bimodal call too would
    leave the session with neither."""
    return enabled() and route_log.bound_store() is not None


def holdout_fraction() -> float:
    """Fraction of calls that get no block (``PRECIS_AUTOSKILL_HOLDOUT``,
    default 0.05); a bad or out-of-range value falls back / clamps."""
    raw = os.environ.get(_HOLDOUT_ENV)
    if raw is None or not raw.strip():
        return _DEFAULT_HOLDOUT
    try:
        return min(1.0, max(0.0, float(raw)))
    except ValueError:
        log.warning(
            "autoskill: %s=%r is not a float; using default %.2f",
            _HOLDOUT_ENV,
            raw,
            _DEFAULT_HOLDOUT,
        )
        return _DEFAULT_HOLDOUT


def _is_holdout() -> bool:
    return random.random() < holdout_fraction()


# ── embedder ─────────────────────────────────────────────────────────────

_embedder: Any | None = None
_embedder_failed = False


def _reset_for_tests() -> None:
    """Drop the cached embedder (and cached construction failure)."""
    global _embedder, _embedder_failed
    _embedder = None
    _embedder_failed = False


def _get_embedder() -> Any | None:
    """Process-wide embedder, built once from ``PrecisConfig`` via
    :func:`precis.skill_index.injection._default_embedder` (a failed or
    wrong-dimension construction is cached — it can load a model)."""
    global _embedder, _embedder_failed
    if _embedder is not None:
        return _embedder
    if _embedder_failed:
        return None
    from precis.skill_index.injection import _default_embedder

    emb = _default_embedder()
    if emb is None:
        _embedder_failed = True
        log.warning("autoskill: no embedder available; disabled for this process")
        return None
    dim = getattr(emb, "dim", _EMBED_DIM)
    if dim != _EMBED_DIM:
        _embedder_failed = True
        log.warning(
            "autoskill: embedder dim %s != %d (autoskill_sessions.embedding); "
            "disabled for this process",
            dim,
            _EMBED_DIM,
        )
        return None
    _embedder = emb
    return emb


# ── plan ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Plan:
    """What :func:`prepare` decided for one call: the query embedding, the
    block to append (``""`` for a hold-out), and what it injects."""

    embedding: list[float]
    block: str
    injected: dict[str, Any]
    holdout: bool = False


@dataclass(frozen=True)
class Neighbour:
    """One past session within reach: its recency-weighted similarity and
    what it used (plus the skills it was injected in full, as credit)."""

    weight: float
    skills: frozenset[str] = field(default_factory=frozenset)
    memories: frozenset[int] = field(default_factory=frozenset)


def _empty_injected(fallback: str | None = None) -> dict[str, Any]:
    return {"core": [], "tail": [], "memories": [], "fallback": fallback}


def neighbour_weight(sim: float, age_days: float) -> float:
    """``sim × 0.5^(age_days / 14)`` — newer, closer sessions count more."""
    return sim * 0.5 ** (max(0.0, age_days) / _HALF_LIFE_DAYS)


def shares(neighbours: list[Neighbour]) -> tuple[dict[str, float], dict[int, float]]:
    """Weighted share of the neighbours using each skill / memory:
    ``Σ weight(users) / Σ weight(all)``."""
    total = sum(n.weight for n in neighbours)
    skills: dict[str, float] = {}
    memories: dict[int, float] = {}
    if total <= 0:
        return skills, memories
    for n in neighbours:
        for s in n.skills:
            skills[s] = skills.get(s, 0.0) + n.weight
        for m in n.memories:
            memories[m] = memories.get(m, 0.0) + n.weight
    return (
        {k: v / total for k, v in skills.items()},
        {k: v / total for k, v in memories.items()},
    )


def memory_sessions(neighbours: list[Neighbour]) -> dict[int, int]:
    """``{memory id: number of distinct neighbour sessions that fetched it}``
    (each :class:`Neighbour` is one session)."""
    out: dict[int, int] = {}
    for n in neighbours:
        for m in n.memories:
            out[m] = out.get(m, 0) + 1
    return out


def _ranked(share: dict[Any, float], floor: float) -> list[Any]:
    """Keys with share ≥ ``floor`` (epsilon-tolerant), best first, ties by key."""
    return [
        k
        for k, v in sorted(share.items(), key=lambda kv: (-kv[1], str(kv[0])))
        if v >= floor - 1e-9
    ]


def select_skills(
    share: dict[str, float], sizes: dict[str, int]
) -> tuple[list[str], list[str]]:
    """Split skills into ``(core, tail)``.

    Core: share ≥ 0.80 in share order while the running body size stays
    within 24,000 chars; the first skill that does not fit — and every later
    core-eligible one — is demoted to a tail pointer (share order is the
    priority). Tail: demoted skills first, then the next skills with share
    ≥ 0.10, at most 8 in all. A skill absent from ``sizes`` (no longer in
    the corpus) is dropped from both tiers."""
    known = {s: v for s, v in share.items() if s in sizes}
    core: list[str] = []
    tail: list[str] = []
    used = 0
    full = False
    for slug in _ranked(known, _CORE_SHARE):
        if not full and used + sizes[slug] <= _CORE_CHARS:
            core.append(slug)
            used += sizes[slug]
        else:
            full = True
            tail.append(slug)
    for slug in _ranked(known, _TAIL_SHARE):
        if slug not in core and slug not in tail:
            tail.append(slug)
    return core, tail[:_TAIL_CAP]


def memory_candidates(share: dict[int, float], sessions: dict[int, int]) -> list[int]:
    """Memory ids fetched by ≥ 3 distinct neighbour sessions, best weighted
    share first (ties by id)."""
    return _ranked(
        {m: v for m, v in share.items() if sessions.get(m, 0) >= _MEMORY_MIN_SESSIONS},
        0.0,
    )


def select_memories(
    share: dict[int, float], sessions: dict[int, int], live: set[int]
) -> list[int]:
    """Memory ids fetched by ≥ 3 distinct neighbour sessions that are ``live``
    (not deleted/STALE), ranked by weighted share, at most 5."""
    return [m for m in memory_candidates(share, sessions) if m in live][:_MEMORY_CAP]


# ── skill / memory text ──────────────────────────────────────────────────


def _skill_body(slug: str) -> str | None:
    """The skill's served text minus its front matter, or ``None`` when the
    slug is unknown."""
    from precis.handlers.skill import _load_skill

    text = _load_skill(slug)
    if text is None:
        return None
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :]
    return text.strip()


def _skill_line(slug: str) -> str:
    """Pointer text for a tail skill: the front-matter ``summary:`` (else the
    first ``answers:`` question, else the title)."""
    from precis.handlers.skill import _load_skill, _skill_title
    from precis.utils.skill_common import parse_frontmatter

    text = _load_skill(slug) or ""
    try:
        fm = parse_frontmatter(text)
        line = fm.summary or (fm.answers[0] if fm.answers else "")
    except Exception:
        line = ""
    return (line or _skill_title(slug)).strip()


def _live_memory_titles(store: Store, ids: list[int]) -> dict[int, str]:
    """``{id: title}`` for the ids that are live memories right now —
    skipping retired (deleted) refs and any carrying a ``STALE:`` tag."""
    if not ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, r.title FROM refs r "
            "WHERE r.kind = 'memory' AND r.retired_at IS NULL "
            "AND r.ref_id = ANY(%s) "
            "AND NOT EXISTS (SELECT 1 FROM ref_tags rt "
            "  JOIN tags t ON t.tag_id = rt.tag_id "
            "  WHERE rt.ref_id = r.ref_id AND t.namespace = 'STALE' "
            "  AND (rt.expires_at IS NULL OR rt.expires_at > now()))",
            (ids,),
        ).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


# ── block rendering ──────────────────────────────────────────────────────


def render_block(
    core: list[str],
    tail: list[str],
    memories: list[tuple[int, str]],
    *,
    fallback: bool = False,
) -> str:
    """The text appended to the prompt. The gripe stanza is always there."""
    parts: list[str] = ["---", "## Autoskill: context from similar sessions"]
    if core:
        how = (
            "matched to your task and injected in full"
            if fallback
            else "used by most similar past sessions and injected in full"
        )
        parts.append(f"Skills {how} — no get(kind='skill', ...) needed for these:")
        for slug in core:
            parts.append(f"### Skill: {slug}\n\n{_skill_body(slug) or ''}")
    if tail:
        parts.append(
            "Also used by similar sessions — read one with "
            "get(kind='skill', id=<slug>) if it fits:"
        )
        parts.extend(f"- {slug} — {_skill_line(slug)}" for slug in tail)
    if memories:
        parts.append(
            "Memories similar sessions read — fetch with get(kind='memory', id=<id>):"
        )
        parts.extend(f"- memory {mid} — {title}" for mid, title in memories)
    parts.append(_GRIPE_STANZA)
    return "\n\n".join(parts)


# ── prepare ──────────────────────────────────────────────────────────────


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def query_text(req: LlmRequest) -> str:
    """The user's side of the request: ``req.prompt``, else the user turns of
    ``req.messages``; capped at :data:`_QUERY_CHARS`."""
    text = req.prompt or ""
    if not text.strip() and req.messages:
        text = "\n\n".join(
            _text_of(m.get("content")) for m in req.messages if m.get("role") == "user"
        )
    return text.strip()[:_QUERY_CHARS]


def _vec_literal(vec: list[float]) -> str:
    return str(list(vec))


def _injected_credit(injected: Any) -> set[str]:
    """Skills a session was handed in full: ``injected.core`` plus the
    bimodal ``injected.fallback`` slug."""
    if not isinstance(injected, dict):
        return set()
    out = {str(s) for s in injected.get("core") or []}
    fallback = injected.get("fallback")
    if fallback:
        out.add(str(fallback))
    return out


def _neighbours(store: Store, vec: list[float]) -> list[Neighbour]:
    """Top-50 past sessions by cosine (attributed, non-errored, in window,
    similarity ≥ 0.5), each weighted by similarity and age.

    A neighbour's skills are what it used PLUS what it was injected in full
    (``injected.core`` and ``injected.fallback``): an agent handed a skill
    never fetches it, so without this credit an injected skill would decay
    out of the core. The stored ``used`` stays raw evidence. The query keeps
    only ``errored IS NOT TRUE`` rows, so an errored session earns no credit;
    hold-out rows carry no injected core."""
    lit = _vec_literal(vec)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT used, injected, 1 - (embedding <=> %s::vector) AS sim, "
            "       extract(epoch FROM (now() - ts)) / 86400.0 AS age_days "
            "FROM autoskill_sessions "
            "WHERE used IS NOT NULL AND errored IS NOT TRUE "
            "  AND ts > now() - (%s || ' days')::interval "
            "ORDER BY embedding <=> %s::vector LIMIT %s",
            (lit, _WINDOW_DAYS, lit, _NEIGHBOURS),
        ).fetchall()
    out: list[Neighbour] = []
    for used, injected, sim, age_days in rows:
        sim = float(sim)
        if sim < _MIN_SIM:
            continue
        used = used if isinstance(used, dict) else {}
        skills = {str(s) for s in used.get("skills") or []}
        skills |= _injected_credit(injected)
        mems: set[int] = set()
        for m in used.get("memories") or []:
            try:
                mems.add(int(m))
            except (TypeError, ValueError):
                continue
        out.append(
            Neighbour(
                weight=neighbour_weight(sim, float(age_days)),
                skills=frozenset(skills),
                memories=frozenset(mems),
            )
        )
    return out


def _fallback_plan(vec: list[float], text: str) -> Plan:
    """Thin history: today's bimodal injection, its single skill as core."""
    from precis.skill_index.injection import match_skill

    match = match_skill(text)
    if match is None or _skill_body(match.slug) is None:
        return Plan(vec, render_block([], [], []), _empty_injected())
    return Plan(
        vec,
        render_block([match.slug], [], [], fallback=True),
        {"core": [match.slug], "tail": [], "memories": [], "fallback": match.slug},
    )


def _select_plan(store: Store, vec: list[float], neighbours: list[Neighbour]) -> Plan:
    skill_share, mem_share = shares(neighbours)
    mem_sessions = memory_sessions(neighbours)
    sizes: dict[str, int] = {}
    for slug in skill_share:
        body = _skill_body(slug)
        if body is not None:
            sizes[slug] = len(body)
    core, tail = select_skills(skill_share, sizes)
    cand = memory_candidates(mem_share, mem_sessions)
    titles = _live_memory_titles(store, cand)
    mems = select_memories(mem_share, mem_sessions, set(titles))
    block = render_block(core, tail, [(m, titles[m]) for m in mems])
    return Plan(
        vec,
        block,
        {"core": core, "tail": tail, "memories": mems, "fallback": None},
    )


def prepare(req: LlmRequest) -> Plan | None:
    """Embed the request and decide the injection; ``None`` (route the
    request unchanged, record nothing) on any fault, with no bound store, or
    with no user text."""
    try:
        store = route_log.bound_store()
        if store is None:
            return None
        text = query_text(req)
        if not text:
            return None
        embedder = _get_embedder()
        if embedder is None:
            return None
        vec = [float(x) for x in embedder.embed_one(text)]
        if _is_holdout():
            return Plan(vec, "", _empty_injected(), holdout=True)
        neighbours = _neighbours(store, vec)
        if len(neighbours) < _MIN_NEIGHBOURS:
            return _fallback_plan(vec, text)
        return _select_plan(store, vec, neighbours)
    except Exception:
        log.warning("autoskill: prepare failed; routing unchanged", exc_info=True)
        return None


# ── apply ────────────────────────────────────────────────────────────────


def apply(plan: Plan | None, req: LlmRequest) -> LlmRequest:
    """Append the plan's block to the END of the user side of ``req`` — the
    prompt and, when set, the last user message (the claude transports read
    one, the local/OSS wires the other). Never ``system_prompt``: the cached
    system prefix stays byte-identical. A hold-out / ``None`` plan returns
    ``req`` unchanged."""
    if plan is None or not plan.block:
        return req
    try:
        changes: dict[str, Any] = {}
        if req.prompt:
            changes["prompt"] = f"{req.prompt}\n\n{plan.block}"
        if req.messages:
            msgs = list(req.messages)
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].get("role") != "user":
                    continue
                m = dict(msgs[i])
                content = m.get("content")
                if isinstance(content, list):
                    m["content"] = [*content, {"type": "text", "text": plan.block}]
                else:
                    m["content"] = f"{content or ''}\n\n{plan.block}"
                msgs[i] = m
                changes["messages"] = msgs
                break
        return _replace(req, **changes) if changes else req
    except Exception:
        log.warning("autoskill: apply failed; routing unchanged", exc_info=True)
        return req


# ── record ───────────────────────────────────────────────────────────────

_ID_RE = re.compile(r"(?:me)?(\d+)")


def _agentlog_id(req: LlmRequest) -> int | None:
    raw = (req.env_overlay or {}).get("PRECIS_CURRENT_AGENTLOG")
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _arg_ids(sample: Any) -> list[Any]:
    """The ``id`` value(s) of a stored ``arg_sample`` (a JSON object, or its
    text), as a list."""
    if isinstance(sample, str):
        try:
            sample = json.loads(sample)
        except ValueError:
            return []
    if not isinstance(sample, dict):
        return []
    val = sample.get("id")
    if isinstance(val, list):
        return val
    return [] if val is None else [val]


def snapshot_used(
    store: Store, agentlog_id: int, since: datetime
) -> dict[str, list[Any]]:
    """What the call used, from its ``tool_calls`` rows (``ts >= since``):
    ``get(kind='skill', id=X)`` → skill X (explicit read, strongest signal);
    any verb on kind K → the skills ``kind_skill_hint(K)`` names; and
    ``get(kind='memory', id=N)`` → memory N."""
    from precis.handlers.skill import skill_exists
    from precis.skill_index.kind_skills import kind_skill_slugs

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT verb, kind, outcome, arg_sample FROM tool_calls "
            "WHERE agentlog_id = %s AND ts >= %s",
            (agentlog_id, since),
        ).fetchall()
    skills: set[str] = set()
    memories: set[int] = set()
    hints: dict[str, list[str]] = {}
    for verb, kind, outcome, sample in rows:
        if not kind:
            continue
        kind = str(kind)
        if kind != "skill":
            if kind not in hints:
                hints[kind] = kind_skill_slugs(kind)
            skills.update(hints[kind])
        if verb != "get" or outcome == "error":
            continue
        if kind == "skill":
            for raw in _arg_ids(sample):
                slug = re.split(r"[~#]", str(raw), maxsplit=1)[0].strip()
                if slug and skill_exists(slug):
                    skills.add(slug)
        elif kind == "memory":
            for raw in _arg_ids(sample):
                m = _ID_RE.fullmatch(str(raw).strip())
                if m:
                    memories.add(int(m.group(1)))
    return {"skills": sorted(skills), "memories": sorted(memories)}


def record(
    plan: Plan | None, req: LlmRequest, result: LlmResult, started: datetime
) -> None:
    """Snapshot ``used`` and insert the ``autoskill_sessions`` row —
    best-effort, never raises. No agentlog id on the request → ``used`` is
    ``NULL`` (the row still records the injection)."""
    if plan is None:
        return
    try:
        store = route_log.bound_store()
        if store is None:
            return
        agentlog_id = _agentlog_id(req)
        used = (
            snapshot_used(store, agentlog_id, started)
            if agentlog_id is not None
            else None
        )
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO autoskill_sessions "
                "(source, agentlog_id, embedding, holdout, injected, used, errored) "
                "VALUES (%s, %s, %s::vector, %s, %s::jsonb, %s::jsonb, %s)",
                (
                    req.source or "",
                    agentlog_id,
                    _vec_literal(plan.embedding),
                    plan.holdout,
                    json.dumps(plan.injected),
                    None if used is None else json.dumps(used),
                    result.error is not None,
                ),
            )
            conn.commit()
    except Exception:
        log.warning("autoskill: record failed", exc_info=True)


__all__ = [
    "Neighbour",
    "Plan",
    "active",
    "apply",
    "enabled",
    "holdout_fraction",
    "memory_candidates",
    "memory_sessions",
    "prepare",
    "record",
    "render_block",
    "select_memories",
    "select_skills",
    "shares",
]
