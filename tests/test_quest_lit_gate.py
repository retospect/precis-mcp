"""Quest lit-search relevance gate (gr592951, spec slices 1–4).

``run_search_step`` checks every candidate before its ``serves`` link:

* a record carrying neither title nor abstract is NEVER linked — the
  lit-search line names it ``[pa<id>] (no title/abstract)``;
* the quest's ``meta.lit_gate`` term lists hold a candidate out when its
  title+abstract misses a configured side — ``(no host term)`` /
  ``(no reaction term)`` — logged, never a durable ``considered`` verdict;
* every ``ref_id % 5 == 0`` gate rejection links anyway, flagged
  ``(sampled past gate)`` — cheap triage is not the only discovery path;
* no ``lit_gate`` config means the gate is off and every candidate passes.

Runs against real PG (the ``store`` fixture) so the link/logbook rows are
real, matching ``tests/test_quest_search_judged_out.py``.
"""

from __future__ import annotations

import re
from typing import Any

from precis.quest import lit_gate
from precis.quest import search as qsearch
from precis.store import Store

GATE = {
    "host": ["Pd", "Pd(111)", "Ir"],
    "reaction": ["NO", "NH3", "nitric oxide reduction"],
}


def _quest(store: Store, gate: Any = None) -> int:
    meta = {"lit_gate": gate} if gate is not None else None
    return store.insert_ref(
        kind="quest", slug=None, title="NO→NH3 on doped Pd(111)", meta=meta
    ).id


def _paper(store: Store, slug: str, title: str, abstract: str | None = None) -> int:
    meta = {"abstract": abstract} if abstract else None
    return store.insert_ref(kind="paper", slug=slug, title=title, meta=meta).id


def _serving(store: Store, quest_id: int) -> set[int]:
    return {
        ln.src_ref_id
        for ln in store.links_for(quest_id, direction="in", relation="serves")
    }


def _log_text(store: Store, quest_id: int) -> str:
    return "\n".join(
        b.text
        for b in store.chunks.list_chunks_for_ref(quest_id)
        if b.chunk_kind == "quest_log"
    )


def _considered(store: Store, quest_id: int) -> list[Any]:
    return store.links_for(quest_id, direction="out", relation="considered")


# ── the pure term check ───────────────────────────────────────────────


class TestTermHit:
    def test_no_matches_no_but_not_notable(self) -> None:
        assert lit_gate._term_hit("NO", "electrochemical NO reduction")
        assert not lit_gate._term_hit("NO", "a notable advance")

    def test_facet_term_matches_surface_text(self) -> None:
        assert lit_gate._term_hit("Pd(111)", "on Pd(111) surfaces")
        assert not lit_gate._term_hit("Pd(111)", "on Pd(100) surfaces")

    def test_case_insensitive(self) -> None:
        assert lit_gate._term_hit("NH3", "selectivity toward nh3")


class TestVerdictForText:
    def test_off_topic_fails_no_host_term(self) -> None:
        ok, reason = lit_gate.verdict_for_text(
            GATE, "Smoke adsorption on activated carbon filters"
        )
        assert ok is False and reason == "no host term"

    def test_host_only_fails_no_reaction_term(self) -> None:
        ok, reason = lit_gate.verdict_for_text(
            GATE, "Hydrogen spillover on Pd surfaces"
        )
        assert ok is False and reason == "no reaction term"

    def test_both_sides_hit_passes(self) -> None:
        ok, reason = lit_gate.verdict_for_text(
            GATE, "Nitric oxide reduction to NH3 on Pd(111)"
        )
        assert ok is True and reason == ""

    def test_no_terms_passes(self) -> None:
        assert lit_gate.verdict_for_text({}, "anything at all") == (True, "")

    def test_absent_side_skips_that_check(self) -> None:
        ok, _ = lit_gate.verdict_for_text({"host": ["Pd"]}, "Pd nanoparticles for ORR")
        assert ok is True


# ── gate_verdict against the store ────────────────────────────────────


class TestGateVerdict:
    def test_no_config_passes(self, store: Store) -> None:
        quest = _quest(store)
        off = _paper(store, "lg-nocfg-off", "Smoke adsorption on carbon")
        assert lit_gate.gate_verdict(store, quest, off) == (True, "")

    def test_misshaped_config_is_gate_off(self, store: Store) -> None:
        quest = _quest(store, gate="Pd NO")
        off = _paper(store, "lg-badcfg-off", "Smoke adsorption on carbon")
        assert lit_gate.gate_verdict(store, quest, off) == (True, "")

    def test_configured_gate(self, store: Store) -> None:
        quest = _quest(store, gate=GATE)
        off = _paper(store, "lg-cfg-off", "Nanofluid interfacial resistance")
        on = _paper(
            store,
            "lg-cfg-on",
            "NH3 synthesis",
            abstract="Nitric oxide reduction on Pd(111) terraces",
        )
        assert lit_gate.gate_verdict(store, quest, off) == (False, "no host term")
        assert lit_gate.gate_verdict(store, quest, on) == (True, "")


# ── run_search_step integration ────────────────────────────────────────


class TestRunSearchStepGate:
    def _search(self, hits: list[tuple[int, float]]):
        def search_fn(_store: Any, _query: str, _exclude: Any) -> Any:
            return list(hits)

        return search_fn

    def test_off_topic_seen_not_linked_no_verdict(self, store: Store) -> None:
        quest = _quest(store, gate=GATE)
        # Mint off-topic candidates until one is not a sampling residue.
        offs = [
            _paper(store, f"lg-off-{i}", "Smoke adsorption on activated carbon")
            for i in range(6)
        ]
        off = next(p for p in offs if p % 5 != 0)
        on = _paper(
            store,
            "lg-on",
            "Nitric oxide reduction to ammonia on Pd(111)",
        )

        step = qsearch.run_search_step(
            store,
            quest,
            ["NO reduction"],
            search_fn=self._search([(off, 1.0), (on, 0.9)]),
        )

        serving = _serving(store, quest)
        assert on in serving
        assert off not in serving
        log = _log_text(store, quest)
        assert f"[pa{off}] (no host term)" in log
        assert f"[pa{on}]" in log
        # Cheap triage is not a verdict: no considered edge was written.
        assert _considered(store, quest) == []
        assert any("seen, not linked" in n for n in step.notes)

    def test_every_fifth_rejection_is_sampled(self, store: Store) -> None:
        quest = _quest(store, gate=GATE)
        cands = [
            _paper(store, f"lg-s-{i}", "Smoke adsorption on activated carbon")
            for i in range(6)
        ]
        sampled = next(p for p in cands if p % 5 == 0)
        held = next(p for p in cands if p % 5 != 0)

        qsearch.run_search_step(
            store,
            quest,
            ["carbon"],
            search_fn=self._search([(sampled, 1.0), (held, 0.9)]),
        )

        serving = _serving(store, quest)
        assert sampled in serving
        assert held not in serving
        log = _log_text(store, quest)
        assert f"[pa{sampled}] (sampled past gate)" in log
        assert f"[pa{held}] (no host term)" in log

    def test_unconfigured_gate_links_everything(self, store: Store) -> None:
        quest = _quest(store)  # no meta.lit_gate → gate off
        off = _paper(store, "lg-open-off", "Zn–CO2 battery review")
        on = _paper(store, "lg-open-on", "anything relevant")

        qsearch.run_search_step(
            store, quest, ["q"], search_fn=self._search([(off, 1.0), (on, 0.9)])
        )

        assert {off, on} <= _serving(store, quest)

    def test_logbook_line_brackets_every_handle(self, store: Store) -> None:
        """gr690465: the tick hands this line to the model through the
        logbook tail, and the model copies the handle form it sees — a bare
        ``pa<id>`` there becomes dead text in the dossier prose."""
        quest = _quest(store, gate=GATE)
        offs = [
            _paper(store, f"lg-br-off-{i}", "Smoke adsorption on activated carbon")
            for i in range(6)
        ]
        off = next(p for p in offs if p % 5 != 0)
        first = _paper(store, "lg-br-1", "Nitric oxide reduction to ammonia on Pd(111)")
        second = _paper(store, "lg-br-2", "NH3 selectivity of NO reduction on Ir")

        qsearch.run_search_step(
            store,
            quest,
            ["NO reduction"],
            search_fn=self._search([(first, 1.0), (off, 0.95), (second, 0.9)]),
        )

        assert {first, second} <= _serving(store, quest)
        assert off not in _serving(store, quest)
        log = _log_text(store, quest)
        assert f"linked 2 source(s): [pa{first}], [pa{second}]" in log
        assert f"seen not linked: [pa{off}] (no host term)" in log
        outside = re.sub(r"\[[a-z]{2}\d+\]", "", log)
        assert re.findall(r"\b[a-z]{2}\d+\b", outside) == []
