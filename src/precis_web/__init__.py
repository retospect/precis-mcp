"""precis_web — the cluster web surface for precis-mcp.

FastAPI renders Jinja, HTMX and Alpine behind HTTP Basic on the Tailscale
LAN. :func:`create_app` wires routers, errors and one lifespan-owned
:class:`precis.runtime.PrecisRuntime`. ``precis web`` lazy-loads ``[web]``.

**Ask chat.** Startup logs ``chat_turns.preflight`` warnings and proceeds.

**Reaction ledger (`/rxn`).** Handler-computed ledgers and kept rxn records;
readers recompute (storage/scientific rationale: `routes/rxn.py`).

**Lists.** ``drive_redirects`` preserves filed rows, modification windows
and direct quest scope when folding refs lists into Drive.

**Ref readers.** ``linkify.linkify_toon`` renders memory Markdown with
escaped HTML, literal code and single newlines. ``ref_urls`` maps handles
and pivots. Stored detail tolerates absent handlers; ORCID uses stored
identity and papers. File readers use chunks because filesystem ``get``
can retire refs on missing mounts. ``routes.papers.replace_pdf`` preserves identity.

**Anki cards.** Keep each math span intact so TeX braces and colons stay
inside cloze answers. KaTeX renders the escaped source.

``/graph/{kind}/{id}.json`` (``routes/graph.py``) is ``Store.neighbourhood``
as JSON plus ``ring_group`` buckets.

**Claim approval.** Disputes show advisory support verdicts, reasoning
and typed ``conflict_search`` pins to counterpart chunks. Coverage reads the
dated ``meta.conflict_search`` ledger. Missing or stale coverage queues
``conflict_sweep`` pre-approval (daily per hub, dark unless lit). Approval
gates stay independent. Disputes offer ``mint_counter_claim``.

Gripe counts link to exact SQL-filtered statuses. The live total counts
nonterminal gripes. Empty lists name the selected status.

**Pathway diagram.** ``refs._pathway_graph_payload`` preserves catpath's
``link_type`` for folding/annotations: adsorption and desorption keep their
columns; only supply→reaction folds. Raw ``kind=supply`` still excludes
reservoir links from kinetic fork probabilities; changing it implies
a measured barrier. Legacy graphs fall back to ``kind``.
Unknown barriers draw dashed ``Ea n/a`` connectors, never zero-height peaks.

**Smartdraft review.** Block widget shows read-only F/C/S/A flags
(✓ current / ~ stale / – unreviewed); structure/adversarial on prose,
S/A on headings. One matrix for full-page and lazy blocks avoids ledger
queries and tooltip drift. Machine-authored blocks
(``store.drafts.authored_provenance``) get an inner-shadow stripe —
grounding provenance owns the left border; "around here" is
``draft_eyes.expand_around``. A per-chunk ⚠ marker
(``smartdraft.chunk_issues``) lists issues.

**Auth.** ``auth.py::BasicAuthMiddleware`` gates every route/mount against
``web_users`` (migration 0131; roster via ``precis users``); accounts have
no roles or per-route ACLs. ``/healthz`` is the probe exemption.
``/podcast`` self-authenticates only with a per-user ``?t=`` token (Basic
is inconsistent across podcast clients on enclosure URLs). Empty roster → 503
with ``precis users add``. State-changing Origin/Referer mismatch → 403:
Basic has no ``SameSite`` CSRF defense. A signed ``SameSite=Lax`` cookie is
also accepted — Safari won't replay Basic into iframe subnavigations.
Presented 401s log login + peer; ``FailureTracker`` locks a (login, address)
pair after 10 failures in 15 min, an address after 50, a login after 100
(429 + ``Retry-After``, before scrypt); success clears the pair. Outermost ``security_headers.py``
allows same-origin framing: the UI embeds its own PDF.js viewer;
``PRECIS_WEB_AUTH=off`` is local-dev only. ``/account`` handles password,
profile (ORCID attributed by local nanopubs), sign-out and podcast URL. The
row stores only the podcast token digest, so the vault is the other source.
Sign-out sends a fresh 401 (evicting cached Basic credentials) and
ends the login's sessions, as does a password change.

``create_app`` builds the middleware stack eagerly: Starlette's lazy
first-request build races under a restart's reconnect flood, and each
racer would serve its own gate, ``FailureTracker`` and credential cache —
splitting the lockout buckets (gr778229). One stack is one shared
tracker; a late ``add_middleware`` now raises.

**OAuth.** ``precis.oauth`` owns grants; ``routes.oauth`` authenticates authorization before reserving capacity.

**Access logs / Secrets.** ``access_log.py`` masks ``t`` queries;
``routes/secrets.py`` provides write-only controls.

Status separates liveness/problems for indexing.

PCB component selection (``pcb_design.py``) uses exact instance IDs in
same-origin Gerber targets; one shown snapshot serves HTML, SVG, chat
context and Apply. SVG geometry caches UTF-8 payloads under entry/byte
bounds to cap retained memory; live hover facts are added per request so
catalogue updates need no Gerber re-render. Apply commits
move/history/consumption atomically; GET never calls a model or writes the
board. Historical revisions are summaries.
PCB chat's ``pcb-design-chat-`` slug prefix cannot collide with legacy
``design-chat-`` slugs; typed reads, append and Apply verify numeric parent
links and ownership.

Nav (``templates/base.html.j2``; badges ``nav.py::nav_badges``): Daily
(Drive, Design); plain links (Quests → ``/refs/quest``, Clusters,
Reactions → the ``/rxn`` ledger — top-level rather than folded
into Drive's New form, gr664411). The former Browse ▾ kind presets are
Drive's sidebar "Saved views" (``drive_views.py``: Drafts, Papers,
Schedules, Structures, CAD, PCB, Figures, Mermaid, plus the Nanopubs
workbench link); the Todos ``queue=doable`` preset — the retired ToDo
tab's dashboard (gr664410; ``/todo/{id}`` stays per-item) — is a Drive facet. Attention (Needs you, Gripes, Alerts,
badged); Manual (top-level, unbadged — the no-idea-where tab);
Ops ▾ (System, Categorizers, Agent Logs, Console, Env, Secrets); search submits
to ``/drive``; "?" launches a tour only when the path matches a manifest
``route`` (``routes/manual.py::tour_slug_for_path``, no extra fetch); Account
(signed-in user's ``abbrev``) → ``/account``.

**Design (`/design`)** — ``routes/design.py``: the hierarchical
molecule-design workbench's landing tree, one tree per live ``se`` design (block
graph, array nodes collapsed to ``name ×N``, L0–L3 presence badges
derived from the loaded columns — never geometry) down to the
``structure`` each atomic-mode leaf is bound to
(``se_blocks.bound_kind='structure'``/``bound_design``, not a link), plus
a flat "Loose structures" section for every unbound live ``structure``. Pure read, ≤ 3 SELECTs per render regardless of design count; a
node click lands on the existing ``/se/{slug}``/``/structure/{slug}``
page.

SE inspection keeps 3D primary, with ``/se/{slug}/2d`` labeled Envelope
projections because its outlines are envelope convex hulls, not realized
solid sections. The 3D SVG export is labeled SVG snapshot: it embeds the
canvas bitmap with vector annotations. Purposeful technical drawings need
their own geometry contract; retiring projections discards useful
axis views before that contract exists.

**Workbench turn** (``design_turn.py``): chat POSTs for ``se``/``structure``
designs; routes, repair and apply policy live in its docstring.

**Drive (`/drive`)** unifies seek and management. The active-folder chip
reuses the sidebar tree; missing/non-folder ids keep
their raw label, titles staying display-only and preserving saved ids. Discover
(`task=discover`, also the cookie-free landing) shows new content/work,
including findings. ``sort=created|modified`` keeps timestamp meaning across
browse/search; legacy ``recency`` maps to created in search and modified in
browse. No-query relevance maps to modified after acquisition, preserving old
queue links. Bare visits honor and label the saved kind cookie; Discover/reset
bypass it without overwriting preferences; findings stay visible.

Search, effective-filter chips and reset stay visible beside the searchable
native picker. One bucket partition drives picker and presets: SE → Design,
finding/tex → Author, anki/citation → Derived, news → Sources, message →
Machine. Curated buckets override; new artifacts default to Author, other
registered/compatibility kinds to visible Other, keeping operational kinds
reachable without implying author provenance. Plugin fallbacks retain
se/protein/route/pathway if hub introspection fails; hidden options remain
successful form controls. Submission captures the form before scheduling:
selected Alpine ``x-for`` tag/suggestion triggers disappear before deferred
``$root`` lookup. Return history and Showcase remain later slices, without
placeholder workflows.

``routes/drive.py::index`` searches cross-kind chunks with ``q=``, kind/tag
facets, ``sort=relevance|recency|oldest|untried`` and
``state=stub|deleted``. Bare URLs list unfiled refs by ``updated_at``;
``folder=*`` ("Anywhere") lifts that filter. Explicit
``k=`` beats ``items_kinds``; only ``submitted=1`` writes the cookie.
``state=stub`` selects fetchable DOI/arXiv/S2 stubs less the cooled no-OA
bucket, page 1 listing read-only (``Store.manual_retrieval_list``);
defaults to
``sort=untried`` via ``manual:open`` ``ref_events``; ``POST
/downloads/mark-tried`` sinks opened rows. "Fetch next 25"
(``POST /drive/requeue-stubs``) stamps ``meta.oa_requeued`` for ``fetch_oa``;
``cited_by=<draft>`` scopes to papers-to-fetch via
``handlers/_citations_view.draft_fetch_ref_ids``. Bespoke lists
(``/items``, ``/papers``, ``/drafts``, ``/papers-needed``,
``/refs/{oracle,patent}``, ``/cfp``) 307 to Drive presets. ``/`` → ``/drive``.

**System (`/status?tab=health|services|models|budget|now`)** —
``routes/status.py::index`` dispatches on ``tab=``: health strip, the
``/factory`` service tables + per-tier chain editor, the ``llm`` catalog
cards + live-routing header, budget cap/pause controls, and **Now**
(htmx-polled ``GET /status/now``: per-worker-process activity via
``precis.workers.activity``/``host_heartbeat.meta.activity``, plus
``kind='job'`` running/queued/recent-terminal lanes and active alerts).
``/factory`` and ``/budget`` GETs redirect into their sub-tab; POST
routes unchanged.

**Card review** — ``/anki`` and ``/learning`` badge automatic cards and
retire batches by source and UTC mint day for Anki sync to remove.
Learning includes orphan cards. Walk drops record owner-specific
suppression on the retired card to preserve shared concept state.

**Recent walk** — ``precis.eye_visits`` records authenticated ``/eye`` visits for ``/learning``.

**Work (`/work`)** — ``routes/work.py``: read-only quest → root → slices
hierarchy off ``precis.todo.work_view.work_model`` (the MCP ``view='work'``
source); rows carry ``data-handle`` and link to
``/eye/<handle>``; linked from the System sub-tab bar.

**Gripes (`/gripes`)** — ``routes/gripes.py``: list by ``STATUS``
(``open → triaged → ready_for_fix → in_review → verify → done/wontfix``),
filing,
detail/comment timeline, and ``retire`` (soft-delete, "fix landed", distinct
from ``wontfix``). Filing appends ``— filed by <login> …``; body is the record.

**SE validation selection** — ``routes/blocktree_view.py`` owns
block/port selection for finding messages: view-state only, with an
activation recheck refusing stale rows.

**Manual (`/manual`)** — ``routes/manual.py`` renders in-package chapters
from ``src/precis_web/manual/`` so the wheel includes them. Ordered filenames
determine routes. First headings and paragraphs supply TOC titles and blurbs.
Distinct from agent-facing ``precis/data/skills/`` and repo-dev ``docs/``.

**Categorizers (`/categorizers`)** — ``routes/categorizers.py``: axes/topics,
coverage + last-run (htmx OOB via ``GET /categorizers/progress``),
enable/disable toggles in ``service_config``, and Drive tag links.
"""

from __future__ import annotations

from precis_web.app import create_app

__all__ = ["create_app"]
