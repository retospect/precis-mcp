"""Tests for the ``roadmap`` tick body — :mod:`precis.quest.roadmap_tick`
(docs/backlog/bootstrap-roadmap-quest.md, acceptance criteria 1–3, 5–7).

Runs against real PG (the ``store`` fixture) through the same handlers the
tick writes through, with a scripted ``.complete()`` client standing in for
the model. AC4 (the write guards) lives in the handler tests; AC8 (the
materials default is untouched) is ``test_quest_tick.py``'s own pair.
"""

from __future__ import annotations

import json
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

    def test_supply_without_papers_writes_nothing(self, store: Any) -> None:
        root, cap = make_root(store, demand=2.0, supply=None)
        client = ScriptedClient({"searches": ["nothing held"]})
        result = rt.roadmap_tick(store, client, root, search_fn=lambda s, q, ex: [])
        assert result["ok"] and result["role"] == "supply"
        assert "supply_written" not in result
        assert "supply" not in (store.get_ref(kind="quest", id=cap).meta or {})
        assert result["dry"] is True


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
