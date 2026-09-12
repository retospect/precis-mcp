"""AST gate: no new absolute LENGTH epsilon constant in cad/se/nm/structsolve.

units-policy-cutover.md item 4 (the relative-tolerance audit) + its
acceptance criterion: "no absolute LENGTH epsilon constants remain in
cad/se/nm/structsolve comparison paths (each is derived from a governing
length); an AST-walk or grep test pins this, with an explicit exempt list
for dimensionless/angular epsilons."

**What this catches.** A module-level constant assignment (``NAME = ...``
or ``NAME: float = ...``) whose name looks like a tolerance/epsilon
(``EPS``/``TOL`` substring, case-insensitive) in the policed packages. An
AST walk, not a grep, so it only sees real assignments — not the same
substring inside a comment, string, or f-string — per the acceptance
criterion's own preference ("AST-walk *or* grep... catch numeric literals
compared against length-valued expressions is too hard; instead pin
MODULE-LEVEL absolute epsilon CONSTANTS").

**What it does not catch** (by design, not oversight): inline literals
(``abs(x) <= 1e-9``) and function/dataclass-field defaults — the shipped
relative-epsilon pattern computes those from a governing length at the
call site (:func:`precis.cad.primitives._linear_eps`,
:func:`precis.cad.relate._governing_length`), so there is no stable
module-level name to pin; a caller-visible constant is exactly the shape
the "silently absolute default" hazard takes (``LINEAR_EPS = 1e-6``,
``CONTACT_TOL_MM``), so that is what this gate polices.

**The exemption rule**, applied in order:

1. Path-based enclaves (pcb, the atomistic layers, generator internals,
   export/serializer modules) that DECIDED to keep a non-SI absolute
   convention, per units-policy-cutover.md's decisions log.
2. Name signals "already relative" (``REL``/``RTOL`` substring — the
   codebase's own naming convention for a fraction-of-a-governing-length
   or relative-tolerance constant: ``LINEAR_REL_EPS``, ``CONTACT_TOL_REL``,
   ``_GRAD_REL_EPS``, ``_SINGULAR_RTOL``, ...).
3. Name signals angle/radians (``_RAD``/``_DEG`` suffix) — angles have no
   length scale to be relative to.
4. The explicit :data:`_EXEMPT_NAMES` allowlist below, each entry
   commented with its reason — dimensionless fractions, a genuinely
   scale-free vector-zero guard, and the one documented author-stated
   absolute-with-unit constant the audit decided to keep.

Anything left over is a NEW (or newly-noticed) absolute length epsilon —
exactly the shape of the bug this item exists to close (gr335192,
gr334785: ``LINEAR_EPS = 1e-6`` culling every face of a nanometre box).
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"

#: Packages this gate polices (units-policy-cutover.md item 4's target
#: set, verbatim).
_POLICED_PACKAGES = (
    _SRC / "precis" / "cad",
    _SRC / "precis_se",
    _SRC / "precis_nm",
    _SRC / "precis" / "structsolve",
)

#: Enclaves exempt WHOLESALE — each ruled by name in
#: units-policy-cutover.md's decisions log:
#: - ``precis/pcb`` — mm-native enclave (self-naming ``_mm`` identifiers;
#:   every cross-package API converts to SI).
#: - ``precis_nm/generators`` — atomistic-internal (bond lengths, VDW
#:   margins, cavity radii): physics constants that get an explicit unit
#:   in the name, per the nm round's "physical Å constants STAY" ruling.
#: - ``precis/structure`` — the Å-native atomistic unit enclave
#:   (structure-unit-enclave.md): ASE's Å/eV-native Atoms/calculators
#:   thread through it, so forcing SI would add a conversion per ASE
#:   call for no payoff.
_ENCLAVE_PARTS = (
    ("precis", "pcb"),
    ("precis_nm", "generators"),
    ("precis", "structure"),
)

#: Export/serializer modules legitimately hold a fixed unit-conversion
#: factor (STL/OpenSCAD ×1000, nm's Å storage ×1e-10) — the audit's
#: explicitly-decided single conversion point at the export boundary, not
#: a comparison epsilon.
_EXPORT_MODULE_NAMES = frozenset({"export.py", "persist.py"})

#: Explicit allowlist: dimensionless/angular constants (units-policy-
#: cutover.md's own list, plus the dossier's structsolve inventory,
#: §2), a scale-free vector-zero guard, and the one documented
#: author-stated absolute-with-unit constant kept on purpose. Every entry
#: must carry its reason.
_EXEMPT_NAMES = frozenset(
    {
        # -- angular / dimensionless (acceptance criterion's own list) --
        "ANGULAR_EPS",  # radians; parallel/coincident-plane test
        "_AM_EPS",  # structsolve additive-manufacturing filter, dimensionless
        "_AM_SMIN_EPS",  # ditto
        "_FILTER_GAMMA",  # structsolve density-filter exponent, dimensionless
        "_SINGULAR_RTOL",  # relative by construction (rtol)
        # -- structsolve's broader dimensionless/count inventory (dossier §2) --
        "_AM_P",
        "_AM_STENCIL",
        "_AM_Q",
        "_OC_MOVE",
        "_OC_ETA",
        "_GYROID_MEAN_GRAD",
        "_DEFAULT_NU",
        # NOTE: the dossier's structsolve inventory also names ``emin`` and
        # ``cg_tol`` as dimensionless — they are a dataclass field and a
        # function default, not module-level constants, so this
        # module-level-only gate never sees them and they need no entry
        # here (see "what this does not catch" above).
        # -- scale-free by construction (operates only on order-1 inputs) --
        "_UNIT_VEC_EPS",  # normalize()'s zero-vector guard — see vec.py docstring
        # -- documented author-stated absolute, decided to keep --
        # CONTACT_TOL_MM is an explicit, named, opt-in absolute band for a
        # caller that states one; CONTACT_TOL_REL (name contains "REL",
        # auto-exempt) is the scale-relative default every unqualified
        # caller actually gets. Keeping the absolute constant nameable is
        # the "author-stated ... accepted as absolute-with-unit" carve-out
        # in the acceptance criteria, not a silent default.
        "CONTACT_TOL_MM",
    }
)


def _is_enclave(path: Path) -> bool:
    parts = path.relative_to(_SRC).parts
    return any(
        len(parts) >= len(enclave) and parts[: len(enclave)] == enclave
        for enclave in _ENCLAVE_PARTS
    )


def _looks_like_tolerance_name(name: str) -> bool:
    upper = name.upper()
    return "EPS" in upper or "TOL" in upper


def _already_relative_by_name(name: str) -> bool:
    upper = name.upper()
    return "REL" in upper or "RTOL" in upper


def _looks_angular_by_name(name: str) -> bool:
    upper = name.upper()
    return upper.endswith("_RAD") or upper.endswith("_DEG") or "_RAD_" in upper


def _module_level_constant_names(path: Path) -> list[tuple[str, int]]:
    """``(name, lineno)`` for every module-level ``NAME = ...`` /
    ``NAME: T = ...`` assignment in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[str, int]] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out.append((target.id, node.lineno))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.append((node.target.id, node.lineno))
    return out


def _offending_constants() -> list[str]:
    offenders: list[str] = []
    for package in _POLICED_PACKAGES:
        for path in sorted(package.rglob("*.py")):
            if _is_enclave(path):
                continue
            if path.name in _EXPORT_MODULE_NAMES:
                continue
            for name, lineno in _module_level_constant_names(path):
                if not _looks_like_tolerance_name(name):
                    continue
                if _already_relative_by_name(name):
                    continue
                if _looks_angular_by_name(name):
                    continue
                if name in _EXEMPT_NAMES:
                    continue
                rel = path.relative_to(_SRC)
                offenders.append(f"{rel}:{lineno} {name}")
    return offenders


def test_no_new_absolute_length_epsilon_constants() -> None:
    offenders = _offending_constants()
    assert not offenders, (
        "found an absolute-looking epsilon/tolerance constant in "
        "cad/se/nm/structsolve with no relative-naming signal and no "
        "exemption — units-policy-cutover.md item 4 requires every "
        "system-supplied LENGTH epsilon to be derived from a governing "
        "length (feature size, else bbox diagonal), not a bare absolute "
        "constant. If this is genuinely dimensionless/angular or an "
        "enclave-internal convention, add it (with its reason) to "
        "_EXEMPT_NAMES in this test — never leave it unexplained:\n"
        + "\n".join(offenders)
    )


def test_the_exempt_list_still_names_real_constants() -> None:
    """Every :data:`_EXEMPT_NAMES` entry should still exist somewhere in
    the policed tree — an allowlist entry for a constant that got renamed
    or deleted is dead weight that hides the next real regression behind
    a name that no longer matches anything."""
    all_names = {
        name
        for package in _POLICED_PACKAGES
        for path in sorted(package.rglob("*.py"))
        for name, _lineno in _module_level_constant_names(path)
    }
    stale = sorted(_EXEMPT_NAMES - all_names)
    assert not stale, f"exempt names no longer defined anywhere: {stale}"
