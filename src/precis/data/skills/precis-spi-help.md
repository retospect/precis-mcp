---
id: precis-spi-help
title: precis — wiring a SPI bus on a PCB
summary: wire a SPI bus on a pcb design — shared SCK/MOSI/MISO plus one chip-select per device, master-out/master-in direction, length-similar lines. Covers SPI, SCK, MOSI, MISO, CS/SS chip select, bus topology.
applies-to: put (kind='pcb'); pattern playbook
status: active
---

# precis-spi-help — the four-wire bus

SPI shares three lines and adds **one chip-select per device**: `SCK`, `MOSI`,
`MISO` are common to all; each peripheral gets its own `CSn` from the master.
Pattern playbook for the `pcb` kind ([[precis-pcb-help]]).

## The rules

1. **Three shared nets** — `SPI_SCK`, `SPI_MOSI`, `SPI_MISO` — class `spi`
   ([[precis-net-class-help]]). MOSI = master-out (master→all), MISO =
   master-in (all→master, the peripheral drives it only when selected).
2. **One chip-select net per peripheral** — `CS_FLASH`, `CS_ADC`, … — from a
   master GPIO each. N devices → N CS nets.
3. Idle-high CS usually wants a **pull-up** so the device isn't selected during
   boot/reset.
4. Keep `SCK` (the clock) **short and away from analog**; at higher speeds keep
   the shared lines **length-similar** (real length-matching is phase 2).

## Capture it

```python
put(kind='pcb', id='s', args={
  'nets':[
    {'name':'SPI_SCK','class':'spi'}, {'name':'SPI_MOSI','class':'spi'},
    {'name':'SPI_MISO','class':'spi'},
    {'name':'CS_FLASH','class':'spi'}, {'name':'CS_ADC','class':'spi'},
  ],
  'connections':[
    # shared lines to master U1 + both peripherals U2 (flash), U3 (adc)
    {'net':'SPI_SCK','refdes':'U1','pin':'SCK'},  {'net':'SPI_SCK','refdes':'U2','pin':'SCK'},  {'net':'SPI_SCK','refdes':'U3','pin':'SCK'},
    {'net':'SPI_MOSI','refdes':'U1','pin':'MOSI'},{'net':'SPI_MOSI','refdes':'U2','pin':'SI'},  {'net':'SPI_MOSI','refdes':'U3','pin':'DIN'},
    {'net':'SPI_MISO','refdes':'U1','pin':'MISO'},{'net':'SPI_MISO','refdes':'U2','pin':'SO'},  {'net':'SPI_MISO','refdes':'U3','pin':'DOUT'},
    # one CS per device
    {'net':'CS_FLASH','refdes':'U1','pin':'GPIO5'},{'net':'CS_FLASH','refdes':'U2','pin':'CS'},
    {'net':'CS_ADC','refdes':'U1','pin':'GPIO6'},  {'net':'CS_ADC','refdes':'U3','pin':'CS'},
  ],
})
```

Pin names differ per part (`SI`/`MOSI`/`DIN` all mean master-out) — read each
from the datasheet ([[precis-datasheet-help]]) and wire the *function*, not the
label.

## Check it

- `get(kind='pcb', id='s@SPI_SCK')` — master + **every** peripheral on the
  clock.
- `get(kind='pcb', id='s@CS_FLASH')` — exactly **two** pins (master GPIO + that
  one device's CS). A CS net with three members is a wiring bug.
- `get(kind='pcb', id='s#U2')` — confirm the flash's SCK/SI/SO/CS each land on
  the right net.

For the two-wire shared-address bus, see [[precis-i2c-help]].
