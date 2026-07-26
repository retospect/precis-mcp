"""Categorizers tab — inventory + live enable/disable toggles.

Unifies the two closed-vocabulary tagging families across the corpus:

  * **Axes** (``src/precis/data/axes/*.yaml``) — one closed-vocabulary
    categorizer per file (schema: see ``axes/README.md``). A chunk-level
    axis (``level: chunk``) writes ``<ID>:<value>`` chunk tags; a
    ref-level axis (``level: ref`` — the default when the field is
    omitted, mirroring ``role.yaml``'s "ref-level runner ignores this
    file" convention) writes a ref tag in the same ``<ID>``-uppercased
    namespace. **role3** + **junk** run under the ``classify`` cascade
    (ADR 0047); every other axis runs under its own ``axis:<id>``
    generic-runner service (``cli/worker.py``'s per-axis wiring,
    ``workers/axis_pass.py``) — default-OFF but independently flippable.
  * **Topics** (``src/precis/data/topics/*.yaml``) — paper/patent-level,
    multi-label ``topic:<slug>`` open tags plus a
    ``TOPICCASCADE:<marker>`` marker. All topics share the one
    ``classify_topics`` pass (ADR 0060), but each topic is independently
    flippable via its own ``topic:<slug>`` service (ADR 0068): the pass
    filters ``_load_topics()`` to the enabled subset and the marker encodes
    that enabled-topic set, so toggling a topic lazily re-sweeps the corpus
    against the new set. ``classify_topics`` itself remains a global
    kill-switch — an explicit off row there force-kills the pass regardless
    of any topic's own state.

The Active column + toggle read/write the live ``service_config``
DB-override resolver ``cli/worker.py`` uses (``ServiceConfigResolver`` /
``set_service_prio`` / ``clear_service_config``), scoped to the
all-hosts (``*``) row — a flip here is picked up by every worker node
within one cache TTL, no redeploy. role3+junk still share one governing
``classify`` service and carry a note that the toggle is shared; topics no
longer do (each is its own row/service now).

Mirrors ``status.py``'s backlog-fragment pattern: the shell (name /
question / granularity / active / prereqs) is cheap (one small
``service_config`` scan, not a corpus aggregate) and renders fast; the
coverage counts are full-table aggregate scans (~1.3M chunks for the
chunk-level axes) so they're deferred to the ``GET
/categorizers/progress`` htmx fragment, loaded on page load.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from precis.utils.env import env_csv_set, env_flag
from precis.workers.service_config import (
    ALL_HOSTS,
    DEFAULT_PRIO,
    ServiceConfigResolver,
    clear_service_config,
    list_service_config,
    set_service_prio,
)
from precis_web.deps import get_store, templates

router = APIRouter(prefix="/categorizers", tags=["categorizers"])

log = logging.getLogger(__name__)

_AXES_DIR = Path(__file__).resolve().parent.parent.parent / "precis" / "data" / "axes"
_TOPICS_DIR = (
    Path(__file__).resolve().parent.parent.parent / "precis" / "data" / "topics"
)

#: Env vars seeding the two shared passes' default_on verdict when no
#: ``service_config`` row overrides them (mirrors
#: ``precis.workers.registry`` ``ServiceSpec.enable_env`` for
#: ``classify`` / ``classify_topics`` — read directly rather than
#: importing the registry so this route stays decoupled from the
#: worker-CLI wiring).
_CLASSIFY_ENABLED_ENV = "PRECIS_CLASSIFY_ENABLED"
_CLASSIFY_TOPICS_ENABLED_ENV = "PRECIS_CLASSIFY_TOPICS_ENABLED"

#: Comma-separated axis ids ``cli/worker.py``'s per-axis wiring seeds its
#: ``default_on`` verdict from (a live ``service_config`` row always wins).
_AXES_ENABLED_ENV = "PRECIS_AXES_ENABLED"

#: Comma-separated topic slugs ``cli/worker.py``'s per-topic gate (ADR 0068)
#: seeds its ``default_on`` verdict from — mirrors ``_AXES_ENABLED_ENV``.
_TOPICS_ENABLED_ENV = "PRECIS_TOPICS_ENABLED"

#: Axis ids the ``classify`` cascade actually drives. ``junk`` is a
#: gate folded into ``ROLE3:furniture`` — it never writes its own
#: ``JUNK:`` tag (see ``workers/classify.py``) — so its own coverage
#: count is always 0 even while the pass is active; that 0% is correct,
#: not a bug.
_CLASSIFY_PASS_AXES = frozenset({"role3", "junk"})

_TOPIC_MARKER_NAMESPACE = "TOPICCASCADE"


def _topic_service(slug: str) -> str:
    """The per-topic ``service_config`` service name (ADR 0068) — each topic
    is independently flippable; ``classify_topics`` itself remains the
    retained global kill-switch (see :func:`_allowed_services`)."""
    return f"topic:{slug}"


def _current_marker_value(enabled_slugs: list[str]) -> str | None:
    """The live done-marker value for ``enabled_slugs``, imported lazily from
    ``workers/classify_topics.py`` (kept import-time-decoupled since another
    agent may be actively editing that module). ``None`` on import failure —
    the caller degrades the topic coverage rows rather than 500ing."""
    try:
        from precis.workers.classify_topics import topic_marker_value

        return topic_marker_value(enabled_slugs)
    except Exception:
        log.exception("categorizers: topic_marker_value import failed")
        return None


def _load_axes() -> list[dict[str, Any]]:
    """Every axis YAML that defines an ``id``.

    Skips ``journal_domains.yaml`` (a journal→domain lookup table, not
    itself a categorizer — it has no ``id`` field) and ``README.md``
    (non-YAML, excluded by the glob already).
    """
    axes: list[dict[str, Any]] = []
    for path in sorted(_AXES_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text())
        except Exception:
            log.exception("categorizers: failed to parse axis file %s", path)
            continue
        if isinstance(data, dict) and data.get("id"):
            axes.append(data)
    return axes


def _load_topics() -> list[dict[str, Any]]:
    topics: list[dict[str, Any]] = []
    for path in sorted(_TOPICS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text())
        except Exception:
            log.exception("categorizers: failed to parse topic file %s", path)
            continue
        if isinstance(data, dict) and data.get("slug"):
            topics.append(data)
    return topics


def _governing_service(axis_id: str) -> str:
    """The ``service_config`` service that gates ``axis_id``'s runtime pass.

    ``role3``/``junk`` share the one ``classify`` cascade pass; every
    other axis runs under its own ``axis:<id>`` generic-runner service
    (``cli/worker.py``'s per-axis wiring, ``workers/axis_pass.py``).
    """
    return "classify" if axis_id in _CLASSIFY_PASS_AXES else f"axis:{axis_id}"


def _allowed_services() -> frozenset[str]:
    """Every ``service_config`` service name this page may toggle.

    Read live off the axis/topic YAMLs (not hardcoded) so a new axis or
    topic file is toggleable without a code change, and re-derived per
    request so a concurrently-added file doesn't need a worker restart to
    become toggleable. Guards ``POST /categorizers/toggle`` against writing
    an arbitrary ``service_config`` row. ``classify_topics`` is retained as
    the global kill-switch target (ADR 0068) alongside each topic's own
    ``topic:<slug>`` service.
    """
    axis_ids = {str(a["id"]) for a in _load_axes()}
    topic_slugs = {
        str(t["slug"]) for t in _load_topics() if isinstance(t, dict) and t.get("slug")
    }
    return frozenset(
        {"classify", "classify_topics"}
        | {_governing_service(a) for a in axis_ids if a not in _CLASSIFY_PASS_AXES}
        | {_topic_service(s) for s in topic_slugs}
    )


def _override_rows(store: Any) -> frozenset[str]:
    """Services carrying an explicit all-hosts (``*``) ``service_config``
    row — the "overridden" (vs "env/profile default") signal the UI
    shows alongside the effective on/off state. Degrades to "no
    overrides known" on a schema surprise (pre-migration DB) rather than
    500ing the page."""
    try:
        rows = list_service_config(store)
    except Exception:
        log.exception("categorizers: list_service_config failed")
        return frozenset()
    return frozenset(str(r["service"]) for r in rows if r["host"] == ALL_HOSTS)


def _effective_state(store: Any) -> dict[str, dict[str, Any]]:
    """``service -> {"enabled": bool, "overridden": bool}`` for every
    service this page governs, scoped to the all-hosts (``*``) row — the
    live ``service_config`` override wins over the env/profile default,
    mirroring ``cli/worker.py``'s ``_pass_enabled`` contract."""
    resolver = ServiceConfigResolver(store, ALL_HOSTS)
    overridden = _override_rows(store)
    axes_env = env_csv_set(_AXES_ENABLED_ENV)
    # Mirrors cli/worker.py's `_classify_topics_enabled_slugs` /
    # `_gate_default_on`: PRECIS_CLASSIFY_TOPICS_ENABLED=1 is the legacy
    # "all topics default-on" admin seed, so a per-topic row's own
    # ``default_on`` must fold it in too — otherwise the UI would show a
    # topic off that the worker actually classifies.
    global_topics_on = env_flag(_CLASSIFY_TOPICS_ENABLED_ENV)

    out: dict[str, dict[str, Any]] = {
        "classify": {
            "enabled": resolver.enabled(
                "classify", default_on=env_flag(_CLASSIFY_ENABLED_ENV)
            ),
            "overridden": "classify" in overridden,
        },
        # Retained as the global kill-switch state (ADR 0068) — each topic
        # now also carries its own independent ``topic:<slug>`` state below.
        "classify_topics": {
            "enabled": resolver.enabled("classify_topics", default_on=global_topics_on),
            "overridden": "classify_topics" in overridden,
        },
    }
    for axis in _load_axes():
        axis_id = str(axis["id"])
        if axis_id in _CLASSIFY_PASS_AXES:
            continue
        service = _governing_service(axis_id)
        out[service] = {
            "enabled": resolver.enabled(service, default_on=axis_id in axes_env),
            "overridden": service in overridden,
        }

    topics_env = env_csv_set(_TOPICS_ENABLED_ENV)
    for topic in _load_topics():
        if not (isinstance(topic, dict) and topic.get("slug")):
            continue
        slug = str(topic["slug"])
        service = _topic_service(slug)
        out[service] = {
            "enabled": resolver.enabled(
                service, default_on=global_topics_on or slug in topics_env
            ),
            "overridden": service in overridden,
        }
    return out


def _axis_row(
    axis: dict[str, Any], effective: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    axis_id = str(axis["id"])
    level = axis.get("level") or "ref"
    service = _governing_service(axis_id)
    state = effective.get(service) or {"enabled": False, "overridden": False}
    shared_note = (
        "Shares the `classify` chunk cascade with role3/junk — toggling this "
        "flips the whole cascade."
        if axis_id in _CLASSIFY_PASS_AXES
        else None
    )
    return {
        "kind": "axis",
        "name": axis_id,
        "question": axis.get("question") or "",
        "granularity": "chunk" if level == "chunk" else "paper+patent",
        "level": level,
        "namespace": axis_id.upper(),
        "prereq": list(axis.get("prereq") or []),
        "service": service,
        "status": "active" if state["enabled"] else "off",
        "active": bool(state["enabled"]),
        "overridden": bool(state["overridden"]),
        "shared_note": shared_note,
    }


def _topic_row(
    topic: dict[str, Any], effective: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    service = _topic_service(str(topic["slug"]))
    state = effective.get(service) or {"enabled": False, "overridden": False}
    return {
        "kind": "topic",
        "name": str(topic["slug"]),
        "question": topic.get("description") or "",
        "granularity": "paper+patent",
        "level": "ref",
        "namespace": None,
        "prereq": [],
        "service": service,
        "status": "active" if state["enabled"] else "off",
        "active": bool(state["enabled"]),
        "overridden": bool(state["overridden"]),
        "shared_note": None,
    }


def _safe(fn: Any) -> Any:
    """Run a query closure, returning its result or ``None`` on error.

    Mirrors ``status.py``'s ``_safe`` — a schema surprise in one
    categorizer's count degrades that row instead of 500ing the whole
    fragment.
    """
    try:
        return fn()
    except Exception:
        log.exception("categorizers: coverage query failed")
        return None


def _chunk_eligible_total(store: Any) -> int:
    """Count of eligible body chunks — the shared ``total`` denominator for
    every chunk-level axis, mirroring the ``classify`` cascade's claim
    predicate (``workers/classify.py``'s ``_claim``): real body paragraphs
    long enough to be worth tagging. Computed once per fragment load (this
    scan is the same for all chunk axes), not per axis."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*)::int FROM chunks "
            "WHERE ord >= 0 AND chunk_kind = 'paragraph' AND length(text) > 120"
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _chunk_axis_progress(
    store: Any, namespace: str, chunk_eligible_total: int
) -> tuple[int, int]:
    """(done, total) for a chunk-level axis's own namespace. ``done`` =
    chunks already carrying a tag in this namespace; ``total`` is the
    shared eligible-chunk count passed in (see :func:`_chunk_eligible_total`)
    so the expensive scan runs once, not once per axis."""
    with store.pool.connection() as conn:
        done_row = conn.execute(
            "SELECT count(DISTINCT ct.chunk_id)::int FROM chunk_tags ct "
            "JOIN tags t ON t.tag_id = ct.tag_id WHERE t.namespace = %s",
            (namespace,),
        ).fetchone()
    done = int(done_row[0]) if done_row and done_row[0] is not None else 0
    return done, chunk_eligible_total


def _ref_axis_progress(
    store: Any, namespace: str, paper_patent_total: int
) -> tuple[int, int]:
    """(done, total) for a ref-level axis: refs tagged in its namespace,
    over the paper+patent corpus. Each non-cascade axis now has a runtime
    pass (``axis:<id>``, default-OFF) — ``done`` reads 0 until its toggle
    is flipped on and it's had a chance to sweep the corpus, not because
    no code exists."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(DISTINCT rt.ref_id)::int FROM ref_tags rt "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "JOIN refs r ON r.ref_id = rt.ref_id "
            "WHERE r.kind = ANY(%(kinds)s) AND r.deleted_at IS NULL "
            "AND t.namespace = %(ns)s",
            {"kinds": ["paper", "patent"], "ns": namespace},
        ).fetchone()
    done = int(row[0]) if row and row[0] is not None else 0
    return done, paper_patent_total


def _paper_patent_total(store: Any) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*)::int FROM refs "
            "WHERE kind = ANY(%s) AND deleted_at IS NULL",
            (["paper", "patent"],),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _topics_marker_done(store: Any, marker_value: str) -> int:
    """Paper+patent refs carrying the current ``TOPICCASCADE:<marker>``
    marker — the ``classify_topics`` pass's coverage of the corpus under the
    *live enabled-topic set* (ADR 0068 — a toggle changes the marker value,
    so this recomputes against the new set; same value for every topic row;
    each topic's own hit-count is a separate, per-slug number — see
    :func:`_topic_hit_count`)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(DISTINCT rt.ref_id)::int FROM ref_tags rt "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "JOIN refs r ON r.ref_id = rt.ref_id "
            "WHERE r.kind = ANY(%(kinds)s) AND r.deleted_at IS NULL "
            "AND t.namespace = %(ns)s AND t.value = %(marker)s",
            {
                "kinds": ["paper", "patent"],
                "ns": _TOPIC_MARKER_NAMESPACE,
                "marker": marker_value,
            },
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _topic_hit_count(store: Any, slug: str) -> int:
    """How many paper+patent refs carry ``topic:<slug>`` (an OPEN tag —
    ``workers/classify_topics.py`` writes ``Tag.open(f"topic:{slug}")``,
    so the DB namespace is the ``OPEN`` sentinel, not the slug itself)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(DISTINCT rt.ref_id)::int FROM ref_tags rt "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "JOIN refs r ON r.ref_id = rt.ref_id "
            "WHERE r.kind = ANY(%(kinds)s) AND r.deleted_at IS NULL "
            "AND t.namespace = 'OPEN' AND t.value = %(val)s",
            {"kinds": ["paper", "patent"], "val": f"topic:{slug}"},
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _progress_rows(store: Any) -> dict[str, dict[str, Any]]:
    """One coverage row per categorizer, keyed by name. Each is computed
    independently via :func:`_safe` so one schema surprise degrades only
    that row (``error: True``, rendered as a dash) instead of the whole
    fragment."""
    rows: dict[str, dict[str, Any]] = {}
    paper_patent_total = _safe(lambda: _paper_patent_total(store)) or 0
    chunk_total = _safe(lambda: _chunk_eligible_total(store)) or 0

    for axis in _load_axes():
        axis_id = str(axis["id"])
        namespace = axis_id.upper()
        level = axis.get("level") or "ref"
        if level == "chunk":
            result = _safe(
                lambda ns=namespace: _chunk_axis_progress(store, ns, chunk_total)
            )
        else:
            result = _safe(
                lambda ns=namespace: _ref_axis_progress(store, ns, paper_patent_total)
            )
        if result is None:
            rows[axis_id] = {"done": 0, "total": 0, "error": True}
        else:
            done, total = result
            rows[axis_id] = {"done": done, "total": total, "error": False}

    topics = _load_topics()
    if topics:
        effective = _safe(lambda: _effective_state(store)) or {}
        enabled_slugs = [
            str(t["slug"])
            for t in topics
            if isinstance(t, dict)
            and t.get("slug")
            and (effective.get(_topic_service(str(t["slug"]))) or {}).get("enabled")
        ]
        marker_value = _current_marker_value(enabled_slugs)
        marker_done = (
            _safe(lambda mv=marker_value: _topics_marker_done(store, mv))
            if marker_value
            else None
        )
        for topic in topics:
            slug = str(topic["slug"])
            hit_count = _safe(lambda s=slug: _topic_hit_count(store, s))
            if marker_done is None or hit_count is None:
                rows[slug] = {
                    "done": 0,
                    "total": 0,
                    "hit_count": 0,
                    "error": True,
                }
            else:
                rows[slug] = {
                    "done": marker_done,
                    "total": paper_patent_total,
                    "hit_count": hit_count,
                    "error": False,
                }
    return rows


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """The categorizer shell: YAML-derived rows + their live toggle state.

    The toggle read is one small ``service_config`` scan (not a corpus
    aggregate), so the page still paints fast — the *coverage* counts are
    what's deferred to the ``/categorizers/progress`` fragment.
    """
    store = get_store(request)
    effective = _safe(lambda: _effective_state(store)) or {}
    axes = [_axis_row(a, effective) for a in _load_axes()]
    topics = [_topic_row(t, effective) for t in _load_topics()]
    # Kill-switch honesty: an explicit prio-0 `classify_topics` row force-
    # disables the pass regardless of any topic's own toggle — surface that
    # so the UI doesn't show individual topics as "On" while nothing runs.
    eff_ct = effective.get("classify_topics") or {"enabled": True, "overridden": False}
    topics_globally_off = (not eff_ct["enabled"]) and eff_ct["overridden"]
    ctx = {
        "active_tab": "categorizers",
        "axes": axes,
        "topics": topics,
        "topics_globally_off": topics_globally_off,
    }
    return templates.TemplateResponse(request, "categorizers.html.j2", ctx)


@router.get("/progress", response_class=HTMLResponse)
async def progress_fragment(request: Request) -> HTMLResponse:
    """Lazy-loaded coverage panel (htmx fragment) — the full-table
    aggregate scans (one per chunk-level axis, plus the paper+patent
    corpus totals), mirroring ``status.py``'s ``/status/backlog``."""
    store = get_store(request)
    progress = _safe(lambda: _progress_rows(store)) or {}
    return templates.TemplateResponse(
        request,
        "_categorizers_progress.html.j2",
        {"progress": progress},
    )


@router.post("/toggle", response_model=None)
async def toggle(
    request: Request,
    service: str = Form(...),
    action: str = Form(...),
) -> RedirectResponse:
    """Live-flip a categorizer's governing ``service_config`` row.

    ``on`` -> ``prio=DEFAULT_PRIO`` (runs), ``off`` -> ``prio=0`` (forced
    off), ``default`` -> delete the row (revert to the env/profile
    default). Always scoped to the all-hosts (``*``) row — every worker
    node picks the flip up within one cache TTL, no redeploy. ``service``
    must be one of :func:`_allowed_services` (``classify`` + the
    ``classify_topics`` kill-switch + one ``axis:<id>`` per non-cascade axis
    + one ``topic:<slug>`` per topic) — an unknown value is rejected rather
    than writing an arbitrary ``service_config`` row.
    """
    if service not in _allowed_services():
        log.warning("categorizers: rejected toggle for unknown service %r", service)
        return RedirectResponse(url="/categorizers", status_code=303)

    store = get_store(request)
    try:
        if action == "on":
            set_service_prio(store, ALL_HOSTS, service, DEFAULT_PRIO, actor="web")
        elif action == "off":
            set_service_prio(store, ALL_HOSTS, service, 0, actor="web")
        elif action == "default":
            clear_service_config(store, ALL_HOSTS, service)
        else:
            log.warning("categorizers: unknown toggle action %r", action)
    except Exception:
        log.warning(
            "categorizers: toggle failed for service=%r action=%r",
            service,
            action,
            exc_info=True,
        )
    return RedirectResponse(url="/categorizers", status_code=303)
