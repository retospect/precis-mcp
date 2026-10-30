# pcb design intent — author it, don't re-infer it

Status: backlog entry (Reto 2026-10-30). Related: pcb-tolerance-selection.md
(the constraint checker this intent feeds), pcb-unified-place-route.md.

## The point

The agent authoring a board script KNOWS what it is building — an RC
low-pass here, a Chebyshev anti-alias there, a 78 %-of-Voc MPPT divider.
Today that knowledge is written as prose comments (see scratch/taz-leaf/
board.py: "MPPT 78 % of Voc: ROC1 15.8M…") and then THROWN AWAY: the
netlist keeps only connectivity, and any downstream analysis (tolerance,
substitution, review) has to re-infer intent from topology. Don't.
Intent is declared once, at authoring time, and rides the design.

## Shape: a template/circuit library

Reusable circuit idioms as first-class objects, each bundling:

- **parameters** (with defaults and units): e.g. RC low-pass {fc},
  Chebyshev {fc, order, ripple, topology}, divider {ratio, absolute bound};
- **formulas**: the closed-form relations from params to component values
  AND from component values (with tolerances) to the derived properties
  (fc actual, attenuation at fstop, ratio corners) — the tolerance corner
  engine (pcb-tolerance-selection) consumes these directly;
- **netlist fragment**: the module's components + connections, emitted
  into the design at place-call time (a board-script call expands it);
- **properties/assertions**: what must hold ("drops ≥ 40 dB by 1 kHz",
  "VBAT_OV < 3.6 V with ref ±2 %") — checked by the corner engine and
  surfaced as persistent DRC findings, not one-off analysis.

Distribution: a skill + a code library both — the skill documents the
idioms and when to pick them; the code module (`precis.pcb.circuits`?)
implements param→netlist expansion and the formula registry. Templates
come from a small vetted set (RC/RLC filters, dividers, Wheatstone,
bandgap-ish references…) growing on demand.

## Authoring surface (board script)

```python
f = b.circuit("rc_lowpass", fc=160, name="sense_lpf")
f.R.bind("R30", net_in=..., net_out=...)
# or fully expanded with authored values; either way the design now
# carries: template=id, params={fc:160}, properties=[...]
```

The template id + params land on the design (component/net meta), so a
later reader (human or agent) sees "this is an RC low-pass, fc=160 Hz"
without reverse-engineering R·C from the netlist.

## Why not auto-detect instead

Divider/filter topologies ARE netlist-detectable, and detection is a fine
fallback for legacy boards — but inference is lossy (is that 15.8M+4.42M
a divider or leakage-biased feedback?) and the author already knows.
Declare intent; detect only what wasn't declared.

## Non-goals (for now)

- No SPICE/simulation — the formulas are the contract; corners + RSS
  arithmetic, not transient analysis.
- No analog-block autorouting of internals beyond what the template's
  netlist fragment states.
