"""Categorizers tab — inventory + live enable/disable toggles.

Unifies the two closed-vocabulary tagging families across the corpus —
the axes (``src/precis/data/axes/*.yaml``) and topics
(``src/precis/data/topics/*.yaml``); :mod:`precis.categorizers` holds the
shared machinery this page reads (inventory, per-service effective
on/off state, coverage counts) so the CLI (``precis classify progress``)
and the search-side coverage gate reuse it without importing the web
package.

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
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from precis.categorizers import (
    CLASSIFY_PASS_AXES,
    allowed_services,
    effective_state,
    governing_service,
    load_axes,
    load_topics,
    progress_rows,
    topic_service,
)
from precis.workers.service_config import (
    ALL_HOSTS,
    DEFAULT_PRIO,
    clear_service_config,
    set_service_concurrency,
    set_service_prio,
)
from precis_web.deps import get_store, templates

router = APIRouter(prefix="/categorizers", tags=["categorizers"])

log = logging.getLogger(__name__)


def _drive_chip_url(tag: str) -> str:
    """Deep-link to /drive showing paper+patent refs carrying ``tag``
    (the categorizer-outcome browse chip). ``tag`` is the /drive tag-facet
    string — ``topic:<slug>`` for a topic, ``<NS>:<value>`` for a
    ref-level axis value."""
    return "/drive?" + urlencode(
        [
            ("submitted", "1"),
            ("k", "paper"),
            ("k", "patent"),
            ("tag", tag),
            ("sort", "recency"),
        ]
    )


def _axis_prompt_preview(axis_id: str) -> dict[str, str] | None:
    """The axis's actual (system, user) LLM prompt, imported lazily from
    ``workers/axis_pass.py`` (kept import-time-decoupled since another agent
    may be actively editing that module — mirrors
    :func:`precis.categorizers.current_marker_value`).
    ``None`` on import/build failure — the caller degrades the popover
    rather than 500ing the page."""
    try:
        from precis.workers.axis_pass import prompt_preview

        return prompt_preview(axis_id)
    except Exception:
        log.exception("categorizers: axis prompt_preview failed for %r", axis_id)
        return None


def _topics_prompt_preview(enabled_slugs: list[str] | None) -> dict[str, str] | None:
    """The topics pass's actual (system, user) LLM prompt over the enabled
    subset, imported lazily from ``workers/classify_topics.py`` (mirrors
    :func:`precis.categorizers.current_marker_value`).
    ``None`` on import/build failure."""
    try:
        from precis.workers.classify_topics import prompt_preview

        return prompt_preview(enabled_slugs)
    except Exception:
        log.exception("categorizers: topics prompt_preview failed")
        return None


def _axis_row(
    axis: dict[str, Any], effective: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    axis_id = str(axis["id"])
    level = axis.get("level") or "ref"
    service = governing_service(axis_id)
    state = effective.get(service) or {
        "enabled": False,
        "overridden": False,
        "concurrency": 1,
    }
    shared_note = (
        "Shares the `classify` chunk cascade with role3/junk — toggling this "
        "flips the whole cascade."
        if axis_id in CLASSIFY_PASS_AXES
        else None
    )
    namespace = axis_id.upper()
    if level == "chunk":
        chips: list[dict[str, str]] = []
    else:
        chips = [
            {
                "label": str(v),
                "tag": f"{namespace}:{v}",
                "url": _drive_chip_url(f"{namespace}:{v}"),
            }
            for v in (axis.get("values") or [])
        ]
    return {
        "kind": "axis",
        "name": axis_id,
        "question": axis.get("question") or "",
        "granularity": "chunk" if level == "chunk" else "paper+patent",
        "level": level,
        "namespace": namespace,
        "prereq": list(axis.get("prereq") or []),
        "service": service,
        "status": "active" if state["enabled"] else "off",
        "active": bool(state["enabled"]),
        "overridden": bool(state["overridden"]),
        "concurrency": int(state.get("concurrency") or 1),
        "shared_note": shared_note,
        "prompt_preview": _axis_prompt_preview(axis_id),
        "chips": chips,
    }


def _topic_row(
    topic: dict[str, Any],
    effective: dict[str, dict[str, Any]],
    prompt_preview: dict[str, str] | None,
) -> dict[str, Any]:
    service = topic_service(str(topic["slug"]))
    state = effective.get(service) or {
        "enabled": False,
        "overridden": False,
        "concurrency": 1,
    }
    slug = str(topic["slug"])
    return {
        "kind": "topic",
        "name": slug,
        "question": topic.get("description") or "",
        "granularity": "paper+patent",
        "level": "ref",
        "namespace": None,
        "prereq": [],
        "service": service,
        "status": "active" if state["enabled"] else "off",
        "active": bool(state["enabled"]),
        "overridden": bool(state["overridden"]),
        "concurrency": int(state.get("concurrency") or 1),
        "shared_note": None,
        "prompt_preview": prompt_preview,
        "chips": [
            {
                "label": slug,
                "tag": f"topic:{slug}",
                "url": _drive_chip_url(f"topic:{slug}"),
            }
        ],
    }


def _safe(fn: Any) -> Any:
    """Run a query closure, returning its result or ``None`` on error.

    Mirrors ``status.py``'s ``_safe`` (and ``precis.categorizers``'s) —
    a schema surprise in one categorizer's count degrades that row
    instead of 500ing the whole fragment.
    """
    try:
        return fn()
    except Exception:
        log.exception("categorizers: coverage query failed")
        return None


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """The categorizer shell: YAML-derived rows + their live toggle state.

    The toggle read is one small ``service_config`` scan (not a corpus
    aggregate), so the page still paints fast — the *coverage* counts are
    what's deferred to the ``/categorizers/progress`` fragment.
    """
    store = get_store(request)
    effective = _safe(lambda: effective_state(store)) or {}
    axes = [_axis_row(a, effective) for a in load_axes()]
    topics_yaml = load_topics()
    # Same enabled-subset derivation as `progress_rows` — the preview must
    # reflect what the pass would actually send today, not the full
    # taxonomy, so a per-topic toggle changes what the popover shows.
    enabled_slugs = [
        str(t["slug"])
        for t in topics_yaml
        if isinstance(t, dict)
        and t.get("slug")
        and (effective.get(topic_service(str(t["slug"]))) or {}).get("enabled")
    ]
    # None -> full taxonomy, so an all-off page still shows a representative
    # prompt rather than an empty one.
    topics_preview = _topics_prompt_preview(enabled_slugs or None)
    topics = [_topic_row(t, effective, topics_preview) for t in topics_yaml]
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
        "kill_switch": {
            "service": "classify_topics",
            "enabled": bool(eff_ct["enabled"]),
            "overridden": bool(eff_ct["overridden"]),
        },
    }
    return templates.TemplateResponse(request, "categorizers.html.j2", ctx)


@router.get("/progress", response_class=HTMLResponse)
async def progress_fragment(request: Request) -> HTMLResponse:
    """Lazy-loaded coverage panel (htmx fragment) — the full-table
    aggregate scans (one per chunk-level axis, plus the paper+patent
    corpus totals), mirroring ``status.py``'s ``/status/backlog``."""
    store = get_store(request)
    progress = _safe(lambda: progress_rows(store)) or {}
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
    must be one of :func:`precis.categorizers.allowed_services`
    (``classify`` + the ``classify_topics`` kill-switch + one ``axis:<id>``
    per non-cascade axis + one ``topic:<slug>`` per topic) — an unknown
    value is rejected rather than writing an arbitrary
    ``service_config`` row.
    """
    if service not in allowed_services():
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


@router.post("/concurrency", response_model=None)
async def set_concurrency(
    request: Request,
    service: str = Form(...),
    concurrency: str | None = Form(default=None),
) -> RedirectResponse:
    """Live-write a categorizer's in-pass LLM-call concurrency.

    Mirrors :func:`toggle` — same allowlist guard, same all-hosts (``*``)
    scope, same "pick it up within one cache TTL" story. Every worker-side
    consumer (today, only ``classify`` — ``cli/worker.py``) clamps this at
    its own hard env ceiling (``PRECIS_CLASSIFY_MAX_CONCURRENCY``) before
    use, so a fat-fingered value here can't stampede the cloud endpoint;
    this route only rejects a non-positive / unparseable value outright.
    An empty *or absent* ``concurrency`` field reverts to the default (1,
    serial) — ``default=None`` (not ``Form(...)``) because some HTTP
    clients drop a genuinely-empty form field instead of sending it blank.
    """
    if service not in allowed_services():
        log.warning(
            "categorizers: rejected concurrency write for unknown service %r", service
        )
        return RedirectResponse(url="/categorizers", status_code=303)

    raw = (concurrency or "").strip()
    value: int | None
    if not raw:
        value = None
    else:
        try:
            value = int(raw)
        except ValueError:
            log.warning("categorizers: rejected non-integer concurrency %r", raw)
            return RedirectResponse(url="/categorizers", status_code=303)
        if value < 1:
            log.warning("categorizers: rejected non-positive concurrency %r", value)
            return RedirectResponse(url="/categorizers", status_code=303)

    store = get_store(request)
    try:
        set_service_concurrency(store, ALL_HOSTS, service, value, actor="web")
    except Exception:
        log.warning(
            "categorizers: concurrency write failed for service=%r value=%r",
            service,
            value,
            exc_info=True,
        )
    return RedirectResponse(url="/categorizers", status_code=303)
