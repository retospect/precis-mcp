"""retire-dossier slice 2: the direction tree on todos.

Ports the dossier-hygiene assertions onto the todo-backed direction ops
(:mod:`precis.quest.directions`) — the Rh/Ru element veto, quoted-text
addressing (≥ 40-char unique prefix), the never-regress near-dup merge,
and the ancestor-shadow do-not-repropose render — plus the todo-side
storage shape (root ``part-of`` the quest, ``parent_id`` nesting, todo
``STATUS`` tag) and the model-side status map (``blocked``/``paused``
stay on the open list).

Runs against real PG (the ``store`` fixture).
"""

from __future__ import annotations

import re
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest.directions import (
    _element_signature,
    _is_near_dup,
    add_direction,
    direction_ref_ids,
    do_not_repropose,
    mark_direction,
    nest_direction,
    open_list,
    open_umbrella,
    quest_evidence,
    read_direction_forest,
)
from precis.store import Tag


def _mk_quest(store: Any, text: str) -> int:
    h = QuestHandler(hub=Hub(store=store))
    resp = h.put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _forest_texts(store: Any, qid: int) -> list[str]:
    return [n.text for n, _p in _flat(store, qid)]


def _flat(store: Any, qid: int) -> list[Any]:
    from precis.quest.directions import _flatten_with_parent

    return _flatten_with_parent(read_direction_forest(store, qid))


def _by_text(store: Any, qid: int, text: str) -> Any:
    for n, _p in _flat(store, qid):
        if n.text == text:
            return n
    return None


def _status_of(store: Any, rid: int) -> str:
    """The todo's raw ``STATUS`` tag value ('open' when untagged)."""
    for t in store.tags_for(rid):
        if t.namespace == "closed" and t.prefix == "STATUS":
            return t.value
    return "open"


# ── storage shape ───────────────────────────────────────────────────


class TestDirectionStorage:
    def test_root_is_todo_part_of_quest(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        assert add_direction(store, qid, "dope with a transition metal") is True
        roots = read_direction_forest(store, qid)
        assert [(n.text, n.status) for n in roots] == [
            ("dope with a transition metal", "open")
        ]
        node = roots[0]
        assert node.ref_id is not None
        ref = store.fetch_refs_by_ids({node.ref_id})[node.ref_id]
        assert ref.kind == "todo" and ref.parent_id is None
        assert _status_of(store, node.ref_id) == "open"
        # the part-of edge to the quest is what makes it a direction
        links = store.links_for(qid, direction="in", relation="part-of")
        assert [ln.src_ref_id for ln in links] == [node.ref_id]

    def test_child_nests_by_parent_id(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        add_direction(store, qid, "dope with a transition metal")
        assert (
            add_direction(
                store, qid, "Cu single-atom", parent="dope with a transition metal"
            )
            is True
        )
        roots = read_direction_forest(store, qid)
        assert len(roots) == 1
        child = roots[0].children[0]
        assert child.text == "Cu single-atom"
        ref = store.fetch_refs_by_ids({child.ref_id})[child.ref_id]
        assert ref.parent_id == roots[0].ref_id
        # a nested direction needs no part-of edge of its own
        assert store.links_for(child.ref_id, direction="out", relation="part-of") == []

    def test_blank_text_is_a_noop(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        assert add_direction(store, qid, "   ") is False
        assert read_direction_forest(store, qid) == []

    def test_unknown_status_clamps_to_open(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        assert add_direction(store, qid, "a direction", status="bogus") is True
        node = read_direction_forest(store, qid)[0]
        assert node.ref_id is not None
        assert node.status == "open"
        assert _status_of(store, node.ref_id) == "open"


# ── add / mark / addressing ─────────────────────────────────────────


class TestAddMark:
    _LONG = (
        "Screen late transition metal single-atom dopants on nitrogen-doped "
        "graphene for the oxygen reduction reaction, ranking each candidate by "
        "the computed limiting potential, the adsorption free energy of the "
        "OOH intermediate, and the thermodynamic stability against metal "
        "clustering under operating conditions, then validate the top three "
        "against explicit solvent and finite-field corrections before "
        "proposing any synthesis route to the experimental collaborators."
    )

    def test_mark_by_140_char_prefix_with_ellipsis_applies(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        assert add_direction(store, qid, self._LONG) is True
        assert mark_direction(store, qid, self._LONG[:140] + "…", "tried") is True
        node = _by_text(store, qid, self._LONG)
        assert node.status == "tried"
        assert _status_of(store, node.ref_id) == "done"

    def test_mark_by_prefix_shorter_than_40_chars_does_not_match(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        add_direction(store, qid, self._LONG)
        assert mark_direction(store, qid, self._LONG[:39], "tried") is False
        assert mark_direction(store, qid, self._LONG[:39] + "…", "tried") is False
        assert _by_text(store, qid, self._LONG).status == "open"

    def test_mark_by_prefix_shared_by_two_nodes_is_ambiguous_noop(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        shared = "explore the shared preamble about zeolite framework topology"
        a = shared + " alpha bravo charlie delta echo"
        b = shared + " foxtrot golf hotel india juliet"
        add_direction(store, qid, a)
        add_direction(store, qid, b)
        assert mark_direction(store, qid, shared, "tried") is False
        assert _by_text(store, qid, a).status == "open"
        assert _by_text(store, qid, b).status == "open"

    def test_exact_match_wins_when_one_node_is_a_prefix_of_another(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        short = "explore the shared preamble about zeolite framework topology"
        longer = short + " alpha bravo charlie delta echo foxtrot golf hotel india"
        add_direction(store, qid, longer)
        add_direction(store, qid, short)
        assert mark_direction(store, qid, short, "tried") is True
        assert _by_text(store, qid, short).status == "tried"
        assert _by_text(store, qid, longer).status == "open"

    def test_mark_by_td_handle_applies(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        add_direction(store, qid, "a direction")
        node = read_direction_forest(store, qid)[0]
        assert node.ref_id is not None
        assert mark_direction(store, qid, f"td{node.ref_id}", "ruled-out") is True
        assert _status_of(store, node.ref_id) == "won't-do"

    def test_mark_by_td_handle_outside_the_forest_is_a_noop(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        other = store.insert_ref(kind="todo", slug=None, title="not a direction")
        add_direction(store, qid, "a direction")
        assert mark_direction(store, qid, f"td{int(other.id)}", "tried") is False

    def test_mark_attempt_unknown_status_is_a_noop(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        add_direction(store, qid, "a direction")
        assert mark_direction(store, qid, "a direction", "bogus") is False
        assert _by_text(store, qid, "a direction").status == "open"

    def test_mark_parent_disambiguates_ambiguous_node(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        add_direction(store, qid, "explore route alpha")
        add_direction(store, qid, "explore route beta")
        add_direction(store, qid, "c", parent="explore route alpha")
        add_direction(store, qid, "c", parent="explore route beta")
        assert mark_direction(store, qid, "c", "ruled-out") is False
        assert (
            mark_direction(store, qid, "c", "ruled-out", parent="explore route alpha")
            is True
        )
        roots = read_direction_forest(store, qid)
        alpha = next(r for r in roots if r.text == "explore route alpha")
        beta = next(r for r in roots if r.text == "explore route beta")
        assert alpha.children[0].status == "ruled-out"
        assert beta.children[0].status == "open"

    def test_unresolvable_parent_lands_as_a_root_with_unresolved_meta(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        add_direction(store, qid, "placed root")
        assert add_direction(store, qid, "a variant", parent="nothing named this")
        nodes = {n.text: n for n in read_direction_forest(store, qid)}
        assert nodes["a variant"].parent_unresolved == "nothing named this"
        ref = store.fetch_refs_by_ids({nodes["a variant"].ref_id})[
            nodes["a variant"].ref_id
        ]
        assert (ref.meta or {}).get("parent_unresolved") == "nothing named this"
        rendered = open_list(store, qid)
        assert '- [open] a variant (unplaced: meant under "nothing named this")' in (
            rendered
        )


# ── the near-dup upsert (dedup-before-insert) ───────────────────────


class TestNearDupUpsert:
    def test_near_dup_same_status_is_a_noop(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        assert add_direction(store, qid, "identify the rate-limiting step") is True
        assert (
            add_direction(store, qid, "identify the rate limiting step in the pathway")
            is False
        )
        assert _forest_texts(store, qid) == ["identify the rate-limiting step"]

    def test_near_dup_advances_status(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        add_direction(store, qid, "identify the rate-limiting step", status="open")
        assert (
            add_direction(
                store,
                qid,
                "identify the rate limiting step in the pathway",
                status="tried",
            )
            is False
        )
        node = _by_text(store, qid, "identify the rate-limiting step")
        assert node.status == "tried"
        assert _status_of(store, node.ref_id) == "done"

    def test_near_dup_never_regresses_status(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        add_direction(store, qid, "identify the rate-limiting step", status="ruled-out")
        assert (
            add_direction(
                store,
                qid,
                "identify the rate limiting step in the pathway",
                status="open",
            )
            is False
        )
        node = _by_text(store, qid, "identify the rate-limiting step")
        assert node.status == "ruled-out"
        assert _status_of(store, node.ref_id) == "won't-do"

    def test_near_dup_scans_whole_forest_not_just_siblings(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        add_direction(store, qid, "explore route alpha")
        add_direction(store, qid, "explore route beta")
        add_direction(
            store,
            qid,
            "identify the rate-limiting step",
            parent="explore route alpha",
        )
        # a near-dup add under a DIFFERENT parent still merges into the
        # existing node (wherever it lives)
        assert (
            add_direction(
                store,
                qid,
                "identify the rate limiting step in the pathway",
                parent="explore route beta",
                status="tried",
            )
            is False
        )
        roots = read_direction_forest(store, qid)
        alpha = next(r for r in roots if r.text == "explore route alpha")
        beta = next(r for r in roots if r.text == "explore route beta")
        assert [c.status for c in alpha.children] == ["tried"]
        assert beta.children == []


class TestElementGuard:
    """Different chemistry never merges, however similar the prose — the
    element-signature veto (:func:`_elements_conflict`), ported from the
    dossier-hygiene tests onto the direction ops."""

    def test_element_signature_extraction(self) -> None:
        sig = _element_signature("Rh-sub on Pd(111) weakens N–O for NO→NH3")
        assert sig == {"Rh", "Pd"}  # one-letter N/O and NO/NH3 never count
        # ambiguous English-word symbols are excluded even capitalised
        assert _element_signature("In situ, As shown, No candidate At all") == set()

    def test_different_elements_never_near_dup(self) -> None:
        rh = "Rh substitutional SAA on Pd(111)"
        assert _is_near_dup("Ru substitutional SAA on Pd(111)", [rh]) is False
        assert (
            _is_near_dup(
                "Surface Ag substitution coverage series (1/2/3-atom SAA)",
                ["Surface Au substitution coverage series (1/2/3-atom SAA)"],
            )
            is False
        )

    def test_same_element_rephrase_still_merges(self) -> None:
        assert (
            _is_near_dup(
                "Rh substitutional single-atom alloy on Pd(111) surface",
                ["Rh substitutional alloy on Pd(111) surface"],
            )
            is True
        )

    def test_upsert_keeps_element_branches_distinct(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        assert add_direction(store, qid, "Rh substitutional SAA on Pd(111)") is True
        # a different element mints a NEW todo instead of upserting into Rh
        assert add_direction(store, qid, "Ru substitutional SAA on Pd(111)") is True
        assert sorted(_forest_texts(store, qid)) == [
            "Rh substitutional SAA on Pd(111)",
            "Ru substitutional SAA on Pd(111)",
        ]


# ── the renders ─────────────────────────────────────────────────────


class TestRenders:
    def test_do_not_repropose_inherits_ruled_out_over_open_descendants(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        add_direction(store, qid, "explore doping direction c", status="ruled-out")
        add_direction(
            store,
            qid,
            "c with x",
            parent="explore doping direction c",
            status="open",
        )
        # the child's own stored STATUS is untouched (render-level shadow)
        child = _by_text(store, qid, "c with x")
        assert _status_of(store, child.ref_id) == "open"

        text = do_not_repropose(store, qid)
        assert "- [ruled-out] explore doping direction c" in text
        assert "[ruled-out] c with x" in text

    def test_do_not_repropose_collapses_a_fully_dead_subtree(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        add_direction(store, qid, "dead direction d", status="ruled-out")
        add_direction(
            store,
            qid,
            "swap the doping cation",
            parent="dead direction d",
            status="tried",
        )
        add_direction(
            store,
            qid,
            "swap the doping anion",
            parent="dead direction d",
            status="ruled-out",
        )
        text = do_not_repropose(store, qid)
        assert "dead direction d" in text
        assert "(3 variants, all ruled out)" in text
        assert "swap the doping cation" not in text
        assert "swap the doping anion" not in text

    def test_do_not_repropose_excludes_open_directions(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        add_direction(store, qid, "an untried lead", status="open")
        assert do_not_repropose(store, qid) == "(nothing pinned yet)"

    def test_open_list_never_truncates_and_stays_addressable(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        long_text = (
            "identify whether the rate-limiting step shifts when the support "
            "is switched from alumina to ceria under humid conditions at 300C, "
            "and whether that shift also changes the dominant side product"
        )
        add_direction(store, qid, long_text)
        rendered = open_list(store, qid)
        assert long_text in rendered
        assert mark_direction(store, qid, long_text, "active") is True
        assert _status_of(store, _by_text(store, qid, long_text).ref_id) == "doing"


class TestStatusMap:
    """Pins the model-side map: the todo STATUS vocabulary renders as the
    ledger's open/active/tried/ruled-out (spec §Status mapping)."""

    def _add(self, store: Any, qid: int, text: str, todo_status: str) -> int:
        assert add_direction(store, qid, text) is True
        node = _by_text(store, qid, text)
        store.add_tag(
            node.ref_id, Tag.closed("STATUS", todo_status), replace_prefix=True
        )
        return node.ref_id

    def test_blocked_and_paused_stay_on_the_open_list(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        self._add(store, qid, "a blocked direction", "blocked")
        self._add(store, qid, "a paused direction", "paused")
        rendered = open_list(store, qid)
        # both read as open to the model — the direction stays visible and
        # does not get re-added as a fresh proposal
        assert "- [open] a blocked direction" in rendered
        assert "- [open] a paused direction" in rendered
        # and out of the do-not-repropose list
        assert do_not_repropose(store, qid) == "(nothing pinned yet)"

    def test_doing_renders_active(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        self._add(store, qid, "an in-flight direction", "doing")
        assert "- [active] an in-flight direction" in open_list(store, qid)

    def test_done_and_auto_timeout_render_tried(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        self._add(store, qid, "a finished direction", "done")
        self._add(store, qid, "a timed-out direction", "auto-timeout")
        text = do_not_repropose(store, qid)
        assert "- [tried] a finished direction" in text
        assert "- [tried] a timed-out direction" in text

    def test_wont_do_renders_ruled_out(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        self._add(store, qid, "a dead direction", "won't-do")
        assert "- [ruled-out] a dead direction" in do_not_repropose(store, qid)


# ── nest / umbrella strand ──────────────────────────────────────────


class TestNest:
    def test_nest_moves_a_direction_under_a_strand(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        add_direction(store, qid, "strand s")
        add_direction(store, qid, "stray direction")
        assert nest_direction(store, qid, "stray direction", under="strand s") is True
        roots = read_direction_forest(store, qid)
        assert len(roots) == 1
        assert roots[0].text == "strand s"
        assert roots[0].children[0].text == "stray direction"
        child_ref = store.fetch_refs_by_ids({roots[0].children[0].ref_id})[
            roots[0].children[0].ref_id
        ]
        assert child_ref.parent_id == roots[0].ref_id

    def test_nested_direction_with_part_of_edge_appears_once(self, store: Any) -> None:
        # nest keeps the moved direction's root part-of edge; the forest
        # must not double-read it as seed + child (the SQL dedupes on
        # ref_id, the real parent row winning).
        qid = _mk_quest(store, "A striving")
        add_direction(store, qid, "strand s")
        add_direction(store, qid, "stray direction")
        assert nest_direction(store, qid, "stray direction", under="strand s") is True
        forest = read_direction_forest(store, qid)
        flat = [n.text for n, _p in _flat(store, qid)]
        assert flat.count("stray direction") == 1
        assert [r.text for r in forest] == ["strand s"]
        assert [c.text for c in forest[0].children] == ["stray direction"]
        # and the id set agrees — exactly once there too
        moved = _by_text(store, qid, "stray direction")
        assert list(direction_ref_ids(store, qid)).count(moved.ref_id) <= 1
        assert moved.ref_id in direction_ref_ids(store, qid)

    def test_nest_unresolvable_operand_is_a_noop(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        add_direction(store, qid, "strand s")
        add_direction(store, qid, "stray direction")
        assert nest_direction(store, qid, "stray direction", under="nothing") is False
        assert len(read_direction_forest(store, qid)) == 2

    def test_open_umbrella_groups_three_or_more_members(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        for t in ("screen dopant alpha", "screen dopant beta", "screen dopant gamma"):
            add_direction(store, qid, t)
        uid = open_umbrella(
            store,
            qid,
            "the dopant-screen strand",
            ["screen dopant alpha", "screen dopant beta", "screen dopant gamma"],
        )
        assert uid is not None
        roots = read_direction_forest(store, qid)
        assert [r.text for r in roots] == ["the dopant-screen strand"]
        assert [c.text for c in roots[0].children] == [
            "screen dopant alpha",
            "screen dopant beta",
            "screen dopant gamma",
        ]
        # the umbrella is itself a direction: part-of the quest, open
        umbrella = store.fetch_refs_by_ids({uid})[uid]
        assert umbrella.kind == "todo"
        links = store.links_for(qid, direction="in", relation="part-of")
        assert uid in [ln.src_ref_id for ln in links]

    def test_open_umbrella_refuses_fewer_than_three_members(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        add_direction(store, qid, "screen dopant alpha")
        add_direction(store, qid, "screen dopant beta")
        assert (
            open_umbrella(
                store,
                qid,
                "a strand",
                ["screen dopant alpha", "screen dopant beta"],
            )
            is None
        )
        # nothing minted, nothing moved
        assert [r.text for r in read_direction_forest(store, qid)] == [
            "screen dopant alpha",
            "screen dopant beta",
        ]


# ── quest_evidence ──────────────────────────────────────────────────


class TestQuestEvidence:
    def _mk_finding(self, store: Any, text: str) -> int:
        ref = store.insert_ref(kind="finding", slug=None, title=text)
        return int(ref.id)

    def test_evidence_on_quest_and_directions_counts(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        add_direction(store, qid, "strand s")
        strand = read_direction_forest(store, qid)[0]
        on_quest = self._mk_finding(store, "serves the quest directly")
        on_dir = self._mk_finding(store, "serves the direction")
        unrelated = self._mk_finding(store, "unrelated finding")
        store.add_link(src_ref_id=on_quest, dst_ref_id=qid, relation="serves")
        store.add_link(src_ref_id=on_dir, dst_ref_id=strand.ref_id, relation="serves")
        ids = {r.id for r in quest_evidence(store, qid)}
        assert ids == {on_quest, on_dir}
        assert unrelated not in ids

    def test_evidence_excludes_retired_and_non_serves(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        retired = self._mk_finding(store, "a retired server")
        store.add_link(src_ref_id=retired, dst_ref_id=qid, relation="serves")
        related = self._mk_finding(store, "a related-to server")
        store.add_link(src_ref_id=related, dst_ref_id=qid, relation="related-to")
        store.retire_ref(retired)
        ids = {r.id for r in quest_evidence(store, qid)}
        assert ids == set()


# ── direction_ref_ids ───────────────────────────────────────────────


class TestDirectionRefIds:
    def test_returns_roots_and_parent_id_descendants(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        add_direction(store, qid, "root r")
        add_direction(store, qid, "child c", parent="root r")
        add_direction(store, qid, "other root")
        forest = read_direction_forest(store, qid)
        expected = {n.ref_id for n, _p in _flat(store, qid)}
        assert direction_ref_ids(store, qid) == expected
        # a todo outside the direction tree is not a member
        stray = store.insert_ref(kind="todo", slug=None, title="stray")
        assert int(stray.id) not in direction_ref_ids(store, qid)
