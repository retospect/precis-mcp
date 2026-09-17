"""The atomic mode's Å↔m seam — where it is, and that it is only there.

nm-se-merge.md's third in-scope build item and two of its acceptance
criteria. Before the merge, ``precis_nm.generators`` emitted *bare-number*
Å envelope text and ``precis_nm.handler._envelope_A_to_m`` re-parsed it,
multiplied by ``1e-10`` and re-emitted it in metres, so the conversion
lived on the handler path as a second seam alongside the real ingest
boundary. Now the generators write their own unit into the DSL text
(:func:`precis_se.atomic.generators.fmt_length_A`) and
``precis.cad.dsl.parse(..., require_units=True)`` does the multiply at the
*one* boundary — the same one a hand-authored ``add_block`` envelope
crosses.

Two things are pinned here:

1. **Every generator's envelope is unit-suffixed and round-trips** — the
   emitted text parses under the units-required grammar, and the parsed
   metres value is exactly the Å number × 1e-10.
2. **The conversion factor appears nowhere else in ``precis_se``** — an
   AST-free literal grep with a named allowlist (the acceptance
   criterion's own shape). The allowlist is the *point* of the test: the
   places an Å↔m multiply is legitimate are named, so the next one has to
   be argued for in this file rather than added quietly. The survivor
   today are the two the merge doc names (nm-se-merge.md "Explicitly NOT
   in scope"): ``atomic/mechanics.py``, where interaction physics keeps
   its Å/nN/eV signatures — cost terms over the geometry, not lengths in
   it — and ``atomic/validate.py``, whose ``envelope_fit`` compares a
   design-space envelope (m) against a bound ``structure`` scene's atoms
   (Å), the permanent structure-enclave crossing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from precis.cad import dsl as cad_dsl
from precis_se.atomic.generators import GENERATORS, GeneratedBlock
from precis_se.atomic.generators._types import ENVELOPE_UNIT, fmt_length_A

_SE = Path(__file__).resolve().parent.parent / "src" / "precis_se"

#: Å→m (``1e-10``) / m→Å (``1e10``) literals, however spelled.
_FACTOR = re.compile(r"1(\.0)?e-?10\b")

#: Modules allowed to carry the factor, each with its reason. Paths are
#: relative to ``src/precis_se``.
_ALLOWED: dict[str, str] = {
    "atomic/mechanics.py": (
        "interaction physics keeps Å/nN/eV signatures — cost terms over "
        "the geometry, not lengths in it (merge doc: NOT in scope)"
    ),
    "atomic/validate.py": (
        "envelope_fit's design(m)↔atomistic(Å) conversion against a bound "
        "structure scene — the permanent structure-enclave crossing"
    ),
}

#: One legal param set per registered generator, cheapest realization of
#: each family (the seam is per-envelope, not per-atom-count).
_CASES: dict[str, dict[str, object]] = {
    "cnt": {"n": 5, "m": 0, "length_A": 10.0},
    "fullerene": {"atoms": 60},
    "cone": {"pentagons": 2, "length_A": 12.0},
    "cyclodextrin": {"variant": "alpha"},
    "hexfold": {
        "spec": (
            "hexfold 0.1\n\nlattice: element=C sigma=1.42\n\n"
            "origin post\npost: tube(5,5,len=2)\n"
        )
    },
}


def _sources() -> list[tuple[str, str]]:
    return [
        # as_posix, not str: _ALLOWED keys are written with "/" and this
        # must match them on Windows too (str gives "atomic\\mechanics.py").
        (p.relative_to(_SE).as_posix(), p.read_text(encoding="utf-8"))
        for p in sorted(_SE.rglob("*.py"))
    ]


def test_every_generator_is_exercised_by_this_file() -> None:
    """The registry is the roster: a new generator must state its params
    here, or its envelope's units go unchecked."""
    assert set(_CASES) == set(GENERATORS)


@pytest.mark.parametrize("name", sorted(_CASES))
def test_generator_envelope_carries_its_unit_on_every_length(name: str) -> None:
    block = GENERATORS[name](dict(_CASES[name]))
    assert isinstance(block, GeneratedBlock)
    env = block.envelope
    alias, _, params = env.partition(":")
    assert params, f"{name}: envelope {env!r} has no params"
    # Every numeric token in the params half must be followed by the unit.
    numbers = re.findall(r"\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", params)
    assert numbers, f"{name}: envelope {env!r} has no numbers"
    assert env.count(ENVELOPE_UNIT) == len(numbers), (
        f"{name}: envelope {env!r} has {len(numbers)} numbers but "
        f"{env.count(ENVELOPE_UNIT)} {ENVELOPE_UNIT} suffixes — a bare "
        "number would be multiplied by nothing at the ingest boundary"
    )
    assert alias in {"cyl", "sphere", "tcone", "torus"}


@pytest.mark.parametrize("name", sorted(_CASES))
def test_generator_envelope_parses_to_metres_at_the_one_boundary(name: str) -> None:
    """The round trip the merge's acceptance criterion names: Å-suffixed
    text in, metres out, with no handler-side pre-conversion."""
    block = GENERATORS[name](dict(_CASES[name]))
    strict = cad_dsl.parse(block.envelope, require_units=True)
    # Same text read as bare numbers = the generator's own Å figures.
    bare = cad_dsl.parse(block.envelope.replace(ENVELOPE_UNIT, ""))
    for key, metres in strict.params.items():
        if key == "n":  # dimensionless count — never converted
            continue
        assert metres == pytest.approx(bare.params[key] * 1e-10, rel=1e-12)
        assert 1e-12 < metres < 1e-7, f"{name}.{key} = {metres} is not a nanoscale m"


def test_a_bare_number_envelope_is_rejected_not_silently_read_as_metres() -> None:
    """Why the suffix has to be in the emitted text at all: the boundary
    refuses a bare number rather than guessing a unit."""
    with pytest.raises(Exception) as exc:
        cad_dsl.parse("cyl:r5h10", require_units=True)
    assert "unit" in str(exc.value).lower()


def test_fmt_length_A_writes_the_unit() -> None:
    assert fmt_length_A(1.7) == f"1.7{ENVELOPE_UNIT}"
    assert fmt_length_A(0.000001) == f"0{ENVELOPE_UNIT}"  # 4-decimal rounding


def test_the_angstrom_factor_lives_only_in_the_allowlisted_modules() -> None:
    offenders = [
        f"{rel}:{i}"
        for rel, text in _sources()
        if rel not in _ALLOWED
        for i, line in enumerate(text.splitlines(), start=1)
        if _FACTOR.search(line)
    ]
    assert not offenders, (
        "an Å↔m conversion factor (1e-10 / 1e10) appeared outside the two "
        "modules allowed to hold one — nm-se-merge.md's acceptance "
        "criterion. The design layer is SI metres and the ONE ingest "
        "boundary (cad.dsl's require_units parse) does the multiply; a new "
        "factor here is a second seam. If it is genuinely a physics "
        "signature or the structure-enclave crossing, add it to _ALLOWED "
        "with its reason:\n" + "\n".join(offenders)
    )


def test_the_allowlist_names_modules_that_exist_and_use_the_factor() -> None:
    """A stale allowlist entry hides the next real regression behind a
    path that no longer matches anything (the epsilon gate's own rule)."""
    by_path = dict(_sources())
    for rel in _ALLOWED:
        assert rel in by_path, f"allowlisted module is gone: {rel}"
        assert _FACTOR.search(by_path[rel]), (
            f"{rel} no longer holds an Å↔m factor — drop its allowlist entry"
        )
