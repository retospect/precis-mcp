---
id: precis-net-class-help
title: precis — naming & classifying PCB nets
summary: name every net for its meaning and give it a class so precis can size the trace, pour it as a plane, and pick measure defaults — power/gnd/i2c/spi/analog/diff/clock. Covers trace width from current, copper planes, ratsnest exclusion, signal integrity intent.
applies-to: put (kind='pcb') nets[]; drives width / planes / measures
status: active
---

# precis-net-class-help — a net's name and class carry its meaning

In a `pcb` design every net has a **required, meaningful name** and an optional
**class**. The name *is* the intent (`VCC3V3`, `I2C_SCL`, `MIC_OUT` — never
`N$12`). The class tells precis how to treat the copper: how wide, whether it
pours as a plane, and which design measures apply by default. Set both in the
`nets` array of `put` ([[precis-pcb-help]]).

```python
'nets': [
  {'name':'VCC3V3', 'class':'power', 'current':0.5},   # 0.5 A → width sized for you
  {'name':'GND',    'class':'gnd'},                     # plane net
  {'name':'I2C_SCL','class':'i2c'},
  {'name':'MIC_OUT','class':'analog'},
]
```

## The classes

| class | meaning | width | plane? | typical measures |
|-------|---------|-------|--------|------------------|
| `power` / `pwr` | a supply rail | from `current` (or wide) | yes (inner plane) | short, low-Z; bypass at each load |
| `gnd` / `ground` | the return | widest / pour | **yes** | continuous pour, stitching |
| `analog` | a sensitive analog signal | signal | no | keep away from `noisy`; guard |
| `i2c` | an I²C bus line (SCL/SDA) | signal | no | pull-ups; short; see [[precis-i2c-help]] |
| `spi` | a SPI line (SCK/MOSI/MISO/CS) | signal | no | length-similar; see [[precis-spi-help]] |
| `diff` | one half of a differential pair | controlled | no | length-match (phase 2) |
| `clock` | a clock / high-speed edge | signal | no | short; away from analog |
| `signal` (default) | generic logic | signal | no | — |

Class is free-text + a recognised set, so a class precis doesn't know is still
stored and rendered — it just gets generic defaults.

## Width from current

Give a net `current` (amps) and precis sizes the trace to a sane width (1 oz
copper, modest temperature rise) and snaps to a conventional step rather than
an arbitrary value. Override with explicit `width` (mm) when you have a reason
(impedance, a connector pad). Rule of thumb you can sanity-check against: ~0.5
mm/A on external 1 oz copper for a ~10 °C rise; bump up for inner layers.

```python
{'name':'VBUS', 'class':'power', 'current':2.0}     # ~1 mm trace, stepped
{'name':'ANT',  'class':'rf',    'width':0.43}      # explicit for 50 Ω on the stack
```

## Planes & the ratsnest

**Plane classes** (`gnd`/`ground`/`power`/`pwr`/`plane`) are **excluded from
the crossing/ratsnest metric** — they pour as copper, they don't route
point-to-point, so counting their airwires would drown the real signal
crossings. The netlist still models **every** GND/VCC connection (so DRC and
connectivity stay honest); only the *placement objective* ignores them. This is
why `view='crossings'` shows you signal congestion, not a hairball.

Default stack is **4-layer Sig / GND / PWR / Sig** (components + signals on the
outer layers, planes inner — manufacturable and quiet). 2-layer is supported;
on 2-layer the ground "plane" is a pour and crossings matter much more.

## Where the classes come from

You assign them from **the datasheet + general circuit reasoning**:

- the datasheet says a pin is a supply, an I²C line, an analog input → class it;
- a node carrying real current → `power` + a `current` estimate;
- a sensitive node (ADC input, microphone, crystal) → `analog`/`clock` and tag
  the parts `roles:['sensitive']` so [[precis-measures-help]] can keep noisy
  parts away.

Use [[precis-datasheet-help]] to pull pin functions; explicit override is
always allowed — the class is your judgement, not a lookup.
