---
id: precis-hexfold-probe-help
family: se
title: precis — hexfold probe: ask what a sites:/atoms:/frame: selector resolves to
summary: probe a selector against a bound hexfold object without running a build — hexfold.probe.probe(obj, selector) resolves atoms:<part>[<indices>], sites:<part>/<band>/s<i>..s<j>, sites:<part>/cells and frame:<key>[!|/touched|/rest] on a Net, Strip, tube bookkeeping object or structure scene at script time, prints "probe <label>: resolves to N <kind>", records on the calling trace frame (view='trace' lists it) and returns {count, digest, sample}; a bad selector raises precis_diag.ProbeError whose Diag.next names the fix
answers:
  - how do I ask what a hexfold selector resolves to without building?
  - what is the sites:/atoms:/frame: selector grammar on a hexfold object?
  - how do I list a net's bands or a strip's ring cells from a script?
  - what do the 'no part'/'no band'/'has no cells' probe refusals mean?
applies-to: hexfold.probe (workspace scripts)
status: active
tags: verbs, design
kinds: se
---

# precis-hexfold-probe-help — what a selector resolves to, no build

`hexfold.probe.probe(obj, selector)` asks what a selector resolves to
**without running a build** (gr522032): prints
`probe <label>: resolves to N <kind>`, records on the calling trace
frame (`view='trace'` lists it), returns `{count, digest, sample}`.
`obj` is a `Net`, `Strip`, tube bookkeeping object, or structure scene —
probing reads the bound object's bookkeeping (atom ordinals, band
tuples, ring cells), never composes or relaxes anything.

```python
probe(net, "atoms:t[0,3,5-9]")      # atoms of instance t
probe(net, "sites:_/foot/s0..s3")   # seam `foot` sites s0..s3
probe(net, "sites:t/out/s0..s2")    # port t.out's dangling atoms
probe(net, "sites:t/cells")         # t's ring cells (kind "cells")
probe(st, "sites:_/cells")          # the strip's (u,v) ring cells
probe(net, "sites:_")               # the net's bands (kind "bands")
probe(net, f"frame:{key}")          # atoms that trace frame made
```

## Grammar

The `sites:`/`atoms:`/`frame:` grammar of `precis-se-regions-help`:

- `atoms:<part>[<ordinals/ranges>]` — ordinals (`0,3,5-9`, inclusive)
  **within the named part's atoms**, the se `atoms:<block>[i]` reading.
  `<part>` is an instance/region name or the whole object (`_`, `*` or
  the object's name). A band or sub-region (`foot`, `t.out`, `rim`,
  `pore0`) is not a part — index its atoms under `sites:`
  (`sites:_/foot/s0..s3`, never `atoms:foot[0-3]`).
- `sites:<part>/<band>/s<i>..s<j>` — sites i..j inclusive of a named
  band: a seam (`foot`), a port (`t.out`), a rim, `seam<i>`/`strip<i>`/
  `arc<i>`/`wall<i>`/junction. No span = the whole band's atoms.
  `sites:<part>` alone lists the part's bands (kind `bands`). The part
  is validated on every form — an unknown part refuses naming the
  known ones.
- `sites:<part>/cells` — the part's ring cells: cells whose atoms sit
  inside the part ((u,v) keys on a strip, `r<i>` elsewhere);
  `sites:<part>/(u,v)` is one cell. `cells` on an object with no ring
  cells refuses — the object has no cells.
- `frame:<key>[!|/touched|/rest]` and `frame:qual=<qual>[…]` — atoms
  the trace frame's subtree made (`!` the frame alone, `/rest` the
  subtree minus it). `/touched` unions the whole subtree's touched
  ordinal ranges; a subtree that recorded only a count refuses — it
  recorded a count, not ids. Needs a live tracer (inside `out.run` or
  `trace.run`).

## Refusals

A bad selector raises `precis_diag.ProbeError` — its Diag's `next`
names the fix: `no part 'x'` names the known parts (`atoms:` adds that
a band's atoms are `sites:<part>/<band>`), `no band 'x' on 'p'` lists
that part's bands, `no cells` means the object carries no ring cells,
and a count-only `/touched` suggests `frame:<key>` for maker atoms.

## See also

- [[precis-hexfold-help]] — the .hx spec, scene grammar, tops, refusals
- [[precis-hexfold-seams-help]] — the equal-120 seam family
