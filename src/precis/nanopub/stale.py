"""``precis.nanopub.stale`` — is a staged ``candidate`` still mintable?

The single source of truth for "does this staged ``fi<id>`` still pass the
gates it would face today", shared by two callers that must never disagree:

* :func:`precis.workers.health_digest._check_nanopub_candidates_fresh` —
  the daily liveness digest's "the ``/nanopub`` queue is offering dead
  work" finding.
* :mod:`precis_web.routes.nanopub` — per-row demotion + the "N stale" chip
  on the ``/nanopub`` workbench itself (gr279770 "queue filter now": stale
  candidates must be visibly demoted, never silently dropped).

Before this module existed, the whole predicate lived worker-side only —
carrying a literal copy of :mod:`precis.nanopub.gates`'s blocking-lint-code
set (``gates`` is llm-tainted and unimportable from the zero-llm
``health_digest``) — so the web queue had nothing telling a reviewer which
staged rows were already dead. Factoring it out means both surfaces read
the exact same reasoning; the copy of the gate-code SET below is the only
duplication left, and it's pinned equal to ``gates``'s own copy by
``tests/workers/test_health_digest.py::test_mint_blocking_codes_copy_is_pinned``.

Deliberately narrower than :func:`precis.nanopub.gates.run_mint_gates`:
only the sentence lints (code-drift-sensitive) and the
canonical/``contradicts`` posture (world-drift-sensitive) are re-checked.
The payload gates (quote/snip/containment) have nothing to bite on — a
staged candidate carries no grounding envelope yet — and the structural
arms barely move under a code release.

**llm-free**, same invariant ``health_digest`` enforces on itself
(``tests/workers/test_health_digest.py::test_module_imports_no_llm``):
only ``precis.taproot.notation``/``precis.taproot.sentence_lint``
(``re``/``typing``-only) are imported here. :mod:`precis.nanopub.gates` is
NOT importable from this module for the same reason it isn't importable
from ``health_digest`` — it reaches ``taproot.canon`` -> ``llm.router`` via
``nanopub.evidence`` -> ``taproot.seniority``.
"""

from __future__ import annotations

from dataclasses import dataclass

from precis.taproot.notation import lint_notation
from precis.taproot.sentence_lint import lint_claim_sentence

#: Literal copy of ``precis.nanopub.gates._BLOCKING_LINT_CODES`` — see the
#: module docstring for why ``gates`` itself can't be imported here. Pinned
#: equal to the original by
#: ``tests/workers/test_health_digest.py::test_mint_blocking_codes_copy_is_pinned``.
BLOCKING_LINT_CODES: frozenset[str] = frozenset(
    {
        "not-falsifiable",
        "dangling-reference",
        "multi-assertion",
        "no-evidence-verb",
        "no-epistemic-mode",
        "over-long",
        "author-name",
        "no-terminal-period",
        "em-dash",
        "past-passive",
        "ascii-plusminus",
        "ascii-micro",
        "ascii-degrees",
        "ascii-ohm",
        "ascii-angstrom",
        "ascii-micrometre",
        "e-notation",
        "digit-grouping",
        "ascii-multiplication",
        "ascii-x-multiplier",
        "hyphen-numeric-range",
        "caret-exponent",
        "ascii-minus-exponent",
        "tex-residue",
    }
)

#: Literal copy of ``precis.nanopub.gates._ARTIFACT_LINT_EXEMPTIONS``.
LINT_EXEMPTIONS: dict[str, frozenset[str]] = {
    "hypothesis": frozenset({"no-epistemic-mode", "no-evidence-verb"}),
}


@dataclass(frozen=True, slots=True)
class StaleReason:
    """Why a staged candidate no longer clears the current mint gates.

    ``label`` is the exact fragment both callers render: the digest's
    compact ``fi<id>(<label>)`` list entry and the web row's "stale:
    ``<label>``" badge — one string, two renderings, never two
    definitions of what it says."""

    #: ``"noncanonical" | "disputed" | "lint"`` — coarse bucket, in case a
    #: caller wants to branch on cause rather than just display it.
    kind: str
    #: Human-facing reason text — a bare word for the structural causes,
    #: a comma-joined list of blocking lint codes for the lint cause.
    label: str


def blocking_lint_hit_codes(title: str, artifact_type: str) -> list[str]:
    """Blocking lint codes ``title`` trips, scoped by ``artifact_type``
    (:data:`LINT_EXEMPTIONS`) — sorted, deduped."""
    blocking = BLOCKING_LINT_CODES - LINT_EXEMPTIONS.get(artifact_type, frozenset())
    warnings = lint_notation(title) + lint_claim_sentence(title)
    hit = sorted({w.split(":", 1)[0].strip() for w in warnings} & blocking)
    return hit


def candidate_stale_reason(
    *, canonical: bool, disputed: bool, title: str, artifact_type: str
) -> StaleReason | None:
    """Why a staged ``candidate`` row no longer passes the current mint
    gates — or ``None`` when it's still clean.

    Checked in the same order ``health_digest`` always has: non-canonical
    (the hub isn't a strict claim hub any more) beats disputed, which
    beats a lint regression — a row failing an earlier check is reported
    for that cause even when a later one would also fire, matching the
    existing digest detail lines' one-reason-per-row shape."""
    if not canonical:
        return StaleReason("noncanonical", "noncanonical")
    if disputed:
        return StaleReason("disputed", "disputed")
    hit = blocking_lint_hit_codes(title, artifact_type)
    if hit:
        return StaleReason("lint", ",".join(hit))
    return None
