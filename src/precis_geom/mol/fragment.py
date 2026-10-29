"""``mol.frag`` — a molecule as a building block: a :class:`Geom` with a
scene and named attachment ports.

Three sources, one result shape:

- ``frag(smiles='[*:1]C(=O)O')`` — RDKit parse, ``AddHs``, seeded ETKDGv3
  embed, best-effort MMFF (:func:`embed_molecule`, the path the
  ``from_smiles`` op shares). Each ``[*:n]`` dummy becomes port ``a<n>``
  and is removed: ``atom`` = the attachment atom's label, ``direction`` =
  unit vector attachment → dummy (the bond that will form, as embedded),
  ``annotations['order']`` = the dummy bond's order.
- ``frag(scene=Scene, ports={...})`` — wrap a built scene; a port given
  only an ``atom`` gets ``pos`` from the atom and ``direction`` from the
  atom's free valence.
- ``frag(ref='structure:<slug>')`` — reads through the read MCP, which a
  workspace script does not have: refuses with the next step.
"""

from __future__ import annotations

import copy
from typing import Any

import numpy as np

from precis.errors import BadInput
from precis_geom import assembly as pga
from precis_geom import trace as _trace
from precis_geom.assembly import Geom, Port
from precis_geom.mol import _valence

_EMBED_FAIL = (
    "3D embedding failed — the SMILES may be valid but geometrically "
    "pathological; try a different seed"
)


def embed_molecule(mol: Any, seed: int = 0) -> None:
    """Embed ``mol`` (an RDKit mol, hydrogens already added) in place:
    ETKDGv3 distance-geometry embed seeded from ``seed`` — same ``(mol,
    seed)`` in, bit-identical geometry out — then a **best-effort** MMFF
    cleanup (any failure is swallowed: the raw ETKDG geometry is
    acceptable on its own). Raises :class:`BadInput` (message in
    ``.cause``) when the embed itself fails.

    The one embed shared by ``mol.frag`` and the ``from_smiles`` op."""
    from rdkit.Chem import AllChem

    # The installed `rdkit-stubs` package's AllChem.pyi doesn't re-export
    # these (they live in rdDistGeom/rdForceFieldHelpers at runtime, via
    # AllChem's real wildcard import) — present at runtime; a stub gap only.
    params = AllChem.ETKDGv3()  # type: ignore[attr-defined]
    params.randomSeed = seed
    try:
        status = AllChem.EmbedMolecule(mol, params)  # type: ignore[attr-defined]
    except Exception as exc:
        raise BadInput(_EMBED_FAIL) from exc
    if status != 0:
        raise BadInput(_EMBED_FAIL)
    try:
        AllChem.MMFFOptimizeMolecule(mol)  # type: ignore[attr-defined]
    except Exception:
        pass  # best-effort cleanup only


# TODO(molecular-workspace-code slice 0): frag / lib become @steps.step
# (cached, recoverable) once precis_geom.steps lands.
@_trace.traced(name="frag")
def frag(
    smiles: str | None = None,
    *,
    scene: Any = None,
    ports: Any = None,
    ref: str | None = None,
    name: str | None = None,
    conf_seed: int = 0,
) -> Geom:
    """A fragment :class:`Geom` — give exactly one of ``smiles=``,
    ``scene=`` (+ ``ports=``) or ``ref=``. ``name`` labels the Geom
    (default: the SMILES). ``conf_seed`` seeds the 3D embed."""
    given = [k for k, v in (("smiles", smiles), ("scene", scene), ("ref", ref)) if v]
    if len(given) != 1:
        raise BadInput(
            f"mol.frag takes exactly one of smiles=, scene=, ref= (got "
            f"{given or 'none'})",
            options=["smiles", "scene", "ref"],
            next="e.g. mol.frag(smiles='[*:1]C(=O)O')",
        )
    if ref:
        raise BadInput(
            f"mol.frag(ref={ref!r}) reads the structure through the read MCP "
            "(blocks.get), which a workspace script runs without",
            next="fetch the structure with the read MCP, save its extxyz "
            "beside the script, load it as a Scene and call "
            "mol.frag(scene=..., ports={...})",
        )
    if scene is not None:
        return _frag_from_scene(scene, ports, name)
    if ports:
        raise BadInput(
            "mol.frag(smiles=) derives its ports from the [*:n] dummies; "
            "ports= belongs to scene=",
            next="mark attachment points as [*:1] in the SMILES",
        )
    return _frag_from_smiles(str(smiles), name, int(conf_seed))


def _frag_from_smiles(smiles: str, name: str | None, conf_seed: int) -> Geom:
    try:
        from rdkit import Chem
    except ImportError as exc:  # pragma: no cover - rdkit is a wheel dep
        raise BadInput(
            "mol.frag(smiles=) needs rdkit (a precis-geom wheel dependency)",
            next="install precis-geom with its dependencies",
        ) from exc

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise BadInput(
            f"{smiles!r} is not parseable as SMILES",
            next="check the SMILES; mark attachment points as [*:1], [*:2]",
        )
    dummies = [a.GetIdx() for a in mol.GetAtoms() if a.GetAtomicNum() == 0]
    numbers: dict[int, int] = {}  # dummy idx -> port number
    taken = {mol.GetAtomWithIdx(i).GetAtomMapNum() for i in dummies} - {0}
    nxt = 1
    for idx in dummies:
        n = mol.GetAtomWithIdx(idx).GetAtomMapNum()
        if n == 0:  # bare [*]: the lowest unused number, in SMILES order
            while nxt in taken:
                nxt += 1
            n = nxt
            taken.add(n)
        if n in numbers.values():
            raise BadInput(
                f"{smiles!r}: attachment number {n} is used twice",
                next="give each [*:n] dummy a distinct n",
            )
        numbers[idx] = n
        if mol.GetAtomWithIdx(idx).GetDegree() != 1:
            raise BadInput(
                f"{smiles!r}: dummy [*:{n}] must be bonded to exactly one atom "
                f"(has {mol.GetAtomWithIdx(idx).GetDegree()})",
                next="write each attachment as a terminal [*:n]",
            )
    attach_of = {
        idx: mol.GetAtomWithIdx(idx).GetNeighbors()[0].GetIdx() for idx in dummies
    }
    orders: dict[int, float] = {}
    for idx in dummies:
        b = mol.GetBondBetweenAtoms(idx, attach_of[idx])
        orders[idx] = 1.5 if b.GetIsAromatic() else float(b.GetBondTypeAsDouble())

    if dummies and all(o == 1.0 for o in orders.values()):
        # Embed with each single-bond dummy as H: a full molecule MMFF can
        # type, and the H lands where the bond will form.
        rw = Chem.RWMol(mol)
        for idx in dummies:
            a = rw.GetAtomWithIdx(idx)
            a.SetAtomicNum(1)
            a.SetAtomMapNum(0)
        rw.UpdatePropertyCache(strict=False)
        Chem.SanitizeMol(rw)
        mol = rw.GetMol()
    mol = Chem.AddHs(mol)
    embed_molecule(mol, conf_seed)

    xyz = np.asarray(mol.GetConformer().GetPositions(), dtype=float)
    dummy_set = set(dummies)
    keep = [a.GetIdx() for a in mol.GetAtoms() if a.GetIdx() not in dummy_set]
    new_of = {old: k for k, old in enumerate(keep)}
    elements = [mol.GetAtomWithIdx(i).GetSymbol() for i in keep]
    carts = xyz[keep]
    bonds: list[tuple[int, int, float]] = []
    aromatic_pairs: set[tuple[int, int]] = set()
    for b in mol.GetBonds():
        i, j = b.GetBeginAtomIdx(), b.GetEndAtomIdx()
        if i in dummy_set or j in dummy_set:
            continue
        order = 1.5 if b.GetIsAromatic() else float(b.GetBondTypeAsDouble())
        bonds.append((new_of[i], new_of[j], order))
        if order == 1.5:
            aromatic_pairs.add((new_of[i], new_of[j]))

    maker = pga._leaf()
    out_scene = pga._scene_from_parts(elements, carts, bonds, maker=maker)
    labels = list(out_scene.atoms)
    for old, k in new_of.items():
        ra = mol.GetAtomWithIdx(old)
        atom = out_scene.atoms[labels[k]]
        atom.charge = int(ra.GetFormalCharge())
        atom.hybridization = "sp2" if ra.GetIsAromatic() else None
    for bond, (i, j, order) in zip(out_scene.bonds, bonds):
        if (i, j) in aromatic_pairs:
            bond.kind = "aromatic"

    port_d: dict[str, Port] = {}
    for idx in dummies:
        a_idx = attach_of[idx]
        a_pos = xyz[a_idx]
        d = xyz[idx] - a_pos
        d = d / float(np.linalg.norm(d))
        pname = f"a{numbers[idx]}"
        port_d[pname] = Port(
            name=pname,
            pos=(float(a_pos[0]), float(a_pos[1]), float(a_pos[2])),
            direction=(float(d[0]), float(d[1]), float(d[2])),
            atom=labels[new_of[a_idx]],
            annotations={"order": orders[idx]},
        )
    pga._record(atoms=len(elements), bonds=len(bonds))
    return Geom(
        scene=out_scene,
        ports=dict(sorted(port_d.items())),
        meta={
            "units": "A",
            "name": name or smiles,
            "smiles": smiles,
            "conf_seed": conf_seed,
        },
    )


def _frag_from_scene(scene: Any, ports: Any, name: str | None) -> Geom:
    if not (
        hasattr(scene, "atoms") and hasattr(scene, "cell") and hasattr(scene, "bonds")
    ):
        raise BadInput(
            f"mol.frag(scene=) wants a precis.structure Scene, got "
            f"{type(scene).__name__}",
            next="build a Scene, or use mol.frag(smiles=...)",
        )
    sc = copy.deepcopy(scene)
    maker = pga._leaf()
    for atom in sc.atoms.values():
        if atom.maker is None:
            atom.maker = maker
    port_d: dict[str, Port] = {}
    for pname, p in (ports or {}).items():
        port = p if isinstance(p, Port) else Port(name=pname, **dict(p))
        if port.atom is not None and port.atom not in sc.atoms:
            raise BadInput(
                f"port {pname!r}: no atom {port.atom!r} in the scene",
                options=sorted(sc.atoms)[:20],
                next="bind the port to an existing atom label",
            )
        if port.atom is not None and (port.pos is None or port.direction is None):
            at = _valence.cart(sc, port.atom)
            free = _valence.free_direction(sc, port.atom, prefer_h=False)
            pos = port.pos or (float(at[0]), float(at[1]), float(at[2]))
            direction = port.direction or (
                float(free[0]),
                float(free[1]),
                float(free[2]),
            )
            port = Port(
                name=port.name or pname,
                pos=pos,
                direction=direction,
                roles=port.roles,
                atom=port.atom,
                annotations=dict(port.annotations) or {"order": 1.0},
            )
        elif port.pos is None:
            raise BadInput(
                f"port {pname!r} needs an atom or a pos",
                next="ports={'a1': {'atom': '<label>'}}",
            )
        port_d[pname] = port
    pga._record(atoms=len(sc.atoms), bonds=len(sc.bonds))
    return Geom(
        scene=sc,
        ports=port_d,
        meta={"units": "A", "name": name or "scene"},
    )
