---
status: draft
pillar: platform
---
# Drive Return and Showcase

Remaining approved behavior: **Return** uses personal viewing history;
**Showcase** uses a curated collection, including emerging discoveries.
Storage, sharing and schema choices below remain a proposal for owner review.
Discover and the shared searchable kind/filter picker are described in
`src/precis_web/__init__.py`; their completed implementation spec is retired.
Reuse that picker, visible search/active chips/reset, URL facets and saved-kind
behavior. Do not add placeholder task controls or infer quality/provenance.
Clouds, fingerprints and scale exploration stay separate; SE drawings stay draft.

## Return / Showcase — assigned storage review

Proposal against `6cf9d5d04`; Discover/correction commits stay immutable and
their independent browser review continues separately. This is a proposed
technical contract for owner/coordinator review, not additional behavior approval.

### Checked premises

- `precis.users.WebUser.id` is a stable bigint identity. Auth sets
  `request.state.web_user`; `auth.current_user()` returns None when auth is off
  and explicitly forbids guessing an identity. All accounts currently have full
  corpus access; there is no general per-user ACL. Use the authenticated id,
  never a client-supplied login/user id, display abbreviation or browser cookie.
- `store/_refs_ops.py::touch_viewed` updates ref-global `last_viewed_at`;
  `routes/papers.py::detail` calls it after the canonical redirect and also on
  htmx requests. It cannot mean personal actual-open history. Smartdraft's
  `reader` does not call it; its retired listing uses global `viewed_desc`.
- Draft rows open `/smartdraft/{id}` (`item_view._OPEN_URL_OVERRIDES`), with
  legacy `/drafts/{ident}` redirecting there. Nanobuds is a draft use case, not
  another kind/reader; no actual draft handle has been supplied/verified here.
  Generic `/refs/{kind}/{id}` resolves live refs, redirects structure/PCB to
  their workbenches and claim-hub findings to `/claim/fi…`. Record the resolved
  ref at the final reader, not aliases/redirects, chunk handles or titles.
- `ref_events` has ref/source/event/time/JSON payload and indexed audit reads,
  but no user FK or personal-history query/deletion contract. `refs.owner_login`
  owns an item; it does not identify every viewer or a collection member.
- `folder` uses one `refs.parent_id` and moves placement; `oracle` contains
  wisdom blocks within one ref. Existing `links` are globally exposed graph
  edges; `contains/part-of` means component BOM. Tags have no collection object
  or user isolation. None is an existing general personal curated collection.
- Browse already accepts `recent_refs(ref_ids=…)`; cross-kind search accepts
  `include_ref_ids=…` through its search legs. Scope can constrain the existing
  filter/query paths before pagination; no new kind API or search engine needed.

### Proposed data / lifetime / sharing contract

One forward-only migration creating **three new tables**, with no existing
table/column changes, historical attribution or production backfill:

| New table | Key and fields | Lifetime / indexes |
|---|---|---|
| `web_ref_views` | PK `(user_id, ref_id)`; FKs to `web_users.id`, `refs.ref_id`; `last_opened_at timestamptz NOT NULL` | Both FKs ON DELETE CASCADE; index `(user_id, last_opened_at DESC, ref_id DESC)`; upsert latest server timestamp with GREATEST. One latest record per item/user, no click stream or URL/query text. |
| `web_collections` | `collection_id bigint identity PK`, `owner_user_id` FK UNIQUE, `name text NOT NULL` (initially Showcase), `created_at timestamptz` | Owner FK ON DELETE CASCADE; one explicit collection per user in this bounded slice. Lazily create on first explicit addition, not a read. |
| `web_collection_members` | PK `(collection_id, ref_id)`; both FKs; `added_at timestamptz NOT NULL` | Both FKs ON DELETE CASCADE; index `(collection_id, added_at DESC, ref_id DESC)`; duplicate add preserves original added time. |

Recommend **private, owner-readable/writable Showcase for this slice**, with no
sharing endpoint, public link or visibility flag. This is a sharing proposal
for review, not a claim Reto selected private sharing. Other users cannot list
or change its membership; foreign collection ids return 404. Collection privacy
does not change existing corpus access. No new MCP verbs/surfaces in this slice.

Retain latest viewing records and curated membership until explicit removal or
account/ref deletion; no arbitrary TTL or automatic quality pruning. Forget one
view / clear own history removes only `web_ref_views`; removing a member preserves
the item and its folder/tags. Clear history leaves Showcase untouched. Soft
retirement hides refs in both tasks but retains records for undelete; hard delete
cascades. Rename/move changes display/reader URL, not identity. Supersession does
not silently transfer a curator's membership to another ref. Disabled accounts
retain state but cannot authenticate; deletion removes their state. Recreating
the same login gets a new id and inherits nothing. Collection/account deletion
and history clearing remain explicit actions, not background cleanup.

### Actual-open / API / query contract

- Add `precis_web/view_history.py` and a shared detail-only template marker.
  Successful full reader documents mark the resolved live ref id; the browser
  sends `POST /drive/opened` once on visible `pageshow` (wait for first visibility
  if opened in a background tab). Back/forward restoration and explicit reload
  count as reopening; ordinary tab refocus does not. No GET-side personal write.
  The marker is absent from list/error/redirect, hover, htmx fragments, polling,
  assets/PDF/exports and background data responses. Embedded previews do not
  count; a full visible reader opened from a preview does. This concrete boundary
  avoids treating current paper salience/global-view side effects as history.
- Reader coverage: papers, smartdraft including legacy redirects, generic refs
  including conversations/non-hub findings/quests, claim hubs, datasheets,
  CAD/structure/SE/PCB/figure/mermaid workbenches, plus a valid focused todo
  `/todo?focus=id` (not the unfocused dashboard). Alternate full views of the
  same ref coalesce. Subordinate structure runs stamp their parent structure.
  Add markers only after successful ref resolution; never guess from URL regex.
- `POST /drive/opened {ref_id}` derives user from current_user, requires a live
  ref, returns 204 after persistence, 404 for missing/retired refs, refuses missing
  identity (including auth-off), and reports storage failure rather than silently
  claiming saved history. Use existing same-origin mutation guard; no client
  timestamp/user id. A failed history write must not break the reader itself.
- `GET /drive?task=return` scopes to that user's views, defaults to all folders
  and all selectable kinds, `sort=viewed` = personal last-open descending.
  `task=showcase` scopes to the caller's collection, `sort=curated` = explicit
  addition descending. Both are consistent with/without q; id-desc breaks ties.
  Reset stays within task, clears refinements and bypasses saved kind preferences
  without writing them. Explicit kinds/facets and existing submitted-cookie
  semantics win. Findings/emerging items remain reachable, including filed ones.
- Reuse search/picker/chips and existing facet applicability labels. Constrain
  browse/search SQL before ordering/count/limit; never filter a global first page
  or reorder an already paginated set. Add internal scope/order joins to existing
  store query paths; preserve `sort=recency` and identifier/DOI redirects. Query
  still searches body chunks; no promise that chunkless items match free text.
  Other explicit supported sorts remain available within each task's scope.
- Add authenticated POST routes `/drive/history/forget {ref_id}`,
  `/drive/history/clear`, `/drive/showcase/add {ref_id}`, and
  `/drive/showcase/remove {ref_id}`. Membership changes require explicit user
  action; opening/searching/modifying/agent synthesis never adds a member or
  changes `added_at`. Mutations redirect to a validated local Drive return URL
  carrying facets, and are idempotent. No individual pin or inferred ranking.
- `store/_drive_state_ops.py` owns `record_open(user_id, ref_id)`,
  `forget_open`, `clear_history`, `ensure_showcase(user_id)`, `add_member`,
  `remove_member`, and scoped reads. Every read/write carries authenticated
  user id and verifies collection ownership in SQL. No global event-log copy.
  Record/query operations use indexed joins; personal routes refuse auth-off.

Identity limitation for owner review: existing auth cannot distinguish a human
from an automated browser using **the same human credentials**. Normal MCP/worker
reads, HTTP fetches without the visible-page beacon and other WebUser accounts
cannot reorder personal history. Do not give agents the human's authenticated
browser context and promise human attribution; that requires a separate identity
or explicitly reviewed automation exclusion, not a different history table.

### Named focused cases / release boundary

Plan `tests/test_drive_state_store.py`,
`tests/precis_web/test_drive_return_showcase.py`, and real Chromium/CDP cases:

1. **personal_open_isolation**: A opens draft D then finding F; B opens D and an
   agent reads/modifies D → A remains F,D; q/no-q use personal viewed time.
2. **smartdraft_actual_open**: numeric/slug/legacy draft URLs, paper canonical
   redirect, claim-hub redirect and focused todo each record the final stable
   ref once. Use a fixture named nanobuds; deployed real handle only when given.
3. **no_passive_open**: hover/claim preview, smartdraft blocks polling, paper
   htmx/JSON, downloads, lists, errors, hidden-tab/refocus, MCP and auth-off
   cannot stamp personal state; visible reader/navigation/back-forward can.
4. **curation_persists**: explicitly add filed finding + draft, reconnect with
   new session, filter kinds away/back → same members; repeated add/open/edit
   does not reorder membership. An uncurated impressive item stays Discover-only.
5. **owner_and_lifecycle**: forged user/foreign collection, disabled/deleted/
   recreated account, forget/clear vs collection, soft-delete/undelete and hard
   delete, rename/move/supersession; no cross-user leaks or orphan memberships.
6. **scope_before_page**: >100 matches with unrelated global leading results;
   exact scoped browse count/order/page and scoped search membership, empty scope
   never becomes global, deterministic ties, no-query/query sort consistency.
7. **task_contract_browser**: hidden selected kinds, tag add/remove deferred
   submit, task reset/saved-cookie isolation, handle/DOI, no console errors and
   curator buttons persist membership without losing URL facets.

Storage review must resolve private collection scope, no-TTL latest-history
retention, visible full-reader boundary and the three additive tables before
implementation assignment. No populated-table change is required by this design:
`thresholds.md`'s “Adding a column to an existing table that is already populated”
does not apply to CREATE TABLE. Adding `user_id` to existing `ref_events`, or
collection/view columns to `refs`/`web_users`, would trip it and is rejected here.
Alternatives: event JSON avoids DDL but lacks user FK/indexed latest-state and
isolated deletion and exposes history through global audit readers; user tags/
folders/graph edges reuse storage but lack the collection/isolation contract or
change item placement. A new-table migration still needs owner review and the
normal migration `/go` gate; no number reserved or SQL authored in this task.
No full suite, production/backfill/deploy or source/UI change at storage review.

## Questions for communicator

1. Return/Showcase owner review: personal-open storage/lifecycle and curated
   collection membership/sharing/API. A real nanobuds/demo handle will ground
   later dogfood when available; preferences themselves are approved.

Integration acceptance example: recover a known design such as the unicycle
from a fresh session, deep link and saved selection; deployed SHA and actual
handle must be supplied before claiming dogfood.

Related: `drive-presenter-completeness.md`, `kind-taxonomy-audit.md`;
design scale is separately tracked in `drive-characteristic-scale.md`.
