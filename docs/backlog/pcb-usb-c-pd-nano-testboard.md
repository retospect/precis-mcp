---
status: ready
title: Test board — USB-C PD Arduino Nano (5V) with switched high-side power outputs
prio: normal
model: opus
pillar: 3d-design
---

# Test board — USB-C PD Arduino Nano (5V) with switched power outputs

The first synthetic test board for the guided place+route system, not one
of Reto's stored real boards. Ready for controlled dev-DB authoring after
the design corrections below, with the remaining clearance residue recorded
in §Blockers. This readiness pass is a source check, not a built/routed
board, a clean DRC receipt, or a manufacturing approval.

## Intent

A 5V board with Nano-compatible headers that sinks USB-C Power Delivery,
exposes the negotiated/derived rails, and has switched high-side outputs,
with LED indication of rail presence and negotiation failure. The brief's
high-side requirement supersedes the original low-side sketch; switch and
driver topology must be resolved during netlist/part review, not inferred
from that sketch.

**Outline ruling (Reto, 2026-10-06 21:35Z):** make the board as big as it
needs to be. A Nano-sized 18 × 45 mm outline is not a constraint. Size it
for actual power-net widths/clearances and clean routing; the authoring
agent records the chosen width/height in mm and the placement/routing
reason here.

**Authored (2026-10-13, fixture landed):** outline **100 × 80 mm**, in
`tests/fixtures/pcb/nano_usb_pd_testboard.py`. Rationale: four switched
20 V / 5 A channels plus the VBUS trunk resolve to ~2.8 mm external
tracks under IPC-2221 at 5 A, three buck stages need inductor +
input/output-cap area, and the two Nano headers pin a fixed 15.24 mm row
spacing; sized for the power netlist, generous rather than tight —
shrink/grow is the anneal item's priced move, not this fixture's. The
fixture is a test-DB put payload; the measured baseline lives in
`tests/test_pcb_nano_usb_pd_testboard.py`.

## Design corrections to fold in before drawing anything

Three things in the original sketch do not survive contact with the USB PD
spec. They change the topology, so they are recorded here rather than
discovered mid-layout.

**12 V is not a standard PD fixed PDO.** The normative fixed voltages are
**5 V, 9 V, 15 V, 20 V**. 12 V is an *optional* PDO — some sources offer
it, many do not, and none are required to. If 12 V is genuinely wanted,
either derive it from a higher rail with a buck, or request it via **PPS**
(programmable, ~3.3–21 V in 20 mV steps) and accept that PPS-capable
sources are a subset. Do not design assuming a 12 V contract is available.

**One PD contract yields one VBUS voltage at a time.** A sink cannot hold
20 V, 12 V and 5 V simultaneously from a single negotiation. The board must
either negotiate the highest rail (20 V) and **derive the lower rails with
bucks**, or switch contracts sequentially and give up simultaneity. The
buck approach is assumed below.

**5 A requires an e-marked cable, and telling "cable can't" from "source
can't" is subtler than it looks.** Full e-marker interrogation needs SOP'
VDM communication (an FUSB302-class PHY plus a PD stack) — which does not
fit comfortably on an ATmega328P. There is a cheaper discrimination that
does work: a source only advertises 5 A PDOs when an e-marked cable is
attached, so the sink can read the *advertised source capabilities* and
infer:

| Advertised | Inference |
|---|---|
| 20 V/5 A present | cable + source both fine |
| 20 V/3 A but no 5 A | cable is the limit (not e-marked) |
| no 20 V at all | source is the limit |

That is enough to drive two distinct error LEDs with an autonomous sink
controller, and avoids a full PD stack.

## Topology

- **USB-C receptacle** + CC handling. Sink controller candidates:
  **STUSB4500** (autonomous, I²C status + PDO readback — preferred, because
  the error-LED requirement needs the negotiation result) or **CH224K**
  (cheapest, minimal feedback) or **FUSB302** (full stack, only if e-marker
  interrogation is later wanted). Chosen for readback: STUSB4500.
- **Negotiate 20 V.** VBUS feeds a wide-Vin buck chain. Rails follow the
  PD fixed set rather than the original 5/12/20 sketch:
  - **20 V** = VBUS direct (post-negotiation)
  - **15 V** = buck from VBUS
  - **9 V** = buck from VBUS
  - **5 V** = buck from VBUS; this rail also powers the ATmega328P. The MCU
    cannot run from vSafe5V once VBUS moves to 20 V, so the 5 V buck is
    load-bearing, not a convenience.

  Four rails means three bucks, which is a real area cost (see §Open
  questions). Populating a subset — say 20 V and 5 V only — is a sane first
  build; the footprints can stay on the board unstuffed.
- **Switched high-side outputs** (current brief): resolve the device/driver
  circuit at netlist review. The older logic-level N-channel low-side,
  direct gate-pulldown sketch is not a validated high-side design. Preserve
  the intended 20 V / 5 A output envelope, off-during-reset requirement and
  thermal-copper requirement when selecting real footprints; do not imply
  a package or direct MCU gate drive is proven. Decide flyback protection
  once loads are known — inductive loads need it.
- **LEDs**: one per rail actually present (5 V, 9 V, 15 V, 20 V), plus
  two error indicators driven off the PDO-inspection table above
  ("cable not 5 A-capable", "source lacks requested PDO").

## Open questions

- Are the outputs driving inductive loads? Decides flyback diodes.
- How many of the four rails get stuffed on the first build? Three bucks is
  a functional/population choice; board area may grow and is not a Nano
  outline constraint.
- If 12 V specifically is still wanted (it is not a negotiable PDO), it has
  to come from a buck like the others, or from PPS on a source that offers
  it.
- Outline size is decided by the routing/clearance needs, not header
  compatibility. Record chosen dimensions and rationale during authoring.

## Blockers — current source readiness and known residue

Checked against owning task HEAD
`1ffc99dfe2ae292ab0cb8cfc564ef619c88d2917`, whose code base is verified main
`1c412f327584a2e81df0c81d4a5bd9f07022dc07`. Native Python search/symbol get
was tried first; its served/indexed `/src` root differs from this isolated
task tree, so targeted local source excerpts verified the anchors below.
No tests, provider requests, placement or routing were run for this pass.

1. **Closed — via geometry is realized and emitted.**
   `src/precis/pcb/realize.py::RealizeResult` includes `vias`;
   `result_copper_rows` emits `ctype='via'` with centre, diameter, drill and
   layer span. “No via copper” is a stale blocker, not a reason to delay
   dev-DB authoring. Actual board via connectivity/DRC still needs to be
   tested when a board exists.
2. **Closed — track width is resolved per net.**
   `src/precis/pcb/realize.py::_resolve_track_rules` calls
   `src/precis/pcb/rules.py::resolve_net_rules` with class overrides/current;
   the realizer uses `rules.track_width_mm` and `result_copper_rows` emits
   each track's width. The historical flat 0.25 mm claim is stale. Author
   the actual rail currents and rules; this source check does not prove
   a chosen power rail or footprint can carry its current.
3. **Open residue — shared router clearance and WARN tier.**
   `src/precis/pcb/realize.py::_realize_maze` uses one clearance: the maximum
   of config and resolved net clearances. Net-class rules do have consumers
   in realization, cost and DRC; the “no consumer” claim is stale.
   `src/precis/pcb/drc.py::check_clearance` resolves the stricter pairwise
   requirement; `_two_tier` makes below-fab-floor violations errors and
   above-floor class/house shortfalls warnings. Report both errors and
   warnings, retain per-net rule evidence, and either route cleanly or
   explain this residue explicitly. Do not call zero errors alone complete
   satisfaction of authored requirements. Existing tracking:
   `ewod-controller-and-hv-supply.md`, ruling 14 / Slice 4.
4. **Out of scope — JLC ordering.** Design, dev-DB route and dogfood preview
   do not require an ordering/console scope grant. No ordering,
   manufacture or provider workflow is authorised by readiness.

Items 1/2 are closed source premises, not new measured board results.
Item 3 is an explicit limitation of the controlled dogfood build, not an
instruction to relax rules or reopen unrelated routing implementation.

## Not blockers, but worth knowing

- There is **no ERC** — the netlist is authored directly at L0, so a wrong
  net is a wrong board with nothing to catch it. Review the netlist by hand.
- Via geometry reaches the copper/DRC path. Future board acceptance must
  still inspect actual via findings/connectivity rather than infer success
  from this readiness check.


## Router failure feedback — Reto correction, 2026-10-06 21:58Z

The anneal, not an LLM, corrects placement after routing failure. This was
built as `anneal_route_loop` in `src/precis/pcb/optimize.py` (priced
outline/layer moves within authored caps, bounded router-evidence feedback,
and an end-of-loop constraint-conflict digest with marginal prices — the
spec file `pcb-anneal-closes-router-loop.md` keeps the open evidence items).
This supersedes the brief's LLM placement-advice step. Router defects still
go to gripes with evidence, but the user-facing output is the conflict
digest, not instructions for an LLM to move parts.


## Readiness handoff — approved docs fold, 2026-10-07

Reto approved folding the Nano readiness and anneal-feedback docs onto main.
The approved distance → rotation → lane-template sequence completed on
snapshot replay: default distance landed3419720b6; rotated poses collide
and the legal short lane pilot regresses51→50/55. Retain the51/55
baseline. The local repair draft was withdrawn 2026-10-12 (Reto: no
hand-routing; pin swaps, placement and outline are free; td472840 comment 1).
This supersedes the fleet wind-down's A/C ownership hold; those levers use
snapshot replay only and never Reto's real boards. The anneal-feedback loop
above remains a separate specification, not implemented by this docs fold.

The future Nano author applies the fixed PDO set5/9/15/20V and one-contract/
derived-rail corrections, resolves the high-side topology and real parts/netlist,
then authors on the dev DB, records outline dimensions/rationale and complete
DRC/routing limits. No Nano board, chosen outline or parts were created here.
`status: ready` describes controlled design work; it does not certify hardware,
routing, PD compliance, pricing or manufacture. Reto retains branch deletion.

## Authored state — 2026-10-13 (fixture + pinned baseline)

Authored as a repo fixture: `tests/fixtures/pcb/nano_usb_pd_testboard.py`
builds the `put` payload; `tests/test_pcb_nano_usb_pd_testboard.py`
applies it to the test DB, places and routes it with the shipped pipeline
(`op='place'`/`op='route'`, seed 1, default iters), and pins the DRC and
route counts as the baseline the anneal item measures against.

Measured baseline (seed 1, default 4-layer stackup — F.Cu/B.Cu routable,
In1/In2 planes): **38/64 nets routed, 26 failed** (congestion-dominated,
some `same-layer-crossing`/`width` reasons). DRC: **89 errors**
(`connectivity` 26, `unrouted` 26 — both tiers count the same failed
nets — `silk_missing` 35, `outline_containment` 1, `silk_edge_clearance`
1) and **15 warnings** (`clearance` 10 at 0.100 mm vs 0.150 mm required,
`board_edge_clearance` 2, `silk_edge_clearance` 3). The pin is the honest
tuple, not a claim of cleanliness — §Blockers item 3's residue stands.

Parts discipline: real LCSC C-numbers only where the fixture ships a
cached `part_footprints` row (C165948 receptacle, C2678061 STUSB4500QTR,
C14877 ATMEGA328P-AU, C44377 TPS54360 bucks, C919581 N-FETs, C621942
high-side drivers — all hand-authored stand-in geometry shaped like the
real package, not datasheet land patterns); design-local `footprints[]`
for headers/terminals/inductors/electrolytic/crystal/diodes/test points;
`landpattern` synthesized bounds for the small 2-pin passives and LEDs,
flagged in each part's `note` and `meta.synthesized_footprint_refdes`.
The MIC5019 + N-FET high-side choice is marked PROVISIONAL in
`meta.provisional` and the GATE*/OUT* net notes carry the netlist-review
TODO; nothing here validates that topology.

**C-number ↔ MPN mapping is UNVERIFIED.** Those six C-numbers were bound
to hand-authored stand-in footprints seeded into the test DB
(`part_footprints` rows inside the fixture, not fetched); the mapping was
never confirmed against the catalog — confirm, at minimum,
C44377 = TPS54360 and C919581 = YJD40N04A before any order. Because the
parts look cached, the `synthesized_footprint` DRC rule does not fire for
them: their geometry is a stand-in that *reads* as real.

**Netlist-review items now carried on the nets** (each marked
`TODO(netlist-review)` in the fixture, consistent with the GATE*/OUT*
tags): there is **no sink power switch** — STUSB4500's VBUS_EN_SNK is
unwired and the bucks, output-FET drains and the 20 V rail LED sit
directly on VBUS, so they run unregulated from the vSafe5V pre-contract
stage; and the ERR_LED resistors share D13/D12 with the ISP's SCK/MISO
while D11 is both MOSI and the EN4 channel drive.
