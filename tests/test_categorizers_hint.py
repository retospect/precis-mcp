"""gr515633 asks 1+2 — the per-ref categorizer machine-hint line, the
≥95% coverage gate, and the search ``tags=`` exclusion note.

The hint line (``precis.categorizers.ref_hint_line``) rides the paper
overview, the patent overview and the finding eye; the gate
(``axis_filter_ready``) is the seam the future filter advertisement
reads, and until an axis crosses it every ``search(tags=...)`` that names
the axis appends the "not yet classified — excluded" note (wired at
``runtime/dispatch.py``'s ``dispatch_with_status``).
"""

from __future__ import annotations

import threading
import time

import pytest

from precis import categorizers as catz
from precis.dispatch import Hub
from precis.runtime.core import PrecisRuntime
from precis.store import Store
from precis.store.types import Ref, Tag
from precis.workers.service_config import (
    ALL_HOSTS,
    clear_service_config,
    set_service_prio,
)


@pytest.fixture(autouse=True)
def _clear_coverage_cache():
    """``_coverage`` is process-cached (300 s TTL) — a test that tags a
    ref between reads would otherwise see the previous test's counts."""
    catz._coverage_cache.clear()
    yield
    catz._coverage_cache.clear()


def _paper(store: Store, slug: str) -> Ref:
    return store.insert_ref(kind="paper", slug=slug, title="a paper", meta={})


# ── ref_hint_line ──────────────────────────────────────────────────────


def test_hint_line_none_when_everything_off(store: Store) -> None:
    """All categorizers default-OFF + no tags on the ref → no line (the
    hint reflects what the passes actually run, not the yaml taxonomy)."""
    ref = _paper(store, "hint-off-paper")
    assert catz.ref_hint_line(store, ref) is None


def test_hint_line_marks_enabled_axis_unclassified(store: Store) -> None:
    ref = _paper(store, "hint-enabled-paper")
    try:
        set_service_prio(store, ALL_HOSTS, "axis:domain", 5, actor="test")
        line = catz.ref_hint_line(store, ref)
    finally:
        clear_service_config(store, ALL_HOSTS, "axis:domain")
    assert line is not None
    assert "filter hint, not curated truth" in line
    assert "DOMAIN=(not yet classified)" in line
    # Off axes with no data on the ref are absent, not "(not yet classified)".
    assert "MATERIAL" not in line
    assert "topic:" not in line


def test_hint_line_shows_values_and_marker_states(store: Store) -> None:
    ref = _paper(store, "hint-tagged-paper")
    store.add_tag(ref.id, Tag.closed("DOMAIN", "chemistry"), set_by="agent")
    marker = catz.current_marker_value([])
    assert marker is not None
    store.add_tag(
        ref.id, Tag.closed(catz.TOPIC_MARKER_NAMESPACE, marker), set_by="agent"
    )
    line = catz.ref_hint_line(store, ref)
    assert line is not None
    assert "DOMAIN=chemistry" in line
    # Marker present, no topic: tag → the pass ran and wrote none.
    assert f"topic: {catz.NO_VALUE}" in line


def test_hint_line_lists_topic_tags(store: Store) -> None:
    ref = _paper(store, "hint-topic-paper")
    store.add_tag(ref.id, Tag.open("topic:mof"), set_by="agent")
    store.add_tag(ref.id, Tag.open("topic:noxrr"), set_by="agent")
    line = catz.ref_hint_line(store, ref)
    assert line is not None
    assert "topic: mof, noxrr" in line


def test_hint_line_shows_disabled_axis_when_ref_carries_data(
    store: Store,
) -> None:
    """A tag written while the pass was on stays visible after the pass
    is off — the line reports data, not just the live pass set."""
    ref = _paper(store, "hint-stale-axis-paper")
    store.add_tag(ref.id, Tag.closed("SCALE", "1nm"), set_by="agent")
    line = catz.ref_hint_line(store, ref)
    assert line is not None
    assert "SCALE=1nm" in line


def test_hint_line_none_for_uncovered_kind(store: Store) -> None:
    """A kind no ref-level axis targets (and topics don't sweep) gets no
    line at all — 'draft' is in no applies_to_kinds/opt_in_kinds."""
    ref = store.insert_ref(kind="draft", slug="hint-draft", title="a draft", meta={})
    assert catz.ref_hint_line(store, ref) is None


def test_hint_line_finding_gated_on_opt_in(store: Store) -> None:
    """findings are default-OFF (workers/_claim.py OPT_IN_KINDS): no line
    until ``categorize_kind:finding`` is flipped on."""
    ref = store.insert_ref(kind="finding", slug=None, title="a claim", meta={})
    set_service_prio(store, ALL_HOSTS, "axis:domain", 5, actor="test")
    try:
        assert catz.ref_hint_line(store, ref) is None
        set_service_prio(store, ALL_HOSTS, "categorize_kind:finding", 5, actor="test")
        line = catz.ref_hint_line(store, ref)
    finally:
        clear_service_config(store, ALL_HOSTS, "categorize_kind:finding")
        clear_service_config(store, ALL_HOSTS, "axis:domain")
    assert line is not None
    assert "DOMAIN=(not yet classified)" in line


def test_paper_overview_carries_hint(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    rt = runtime_with_store
    ref = _paper(store, "hint-overview-paper")
    try:
        set_service_prio(store, ALL_HOSTS, "axis:domain", 5, actor="test")
        body = rt.dispatch("get", {"kind": "paper", "id": ref.slug})
    finally:
        clear_service_config(store, ALL_HOSTS, "axis:domain")
    assert "filter hint, not curated truth" in body
    assert "DOMAIN=(not yet classified)" in body


# ── coverage gate + search exclusion note ──────────────────────────────


def test_axis_filter_ready(store: Store) -> None:
    """The ≥95% gate: an untouched axis is never ready; an axis at full
    coverage of a one-ref corpus is."""
    assert catz.axis_filter_ready(store, "domain") is False
    ref = _paper(store, "gate-paper")
    store.add_tag(ref.id, Tag.closed("DOMAIN", "chemistry"), set_by="agent")
    catz._coverage_cache.clear()
    assert catz.axis_filter_ready(store, "domain") is True
    # An axis id no yaml declares is never ready.
    assert catz.axis_filter_ready(store, "bogus-axis") is False


def test_axis_filter_ready_boundary(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bar itself: 94% is below, 95% is at/above it."""
    monkeypatch.setattr(catz, "_coverage", lambda _s, _k, _lvl: (94, 100))
    assert catz.axis_filter_ready(store, "domain") is False
    monkeypatch.setattr(catz, "_coverage", lambda _s, _k, _lvl: (95, 100))
    assert catz.axis_filter_ready(store, "domain") is True


def test_filter_notes_silent_for_plain_tags(store: Store) -> None:
    assert catz.categorizer_filter_notes(store, ["STATUS:open", "workspace"]) == []
    assert catz.categorizer_filter_notes(store, None) == []


def test_filter_notes_warn_per_axis(store: Store) -> None:
    _paper(store, "note-paper")
    notes = catz.categorizer_filter_notes(store, ["DOMAIN:chemistry"])
    assert notes == [
        "DOMAIN: 1 of 1 paper+patent refs not yet classified "
        "— excluded from this filter"
    ]


def test_filter_notes_topic_tag(store: Store) -> None:
    _paper(store, "note-topic-paper")
    notes = catz.categorizer_filter_notes(store, ["topic:mof"])
    assert len(notes) == 1
    assert notes[0].startswith("topic: ")


def test_filter_notes_quiet_at_full_coverage(store: Store) -> None:
    """At/above the bar the axis is treated as filter-ready — no warning
    (that same verdict is what the future advertisement will read)."""
    ref = _paper(store, "note-ready-paper")
    store.add_tag(ref.id, Tag.closed("DOMAIN", "chemistry"), set_by="agent")
    assert catz.categorizer_filter_notes(store, ["DOMAIN:chemistry"]) == []


def test_search_appends_exclusion_note(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    rt = runtime_with_store
    _paper(store, "note-search-paper")
    body = rt.dispatch(
        "search",
        {
            "kind": "paper",
            "q": "catalyst",
            "mode": "lexical",
            "tags": ["DOMAIN:chemistry"],
        },
    )
    assert "not yet classified — excluded from this filter" in body
    assert "DOMAIN:" in body


def test_search_without_categorizer_tags_has_no_note(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    rt = runtime_with_store
    _paper(store, "note-search-plain-paper")
    body = rt.dispatch(
        "search",
        {"kind": "paper", "q": "catalyst", "mode": "lexical", "tags": ["workspace"]},
    )
    assert "not yet classified" not in body


def test_search_survives_note_hook_error(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hook is best-effort: a raising coverage read leaves the
    search body alone (no error, no note)."""
    rt = runtime_with_store
    _paper(store, "note-hook-raise-paper")

    def _boom(_store: Store, _tags: object) -> list[str]:
        raise RuntimeError("coverage read exploded")

    monkeypatch.setattr(catz, "categorizer_filter_notes", _boom)
    body = rt.dispatch(
        "search",
        {
            "kind": "paper",
            "q": "catalyst",
            "mode": "lexical",
            "tags": ["DOMAIN:chemistry"],
        },
    )
    assert "[error:" not in body
    assert "not yet classified" not in body


def test_search_note_gated_on_kind(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The note only speaks for the categorized kinds — a memory or
    cross-kind search that names an axis gets no population math it
    didn't ask about."""
    rt = runtime_with_store
    _paper(store, "note-kind-gate-paper")
    body = rt.dispatch(
        "search",
        {
            "kind": "memory",
            "q": "catalyst",
            "mode": "lexical",
            "tags": ["DOMAIN:chemistry"],
        },
    )
    assert "not yet classified" not in body
    body = rt.dispatch(
        "search",
        {
            "kind": "paper",
            "q": "catalyst",
            "mode": "lexical",
            "tags": ["DOMAIN:chemistry"],
        },
    )
    assert "not yet classified — excluded from this filter" in body


def test_coverage_cached_within_ttl(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 300 s process cache: a second note read inside the TTL runs
    no fresh corpus count."""
    calls = 0
    real = catz.ref_axis_progress

    def _spy(s: Store, ns: str, total: int) -> tuple[int, int, object]:
        nonlocal calls
        calls += 1
        return real(s, ns, total)

    monkeypatch.setattr(catz, "ref_axis_progress", _spy)
    catz.categorizer_filter_notes(store, ["DOMAIN:chemistry"])
    catz.categorizer_filter_notes(store, ["DOMAIN:chemistry"])
    assert calls == 1


def test_chunk_axis_cold_cache_skips_inline_scan(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chunk-level axis on a cold cache produces no note and runs no
    count on the caller's thread — a single-flight background fill warms
    the cache so the *next* read shows the note."""
    scanned_on: list[str] = []

    def _spy_done(s: Store, ns: str, total: int) -> tuple[int, int, None]:
        scanned_on.append(threading.current_thread().name)
        return (0, total, None)

    monkeypatch.setattr(catz, "chunk_eligible_total", lambda _s: 100)
    monkeypatch.setattr(catz, "chunk_axis_progress", _spy_done)

    assert catz.categorizer_filter_notes(store, ["ROLE3:enzyme"]) == []
    assert "MainThread" not in scanned_on

    deadline = time.monotonic() + 10
    while "role3" not in catz._coverage_cache:
        if time.monotonic() > deadline:
            pytest.fail("background coverage fill never landed")
        time.sleep(0.02)
    assert scanned_on  # the fill did run, off the caller's thread

    assert catz.categorizer_filter_notes(store, ["ROLE3:enzyme"]) == [
        "ROLE3: 100 of 100 eligible chunks not yet classified "
        "— excluded from this filter"
    ]


def test_patent_overview_carries_hint(store: Store, hub: Hub) -> None:
    """The same machine-hint line on the patent overview — for a patent
    the applicable axis is ``topic:`` (ref-level axes claim papers).
    Rendered directly: the patent kind is secret-gated off in the test
    build, so dispatch would answer ``[error:Unsupported]``."""
    from precis.handlers.patent import PatentHandler

    ref = store.insert_ref(kind="patent", slug="hint-patent", title="a patent", meta={})
    handler = PatentHandler(hub=hub)
    try:
        set_service_prio(store, ALL_HOSTS, "classify_topics", 5, actor="test")
        body = handler._render_overview(ref).body
    finally:
        clear_service_config(store, ALL_HOSTS, "classify_topics")
    assert "filter hint, not curated truth" in body
    assert "topic: (not yet classified)" in body
