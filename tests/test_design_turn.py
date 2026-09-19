"""Workbench turn engine — design-workbench build, slice 3, acceptance
**S3** (:mod:`precis_web.design_turn`). The model is a stub everywhere;
the router is never reached.

* two valid pure se ops → one new ``design_revisions`` row (ops verbatim,
  ``turn`` = the transcript handle), one ``conv`` block linked
  ``related-to`` the design, no ``todo``/``job`` ref;
* an unknown op / raw coordinates → nothing applied, the validator's
  message names the offender;
* ``bind_structure`` → a proposal, no revision;
* prose with a narrated ``put(...)`` → error, the store untouched;
* structure: a valid atom op → proposal ``valid=True``, no revision;
  :func:`apply_proposal` → one revision via ``edit``, version bumped in
  place, no ``derived-from`` link.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

import precis_se
from precis.design import history
from precis.dispatch import Hub
from precis.handlers.structure import StructureHandler
from precis.store import Store
from precis_se.atomic.apply import all_op_names
from precis_se.handler import SeHandler
from precis_web import design_turn
from precis_web.design_turn import TurnResult, apply_proposal, run_turn

_SE_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"

_PD = json.dumps(
    {
        "cell": {"a": 10.0, "b": 10.0, "c": 10.0, "pbc": [True, True, False]},
        "ops": [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.26, 0.0, 0.0]},
            {"op": "add_bond", "i": "aPd1", "j": "aPd2", "order": 1},
        ],
    }
)

_NANOBUD_OPS = [
    {"op": "add_block", "name": "tube", "envelope": "cyl:r0.0007h0.005"},
    {"op": "add_block", "name": "bud", "envelope": "cyl:r0.0005h0.001"},
    {"op": "add_port", "block": "tube", "name": "side"},
    {"op": "add_port", "block": "bud", "name": "foot"},
]

_LINKER_OPS = [
    {"op": "add_block", "name": "linker", "envelope": "box:w0.001d0.001h0.002"},
    {"op": "add_port", "block": "linker", "name": "a"},
    {"op": "connect", "a": "linker.a", "b": "tube.side"},
]


@pytest.fixture
def structure(store: Store) -> StructureHandler:
    return StructureHandler(hub=Hub(store=store))


@pytest.fixture
def se(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_SE_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return SeHandler(hub=hub)


@pytest.fixture
def nanobud(se: SeHandler) -> str:
    se.put(id="nanobud1", text=json.dumps({"ops": _NANOBUD_OPS}))
    return "nanobud1"


def _ref_id(store: Store, kind: str, slug: str) -> int:
    ref = store.get_ref(kind=kind, id=slug)
    assert ref is not None
    return int(ref.id)


def _count(store: Store, sql: str, *params: Any) -> int:
    with store.pool.connection() as c:
        row = c.execute(sql, params).fetchone()
    assert row is not None
    return int(row[0])


def _stub(reply: str) -> Any:
    seen: list[str] = []

    def call(prompt: str) -> str:
        seen.append(prompt)
        return reply

    cast(Any, call).seen = seen
    return call


def _kind_counts(store: Store) -> dict[str, int]:
    with store.pool.connection() as c:
        rows = c.execute("SELECT kind, count(*) FROM refs GROUP BY kind").fetchall()
    return {str(k): int(n) for k, n in rows}


# ── digest + vocabulary ────────────────────────────────────────────────


def test_se_digest_carries_uids_ports_connects_and_the_roster(
    hub: Hub, store: Store, nanobud: str
) -> None:
    digest = design_turn.build_digest(hub, kind="se", slug=nanobud)
    tube = store.get_ref(kind="se", id=nanobud)
    assert tube is not None
    assert "tube" in digest and "bud" in digest
    assert "ports foot" in digest and "ports side" in digest
    assert "#" in digest  # a uid
    assert "realization: — (envelope only)" in digest
    # The vocabulary lists the real roster, both classes marked.
    for name in all_op_names():
        assert name in digest
    assert "Proposal only" in digest and "Auto-applied" in digest


def test_signature_tables_name_only_real_ops() -> None:
    assert set(design_turn._SE_OP_SIGNATURES) <= all_op_names()
    assert set(design_turn._SE_STORE_AWARE_SIGNATURES) <= all_op_names()
    assert set(design_turn._STRUCTURE_OP_SIGNATURES) <= design_turn.roster("structure")
    # Every structure op is in the digest by name.
    vocab = design_turn.op_vocabulary("structure")
    for name in design_turn.roster("structure"):
        assert name in vocab


def test_destructive_se_ops_are_a_nonempty_subset_of_known_ops() -> None:
    from precis_se.ops import known_ops

    assert design_turn.DESTRUCTIVE_SE_OPS
    assert design_turn.DESTRUCTIVE_SE_OPS.issubset(known_ops())


def test_structure_digest_truncates_past_the_atom_cap(
    hub: Hub, store: Store, structure: StructureHandler, monkeypatch: Any
) -> None:
    structure.put(id="pd_pair", text=_PD)
    monkeypatch.setattr(design_turn, "MAX_DIGEST_ATOMS", 1)
    digest = design_turn.build_digest(hub, kind="structure", slug="pd_pair")
    assert "aPd1 Pd" in digest
    assert "1 more atoms not listed" in digest
    assert "add_atom{" in digest


# ── S3: pure se ops auto-apply as one revision + one transcript block ──


def test_two_pure_se_ops_apply_as_one_revision_with_the_turn_handle(
    hub: Hub, store: Store, nanobud: str
) -> None:
    ref_id = _ref_id(store, "se", nanobud)
    before = history.list_revisions(store, ref_id)
    kinds_before = _kind_counts(store)
    stub = _stub(
        json.dumps({"ops": _LINKER_OPS, "rationale": "a linker joins tube and bud"})
    )

    res = run_turn(
        hub,
        kind="se",
        slug=nanobud,
        message="add a block `linker` between `bud` and `tube`",
        handles=["#1", "#2"],
        model_call=stub,
    )

    assert isinstance(res, TurnResult)
    assert res.applied is True and res.error is None and res.proposal is None
    assert res.ops == _LINKER_OPS
    assert res.rationale == "a linker joins tube and bud"
    # The prompt carried the digest, the clicked handles and the message.
    prompt = stub.seen[0]
    assert "# Clicked handles\n#1, #2" in prompt
    assert "add a block `linker`" in prompt
    assert "Op vocabulary" in prompt

    after = history.list_revisions(store, ref_id)
    assert len(after) == len(before) + 1
    new = after[-1]
    assert new.ops == _LINKER_OPS
    assert res.revision == new.rev
    conv_slug = design_turn.conv_slug(nanobud)
    assert new.turn == f"{conv_slug}~0" == res.turn

    # One conv block, linked related-to the design.
    conv = store.get_ref(kind="conv", id=conv_slug)
    assert conv is not None
    blocks = store.chunks.list_chunks_for_ref(conv.id)
    assert len(blocks) == 1 and blocks[0].ord == 0
    assert (blocks[0].meta or {}).get("msg_id") == res.turn
    assert "add a block `linker`" in blocks[0].text
    assert "a linker joins tube and bud" in blocks[0].text
    assert f"applied as revision {new.rev}" in blocks[0].text
    links = store.links_for(conv.id, direction="out", relation="related-to")
    assert [link.dst_ref_id for link in links] == [ref_id]

    # No todo/job minted; the only new ref is the conv.
    kinds_after = _kind_counts(store)
    assert kinds_after.get("todo", 0) == kinds_before.get("todo", 0) == 0
    assert kinds_after.get("job", 0) == kinds_before.get("job", 0) == 0
    assert kinds_after.get("conv", 0) == 1

    # The tree really changed.
    from precis_se import persist

    tree = persist.load_tree(store, ref_id)
    assert "linker" in tree.blocks
    assert any({c.a_block, c.b_block} == {"linker", "tube"} for c in tree.connects)


def test_second_turn_appends_block_one_and_links_once(
    hub: Hub, store: Store, nanobud: str
) -> None:
    ref_id = _ref_id(store, "se", nanobud)
    first = json.dumps({"ops": _LINKER_OPS, "rationale": "r1"})
    second = json.dumps(
        {
            "ops": [{"op": "set_desc", "block": "linker", "desc": "the joint"}],
            "rationale": "r2",
        }
    )
    run_turn(hub, kind="se", slug=nanobud, message="m1", model_call=_stub(first))
    res = run_turn(hub, kind="se", slug=nanobud, message="m2", model_call=_stub(second))
    conv_slug = design_turn.conv_slug(nanobud)
    assert res.turn == f"{conv_slug}~1"
    revs = history.list_revisions(store, ref_id)
    assert [r.turn for r in revs[-2:]] == [f"{conv_slug}~0", f"{conv_slug}~1"]
    conv = store.get_ref(kind="conv", id=conv_slug)
    assert conv is not None
    assert len(store.chunks.list_chunks_for_ref(conv.id)) == 2
    assert len(store.links_for(conv.id, direction="out", relation="related-to")) == 1


# ── destructive pure se ops propose instead of auto-applying ──────────


def test_destructive_pure_op_makes_the_whole_turn_a_proposal(
    hub: Hub, store: Store, nanobud: str
) -> None:
    ref_id = _ref_id(store, "se", nanobud)
    n = len(history.list_revisions(store, ref_id))
    ops = [
        {"op": "add_block", "name": "temp"},
        {"op": "remove_block", "block": "temp"},
    ]
    reply = json.dumps({"ops": ops, "rationale": "scratch a block"})
    res = run_turn(hub, kind="se", slug=nanobud, message="m", model_call=_stub(reply))

    assert res.applied is False and res.proposal == ops and res.ops == ops
    assert res.valid is True and res.error is None
    assert res.revision is None
    assert len(history.list_revisions(store, ref_id)) == n
    conv = store.get_ref(kind="conv", id=design_turn.conv_slug(nanobud))
    assert conv is not None
    blocks = store.chunks.list_chunks_for_ref(conv.id)
    assert len(blocks) == 1 and "proposal (2 op(s), valid)" in blocks[0].text

    applied = apply_proposal(hub, kind="se", slug=nanobud, ops=ops, turn=res.turn)
    assert applied.applied is True
    revs = history.list_revisions(store, ref_id)
    assert len(revs) == n + 1
    assert revs[-1].ops == ops and revs[-1].turn == res.turn


# ── S3: rejections write nothing ───────────────────────────────────────


def _assert_untouched(store: Store, ref_id: int, revs_before: int) -> None:
    assert len(history.list_revisions(store, ref_id)) == revs_before
    assert _kind_counts(store).get("conv", 0) == 0


def test_unknown_op_rejects_the_turn_naming_the_op(
    hub: Hub, store: Store, nanobud: str
) -> None:
    ref_id = _ref_id(store, "se", nanobud)
    n = len(history.list_revisions(store, ref_id))
    reply = json.dumps(
        {
            "ops": [
                {"op": "add_block", "name": "linker"},
                {"op": "teleport_block", "block": "bud"},
            ],
            "rationale": "x",
        }
    )
    res = run_turn(hub, kind="se", slug=nanobud, message="m", model_call=_stub(reply))
    assert res.applied is False and res.ops == [] and res.proposal is None
    assert res.error is not None and "teleport_block" in res.error
    assert res.revision is None and res.turn is None
    _assert_untouched(store, ref_id, n)


def test_raw_coordinates_reject_the_turn(hub: Hub, store: Store, nanobud: str) -> None:
    ref_id = _ref_id(store, "se", nanobud)
    n = len(history.list_revisions(store, ref_id))
    # A blocklisted key …
    reply = json.dumps(
        {
            "ops": [
                {"op": "add_block", "name": "linker", "xyz": [[0, 0, 0], [1, 1, 1]]}
            ],
            "rationale": "x",
        }
    )
    res = run_turn(hub, kind="se", slug=nanobud, message="m", model_call=_stub(reply))
    assert res.applied is False and res.ops == []
    assert (
        res.error is not None and "raw coordinates" in res.error and "xyz" in res.error
    )
    _assert_untouched(store, ref_id, n)
    # … and a coordinate table under an innocent key.
    reply = json.dumps(
        {
            "ops": [
                {"op": "set_pose", "block": "bud", "path": [[0, 0, 0], [0, 0, 1e-3]]}
            ],
            "rationale": "x",
        }
    )
    res = run_turn(hub, kind="se", slug=nanobud, message="m", model_call=_stub(reply))
    assert res.applied is False and res.error is not None
    assert "raw coordinates" in res.error and "path" in res.error
    _assert_untouched(store, ref_id, n)


def test_prose_with_a_narrated_put_rejects_and_writes_nothing(
    hub: Hub, store: Store, nanobud: str
) -> None:
    ref_id = _ref_id(store, "se", nanobud)
    n = len(history.list_revisions(store, ref_id))
    refs_before = _count(store, "SELECT count(*) FROM refs")
    chunks_before = _count(store, "SELECT count(*) FROM chunks")
    reply = (
        "Sure — I'll record this as a draft first: put(kind='draft', "
        "id='linker-plan', text='add a linker between bud and tube') and "
        "then add the block."
    )
    res = run_turn(hub, kind="se", slug=nanobud, message="m", model_call=_stub(reply))
    assert res.applied is False and res.ops == [] and res.proposal is None
    assert res.error is not None and "JSON" in res.error
    assert _count(store, "SELECT count(*) FROM refs") == refs_before
    assert _count(store, "SELECT count(*) FROM chunks") == chunks_before
    assert store.get_ref(kind="draft", id="linker-plan") is None
    _assert_untouched(store, ref_id, n)


def test_cannot_be_expressed_reply_is_valid_and_writes_nothing(
    hub: Hub, store: Store, nanobud: str
) -> None:
    ref_id = _ref_id(store, "se", nanobud)
    n = len(history.list_revisions(store, ref_id))
    reply = (
        "```json\n"
        + json.dumps(
            {"ops": [], "rationale": "cannot be expressed as ops because no bend op"}
        )
        + "\n```"
    )
    res = run_turn(hub, kind="se", slug=nanobud, message="m", model_call=_stub(reply))
    assert res.applied is False and res.error is None and res.ops == []
    assert res.rationale.startswith("cannot be expressed as ops")
    _assert_untouched(store, ref_id, n)


def test_invalid_pure_ops_fail_the_dry_run_and_write_nothing(
    hub: Hub, store: Store, nanobud: str
) -> None:
    ref_id = _ref_id(store, "se", nanobud)
    n = len(history.list_revisions(store, ref_id))
    reply = json.dumps(
        {"ops": [{"op": "connect", "a": "ghost.a", "b": "tube.side"}], "rationale": "x"}
    )
    res = run_turn(hub, kind="se", slug=nanobud, message="m", model_call=_stub(reply))
    assert res.applied is False and res.proposal is None
    assert res.error is not None and "ghost" in res.error
    _assert_untouched(store, ref_id, n)


# ── S3: store-aware se ops propose ─────────────────────────────────────


def test_bind_structure_is_a_proposal_not_a_revision(
    hub: Hub, store: Store, nanobud: str, structure: StructureHandler
) -> None:
    structure.put(id="pd_pair", text=_PD)
    ref_id = _ref_id(store, "se", nanobud)
    n = len(history.list_revisions(store, ref_id))
    ops = [
        {"op": "add_port", "block": "bud", "name": "anchor"},
        {"op": "bind_structure", "block": "bud", "design": "pd_pair"},
    ]
    reply = json.dumps({"ops": ops, "rationale": "bind the bud"})
    res = run_turn(hub, kind="se", slug=nanobud, message="m", model_call=_stub(reply))
    assert res.applied is False and res.proposal == ops and res.ops == ops
    assert res.valid is True and res.error is None
    assert res.revision is None
    assert len(history.list_revisions(store, ref_id)) == n
    # The transcript recorded the proposal, and the tree is untouched.
    conv = store.get_ref(kind="conv", id=design_turn.conv_slug(nanobud))
    assert conv is not None
    blocks = store.chunks.list_chunks_for_ref(conv.id)
    assert len(blocks) == 1 and "proposal (2 op(s), valid)" in blocks[0].text
    assert res.turn == f"{design_turn.conv_slug(nanobud)}~0"
    from precis_se import persist

    tree = persist.load_tree(store, ref_id)
    assert tree.blocks["bud"].bound is None
    assert "anchor" not in tree.blocks["bud"].ports

    # The human Apply lands it as one revision carrying the turn.
    applied = apply_proposal(hub, kind="se", slug=nanobud, ops=ops, turn=res.turn)
    assert applied.applied is True
    revs = history.list_revisions(store, ref_id)
    assert len(revs) == n + 1
    assert revs[-1].ops == ops and revs[-1].turn == res.turn
    assert persist.load_tree(store, ref_id).blocks["bud"].bound == "pd_pair"


def test_invalid_store_aware_proposal_is_marked_invalid(
    hub: Hub, store: Store, nanobud: str
) -> None:
    ops = [{"op": "bind_structure", "block": "bud", "design": "no-such-design"}]
    reply = json.dumps({"ops": ops, "rationale": "x"})
    res = run_turn(hub, kind="se", slug=nanobud, message="m", model_call=_stub(reply))
    assert res.applied is False and res.proposal == ops and res.valid is False
    assert res.error is not None and "no-such-design" in res.error


# ── S3: structure ops propose, Apply edits in place ────────────────────


def test_structure_atom_op_proposes_then_apply_edits_in_place(
    hub: Hub, store: Store, structure: StructureHandler
) -> None:
    structure.put(id="pd_pair", text=_PD)
    ref_id = _ref_id(store, "structure", "pd_pair")
    n = len(history.list_revisions(store, ref_id))
    version = store.structure_version(ref_id)
    ops = [{"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.5]}]
    stub = _stub(json.dumps({"ops": ops, "rationale": "an O on top"}))

    res = run_turn(
        hub, kind="structure", slug="pd_pair", message="add an O", model_call=stub
    )
    assert "aPd1 Pd frac=" in stub.seen[0]
    assert res.applied is False and res.proposal == ops and res.valid is True
    assert res.error is None and res.revision is None
    assert len(history.list_revisions(store, ref_id)) == n
    assert store.structure_version(ref_id) == version
    assert res.turn == f"{design_turn.conv_slug('pd_pair')}~0"

    applied = apply_proposal(
        hub, kind="structure", slug="pd_pair", ops=ops, turn=res.turn
    )
    assert applied.applied is True and applied.error is None
    # Same ref, version +1 (edit, not derive), the revision carries the turn.
    assert store.structure_version(ref_id) == version + 1
    live = store.get_ref(kind="structure", id="pd_pair")
    assert live is not None and live.id == ref_id
    assert (live.meta or {}).get("version") == version + 1
    revs = history.list_revisions(store, ref_id)
    assert len(revs) == n + 1
    assert revs[-1].ops == ops and revs[-1].turn == res.turn
    assert applied.revision == revs[-1].rev == version + 1
    assert store.links_for(ref_id, relation="derived-from") == []
    assert _kind_counts(store).get("structure", 0) == 1
    assert "aO1" in store.structure_load(ref_id)[0].atoms


def test_structure_invalid_op_is_an_invalid_proposal(
    hub: Hub, store: Store, structure: StructureHandler
) -> None:
    structure.put(id="pd_pair", text=_PD)
    ops = [{"op": "vacancy", "atom": "aXx99"}]
    reply = json.dumps({"ops": ops, "rationale": "x"})
    res = run_turn(
        hub, kind="structure", slug="pd_pair", message="m", model_call=_stub(reply)
    )
    assert res.applied is False and res.proposal == ops and res.valid is False
    assert res.error is not None and "aXx99" in res.error


def test_structure_relax_is_not_in_the_vocabulary(
    hub: Hub, store: Store, structure: StructureHandler
) -> None:
    structure.put(id="pd_pair", text=_PD)
    reply = json.dumps({"ops": [{"op": "relax", "fidelity": "emt"}], "rationale": "x"})
    res = run_turn(
        hub, kind="structure", slug="pd_pair", message="m", model_call=_stub(reply)
    )
    assert res.applied is False and res.proposal is None
    assert res.error is not None and "relax" in res.error


def test_apply_proposal_regates_the_roster(
    hub: Hub, store: Store, structure: StructureHandler
) -> None:
    structure.put(id="pd_pair", text=_PD)
    ref_id = _ref_id(store, "structure", "pd_pair")
    res = apply_proposal(
        hub,
        kind="structure",
        slug="pd_pair",
        ops=[{"op": "import_fragment", "design": "pd_pair"}],
        turn=None,
    )
    assert res.applied is False and res.error is not None
    assert "import_fragment" in res.error
    assert store.structure_version(ref_id) == 1


def test_model_failure_is_an_error_result(hub: Hub, store: Store, nanobud: str) -> None:
    def boom(prompt: str) -> str:
        raise RuntimeError("router down")

    res = run_turn(hub, kind="se", slug=nanobud, message="m", model_call=boom)
    assert res.applied is False and res.error == "model call failed: router down"
    assert _kind_counts(store).get("conv", 0) == 0


# ── S3: a concurrent double-save is a friendly error, not a 500 ────────


def test_concurrent_double_save_is_a_friendly_error_not_a_500(
    hub: Hub, store: Store, nanobud: str, monkeypatch: Any
) -> None:
    from psycopg.errors import UniqueViolation

    def boom(self: Any, **kwargs: Any) -> None:
        raise UniqueViolation("dup")

    monkeypatch.setattr(SeHandler, "edit", boom)
    ref_id = _ref_id(store, "se", nanobud)
    n = len(history.list_revisions(store, ref_id))
    reply = json.dumps({"ops": [{"op": "add_block", "name": "temp"}], "rationale": "x"})

    res = run_turn(hub, kind="se", slug=nanobud, message="m", model_call=_stub(reply))

    assert res.applied is False
    assert res.error == "another save landed first — reload the page and retry"
    _assert_untouched(store, ref_id, n)
