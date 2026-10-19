"""CIF text -> :class:`Scene`, the shared parser behind the CIF ``put`` and the MOF adapters.

Two layers, like the Catalysis-Hub adapter:

* :func:`cif_to_scene` — pure, no I/O. Caps the text size before any
  parse, reads the CIF blocks with ASE's linear tokeniser
  (``ase.io.cif.parse_cif``), refuses a block whose site table times its
  symmetry operations would expand past the atom cap, then expands it
  (``CIFBlock.get_atoms``) and checks the true atom count. ASE's symmetry
  expansion is what hangs on a huge cell, so the estimate runs before it.
  A 3D framework keeps the CIF's ``pbc=(True, True, True)``; nothing here
  is slab-specific.
* :func:`fetch_cif_url` — the network layer. One GET through
  :func:`precis.utils.safe_fetch.safe_get` (SSRF-checked, redirects
  re-validated per hop), refused over :data:`MAX_CIF_BYTES`.

ASE is the optional ``[dft]`` extra; a missing one raises
:class:`CifUnsupported` with the install hint, never a bare ImportError.
Refusals raise :class:`CifRefused`, whose ``next`` the handler forwards.

Unit enclave (``precis.structure`` package docstring): Å, as CIF stores it.
"""

from __future__ import annotations

import io
import warnings
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..scene import Scene

#: Largest CIF text accepted, checked before any parse. A 5,000-atom P1
#: cell is about 0.4 MB of ``_atom_site`` rows; 2 MiB leaves room for
#: comments and property loops.
MAX_CIF_BYTES = 2 * 1024 * 1024

#: Largest expanded cell accepted. CoRE MOF 2024 and QMOF cells sit well
#: under this; a bigger one is a supercell or a protein, not a seed.
MAX_CIF_ATOMS = 5000

#: Most symmetry operations any space group has (Fm-3m and friends,
#: centring included).
_MAX_SPACEGROUP_ORDER = 192

_NEXT_TEXT = (
    "put(kind='structure', id='<slug>', args={'cif': '<CIF text>'}) or "
    "args={'cif_url': 'https://…/x.cif'}; the text must hold a data_ block "
    "with _cell_length_a/b/c and an _atom_site loop"
)
_NEXT_SMALLER = (
    f"pass a primitive or asymmetric cell with at most {MAX_CIF_ATOMS} atoms "
    "after symmetry expansion"
)


class CifRefused(ValueError):
    """A CIF that is not parsed: too large, not CIF, or no structure in it."""

    def __init__(self, message: str, *, next: str = _NEXT_TEXT) -> None:
        super().__init__(message)
        self.next = next


class CifUnsupported(RuntimeError):
    """ASE (the ``[dft]`` extra) is not installed, so CIF cannot be parsed."""


def _require_ase_cif() -> Any:
    try:
        from ase.io import cif as ase_cif
    except ImportError as exc:
        raise CifUnsupported(
            "CIF import needs ASE: pip install 'precis-mcp[dft]'"
        ) from exc
    return ase_cif


def _site_count(block: Any) -> int:
    get = block.get
    for tag in ("_atom_site_fract_x", "_atom_site_cartn_x", "_atom_site_label"):
        col = get(tag)
        if col is not None:
            return len(col)
    return 0


def _symop_count(block: Any) -> int:
    get = block.get
    for tag in (
        "_space_group_symop_operation_xyz",
        "_space_group_symop.operation_xyz",
        "_symmetry_equiv_pos_as_xyz",
    ):
        col = get(tag)
        if col is not None:
            return max(1, len(col))
    # No symop loop: ASE expands from the space-group name or number, so
    # bound by that group's operation count (centring included). A group
    # ASE cannot resolve is bounded by the largest order, 192.
    try:
        return max(1, int(block.get_spacegroup(None).nsymop))
    except Exception:
        return _MAX_SPACEGROUP_ORDER


def cif_to_scene(text: str) -> Scene:
    """Parse the first CIF block that holds a structure into a :class:`Scene`.

    Raises :class:`CifRefused` for an oversized text, a non-CIF body, a CIF
    with no cell or sites, or a cell over :data:`MAX_CIF_ATOMS`; raises
    :class:`CifUnsupported` when ASE is missing.
    """
    if not isinstance(text, str) or not text.strip():
        raise CifRefused("CIF import got an empty body")
    size = len(text.encode("utf-8"))
    if size > MAX_CIF_BYTES:
        raise CifRefused(
            f"CIF text is {size} bytes, over the {MAX_CIF_BYTES}-byte cap",
            next=_NEXT_SMALLER,
        )
    lowered = text.lower()
    if "data_" not in lowered or "_cell_length_a" not in lowered:
        raise CifRefused(
            "body is not CIF: no data_ block with _cell_length_a was found"
        )
    ase_cif = _require_ase_cif()
    try:
        blocks = [
            b
            for b in ase_cif.parse_cif(io.BytesIO(text.encode("utf-8")))
            if b.has_structure()
        ]
    except Exception as exc:  # ASE raises bare ValueError/KeyError/IndexError
        raise CifRefused(f"CIF text did not parse: {exc}") from exc
    if not blocks:
        raise CifRefused("CIF has no block with a cell and an _atom_site loop")
    block = blocks[0]
    sites, symops = _site_count(block), _symop_count(block)
    estimate = sites * symops
    if estimate > MAX_CIF_ATOMS:
        raise CifRefused(
            f"CIF expands to up to {estimate} atoms "
            f"({sites} sites x {symops} symmetry "
            f"operations), over the {MAX_CIF_ATOMS}-atom cap",
            next=_NEXT_SMALLER,
        )
    try:
        with warnings.catch_warnings():
            # ASE warns that it does not interpret a P1 cell's crystal system;
            # P1 needs no setting, and the warning is noise to the caller.
            warnings.filterwarnings(
                "ignore", message="crystal system", category=UserWarning
            )
            atoms = block.get_atoms()
    except Exception as exc:
        raise CifRefused(f"CIF structure could not be built: {exc}") from exc
    if len(atoms) == 0:
        raise CifRefused("CIF structure has no atoms")
    if len(atoms) > MAX_CIF_ATOMS:
        raise CifRefused(
            f"CIF cell has {len(atoms)} atoms, over the {MAX_CIF_ATOMS}-atom cap",
            next=_NEXT_SMALLER,
        )
    from ..search import scene_from_ase

    return scene_from_ase(atoms)


def fetch_cif_url(url: str, *, timeout: float = 30.0) -> str:
    """GET ``url`` through :func:`safe_get` and return the CIF text.

    Refuses a non-2xx answer and a body over :data:`MAX_CIF_BYTES` (by the
    ``Content-Length`` header, then by the received length). ``safe_get``
    reads the whole body, so the client timeout bounds a server that lies
    about its length.
    """
    from ...utils import safe_fetch
    from ...utils.http import (
        POLICIES,
        VendorError,
        http_client,
        polite_safe_get,
        polite_user_agent,
    )
    from ...utils.rate_limit import RateLimited

    try:
        with http_client(timeout=timeout, user_agent=polite_user_agent()) as client:
            resp = polite_safe_get(client, url, policy=POLICIES["cif-fetch"])
            declared = resp.headers.get("Content-Length")
            if declared is not None and declared.isdigit():
                if int(declared) > MAX_CIF_BYTES:
                    raise CifRefused(
                        f"{url} declares {declared} bytes, over the "
                        f"{MAX_CIF_BYTES}-byte cap",
                        next=_NEXT_SMALLER,
                    )
            if resp.status_code >= 400:
                raise CifRefused(
                    f"{url} answered HTTP {resp.status_code}",
                    next="check the URL, or pass the CIF text as args={'cif': ...}",
                )
            body = resp.content
    except safe_fetch.SsrfBlocked as exc:
        raise CifRefused(
            f"URL refused: {exc}",
            next="use a public http(s) URL, or pass the CIF text as args={'cif': ...}",
        ) from exc
    except (VendorError, RateLimited) as exc:
        raise CifRefused(
            f"{url} unavailable: {exc}",
            next="retry shortly, or pass the CIF text as args={'cif': ...}",
        ) from exc
    if len(body) > MAX_CIF_BYTES:
        raise CifRefused(
            f"{url} returned {len(body)} bytes, over the {MAX_CIF_BYTES}-byte cap",
            next=_NEXT_SMALLER,
        )
    return body.decode("utf-8", errors="replace")


__all__ = [
    "MAX_CIF_ATOMS",
    "MAX_CIF_BYTES",
    "CifRefused",
    "CifUnsupported",
    "cif_to_scene",
    "fetch_cif_url",
]
