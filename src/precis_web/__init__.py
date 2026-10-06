"""precis_web — the cluster web surface for precis-mcp.

FastAPI service, imports ``precis`` directly, server-side rendered (Jinja +
HTMX + Alpine), served over the Tailscale LAN behind **HTTP Basic auth**
(``auth.py``). :func:`create_app` (``app.py``) wires one router per page,
error handlers, and a lifespan building the single
:class:`precis.runtime.PrecisRuntime`. Optional install extra
(``precis-mcp[web]``); ``precis web`` CLI subcommand imports it lazily.

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
won't replay Basic credentials into iframe subnavigations. Every 401 on a
*presented* credential logs login + peer address, and ``FailureTracker``
locks a login or an address after 10 failures in 15 min (429 +
``Retry-After``, refused before the scrypt); a success clears the login's
window only. Clickjack
defense is ``security_headers.py`` (outermost, rides the 401 too):
framing is same-origin, not ``DENY``/``'none'`` (the UI frames its own
PDF.js viewer). ``PRECIS_WEB_AUTH=off`` disables the gate — local dev
only. ``/account`` (``routes/account.py``): password, profile (ORCID iD
that nanopubs signed here attribute to), sign-out, podcast subscribe URL
(shown whole; the row holds only the token digest, so the vault is the
only other source). Sign-out = 401 with a fresh challenge (evicts cached
Basic credential) + session-cookie delete.

**Secrets.** Elsevier key checks use fixed OA Article Retrieval metadata;
ScienceDirect Search needs a separate entitlement and falsely rejects working
retrieval keys. CORE throttling preserves HTTP 429 as a rate-limited detail.
Add/replacement controls offer a write-only multiline editor;
existing values never populate it. Password inputs discard pasted line breaks,
so multiline values use a textarea and cannot switch to a password input while
line breaks remain. Blank replacement leaves the stored value unchanged; the
editor reads only masked inventory. Live character/line counts describe only
current input (Unicode code points and newline-delimited segments), not stored
lengths or validity. Saved counts are stamped in the existing masked hint by
``vault._hint`` at write time; legacy hints stay uncounted until replacement.
This keeps the render path free of decryption, backfill or stored-value forms.

Nav (template ``templates/base.html.j2``; badges ``nav.py::nav_badges``):
Daily (Drive, Tags, ToDo, Design) always visible; Browse ▾ (Quests,
Schedules, Clusters, Structures, CAD, Figures, Mermaid); Attention (Needs
you, Gripes, Alerts, badged); Manual (top-level, unbadged — the
no-idea-where tab, so never in a dropdown); Ops ▾ (System, Categorizers,
Agent Logs, Console, Env, Secrets); 🔍 loupe submits to ``/drive``; "?"
tour launcher (only on a page whose path matches a tour manifest's
``route`` — ``routes/manual.py::tour_slug_for_path``, no extra client
fetch); Account (far right, signed-in user's ``abbrev`` chip) →
``/account``.

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

**Workbench turn** (``design_turn.py``, slice 3's engine; the routes are
``POST /se/{slug}/chat`` + ``/chat/apply`` in ``routes/blocktree_view.py``
and ``POST /structure/{slug}/chat`` + ``/chat/apply`` in
``routes/structure.py``, sharing ``design_chat.py`` and the
``_design_chat.html.j2`` panel — one blocking POST per turn, 303 back with
the outcome in the query string, 409 on a past ``?rev=``):
``run_turn(hub, kind=, slug=, message=, handles=, model_call=)``
is one tool-less ``Tier.BIG`` ``route()`` call whose prompt is a text
digest of the design plus the clicked handles; the reply is JSON
``{ops, rationale}`` vetted against the kind's real roster (unknown op,
raw coordinates, no JSON, or a failed dry run → the validator's message
goes back to the model for ONE repair round; still bad → whole turn
rejected, nothing written to the design). Pure se ops dry-run then
auto-apply via ``SeHandler.edit(turn=…)`` as one revision; store-aware se
ops and every structure op come back as a proposal for ``apply_proposal``
(``StructureHandler.edit`` in place, never ``derive``). Transcript = one
``conv`` per design (``design-chat-<slug>``, linked ``related-to``), one
block per answered turn tagged applied / proposal / rejected / no-op; the
revision's ``turn`` is ``<conv-slug>~<block ordinal>``.

**Drive (`/drive`)** is the unified seek+manage surface:
Discover (``task=discover``; also the cookie-free landing) shows newly created
content/work across folders, including findings. ``sort=created|modified``
names the timestamp consistently in browse/search; legacy ``recency`` retains
its original branch-specific order. A saved kind cookie is honored on bare
visits and labelled visibly; Discover/reset bypasses it without overwriting it.
This keeps personal preferences while making broad discovery explicit rather
than silently hiding findings. Search, effective filter chips and reset stay
visible; a native details picker contains a searchable full kind roster and
the shared filters. Hidden/closed kind options stay successful form controls.
Deferred filter submission captures the form before scheduling: tag chips and
suggestions destroy their own Alpine ``x-for`` trigger when selected, so its
element-bound ``$root`` cannot be resolved later from the timer.
Operational/stateless kinds remain reachable without treating kind as author
provenance. Return's personal history and Showcase's curated collection are
subsequent slices; neither has a placeholder workflow here.
``routes/drive.py::index`` runs cross-kind chunk search (``q=``, kind/tag
facets, ``sort=relevance|recency|oldest|untried``, ``state=stub|deleted``)
over the folder tree + CRUD. Legacy no-query URLs list unfiled refs by
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
