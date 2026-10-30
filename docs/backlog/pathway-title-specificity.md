---
status: draft
title: "pathway titles carry the surface detail — dopant/substitution, not just the host element"
pillar: 3d-design
prio: low
---

# Pathway titles carry the surface detail

Source gripes: gr530284.

Reto via the /gripes web form on pathway ref 527720: the title reads
"NO → NH3 on Pd" where it could read "NO → NH3 on Pd with one Rh at
surface" — the dopant/substitution is the detail that distinguishes the
run from every other NO→NH3-on-Pd pathway in the list.

## Motivation / why

`precis_pathway.persist.pathway_title` renders
`f"{substrate} → {target} on {element}"` from the artifact's
`config.slab.element` — three tokens, no surface composition. An agent
listing pathways (the handler's search view prints
`slug [status] title`) reads titles to pick a run; in a doped-surface
study every row is indistinguishable. The title is also what search
results and ref links surface.

## In scope

- A naming convention for `pathway_title` (or its replacement) that
  carries the deciding surface detail — dopant/substitution identity
  and count; facet/termination and coverage where they are the
  variable.
- Retitling existing pathway refs to the convention — titles are
  display metadata, rewritten at persist time today
  (`persist_result`'s `update_ref` is the same door).

## Explicitly NOT in scope

- Changing `content_key`, slugs or any stored geometry — titles are
  display only.
- The offline report's own page titles (`autocatpath/report.py`) unless
  they share the titler — check before widening.

## Acceptance criteria

Draft — to be hardened at `ready`.

- A pathway whose slab carries a substitutional/dopant atom shows it in
  the title (e.g. "NO → NH3 on Pd (1 Rh at surface)"), distinguishable
  in the search list from the pure-Pd run.
- A `slab_extxyz`-injected slab still gets a descriptive title, not `?`
  — TBD how the surface description is derived there (open question).
- What happens to the ~700 existing pathway titles — restamp under the
  new convention or leave — TBD on the open question below.

## Target + blast radius

`src/precis_pathway/persist.py::pathway_title` (the only titler; called
from `handler.py`'s insert path and `persist_result`'s re-stamp), the
artifact `config.slab` block it reads, and whatever surface census the
injected-slab path can compute over the stored geometry. Display-only;
no stored data changes.

## Open questions / decisions log

Reto's (`reto:ask`) — unresolved; stays `draft`.

- **The convention.** How specific: dopant element + count only, or
  also facet, termination, coverage, adsorbates? The gripe's example
  suggests count + "at surface".
- **Where the surface description comes from** for `slab_extxyz` runs:
  the config carries no element label there — census the injected
  geometry (composition minus the majority element = the dopant list)
  or carry a human label from the source structure ref.
- **Restamp or not.** Re-title the existing pathway refs under the new
  convention, or apply it to new runs only.
