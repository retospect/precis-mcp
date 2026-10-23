"""extXYZ reader — the workspace import path's atomistic geometry format.

The sibling of :mod:`precis.structure.importers.cif` for
``blocks/<name>.extxyz`` files (docs/backlog/structure-workspaces.md
§Import boundary): parses the extended-XYZ form
:func:`precis.structure.export.to_extxyz` writes — ``Lattice``/``pbc``
header, ``Properties``-declared columns — plus the two precis-native
per-atom columns the workspace writer emits:

* ``label:S:1`` — the atom's stable label, kept verbatim so the manifest's
  port→atom bindings and ``AtomPath`` names match across runs.
* ``maker:I:1`` — the construction-trace frame id that made the atom
  (``struct_atoms.maker``); ``-1`` reads back as "no maker".

Bonds ride a JSON sidecar — extXYZ has no bond concept:
``blocks/<name>.bonds.json`` is ``{"schema": 1, "bonds": [{"i": <label>,
"j": <label>, "order"?: 1.0, "kind"?: "pairwise", "maker"?: <id>}]}``
(:func:`bonds_from_json`).

No ASE dependency: the label/maker columns are strings/ints ASE's own
reader would not map onto the Scene anyway, and the format we accept is
the one our writer emits.
"""

from __future__ import annotations

import json
import re

import numpy as np

from precis.errors import BadInput
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Bond, Scene

_HEADER_KV_RE = re.compile(r'(\w+)=(?:"([^"]*)"|(\S+))')


def _parse_header(line: str) -> dict[str, str]:
    return {
        m.group(1): (m.group(2) if m.group(2) is not None else m.group(3))
        for m in _HEADER_KV_RE.finditer(line)
    }


def _parse_properties(spec: str) -> list[tuple[str, str, int]]:
    """``species:S:1:pos:R:3:label:S:1:maker:I:1`` → column descriptors."""
    parts = spec.split(":")
    if len(parts) % 3:
        raise BadInput(f"extxyz: malformed Properties spec {spec!r}")
    return [
        (parts[i], parts[i + 1], int(parts[i + 2])) for i in range(0, len(parts), 3)
    ]


def extxyz_to_scene(text: str, *, fallback_cell: float = 20.0) -> Scene:
    """Parse extXYZ text into a :class:`Scene` (Cartesian → fractional).

    Requires ``species`` + ``pos`` columns; tolerates and skips columns it
    does not know. Labels come from the ``label`` column verbatim —
    duplicate labels refuse; a file with no ``label`` column mints
    ``a<El><n>`` in file order. ``maker`` ints map to ``Atom.maker``
    (``-1`` → ``None``). Without a ``Lattice`` header the atoms go into a
    non-periodic box of ``fallback_cell`` Å centred on the origin — the
    same "comfortably contains, never wraps" posture
    :func:`precis_se.atomic.generate.generated_cell` takes."""
    lines = text.splitlines()
    try:
        n = int(lines[0].strip())
    except (IndexError, ValueError) as exc:
        raise BadInput("extxyz: first line must be the atom count") from exc
    if len(lines) < n + 2:
        raise BadInput(
            f"extxyz: {n} atoms declared but only {len(lines) - 2} rows follow"
        )
    header = _parse_header(lines[1])
    cols = _parse_properties(header.get("Properties", ""))
    col_names = [c[0] for c in cols]
    if "species" not in col_names or "pos" not in col_names:
        raise BadInput(
            "extxyz: Properties must declare 'species' and 'pos' columns, "
            f"got {col_names}"
        )
    lat_raw = header.get("Lattice")
    if lat_raw:
        try:
            vals = [float(x) for x in lat_raw.split()]
            assert len(vals) == 9
            lattice = np.array(vals, dtype=float).reshape(3, 3)
        except (ValueError, AssertionError) as exc:
            raise BadInput("extxyz: Lattice must be 9 floats") from exc
        pbc_raw = header.get("pbc", "T T T").split()
        pbc_list = [x.upper() in ("T", "TRUE", "1") for x in pbc_raw[:3]]
        while len(pbc_list) < 3:
            pbc_list.append(True)
        cell = Cell(lattice, (pbc_list[0], pbc_list[1], pbc_list[2]))
    else:
        cell = Cell.from_lengths_angles(
            fallback_cell, fallback_cell, fallback_cell, pbc=(False, False, False)
        )
    # Column → flat offsets.
    offsets: dict[str, int] = {}
    pos = 0
    for name, _kind, width in cols:
        offsets.setdefault(name, pos)
        pos += width
    scene = Scene(cell=cell)
    for i, raw in enumerate(lines[2 : 2 + n]):
        toks = raw.split()
        if len(toks) < pos:
            raise BadInput(f"extxyz: row {i + 1} has {len(toks)} cols, need {pos}")
        element = toks[offsets["species"]]
        try:
            cart = np.array(
                [float(toks[offsets["pos"] + k]) for k in range(3)], dtype=float
            )
        except ValueError as exc:
            raise BadInput(f"extxyz: row {i + 1} bad positions: {raw!r}") from exc
        label = toks[offsets["label"]] if "label" in offsets else None
        maker = None
        if "maker" in offsets:
            try:
                m = int(toks[offsets["maker"]])
            except ValueError as exc:
                raise BadInput(f"extxyz: row {i + 1} bad maker") from exc
            maker = m if m >= 0 else None
        if label is None:
            label = scene.next_label(element)
        if label in scene.atoms:
            raise BadInput(f"extxyz: duplicate atom label {label!r} (row {i + 1})")
        scene.atoms[label] = Atom(
            label=label,
            element=element,
            frac=cell.wrap(cell.cart_to_frac(cart)),
            maker=maker,
        )
    return scene


def bonds_from_json(text: str) -> list[Bond]:
    """Parse a ``*.bonds.json`` sidecar into :class:`Bond`s (labels —
    resolved against the block's scene by the caller)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BadInput(f"bonds.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise BadInput("bonds.json: expected {'schema': 1, 'bonds': [...]}")
    for key in data:
        if key not in ("schema", "bonds"):
            raise BadInput(f"bonds.json: unknown key {key!r}")
    bonds = data.get("bonds")
    if not isinstance(bonds, list):
        raise BadInput("bonds.json: 'bonds' must be a list")
    out: list[Bond] = []
    for i, raw in enumerate(bonds):
        if not isinstance(raw, dict):
            raise BadInput(f"bonds.json: bonds[{i}] must be an object")
        for key in raw:
            if key not in ("i", "j", "order", "kind", "maker"):
                raise BadInput(f"bonds.json: bonds[{i}] unknown key {key!r}")
        i_lab, j_lab = str(raw.get("i") or ""), str(raw.get("j") or "")
        if not i_lab or not j_lab:
            raise BadInput(f"bonds.json: bonds[{i}] needs 'i' and 'j' labels")
        maker = raw.get("maker")
        out.append(
            Bond(
                i=i_lab,
                j=j_lab,
                order=float(raw.get("order", 1.0)),
                kind=str(raw.get("kind") or "pairwise"),
                maker=int(maker) if maker is not None else None,
            )
        )
    return out
