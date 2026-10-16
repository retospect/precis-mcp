---
status: idea
title: pcb — a `prototype` board flag that turns thermal reliefs on everywhere
pillar: 3d-design
---

# pcb: `prototype` board flag → thermal reliefs everywhere

Reto, 2026-10-16: pours tie pads solid by default (machine assembly,
`planes.py` docstring); reliefs are per-class opt-in
([pcb-stackup-regions](pcb-stackup-regions.md) slice 10). A board-level
`prototype` flag would switch every pad to reliefs at once, so hand rework
and hackable boards solder easily. Needs slice 10's spoke generator and
`starved_thermal` check; the flag is one board key that overrides the
per-class default.
