"""``mol.generate`` and ``mol.prism`` — the hex tiler and the closed-form
generators as cached steps (molecular-workspace-code slice 5).

- :func:`prism` — ``hexfold.prism.prism_tube`` + ``hexfold.stick.stick`` +
  ``pga.geom``, the three calls ``examples/hexfold/prism/main.py`` makes,
  as one step returning a Geom (its ``meta['_net']`` rides in the cache
  entry, so ``mol.cap`` / ``mol.sites`` keep the Net path after a hit).
- :func:`generate` — one generic wrapper over the generator registry
  (``precis_se.atomic.generators.GENERATORS``) for ``hexfold_scene``,
  ``cnt``, ``fullerene`` and ``cyclodextrin``: the params dict goes to the
  generator, the :class:`GeneratedBlock` comes back as a Geom — atoms with
  the generator's hybridization and bond kinds, one port per
  ``GeneratedPort`` (``atom`` = the atom label, ``direction`` as given,
  the ring/lattice detail under ``annotations``), the topology and
  provenance under ``meta``. ``cyclodextrin`` embeds with RDKit and takes
  ~25 s cold; every call here is a hit afterwards.

A step's parameters are plain values (``steps`` §What is a step), which is
why neither takes a Net or a block: the recipe values go in, the object
is built inside the step.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import numpy as np

from precis.errors import BadInput
from precis_geom import assembly as pga
from precis_geom import steps as _steps
from precis_geom.assembly import Geom, Port
from precis_geom.mol.fragment import build_salt

#: Generators the wrapper covers (a subset of ``GENERATORS``: the ones whose
#: output is a finite atom block with the ports above).
GENERATOR_NAMES = ("hexfold_scene", "cnt", "fullerene", "cyclodextrin")
#: Bump when a generator's output changes in a way the wrapper's own source
#: does not show (the step key covers that source, not the generators').
_REV = 1


def _plain(v: Any) -> Any:
    """``v`` as JSON-safe plain data (numpy -> lists, tuples -> lists)."""
    return json.loads(json.dumps(v, default=_np_default))


def _np_default(o: Any) -> Any:
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def _salt() -> str:
    return f"generators/{_REV}; " + build_salt("precis_se.atomic.generators", "hexfold")


@_steps.step(name="generate", frame="generate", salt=_salt)
def generate(name: str, **params: Any) -> Geom:
    """The generator ``name`` run on ``params`` as a :class:`Geom`.

    ``name`` is one of :data:`GENERATOR_NAMES`; ``params`` is that
    generator's params dict as keyword arguments (``mol.generate('cnt',
    n=6, m=6, length_A=20)``, ``mol.generate('cyclodextrin',
    variant='alpha')``). A cached step: the same name and params again are
    a hit, from any call site."""
    if name not in GENERATOR_NAMES:
        raise BadInput(
            f"mol.generate: unknown generator {name!r}",
            options=list(GENERATOR_NAMES),
            next="pick one of the options; other generators (cone, tpms, "
            "smooth_drum, …) run through the structure 'generate' op",
        )
    from precis_se.atomic.generators import GENERATORS, GeneratorError

    try:
        block = GENERATORS[name](copy.deepcopy(params))
    except GeneratorError as exc:
        raise BadInput(
            f"mol.generate({name!r}): {exc}",
            next=exc.fix or f"fix the {name} params the message names",
        ) from exc
    if block.dry_run or not block.elements:
        raise BadInput(
            f"mol.generate({name!r}) returned no atoms",
            next="drop dry_run from the params",
        )
    return _block_geom(name, block)


def _block_geom(name: str, block: Any) -> Geom:
    coords = np.asarray(block.coords, dtype=np.float64)
    scene = pga._scene_from_parts(
        list(block.elements),
        coords,
        [tuple(b) for b in block.bonds],
        maker=pga._leaf(),
    )
    labels = list(scene.atoms)
    hyb = block.hybridizations
    for k, atom in enumerate(scene.atoms.values()):
        atom.hybridization = hyb[k] if hyb is not None else block.hybridization
    ports: dict[str, Port] = {}
    for gp in block.ports:
        label = labels[gp.atom_index]
        xyz = coords[gp.atom_index]
        d = gp.direction
        ann: dict[str, Any] = {"order": 1.0}
        if gp.atoms:
            ann["atoms"] = [labels[i] for i in gp.atoms]
        if gp.lattice is not None:
            ann["lattice"] = gp.lattice
        if gp.payload is not None:
            ann["payload"] = _plain(gp.payload)
        ports[gp.name] = Port(
            name=gp.name,
            pos=(float(xyz[0]), float(xyz[1]), float(xyz[2])),
            direction=(float(d[0]), float(d[1]), float(d[2])),
            roles=tuple(gp.roles),
            atom=label,
            annotations=ann,
        )
    pga._record(atoms=len(scene.atoms), bonds=len(scene.bonds))
    return Geom(
        scene=scene,
        ports=ports,
        meta={
            "units": "A",
            "name": name,
            "generator": name,
            "topology": _plain(block.topology),
            "provenance": str(block.provenance),
        },
    )


@_steps.step(name="prism", frame="prism", salt=lambda: build_salt("hexfold"))
def prism(
    periods: int,
    rows: int,
    strip_rows: int,
    seams: str,
    pores: Any = None,
) -> Geom:
    """The hexagonal prism tube ``prism_tube(periods, rows, strip_rows,
    seams, pores)`` stick-relaxed and exported — ``pga.geom(net,
    coords=stick(net))`` — as a Geom. A cached step: the stick relax is
    the cost of a recipe run, and a hit skips it."""
    from hexfold.prism import prism_tube
    from hexfold.stick import stick

    try:
        net, _tube = prism_tube(periods, rows, strip_rows, seams, pores)
    except ValueError as exc:
        raise BadInput(
            f"mol.prism({periods}, {rows}, {strip_rows}, {seams!r}): {exc}",
            next="adjust the prism recipe the message names (closure needs "
            "#plus − #minus = 6; rows even)",
        ) from exc
    if not net.report.ok:
        errs = "; ".join(str(f) for f in net.report.errors()[:3])
        raise BadInput(
            f"mol.prism({periods}, {rows}, {strip_rows}, {seams!r}): the net "
            f"fails hexfold.check — {errs}",
            next="adjust the prism recipe, or read net.report for the findings",
        )
    return pga.geom(net, coords=stick(net))
