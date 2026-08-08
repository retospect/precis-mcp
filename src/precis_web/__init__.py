"""precis_web — the cluster web surface for precis-mcp.

A FastAPI service that imports the ``precis`` package directly and renders
server-side (Jinja + HTMX + Alpine), served over the Tailscale LAN (no
auth). :func:`create_app` (``app.py``) wires one router per page plus error
handlers and a lifespan that builds the single
:class:`precis.runtime.PrecisRuntime`. Optional install extra
(``precis-mcp[web]``); the ``precis web`` CLI subcommand imports it lazily.

Nav (template ``templates/base.html.j2``; badge counts
``nav.py::nav_badges``):

* **Daily** (always visible) — Drive (``/drive``), Tags, ToDo (``/tasks``).
* **Browse ▾** — Quests + Schedules (Drive presets) and the five
  kind-specific readers Drive's generic rows can't reproduce: Clusters,
  Structures, CAD, Figures, Mermaid.
* **Attention** (right, badged) — Needs you, Gripes, Alerts.
* **Ops ▾** — System, Categorizers, Agent Logs, Console, Env, Secrets.
* 🔍 loupe — global search, submits to ``/drive``.

**Drive (`/drive`)** is the unified seek+manage surface:
``routes/drive.py::index`` runs cross-kind chunk search (``q=``, kind/tag
facets, ``sort=relevance|recency|oldest|untried``, ``state=stub|deleted``)
grafted onto the folder tree + CRUD. The no-query landing lists unfiled
refs by ``updated_at``. ``state=stub`` is the downloads queue: fetchable
stubs only (DOI/arXiv/S2 id present, shared predicate
``precis/store/_stub_predicate.py::stub_predicate_sql``), default
``sort=untried`` via ``manual:open`` ``ref_events``; opening a row beacons
``POST /downloads/mark-tried`` so opened stubs sink and re-load serves the
next batch. "Fetch next 25" (``POST /drive/requeue-stubs``) stamps
``meta.oa_requeued`` for ``fetch_oa``'s next pass. ``cited_by=<draft>``
scopes the queue to a draft's papers-to-fetch set
(``handlers/_citations_view.draft_fetch_ref_ids``). Every bespoke list
Drive replaced (``/items``, ``/papers``, ``/drafts``, ``/papers-needed``,
``/refs/{oracle,patent}``, ``/cfp``) 307-redirects to a Drive preset;
per-kind *detail* readers are untouched. ``/`` redirects to ``/drive``.

**System (`/status?tab=health|services|models|budget`)** —
``routes/status.py::index`` dispatches on ``tab=``: health strip, the old
``/factory`` service tables + per-tier chain editor (ADR 0066), the ``llm``
catalog cards + live-routing header, and the budget cap/pause controls.
``/factory`` and ``/budget`` GETs redirect into their sub-tab; their POST
write routes are unchanged.

**Gripes workbench (`/gripes`)** — ``routes/gripes.py``: list grouped by
``STATUS`` (closed vocab ``open → triaged → ready_for_fix → in_review →
wontfix``), detail + comment timeline, and ``retire`` (soft-delete, the
"fix landed" resolution, distinct from ``wontfix``).

**Categorizers console (`/categorizers`)** — ``routes/categorizers.py``:
every axis/topic with coverage + last-run (deferred htmx OOB swaps via
``GET /categorizers/progress``), live enable/disable toggles writing
``service_config``, and per-tag Drive deep-link chips.
"""

from __future__ import annotations

from precis_web.app import create_app

__all__ = ["create_app"]
