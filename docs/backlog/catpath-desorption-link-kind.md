---
status: idea
pillar: 3d-design
---

# catpath desorption links carry a zero-energy supply convention

Desorption edges are bookkept like H-reservoir supply edges (ΔE = 0), but a
desorption is a real gas-phase-referenced energetic cost. Needs a typed link
kind on the catpath side (not a precis fix) so harvest/CHE math can
distinguish "supply, bookkeeping only" from "desorption, a real cost" instead
of silently treating both as zero.

Ranked by the catalysis-selectivity thread (Do-next 2) since 2026-10-02:
the first slice of `pathway-diagram-step-annotations.md`, which adds the
typed `kind` and `added`/`removed` link fields together.
