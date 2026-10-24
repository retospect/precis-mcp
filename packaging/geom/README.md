# precis-geom — run a workspace script locally

The construction core the structure workspaces run: `precis_geom.pcb`
(the board builder), `precis_geom.trace` (the construction trace) and
`precis_geom.out` (a run's `out/` in the importer's layout). This is the
recipe for the dev loop: load a `main.py`, build, check, fit, trace.
Every python block below was run at the repo root under `uv run python`;
the output shown is the output it produced.

## Install

In a repo checkout there is nothing to install — the dev env already
imports `precis_geom` (`uv run python -c "import precis_geom.pcb"`).
The wheel the sandbox runs is built with `scripts/ws-promote geom`, or
directly — either way the wheel lands in `packaging/geom/dist/`
(gitignored):

```text
$ uv build --wheel packaging/geom
Building wheel...
Successfully built packaging/geom/dist/precis_geom-0.1.0-py3-none-any.whl
```

## Atomic scene generators

The wheel also ships `precis_se.atomic.generators` (pure params → block
factories), so a workspace script can call the `hexfold_scene` scene
planner — `GENERATORS["hexfold_scene"]({"sheet": [30, 24], "features":
[...]})` — which the bare `hexfold` package's prism/rail/strip builders
do not reach (gr514057). The wheel's `precis_se/__init__.py` is a stub
(`packaging/geom/precis_se/`) that imports nothing — the real init pulls
in the DB stack — and must never be installed next to the server wheel.
This is the near-term surface; `docs/backlog/hexfold-scene-grammar-from-primitives.md`
is the long-term replacement.

## Load and build

A board `main.py` is a plain script — `importlib` loads it, `build()`
returns the `Board` (the same `_load` pattern
`tests/workspaces/test_pcb_board.py` uses):

```python
import importlib.util
spec = importlib.util.spec_from_file_location("board", "examples/boards/blinky_led/main.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
b = mod.build()
```

`b.to_batch()` is then the `pcb_apply`-shaped dict; `b.manifest()` /
`pcb.write_manifest(b, "out/")` serialise it.

The netlist queries name what they return: `nets_of` is the part's
sorted net names, `pin_nets` the pin → net map in the part's pin order,
and `pins_on` (also `net.pins()`) the `(refdes, pin)` pairs on a net:

```python
print(b.nets_of("R1"), b.pin_nets("R1"), b.pins_on("VIN"))
```

```text
['LED_A', 'VIN'] {'1': 'VIN', '2': 'LED_A'} [('R1', '1')]
```

## Check and fit

`b.check()` is the offline L0 pass — NOT-CHECKED names parts with
neither a catalog part nor a registered footprint; findings cover
unconnected nets and unknown footprints:

```python
print(b.check().summary())
```

```text
0 finding(s), 0 NOT-CHECKED (no catalog part, no footprint)
```

`b.shape()` derives the board's 3-D mechanical shape (outline, slab,
one prism per placed part); `.fits(...)` checks it against
counterparty boxes — `pcb_shape.enclosure(w, d, h)` asks whether the
whole board fits inside a w×d×h mm cavity:

```python
from precis_geom import pcb_shape
fit = b.shape().fits(pcb_shape.enclosure(30, 25, 15))
print(fit.summary())
```

```text
fit OK (0 finding(s))
```

A too-shallow cavity fails per part, naming the overflow:

```python
fit = b.shape().fits(pcb_shape.enclosure(30, 25, 2))
print(fit.summary())
```

```text
fit FAIL (2 finding(s)):
  error fit.outside_enclosure R1: R1 exceeds enclosure 'enclosure': z by 0.5 mm (top 2.5 > 2) (cavity 30×25×2 mm)
  error fit.outside_enclosure D1: D1 exceeds enclosure 'enclosure': z by 0.5 mm (top 2.5 > 2) (cavity 30×25×2 mm)
```

(2.5 mm because the `CHIP-0603` footprint maps to a proposed 0.9 mm in
the package-height table — `precis_geom.pcb_shape.HEIGHT_TABLE` lists
the regexes and which spellings each matches. An unmapped spelling
takes the table-maximum fallback and the finding says the height was
assumed; pass `height_mm=` on `b.part(...)` for an authored number. See
`precis_geom.pcb_shape.height_mm`.)

Unplaced parts are reported, not guessed: a part with no `.at(...)`
gets no prism and is named in `b.shape().unplaced`.

## Trace and out

`trace.run(work_root=...)` records the construction — `work_root` is
the dir that counts as user code (calls under it mint `user` frames);
`tr.dump()` is the trace document:

```python
from precis_geom import trace
with trace.run(work_root="examples/boards") as tr:
    mod.build()
doc = tr.dump()
print(len(doc["frames"]), [f["qual"] for f in doc["frames"]])
```

```text
11 ['', 'main.build', 'pcb.Board.outline', 'pcb.Board.footprint', 'pcb.Board.net', 'pcb.Board.part', 'pcb.Board.footprint', 'pcb.Board.part', 'pcb.Board.footprint', 'pcb.Board.wire', 'pcb.Board.net']
```

`out.run(name)` wraps a run in the importer's layout — a bare name lands
under `scratch/out/<name>/` (an explicit path is used as-is), and exit
writes `manifest.json` plus `trace.json` of everything traced inside
(`o.block(...)`/`o.instance(...)`/`o.connect(...)` calls would also
land `blocks/` geometry files):

```python
from precis_geom import out
with out.run("blinky") as o:
    mod.build()
print(o.root)
print(sorted(p.name for p in o.root.iterdir()))
```

```text
scratch/out/blinky
['manifest.json', 'trace.json']
```

If the body raises, `out.run` still writes the record — `manifest.json`
carries `status: "failed"` plus the error anchored at the user frame,
`trace.json` keeps the error-marked frames (a previous good one moves
to `trace.prev.json`), and a `failed -> …/trace.json` line lands on
stderr.

## Which line made U1

On an authored board the question is grep-able — `b.part("D1", ...)` is
line 33 of `examples/boards/blinky_led/main.py`. On the generated
boards it is not: `examples/boards/nano_usb_pd/main.py` is ~33 KB of
`scripts/pcb-batch-to-board` output, so the answer there is a line in
a generated pad list. The trace answers it mechanically either way —
every `pcb.Board.part` frame records the user call site's `file`/`line`
and the call's arguments:

```python
print([(f["file"], f["line"], f["args"]["refdes"])
       for f in doc["frames"] if f["qual"] == "pcb.Board.part"])
```

```text
[('blinky_led/main.py', 32, 'R1'), ('blinky_led/main.py', 33, 'D1')]
```

## Build an atomic structure (hexfold)

The atomic builder beside `precis_geom.pcb` is `hexfold`.
`hexfold.prism.prism_tube(periods, rows, strip_rows, seams)` closes a
polygonal tube of flat wall strips joined at equal-120 seam lines, each
seam's third sheet an outward fin (`+`) or an inward rib (`-`). `seams`
is the sign string — closure needs `#plus − #minus = 6` (the plain
hexagonal prism is `"++++++"`), `rows` must be even, and the argument
bounds are in `hexfold.prism.prism_tube`. It returns `(net, tube)` —
the atom/bond `Net` plus the seam bookkeeping:

```python
from hexfold.prism import prism_tube

net, tube = prism_tube(8, 4, 2, "++++++")
print("atoms", len(net.atoms), "bonds", len(net.bonds), "ok", net.report.ok)
```

```text
atoms 768 bonds 1068 ok True
```

The same `pga.geom` → `out.run` + `o.block` hand-off lands the geometry
in `scratch/out/<name>/` — the `.extxyz` block file plus its
`.bonds.json` sidecar:

```python
import precis_geom.assembly as pga
from precis_geom import out

with out.run("hexprism") as o:
    o.block("prism", pga.geom(net), desc="hexagonal prism tube, 6 fin seams")
print(o.root)
print(sorted(p.name for p in o.root.iterdir()))
print(sorted(p.name for p in (o.root / "blocks").iterdir()))
```

```text
scratch/out/hexprism
['blocks', 'manifest.json', 'trace.json']
['prism.bonds.json', 'prism.extxyz']
```

`pga.geom(net)` exports `net.seed3` — the seed placement, NOT a relaxed
geometry; relaxation is the SE side's job after import.
