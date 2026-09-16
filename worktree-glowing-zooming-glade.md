# Continuation — `worktree-glowing-zooming-glade`

Handoff for resuming this thread cold. Pointers only: the durable artifacts
are the backlog specs, the gripes, and `git log` — not this file. Delete it
when the work below is done.

Session span: 2026-09-14 → 2026-09-16.

## Landed this session

| sha | what |
|---|---|
| `5093cd79` | viewer embed fix (shell→viewport sizing, margin-blind overflow check, `treeHeight` cap) + 3D scale bar; deployed |
| `0e15f509` | gr244679 — title-match row renders the `pa` handle + `authors (year). title` |
| `465306d4` | **dispatch: a handler's dead network dependency no longer kills MCP boot** (`OSError` added to `_try`'s catch) + 3 regression tests; deployed cluster-wide, 6/6 hosts |
| `222633ae` | `pcb-argue-with-design.md` spec (vetted, `status: ready`) |
| `87c2b104` | `pcb-pre-place-route-blocks.md` spec (**not** vetted) |

## Immediate: gripe bookkeeping (blocked only on an MCP connection)

The MCP dropped before these could be written. Each is one call:

- **`gr341576` — CLOSE.** Fixed by `465306d4` (the `OSError` boot kill).
- **`gr339236`** — re-point at `docs/backlog/pcb-pre-place-route-blocks.md`;
  it is absorbed there (router can't see the plaza via → fabric becomes
  real copper → the escape routes).
- **`gr341516`** — note the re-sequencing: it is now a **prerequisite** of
  the fabric round, not a follow-on. Reason in the spec's §Sequencing.
- **`gr341515`** — MCP watchdog observability (exit reason + old→new sha in
  `precis-status`, distinguishing watchdog-exit from boot-crash). Still open;
  half the motivation is gone now that boot survives a dead dependency.
- **`gr341532`, `gr341578`** — unchanged, both prerequisites below.

## The pcb thread — ruled, specced, not built

Reto's ruling (2026-09-15) on whether the generator should materialize plaza
vias: *"Yes, fixed copper (ie vias and wires to the pads) seem right. There's
a tight pattern we need to do that can be optimized, and it should be …
there automatically, with all the spacing."*

Specced in **`docs/backlog/pcb-pre-place-route-blocks.md`**, which fills the
round `pcb-ewod-multitile.md` §Cross-cutting reserved on 2026-09-13.

**Build order (corrected — do not use the earlier one):**

1. `gr341532` — real footprints for C639448 (HV507) / C32254, plus a loud
   signal when pad geometry is synthesized. The B.Cu fan must land on real
   pins; today `part_footprints` is empty, `apply_real_pin_offsets` no-ops,
   and `landpattern.offsets_for` falls back to `_quad(59, 0.65mm)` — a
   phantom perimeter. **No MCP verb exists for `store.part_footprint_put`**,
   only `ensure_footprint`'s network pull.
2. `gr341516` — layer-aware clearance/courtyard (`rules.py::PAD_LAYER` forces
   layer 0). Prerequisite: emitting real 4-layer copper under a layer-blind
   check re-creates the 65-false-positive flood on *real* copper.
3. Fabric **slice 1** — storage seam only, no behaviour change:
   `pcb_fixed_copper` (authored) → `GeneratorExpansion.copper` → `_pcb_apply`
   scoped by generator identity → realize seeds `pcb_copper` from it.
4. Fabric **slice 2** — `ewod_pad_array` emits stubs + plaza vias as real
   `track`/`via` rows and the per-tile B.Cu fan.
5. **9×9** — needs no new lattice rule (see below).
6. `gr341578` — `pads_for_ir` drops paste/mask/role/drill (`F_Paste` came out
   byte-identical to `F_Cu`). Independent; land any time before fab review.

**Two corrections that were made mid-session** — the earlier statements are
wrong and are repeated in older notes:

- **9×9 is not blocked.** `r % 3 == 1 and c % 3 == 1`
  (`generators.py:559-567`) puts plazas at {1,4,7} — exactly three interior
  tile centres per axis on a 9×9: nine plazas, 72 driven electrodes, no
  truncation. 9×9 is the spec's own canonical target
  (`pcb-ewod-multitile.md:34-41`); the **8×8 dogfood is the awkward fit**.
- **`gr341516` moved from last to prerequisite** (reason in step 2 above).

**Open question the spec does not settle** — decide before slice 1 locks the
format: is the fabric **per generator-instance** (simpler, matches
`_pcb_apply`'s existing `canonical_params` identity diffing) or
**footprint-scoped and inherited** (fewer rows, which starts to matter at
9×9 across multiple cards)?

**The spec is unvetted.** Written directly, not through the `ready` agent.
Three claims want a read against current code first: the storage shape
(`pcb_fixed_copper` vs extending the `pcb_planes` pattern), the migration
number, and the realize-seam assumption.

## Also ready to build

**`docs/backlog/pcb-argue-with-design.md`** — `status: ready`, vetted. One
text box on `/pcb/{slug}` that accumulates `data-handle` anchors as you click
pads/parts/nets, stored verbatim as `pcb_notes`. Load-bearing detail: the
board is an `<object type="image/svg+xml">` embed, so a host-page listener
never fires — reach in via `contentDocument` (same-origin). Claims core
migration **0163**. Explicit non-goals: no place/route button, no AI rewrite
of the note. Slice 2 (`pcb_argue` job) additionally needs
`can_own_jobs=True` on `PcbHandler`, a params schema, an executor lane, and
a **prod `service_config` row** — that last gap has shipped a job dark twice.

Its se back-port: `docs/backlog/pcb-argue-backport-se.md` (blocked on the
above).

## Surfaced but nowhere specced

**EWOD design rules are not expressible as `measures`.** The pcb intent
surface is the `measures` array (metric/operands/goal/strength/reason,
evaluated by `view='measures'`), but only `separation`/`proximity`/`height`
evaluate today — the five electrical metrics report `pending`. None of the
rules that matter here — one via per electrode, no via-in-pad, a merged pad
may never cover a plaza, HV separation sets the pitch floor, matched stub
lengths for uniform capacitive load — can be stated as a measure. They live
in prose and in generator code, with nothing evaluating them. Note also that
a `hard` measure **steers and does not gate**: the placer prices it ~40×
soft and proceeds, so a violated hard measure never reddens anything. A new
metric class would be a real spec round; it does not exist.

## Standing items (older, unchanged)

- `docs/backlog/pose-vector-units.md` — `status: ready`, unblocked. Open
  sub-decision: sticky `meta.prefunits` on the design (recommendation: ship
  it in-round).
- `pcb-se-binding.md` (unblocked by the nm→se merge), `pcb-flexboard.md`,
  `pcb-se-negotiation.md`.
- EWOD **pre-place-route block spec round** — now written; slice 3 of
  `pcb-ewod-multitile.md` waits on it.
- `gr339251` (agent-usable mypy); YouTube upload + Pages toggle (user-side);
  melchior colima registry-pin retirement.
- `retire-claude-context` — decision due **2026-09-28** (extended twice).
- GitHub reports **12 Dependabot vulnerabilities** on the default branch (10
  high). Untriaged; not looked at.

## Traps worth carrying

- **The session `precis` MCP targets PROD** (`agent_rw`). Write-path testing
  goes on the dev DB (`scripts/dev`), never the session MCP. Ad-hoc SQL via
  `scripts/prod-psql`; `refs`' PK column is `ref_id`, not `id`.
- A **deploy reinstalls `/opt/mcps/venv`**, the install watchdog exits, and
  the MCP disconnects mid-session. Expected. Reconnect with `/mcp`.
- **Deploys are ~14 min**, not hour-scale. One sibling run took 1h11m purely
  because `docker build --target agent` async-failed once and retried cold
  (the gr335099 class); the same build took 107s on the next deploy.
- Never pipe `scripts/ship|deploy|bump` into `tail`/`grep`/`tee` — the
  pipeline reports the filter's status, so a red gate reads as exit 0.
- `view='gerber'` **correctly refuses** `ewod-dogfood-1` ("61 net(s) carry
  synthesized pad geometry"). That refusal is the thing keeping an unfabbable
  board from escaping — it should stop firing because the geometry became
  real, never because the check was relaxed.
