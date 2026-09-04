"""precis_web — the cluster web surface for precis-mcp.

FastAPI service, imports ``precis`` directly, server-side rendered (Jinja +
HTMX + Alpine), served over the Tailscale LAN behind **HTTP Basic auth**
(``auth.py``). :func:`create_app` (``app.py``) wires one router per page,
error handlers, and a lifespan building the single
:class:`precis.runtime.PrecisRuntime`. Optional install extra
(``precis-mcp[web]``); ``precis web`` CLI subcommand imports it lazily.

**Auth.** ``auth.py::BasicAuthMiddleware`` gates every route/mount against
``web_users`` (migration 0131, roster via ``precis users``); every account
is fully authorized — no roles, no per-route ACLs. Exemptions:
``/healthz`` (probe), ``/podcast`` (self-authenticates, plus a per-user
``?t=`` feed token — podcast clients handle Basic inconsistently on
enclosure URLs). Empty roster → 503 naming ``precis users add``.
Origin/Referer mismatch on a state-changing request → 403 (Basic's
ambient header carries no ``SameSite`` CSRF defense). A signed
``SameSite=Lax`` session cookie rides alongside Basic (minted on
Basic-authenticated responses, accepted as an alternative) because Safari
won't replay Basic credentials into iframe subnavigations. Clickjack
defense is ``security_headers.py`` (outermost, rides the 401 too):
framing is same-origin, not ``DENY``/``'none'`` (the UI frames its own
PDF.js viewer). ``PRECIS_WEB_AUTH=off`` disables the gate — local dev
only. ``/account`` (``routes/account.py``): password, profile (ORCID iD
that nanopubs signed here attribute to), sign-out, podcast subscribe URL
(shown whole; the row holds only the token digest, so the vault is the
only other source). Sign-out = 401 with a fresh challenge (evicts cached
Basic credential) + session-cookie delete.

Nav (template ``templates/base.html.j2``; badges ``nav.py::nav_badges``):
Daily (Drive, Tags, ToDo) always visible; Browse ▾ (Quests, Schedules,
Clusters, Structures, CAD, Figures, Mermaid); Attention (Needs you,
Gripes, Alerts, badged); Manual (top-level, unbadged — the no-idea-where
tab, so never in a dropdown); Ops ▾ (System, Categorizers, Agent Logs,
Console, Env, Secrets); 🔍 loupe submits to ``/drive``; "?" tour launcher
(only on a page whose path matches a tour manifest's ``route`` —
``routes/manual.py::tour_slug_for_path``, no extra client fetch); Account
(far right, signed-in user's ``abbrev`` chip) → ``/account``.

**Drive (`/drive`)** is the unified seek+manage surface:
``routes/drive.py::index`` runs cross-kind chunk search (``q=``, kind/tag
facets, ``sort=relevance|recency|oldest|untried``, ``state=stub|deleted``)
over the folder tree + CRUD. No-query landing lists unfiled refs by
``updated_at``; ``folder=*`` ("Anywhere") drops that filter for a
whole-kind pivot (Status's "Refs by kind" chips land here). An explicit
``k=`` beats the ``items_kinds`` cookie, but only a form submit
(``submitted=1``) writes it, so a deep link can't clobber the saved
facet. ``state=stub`` is the downloads queue: fetchable stubs only
(DOI/arXiv/S2 id present, ``precis/store/_stub_predicate.py::stub_predicate_sql``),
default ``sort=untried`` via ``manual:open`` ``ref_events``; opening a row
beacons ``POST /downloads/mark-tried`` to sink it. "Fetch next 25"
(``POST /drive/requeue-stubs``) stamps ``meta.oa_requeued`` for
``fetch_oa``'s next pass. ``cited_by=<draft>`` scopes to a draft's
papers-to-fetch set (``handlers/_citations_view.draft_fetch_ref_ids``).
Every bespoke list Drive replaced (``/items``, ``/papers``, ``/drafts``,
``/papers-needed``, ``/refs/{oracle,patent}``, ``/cfp``) 307-redirects to
a Drive preset; per-kind detail readers are untouched. ``/`` → ``/drive``.

**System (`/status?tab=health|services|models|budget|now`)** —
``routes/status.py::index`` dispatches on ``tab=``: health strip, the
``/factory`` service tables + per-tier chain editor, the ``llm`` catalog
cards + live-routing header, budget cap/pause controls, and **Now**
(htmx-polled ``GET /status/now``: per-worker-process activity via
``precis.workers.activity``/``host_heartbeat.meta.activity``, plus
``kind='job'`` running/queued/recent-terminal lanes and active alerts).
``/factory`` and ``/budget`` GETs redirect into their sub-tab; POST
routes unchanged.

**Gripes workbench (`/gripes`)** — ``routes/gripes.py``: list grouped by
``STATUS`` (``open → triaged → ready_for_fix → in_review → wontfix``),
filing form, detail + comment timeline, ``retire`` (soft-delete, "fix
landed", distinct from ``wontfix``). Filing appends
``— filed by <login> …``; gripe body is the whole record.

**Manual (`/manual`)** — ``routes/manual.py``: user-facing how-to,
rendered from markdown chapters in ``src/precis_web/manual/`` (in-package
not ``docs/``: the wheel ships only ``src/``, so a chapter must ship with
the button it describes). Filename = order + slug
(``01-writing-a-paper.md`` → ``/manual/writing-a-paper``); title/blurb
parsed from the first heading+paragraph — no second TOC to drift.
Distinct from ``precis/data/skills/`` (agent-facing) and ``docs/``
(repo-dev).

**Categorizers console (`/categorizers`)** — ``routes/categorizers.py``:
every axis/topic with coverage + last-run (htmx OOB swaps via
``GET /categorizers/progress``), enable/disable toggles writing
``service_config``, per-tag Drive deep-link chips.
"""

from __future__ import annotations

from precis_web.app import create_app

__all__ = ["create_app"]
