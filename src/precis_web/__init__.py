"""precis_web — the cluster web surface for precis-mcp.

A FastAPI service that imports the ``precis`` package directly and renders
server-side (Jinja + HTMX + Alpine), served over the Tailscale LAN behind
**HTTP Basic auth** (``auth.py``). :func:`create_app` (``app.py``) wires
one router per page plus error handlers and a lifespan that builds the
single :class:`precis.runtime.PrecisRuntime`. Optional install extra
(``precis-mcp[web]``); the ``precis web`` CLI subcommand imports it lazily.

**Auth.** Every route and mount is gated by
``auth.py::BasicAuthMiddleware`` against the ``web_users`` table
(migration 0131, roster managed by ``precis users``). Each account is
fully authorized — there are no roles; per-route ACLs and ask-routing are
a separate deferred design. Exemptions: ``/healthz`` (supervisor probe)
and ``/podcast`` (authenticates itself, additionally accepting a per-user
``?t=`` feed token because podcast clients handle Basic inconsistently on
enclosure URLs). An empty roster fails *closed* with a 503 naming the
``precis users add`` line to run. A cross-site state-changing request is
refused 403 (``Origin``/``Referer`` must match) — Basic auth makes every
mutating route a CSRF target, with no cookie to mark ``SameSite``.
The clickjack that check cannot close is shut by
``security_headers.py`` (outermost, so it rides the 401 too): framing is
same-origin, **not** ``DENY``/``'none'`` — the UI frames its own pages
(/nanopub's ``?embed=1`` panes, the reader's PDF.js viewer).
``PRECIS_WEB_AUTH=off`` disables the gate for local development only.
``/account``
(``routes/account.py``) is the signed-in user's own page — password,
profile, and the podcast subscribe URL (shown whole, copyable: the row
holds only the token's digest, so the readable copy comes from the
vault); roster management stays in the CLI.

Nav (template ``templates/base.html.j2``; badge counts
``nav.py::nav_badges``):

* **Daily** (always visible) — Drive (``/drive``), Tags, ToDo (``/tasks``).
* **Browse ▾** — Quests + Schedules (Drive presets) and the five
  kind-specific readers Drive's generic rows can't reproduce: Clusters,
  Structures, CAD, Figures, Mermaid.
* **Attention** (right, badged) — Needs you, Gripes, Alerts.
* **Manual** (``/manual``) — the user-facing how-to. Top-level and
  unbadged: it is what you reach for when you don't know which tab you
  need, so it is never inside a dropdown.
* **Ops ▾** — System, Categorizers, Agent Logs, Console, Env, Secrets.
* 🔍 loupe — global search, submits to ``/drive``.
* **Account** (far right) — the signed-in user's ``abbrev`` as a chip,
  linking to ``/account``; a plain "Account" label when the gate is off.

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

**System (`/status?tab=health|services|models|budget|now`)** —
``routes/status.py::index`` dispatches on ``tab=``: health strip, the old
``/factory`` service tables + per-tier chain editor, the ``llm``
catalog cards + live-routing header, the budget cap/pause controls, and
**Now** — a live view (htmx-polled fragment, ``GET /status/now``) of what
each worker process is doing this instant (``precis.workers.activity`` via
``host_heartbeat.meta.activity``) alongside the ``kind='job'`` running /
queued / recent-terminal lanes and active alerts. ``/factory`` and
``/budget`` GETs redirect into their sub-tab; their POST write routes are
unchanged.

**Gripes workbench (`/gripes`)** — ``routes/gripes.py``: list grouped by
``STATUS`` (closed vocab ``open → triaged → ready_for_fix → in_review →
wontfix``), a filing form, detail + comment timeline, and ``retire``
(soft-delete, the "fix landed" resolution, distinct from ``wontfix``).
Filing appends ``— filed by <login> …`` to the text: the gripe body is
the whole record, and gripes filed from the browser come from a *human*
worth going back to, unlike the agent-filed ones.

**Manual (`/manual`)** — ``routes/manual.py``: the *user*-facing manual
(how to write a paper, publish a claim, clear a figure, watch a quest
loop), rendered from markdown chapters in ``src/precis_web/manual/``.
Deliberately inside the package, not ``docs/``: the wheel ships only
``src/`` (``docs/`` is sdist-only, so a chapter there is absent on a
deployed node), and a chapter describing a button belongs in the same
diff as the button. Filename carries order + slug
(``01-writing-a-paper.md`` → chapter 1 at ``/manual/writing-a-paper``);
title and index blurb are parsed from the first heading + paragraph, so
there is no second table of contents to drift. Distinct from
``precis/data/skills/`` (agent-facing runtime docs) and ``docs/``
(repo-dev docs).

**Categorizers console (`/categorizers`)** — ``routes/categorizers.py``:
every axis/topic with coverage + last-run (deferred htmx OOB swaps via
``GET /categorizers/progress``), live enable/disable toggles writing
``service_config``, and per-tag Drive deep-link chips.
"""

from __future__ import annotations

from precis_web.app import create_app

__all__ = ["create_app"]
