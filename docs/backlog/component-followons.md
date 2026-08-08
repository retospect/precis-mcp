# component kind follow-ons (v1 + assembly tree shipped)

Feature extensions to the shipped component kind (ADR 0071/0072).

- Comparator/violator query: explicit pass/fail against a target spec on the
  tree walk (v1 ships only the uniformity summary).
- Price-break-aware costing + per-each/per-metre uom reconciliation.
- Optional-part modelling ("included at qty 0" — v1's qty=0 removes the edge).
- Laminate layer structure (ordered layers + effective-property
  homogenization from the stack).
- Effective-property inheritance via `made-of → material` at read time.
- `realized_by → part` binding to a catalog C-number — discrete procurable
  parts only, NOT PCB internals; a PCBA component may realized_by → the pcb
  design.
- Category taxonomy tree with inherited spec sets (v1 flat).
Owner component handler/store; extend tests/test_component.py.
