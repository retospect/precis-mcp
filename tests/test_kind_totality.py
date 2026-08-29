"""Totality guard for ``KindSpec``-derived kind sets — the "KindSpec facts
re-hardcoded downstream" drift class (see ``precis.utils.kind_facts``'s
docstring).

Several modules restate a fact ``precis.protocol.KindSpec`` already
declares (``is_numeric`` / ``corpus_role`` / ``role``) as a hand-maintained
``frozenset[str]`` constant, because they have no hub/runtime reachable at
import time. This test uses ``kind_facts.all_declared_specs()`` — the full
static ``precis.handlers`` roster, independent of which kinds actually
construct in this environment (booting a real hub under-reports:
credential-gated kinds like ``patent``/``math`` never construct without an
EPO OPS key / ``WOLFRAM_APP_ID``, which most test containers don't carry) —
to derive each fact and pin every such constant against it, so a handler's
``KindSpec`` changing (a new kind, a flipped flag) without its matching
touch-point updated fails here instead of drifting silently. Mirrors
``tests/test_worker_registry.py``'s frozen-snapshot style and
``tests/test_handle_registry.py``'s mirrored-list style (including that
test's scoping choice: plugin kinds, e.g. ``precis_bio``'s ``protein`` or
``precis_pathway``'s ``route``, are out of this repo's totality contract).

Two of the constants below are **deliberately hand-maintained** rather than
derived (``precis.taproot.hub.EVIDENCE_SRC_KINDS`` /
``precis.utils.eye_render._DOC_KINDS`` — see each module's docstring for
why; membership changes are human scope calls, not mechanical swaps); those
get an *invariant* pin (documenting exactly how far they diverge, if at
all, and why) rather than an equality pin, so an unnoticed widening of the
divergence still fails CI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.taproot.authoring import _SUPPORTER_KINDS
from precis.taproot.hub import (
    CLAIM_LINK_RELATIONS,
    EVIDENCE_SRC_KINDS,
    HUB_ROLES,
    MOTIVATION_RELATION,
)
from precis.utils import handle_registry, kind_facts
from precis.utils.eye_render import _DOC_KINDS
from precis.utils.refeye import _CITED_KINDS, CLAIM_RELATIONS
from precis_web.item_view import _ARTIFACT_KIND_FALLBACK
from precis_web.routes.preview import _NUMERIC_KIND_EXCEPTIONS, _NUMERIC_KINDS_FALLBACK

if TYPE_CHECKING:
    from precis.protocol import KindSpec

# ---------------------------------------------------------------------------
# Kinds with no universal handle by design (handle_registry.py's own module
# docstring: "Providers ... and stateless tools ... are addressed by
# URL/query/compute, not handles"). A kind landing here that should
# actually carry a handle code is a real gap — add its code to
# ``KIND_CODES`` instead of adding it to this list.
# ---------------------------------------------------------------------------
_NO_HANDLE_KINDS: frozenset[str] = frozenset(
    {
        # live external fetch, no local persistent-ref row to address
        "web",
        "youtube",
        "wikipedia",
        "semanticscholar",
        "perplexity-reasoning",
        "perplexity-research",
        "websearch",
        "email",  # live IMAP browse — addressed by folder/uid, never a ref
        # stateless compute — addressed by the query/expression itself
        "calc",
        "math",
        "provenance",
        "random",
    }
)


def _specs() -> list[KindSpec]:
    return kind_facts.all_declared_specs()


def test_every_declared_kind_is_handle_registered_or_explicitly_exempt() -> None:
    """Every kind declared under ``precis.handlers`` either carries a
    ``handle_registry.KIND_CODES`` entry or is named in
    :data:`_NO_HANDLE_KINDS` (a live-fetch provider / stateless tool, by
    design). A kind satisfying neither is the exact "forgot the
    touch-point" bug ``handle_registry.py``'s own docstring calls out
    (``news``/``message``/``cron`` all slipped through by hand once)."""
    declared_kinds = frozenset(s.kind for s in _specs())
    unaccounted = declared_kinds - set(handle_registry.KIND_CODES) - _NO_HANDLE_KINDS
    assert not unaccounted, (
        f"kind(s) {sorted(unaccounted)} are neither handle-coded "
        "(add to handle_registry.KIND_CODES) nor exempt "
        "(add to tests/test_kind_totality.py::_NO_HANDLE_KINDS if this is "
        "a provider/stateless kind with no addressable ref)"
    )


def test_no_handle_kinds_are_all_actually_declared() -> None:
    """The flip side of the totality check: every kind named in
    :data:`_NO_HANDLE_KINDS` must still be a real, currently-declared
    kind — else the exemption list is itself stale and silently hiding a
    retired kind rather than a genuine no-handle one."""
    declared_kinds = frozenset(s.kind for s in _specs())
    stale = _NO_HANDLE_KINDS - declared_kinds
    assert not stale, (
        f"{sorted(stale)} in _NO_HANDLE_KINDS but not a declared kind on "
        "this build — stale exemption, drop it"
    )


def test_no_handle_kinds_never_also_carry_a_handle_code() -> None:
    """A kind can't be both handle-addressed and exempt — that would mean
    the exemption list is stale (the kind grew a code later) or wrong."""
    overlap = _NO_HANDLE_KINDS & set(handle_registry.KIND_CODES)
    assert not overlap, f"{sorted(overlap)} both handle-coded and exempt"


def test_preview_numeric_kinds_fallback_matches_live_derivation() -> None:
    """``precis_web.routes.preview._NUMERIC_KINDS_FALLBACK`` (used whenever
    no hub is reachable) must equal the ``KindSpec.is_numeric``-derived set
    minus the one documented exception (``finding`` — see that module's
    docstring and ``tests/precis_web/test_resolve_ref_id.py::
    test_resolves_finding_by_pub_id``, which pins WHY finding must stay off
    the numeric fast path despite ``is_numeric=True``)."""
    live = kind_facts.numeric_kinds(_specs())
    assert live - _NUMERIC_KIND_EXCEPTIONS == _NUMERIC_KINDS_FALLBACK


def test_refeye_cited_kinds_matches_corpus_role_derivation() -> None:
    """``precis.utils.refeye._CITED_KINDS`` == every kind with a non-``none``
    ``corpus_role`` (evidence *or* spec — both render in the ring's "Cited"
    group; only ``corpus_role`` distinguishes citable-as-evidence from
    read-only-spec downstream, e.g. in taproot)."""
    live = kind_facts.corpus_role_kinds(_specs(), "evidence", "spec")
    assert live == _CITED_KINDS


def test_refeye_claim_relations_matches_taproot_hub_derivation() -> None:
    """``precis.utils.refeye.CLAIM_RELATIONS`` == the live union of
    ``taproot.hub.HUB_ROLES`` (paper→hub evidence roles),
    ``taproot.hub.CLAIM_LINK_RELATIONS`` (hub→hub advisory links) and
    ``taproot.hub.MOTIVATION_RELATION`` (hypothesis→motivator).

    ``refeye.py`` holds it as a static literal on purpose (not an import
    constraint — see that module's comment): this assertion is the gate.
    A relation added to ``taproot.hub`` reddens *here* and forces a human
    call on whether it belongs in agent-facing ring output, instead of
    either silently widening the ring or silently falling out of it."""
    live = HUB_ROLES | CLAIM_LINK_RELATIONS | {MOTIVATION_RELATION}
    assert live == CLAIM_RELATIONS


def test_artifact_kind_fallback_matches_live_role_derivation() -> None:
    """``precis_web.item_view._ARTIFACT_KIND_FALLBACK`` (used whenever no
    hub is reachable) must equal the ``role='artifact'`` set minus
    ``folder`` — the same exclusion ``artifact_kinds()`` applies when a
    hub IS reachable."""
    live = kind_facts.role_kinds(_specs(), "artifact") - {"folder"}
    assert frozenset(_ARTIFACT_KIND_FALLBACK) == live


def test_taproot_evidence_src_kinds_is_a_corpus_role_evidence_subset() -> None:
    """``precis.taproot.hub.EVIDENCE_SRC_KINDS`` stays hand-maintained even
    though it currently *equals* the ``corpus_role='evidence'`` derivation
    (see that module's docstring: widening it is a scope call on Taproot's
    evidence model, not a mechanical derivation) — a STOP case, not a swap.
    This pins two things:

    1. The one invariant that must never break regardless: every kind this
       module treats as evidence-worthy really IS flagged
       ``corpus_role='evidence'`` in ``KindSpec`` — a kind here that isn't
       flagged evidence at all would be a real bug, not a scope call.
    2. That there is currently NO divergence (``edgar`` and ``datasheet``
       were each approved by a human call), so another kind silently
       joining ``corpus_role='evidence'`` without a matching call on
       Taproot's evidence set fails here instead of nobody noticing.

    ``precis.taproot.authoring._SUPPORTER_KINDS`` is asserted equal to
    ``EVIDENCE_SRC_KINDS`` — it's now a direct import (single definition),
    not a second hand-copy.
    """
    live_evidence = kind_facts.corpus_role_kinds(_specs(), "evidence")
    assert live_evidence >= EVIDENCE_SRC_KINDS
    assert _SUPPORTER_KINDS == EVIDENCE_SRC_KINDS
    assert live_evidence - EVIDENCE_SRC_KINDS == frozenset()


def test_doc_kinds_diverges_from_corpus_role_only_by_known_gaps() -> None:
    """``precis.utils.eye_render._DOC_KINDS`` is hand-maintained on purpose
    (see that module's docstring: a pure ``corpus_role`` derivation would
    DROP ``web``, which has no ``corpus_role`` but genuinely belongs). This
    pins the exact, currently-known divergence so either side growing
    silently fails here:

    * ``_DOC_KINDS`` has ``web`` that ``corpus_role`` doesn't cover
      (by design, kept).
    * every ``corpus_role`` doc kind is otherwise covered (``edgar`` joined
      by human call — a new doc-family kind must be added here too, or the
      second assertion flags it).
    """
    corpus_doc = kind_facts.corpus_role_kinds(_specs(), "evidence", "spec")
    assert _DOC_KINDS - corpus_doc == frozenset({"web"})
    assert corpus_doc - _DOC_KINDS == frozenset()
