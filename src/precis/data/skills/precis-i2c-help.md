---
id: precis-i2c-help
title: precis — wiring an I²C bus on a PCB
summary: wire an I²C bus on a pcb design — SCL/SDA as a shared two-wire net with one pair of pull-ups, multiple devices on the same two nets, addresses from datasheets. Covers I2C, SCL, SDA, pull-up resistors, bus topology.
applies-to: put (kind='pcb'); pattern playbook
status: active
---

# precis-i2c-help — the two-wire bus

I²C is a **shared two-wire bus**: every device hangs off the same `SCL` and
`SDA` nets, with **one** pull-up resistor per line for the whole bus. Pattern
playbook for the `pcb` kind ([[precis-pcb-help]]).

## The rules

1. **Two nets total** — `I2C_SCL`, `I2C_SDA` — no matter how many devices.
   Class them `i2c` ([[precis-net-class-help]]).
2. **One pull-up per line** to the bus voltage (usually VDD): typically
   **4.7 kΩ** (100 kHz/400 kHz, short bus), down to ~2.2 kΩ for fast-mode-plus
   or many devices / long traces. Place them once, near the master.
3. **Every device's SCL/SDA pins join the same two nets.** Addresses
   differentiate devices — read each address (and its strap pins) from the
   datasheet ([[precis-datasheet-help]]); fix conflicts by strapping or moving a
   device to a second bus.
4. Keep the bus **short** and off noisy/switching nodes (tag the regulator
   `noisy`, the bus parts… just keep them compact).

## Capture it

```python
put(kind='pcb', id='s', args={
  'components':[
    {'refdes':'R1','label':'4.7k 0402','part':'C25900','pins':[{'name':'1'},{'name':'2'}]},  # SCL pull-up
    {'refdes':'R2','label':'4.7k 0402','part':'C25900','pins':[{'name':'1'},{'name':'2'}]},  # SDA pull-up
    # U1 = master (MCU), U2 = a sensor — both already have SCL/SDA pins
  ],
  'nets':[
    {'name':'I2C_SCL','class':'i2c'},
    {'name':'I2C_SDA','class':'i2c'},
    {'name':'VCC3V3','class':'power'},
  ],
  'connections':[
    {'net':'I2C_SCL','refdes':'U1','pin':'SCL'}, {'net':'I2C_SDA','refdes':'U1','pin':'SDA'},
    {'net':'I2C_SCL','refdes':'U2','pin':'SCL'}, {'net':'I2C_SDA','refdes':'U2','pin':'SDA'},
    {'net':'I2C_SCL','refdes':'R1','pin':'1'},   {'net':'VCC3V3','refdes':'R1','pin':'2'},
    {'net':'I2C_SDA','refdes':'R2','pin':'1'},   {'net':'VCC3V3','refdes':'R2','pin':'2'},
  ],
})
```

Add more devices by adding two more `connections` (their SCL/SDA → the same two
nets). **Don't** add more pull-ups.

## Check it

- `get(kind='pcb', id='s@I2C_SCL')` — should list **every** device's SCL pin
  **plus** R1; same for `@I2C_SDA` + R2. Exactly one resistor per line.
- `get(kind='pcb', id='s', view='trace', args={'net':'I2C_SCL'})` to walk it.
- Two devices with the same fixed address → a real bug; resolve before
  ordering.

For the 4-wire master/slave-select bus, see [[precis-spi-help]].
