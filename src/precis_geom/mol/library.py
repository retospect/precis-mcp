"""``mol.lib`` — a small named set of functional groups, spacers and a
cage, each a :class:`~precis_geom.assembly.Geom` from :func:`mol.frag`.

Groups carry one port ``a1`` (the open valence); the spacers ``CH2n`` /
``C2H4`` carry two (``a1``, ``a2``, one at each end); ``c60`` is the
``GENERATORS['fullerene']`` cage and has no port — it is a guest, placed
with ``out.instance`` or wrapped by ``mol.frag(scene=..., ports=...)``.
The spacers are se-seam-spacers' (CH2)n / C2H4. Names beyond this set: the chem-name-lookup verb through
the read MCP.
"""

from __future__ import annotations

from typing import Any

from precis.errors import BadInput
from precis_geom import assembly as pga
from precis_geom import steps as _steps
from precis_geom.assembly import Geom
from precis_geom.mol.fragment import build_salt, frag

#: name (as written in the spec) → SMILES for the fixed groups.
_FIXED: dict[str, str] = {
    "OH": "[*:1]O",
    "NH2": "[*:1]N",
    "CH3": "[*:1]C",
    "COOH": "[*:1]C(=O)O",
    "Ph": "[*:1]c1ccccc1",
}
#: Upper bound on chain length for the parametric names (n=); longer
#: chains embed poorly and are a polymer job, not a decorator.
_MAX_N = 24

NAMES = (*_FIXED, "PEG", "CH2n", "C2H4", "c60")


def _count(name: str, params: dict[str, Any]) -> int:
    n = params.pop("n", None)
    if n is None:
        raise BadInput(
            f"mol.lib({name!r}) needs n=",
            next=f"mol.lib({name!r}, n=4)",
        )
    try:
        k = int(n)
    except (TypeError, ValueError):
        k = 0
    if k != n or not 1 <= k <= _MAX_N:
        raise BadInput(
            f"mol.lib({name!r}): n must be an integer in 1..{_MAX_N}, got {n!r}",
            next=f"mol.lib({name!r}, n=4)",
        )
    return k


@_steps.step(
    name="lib",
    frame="lib",
    salt=lambda: build_salt("precis_se.atomic.generators", "hexfold"),
)
def lib(name: str, **params: Any) -> Geom:
    """The named library entry as a :class:`Geom`.

    ``OH``, ``NH2``, ``CH3``, ``COOH``, ``Ph`` — one-port groups.
    ``PEG`` (``n=`` ethylene-glycol units, ``-(OCH2CH2)n-OH``) — one port.
    ``CH2n`` (``n=``) / ``C2H4`` — two-port spacers. ``c60`` — the
    fullerene cage. ``conf_seed=`` seeds the embed (default 0).

    A cached step (:mod:`precis_geom.steps`): the entry built from one call
    site is a hit at another."""
    key = {k.lower(): k for k in NAMES}.get(str(name).lower())
    if key is None:
        raise BadInput(
            f"mol.lib: unknown entry {name!r}",
            options=list(NAMES),
            next="pick one of the options, or build it: mol.frag(smiles='[*:1]...')",
        )
    p = dict(params)
    seed = int(p.pop("conf_seed", 0))
    if key in _FIXED:
        _no_extra(key, p)
        return frag(_FIXED[key], name=key, conf_seed=seed)
    if key == "PEG":
        n = _count(key, p)
        _no_extra(key, p)
        return frag("[*:1]" + "OCC" * n + "O", name=f"PEG_{n}", conf_seed=seed)
    if key == "CH2n":
        n = _count(key, p)
        _no_extra(key, p)
        return frag("[*:1]" + "C" * n + "[*:2]", name=f"CH2n_{n}", conf_seed=seed)
    if key == "C2H4":
        _no_extra(key, p)
        return frag("[*:1]CC[*:2]", name="C2H4", conf_seed=seed)
    _no_extra(key, p)
    return _c60()


def _no_extra(name: str, params: dict[str, Any]) -> None:
    if params:
        raise BadInput(
            f"mol.lib({name!r}) takes no {sorted(params)}",
            next="drop the extra argument",
        )


def _c60() -> Geom:
    from precis_se.atomic.generators import GENERATORS

    block = GENERATORS["fullerene"]({"atoms": 60})
    g = pga.geom(block, meta={"name": "c60"})
    if g.scene is None:  # pragma: no cover - a generated block is a scene
        raise BadInput("mol.lib('c60'): the fullerene generator returned no atoms")
    maker = pga._leaf()
    for atom in g.scene.atoms.values():
        atom.hybridization = block.hybridization
        atom.maker = maker
    for bond in g.scene.bonds:
        bond.maker = maker
    pga._record(atoms=len(g.scene.atoms), bonds=len(g.scene.bonds))
    return g
