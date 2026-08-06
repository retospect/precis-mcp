"""``src/precis/taproot/trust.py`` — the single shared trust derivation
(docs/proposals/finding-trust-surfaces.md). DB-backed (real
``refs``/``ref_tags``/``links`` via the ``store`` fixture), mirroring
``tests/test_taproot_cite.py``'s setup style.
"""

from __future__ import annotations

import re
from typing import Any

from precis.dispatch import Hub
from precis.handlers.finding import FindingHandler
from precis.store.types import Tag
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from precis.taproot.trust import claim_trust

_CLAIM = CanonicalClaim(
    sentence="Pd/C catalyzes Suzuki coupling at room temperature with a mild base.",
    scope={"material": "Pd/C", "method": "Suzuki coupling", "regime": "RT"},
)


def _search(pattern: str, text: str) -> re.Match[str]:
    m = re.search(pattern, text)
    assert m is not None, f"pattern {pattern!r} not found in {text!r}"
    return m


def _make_handler(store: Any) -> FindingHandler:
    return FindingHandler(hub=Hub(store=store))


def _paper(store: Any, *, cite_key: str, title: str = "a paper") -> int:
    ref = store.insert_ref(kind="paper", slug=cite_key, title=title, meta={})
    return ref.id


def _finding(store: Any, *, cite_key: str = "src01a") -> int:
    """A plain (non-hub) finding cited off ``cite_key``, STATUS:tracing."""
    _paper(store, cite_key=cite_key)
    handler = _make_handler(store)
    resp = handler.put(title="t", body="claim body", scope={}, cited_in=cite_key)
    return int(_search(r"id=(\d+)", resp.body).group(1))


def _set_status(store: Any, ref_id: int, value: str) -> None:
    store.add_tag(
        ref_id, Tag.closed("STATUS", value), set_by="chase", replace_prefix=True
    )


# ── established arm — clean ─────────────────────────────────────────────


def test_established_no_verification_is_clean(store: Any) -> None:
    """No LLM ran this hop — the chain traced to ground, today's bar."""
    ref_id = _finding(store)
    store.update_ref(ref_id, meta_patch={"chain": [{"ref_id": 1, "ord": 0}]})
    _set_status(store, ref_id, "established")

    result = claim_trust(store, ref_id)

    assert result.label == "clean"
    assert result.note is None
    assert result.overridden is False


def test_established_supports_yes_is_clean(store: Any) -> None:
    ref_id = _finding(store)
    store.update_ref(
        ref_id,
        meta_patch={
            "chain": [{"ref_id": 1, "ord": 0, "verification": {"supports": "yes"}}]
        },
    )
    _set_status(store, ref_id, "established")

    result = claim_trust(store, ref_id)

    assert result.label == "clean"


def test_established_partial_without_contradicts_is_clean(store: Any) -> None:
    ref_id = _finding(store)
    store.update_ref(
        ref_id,
        meta_patch={
            "chain": [
                {
                    "ref_id": 1,
                    "ord": 0,
                    "verification": {
                        "supports": "partial",
                        "contradicts": False,
                    },
                }
            ]
        },
    )
    _set_status(store, ref_id, "established")

    result = claim_trust(store, ref_id)

    assert result.label == "clean"


def test_established_non_dict_verification_is_clean(store: Any) -> None:
    """A malformed/legacy ``verification`` blob (not a dict) can't
    establish a negative verdict — treated the same as absent: clean.
    Also proves ``claim_trust`` never raises on it (the export
    call sites additionally guard against any escaped exception)."""
    ref_id = _finding(store)
    store.update_ref(
        ref_id,
        meta_patch={"chain": [{"ref_id": 1, "ord": 0, "verification": "not-a-dict"}]},
    )
    _set_status(store, ref_id, "established")

    result = claim_trust(store, ref_id)

    assert result.label == "clean"
    assert result.note is None


# ── established arm — unsupported ───────────────────────────────────────


def test_established_supports_no_is_unsupported(store: Any) -> None:
    ref_id = _finding(store)
    store.update_ref(
        ref_id,
        meta_patch={
            "chain": [
                {
                    "ref_id": 1,
                    "ord": 0,
                    "verification": {
                        "supports": "no",
                        "support_reason": "chunk reports the opposite trend",
                    },
                }
            ]
        },
    )
    _set_status(store, ref_id, "established")

    result = claim_trust(store, ref_id)

    assert result.label == "unsupported"
    assert result.note == "chunk reports the opposite trend"


def test_established_partial_with_contradicts_is_unsupported(store: Any) -> None:
    ref_id = _finding(store)
    store.update_ref(
        ref_id,
        meta_patch={
            "chain": [
                {
                    "ref_id": 1,
                    "ord": 0,
                    "verification": {
                        "supports": "partial",
                        "contradicts": True,
                        "support_reason": "scoped but opposite on the key regime",
                    },
                }
            ]
        },
    )
    _set_status(store, ref_id, "established")

    result = claim_trust(store, ref_id)

    assert result.label == "unsupported"
    assert result.note == "scoped but opposite on the key regime"


# ── unverified arm — every reachable non-established status ────────────


def test_tracing_is_unverified_source_pending(store: Any) -> None:
    ref_id = _finding(store)  # put() leaves STATUS:tracing by default

    result = claim_trust(store, ref_id)

    assert result.label == "unverified"
    assert result.note == "source pending"


def test_acquiring_is_unverified_source_pending(store: Any) -> None:
    ref_id = _finding(store)
    _set_status(store, ref_id, "acquiring")

    result = claim_trust(store, ref_id)

    assert result.label == "unverified"
    assert result.note == "source pending"


def test_cycle_is_unverified(store: Any) -> None:
    ref_id = _finding(store)
    _set_status(store, ref_id, "cycle")

    result = claim_trust(store, ref_id)

    assert result.label == "unverified"


def test_multi_candidate_is_unverified_ambiguous(store: Any) -> None:
    ref_id = _finding(store)
    _set_status(store, ref_id, "multi_candidate")

    result = claim_trust(store, ref_id)

    assert result.label == "unverified"
    assert result.note == "ambiguous citation awaiting pick"


def test_dead_chain_unacquirable_has_specific_note(store: Any) -> None:
    ref_id = _finding(store)
    store.update_ref(ref_id, meta_patch={"dead_reason": "unacquirable"})
    _set_status(store, ref_id, "dead_chain")

    result = claim_trust(store, ref_id)

    assert result.label == "unverified"
    assert result.note == "no OA copy obtainable; hand-download queued"
    assert result.overridden is False


def test_dead_chain_other_reason_notes_the_slug(store: Any) -> None:
    ref_id = _finding(store)
    store.update_ref(ref_id, meta_patch={"dead_reason": "no_resolvable_cite"})
    _set_status(store, ref_id, "dead_chain")

    result = claim_trust(store, ref_id)

    assert result.label == "unverified"
    assert result.note == "no_resolvable_cite"


# ── hub arm ──────────────────────────────────────────────────────────


def test_hub_empty_print_set_is_unverified(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)

    result = claim_trust(store, hub)

    assert result.label == "unverified"
    assert result.note == "claim hub has no print-visible supporter yet"
    assert result.status == "hub"


def test_hub_with_supporter_is_clean(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    origin = _paper(store, cite_key="ftco01a")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=origin, role="corroborates")

    result = claim_trust(store, hub)

    assert result.label == "clean"
    assert result.overridden is False


# ── override — folds to the softer abstract/vouched, never clean ──────


def test_legacy_finding_override_folds_to_vouched(store: Any) -> None:
    """A legacy finding-level override (no ``mode``) folds unverified → ✍
    vouched (author asserts; source unobtainable), never all the way to
    clean — no one read the full text."""
    ref_id = _finding(store)
    store.update_ref(ref_id, meta_patch={"dead_reason": "unacquirable"})
    _set_status(store, ref_id, "dead_chain")
    store.update_ref(
        ref_id,
        meta_patch={
            "unacquirable_override": {
                "by": "agent",
                "at": "2026-08-04T00:00:00+00:00",
                "note": "print-only monograph",
            }
        },
    )

    result = claim_trust(store, ref_id)

    assert result.label == "vouched"
    assert result.overridden is True
    # the author's own note wins over the generic lifecycle note.
    assert result.note == "print-only monograph"


def test_finding_override_mode_abstract_folds_to_abstract(store: Any) -> None:
    ref_id = _finding(store)  # STATUS:tracing → unverified
    store.update_ref(
        ref_id,
        meta_patch={
            "unacquirable_override": {
                "mode": "abstract",
                "by": "web:owner",
                "at": "2026-08-04T00:00:00+00:00",
                "note": "abstract states the result outright",
            }
        },
    )

    result = claim_trust(store, ref_id)

    assert result.label == "abstract"
    assert result.overridden is True
    assert result.note == "abstract states the result outright"


def test_paper_level_override_reads_through_frontier(store: Any) -> None:
    """The author marks the SOURCE PAPER unobtainable (from its Meta tab);
    a lifecycle finding whose chain frontier is that paper reads through and
    renders ✍ — without the finding itself being edited."""
    paper_id = _paper(store, cite_key="unob01a", title="Kroto 1985")
    store.update_ref(
        paper_id,
        meta_patch={
            "unacquirable_override": {
                "mode": "vouched",
                "by": "web:owner",
                "at": "2026-08-04T00:00:00+00:00",
                "note": "paywalled; UoL + UCSC exhausted",
            }
        },
    )
    ref_id = _finding(store)  # STATUS:tracing → unverified
    store.update_ref(ref_id, meta_patch={"chain": [{"ref_id": paper_id, "ord": 0}]})

    result = claim_trust(store, ref_id)

    assert result.label == "vouched"
    assert result.overridden is True
    assert result.note == "paywalled; UoL + UCSC exhausted"


def test_paper_level_override_abstract_mode_reads_through(store: Any) -> None:
    paper_id = _paper(store, cite_key="unob02a")
    store.update_ref(
        paper_id,
        meta_patch={
            "unacquirable_override": {
                "mode": "abstract",
                "note": "abstract backs it",
            }
        },
    )
    ref_id = _finding(store)
    store.update_ref(ref_id, meta_patch={"chain": [{"ref_id": paper_id, "ord": 0}]})

    assert claim_trust(store, ref_id).label == "abstract"


def test_finding_override_wins_over_paper_frontier(store: Any) -> None:
    """A finding-level override short-circuits before the paper read-through
    — the finding's own declaration is the more specific signal."""
    paper_id = _paper(store, cite_key="unob03a")
    store.update_ref(
        paper_id,
        meta_patch={"unacquirable_override": {"mode": "vouched", "note": "paper note"}},
    )
    ref_id = _finding(store)
    store.update_ref(
        ref_id,
        meta_patch={
            "chain": [{"ref_id": paper_id, "ord": 0}],
            "unacquirable_override": {"mode": "abstract", "note": "finding note"},
        },
    )

    result = claim_trust(store, ref_id)

    assert result.label == "abstract"
    assert result.note == "finding note"


def test_paper_frontier_without_override_stays_unverified(store: Any) -> None:
    """A frontier paper with no declaration doesn't fold — the read-through
    is a no-op, the claim stays ⚠."""
    paper_id = _paper(store, cite_key="unob04a")
    ref_id = _finding(store)
    store.update_ref(ref_id, meta_patch={"chain": [{"ref_id": paper_id, "ord": 0}]})

    result = claim_trust(store, ref_id)

    assert result.label == "unverified"
    assert result.overridden is False


def test_override_does_not_convert_unsupported(store: Any) -> None:
    """A negative terminal verification outranks the override — the
    paper was read, an override doesn't unread it."""
    ref_id = _finding(store)
    store.update_ref(
        ref_id,
        meta_patch={
            "chain": [{"ref_id": 1, "ord": 0, "verification": {"supports": "no"}}],
            "unacquirable_override": {
                "by": "agent",
                "at": "2026-08-04T00:00:00+00:00",
                "note": "print-only monograph",
            },
        },
    )
    _set_status(store, ref_id, "established")

    result = claim_trust(store, ref_id)

    assert result.label == "unsupported"
    assert result.overridden is False


def test_override_absent_leaves_unverified(store: Any) -> None:
    ref_id = _finding(store)  # STATUS:tracing, no override

    result = claim_trust(store, ref_id)

    assert result.label == "unverified"
    assert result.overridden is False


# ── hub supporter override — a supporter paper's own Meta-tab unacquirable
#    declaration softens a clean hub resting on it (the hub twin of the
#    lifecycle frontier read-through). `_paper` creates no inter-paper cites,
#    so every attached supporter is a corroborator → one grounding group. ──


def _unacq(*, mode: str | None = None, note: str = "paywalled") -> dict[str, Any]:
    ov: dict[str, Any] = {
        "by": "web:owner",
        "at": "2026-08-04T00:00:00+00:00",
        "note": note,
    }
    if mode is not None:
        ov["mode"] = mode
    return ov


def test_hub_sole_supporter_unacquirable_folds_clean_to_vouched(store: Any) -> None:
    """A clean hub whose only print-visible supporter is declared unacquirable
    (legacy, no mode) reads ✍ vouched, not clean — the printed citation rests
    on a source no one read in full."""
    hub = mint_hub(store, _CLAIM)
    origin = _paper(store, cite_key="ftco01a")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=origin, role="corroborates")
    store.update_ref(
        origin,
        meta_patch={"unacquirable_override": _unacq(note="print-only monograph")},
    )

    result = claim_trust(store, hub)

    assert result.label == "vouched"
    assert result.overridden is True
    assert result.note == "print-only monograph"
    assert result.status == "hub"


def test_hub_supporter_unacquirable_abstract_mode_folds_to_abstract(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    origin = _paper(store, cite_key="ftco02a")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=origin, role="corroborates")
    store.update_ref(
        origin,
        meta_patch={
            "unacquirable_override": _unacq(mode="abstract", note="abstract backs it")
        },
    )

    result = claim_trust(store, hub)

    assert result.label == "abstract"
    assert result.overridden is True


def test_hub_stays_clean_when_one_supporter_is_acquirable(store: Any) -> None:
    """Only when EVERY grounding supporter is unacquirable does the hub soften.
    A readable supporter keeps a real read-grounding → clean."""
    hub = mint_hub(store, _CLAIM)
    unacq = _paper(store, cite_key="una01a")
    readable = _paper(store, cite_key="rd01a")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=unacq, role="corroborates")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=readable, role="corroborates")
    store.update_ref(unacq, meta_patch={"unacquirable_override": _unacq()})

    result = claim_trust(store, hub)

    assert result.label == "clean"
    assert result.overridden is False


def test_hub_all_supporters_unacquirable_abstract_wins_over_vouched(store: Any) -> None:
    """Softest override wins: Ⓐ (abstract backs it) is better grounding than a
    bare ✍ vouch, so one abstract-mode supporter makes the whole claim Ⓐ."""
    hub = mint_hub(store, _CLAIM)
    a = _paper(store, cite_key="aa01a")
    b = _paper(store, cite_key="bb01a")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=a, role="corroborates")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=b, role="corroborates")
    store.update_ref(a, meta_patch={"unacquirable_override": _unacq(note="vouch")})
    store.update_ref(
        b, meta_patch={"unacquirable_override": _unacq(mode="abstract", note="abs")}
    )

    result = claim_trust(store, hub)

    assert result.label == "abstract"
    assert result.overridden is True


def test_hub_inflight_ignores_supporter_override_path(store: Any) -> None:
    """An inflight hub (no print-visible supporter) stays unverified — there's
    no grounding group to read a paper override through."""
    hub = mint_hub(store, _CLAIM)

    result = claim_trust(store, hub)

    assert result.label == "unverified"
    assert result.overridden is False


# ── worst-of ordering (block badge / CSS precedence) ──────────────────


def test_worse_trust_confidence_ladder() -> None:
    """clean ‹ abstract ‹ vouched ‹ unverified ‹ unsupported — the block
    badge takes the loudest of its cite heads."""
    from precis.taproot.trust import worse_trust

    assert worse_trust("clean", "abstract") == "abstract"
    assert worse_trust("abstract", "vouched") == "vouched"
    assert worse_trust("vouched", "unverified") == "unverified"
    assert worse_trust("unverified", "unsupported") == "unsupported"
    # associative worst-of across a mixed set
    assert (
        worse_trust(worse_trust("abstract", "unsupported"), "vouched") == "unsupported"
    )
