---
id: precis-workspace-mol-help
family: se
title: precis — build molecules by workspace script (mol.* fragments, decorators, step cache, collapse check)
summary: the molecular side of the workspace script loop — precis_geom.mol builds a base (mol.prism / mol.generate / mol.frag + mol.lib), decorates it with the site grammar (open & face:out & every:3), caps, and quick-relaxes it (mol.relax collapse check, rungs uff/emt/xtb/geo, named broken bonds), with every costly step cached in scratch/cache so a re-run after a crash or a recipe edit starts at the first changed step (steps.recipe/ls/load/clear). Covers the chemistry check at out.run exit (check.json, strict), and pg.inspect ray/section/overlap for "does the guest fit". One runnable worked example (decorated prism with a C60 guest) quoted from examples/hexfold/decorated-prism.
answers:
  - how do I build a molecule or nanostructure by script and decorate it with functional groups?
  - how do I attach COOH / OH / PEG to every third edge atom of a prism or nanotube?
  - what does the site grammar 'open & face:out & every:3' select?
  - how do I resume a molecular build after a crash or change one number without rebuilding everything?
  - what are steps.ls, steps.load, steps.clear, recipe.json and the step cache?
  - how do I check whether a built structure collapses (bonds break) before import?
  - what is mol.relax, the collapse check, and what does relaxed_by mean on a block?
  - why did out.run print chem warnings and where is check.json?
  - how do I check that a guest molecule fits in a nanotube pore by script?
applies-to: workspace scripts that build atomic structures (precis_geom.mol, precis_geom.steps, precis_geom.inspect → out.run → import_workspace)
status: active
tags: [design]
kinds: [se, structure]
---

# precis-workspace-mol-help — build molecules by script

The molecular loop inside the workspace script of [[precis-workspace-help]]:
build a base (`mol.prism`, `mol.generate`, `mol.frag` + `mol.lib`),
`mol.decorate` it at selected sites, `mol.cap` the rest, `mol.relax` as a
collapse check, then `o.block` / `o.instance` and `import_workspace`. Blocks
are atomic; the importer, manifest and trace are the ones that skill
describes. Alias: `import precis_geom.mol as mol`; `from precis_geom import
out, steps`.

## Build a decorated prism with a guest, check it, emit it

`recipe.json` beside `main.py` holds every knob; `steps.recipe()` reads it.

```json
{
  "periods": 4,
  "rows": 4,
  "strip_rows": 2,
  "seams": "++++++",
  "every": 3,
  "group": "COOH",
  "relax_steps": 200,
  "guest_nm": 2.0
}
```

```python
from __future__ import annotations

import precis_geom.mol as mol
from precis_geom import out, steps


def main() -> None:
    R = steps.recipe()  # recipe.json
    with out.run("decorated-prism") as o:
        p = mol.prism(R["periods"], R["rows"], R["strip_rows"], R["seams"])
        p = mol.decorate(
            p,
            sites=f"open & face:out & every:{R['every']}",
            group=R["group"],
        )
        p = mol.cap(p)  # H on the rest
        p, cc = mol.relax(p, steps=R["relax_steps"])  # auto: finite -> uff
        cc.raise_if_collapsed()  # Run.fail records it, naming the frames
        o.block(
            "p",
            p,
            relax={"method": "gfn2"},
            desc="hexagonal prism tube (4, 4, 2, '++++++'), COOH on every "
            "third open outward edge atom, H-capped, UFF collapse-checked",
        )
        o.block("c60", mol.lib("c60"), template=True, desc="C60 guest template")
        o.instance(
            "guest",
            template="c60",
            parent="p",
            pose=[0.0, 0.0, R["guest_nm"]],
            pose_unit="nm",
        )
    print(o.root)


if __name__ == "__main__":
    main()
```

`uv run python main.py` writes `scratch/out/decorated-prism/` (cold ~3 s) and
echoes `steps: 0 hits · 8 rebuilt (from prism#0)`, `fit: ok`, `chem: …`.
Import with the `import_workspace` op. `relax={'method': 'gfn2'}` asks the
service relax for the record; the emitted coordinates are the UFF-checked seed.

## Choose a base: prism, generator, fragment, library

| call | returns |
|---|---|
| `mol.prism(periods, rows, strip_rows, seams)` | hexagonal prism tube, stick-relaxed, Net-built Geom |
| `mol.generate(name, **params)` | `name` ∈ `hexfold_scene`, `cnt`, `fullerene`, `cyclodextrin`; params as the generator's dict — `mol.generate('cnt', n=10, m=10, length_A=40)`; its ports carry over |
| `mol.frag(smiles='[*:1]C(=O)O')` | fragment; each `[*:n]` dummy is removed and becomes port `a<n>` (attachment atom + bond direction). Also `scene=Scene, ports={…}`, `conf_seed=`; `ref=` refuses in a script |
| `mol.lib(name, **params)` | `OH` `NH2` `CH3` `COOH` `Ph` (one port), `PEG` (`n=`, one port), `CH2n` (`n=`) / `C2H4` (two ports), `c60`; an unknown name lists the options |
| `mol.attach(host, site, frag.port('a1'), torsion=0.0, bond=None)` | one fused Geom; `site` is a host Port or an atom label with an open valence; bond length from covalent radii unless `bond=` (Å) |

A **fragment port** is the attachment atom plus the bond direction;
`attach` and `decorate` align it to the host site and bond the two. Every
result is a `Geom`: `pga.tile`, `pga.mirror` and `pga.place` take it
unchanged, and each atom keeps the `maker` frame of the line that made it.

## Pick the sites for a group — the site grammar

`mol.decorate(geom, sites, group, *, pattern=None, seed=0, torsion='scan')`
places `group` at every site `sites` selects. `sites` is a selector string
or the list `mol.sites(geom, selector)` returns (`MolSite`: `atom`,
`direction`, `face`, `ring_class`, `free`, `has_h`). Tokens join with `&`:

| token | selects |
|---|---|
| `open` | atoms below their valence |
| `face:out\|in\|axial` | the side of the surface the free bond leaves on; needs an axis (a tube's, a sheet's normal — read from the atoms, or `axis=`), else it refuses |
| `ring:<class>` | `5-ring` … `8-ring`, `open-edge`, `non-ring`, `sp3`, `substituted`, `hetero` |
| `element:<sym>` | the host element |
| `frame:<key>` | atoms made by that trace frame or its subtree (`frame:qual=<qual>`) |
| `every:k` | every k-th selected site, starting with the first |

```python
mol.sites(p, "open & face:out")                       # inspect before deciding
p = mol.decorate(p, "open & every:2", pattern=["OH", "CH3"])  # cycle groups
p = mol.cap(p)                                        # H on every open valence left
```

`group` is `'H'`, a library name, a SMILES with `[*:1]` or a Geom.
`torsion='scan'` tries 12 turns about each new bond and keeps the one with
the largest minimum non-bonded distance; a number fixes it (degrees).
Findings land on the block: `decorate.site_skipped` (the best torsion still
overlaps, or the group sits in bonding range of a non-bonded host atom —
nothing placed, cap it with H) and `decorate.valence` (site saturated).
`mol.cap(geom)` on a Net-built Geom is the `terminate` op's own function.

## Resume after a crash or an edit — the step cache

Every costly `mol` call is a cached step: `mol.prism`, `mol.generate`,
`mol.frag` (the SMILES embed), `mol.lib`, `mol.decorate`, `mol.cap`,
`mol.relax`. Re-running `main.py` is resume — each step before the first
changed one is a hit. The echo names where the rebuild began:
`steps: 3 hits · 4 rebuilt (from decorate#0)` after editing `every`.

- **Key.** Step name, parameters, the keys of the Geoms passed in, the
  `precis_geom` version, and the source of the step's function. A hit atom's
  `view='trace'` equals the fresh build's plus a `step … (hit)` tag.
- **Where.** `scratch/cache/` (LRU, 2 GB), never in the design. Reuse comes
  from re-running the same script; a copy at another path may rebuild the
  steps whose atoms were made under the old call sites.
- **Your own step.** `@steps.step` on a pure function returning a `Geom`
  (atomic, scene only), a cad `SceneSpec` or a pcb `Board`; parameters are
  strings, numbers, lists, or Geoms. An assembly, a pose or a template Geom
  runs uncached with a `UserWarning` naming why.
- **Stale hit.** The key covers a step's own source, not the helpers it
  calls. After editing a helper in `main.py` that a `@steps.step` function
  uses, run once with the environment variable `PRECIS_STEPS_FRESH` set (any
  value: existing entries are ignored and rewritten) or call `steps.clear()`.
  The `mol` steps also key the library sources, so a `precis_geom` upgrade
  misses by itself.

```python
for r in steps.ls("relax"):     # {key, name, params_digest, wall_s, hits, bytes, version, src_sha}
    print(r["key"][:12], r["wall_s"], r["hits"])
mid = steps.load("3fa91c")      # full key or unique prefix of ≥ 6 chars → the Geom
o.block("mid", mid)             # land an intermediate: through the importer, like any block
steps.clear()                   # drop every entry
```

## Did it collapse? — `mol.relax`, the cached collapse check

`p, cc = mol.relax(p, rung='auto', steps=200, fmax=None, max_disp=2.0)`
relaxes on an energy rung and compares the bonds the relaxed positions
perceive with the bonds the Geom declares. `p` is the relaxed Geom (atoms
keep their `maker`), `cc` the `CollapseCheck`: `cc.verdict` (`ok` |
`collapsed`), `cc.collapsed`, `cc.broken`, `cc.formed`, `cc.input_contacts`,
`cc.limits`, `cc.summary()`, `cc.raise_if_collapsed()` (raises
`RelaxCollapsed`; `out.run` then writes the failed run record).

| rung | what it is |
|---|---|
| `uff` | RDKit UFF on the declared bond graph; finite scenes only (refuses `pbc`) |
| `emt` | periodic-capable; a metals potential — H/C/N/O only as a stand-in |
| `xtb` | GFN2 where tblite is installed |
| `geo` | geometry repair on the declared graph; cannot break a bond, so the verdict covers displacement and ring census only; takes no `fmax` |

`auto`: finite → `uff`. Periodic → `xtb` if installed; else `emt` only for a
scene holding an EMT metal (Al, Ni, Cu, Pd, Ag, Pt, Au) and nothing outside
EMT's elements; else `BadInput` naming `rung='xtb'` and the service relax
(the block's `relax:`). An organic periodic scene never falls back to EMT —
it breaks intact C–H bonds. An explicit `rung='emt'` on an organic scene
returns a check whose `limits` say so.

**Verdict.** `collapsed` when the input already has a non-bonded contact
inside the bond-inference window (a build error, so every rung says it; UFF
would only push it apart), a declared bond breaks or a contact forms, a
declared ring is lost, the fragment count changes, or any atom moves more
than `max_disp` Å. Each is named by both atoms' maker frames:
`junction#3 ← main.py:9 lost C12–C13 (1.42 → 2.31 Å)`. On the energy rungs
the bonds are re-perceived every 10 steps and the run stops at the first
change; a long check checkpoints and resumes. The call is a cached step keyed
on rung, `steps`, `fmax`, `max_disp`: a rejected recipe reverted is a hit.

**Check, not record.** UFF can break a bond on a strained carbon seam that
GFN2 keeps; the verdict names the bond. `out.block` stamps the row
`relaxed_by: {rung, key, verdict}` while its coordinates are still the
checked ones, and `view='validate'` shows `relaxed_by uff ok`. The record is
the service relax the row's `relax:` requests.

## Read the chemistry check at finish

`out.run` exit runs the chemistry check on every atomic block (open valences
not declared as a port or radical, over-coordination, overlaps, bond lengths,
VSEPR angle and π-twist advisories, disconnected fragments) and writes
`check.json` beside `fit.json`:

```text
scratch/out/decorated-prism: chem: 0 error(s), 3 warning(s) -> check.json
```

Each finding carries the script line that made its first atom
(`… at main.py:32`). `import_workspace` runs the same function on arrival and
echoes the same text. Warnings never stop a run.

```python
with out.run("decorated-prism", check={"strict": True}) as o:   # error findings raise
    ...
with out.run("scratch-try", check=False) as o:                  # skip fit + chemistry checks
    ...
```

Strict raises `ChemCheckError` after `manifest.json` is written, and also
makes the fit check strict. `mol.check(geom)` runs the same check mid-script.

## Does the guest fit? — `precis_geom.inspect`

`import precis_geom.inspect as pgi` — `ray`, `section`, `overlap`,
`thickness` on any `Geom`, atomic or macro, in metres or unit strings
(`"2 nm"`). Atomic geometry is the union of van der Waals spheres; results
are plain data a script can assert on. `profile_samples=` on a ray along the
channel axis gives the free radius to the nearest wall: its minimum
(`.pinch_m`) is the bottleneck and `.pinch_features[i].label` the maker frame
of the wall that pinches it. `max_steps=` raises the march budget (default
4096) when a long grazing ray stalls — a bail sets `.stalled` and the kept
hits are partial. From `examples/hexfold/cnt-guest/main.py`:

```python
prof = pgi.ray(
    tube,
    origin=(cx * 1e-10, cy * 1e-10, z0 * 1e-10),   # metres: Å × 1e-10
    axis=(0.0, 0.0, 1.0),
    t_max=(z1 - z0) * 1e-10,
    profile_samples=200,
    events=(),
)
free_A = prof.pinch_m / 1e-10
if free_A <= r_guest:                    # cage radius, Å
    raise ValueError(f"guest does not fit: free radius {free_A:.2f} Å, "
                     f"wall {[f.label for f in prof.pinch_features]}")
```

`pgi.overlap(host, guest)` returns `.volume_m3` and names the frames on both
sides; `pgi.section(g, origin=…, normal=…)` returns `.loops`, `.hole_count`
and the bonds crossing the plane (`.crossings`). The model is the vdW
surface, not electron density; a benzene ring-plane section is one loop with
`hole_count` 0. For a design already imported, `view='inspect'` runs the same
probes ([[precis-se-regions-help]]).

## See also

- [[precis-workspace-help]] — the loop: `out.*` calls, manifest keys, trace, `import_workspace`
- [[precis-se-atomic-help]] — chemistry over the imported block tree; `view='validate'`, `view='sites'`
- [[precis-se-regions-help]] — regions, observation rays, `view='inspect'`
- [[precis-hexfold-help]] — the hex tiler `mol.prism` wraps
