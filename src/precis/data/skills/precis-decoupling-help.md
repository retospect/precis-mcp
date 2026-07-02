---
id: precis-decoupling-help
title: precis — power decoupling & bypass caps on a PCB
summary: add the right bypass/decoupling capacitors to a pcb design — one 100nF per power pin placed AT the pin, bulk caps per rail, and the proximity measures that keep them there. Covers power integrity, VDD bypass, bulk capacitance, supply rails.
applies-to: put (kind='pcb'); pattern playbook
status: active
---

# precis-decoupling-help — bypass caps, done right

Decoupling is the most common thing a board gets wrong, and it's almost
entirely a **placement** problem — the right cap in the wrong place does
nothing. This is a pattern playbook for the `pcb` kind ([[precis-pcb-help]]).

## The rule

1. **One 100 nF (0402, X7R) per power pin**, on the **same rail** as that pin.
2. **Placed AT the pin** — shortest loop from cap to the IC's VDD/GND. This is
   the part that matters and the part a netlist alone can't enforce, so you
   state it as a `proximity` measure.
3. **Bulk capacitance per rail** — one 1–10 µF (0805) near where the rail
   enters the board / the regulator output, to refill the small caps.
4. Big/fast loads (MCU core, RF PA) may want **two** small caps of different
   value at the pin.

## Capture it

```python
put(kind='pcb', id='s', args={
  'components':[
    # one bypass cap per VDD pin
    {'refdes':'C1','label':'100nF 0402','part':'C1525','pins':[{'name':'1'},{'name':'2'}]},
    {'refdes':'C2','label':'100nF 0402','part':'C1525','pins':[{'name':'1'},{'name':'2'}]},
    # bulk on the 3V3 rail
    {'refdes':'C3','label':'10uF 0805','part':'C15850','pins':[{'name':'1'},{'name':'2'}]},
  ],
  'connections':[
    {'net':'VCC3V3','refdes':'C1','pin':'1'}, {'net':'GND','refdes':'C1','pin':'2'},
    {'net':'VCC3V3','refdes':'C2','pin':'1'}, {'net':'GND','refdes':'C2','pin':'2'},
    {'net':'VCC3V3','refdes':'C3','pin':'1'}, {'net':'GND','refdes':'C3','pin':'2'},
  ],
  'measures':[
    {'metric':'proximity','operands':[{'instance':'C1'},{'instance':'U1'}],
     'goal':2,'strength':'hard','reason':'VDD bypass at U1 pin'},
    {'metric':'proximity','operands':[{'instance':'C2'},{'instance':'U1'}],
     'goal':2,'strength':'hard','reason':'second VDD bypass at U1'},
  ],
})
```

The `proximity` `hard` measures are what survive autoplace — without them the
annealer is free to scatter the caps to cut a crossing. See
[[precis-measures-help]].

## Picking the caps

- Bypass: **100 nF 0402 X7R**, any voltage ≥ 2× the rail. Search
  `100nF 0402 X7R` ([[precis-part-select-help]]) and take the top Basic row.
- Bulk: **1–10 µF 0805 X5R/X7R**, voltage ≥ 2× the rail.
- Don't over-think the value spread; modern practice is "lots of 100 nF + bulk",
  not a decade ladder.

## Check it

- `get(kind='pcb', id='s#U1')` — every VDD/GND pin should land on the rail and
  have a cap on the same net nearby.
- `get(kind='pcb', id='s', view='measures')` after `autoplace` — the bypass
  `proximity` rows should read `ok`.
- The datasheet's "recommended decoupling" section is authoritative for *how
  many* and *what values* — [[precis-datasheet-help]].
