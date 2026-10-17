---
status: in-progress
title: Test board — USB-C PD Arduino Nano (5V) with switched high-side power outputs
prio: normal
model: opus
pillar: 3d-design
---

Status 2026-10-17: fixture + baseline on main (ef454a295); slice 2 is being built.

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
- **Switched high-side outputs** (resolved, slice 2): a P-FET per channel
  (source on VBUS, drain on OUTn) held off by a 10 k gate-source resistor
  with a 12 V zener clamp, switched on by an N-FET level shifter on Nano
  D4..D7 through a 1 k series resistor (|Vsg| ~4.5 V at a 5 V VBUS, 12 V
  clamped at 20 V). A 100 k pulldown on the
  level-shifter gate keeps each channel off while the MCU pin floats. The
  20 V / 5 A envelope and thermal copper are kept; the flyback footprint
  stays (decide once loads are known — inductive loads need it). A P-FET
  sink power switch, driven by STUSB4500 VBUS_EN_SNK, separates the
  connector side (VBUS_RAW) from the switched VBUS. No P-FET / N-FET MPN is
  selected (see §Authored state).
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

Two boards. The original (`nano_usb_pd_testboard.py`, exact pins in
`test_pcb_nano_usb_pd_testboard.py`) stays unchanged as the **router
regression fixture**: its floors guard the router, not the design.
**r2** (`nano_usb_pd_testboard_r2.py`, `test_pcb_nano_usb_pd_testboard_r2.py`)
is the reviewed design (slice 2) with its own one-way floors, measured
2026-10-17 (seed 1, default 4-layer stackup): **35/72 nets routed, 37
failed**; DRC **106 errors** (`connectivity` 10, `unrouted` 37,
`silk_missing` 54, `outline_containment` 2, `silk_edge_clearance` 3) and
**15 warnings** (`clearance` 11, `board_edge_clearance` 3,
`silk_edge_clearance` 1). The floors may only improve. They are the honest
tuple, not a claim of cleanliness — §Blockers item 3's residue stands.

Parts discipline (r2; the original fixture additionally keeps its
provisional MIC5019 C621942 / YJD40N04A C919581 high-side stand-ins):
real LCSC C-numbers only where the fixture ships a
cached `part_footprints` row (C165948 receptacle, C2678061 STUSB4500QTR,
C14877 ATMEGA328P-AU, C44377 TPS54360 bucks — hand-authored stand-in
geometry shaped like the real package, not datasheet land patterns);
design-local `footprints[]` for headers/terminals/inductors/electrolytic/
crystal/diodes/test points and the part-agnostic DPAK/SOT-23 FET lands;
`landpattern` synthesized bounds for the small 2-pin passives and LEDs,
flagged in each part's `note` and `meta.synthesized_footprint_refdes`.

**C-number ↔ MPN mapping is UNVERIFIED.** Those four C-numbers were bound
to hand-authored stand-in footprints seeded into the test DB
(`part_footprints` rows inside the fixture, not fetched); confirm at
minimum C44377 = TPS54360 against the catalog before any order. Because the
parts look cached, the `synthesized_footprint` DRC rule does not fire for
them: their geometry is a stand-in that *reads* as real.

**Netlist review (slice 2, done in r2).** The high-side topology is
resolved, the sink power switch (VBUS_EN_SNK → Q_SNK) is wired, and the
D11..D13 sharing is gone (channel enables D4..D7, error LEDs D8/D9). Gate
drive: a 1 k series / 10 k pull-up pair gives ~4.5 V |Vsg| at a 5 V VBUS
(ERR_SOURCE case; ~3.9 V for Q_SNK at vSafe5V, whose gate sits a D_SNK
drop above the pulled-low pin) and a 12 V zener per gate clamps |Vsg|
at 20 V (a plain 10 k/10 k divider gave only 2.5 V at 5 V, which does not
reliably enhance a 40 V P-FET). VBUS_EN_SNK sits behind a blocking diode
(D_SNK) because its absolute-maximum rating could not be confirmed
(datasheet text was not reachable; ST's summaries cite 22 V on CC and 28 V
on VBUS pins only, not this pin). Left, as intake items: pick and verify
the P-FET, the level-shift N-FET and their LCSC numbers (the fixture
carries `meta.provisional` and a `TODO(netlist-review)` on each GATE net
until then); read DS12499's absolute-maximum table for VBUS_EN_SNK (voltage, and
the ~7 mA it sinks through D_SNK when Q_SNK's zener conducts at 20 V) and
its drive levels, then keep or drop D_SNK; Reto decides inductive loads
(flyback) and which rails are stuffed.
