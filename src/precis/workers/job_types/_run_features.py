"""Per-run features rows for task-aware routing (gr595544).

Spec home: the task-aware-routing spec (me568172, demand contract
me568185); the parked model-based requirement classifier
(:mod:`precis.utils.llm.requirement`, td568400 S0b) is the only
pre-analysis path today. This module is the cheap half the spec asks
for first: one ``slice_run_features`` row per ``run_slice`` launch —
written alongside the attempts ledger the ``kind='job'`` refs already
are — extracted in microseconds with no model call:

* ``prompt_chars`` — the rendered brief's length, the real prompt size.
* ``handle_count`` — distinct well-formed graph handles in the task
  text (title + body + newest brief/handoff): the context-breadth
  signal.
* ``fisheye_2hop_nodes`` — live refs within two link-hops of the todo,
  the fisheye's neighbourhood count.
* ``max_dep_depth`` — the longest ``blocked-by`` chain through the
  todo, blockers above or dependents below, whichever is longer.
* ``constraint_clauses`` — clauses carrying a constraint cue
  (``must``/``never``/``only``/…): how guarded the task reads.
* ``op_type`` — ``create`` / ``modify`` / ``lookup`` from cue verbs;
  ``NULL`` when the text carries none.
* ``novelty_cosine`` — cosine distance to the nearest past row's
  ``prompt_embedding``: how new this task is to the fleet.
* plus the pick's own context: ``submitter`` (the todo's ``set_by``),
  ``task_kind`` (the dispatch ``meta.task_class``), ``node_kind``,
  ``lane`` / ``lane_class`` (cost class) / ``model`` / ``effort``, the
  1-based ``attempt`` ordinal off the todo's ``attempt:<n>`` tags, and
  ``launch`` — which run of this job the row is.

The row's key is ``(job_ref_id, launch)``, not the job alone: a parked
outage's :func:`~precis.workers.job_types.run_slice._relaunch_when_due`
resubmits the SAME job ref without burning an ``attempt:`` tag, so a
job-keyed row would drop every launch after the first. ``attempt``
stays a feature (the burned-attempt ordinal); ``launch`` is this job's
1-based row count.

After a few hundred rows the table fits a small gradient-boosted tree
or logistic regression predicting whether the small rung passes the
hard gates — lift measured against recipe-only routing, no hand-picked
weights. The outcome lives on the job ref's own STATUS, joined through
``job_ref_id``.

Dark by construction, like :mod:`precis.route_log` and
:mod:`_lane_decide`: :func:`record` never raises — a missing table
(migrations not yet applied), a dead store or a down embedder logs at
debug and returns ``False``; it can never break a launch. The vector
pair (``prompt_embedding``, ``novelty_cosine``) stays NULL unless a
``PRECIS_EMBEDDER_URL`` remote answers one bounded call
(:data:`_EMBED_TIMEOUT_S`, zero retries); ``prompt_text`` keeps the
embedded task text so a backfill can vectorise rows written while the
service was down.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any

from pgvector import Vector

from precis.plans.nodes import node_kind
from precis.utils import handle_registry

log = logging.getLogger(__name__)

#: Env naming the remote embedder service (``precis serve-embeddings``);
#: unset means no novelty pass — the row still lands.
_EMBED_URL_ENV = "PRECIS_EMBEDDER_URL"
#: Bound on the one novelty embed a submit pays. Zero retries: a down
#: service costs a NULL pair, never a stalled launch.
_EMBED_TIMEOUT_S = 10.0
#: Text cap for the embedded task text (bge-m3's window is far larger,
#: but the signal saturates long before).
_EMBED_CHARS = 8_000
#: Bound on the stored ``prompt_text`` — the backfill substrate.
_STORED_TEXT_CHARS = 32_000

#: Characters a handle may be wrapped in inside prose (same set
#: ``plans.route`` strips before judging tokens).
_HANDLE_WRAP = ".,;:()[]{}`'\"*_#<>|"

#: A clause boundary for the constraint count: sentence punctuation or
#: a line break.
_CLAUSE_SPLIT = re.compile(r"[.!?\n;]")

#: Cue words that make a clause a constraint the task imposes.
_CONSTRAINT_CUE = re.compile(
    r"\b(?:must|never|cannot|can't|won't|don't|do not|may not|shall not"
    r"|should not|shouldn't|forbidden|deny|denied|always|only|exactly"
    r"|at most|at least|refuse|reject)\b",
    re.IGNORECASE,
)

#: Operation-type cue verbs. ``lookup`` wins only when no edit cue
#: appears at all — a task that names any mutation is not a read.
_LOOKUP_CUE = re.compile(
    r"\b(?:find|check|search|report|list|audit|review|verify|answer"
    r"|investigate|look(?:ing)? ?up|summari[sz]e|triage|diagnose|explain"
    r"|inspect|read)\b",
    re.IGNORECASE,
)
_CREATE_CUE = re.compile(
    r"\b(?:create|add|new|mint|implement|write|author|scaffold|introduce"
    r"|build|generate|draft)\b",
    re.IGNORECASE,
)
_MODIFY_CUE = re.compile(
    r"\b(?:fix|update|change|refactor|rename|delete|remove|migrate|move"
    r"|edit|patch|adjust|repair|close|land|retire|tweak|correct)\b",
    re.IGNORECASE,
)

#: The fisheye's two-hop node count: distinct live refs reachable from
#: the todo over ``links`` in at most two hops, the focus excluded.
#: ``UNION`` (not ``UNION ALL``) dedups each ring, so a cyclic
#: neighbourhood still terminates at the depth bound.
_TWO_HOP_SQL = """
WITH RECURSIVE walk(id, depth) AS (
    SELECT %s::bigint, 0
    UNION
    SELECT CASE WHEN l.src_ref_id = w.id THEN l.dst_ref_id
                ELSE l.src_ref_id END,
           w.depth + 1
      FROM links l JOIN walk w
        ON (l.src_ref_id = w.id OR l.dst_ref_id = w.id) AND w.depth < 2
)
SELECT count(*) FROM (
    SELECT DISTINCT w.id FROM walk w
     JOIN refs r ON r.ref_id = w.id AND r.retired_at IS NULL
    WHERE w.id <> %s
) n
"""

#: The task's dependency depth through the todo: the longest
#: ``blocked-by`` chain — ``src`` waits on ``dst`` — up to what it waits
#: on or down to what waits on it, whichever is longer. ``parent_id``
#: is containment (the plan's outline), not dependency, so it measures
#: the wrong axis here. Dependents are counted live only, like the
#: doable view's blocker policy; the depth bound stops a cycle, and
#: ``UNION`` keeps a revisited pair from recursing forever.
_DEP_DEPTH_SQL = """
WITH RECURSIVE up(id, lvl) AS (
    SELECT l.dst_ref_id, 1 FROM links l
     WHERE l.src_ref_id = %s AND l.relation = 'blocked-by'
    UNION
    SELECT l.dst_ref_id, u.lvl + 1 FROM links l
     JOIN up u ON l.src_ref_id = u.id
     WHERE l.relation = 'blocked-by' AND u.lvl < 25
), down(id, lvl) AS (
    SELECT l.src_ref_id, 1 FROM links l
     JOIN refs r ON r.ref_id = l.src_ref_id AND r.retired_at IS NULL
     WHERE l.dst_ref_id = %s AND l.relation = 'blocked-by'
    UNION
    SELECT l.src_ref_id, d.lvl + 1 FROM links l
     JOIN refs r ON r.ref_id = l.src_ref_id AND r.retired_at IS NULL
     JOIN down d ON l.dst_ref_id = d.id
     WHERE l.relation = 'blocked-by' AND d.lvl < 25
)
SELECT GREATEST(COALESCE((SELECT max(lvl) FROM up), 0),
                COALESCE((SELECT max(lvl) FROM down), 0))
"""

#: This run's 1-based attempt ordinal: one open ``attempt:<n>`` tag
#: stands per prior burned attempt, so the current run is max(n) + 1.
_ATTEMPT_SQL = """
SELECT COALESCE(max(substring(t.value from 'attempt:(\\d+)')::int), 0) + 1
  FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
 WHERE rt.ref_id = %s AND t.namespace = 'OPEN' AND t.value ~ '^attempt:\\d+$'
"""

#: This launch's 1-based ordinal on the job: a relaunched job ref gets
#: a fresh row under ``(job_ref_id, launch)``, never a key collision.
_LAUNCH_SQL = """
SELECT COALESCE(max(launch), 0) + 1 FROM slice_run_features
 WHERE job_ref_id = %s
"""

#: The nearest past prompt's cosine distance (pgvector ``<=>``), same
#: embedder only — a model swap would compare apples to oranges.
_NOVELTY_SQL = """
SELECT min(prompt_embedding <=> %s)
  FROM slice_run_features
 WHERE prompt_embedding IS NOT NULL AND prompt_embedder = %s
"""

_INSERT_SQL = """
INSERT INTO slice_run_features (
    job_ref_id, todo_ref_id, attempt, launch, task_kind, node_kind,
    lane, lane_class, model, effort, submitter,
    prompt_chars, handle_count, fisheye_2hop_nodes, max_dep_depth,
    constraint_clauses, op_type, novelty_cosine,
    prompt_sha256, prompt_text, prompt_embedder, prompt_embedding
) VALUES (
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s
)
"""


def _handle_count(text: str) -> int:
    """Distinct well-formed graph handles the text names."""
    seen: set[str] = set()
    for token in text.split():
        t = token.strip(_HANDLE_WRAP)
        if handle_registry.is_well_formed(t):
            seen.add(handle_registry.normalize(t))
    return len(seen)


def _constraint_clauses(text: str) -> int:
    """Clauses carrying a constraint cue (``must``/``never``/``only``…)."""
    return sum(
        1 for clause in _CLAUSE_SPLIT.split(text) if _CONSTRAINT_CUE.search(clause)
    )


def _op_type(text: str) -> str | None:
    """``create`` / ``modify`` / ``lookup`` from cue verbs, else ``None``.

    An edit cue anywhere makes it a write task; ``create`` wins over
    ``modify`` when both appear (the new thing is usually the point);
    ``lookup`` only when the text names a read and no edit at all.
    """
    create = bool(_CREATE_CUE.search(text))
    modify = bool(_MODIFY_CUE.search(text))
    if create:
        return "create"
    if modify:
        return "modify"
    if _LOOKUP_CUE.search(text):
        return "lookup"
    return None


def extract(task_text: str) -> dict[str, Any]:
    """The text-only feature bundle — pure, no store, microseconds."""
    return {
        "handle_count": _handle_count(task_text),
        "constraint_clauses": _constraint_clauses(task_text),
        "op_type": _op_type(task_text),
    }


def _embed(text: str, embedder: Any | None) -> tuple[list[float], str] | None:
    """``(vector, model)`` for ``text``, or ``None`` — dark on any miss.

    An injected ``embedder`` (tests) is used as-is; otherwise a
    ``PRECIS_EMBEDDER_URL`` remote gets one bounded shot. Any failure —
    no URL, a down service, a dim mismatch — is a NULL pair, never an
    error up the launch path.
    """
    try:
        emb = embedder
        if emb is None:
            url = os.environ.get(_EMBED_URL_ENV, "").strip()
            if not url:
                return None
            from precis.embedder import RemoteEmbedder

            emb = RemoteEmbedder(
                url,
                expected_dim=1024,
                timeout=_EMBED_TIMEOUT_S,
                max_retries=0,
            )
        vec = emb.embed_one(text[:_EMBED_CHARS])
        if not vec:
            return None
        return vec, str(emb.model)
    except Exception:
        log.debug("run_features: novelty embed failed", exc_info=True)
        return None


def record(
    store: Any,
    *,
    job_ref_id: Any,
    todo_id: int,
    todo: Any,
    task_text: str,
    brief: str,
    lane: Any,
    model: str | None,
    effort: str,
    task_class: str,
    embedder: Any | None = None,
) -> bool:
    """Write one ``slice_run_features`` row for this launch.

    Best-effort, never raises: ``False`` on a missing store/table or any
    failed read/write, the failure logged at debug. ``job_ref_id`` is
    the job's ``kind='job'`` ref (``ctx.ref_id`` at submit), reused by
    an outage relaunch — ``launch`` (this job's prior rows + 1) keeps
    each launch its own row; not an int means no ledger entry to hang
    the row on. ``task_text`` is the title + body + newest
    brief/handoff the semantic features and the embedding read;
    ``brief`` the rendered prompt, for ``prompt_chars``.
    """
    try:
        if (
            store is None
            or not isinstance(job_ref_id, int)
            or isinstance(job_ref_id, bool)
        ):
            return False
        meta = getattr(todo, "meta", None)
        meta = meta if isinstance(meta, dict) else {}
        embedded = _embed(task_text, embedder)
        vec, embedder_name, novelty = (
            (embedded[0], embedded[1], None) if embedded else (None, None, None)
        )
        with store.tx() as conn:
            hop2 = conn.execute(_TWO_HOP_SQL, (todo_id, todo_id)).fetchone()
            dep = conn.execute(_DEP_DEPTH_SQL, (todo_id, todo_id)).fetchone()
            att = conn.execute(_ATTEMPT_SQL, (todo_id,)).fetchone()
            lau = conn.execute(_LAUNCH_SQL, (job_ref_id,)).fetchone()
            if vec is not None:
                row = conn.execute(
                    _NOVELTY_SQL, (Vector(vec), embedder_name)
                ).fetchone()
                novelty = row[0] if row else None
            conn.execute(
                _INSERT_SQL,
                (
                    job_ref_id,
                    todo_id,
                    int(att[0]) if att and att[0] is not None else 1,
                    int(lau[0]) if lau and lau[0] is not None else 1,
                    task_class or None,
                    node_kind(meta) or "task",
                    getattr(lane, "name", None),
                    getattr(lane, "cost_class", None),
                    model,
                    effort or None,
                    getattr(todo, "set_by", None),
                    len(brief),
                    _handle_count(task_text),
                    int(hop2[0]) if hop2 and hop2[0] is not None else None,
                    int(dep[0]) if dep and dep[0] is not None else None,
                    _constraint_clauses(task_text),
                    _op_type(task_text),
                    float(novelty) if novelty is not None else None,
                    hashlib.sha256(task_text.encode("utf-8")).hexdigest(),
                    task_text[:_STORED_TEXT_CHARS],
                    embedder_name,
                    Vector(vec) if vec is not None else None,
                ),
            )
        return True
    except Exception:
        log.debug("run_features: record failed", exc_info=True)
        return False


__all__ = ["extract", "record"]
