"""precis_web — the cluster web surface for precis-mcp.

FastAPI service, imports ``precis`` directly, server-side rendered (Jinja +
HTMX + Alpine), served over the Tailscale LAN behind **HTTP Basic auth**
(``auth.py``). :func:`create_app` (``app.py``) wires one router per page,
error handlers, and a lifespan building the single
:class:`precis.runtime.PrecisRuntime`. Optional install extra
(``precis-mcp[web]``); ``precis web`` CLI subcommand imports it lazily.

**Reaction ledger (`/rxn`).** Handler-computed ledgers and kept rxn records;
readers recompute (storage/scientific rationale: `routes/rxn.py`).

**Ref readers.** ``ref_urls`` is the shared native-reader map for Drive,
handle resolution and tag pivots. Browse-menu membership cannot gate stored
ref detail: optional kinds still render when their handler is absent. ORCID
reads its stored identity/authored papers without upstream refresh. File kinds
read stored chunks; calling filesystem ``get`` could retire a ref merely
because this web process lacks its originating corpus mount.

**Claim approval.** The open-disputes panel reads stored support verdicts
and reasoning alongside pinned passages, including typed paper, patent
and finding handles from ``conflict_search``. Pins must name the counterpart
ref's chunk to avoid attributing an unrelated passage. These are advisory
records, not adjudications; rendering never refreshes conflict search or
changes the approval gate.

Gripe summary counts link to the existing list route with exact workflow
status filters; the live total retains the nonterminal cohort. Filtering
happens in SQL so a clicked count cannot silently show the whole queue. Empty
exact-status lists name that selected status rather than making a claim about
the whole live queue.

**Pathway diagram.** ``refs._pathway_graph_payload`` preserves catpath's
``link_type`` for folding/annotations: adsorption and desorption keep their
columns; only supply→reaction folds. Raw ``kind=supply`` still excludes
reservoir links from kinetic fork probabilities; changing it would imply
a measured barrier. Legacy graphs fall back to ``kind``.

**Smartdraft review.** Shared block widget shows read-only F/C/S/A flags
(✓ current / ~ stale / – unreviewed); structure/adversarial stay on prose,
headings show S/A. One matrix for full-page and lazy blocks avoids ledger
queries and keeps flags aligned with the tooltip.

**Auth.** ``auth.py::BasicAuthMiddleware`` gates every route/mount against
``web_users`` (migration 0131; roster via ``precis users``); accounts have
no roles or per-route ACLs. ``/healthz`` is the probe exemption. ``/podcast``
self-authenticates and accepts a per-user ``?t=`` token because podcast
clients handle Basic inconsistently on enclosure URLs. Empty roster → 503
with ``precis users add``. State-changing Origin/Referer mismatch → 403:
Basic has no ``SameSite`` CSRF defense. A signed ``SameSite=Lax`` cookie is
also accepted because Safari won't replay Basic into iframe subnavigations.
Presented 401s log login + peer; ``FailureTracker`` locks a login or address
after 10 failures in 15 min (429 + ``Retry-After``, before scrypt), while
success clears only the login window. Outermost ``security_headers.py``
allows same-origin framing because the UI embeds its own PDF.js viewer;
``PRECIS_WEB_AUTH=off`` is local-dev only. ``/account`` handles password,
profile (ORCID attributed by local nanopubs), sign-out and podcast URL. The
row stores only the podcast token digest, so the vault is the other source.
Sign-out sends a fresh 401 challenge (evicting cached Basic credentials) and
deletes the session cookie.

**Secrets.** Elsevier checks fixed OA Article Retrieval metadata;
ScienceDirect Search has a separate entitlement and can falsely reject valid
retrieval keys. CORE preserves HTTP 429. Secret controls are write-only;
multiline input uses a textarea because password fields drop pasted newlines
and cannot be selected while line breaks remain. Blank replacement preserves
the stored value; rendering reads masked inventory only. Character/line
counts describe current input (Unicode code points and newline-delimited
segments), not stored length or validity. ``vault._hint`` stamps counts on
write; legacy hints remain uncounted until replacement. Rendering never
decrypts, backfills or prepopulates stored values.

Nav (``templates/base.html.j2``; badges ``nav.py::nav_badges``): Daily
(Drive, Tags, ToDo, Design); Browse ▾ (Quests, Schedules, Clusters,
Structures, CAD, Figures, Mermaid); Attention (Needs you, Gripes, Alerts,
badged); Manual (top-level and unbadged because it is the no-idea-where tab);
Ops ▾ (System, Categorizers, Agent Logs, Console, Env, Secrets); search submits
to ``/drive``; "?" launches a tour only when the path matches a manifest
``route`` (``routes/manual.py::tour_slug_for_path``, no extra fetch); Account
(signed-in user's ``abbrev``) → ``/account``.

**Design (`/design`)** — ``routes/design.py``: the hierarchical
molecule-design workbench's landing tree (docs/backlog/
the design-workbench build, slice 1), one tree per live ``se`` design (block
graph, array nodes collapsed to ``name ×N``, L0–L3 presence badges
derived from the loaded columns — never geometry) down to the
``structure`` each atomic-mode leaf is bound to
(``se_blocks.bound_kind='structure'``/``bound_design``, not a link), plus
a flat "Loose structures" section for every live ``structure`` no block
binds. Pure read, ≤ 3 SELECTs per render regardless of design count; a
node click lands on the existing ``/se/{slug}``/``/structure/{slug}``
page. Later slices (revision scrubber, chat, realize-in-the-loop) build
on this same tree.

SE inspection keeps 3D primary, with ``/se/{slug}/2d`` labeled Envelope
projections because its outlines are envelope convex hulls, not realized
solid sections. The 3D SVG export is labeled SVG snapshot: it embeds the
canvas bitmap with vector annotations. Purposeful technical drawings need
their own geometry contract; retiring projections would discard useful
axis views before that contract exists.

**Workbench turn** (``design_turn.py``, slice 3): ``POST /se/{slug}/chat`` +
``/chat/apply`` and ``POST /structure/{slug}/chat`` +
``/chat/apply`` share ``design_chat.py`` and ``_design_chat.html.j2``. Each
blocking POST returns 303 with its outcome; a past ``?rev=`` returns 409.
``run_turn(hub, kind=, slug=, message=, handles=, model_call=)`` makes one
tool-less ``Tier.BIG`` ``route()`` call with a design digest and clicked
handles. Its ``{ops, rationale}`` reply is checked against the kind's roster;
an unknown op, raw coordinates, invalid JSON or failed dry run gets one repair
round, then rejects the whole turn without writing. Pure SE ops dry-run and
apply through one ``SeHandler.edit(turn=…)`` revision. Store-aware SE ops and
all structure ops require a proposal; ``StructureHandler.edit`` updates in
place, never ``derive``. Each design has one ``conv`` (``design-chat-<slug>``,
``related-to``); each answered turn adds one applied/proposal/rejected/no-op
block, and its revision stores ``<conv-slug>~<block ordinal>`` as ``turn``.

**Drive (`/drive`)** unifies seek and management. The active-folder chip
uses the sidebar tree without another lookup; missing/non-folder ids keep
their raw label and titles stay display-only, preserving saved ids. Discover
(`task=discover`, also the cookie-free landing) shows new content/work,
including findings. ``sort=created|modified`` keeps timestamp meaning across
browse/search; legacy ``recency`` maps to created in search and modified in
browse. No-query relevance maps to modified after acquisition, preserving old
queue links. Bare visits honor and label the saved kind cookie; Discover/reset
bypass it without overwriting preferences, so findings stay visible.

Search, effective-filter chips and reset stay visible beside the searchable
native picker. One bucket partition drives picker and presets: SE → Design,
finding/tex → Author, anki/citation → Derived, news → Sources, message →
Machine. Curated buckets override; new artifacts default to Author, other
registered/compatibility kinds to visible Other. This keeps operational kinds
reachable without implying author provenance. Plugin fallbacks retain
se/protein/route/pathway if hub introspection fails; hidden options remain
successful form controls. Submission captures the form before scheduling:
selected Alpine ``x-for`` tag/suggestion triggers disappear before deferred
``$root`` lookup. Return history and Showcase remain later slices, without
placeholder workflows.

``routes/drive.py::index`` searches cross-kind chunks with ``q=``, kind/tag
facets, ``sort=relevance|recency|oldest|untried`` and
``state=stub|deleted``. Legacy bare URLs list unfiled refs by ``updated_at``;
``folder=*`` ("Anywhere") removes that filter for a whole-kind pivot. Explicit
``k=`` beats ``items_kinds``, but only ``submitted=1`` writes the cookie.
``state=stub`` selects fetchable DOI/arXiv/S2 stubs, defaults to
``sort=untried`` via ``manual:open`` ``ref_events``, and ``POST
/downloads/mark-tried`` sinks opened rows. "Fetch next 25"
(``POST /drive/requeue-stubs``) stamps ``meta.oa_requeued`` for ``fetch_oa``;
``cited_by=<draft>`` scopes to papers-to-fetch via
``handlers/_citations_view.draft_fetch_ref_ids``. Replaced bespoke lists
(``/items``, ``/papers``, ``/drafts``, ``/papers-needed``,
``/refs/{oracle,patent}``, ``/cfp``) 307 to Drive presets; per-kind detail
readers stay. ``/`` → ``/drive``.

**System (`/status?tab=health|services|models|budget|now`)** —
``routes/status.py::index`` dispatches on ``tab=``: health strip, the
``/factory`` service tables + per-tier chain editor, the ``llm`` catalog
cards + live-routing header, budget cap/pause controls, and **Now**
(htmx-polled ``GET /status/now``: per-worker-process activity via
``precis.workers.activity``/``host_heartbeat.meta.activity``, plus
``kind='job'`` running/queued/recent-terminal lanes and active alerts).
``/factory`` and ``/budget`` GETs redirect into their sub-tab; POST
routes unchanged.

**Gripes (`/gripes`)** — ``routes/gripes.py``: list by ``STATUS``
(``open → triaged → ready_for_fix → in_review → wontfix``), filing,
detail/comment timeline, and ``retire`` (soft-delete, "fix landed", distinct
from ``wontfix``). Filing appends ``— filed by <login> …``; body is the record.

**Manual (`/manual`)** — ``routes/manual.py`` renders user how-to chapters
from ``src/precis_web/manual/``. They live in-package because the wheel ships
only ``src/``; each chapter ships with its described button. Ordered filenames
(``01-writing-a-paper.md`` → ``/manual/writing-a-paper``) supply routes;
title/blurb come from the first heading+paragraph, avoiding a drifting TOC.
Distinct from agent-facing ``precis/data/skills/`` and repo-dev ``docs/``.

**Categorizers (`/categorizers`)** — ``routes/categorizers.py``: axes/topics,
coverage + last-run (htmx OOB via ``GET /categorizers/progress``),
enable/disable toggles in ``service_config``, and Drive tag links.
"""

from __future__ import annotations

from precis_web.app import create_app

__all__ = ["create_app"]
