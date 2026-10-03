"""Tests for the ``roadmap`` tick body — :mod:`precis.quest.roadmap_tick`
(docs/backlog/bootstrap-roadmap-quest.md, acceptance criteria 1–3, 5–7).

Runs against real PG (the ``store`` fixture) through the same handlers the
tick writes through, with a scripted ``.complete()`` client standing in for
the model. AC4 (the write guards) lives in the handler tests; AC8 (the
materials default is untouched) is ``test_quest_tick.py``'s own pair.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.handlers.todo import TodoHandler
from precis.quest import roadmap_ledger as ledger
from precis.quest import roadmap_tick as rt
from precis.quest.dossier import dossier_ref_id
from precis.quest.gaps import quest_gaps
from precis.quest.logbook import append_entry
from precis.quest.search import AcquiringSearch, QueryReport
from precis.quest.weave_tick import mark_roadmap_quest
from precis.store.types import ChunkInsert
from precis_se.handler import SeHandler
from tests.test_quest_roadmap_ledger import KEY, _qid, make_root, make_rung

#: AC1 — the four materials-only prompt tokens (mirrors
#: ``test_quest_tick.py::TestQuestBodyInquiry._MATERIALS_ONLY_TOKENS``).
_MATERIALS_ONLY_TOKENS = (
    "measured barriers",
    "awaiting a sim",
    "candidate materials to simulate",
    '"structure": {"cell"',
)


class ScriptedClient:
    """``.complete(messages)`` returns the next scripted JSON payload; every
    prompt it saw is kept on ``prompts`` for assertions."""

    def __init__(self, *replies: dict[str, Any]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.prompts.append("\n".join(m["content"] for m in messages))
        payload = self.replies.pop(0) if self.replies else {}
        return SimpleNamespace(text=json.dumps(payload), data=None)


def _quests(store: Any) -> QuestHandler:
    return QuestHandler(hub=Hub(store=store))


def _todos(store: Any) -> TodoHandler:
    return TodoHandler(hub=Hub(store=store))


def make_pathway(store: Any, root: int, title: str = "Pathway: DNA scaffold") -> int:
    pid = _qid(_quests(store).put(text=title, tags=["STATUS:dormant"]))
    mark_roadmap_quest(store, pid)
    store.add_link(src_ref_id=pid, dst_ref_id=root, relation="serves")
    return pid


def seed_paper(store: Any, *, cite_key: str, body: str) -> int:
    ref = store.insert_ref(
        kind="paper", slug=cite_key, title=f"Paper {cite_key}", meta={}
    )
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=body, meta={})])
    return ref.id


def _entries(store: Any, ref_id: int, entry_type: str) -> list[Any]:
    return [
        b
        for b in store.chunks.list_chunks_for_ref(ref_id)
        if b.chunk_kind == "quest_log"
        and (b.meta or {}).get("entry_type") == entry_type
    ]


def _rungs_on(store: Any, cap: int) -> list[int]:
    ids = [
        ln.src_ref_id for ln in store.links_for(cap, direction="in", relation="serves")
    ]
    refs = store.fetch_refs_by_ids(set(ids))
    return [i for i in ids if (r := refs.get(i)) is not None and r.kind == "todo"]


def _tags(store: Any, ref_id: int) -> set[str]:
    return {str(t) for t in store.tags_for(ref_id)}


def _pinned_ledger_chunks(store: Any, root: int) -> list[Any]:
    did = dossier_ref_id(store, root)
    if did is None:
        return []
    return [
        c
        for c in store.drafts.reading_order(did)
        if (c.meta or {}).get("pinned") == rt.LEDGER_PINNED
    ]


def _rung_reply(
    cap: int, pathway: int, *, value: float, evidence: list[str], title: str
) -> dict[str, Any]:
    return {
        "outcome": "rung",
        "rung": {
            "title": title,
            "deliverable": "Place one tile at the cited accuracy and read it back.",
            "pathway": f"qu{pathway}",
            "consumes": [],
            "produces": [
                {
                    "capability": f"qu{cap}",
                    "key": KEY,
                    "value": value,
                    "evidence": evidence,
                }
            ],
        },
    }


# ── role selection (pure graph read) ──────────────────────────────────


class TestRoadmapRole:
    def test_no_demand_picks_demand(self, store: Any) -> None:
        root, cap = make_root(store, demand=None, supply=None)
        choice = rt.roadmap_role(store, root)
        assert choice is not None
        assert (choice.role, choice.capability_id, choice.key) == ("demand", cap, KEY)
        assert choice.gap.handle == f"qu{cap}"

    def test_role_prompt_shows_the_whole_capability_statement(self, store: Any) -> None:
        long_title = (
            "Capability: positional accuracy: place a building block where we "
            "choose, in liquid at room temperature, to within the distance the "
            "next step tolerates"
        )
        root, _cap = make_root(store, demand=None, supply=None, cap_title=long_title)
        choice = rt.roadmap_role(store, root)
        assert choice is not None and choice.role == "demand"
        assert choice.capability_statement == long_title
        assert len(choice.capability_title) == 60, "the ledger stub is untouched"
        prompt = rt.build_role_prompt(
            store, store.get_ref(kind="quest", id=root), choice
        )
        assert long_title in prompt
        assert "where we choose,\n" not in prompt, (
            "the 60-char stub must not be the heading"
        )

    def test_demand_without_supply_picks_supply(self, store: Any) -> None:
        root, _cap = make_root(store, demand=2.0, supply=None)
        choice = rt.roadmap_role(store, root)
        assert choice is not None and choice.role == "supply"

    def test_worse_supply_picks_bridge_with_the_unmet_gap(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=6.0)
        choice = rt.roadmap_role(store, root)
        assert choice is not None and choice.role == "bridge"
        assert choice.gap.kind == "unmet-capability"
        assert choice.gap.handle == f"qu{cap}"
        assert (choice.demanded, choice.best_supply) == (2.0, 6.0)

    def test_met_axis_yields_no_role(self, store: Any) -> None:
        root, _cap = make_root(store, demand=2.0, supply=1.5)
        assert rt.roadmap_role(store, root) is None

    def test_bridge_outranks_supply_outranks_demand(self, store: Any) -> None:
        # Three capabilities in link order: no-demand, no-supply, unmet.
        root, cap_demand = make_root(store, demand=None, supply=None)
        h = _quests(store)
        cap_supply = _qid(h.put(text="Capability: cycle time"))
        h.edit(
            id=cap_supply,
            meta={
                "quest_body": "roadmap",
                "rubric_objectives": [{"key": "cycle_time_s", "sense": "min"}],
                "demand": {
                    "cycle_time_s": {"value": 1.0, "source": "se:x", "reason": "r"}
                },
            },
        )
        store.add_link(src_ref_id=cap_supply, dst_ref_id=root, relation="serves")
        cap_unmet = _qid(h.put(text="Capability: force"))
        h.edit(
            id=cap_unmet,
            meta={
                "quest_body": "roadmap",
                "rubric_objectives": [{"key": "force_pn", "sense": "max"}],
                "demand": {
                    "force_pn": {"value": 10.0, "source": "se:x", "reason": "r"}
                },
                "supply": {"force_pn": {"value": 3.0, "evidence": ["fi1"]}},
            },
        )
        store.add_link(src_ref_id=cap_unmet, dst_ref_id=root, relation="serves")

        choice = rt.roadmap_role(store, root)
        assert choice is not None
        assert (choice.role, choice.capability_id) == ("bridge", cap_unmet)
        # Meet the unmet axis → supply is next; then demand.
        h.edit(
            id=cap_unmet,
            meta={"supply": {"force_pn": {"value": 12.0, "evidence": ["fi1"]}}},
        )
        choice = rt.roadmap_role(store, root)
        assert choice is not None
        assert (choice.role, choice.capability_id) == ("supply", cap_supply)
        h.edit(
            id=cap_supply,
            meta={"supply": {"cycle_time_s": {"value": 0.5, "evidence": ["fi2"]}}},
        )
        choice = rt.roadmap_role(store, root)
        assert choice is not None
        assert (choice.role, choice.capability_id) == ("demand", cap_demand)

    def test_lowest_capability_first_by_consumer_count(self, store: Any) -> None:
        # Two unmet capabilities; the SECOND in link order is consumed by a
        # rung, so it sits lower in the chain and goes first.
        root, cap_a = make_root(store, demand=2.0, supply=6.0)
        h = _quests(store)
        cap_b = _qid(h.put(text="Capability: force"))
        h.edit(
            id=cap_b,
            meta={
                "quest_body": "roadmap",
                "rubric_objectives": [{"key": "force_pn", "sense": "max"}],
                "demand": {
                    "force_pn": {"value": 10.0, "source": "se:x", "reason": "r"}
                },
                "supply": {"force_pn": {"value": 3.0, "evidence": ["fi1"]}},
            },
        )
        store.add_link(src_ref_id=cap_b, dst_ref_id=root, relation="serves")
        # A done rung on cap_a that CONSUMES cap_b's axis (a value from below).
        rid = make_rung(store, cap_a, value=9.0, status="done")
        _todos(store).tag(
            id=rid,
            meta={
                "rung": {
                    "pathway": "qu161906",
                    "consumes": [
                        {"capability": f"qu{cap_b}", "key": "force_pn", "value": 10.0}
                    ],
                    "produces": [
                        {
                            "capability": f"qu{cap_a}",
                            "key": KEY,
                            "value": 9.0,
                            "evidence": ["fi7"],
                        }
                    ],
                }
            },
        )
        choice = rt.roadmap_role(store, root)
        assert choice is not None
        assert (choice.role, choice.capability_id) == ("bridge", cap_b)

    def test_non_roadmap_quest_has_no_role(self, store: Any) -> None:
        qid = _qid(_quests(store).put(text="a plain materials quest"))
        assert rt.roadmap_role(store, qid) is None

    def test_role_tiers(self) -> None:
        assert rt.role_tier("demand") == "big"
        assert rt.role_tier("supply") == "big"
        assert rt.role_tier("bridge") == "frontier"
        assert rt.role_tier(None) == "big"


# ── AC1: demand role, dry run ─────────────────────────────────────────


@pytest.fixture
def se_handler(hub: Hub, store: Any) -> SeHandler:
    return SeHandler(hub=hub)


class TestDemandRole:
    def test_dry_run_prints_demand_role_with_the_se_measures(
        self, store: Any, se_handler: SeHandler
    ) -> None:
        root, _cap = make_root(store, demand=None, supply=None)
        se_handler.put(
            id="hexfold-valve",
            text=json.dumps(
                {
                    "ops": [
                        {
                            "op": "add_block",
                            "name": "port",
                            "envelope": "box:w0.001d0.001h0.001",
                        },
                        {
                            "op": "add_measure",
                            "block": "port",
                            "name": "port_pitch",
                            "value": 2e-9,
                        },
                    ]
                }
            ),
        )
        se_ref = store.get_ref(kind="se", id="hexfold-valve")
        assert se_ref is not None
        store.add_link(src_ref_id=se_ref.id, dst_ref_id=root, relation="serves")

        result = rt.roadmap_tick(store, None, root, dry_run=True)
        assert result["ok"] and result["applied"] is False
        assert result["role"] == "demand"
        report = rt.render_role_report(result)
        assert report.startswith("role: demand")
        assert "se:hexfold-valve" in result["prompt"]
        assert "port_pitch" in result["prompt"]  # the part's measure, by name
        for token in _MATERIALS_ONLY_TOKENS:
            assert token not in result["prompt"], token
        # A dry run writes nothing: no demand, no logbook, no ledger chunk.
        cap_ref = store.get_ref(kind="quest", id=_cap)
        assert "demand" not in (cap_ref.meta or {})
        assert _pinned_ledger_chunks(store, root) == []

    def test_live_demand_writes_meta_and_a_decision(self, store: Any) -> None:
        root, cap = make_root(store, demand=None, supply=None)
        client = ScriptedClient(
            {
                "value": 2.0,
                "source": "se:hexfold-valve",
                "reason": "port pitch is 2 nm",
                "calculation": "pitch / 1 = 2 nm",
            }
        )
        result = rt.roadmap_tick(store, client, root)
        assert result["ok"] and result["role"] == "demand"
        cap_ref = store.get_ref(kind="quest", id=cap)
        assert cap_ref.meta["demand"][KEY] == {
            "value": 2.0,
            "source": "se:hexfold-valve",
            "reason": "port pitch is 2 nm",
        }
        decisions = _entries(store, cap, "decision")
        assert len(decisions) == 1 and "pitch / 1 = 2 nm" in decisions[0].text
        # A demand write opens an unmet gap → not dry, but no ledger value
        # moved → not improved either.
        before, after = result["gap_count"]
        assert after == before + 1  # the new unmet-capability gap
        assert result["dry"] is False and result["improved"] is False
        assert len(_pinned_ledger_chunks(store, root)) == 1


# ── AC2: supply role ──────────────────────────────────────────────────


class TestSupplyRole:
    def test_supply_emits_searches_mints_hubs_and_no_jobs(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=None)
        paper = seed_paper(
            store,
            cite_key="dna23",
            body="DNA origami placement achieved 2.1 nm positional accuracy.",
        )
        client = ScriptedClient(
            {"searches": ["DNA origami positional accuracy nm"]},
            {
                "findings": [
                    {
                        "claim": "DNA origami placement achieves 2.1 nm positional accuracy",
                        "value": 2.1,
                        "paper": f"pa{paper}",
                        "quote": "DNA origami placement achieved 2.1 nm positional accuracy.",
                    }
                ]
            },
        )
        seen_queries: list[str] = []

        def _search(
            store_: Any, query: str, exclude: list[int]
        ) -> list[tuple[int, float]]:
            seen_queries.append(query)
            return [(paper, 1.0)]

        result = rt.roadmap_tick(store, client, root, search_fn=_search)
        assert result["ok"] and result["role"] == "supply"
        assert seen_queries == ["DNA origami positional accuracy nm"]
        assert result["searches_run"] == 1 and result["papers_linked"] == 1
        assert len(result["hubs"]) == 1 and result["hubs"][0].startswith("fi")
        cap_ref = store.get_ref(kind="quest", id=cap)
        assert cap_ref.meta["supply"][KEY] == {"value": 2.1, "evidence": result["hubs"]}
        # The literature lane only: no relax / autocatpath / sandbox job minted.
        with store.pool.connection() as conn:
            (jobs,) = conn.execute(
                "SELECT count(*) FROM refs WHERE kind = 'job' AND retired_at IS NULL"
            ).fetchone()
        assert jobs == 0
        # The tick's own supply write IS a deed: none → 2.1, one entry each.
        assert result["deeds"] == 2 and result["improved"] is True
        assert result["ledger_delta"] == {f"qu{cap}:{KEY}": [None, 2.1]}
        assert len(_entries(store, cap, "milestone")) == 1
        assert len(_entries(store, root, "milestone")) == 1
        (row,) = ledger.compute_ledger(store, root)
        assert (row.best_supply, row.state) == (2.1, "unmet")

    def test_extraction_sees_this_ticks_paper_past_the_cap(self, store: Any) -> None:
        # qu453869 2026-10-02: with more paper servers than the cap, the
        # oldest-first window hid every paper the tick's own search linked.
        root, cap = make_root(store, demand=2.0, supply=None)
        for i in range(rt._SUPPLY_MAX_PAPERS + 1):
            old = seed_paper(
                store, cite_key=f"old{i}", body="Unrelated origami review."
            )
            store.add_link(src_ref_id=old, dst_ref_id=cap, relation="serves")
        new = seed_paper(
            store, cite_key="fresh24", body="Probe drift measured at 0.2 nm per cycle."
        )
        client = ScriptedClient(
            {"searches": ["probe drift per cycle"]}, {"findings": []}
        )
        result = rt.roadmap_tick(
            store, client, root, search_fn=lambda s, q, ex: [(new, 1.0)]
        )
        assert result["papers_linked"] == 1
        extraction_prompt = client.prompts[1]
        assert f"pa{new}" in extraction_prompt
        assert "0.2 nm per cycle" in extraction_prompt

    def test_supply_without_papers_writes_nothing(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=None)
        client = ScriptedClient({"searches": ["nothing held"]})
        result = rt.roadmap_tick(store, client, root, search_fn=lambda s, q, ex: [])
        assert result["ok"] and result["role"] == "supply"
        assert "supply_written" not in result
        assert "supply" not in (store.get_ref(kind="quest", id=cap).meta or {})
        assert result["dry"] is True

    def test_supply_gate_drops_textless_papers_and_checks_every_quote(
        self, store: Any
    ) -> None:
        # pa459574, 2026-10-02: a paper with ZERO chunks yielded a recalled
        # 1.2 nm that was a method uncertainty. Four findings, one per verdict.
        root, cap = make_root(store, demand=2.0, supply=None)
        empty = store.insert_ref(
            kind="paper", slug="empty01", title="Paper empty01", meta={}
        ).id
        refuse_body = "Placement was reproducible across all 40 origami tiles."
        hold_body = "An uncertainty margin of 1.2 nm was estimated by SAXS."
        ok_body = "DNA origami placement achieved 2.1 nm positional accuracy."
        refused = seed_paper(store, cite_key="refuse01", body=refuse_body)
        held = seed_paper(store, cite_key="hold01", body=hold_body)
        ok = seed_paper(store, cite_key="ok01", body=ok_body)
        for pid in (empty, refused, held, ok):
            store.add_link(src_ref_id=pid, dst_ref_id=cap, relation="serves")
        client = ScriptedClient(
            {"searches": ["positional accuracy"]},
            {
                "findings": [
                    # Recalled number, paraphrased quote (min sense: 0.5 would win).
                    {
                        "claim": "Placement reaches 0.5 nm",
                        "value": 0.5,
                        "paper": f"pa{refused}",
                        "quote": "Placement reaches 0.5 nm.",
                    },
                    {
                        "claim": "Placement uncertainty is 1.2 nm",
                        "value": 1.2,
                        "paper": f"pa{held}",
                        "quote": hold_body,
                    },
                    {
                        "claim": "DNA origami placement achieves 2.1 nm",
                        "value": 2.1,
                        "paper": f"pa{ok}",
                        "quote": ok_body,
                    },
                    # The text-less paper is not in the prompt, so not citable.
                    {
                        "claim": "Placement reaches 0.1 nm",
                        "value": 0.1,
                        "paper": f"pa{empty}",
                        "quote": "Placement reaches 0.1 nm.",
                    },
                ]
            },
        )
        result = rt.roadmap_tick(store, client, root, search_fn=lambda s, q, ex: [])
        prompt = client.prompts[1]
        # (the search prompt still counts it among the papers already held)
        assert not re.search(rf"\bpa{empty}\b", prompt) and ok_body in prompt
        assert (result["refused"], result["held"]) == (1, 1)
        assert "1 refused, 1 held" in result["note"]
        assert len(result["hubs"]) == 2  # held + ok; the refused one minted none
        hub_by_title = {}
        for h in result["hubs"]:
            ref = store.get_ref(kind="finding", id=int(h[2:]))
            hub_by_title[ref.title] = int(h[2:])
        assert set(hub_by_title) == {
            "Placement uncertainty is 1.2 nm",
            "DNA origami placement achieves 2.1 nm",
        }
        held_hub = hub_by_title["Placement uncertainty is 1.2 nm"]
        ok_hub = hub_by_title["DNA origami placement achieves 2.1 nm"]
        assert "review:supply-held" in _tags(store, held_hub)
        assert "review:supply-held" not in _tags(store, ok_hub)
        held_entries = [
            e for e in _entries(store, cap, "observation") if "HELD" in e.text
        ]
        assert len(held_entries) == 1
        assert hold_body in held_entries[0].text
        assert "uncertainty cue" in held_entries[0].text
        assert f"pa{held}" in held_entries[0].text
        # Only the ok finding feeds the supply; the held 1.2 and refused 0.5
        # would each have won under sense=min had they counted.
        assert result["supply_written"] == {
            "key": KEY,
            "value": 2.1,
            "evidence": [f"fi{ok_hub}"],
        }
        cap_ref = store.get_ref(kind="quest", id=cap)
        assert cap_ref.meta["supply"][KEY]["value"] == 2.1
        # The ok hub is grounded at the card's chunk.
        (chunk,) = store.chunks.list_chunks_for_ref(ok)
        links = store.links_for(ok_hub, direction="in") + store.links_for(
            ok_hub, direction="out"
        )
        assert f"pc{chunk.id}" in [(ln.meta or {}).get("source_handle") for ln in links]

    def test_supply_gate_refusal_alone_leaves_the_tick_dry(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=None)
        p = seed_paper(store, cite_key="para01", body="Tiles were reproducible.")
        client = ScriptedClient(
            {"searches": ["x"]},
            {
                "findings": [
                    {
                        "claim": "Placement reaches 0.5 nm",
                        "value": 0.5,
                        "paper": f"pa{p}",
                        "quote": "Placement reaches 0.5 nm.",
                    }
                ]
            },
        )
        result = rt.roadmap_tick(
            store, client, root, search_fn=lambda s, q, ex: [(p, 1.0)]
        )
        assert result["refused"] == 1 and result["hubs"] == []
        assert "supply_written" not in result
        assert "supply" not in (store.get_ref(kind="quest", id=cap).meta or {})
        assert result["dry"] is True


class _SpySearch(AcquiringSearch):
    """An AcquiringSearch whose graph always "answers" (10 local hits in prod):
    the outside leg runs only when ``force_external`` is set. Records the flag
    as seen DURING each search call."""

    def __init__(self, hits: list[tuple[int, float]] | None = None) -> None:
        super().__init__(1, SimpleNamespace(store=object(), embedder=None))
        self.hits = hits or []
        self.seen: list[bool] = []

    def __call__(
        self, store: Any, query: str, exclude_ref_ids: list[int]
    ) -> list[tuple[int, float | None]]:
        self.seen.append(self.force_external)
        self._reports[query] = QueryReport(
            local=10,
            external_ran=self.force_external,
            forced=self.force_external,
        )
        return list(self.hits)


class _FailingOutsideSearch(_SpySearch):
    """A :class:`_SpySearch` whose outside leg runs and fails (S2 429, outage,
    or a venv without the S2 client)."""

    def __call__(
        self, store: Any, query: str, exclude_ref_ids: list[int]
    ) -> list[tuple[int, float | None]]:
        out = super().__call__(store, query, exclude_ref_ids)
        if self.force_external:
            self._reports[query].external_error = "HTTPError: 429"
        return out


def _supply_metas(store: Any, cap: int, meta_key: str) -> list[dict[str, Any]]:
    return [
        b.meta[meta_key]
        for b in store.chunks.list_chunks_for_ref(cap)
        if b.chunk_kind == "quest_log" and meta_key in (b.meta or {})
    ]


def _unmet_gap_detail(store: Any, root: int) -> str:
    (gap,) = [g for g in quest_gaps(store, root) if g.kind == "unmet-capability"]
    return gap.detail


class TestSupplyEscalation:
    """A dry supply tick escalates the next one to search outside; two dry
    ticks that searched outside log "not found outside" once."""

    def _dry_tick(self, store: Any, root: int, fn: Any, query: str) -> dict[str, Any]:
        client = ScriptedClient({"searches": [query]})
        result = rt.roadmap_tick(store, client, root, search_fn=fn)
        assert result["ok"] and result["role"] == "supply"
        return result

    def test_dry_ticks_escalate_then_stop_and_log_once(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=None)
        fn = _SpySearch()

        # tick 1: no history → local-first, nothing outside.
        self._dry_tick(store, root, fn, "q1")
        assert fn.seen == [False]
        assert _supply_metas(store, cap, "supply_outcome") == [
            {"key": KEY, "dry": True, "external": False, "queries": ["q1"]}
        ]
        assert "outside searched: no" in _entries(store, cap, "observation")[-1].text

        # tick 2: escalated; the flag is set DURING the search, restored after.
        self._dry_tick(store, root, fn, "q2")
        assert fn.seen == [False, True]
        assert fn.force_external is False
        assert _supply_metas(store, cap, "supply_outcome")[-1] == {
            "key": KEY,
            "dry": True,
            "external": True,
            "queries": ["q2"],
        }
        assert "not found outside" not in _unmet_gap_detail(store, root)

        # tick 3: still escalated (only one outside-dry tick so far).
        self._dry_tick(store, root, fn, "q3")
        assert fn.seen == [False, True, True]
        assert fn.force_external is False
        assert _supply_metas(store, cap, "supply_not_found_outside") == []

        # tick 4: two outside-dry ticks → local-first again, verdict logged once.
        self._dry_tick(store, root, fn, "q4")
        assert fn.seen == [False, True, True, False]
        (nf,) = _supply_metas(store, cap, "supply_not_found_outside")
        assert nf == {"key": KEY, "queries": ["q2", "q3"]}
        nf_text = [
            e.text for e in _entries(store, cap, "observation") if "not found" in e.text
        ]
        assert len(nf_text) == 1 and "q2; q3" in nf_text[0]
        assert _unmet_gap_detail(store, root).endswith(
            "; not found outside (2 queries)"
        )

        # tick 5: still dry, still not escalating, no second verdict entry.
        self._dry_tick(store, root, fn, "q5")
        assert fn.seen[-1] is False
        assert len(_supply_metas(store, cap, "supply_not_found_outside")) == 1

        # tick 6: a tick that writes a supply resets the streak and the verdict.
        paper = seed_paper(
            store,
            cite_key="dna23",
            body="DNA origami placement achieved 2.1 nm positional accuracy.",
        )
        fn.hits = [(paper, 1.0)]
        client = ScriptedClient(
            {"searches": ["q6"]},
            {
                "findings": [
                    {
                        "claim": "DNA origami placement achieves 2.1 nm accuracy",
                        "value": 2.1,
                        "paper": f"pa{paper}",
                        "quote": "DNA origami placement achieved 2.1 nm positional accuracy.",
                    }
                ]
            },
        )
        result = rt.roadmap_tick(store, client, root, search_fn=fn)
        assert result["supply_written"]["value"] == 2.1
        assert _supply_metas(store, cap, "supply_outcome")[-1]["dry"] is False
        assert ledger.supply_history(store, cap, KEY) == ledger.SupplyHistory()
        assert "not found outside" not in _unmet_gap_detail(store, root)

    def test_failed_outside_search_does_not_count_as_searched(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=None)
        fn = _FailingOutsideSearch()

        self._dry_tick(store, root, fn, "q1")
        self._dry_tick(store, root, fn, "q2")
        assert fn.seen == [False, True]
        assert _supply_metas(store, cap, "supply_outcome")[-1] == {
            "key": KEY,
            "dry": True,
            "external": False,
            "queries": ["q2"],
            "external_error": "HTTPError: 429",
        }
        last = _entries(store, cap, "observation")[-1].text
        assert "outside search failed: HTTPError: 429" in last

        # Failed outside searches keep the key escalating and never reach the
        # two-tick "not found outside" verdict.
        for q in ("q3", "q4", "q5"):
            self._dry_tick(store, root, fn, q)
        assert fn.seen == [False, True, True, True, True]
        assert ledger.supply_history(store, cap, KEY).ext_dry == 0
        assert _supply_metas(store, cap, "supply_not_found_outside") == []
        assert "not found outside" not in _unmet_gap_detail(store, root)

    def test_plain_search_fn_is_never_escalated(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=None)
        seen: list[str] = []

        def _plain(s: Any, q: str, ex: list[int]) -> list[tuple[int, float]]:
            seen.append(q)
            return []

        for q in ("a", "b"):
            result = rt.roadmap_tick(
                store, ScriptedClient({"searches": [q]}), root, search_fn=_plain
            )
            assert result["ok"]
        # No report_for → external never counted; the outcome is still logged.
        assert [o["external"] for o in _supply_metas(store, cap, "supply_outcome")] == [
            False,
            False,
        ]
        assert seen == ["a", "b"]

    def test_history_streak_resets_on_a_written_tick(self, store: Any) -> None:
        _root, cap = make_root(store, demand=2.0, supply=None)

        def _log(meta_key: str, payload: dict[str, Any]) -> None:
            append_entry(
                store,
                cap,
                text="x",
                entry_type="observation",
                by="agent",
                extra_meta={meta_key: payload},
            )

        def _outcome(*, dry: bool, external: bool, q: str, key: str = KEY) -> None:
            _log(
                "supply_outcome",
                {"key": key, "dry": dry, "external": external, "queries": [q]},
            )

        _outcome(dry=True, external=True, q="old")
        _outcome(dry=False, external=False, q="wrote")  # resets the streak
        _outcome(dry=True, external=False, q="a")
        _outcome(dry=True, external=True, q="b")
        _outcome(dry=True, external=True, q="other-key", key="other")  # ignored
        h = ledger.supply_history(store, cap, KEY)
        assert (h.streak, h.ext_dry, h.ext_queries) == (2, 1, ("b",))
        assert h.not_found_queries is None
        _log("supply_not_found_outside", {"key": KEY, "queries": ["b", "c"]})
        assert ledger.supply_history(store, cap, KEY).not_found_queries == 2
        assert ledger.not_found_outside_note(store, cap, KEY) == (
            "; not found outside (2 queries)"
        )
        assert ledger.not_found_outside_note(store, cap, "other") == ""
        _outcome(dry=False, external=False, q="wrote again")
        assert ledger.supply_history(store, cap, KEY).not_found_queries is None
        assert ledger.not_found_outside_note(store, cap, KEY) == ""


class TestCheckSupplyQuote:
    @staticmethod
    def _check(
        quote: str, text: str, value: float = 1.2, unit: str | None = "nm"
    ) -> tuple[str, str]:
        r = rt.check_supply_quote(quote, text, value, unit)
        return r.verdict, r.reason

    def test_ok(self) -> None:
        s = "Tiles landed within 1.2 nm of the target site."
        assert self._check(s, "Intro. " + s + " More.") == ("ok", "")

    def test_whitespace_collapsed_in_the_substring_test(self) -> None:
        text = "Tiles landed within\n  1.2 nm of the\ttarget."
        assert self._check("Tiles landed within 1.2 nm of the target.", text)[0] == "ok"

    def test_paraphrase_is_refused(self) -> None:
        text = "Tiles landed within 1.2 nm of the target site."
        assert self._check("Tiles hit 1.2 nm of the target site.", text) == (
            "refuse",
            "quote not in the paper text",
        )

    def test_case_is_significant(self) -> None:
        assert self._check("tiles within 1.2 nm", "Tiles within 1.2 nm")[0] == "refuse"

    def test_empty_card_text_is_refused(self) -> None:
        # The pa459574 shape: a title-only card has no text to quote from.
        assert self._check("Placement of 1.2 nm.", "") == (
            "refuse",
            "quote not in the paper text",
        )
        assert self._check("", "Some text 1.2 nm")[0] == "refuse"

    def test_value_absent_is_refused(self) -> None:
        s = "Tiles landed within 3.4 nm of the target."
        assert self._check(s, s) == ("refuse", "value not stated in the quote")

    def test_unit_absent_is_refused(self) -> None:
        s = "Tiles landed within 1.2 of the target."
        assert self._check(s, s) == ("refuse", "unit not stated with the value")
        s2 = "Tiles landed within 1.2 s of the target."
        assert self._check(s2, s2)[0] == "refuse"

    def test_no_unit_determined_skips_the_unit_rule(self) -> None:
        s = "The count was 1.2 per tile."
        assert self._check(s, s, unit=None)[0] == "ok"

    def test_plus_minus_value_is_refused(self) -> None:
        for sign in ("±", "± ", "+/-", "+-", "∓"):
            s = f"Position was 5.1 {sign}1.2 nm overall."
            assert self._check(s, s) == (
                "refuse",
                "value is an uncertainty (±)",
            ), sign

    def test_uncertainty_cue_holds(self) -> None:
        s = "An uncertainty margin of 1.2 nm was estimated."
        assert self._check(s, s) == ("hold", "uncertainty cue: uncertainty")
        s2 = "The 1.2 nm precision of the stage limits this."
        assert self._check(s2, s2) == ("hold", "uncertainty cue: precision")

    def test_cue_beyond_the_window_does_not_hold(self) -> None:
        s = "Tiles landed within 1.2 nm of the target" + " site" * 20 + ", error free."
        assert self._check(s, s)[0] == "ok"

    def test_angstrom_converts_to_the_axis_unit(self) -> None:
        s = "Tiles landed within 12 Å of the target."
        assert self._check(s, s) == ("ok", "")
        # 12 Å is 1.2 nm, not 12 nm.
        assert self._check(s, s, value=12.0)[0] == "refuse"

    def test_rounding_tolerance(self) -> None:
        s = "Tiles landed within 1.2 nm of the target."
        assert self._check(s, s, value=1.24)[0] == "ok"
        assert self._check(s, s, value=1.3) == (
            "refuse",
            "value not stated in the quote",
        )
        s2 = "Tiles landed within 12 nm of the target."
        assert self._check(s2, s2, value=12.3)[0] == "ok"

    def test_thousands_separator(self) -> None:
        s = "The scaffold spans 1,200 nm end to end."
        assert self._check(s, s, value=1200.0)[0] == "ok"

    def test_axis_unit_from_the_key_suffix(self) -> None:
        assert rt._axis_unit("placement_error_nm", None) == "nm"
        assert rt._axis_unit("generation_time_s", None) == "s"
        assert rt._axis_unit("operating_stability_hours", None) == "hours"
        assert rt._axis_unit("yield_fraction", None) is None
        assert rt._axis_unit("force_pn", "pN") == "pN"


# ── AC3: bridge role ──────────────────────────────────────────────────


class TestBridgeRole:
    def test_gap_bridge_rung_mint_and_dedup(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=6.0)
        pathway = make_pathway(store, root)
        unmet = [g for g in quest_gaps(store, root) if g.kind == "unmet-capability"]
        assert len(unmet) == 1

        # Outcome (b): the rung's deliverable is the shortfall — it promises
        # 1.9 nm (cited), which is what makes it close the gap once done. A
        # rung promising only today's 6 nm would leave the gap (and the dry
        # counter) exactly where it was.
        reply = _rung_reply(
            cap, pathway, value=1.9, evidence=["fi99"], title="Place one tile at 1.9 nm"
        )
        result = rt.roadmap_tick(store, ScriptedClient(reply), root)
        assert result["ok"] and result["role"] == "bridge"
        assert result["tier"] == "frontier"
        rid = result["rung_id"]
        rung = store.get_ref(kind="todo", id=rid)
        assert rung is not None
        assert "llm_tier" not in (rung.meta or {})  # the rotation lock
        tags = _tags(store, rid)
        assert "STATUS:open" in tags and "waiting-for:reto" in tags
        assert rung.meta["rung"]["pathway"] == f"qu{pathway}"
        served = {
            ln.dst_ref_id
            for ln in store.links_for(rid, direction="out", relation="serves")
        }
        assert served == {pathway, cap}
        # The open rung is a promise: the gap is now partial, ledger unchanged.
        assert result["gap_count"][1] == result["gap_count"][0] - 1
        assert result["dry"] is False and result["improved"] is False
        assert [
            g.kind for g in quest_gaps(store, root) if g.kind == "unmet-capability"
        ] == []

        # Second identical bridge tick: the rung in flight already closes
        # the gap, so no bridge role; force one by ruling the rung out and
        # replaying the same title — the near-dup gate refuses the twin.
        _todos(store).tag(id=rid, add=["STATUS:won't-do"])
        assert rt.roadmap_role(store, root).role == "bridge"  # type: ignore[union-attr]
        result2 = rt.roadmap_tick(store, ScriptedClient(reply), root)
        assert result2["ok"] and result2.get("near_dup_of") == rid
        assert _rungs_on(store, cap) == [rid]

    def test_rung_with_uncited_better_number_is_refused(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=6.0)
        pathway = make_pathway(store, root)
        reply = _rung_reply(
            cap, pathway, value=1.0, evidence=[], title="Beat the demand"
        )
        result = rt.roadmap_tick(store, ScriptedClient(reply), root)
        assert result["ok"] and "no number, no rung" in result["note"]
        assert _rungs_on(store, cap) == []

    def test_rung_names_a_foreign_pathway_is_refused(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=6.0)
        make_pathway(store, root, "Pathway: A")
        make_pathway(store, root, "Pathway: B")
        reply = _rung_reply(cap, 999_999, value=6.0, evidence=["fi42"], title="x y z")
        result = rt.roadmap_tick(store, ScriptedClient(reply), root)
        assert result["ok"] and "not one serving this root" in result["note"]
        assert _rungs_on(store, cap) == []

    def test_dead_end_lands_on_the_capability_and_the_ledger(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=6.0)
        make_pathway(store, root)
        reply = {"outcome": "dead-end", "dead_end": {"reason": "no bet reaches 2 nm"}}
        result = rt.roadmap_tick(store, ScriptedClient(reply), root)
        assert result["ok"] and result["dead_end"] == "no bet reaches 2 nm"
        (entry,) = _entries(store, cap, "dead-end")
        assert KEY in entry.text and "demanded 2" in entry.text and "6" in entry.text
        (row,) = ledger.compute_ledger(store, root)
        assert row.state == "dead-end"
        assert (
            rt.roadmap_role(store, root) is None
        )  # dead-ended axes are not re-bridged

    def test_new_pathway_mints_dormant_and_serves_the_root(self, store: Any) -> None:
        root, _cap = make_root(store, demand=2.0, supply=6.0)
        reply = {
            "outcome": "pathway",
            "pathway": {
                "title": "Pathway: protein machinery",
                "statement": "Enzymes place tiles.",
            },
        }
        result = rt.roadmap_tick(store, ScriptedClient(reply), root)
        assert result["ok"] and "pathway_id" in result
        pid = result["pathway_id"]
        assert "STATUS:dormant" in _tags(store, pid)
        assert (store.get_ref(kind="quest", id=pid).meta or {}).get(
            "quest_body"
        ) == "roadmap"
        assert any(
            ln.src_ref_id == pid
            for ln in store.links_for(root, direction="in", relation="serves")
        )

    def test_terminal_rung_consumes_the_benign_axes(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=6.0)
        pathway = make_pathway(store, root)
        h = _quests(store)
        benign = _qid(h.put(text="Capability: benign end product"))
        h.edit(
            id=benign,
            meta={
                "quest_body": "roadmap",
                "rubric_objectives": [{"key": "benign_fragments", "sense": "max"}],
                "demand": {
                    "benign_fragments": {
                        "value": 1.0,
                        "source": "qu1",
                        "reason": "safe",
                    }
                },
                "supply": {"benign_fragments": {"value": 1.0, "evidence": ["fi3"]}},
            },
        )
        store.add_link(src_ref_id=benign, dst_ref_id=root, relation="serves")
        reply = _rung_reply(
            cap, pathway, value=6.0, evidence=["fi42"], title="Deliver the part"
        )
        result = rt.roadmap_tick(store, ScriptedClient(reply), root)
        assert result["ok"] and result["terminal"] is True
        assert result["benign_added"] == ["benign_fragments"]
        rung = store.get_ref(kind="todo", id=result["rung_id"])
        assert rung.meta["rung"]["consumes"] == [
            {"capability": f"qu{benign}", "key": "benign_fragments", "value": 1.0}
        ]
        assert rt.rung_is_terminal(store, result["rung_id"], root) is True

    def test_intermediate_rung_is_not_terminal(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=6.0)
        pathway = make_pathway(store, root)
        # A rung already consuming this axis makes the new producer intermediate.
        consumer = make_rung(store, cap, value=9.0, title="downstream: use the tile")
        _todos(store).tag(
            id=consumer,
            meta={
                "rung": {
                    "pathway": f"qu{pathway}",
                    "consumes": [{"capability": f"qu{cap}", "key": KEY, "value": 6.0}],
                    "produces": [
                        {
                            "capability": f"qu{cap}",
                            "key": KEY,
                            "value": 9.0,
                            "evidence": ["fi7"],
                        }
                    ],
                }
            },
        )
        reply = _rung_reply(
            cap, pathway, value=6.0, evidence=["fi42"], title="Place one tile"
        )
        result = rt.roadmap_tick(store, ScriptedClient(reply), root)
        assert result["ok"] and "rung_id" in result
        assert result["terminal"] is False
        assert rt.rung_is_terminal(store, result["rung_id"], root) is False


class TestRungReadingIsTheLedgers:
    """``roadmap_tick`` reads rungs (and their STATUS) through
    ``roadmap_ledger.rungs_for`` — one reader, so the two cannot drift."""

    def test_bridge_prompt_status_comes_from_ledger_rungs_for(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, cap = make_root(store, demand=2.0, supply=6.0)
        make_rung(store, cap, value=9.0, status="done", title="rung: place one tile")
        choice = rt.roadmap_role(store, root)
        assert choice is not None and choice.role == "bridge"
        root_ref = store.get_ref(kind="quest", id=root)

        assert "[done] rung: place one tile" in rt.build_role_prompt(
            store, root_ref, choice
        )

        real = ledger.rungs_for

        def patched(s: Any, ids: list[int]) -> Any:
            return {
                cid: [ledger.Rung(ref=r.ref, status="sentinel-status") for r in rungs]
                for cid, rungs in real(s, ids).items()
            }

        monkeypatch.setattr(ledger, "rungs_for", patched)
        prompt = rt.build_role_prompt(store, root_ref, choice)
        assert "[sentinel-status] rung: place one tile" in prompt

    def test_rungs_under_dedups_across_capabilities(self, store: Any) -> None:
        root, cap_a = make_root(store, demand=2.0, supply=6.0)
        cap_b = _qid(_quests(store).put(text="Capability: cycle time"))
        store.add_link(src_ref_id=cap_b, dst_ref_id=root, relation="serves")
        rid = make_rung(store, cap_a, value=9.0, status="done")
        store.add_link(src_ref_id=rid, dst_ref_id=cap_b, relation="serves")
        caps = [store.get_ref(kind="quest", id=i) for i in (cap_a, cap_b)]
        got = rt._rungs_under(store, caps)
        assert [(r.ref.id, r.status) for r in got] == [(rid, "done")]


# ── AC5: the code-stamped deed ────────────────────────────────────────


class TestDeed:
    def test_done_rung_beating_supply_stamps_one_milestone_each(
        self, store: Any
    ) -> None:
        root, cap = make_root(store, demand=2.0, supply=6.0)
        pathway = make_pathway(store, root)
        # Tick 1 pins the ledger at 6.0 (bridge mints a rung; no deed).
        reply = _rung_reply(
            cap, pathway, value=1.9, evidence=["fi99"], title="Place a tile at 1.9"
        )
        r1 = rt.roadmap_tick(store, ScriptedClient(reply), root)
        assert r1["ok"] and r1["deeds"] == 0
        assert rt.previous_ledger_signature(store, root) == {f"qu{cap}:{KEY}": 6.0}
        rid = r1["rung_id"]
        # A human completes the rung between ticks.
        _todos(store).tag(id=rid, add=["STATUS:done"])

        r2 = rt.roadmap_tick(store, ScriptedClient(), root)
        assert r2["ok"] and r2["role"] is None  # the axis is met — nothing to act on
        assert r2["improved"] is True and r2["dry"] is False
        assert r2["ledger_delta"] == {f"qu{cap}:{KEY}": [6.0, 1.9]}
        assert r2["deeds"] == 2
        (row,) = ledger.compute_ledger(store, root)
        assert (row.best_supply, row.closing_rung, row.state) == (
            1.9,
            f"td{rid}",
            "met",
        )
        cap_deeds = _entries(store, cap, "milestone")
        root_deeds = _entries(store, root, "milestone")
        assert len(cap_deeds) == 1 and len(root_deeds) == 1
        assert (
            cap_deeds[0].meta["by"] == "system" and root_deeds[0].meta["by"] == "system"
        )
        assert "6 → 1.9 nm [fi99]" in cap_deeds[0].text
        assert root_deeds[0].text.startswith(f"qu{cap} ")
        # The chunk now carries the new signature; a third tick stamps nothing.
        assert rt.previous_ledger_signature(store, root) == {f"qu{cap}:{KEY}": 1.9}
        r3 = rt.roadmap_tick(store, ScriptedClient(), root)
        assert r3["deeds"] == 0 and r3["dry"] is True
        assert len(_entries(store, cap, "milestone")) == 1

    def test_first_tick_seeds_the_signature_without_stamping(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=1.5)  # already met
        assert rt.previous_ledger_signature(store, root) is None
        result = rt.roadmap_tick(store, ScriptedClient(), root)
        assert result["deeds"] == 0 and result["improved"] is False
        assert _entries(store, cap, "milestone") == []
        assert rt.previous_ledger_signature(store, root) == {f"qu{cap}:{KEY}": 1.5}

    def test_ledger_improvements_respects_sense(self) -> None:
        row = ledger.LedgerRow(
            capability="qu1",
            capability_title="t",
            key="force_pn",
            sense="max",
            unit=None,
            demanded=10.0,
            best_supply=5.0,
            best_supply_evidence=("fi1",),
            closing_rung=None,
            closing_rung_status=None,
            state="unmet",
        )
        assert rt.ledger_improvements({"qu1:force_pn": 3.0}, [row]) == {
            "qu1:force_pn": (3.0, 5.0)
        }
        assert rt.ledger_improvements({"qu1:force_pn": 7.0}, [row]) == {}
        assert rt.ledger_improvements({}, [row]) == {"qu1:force_pn": (None, 5.0)}
        assert rt.ledger_improvements(None, [row]) == {}


# ── AC6 + AC7: the views ──────────────────────────────────────────────


class TestViews:
    def test_tree_shows_the_ledger_and_dossier_has_one_pinned_chunk(
        self, store: Any
    ) -> None:
        root, cap = make_root(store, demand=2.0, supply=6.0)
        body = _quests(store).get(id=root, view="tree").body
        assert "── capability ledger ──" in body
        assert (
            f"| qu{cap} Capability: positional accuracy | {KEY} ↓ | 2 nm | 6 nm | fi42 | — | unmet |"
            in body
        )
        # N ticks → exactly one regenerated chunk, never appended.
        for _ in range(3):
            rt.roadmap_tick(store, ScriptedClient({"outcome": "nonsense"}), root)
        chunks = _pinned_ledger_chunks(store, root)
        assert len(chunks) == 1
        assert "| unmet |" in chunks[0].text and rt._LEDGER_SEED not in chunks[0].text

    def test_non_roadmap_tree_has_no_ledger_block(self, store: Any) -> None:
        qid = _qid(_quests(store).put(text="a plain quest"))
        assert "capability ledger" not in _quests(store).get(id=qid, view="tree").body

    def test_frontier_says_the_body_has_none(self, store: Any) -> None:
        root, _cap = make_root(store)
        body = _quests(store).get(id=root, view="frontier").body
        assert "the body has no frontier" in body
        assert "objective:" not in body

    def test_materials_frontier_is_unchanged(self, store: Any) -> None:
        qid = _qid(_quests(store).put(text="a plain quest"))
        body = _quests(store).get(id=qid, view="frontier").body
        assert "no frontier" not in body


# ── the coordinator-facing shape ──────────────────────────────────────


class TestTickShape:
    def test_not_a_roadmap_root_is_an_error(self, store: Any) -> None:
        qid = _qid(_quests(store).put(text="a plain quest"))
        result = rt.roadmap_tick(store, ScriptedClient(), qid)
        assert result == {
            "ok": False,
            "error": "not_a_roadmap_root",
            "role": None,
            "gap": None,
        }

    def test_role_exception_is_a_failed_tick_not_a_raise(self, store: Any) -> None:
        root, _cap = make_root(store, demand=None, supply=None)

        class Boom:
            def complete(self, messages: Any) -> Any:
                raise RuntimeError("model down")

        result = rt.roadmap_tick(store, Boom(), root)
        assert result["ok"] is False and "model down" in result["error"]
        assert result["role"] == "demand"

    def test_dry_run_report_names_role_gap_and_prompt(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=6.0)
        report = rt.render_role_report(rt.roadmap_tick(store, None, root, dry_run=True))
        assert report.startswith("role: bridge\ntier: frontier\ngap: unmet-capability:")
        assert f"[qu{cap}]" in report and "── prompt ──" in report
        assert "best cited supply: 6 nm [fi42]" in report
