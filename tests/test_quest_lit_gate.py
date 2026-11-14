"""The lit-search's pre-link relevance gate (gr592951).

``precis.quest.lit_gate`` — the term gate a paper candidate must pass
before its ``serves`` edge: the quest's ``meta.lit_gate`` ``{host,
reaction}`` term lists against the candidate's title+abstract, opt-in
(unset = no gate), with the mandatory no-metadata rule beside it (a
candidate that can't be read is deferred, never linked unverified).
Plus the ``run_search_step`` wiring: per-candidate ``seen, not linked``
verdicts in the logbook line, the deterministic sampled-for-review
flag, and the weekly ``new literature`` digest.

Runs against real PG (the ``store`` fixture) so the considered-verdict
and logbook writes are exercised end to end.
"""

from __future__ import annotations

from typing import Any

from precis.quest import lit_gate
from precis.quest import search as qsearch
from precis.store import Store

GATE = {
    "host": ["Pd", "Pd(111)", "Pt"],
    "reaction": ["NO", "NH3", "nitric oxide", "ammonia"],
}


def _quest(store: Store, gate: dict[str, Any] | None = None) -> int:
    meta = {lit_gate.LIT_GATE_META_KEY: gate} if gate is not None else {}
    return store.insert_ref(
        kind="quest",
        slug=None,
        title="Electrochemical NO reduction to NH3 on doped Pd(111)",
        meta=meta,
    ).id


def _paper(store: Store, slug: str, title: str, abstract: str = "") -> int:
    ref = store.insert_ref(
        kind="paper",
        slug=slug,
        title=title,
        meta={"abstract": abstract} if abstract else {},
    )
    return ref.id


def _serving(store: Store, quest_id: int) -> set[int]:
    return {
        ln.src_ref_id
        for ln in store.links_for(quest_id, direction="in", relation="serves")
    }


def _lit_search_lines(store: Store, quest_id: int) -> list[str]:
    return [
        b.text
        for b in store.chunks.list_chunks_for_ref(quest_id)
        if b.chunk_kind == "quest_log" and b.text.startswith("lit-search")
    ]


def _digest_lines(store: Store, quest_id: int) -> list[str]:
    return [
        b.text
        for b in store.chunks.list_chunks_for_ref(quest_id)
        if b.chunk_kind == "quest_log" and b.text.startswith("new literature")
    ]


# ── gate_verdict ────────────────────────────────────────────────────


class TestGateVerdict:
    def test_off_topic_title_fails_no_host_term(self, store: Store) -> None:
        q = _quest(store, GATE)
        p = _paper(
            store,
            "off-topic",
            "Smoke adsorption on activated carbon filters",
            "We study flue-gas adsorption equilibria on porous carbons.",
        )
        ok, why = lit_gate.gate_verdict(store, q, p)
        assert not ok
        assert why == "no host term"

    def test_host_only_fails_no_reaction_term(self, store: Store) -> None:
        q = _quest(store, GATE)
        p = _paper(
            store,
            "host-only",
            "Hydrogen peroxide synthesis on Pd surfaces",
            "Direct H2O2 formation over supported Pd catalysts.",
        )
        ok, why = lit_gate.gate_verdict(store, q, p)
        assert not ok
        assert why == "no reaction term"

    def test_both_axes_pass(self, store: Store) -> None:
        q = _quest(store, GATE)
        p = _paper(
            store,
            "on-topic",
            "Selective nitric oxide electroreduction on doped Pd(111)",
            "We compute NO adsorption and NH3 formation pathways.",
        )
        ok, _why = lit_gate.gate_verdict(store, q, p)
        assert ok

    def test_no_config_passes(self, store: Store) -> None:
        q = _quest(store)
        p = _paper(store, "anything", "An unrelated field entirely", "…")
        ok, _why = lit_gate.gate_verdict(store, q, p)
        assert ok

    def test_partial_config_gates_only_that_axis(self, store: Store) -> None:
        q = _quest(store, {"host": ["Pd"]})
        off = _paper(store, "no-host", "Zinc-air battery review", "…")
        on = _paper(store, "has-host", "Pd membrane hydrogenation", "…")
        assert lit_gate.gate_verdict(store, q, off) == (False, "no host term")
        ok, _ = lit_gate.gate_verdict(store, q, on)
        assert ok

    def test_terms_match_as_tokens_not_substrings(self, store: Store) -> None:
        """``NO`` must not match inside "novel"/"another" — the term is a
        token, not a substring."""
        q = _quest(store, GATE)
        p = _paper(
            store,
            "novelty",
            "A novel anode for Zn-CO2 batteries",
            "Another look at carbonate electrolytes.",
        )
        ok, why = lit_gate.gate_verdict(store, q, p)
        assert not ok
        assert why == "no host term"


# ── run_search_step wiring ───────────────────────────────────────────


class TestRunSearchStepGate:
    def test_rejected_candidates_named_in_line(self, store: Store) -> None:
        q = _quest(store, GATE)
        good = _paper(
            store,
            "good",
            "Nitric oxide electroreduction on doped Pd(111)",
            "NH3 selectivity on Pd facets.",
        )
        bad = _paper(
            store,
            "bad",
            "Nanofluid interfacial thermal resistance review",
            "Heat transfer in colloidal suspensions.",
        )
        meta_less = _paper(store, "stub", "")

        def search_fn(_s: Any, _q: Any, _ex: Any) -> list[tuple[int, float | None]]:
            return [(good, 1.0), (bad, 0.9), (meta_less, 0.8)]

        step = qsearch.run_search_step(store, q, ["NO reduction"], search_fn=search_fn)

        assert step.papers_linked == 1
        lines = _lit_search_lines(store, q)
        assert len(lines) == 1
        line = lines[0]
        assert f"pa{good}" in line
        assert "seen, not linked" in line
        assert f"pa{meta_less} (no title/abstract)" in line
        if bad % 5 == 0:
            assert f"pa{bad} (no host term — sampled for review)" in line
        else:
            assert f"pa{bad} (no host term)" in line
        assert bad not in _serving(store, q)

    def test_no_gate_links_as_before(self, store: Store) -> None:
        """A quest with no lit_gate config links every metadata-carrying
        hit — the gate is opt-in (gr592951's fix must not quietly narrow
        every quest's recall)."""
        q = _quest(store)
        p = _paper(store, "p", "Smoke adsorption on carbon", "unrelated topic")
        qsearch.run_search_step(
            store, q, ["q"], search_fn=lambda _s, _q, _e: [(p, 1.0)]
        )
        assert p in _serving(store, q)

    def test_every_fifth_rejection_is_sampled(self, store: Store) -> None:
        """Every GATE_SAMPLE_MODULUS-th gate rejection is flagged
        ``— sampled for review`` — named for a later ``judgments``
        verdict, NEVER linked past the required gate (td678025 review)."""
        q = _quest(store, GATE)
        # Any run of 5 consecutive ref ids holds a multiple of 5.
        papers = [
            _paper(store, f"rej-{i}", f"Off-topic digest {i}", "unrelated")
            for i in range(6)
        ]
        sampled = {r for r in papers if r % lit_gate.GATE_SAMPLE_MODULUS == 0}
        assert sampled  # at least one candidate is sample-eligible

        qsearch.run_search_step(
            store,
            q,
            ["q"],
            search_fn=lambda _s, _q, _e: [(r, 1.0) for r in papers],
        )

        served = _serving(store, q)
        assert not served  # nothing passed the gate — nothing links
        lines = _lit_search_lines(store, q)
        assert len(lines) == 1
        assert "sampled for review" in lines[0]
        for r in sampled:
            assert f"pa{r} (no host term — sampled for review)" in lines[0]
        for r in set(papers) - sampled:
            assert f"pa{r} (no host term)" in lines[0]
            assert f"pa{r} (no host term — sampled for review)" not in lines[0]

    def test_unreadable_candidate_is_deferred_not_linked(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """A store blip on a candidate's ref defers the link (the
        mandatory no-metadata rule is verified, never assumed) — the
        line names it ``(unreadable — deferred)``."""
        q = _quest(store, GATE)
        p = _paper(store, "blip", "NO reduction on Pd", "NH3 selectivity")
        orig_get_ref = store.get_ref

        def _flaky(*, kind: str, id: int, **kw: Any) -> Any:
            if kind == "paper" and id == p:
                raise RuntimeError("store blip")
            return orig_get_ref(kind=kind, id=id, **kw)

        monkeypatch.setattr(store, "get_ref", _flaky)

        step = qsearch.run_search_step(
            store, q, ["q"], search_fn=lambda _s, _q, _e: [(p, 1.0)]
        )

        assert step.papers_linked == 0
        assert p not in _serving(store, q)
        lines = _lit_search_lines(store, q)
        assert len(lines) == 1
        assert f"pa{p} (unreadable — deferred)" in lines[0]

    def test_gate_failure_writes_no_considered_row(self, store: Store) -> None:
        """Cheap triage never becomes a durable rejection: a gate-rejected
        candidate gets no `considered` verdict — the tick's `judgments`
        channel owns those."""
        q = _quest(store, GATE)
        bad = _paper(store, "bad", "Smoke adsorption on carbon", "unrelated")
        qsearch.run_search_step(
            store, q, ["q"], search_fn=lambda _s, _q, _e: [(bad, 1.0)]
        )
        assert bad not in _serving(store, q)
        assert not store.scoped_considerations(q)


# ── the weekly "new literature" digest ───────────────────────────────


class _ReportingSearch:
    """A ``search_fn`` carrying a :class:`~precis.quest.search.QueryReport`
    so the lit-search entry stamps ``lit_acquired``."""

    def __init__(self, hits: list[tuple[int, float]], acquired: int = 0) -> None:
        self._hits = hits
        self._acquired = acquired

    def __call__(self, _s: Any, _q: Any, _e: Any) -> list[tuple[int, float | None]]:
        return list(self._hits)

    def report_for(self, _q: str) -> qsearch.QueryReport:
        return qsearch.QueryReport(acquired=self._acquired)


class TestWeeklyLiteratureLine:
    def test_digest_lists_only_acquisitions_and_gate_passers(
        self, store: Store
    ) -> None:
        """gr592951 (d): the weekly ``new literature`` line sums the
        window's acquisitions and lists only links that passed the
        gate — a rejected candidate never reaches it."""
        q = _quest(store, GATE)
        good = _paper(
            store,
            "good",
            "Nitric oxide electroreduction on doped Pd(111)",
            "NH3 selectivity on Pd facets.",
        )
        bad = _paper(store, "bad", "Smoke adsorption on carbon", "unrelated")

        qsearch.run_search_step(
            store, q, ["q"], search_fn=_ReportingSearch([(good, 1.0), (bad, 0.9)], 3)
        )

        digests = _digest_lines(store, q)
        assert len(digests) == 1
        assert "acquired 3" in digests[0]
        assert f"pa{good}" in digests[0]
        assert f"pa{bad}" not in digests[0]

    def test_digest_is_weekly_not_per_step(self, store: Store) -> None:
        """A second step inside the window writes no second digest."""
        q = _quest(store, GATE)
        p = _paper(store, "p", "NO reduction on Pd", "NH3")
        hits = [(p, 1.0)]

        qsearch.run_search_step(store, q, ["q"], search_fn=_ReportingSearch(hits))
        qsearch.run_search_step(store, q, ["q2"], search_fn=_ReportingSearch(hits))

        digests = _digest_lines(store, q)
        assert len(digests) == 1
        assert f"pa{p}" in digests[0]

    def test_no_lit_activity_no_digest(self, store: Store) -> None:
        """weekly_literature_line on a quest with no stamped lit-search
        entries writes nothing — a quiet quest stays quiet."""
        q = _quest(store)
        assert qsearch.weekly_literature_line(store, q) is False
        assert _digest_lines(store, q) == []
