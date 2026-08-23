"""``gates.check_claim_sentence`` — the claim-sentence lint's blocking
half (``docs/backlog/nanopub-corpus-remediation.md`` Phase 1). Table-
driven over the block-vs-advise split (``gates._BLOCKING_LINT_CODES``);
the DB-backed section reuses the existing mint-gate fixtures
(``_seed_paper``/``_seed_hub``/``_gate_slugs`` from
``test_nanopub_gates_mint``) to confirm the gate is actually wired into
``run_mint_gates``, not just correct in isolation."""

from __future__ import annotations

from typing import Any

from precis.nanopub import gates
from tests.test_nanopub_gates_mint import (
    _gate_slugs,
    _payload,
    _seed_hub,
    _seed_paper,
)

# One sentence per BLOCKING code, each crafted to trip *only* that code
# (evidence verb + epistemic-mode token + terminal period + single short
# clause + no author name + no dangling reference present everywhere
# else, per gates.py's `_BLOCKING_LINT_CODES` comment). The table is
# exhaustive over the blocking set — `test_blocking_set_matches_the
# _fixture_table` fails if a code is added without a fixture.
_ONE_VIOLATION_PER_CODE: dict[str, str] = {
    "not-falsifiable": (
        "DFT shows NUPACK is a software suite for nucleic acid design."
    ),
    "dangling-reference": "DFT shows the same group increases the mobility.",
    "multi-assertion": (
        "DFT shows the gap increases, and TEM demonstrates the strain decreases."
    ),
    # `suggests` is deliberately outside `_EVIDENCE_VERB_RE` (`indicates`,
    # the previous specimen here, graduated into it 2026-08-23).
    "no-evidence-verb": "DFT suggests the bandgap of 1.5 eV in this material.",
    "no-epistemic-mode": "This alloy shows a bandgap of 1.5 eV at room temperature.",
    "over-long": (
        "DFT shows the extremely anisotropic elastic modulus of this remarkably "
        "complex layered van der Waals heterostructure material varies "
        "continuously and smoothly across the full range of applied uniaxial "
        "strain conditions tested throughout the extensive computational "
        "simulation performed carefully."
    ),
    "author-name": "DFT shows Smith 2020 measured a gap of 1.5 eV.",
    "no-terminal-period": "DFT shows the gap increases with strain",
    "ascii-plusminus": "DFT shows a temperature of 25 +/- 2 K in this sample.",
    "ascii-micro": "DFT shows a resistivity of 50 ug in this sample.",
    "ascii-degrees": "DFT shows a transition at 25 degrees C in this sample.",
    "ascii-ohm": "DFT shows a resistance of 5 kOhm in this sample.",
    "ascii-angstrom": "DFT shows a bond length of 2 Angstrom in this sample.",
    "ascii-micrometre": "DFT shows a thickness of 500 micrometer in this sample.",
    "e-notation": "DFT shows a density of 4.6e3 in this sample.",
    "digit-grouping": "DFT shows a count of 4,600 atoms in this sample.",
    "ascii-multiplication": "DFT shows a rate of 4.6 x 10 boosts in this sample.",
    "ascii-x-multiplier": "DFT shows a conductance gain of 4.6x in this sample.",
    "hyphen-numeric-range": "DFT shows a tilt angle of 19-39° in this sample.",
    "caret-exponent": "DFT shows a value of cm^2 in this sample.",
    "ascii-minus-exponent": "DFT shows a rate of 10 s-1 in this sample.",
    "tex-residue": "DFT shows a shift of $Delta$ in this sample.",
    "past-passive": (
        "DFT shows the sampling algorithm was proposed for optimization problems."
    ),
    # The em-dash must sit past the first 60 characters, or `not-falsifiable`
    # co-fires: its leading-label rule only matches a dash near the start.
    "em-dash": (
        "DFT shows the elastic modulus of the annealed specimen increases "
        "sharply — the sample stiffens under uniaxial strain."
    ),
}

_TESTABLE_BLOCKING_CODES = gates._BLOCKING_LINT_CODES


def test_blocking_set_matches_the_fixture_table() -> None:
    # A code added to (or removed from) `_BLOCKING_LINT_CODES` without a
    # matching fixture is exactly the drift this test exists to catch.
    assert set(_ONE_VIOLATION_PER_CODE) == _TESTABLE_BLOCKING_CODES


def test_clean_sentence_passes() -> None:
    sentence = "DFT shows the elastic modulus increases by 12% under uniaxial strain."
    assert gates.check_claim_sentence(sentence) == []


def test_each_blocking_code_yields_exactly_one_violation() -> None:
    for code, sentence in _ONE_VIOLATION_PER_CODE.items():
        violations = gates.check_claim_sentence(sentence)
        assert len(violations) == 1, (code, sentence, violations)
        assert violations[0].gate == "claim-sentence"
        assert violations[0].message.startswith(f"{code}:"), (code, violations)


def test_advisory_only_codes_never_block() -> None:
    # two-denominator-solidus: 'cm²/Vs' — deciding which factor moves under
    # a negative exponent is judgment, not transcription (canon v2, Phase
    # 0). Advisory-only by design; must never appear as a violation here.
    sentence = "DFT shows the mobility reaches 5 cm²/Vs in this sample."
    assert gates.check_claim_sentence(sentence) == []


def test_empty_sentence_never_raises() -> None:
    assert gates.check_claim_sentence("") == []
    assert gates.check_claim_sentence(None) == []  # type: ignore[arg-type]


# ── wired into run_mint_gates (DB-backed) ─────────────────────────────────


def test_dirty_sentence_blocks_at_run_mint_gates(store: Any) -> None:
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "MOFs can be anisotropic up to 400:1.", paper, chunk)
    slugs = _gate_slugs(store, hub, _payload(chunk))
    assert "claim-sentence" in slugs


def test_clean_sentence_does_not_block_at_run_mint_gates(store: Any) -> None:
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(
        store, "DFT shows MOFs can be anisotropic up to 400:1.", paper, chunk
    )
    slugs = _gate_slugs(store, hub, _payload(chunk))
    assert "claim-sentence" not in slugs
