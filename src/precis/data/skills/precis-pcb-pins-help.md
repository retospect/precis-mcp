---
id: precis-pcb-pins-help
family: pcb
title: precis — first-use pin classification (etype + facts + provenance)
summary: every pcb pin a net connects must carry a first-use class — an etype from the closed PIN_ETYPES vocabulary, the facts that etype carries (power pins take max_current_a + max_voltage_v, never fmax_hz; other used pins take fmax_hz, 0 = explicit DC; nc is exempt), and provenance (a source chunk handle or an assume note); the workspace builder's net.tie/board.wire is the gate — it refuses an unclassified pin or any_of slot and names the gaps; on a stored board op='pins' annotates the same fields in place
answers:
  - what pin classification does net.tie/board.wire require before it connects a pin?
  - which facts does each pin etype carry — fmax_hz vs max_current_a/max_voltage_v?
  - how do I classify pins on an imported or batch-authored pcb board (op='pins')?
  - how does an any_of flex slot's class filter its candidate pins?
applies-to: pcb workspace scripts (part.pin/pin.etype + net.tie/board.wire); put(kind='pcb', args={'op': 'pins'})
status: active
tags: [design]
kinds: [pcb]
---

# precis-pcb-pins-help — first-use pin classification

Every pin a board connects must be **classified at first use**
(gr520810) — the connection write is the gate, so a pin's class is
declared at or before the call that ties it, never after.

## What a class is

Three parts, all required for a *tied* pin:

- **etype** — one word from the closed `PIN_ETYPES` vocabulary:
  `power_in`/`power_out`/`input`/`output`/`bidi`/`passive`/`open_drain`/
  `tristate`/`analog_in`/`analog_out`/`nc`. The store writes it as an
  etype-vocabulary `tags` entry, so a stored pin classifies by tag too.
- **the facts that etype carries** — `power_in`/`power_out` take
  `max_current_a` + `max_voltage_v` and **never** `fmax_hz` (a supply
  pin's AC content is a decoupling question, not a bandwidth). Every
  other used etype takes `fmax_hz` — the highest significant frequency
  component of the signal (knee ≈ `0.35 / t_rise`), where `0` is
  explicit DC and an absent value is "unstated". `nc` is exempt — the
  marker IS the electrical fact.
- **provenance** — `source` (a datasheet chunk handle like
  `"pa…~pc…"`, or a table+row) or an explicit `assume` note when no
  datasheet evidence exists.

Completeness is a first-use rule, checked at the tie — a
declared-but-never-tied pin may carry a partial class.

## The gate — net.tie / board.wire

In the workspace builder the write — `net.tie(...)`/`board.wire(...)` —
refuses the whole call when any pin or `any_of` slot is unclassified,
naming each pin and what its class lacks; nothing is emitted until
every argument answers its class. Classify on the same call or earlier:

```python
b = pcb.Board("t")
u1, r1 = b.part("U1"), b.part("R1")
u1["PA1"].etype("output", fmax_hz=0.0, assume="gpio")
r1["1"].etype("passive", fmax_hz=0.0, assume="jellybean")
n1 = b.net("N1")
n1.tie(u1["PA1"], r1["1"], current=pcb.Max("20mA"), source="ds:9~c4")
vdd = u1.pin(7, etype="power_in", max_current_a=3.0,
             max_voltage_v=5.0, source="pa5~pc7")
b.net("VCC").tie(vdd)
```

On an `any_of` flex slot the class attaches to the **slot** — it
travels to the pin the solver assigns and filters the candidates (an
`analog_in` slot lands only on a pin whose `pin_functions` entry shows
AIN/ADC; a `max_current_a` need lands only on a candidate rated for
it).

## Classifying a stored board — op='pins'

A re-`put` never edits pins, so an imported or batch-authored board's
unclassified pins are annotated in place, one refdes per call, by
`op='pins'` — the same `etype`/facts/`source`/`assume` fields:

```python
put(kind="pcb", id="s", args={
    "op": "pins", "refdes": "U7", "source": "datasheet p.12",
    "pins": [{"name": "VDD", "etype": "power_in",
              "max_voltage_v": 3.6, "max_current_a": 0.5},
             {"name": "SDA", "etype": "bidi", "fmax_hz": 400000}]})
```

That is what makes a stored board scriptable — a `board_batch` →
`emit` script runs only when every tied pin answers its class. The
rest of the `op='pins'` surface (rail, vih/vil, current_role,
min_dielectric, unused/must_terminate): [[precis-pcb-check-help]].
