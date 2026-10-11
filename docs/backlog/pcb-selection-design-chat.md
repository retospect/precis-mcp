---
status: draft
title: "pcb: select component identity and attach it to design chat"
pillar: 3d-design
prio: high
---

# Select PCB components for design changes

Reto requested this interaction2026-10-07T18:59:53Z: click a component,
see its footprint ID and send a design-change message as on SE/structure.
Owning request: fleet-state/inbox/pcb-selection-design-chat.md. Existing
PCB owner window6 owns source mapping and the subsequent reviewed slice;
window25 retains its active SE validation-click work. No new session.

## Checked premise

PCB detail currently embeds board.svg as an object and schematic.svg as
an image, with no selection/chat panel. The shared design_chat panel and
design_turn proposal/apply machinery exist; DesignKind currently accepts
only se/structure. PCB support therefore needs an explicit adapter,
not merely inclusion of the partial. Source comparison47530..currentmain
shows these route/chat/template/SVG files unchanged. Native Python symbol
lookup corroborated pcb_detail and design_chat outline, but served/app
is not the task worktree. This is source evidence, not live browser proof.

## First deliverable — source map and bounded adapter plan

Before implementation, map current board instance identity, footprint
catalogue identity, SVG ownership/hit targets, graph/revision persistence,
existing supported PCB edits, design history and shared chat contracts.
Inspect owning package docstrings and fresh origin/main in an isolated
slice. Final all-status dedup and publication of a distinct requirement
belong to PCB owner; unrelatedgr458878 is not a duplicate. Preserve related
records and return the new ID with native readback.

Return exact files and proposed request/selection/revision shapes, which
existing operations can be proposed/applied, how dry validation and atomic
stale rejection work, and a meaningful test plan. Coordinator reviews
this map before source changes. Any missing persistence/transaction seam
must be identified concretely; no speculative schema or second chat engine.

## Interaction and identity contract

- A click or keyboard activation selects the exact shown board instance.
  Show separately its stable instance ID/refdes and library footprint ID.
  Two instances using one footprint must remain distinct; missing catalogue
  IDs are explicitly unavailable, never substituted with refdes.
- Reuse current SVG/rendered instance identity where possible. Do not infer
  identity from screen position, display text or an arbitrary first match.
  Selection highlights that component, is copyable, supports deselection,
  and does not mutate geometry, routing, connections or annotations.
- Message context includes board identity, exact shown revision or an
  existing trustworthy snapshot token, stable selected instance and footprint
  identity. Server re-resolves and validates it. Reload/replacement clears
  stale selection; mismatched, ambiguous or removed targets are refused.
  Check both submission and Apply, including change during model generation.
- Reuse shared transcript/proposal UI and supported PCB operations through
  an additive PCB adapter. PCB design changes require an explicit human
  Apply to a visible proposal; selection or message generation alone must
  not silently modify the board. Preserve SE/structure behavior and history.
  Unsupported operations are explained, not executed as raw tool text or
  arbitrary code. No model call occurs on page load, selection or polling.
- Validate selected targets independently of the model response; prevent
  forged cross-board references, double Apply and stale proposal replay.
  Existing auth/CSRF/error conventions remain authoritative. A history view
  is read-only unless a supported explicit current-state flow is chosen.

## Boundaries and sequencing

Start with component instances/footprints only, not tracks/pads/nets or a
new router. Suggested owned files are routes/pcb.py, PCB template/client,
PCB SVG identity adapter and focused tests. Shared design_chat/design_turn
extensions and web/PCB rationale paragraphs require the reviewed map and
explicit file ownership before edits; no changes to window25's blocktree
or structure files. Preserve all other active owners' work and scratch.

The completed routing levers remain complete; pcb-escape-local-repair
stays draft. This request assigns a viewer feature, not that repair build.
No autonomous real-board edits/routing, EasyEDA operations, fabrication,
provider jobs or new science/compute. Runtime tests use fixtures/dev DB;
the named real/private URL is context, not permission to mutate it.

Implementation follows source-map acceptance, then focused and rendered
browser checks, full scripts/test, canonical types and host Ruff/format,
changed-line coverage, source review, separately assigned version and normal
scripts/ship. No bump/ship slot yet; frozen R18 deployment is independent.
After actual landing, report exact SHA and normal round mark, without
inventing a new round or treating CI as deployed acceptance.

## Acceptance

Demonstrate click, keyboard, deselect and copy on rendered fixture board;
two instances share a footprint yet attach distinct instance targets.
Cover missing footprint, overlapping/ambiguous renderer hits, stale page,
replaced/deleted component, forged cross-board context, concurrent edit
during proposal, duplicate Apply and historical read-only view. Verify
selection/GET never writes or computes; proposal alone never changes PCB;
explicit supported Apply changes only the intended current target and
records its outcome. SE/structure chat regressions remain green. Full
browser/test evidence and scoped source review precede any shipping claim.

## Source map for review — gr473212

Map only, not implemented. Baseline origin/main
`91d6af1353630cac8ca800d712d1a9f30231c90d`, task branch
`work/pcb/selection-design-chat`. The control text above was adopted
byte-exact at SHA256
`b65f452b705d9bc19d4121c2d392aec1213dcd293e03d86dd69a8da28a7cb428`;
this section is the PCB owner's additive map. Source implementation,
version assignment and shipping require subsequent reviewed assignment.

Native Python search/outline resolved the symbols below in repo `precis`.
The coordinator's retained native receipt identifies its served root as
`/src`, which differs from this isolated task checkout. The map therefore
checks the corresponding local source; there is no live-browser or deployed
feature claim and no new native-status probe.

### Checked seams and proposed ownership

| File / symbol | Present contract | Proposed change after review |
| --- | --- | --- |
| `src/precis_web/routes/pcb.py`: `pcb_detail`, `pcb_board_svg` | Current vitals plus object-embedded fab SVG; no panel, token or POST | Serve a consistent shown-board manifest/token; add PCB chat and Apply POSTs, historical read-only summary |
| `src/precis_web/templates/pcb/detail.html.j2` | Board object and schematic image only | Component selection/status/copy/deselect UI and existing shared panel; load a vendored PCB client |
| new `src/precis_web/static/pcb-selection.js` | No PCB selection client | Bind authenticated same-origin object load to its manifest; keyboard/pointer selection; exact target context; no model calls or writes |
| `src/precis/pcb/gerber_view.py`: `Flash`, `_flash_svg`, `_flash_hit_target`, `render_fab_svg` | Gerber X2 preserves refdes/pin on flashes; tooltips exist, stable instance attributes do not | Optional instance manifest: explicit escaped attributes on owned flashes/mask hit targets and highlight layer; leave ordinary fab output unchanged when absent |
| `src/precis/handlers/pcb.py`: `_render_fab_svg`, `_op_move`, `_move_poses` | Fab path exports Gerber then reads it; move may carry a generator group and rip router copper | Pass an explicit resolved manifest for web selection; reuse move through the guarded adapter, refusing unreviewed collateral effects |
| new `src/precis_web/pcb_design.py` | No PCB design-chat adapter | Typed context/identity resolution, snapshot projection, digest, restricted op validation, rollback-only dry validation and guarded Apply |
| `src/precis/store/_pcb_ops.py`: `pcb_load`, `pcb_judged_tx`, `_pcb_conn`, `_pcb_tx` | `pcb_load` returns live instance IDs; ambient transaction shares PCB reads/writes; no compare-and-apply/lock seam | Add a PCB read-snapshot/guard context on the existing transaction surface; reviewed row/cache lock contract below |
| `src/precis/pcb/snapshot.py`: `export_query`, `export_snapshot`, `normalize` | One SELECT captures raw relational inputs; replay normalization renumbers IDs and includes job configuration | Factor a raw connection-taking capture helper if needed, preserving public replay behavior; token/checkpoint projection retains real IDs and excludes job/volatile bookkeeping |
| `src/precis_web/design_turn.py` | `DesignKind=se|structure`; tool-less model/one repair round; transcript, proposal, Apply | Add PCB dispatch to the existing engine; persist structured PCB context, force proposal-only, load canonical proposal for PCB Apply |
| `src/precis_web/design_chat.py`, `templates/_design_chat.html.j2` | Shared forms use free-text handles and client-posted ops; non-SE captions mean atoms | Optional PCB context/form captions; use stored proposal handle on PCB Apply; preserve existing SE/structure forms and policies |
| `src/precis/handlers/conversation.py`: `ConversationHandler.put`; `src/precis/store/_chunks_ops.py`: `insert_chunks` | Conversation append predicts ordinal and writes without a caller transaction; chunk insertion already accepts `conn=` | Reviewed shared writer extension: resolve/create conv and allocate/append its actual turn under one connection/conv lock; reuse `insert_chunks(conn=...)`, no second store |
| `src/precis/design/history.py`: `save_checkpoint`, `record_revision`, `list_revisions` | Opaque JSON snapshots, optional `conn=`, existing `UNIQUE(ref_id,rev)` | Reuse unchanged APIs; no migration or new history table |
| `src/precis_web/__init__.py`, `src/precis/pcb/__init__.py` | Present subsystem rationale | Compact why: stable ownership, actual displayed snapshot and human Apply; preserve prior rationale |

These are proposed ownership, not edit permission for shared files. Coordinator
must explicitly assign `design_turn.py`, `design_chat.py`, the shared partial,
web/PCB rationale, the PCB store/snapshot guard and any transcript-helper
extension, including the conversation handler if this append strategy is chosen.
No edits to window25's `routes/blocktree_view.py`,
`routes/structure.py`, their templates or SE/structure persist layers.

### Instance and footprint identity

`pcb_load(ref_id)` joins `pcb_instances.instance_id/refdes` with component
`part_lcsc/footprint`. An instance ID survives an in-place move; a retired,
recreated or generator-replaced instance has a different ID. Resolve by
`ref_id + instance_id`, require live membership and matching refdes, and
never resolve by footprint, text position or first matching label.

`pcb_graph` omits instance_id. It is an analysis projection, not a selection
identity source. The generic component `footprint` text is a package label
for a catalogue part, or a design-local key when part_lcsc is absent:
neither interpretation can silently become the other's ID.

For a cached catalogue footprint, `part_footprints` is keyed by LCSC
C-number (a cache/part key). `easyeda.parse_component` records the actual
package UUID in `source="easyeda:packageDetail:<uuid>"` when present.
Expose that UUID as the library footprint ID, the C-number as its separate
part/cache key, and the component footprint text as a separate label.
Recognized UUID absence stays unavailable; do not invent one from refdes,
C-number or package label. No provider fetch to fill the gap.

For a design-local footprint, resolve the exact stored
`pcb_local_footprints(ref_id,name)` row and expose a board-scoped local
identity. Missing rows/UUIDs are explicit null/unavailable, with selection
still possible. Two component instances sharing either footprint retain
separate selected instance IDs.

Proposed resolved selection manifest/context (illustrative IDs only):

```json
{
  "board": {"ref_id": 42, "slug": "fixture-board"},
  "shown": {"snapshot": "pcb-view-v1:<sha256>", "revision": null},
  "selected": [{
    "instance_id": 101,
    "refdes": "U1",
    "footprint": {
      "scheme": "easyeda-package",
      "id": "<stored-package-uuid>",
      "part_lcsc": "C123",
      "label": "QFN-32",
      "available": true
    }
  }]
}
```

Local footprints use `scheme="pcb-local"`, their actual name and board owner;
missing IDs use null plus an explicit reason. Revision null means no recorded
PCB save, not a fabricated revision 1. The server recomputes all identity
fields and rejects altered or mismatched echoes.

### SVG and shown-board binding

Keep the fab renderer and layer toggles. The handler resolves a manifest
from the same read-only repeatable snapshot used to build the board picture.
The SVG root carries board ref/token; its structured metadata names the exact
instance inventory and separately resolved footprint identities. Bind
Gerber flashes through their own refdes/pin plus that inventory to stable
instance IDs. No title parsing, nearest-coordinate lookup or arbitrary first
match. Unowned flashes/tracks/vias remain unselectable.

The page passes its shown token to `board.svg?shown=<token>`. The SVG endpoint
must compare before returning the picture; a change between HTML and object
GET produces a stale response/reload prompt, never a silently newer picture
attached to the older context. Parent code accepts only a same-origin SVG
whose board/token equals the page manifest; an error, object replacement,
reload or token mismatch clears selection and disables Send.

Use explicit DOM target attributes and gather distinct instance IDs from
the rendered hit stack. Multiple layers of one instance collapse to that
instance; overlapping distinct identities require an explicit component
choice or refusal. Do not silently pick the topmost unrelated component.
A parent component list provides keyboard focus/Enter/Space, visible
highlight, copy IDs and Escape/deselect, including unplaced or unowned-film
instances. The SVG highlights only explicitly mapped shapes. Escape all
refdes/labels/metadata at XML/HTML sinks; assign client text with textContent.
Layer toggles and schematic rendering keep their existing behavior.

### Snapshot and persistence contracts

PCB has no integrated saved-revision contract:
`handlers/pcb.py::_export_date` documents the absent revision and title-block
rendering deliberately omits one.
`design_revisions` exists generically but PCB does not record rows there.
A refs timestamp is not a sufficient version; many PCB child writes do not
advance it. `session.content_hash` is routing-job dedup, deliberately omitting
route/copper checkpoints, instance IDs, side and footprint identity; it cannot
prove which board the viewer showed. Do not repurpose or change that hash.

Use a versioned SHA256 projection of one raw relational capture: actual board,
instance/component IDs and live/retired status; pose/side/locks; component pins,
nets/connections/classes, features/generators/local footprints, authored and
derived routes/planes/swaps/copper; referenced footprint geometry/source and
identity used by rendering/validation. Include geometry-relevant catalogue
facts, not stock/price or unrelated catalogue entries. Preserve ID allocation;
do not call replay normalize for live context. Exclude capture timestamps,
latest-job parameters, DRC-run timestamps and export date, while retaining
retirement state. Token must change on remove/recreate with the same refdes,
footprint refresh, generator replacement, copper/rule/side/pose changes.

Factor capture/render snapshot consistency using the existing ambient PCB
connection. Read-only GET begins its isolation before any SELECT and performs
no ensure-board, checkpoint/history mint, catalogue fetch or job dispatch.
The consistent capture/token inventory travels with the displayed SVG.
Capture can reuse raw `snapshot.export_query` structure but must not treat
its current job/replay-normalization payload as a user revision.

Proposals remain existing conv message chunks: persist server-resolved context,
base token, canonical validated ops and rationale in chunk metadata/body.
Use a PCB-kind-qualified conversation slug to avoid cross-kind slug collisions,
with the same conv/transcript/proposal machinery and related-to link.
Require board/kind ownership when loading a turn; do not accept a foreign
conv handle. The current `_next_turn_handle` predicts an ordinal without
serialization: PCB proposal append must serialize ordinal allocation and
return the actual stored handle. Use the existing conv writer with a reviewed
serialization/context extension; do not create a second transcript store.
No chunk text updates.

On successful Apply, existing `save_checkpoint(conn=...)` captures the raw
after-snapshot, and `record_revision(conn=...)` stores canonical ops and turn
in the same board transaction. Preserve a base checkpoint for the first
chat Apply so the actual prior snapshot is inspectable; do not invent earlier
history. The recorded applied turn is the duplicate-consumption record.
Transcript outcomes render from those revision rows, as existing
`design_chat._applied_turns` does. PCB history initially shows a read-only
checkpoint/change summary; never substitute the live SVG for a past revision.
No selection/chat/Apply on a historical view. Full historical SVG/restore is
outside the first adapter.

### Request and supported-op contract

Proposed web requests, additive to existing routes:

- `POST /pcb/{slug}/chat`: `message` plus `context` JSON above. Validate
  board ownership, shown token and every selected instance/footprint before
  calling the shared engine; recheck after generation, including repair.
- `POST /pcb/{slug}/chat/apply`: `turn` (stored proposal handle) plus
  shown/base token. Load canonical ops/context from the stored proposal;
  client ops are never authoritative. Require current non-historical view,
  newest eligible proposal, matching design/selected target and no recorded
  consumption. Return 409 on stale/concurrent/duplicate; existing error/303
  conventions for other outcomes. Existing auth/check_same_origin applies.

The native PCB vocabulary is `put(kind='pcb', args={'op':...})`, not a
general `PcbHandler.edit(ops=...)`. Dispatch has place/route jobs and inline
move/rip/pin_side/plane_net/class_rules/stackup/footprint, plus datasheet jobs.
The web adapter must not advertise these all as safe component edits.

Recommend first proposal roster: exactly one selected instance's existing
`move{refdes,x?,y?,rot?,fixed?}` operation, with finite board-mm/degree values
and the existing lock vocabulary. No layer flip: current public _pose_kwargs
does not expose layer. Server replaces model refdes with the resolved target
only after confirming an exact match; foreign/additional targets reject the
whole proposal. No batch moves or fabricated rename/replacement op.

Existing move can carry generator members/fixed copper and rip routes.
Dry validation must therefore compute a before/after delta and refuse
a proposal that changes another instance, fixed copper, connections, routed
copper or board annotations. Initial geometric moves are limited to independent
components whose accepted move has no collateral delta; lock-only changes can
be proposed on routed boards. Explain coupled/routed cases as unsupported
rather than silently building a new group/route feature. Missing trusted
footprint geometry prevents a geometric proposal, without preventing identity
inspection or supported lock changes. Validation is not a route trial.

All PCB ops are proposal-only, including harmless locks. No geometry writes
from selection or model generation. Dry validation invokes the same restricted
existing handler under an ambient transaction/savepoint and ALWAYS rolls
back; it must never dispatch jobs or providers. Model repair uses that error,
then returns a visible valid/invalid/rejected proposal through shared code.

### Atomic stale/concurrent/duplicate guard — review decision

The current `pcb_judged_tx` shares connections and rolls back invalid edits,
but does not lock a displayed revision or serialize proposals. Merely checking
the hash before calling `_op_move`, or taking an advisory lock only in web
Apply, leaves races with existing MCP/job/cache writers.

Recommended initial guard in the PCB store adapter: inside one short transaction,
lock the board and proposing-conv refs in deterministic ref-ID order, then board
parents and all board-owned rows in a deterministic
dependency/ID order, including retired rows that could be revived. Lock component/
instance/net parents before pins/netconns/routes; existing FK parent checks
then prevent new scoped child inserts while the guard is held. Re-read the
canonical snapshot after acquiring locks; compare base token, live instance and
proposal ownership/latest-eligible status/consumption on that connection;
the conv parent lock also serializes a new proposal arriving during Apply.
Execute restricted move, verify delta, save checkpoint
and record revision before commit. Lock failure/deadlock/serialization conflict
rolls back and returns concurrent-edit refusal; never retry against a new state
or apply to a different instance.

Shared `parts`/`part_footprints` caches have no FK that protects absent keys.
The proposed initial correctness seam needs short SHARE table locks on those
geometry-dependency caches while capturing/validating/applying, or an explicitly
reviewed alternative that coordinates ALL cache writers. These locks block cache
writes, not reads; they are not held during model generation or GET. Review their
blast radius before implementation. A board-only advisory lock cannot silently
replace this missing-key protection. Confirm remaining raw capture dependencies/
FKs and all write paths in the implementation review; if any writer can evade
the chosen guard, Apply remains refused until that seam is resolved.

Proposal append/ordinal serialization must use the shared conv path and avoid
holding the board guard across separate-connection transcript/link writes.
Apply only needs the already stored proposal and atomic revision-consumption
record; the transcript's applied display derives from that record. Existing
generic Apply does not enforce these checks; add them for PCB without changing
SE/structure auto-apply or accepting arbitrary client-posted ops.

No migration is proposed. Coordinator must approve shared chat ownership and
the concrete board/cache guard before implementation; this map is the review
artefact, not a claim that a safe PCB Apply already exists.

### Meaningful validation plan after reviewed assignment

- `tests/precis_web/test_pcb.py`: fixture GET/object manifest agreement,
  identity/footprint labels, stale HTML-to-SVG request, no GET writes/model/jobs,
  historical summary read-only, auth/origin refusals, 303/error behavior.
- new `tests/test_pcb_design_chat.py`: injected model stub, two instances
  sharing one footprint, missing UUID/local cache, canonical proposal metadata,
  unknown ops/forged board or target, explicit-Apply-only, no collateral edits.
- `tests/test_design_turn.py` and `tests/precis_web/test_design_chat.py`:
  additive PCB branch plus unchanged SE/structure transcript/proposal/apply
  tests, correct captions, no ordinal collisions or cross-kind transcript reuse.
- `tests/test_pcb_gerber_view.py`: explicit flash/mask bindings, absent or
  ambiguous identity not guessed, XML escaping, layer toggle/mirror consistency,
  opt-in selection metadata leaves default renderer byte behavior intact.
- Fixture/dev DB concurrency tests with barriers and two connections/processes:
  board edit during model generation; remove/recreate same refdes; concurrent
  raw child insert/job copper write/cache insertion; simultaneous and replayed
  Apply; rollback-only dry run leaves all raw rows/checkpoints/revisions unchanged.
  Assert exactly one Apply and revision, no partial board change on refusal.
- Rendered fixture-browser check: pointer/keyboard/highlight/copy/deselect,
  overlapping different-instance hit refusal, hidden layers and replacement,
  latest visible token attached to Send, proposal visible then explicit Apply.
  Use existing harness/dependencies; no real/private board and no page-load model.
- After implementation only: canonical focused + full `scripts/test`,
  `scripts/test --typecheck`, host Ruff/format, changed-line coverage and
  scoped source review. No present test/coverage/browser/feature PASS claimed.

### Publication and review handoff

Distinct requirement `gr473212` was published by this PCB owner and native
readback matched the full request. All-status searches covered selection/chat,
footprint and click wording; directly read `gr458878` (pin provenance) and
`gr451278` (pad mouseover), both distinct and preserved. Initial open-only
query was followed by `status='*'` queries; no all-status claim is made for
the initial query. The returned related records did not contain this combined
instance/snapshot/proposal/Apply requirement.

Proposed internal APIs, names for review rather than existing capabilities:

- `pcb_design.capture_view(store, ref, conn=...) -> PcbView`: immutable raw
  snapshot, resolved instance inventory and versioned token; actual ref identity
  re-read on that connection, not assumed from an earlier independent lookup.
- `pcb_design.resolve_selection(view, context) -> PcbSelection`: validate
  echoed board/token/instances/footprints and return canonical server context.
- `PcbMixin.pcb_view_tx(read_only=True)` and
  `PcbMixin.pcb_guarded_tx(ref_id)`: consistent read scope and reviewed atomic
  board/cache guard, both reusing ambient PCB connection behavior.
- `pcb_design.dry_run(hub, ref, context, ops) -> Validation`: canonical restricted
  move plus before/after delta in rollback-only savepoint; no jobs/providers.
- `pcb_design.apply_proposal(hub, ref, turn, shown_token) -> TurnResult`:
  resolve stored proposal, compare/validate/consume/checkpoint in one guard.
- Shared `run_turn(..., context=None)` / transcript writer and
  `panel_context(..., context=None)` gain additive PCB branches; existing
  SE/structure callers and signatures remain compatible.
- Optional internal `ConversationHandler.put(..., conn=None)` extension must
  serialize creation/ordinal allocation and return the actual stored handle;
  transaction-aware ref resolution belongs in that reviewed shared path.
  Existing `Store.insert_ref`, `Store.add_link` and `chunks.insert_chunks`
  already support `conn=`; avoid a lock across their independent connections.

Next is coordinator review of this exact source map, proposed file ownership,
initial move restrictions, snapshot/history shape and DB cache-lock choice.
No source implementation, bump, ship, deploy, runtime probe, real-board mutation,
routing-lever repetition or residual repair build is authorized by this map.


---

# Coordinator review: PCB selection and design chat

Reviewed owner commit `3f69a9614f582d30a05189c0807444aee6a7f9e5`, spec
SHA256 `bf4a693294ade6add7353269f9482979b9177c40a870f0b8fb151edaceaf4b0c`.
Decision: map accepted with the amendments below; bounded implementation
assigned to PCB window6. This is architecture/source-map review, not a
source, concurrency, browser, feature or deployment PASS.

## Ownership and sequence

PCB owns the mapped PCB route/template/client, opt-in Gerber identity,
PCB adapter/store/snapshot changes and their tests. PCB also owns the
additive shared `precis_web/design_turn.py`, `design_chat.py`,
`templates/_design_chat.html.j2` changes, the shared conversation
append/connection plumbing in `handlers/conversation.py` and narrowly
needed store helpers.
PCB owns its PCB/web/store rationale paragraphs and may update the
conversation module rationale for transaction semantics. Do not rewrite
unrelated paragraphs or change existing SE/structure contracts. Window25
retains blocktree/structure routes, templates, clients and tests; Hex retains
relaxation derivation/handler contracts. No migration, upstream dependency,
new chat engine, generic all-writer refactor or global API redesign.

First adopt these amendments in the owner spec, then deliver the already
assigned classic Nano reference/capability/BOM/netlist and finite execution
plan before the larger viewer implementation. That independent plan does
not wait for this feature to ship. Implementation is authorized after this
adoption without another permission question; fixtures/dev DB only.
Version and ship slot remain unassigned behind the existing queue.

## Accepted product contract

Keep exact instance/refdes distinct from actual library footprint UUID or
board-local footprint key; unknown stays unavailable. Same shown snapshot
must bind HTML, SVG, selection, message and Apply. Raw canonical projection
must include every renderer/validator dependency, preserve actual IDs and
reject cross-owner component/instance/net/board relationships. Keep replay
normalization and routing-job hashes unchanged. GET is read-only and
repeatably consistent. A historical checkpoint is a read-only summary,
never a live SVG presented as historical geometry.

The first roster is exactly one selected instance's `move` with finite
x/y/rot and existing fixed vocabulary, at least one supported change,
strict keys/types and exact target match. Do not rewrite a foreign target
into the selected one. No batch, side flip, generated-group collateral,
router/copper rip, connectivity or annotation delta. Use the existing handler
under rollback-only validation; reject unsupported cases. All changes require
explicit Apply of the canonical stored proposal. No client-supplied ops,
auto-apply, model/provider/job call on read, or production-board mutation.

## Required transaction amendments

1. Conversation append must be one shared connection-aware operation:
   serialize missing-conversation creation by a stable namespaced slug lock
   before resolution, lock the existing conv ref, deduplicate msg_id, allocate
   MAX(body ordinal)+1, insert and return actual stored ordinal/chunk identity.
   Use a typed internal result, not parsing the human Response body. Existing
   put response meaning/shape and SE/structure slugs/handles stay compatible.
   Every normal ConversationHandler.put uses the shared allocation seam.
   Do not claim this fixes the separate legacy SE auto-apply/transcript race.
2. get_ref and list_chunks_for_ref currently open independent connections.
   Add narrowly scoped conn-aware reads/helper APIs where needed: all ownership,
   latest-proposal, duplicate-consumption and append checks must observe the
   guarded connection. No pooled second connection while holding its locks.
3. Apply/dry-validation use READ COMMITTED, not an earlier repeatable snapshot.
   Acquire cache locks first, then board/conv refs in numeric order, canonical
   cite-key bindings, board/components/nets, instances/pins, and all captured
   leaves/direct-owner rows in stable table/key order, including retired rows.
   Use FOR UPDATE, not FOR NO KEY UPDATE. Re-capture only after all locks.
   Recheck numeric board ownership and locked slug bindings. If a mutable
   related-to link is relied upon for ownership, lock/recheck that row too.
   Shared append and Apply must use a documented compatible lock order.
4. Short SHARE NOWAIT locks on parts then part_footprints are approved only
   inside guarded dry-validation/Apply. They protect absent cache keys and
   DROP/rename refresh as well as updates, but block cache writes globally.
   Never hold them during model generation, GET or network calls. Use local
   lock/statement timeouts plus an application deadline and record concrete
   finite values and measured lock residence in the implementation evidence.
   If the bounded handler cannot meet that envelope, refuse/defer the move;
   do not silently widen duration or replace this with a board-only advisory lock.
5. Existing writers have other lock orders. Timeout, deadlock, serialization
   conflict or changed snapshot means full rollback and explicit refusal, no
   retry against a new state. Capture/checkpoint/revision/turn consumption and
   the accepted move commit together. Dry-run leaves all rows/history unchanged.

## Required evidence before source acceptance

Keep the owner's planned browser, origin/auth, fixture and full gates. Add
two-connection barriers for a child committing while the guard waits, slug
deletion/rebinding, missing cache insertion and catalogue replacement, existing
row mutation, first-conversation creation/ordinal races, proposal arriving
during Apply, and simultaneous duplicate Apply. Verify cross-owner dependency
refusal, unrelated cached reads continuing, bounded cache-write contention,
and rollback without partial history or transcript corruption. Passing mocks
alone do not establish database concurrency. Freeze reviewed source fingerprints
before final gates. Preserve all failed/interrupted receipts and private inputs
outside the shipping payload. No source readiness claim before exact review
and terminal passing gates.


## Implementation envelope for source review

The assigned adapter keeps the original map and appended coordinator ruling
above as its review history. The additive implementation uses READ COMMITTED
Apply/dry-validation with parts then part_footprints SHARE NOWAIT, numeric ref
locks, locked canonical cite-key bindings and parent/leaf FOR UPDATE including
retired rows. Same-connection captures happen after lock acquisition. The shared
append seam locks a namespaced slug before absent-conversation resolution and
returns the actual stored chunk/ordinal; SE/structure handles remain unchanged.

Finite guard values: lock timeout500ms, statement1500ms, idle transaction2000ms,
application residence deadline2s checked before commit. Python CPU is not forcibly
preempted by that application check; the database idle timeout releases locks on
an overrun, and every overrun refuses rather than committing or widening bounds.
Geometry moves beyond128instances/2048pin rows refuse. Existing-handler validation
runs in rollback-only dry savepoints. Any generator/copper/group/annotation delta
outside the one selected instance's x/y/rot/fixed fields refuses. No-op state
changes refuse. Trusted footprint geometry must be present for geometry moves.
The fixture contention test records a measured upper bound on cache-lock residence
and a100ms bounded absent-cache write refusal while unrelated reads continue.

Initial implementation evidence:149 focused canonical tests passed under the
repository --cov=src mode. Earlier focused failures and the module-targeted
coverage NumPy import failure are retained outside the shipping payload. The
container type check passed2582files before the final added regressions; repeat
it on the frozen candidate. Rendered Chromium fixture rehearsal passed9 pointer,
keyboard/copy/deselect/overlap/hidden-layer/stale/Send/explicit-Apply checks, exact
HTML/SVG/client hashes retained. The stale-root observer loop's interrupted143
receipt is preserved. These are local fixture checks, not deployed feature proof.
Final full suite, exact candidate type/format/diff coverage and source review remain
required. Version/ship slot unassigned; no bump, ship, deployment or real-board
mutation. Independent classic Nano plan delivered separately as td473395 at
local d3f4493d6; its digital execution/provenance review is separate.

Reader compatibility: a bare PCB ref has a read-only empty page and SVG, no
physical board creation and no selectable instances. Selection refuses before
model generation. The page title comes from the same captured read transaction
as its manifest. The full-suite reader-totality regression and focused fixture
checks verify this contract; unsupported multiple/retired physical boards still
refuse rather than offering selection.


## Coordinator namespace correction

The exact352aab138 review required correction: PCB `foo` and legacy
SE/structure `pcb-foo` both resolved to `design-chat-pcb-foo`. PCB now uses
`pcb-design-chat-<slug>`, structurally disjoint from the preserved legacy
`design-chat-<slug>` namespace. This unreleased adapter needs no migration.
Typed panel reads hide foreign or retired conversations. Generation refuses
foreign ownership before model or automatic edits. Append verifies the live
numeric parent and metadata on the serialized append connection, before chunk
or parent-link insertion. Apply refuses foreign conversation handles. Existing
linked, metadata-free legacy transcripts remain readable and appendable;
generic conversation puts retain their policy. Legacy edit-before-transcript
atomicity remains outside this bounded correction.

Fixture regressions cover both creation orders for SE and structure, separate
visibility/pending proposals/appends and real PCB Apply, old colliding PCB
transcripts refusing legacy read/generation/append/Apply without mutation,
unrelated PCB conversations, and metadata-free linked legacy compatibility.
The first correction gate passed92 tests and container typecheck2582 files,
both exit0. Initial fixture-format/type failures are retained. Added legacy
compatibility regressions require refreshed exact gates and candidate manifest;
prior28/352 manifests and browser inputs remain historical evidence. Full gate,
diff coverage and coordinator source acceptance remain pending. No version,
ship, release, deployment or board execution authority follows these tests.
