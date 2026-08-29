"""`[fi]` cite-fit audit (`_draft_lint.fi_cite_segments` /
`fi_cite_fit_report` / `fi_cite_fit_hint`) — the draft↔hub seam check.
Segmentation is pure-text; the report runs against the real store with an
injected fake judge (the dispatch-backed `cite_fit_judge` is exercised only
via its coercion helper — no LLM in tests).
"""

from __future__ import annotations

from precis.dispatch import Hub
from precis.handlers._draft_lint import (
    FitVerdict,
    _coerce_fit_verdict,
    fi_cite_fit_hint,
    fi_cite_fit_report,
    fi_cite_segments,
)
from tests.workers._helpers import seed_ref


def _v(kind: str) -> FitVerdict:
    return FitVerdict(verdict=kind, confidence=0.9, rationale="because")  # type: ignore[typeddict-item]


# ── segmentation ─────────────────────────────────────────────────────────


def test_segment_is_prose_since_start() -> None:
    segs = fi_cite_segments("C60 was made in bulk [fi7].")
    assert segs == [("fi7", 7, "C60 was made in bulk")]


def test_segment_since_previous_cite_of_any_kind() -> None:
    segs = fi_cite_segments("First fact [pc3]. Second fact [fi7].")
    assert segs == [("fi7", 7, "Second fact")]


def test_contiguous_fi_run_shares_segment() -> None:
    segs = fi_cite_segments("One claim [fi1][fi2].")
    assert segs == [("fi1", 1, "One claim"), ("fi2", 2, "One claim")]


def test_prefix_cite_grounds_nothing() -> None:
    assert fi_cite_segments("[fi7] leads the sentence.") == []


def test_pc_boundary_breaks_fi_run() -> None:
    # [pc3] between the two fi cites: fi2's span is empty and the run was
    # broken, so fi2 is a prefix cite, not a sharer of fi1's segment.
    segs = fi_cite_segments("One claim [fi1][pc3][fi2].")
    assert segs == [("fi1", 1, "One claim")]


def test_pinned_fi_cite_anchors() -> None:
    segs = fi_cite_segments("Pinned fact [fi9>pc2].")
    assert segs == [("fi9", 9, "Pinned fact")]


def test_markers_stripped_from_segment() -> None:
    segs = fi_cite_segments("As shown [pc3], the yield doubled [fi7].")
    assert segs == [("fi7", 7, "the yield doubled")]


# ── verdict coercion ─────────────────────────────────────────────────────


def test_coerce_malformed_degrades_to_error() -> None:
    v = _coerce_fit_verdict(None, default_rationale="boom")
    assert v == {"verdict": "error", "confidence": 0.0, "rationale": "boom"}
    v = _coerce_fit_verdict({"verdict": "same"}, default_rationale="d")
    assert v["verdict"] == "error"


def test_coerce_clamps_confidence() -> None:
    v = _coerce_fit_verdict(
        {"verdict": "adjacent", "confidence": 7, "rationale": "r"},
        default_rationale="d",
    )
    assert v == {"verdict": "adjacent", "confidence": 1.0, "rationale": "r"}


# ── report + hint ────────────────────────────────────────────────────────


def test_report_judges_live_hubs_and_skips_dangling(hub: Hub) -> None:
    store = hub.live_store
    fi = seed_ref(store, title="IR spectroscopy shows X.", kind="finding")
    paper = seed_ref(store, title="Not a finding", kind="paper")

    calls: list[tuple[str, str]] = []

    def judge(segment: str, claim: str) -> FitVerdict:
        calls.append((segment, claim))
        return _v("adjacent")

    text = (
        f"Bulk production was achieved [fi{fi}]. Other [fi{paper}]. Gone [fi999999999]."
    )
    rows = fi_cite_fit_report(store, text, judge_fn=judge)

    assert [r.token for r in rows] == [f"fi{fi}"]
    assert rows[0].claim == "IR spectroscopy shows X."
    assert rows[0].segment == "Bulk production was achieved"
    assert calls == [("Bulk production was achieved", "IR spectroscopy shows X.")]


def test_report_dedups_repeated_pair(hub: Hub) -> None:
    store = hub.live_store
    fi = seed_ref(store, title="Claim A.", kind="finding")
    n_calls = 0

    def judge(segment: str, claim: str) -> FitVerdict:
        nonlocal n_calls
        n_calls += 1
        return _v("supports")

    text = f"Same prose [fi{fi}]. Same prose [fi{fi}]."
    rows = fi_cite_fit_report(store, text, judge_fn=judge)
    assert len(rows) == 1 and n_calls == 1


def test_hint_flags_adjacent_and_unrelated_only(hub: Hub) -> None:
    store = hub.live_store
    a = seed_ref(store, title="Characterization claim.", kind="finding")
    b = seed_ref(store, title="Matching claim.", kind="finding")

    verdicts = {f"fi{a}": _v("adjacent"), f"fi{b}": _v("supports")}

    def judge(segment: str, claim: str) -> FitVerdict:
        return verdicts[f"fi{a}" if "scale" in segment else f"fi{b}"]

    text = f"Made at scale [fi{a}]. Well characterized [fi{b}]."
    rows = fi_cite_fit_report(store, text, judge_fn=judge)
    hint = fi_cite_fit_hint(rows)
    assert f"[fi{a}]" in hint and f"[fi{b}]" not in hint
    assert "cite-fit (adjacent)" in hint
    assert "mint THAT claim" in hint
    assert fi_cite_fit_hint([r for r in rows if r.token == f"fi{b}"]) == ""
