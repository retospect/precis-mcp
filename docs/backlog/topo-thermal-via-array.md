---
status: draft
---
# Thermal via arrays — geometry and process prerequisites

G2t in [topo-route-geometry-truth](topo-route-geometry-truth.md) requires
arrays inside explicitly identified exposed thermal pads, independent of
ordinary ground escape. Its gate is an array for every declared thermal pad,
unchanged signals and no new geometric DRC errors.

## Process evidence before integration

JLCPCB distinguishes soldermask plugging from epoxy/copper fill and cap;
the ordinary soldermask-plugging description excludes via-in-pad holes.
Its guide describes automatic process selection for six or more layers,
which does not establish a choice for this four-layer design. Sources:
[via coverings](https://jlcpcb.com/help/article/pcb-via-covering) and
[via-in-pad guide](https://jlcpcb.com/help/article/via-in-pad-pofv-guide),
checked 2026-11-04 UTC. Flex-process availability remains to be established.

The local board description says flex, but an absent `meta.process` takes
the layer-count fallback in `src/precis/pcb/session.py::board_capability`.
Neither description text nor a manufacturer's available options establishes
the selected process. Do not change the board's manufacturing choice or
borrow rigid-board limits for flex to make an array pass.

## Geometry prototype

Prepare a standalone, deterministic prototype for rectangular local lands.
Inputs explicitly supply land width/height, centre-to-edge inset and minimum
centre spacing. Compare square and staggered triangular lattices in both
orientations and deterministic phases; retain a highest-count feasible
candidate. Check every emitted pair and boundary independently. This is a
bounded candidate search, not proof of globally optimal circle packing.

Return unquantized local centres; any eventual integration transforms and
quantizes once in board coordinates. No implicit drill, pitch, mask, web or
fabrication defaults. Report geometry separately from manufacturing legality
and thermal performance. Generic synthetic inputs can be evaluated now.

## Integration remains separate

An authored thermal-pad identity and selected, supported fabrication process
must precede router/DRC/export integration. The manufacturing rules must
derive the explicit geometry inputs and retain existing general-pad checks.
A thermal exception cannot become a blanket same-net via-in-pad exemption.
Review classification, fanout, spacing, export and process provenance together
before any array reaches a board. The prototype changes none of those paths.
