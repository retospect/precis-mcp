"""``asks_triage`` — the LLM triage tick over the asks ("needs you")
list, gr652333.

The model call is stubbed: tests pass ``llm`` a callable returning an
:class:`~precis.utils.llm.router.LlmResult` keyed on the row handle in
the prompt; no real LLM touches the suite. Covered: kept/resolved/
routed/rearmed outcomes with the tag + status + ``meta.asks_triage``
stamp each writes, the kept outcome's ``meta.ask`` rewrite through the
guards lint (and its ``ask-user:<question>`` tag swap), the
proof/unknown-route downgrades, malformed replies leaving the row
unstamped, the post-call lock/revalidation (an answer landed mid-call
is never overwritten), chore batching, the re-arm's prior-attempt
audit (verified → released; refused → coordinator route), and the
service_config dark gate.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.todo import asks_view
from precis.utils.llm.router import LlmResult, Tier
from precis.workers import asks_triage

pytestmark = pytest.mark.db


def _light(store: Store, prio: int = 1, host: str = "*") -> None:
    """Light (or dim, ``prio=0``) ``asks_triage`` for ``host`` — the
    ``*`` wildcard lights every host; an exact-host row beats it
    (:func:`asks_triage.asks_triage_enabled`)."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config (host, service, prio) "
            "VALUES (%s, 'asks_triage', %s) "
            "ON CONFLICT (host, service) DO UPDATE SET prio = EXCLUDED.prio",
            (host, prio),
        )


def _todo(store: Store, title: str, *, meta: dict | None = None) -> int:
    return int(store.insert_ref(kind="todo", slug=None, title=title, meta=meta).id)


def _job(store: Store, parent_id: int, *, meta: dict | None = None) -> int:
    """A ``plan_tick`` job child — the unit the re-arm audit reads."""
    return int(
        store.insert_ref(
            kind="job",
            slug=None,
            title="plan_tick",
            meta={"job_type": "plan_tick", **(meta or {})},
            parent_id=parent_id,
        ).id
    )


def _chunk(store: Store, ref_id: int, ord_: int, kind: str, text: str) -> None:
    from precis.store.types import ChunkInsert

    store.chunks.insert_chunks(
        ref_id, [ChunkInsert(ord=ord_, text=text, meta={"chunk_kind": kind})]
    )


#: Minimal stream-json transcripts for the prior-attempt audit:
#: init + a terminal result is a complete read-only stream; an
#: assistant ``tool_use`` for a non-read tool fails the audit.
_SAFE_TRANSCRIPT = "\n".join(
    [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "reading"}]},
            }
        ),
        json.dumps({"type": "result", "is_error": False}),
    ]
)
_WRITE_TRANSCRIPT = "\n".join(
    [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Bash",
                            "input": {"command": "echo hi"},
                        }
                    ]
                },
            }
        ),
        json.dumps({"type": "result", "is_error": False}),
    ]
)


def _ask_todo(
    store: Store,
    title: str,
    *,
    tag: str = "ask-user",
    meta: dict | None = None,
) -> int:
    """A todo on the asks list — the ``ask-user`` tag is the listing
    tag; ``meta.ask`` carries the question shape the view renders."""
    rid = _todo(
        store,
        title,
        meta={
            "ask": {
                "kind": "ruling",
                "options": ["yes", "no"],
                "blocks": "nothing",
                "context": "waits on the owner",
            },
            **(meta or {}),
        },
    )
    store.add_tag(rid, Tag.open(tag), set_by="agent")
    return rid


def _llm(verdicts: dict[int, Any]) -> Any:
    """A stubbed per-row call: parse ``handle: td<N>`` out of the
    prompt's row block and reply with that row's verdict."""

    def call(prompt: str) -> LlmResult:
        m = re.search(r"handle: td(\d+)", prompt)
        assert m, f"prompt carries no todo handle:\n{prompt}"
        payload = verdicts[int(m.group(1))]
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return LlmResult(
            text=text,
            cost_usd=0.0,
            turns_used=None,
            model="stub",
            tier=Tier.FRONTIER,
            data=payload if isinstance(payload, dict) else None,
        )

    return call


def _tags(store: Store, ref_id: int) -> set[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = 'OPEN'",
            (ref_id,),
        ).fetchall()
    return {str(r[0]) for r in rows}


def _status(store: Store, ref_id: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS'",
            (ref_id,),
        ).fetchone()
    return str(row[0]) if row else "open"


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return dict(row[0]) if row and isinstance(row[0], dict) else {}


def test_dark_without_service_config(store: Store) -> None:
    rid = _ask_todo(store, "Approve the carve-out?")
    result = asks_triage.run_asks_triage_pass(
        store, llm=_llm({rid: {"outcome": "kept", "reason": "x"}})
    )
    assert (result.claimed, result.ok, result.failed) == (0, 0, 0)
    assert "asks-triage:kept" not in _tags(store, rid)


def test_outcomes_applied(store: Store) -> None:
    """The fake model returns one of each outcome; each row gets its
    tags/status/rewrite and the ``asks-triage:<outcome>`` stamp."""
    _light(store)
    kept = _ask_todo(store, "Vague ask the model sharpens")
    resolved = _ask_todo(store, "Already answered ask")
    coord = _ask_todo(store, "Coordinator ticket, not a question")
    chore = _ask_todo(store, "Paper metadata chore")
    llm = _llm(
        {
            kept: {
                "outcome": "kept",
                "reason": "real decision, sharpened",
                "question": "Approve the staged rollout?",
                "ask": {
                    "kind": "approval",
                    "options": ["staged rollout", "full rollout", "hold"],
                    "blocks": "nothing",
                    "context": "the model rewrote it",
                },
            },
            resolved: {
                "outcome": "resolved",
                "reason": "the answer already landed",
                "proof": "gr1",
            },
            coord: {
                "outcome": "routed",
                "route": "coordinator",
                "reason": "a lane decision, not an owner question",
            },
            chore: {
                "outcome": "routed",
                "route": "chore:paper-metadata",
                "reason": "mechanical metadata fix",
            },
        }
    )
    result = asks_triage.run_asks_triage_pass(store, llm=llm)
    assert (result.claimed, result.ok, result.failed) == (4, 4, 0)

    # kept: stamp + rewritten meta.ask through the guards lint + retitle.
    tags = _tags(store, kept)
    assert "asks-triage:kept" in tags
    assert "ask-user" in tags  # kept rows keep their ask marker
    meta = _meta(store, kept)
    assert meta["asks_triage"]["outcome"] == "kept"
    assert meta["ask"]["kind"] == "approval"
    assert meta["ask"]["options"] == ["staged rollout", "full rollout", "hold"]
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT title FROM refs WHERE ref_id = %s", (kept,)
        ).fetchone()
    assert row is not None and "Approve the staged rollout?" in str(row[0])

    # resolved: STATUS:done + stamp, off the asks model.
    assert _status(store, resolved) == "done"
    assert "asks-triage:resolved" in _tags(store, resolved)

    # routed coordinator: ask markers shed for waiting-for:coordinator.
    tags = _tags(store, coord)
    assert "asks-triage:routed" in tags
    assert "waiting-for:coordinator" in tags
    assert "ask-user" not in tags

    # routed chore: chore:<kind> tag + part-of a minted batch todo.
    tags = _tags(store, chore)
    assert "asks-triage:routed" in tags
    assert "chore:paper-metadata" in tags
    assert "ask-user" not in tags
    with store.pool.connection() as conn:
        batch = conn.execute(
            "SELECT l.dst_ref_id, r.meta->>'chore_batch' FROM links l "
            "JOIN refs r ON r.ref_id = l.dst_ref_id "
            "WHERE l.src_ref_id = %s AND l.relation = 'part-of'",
            (chore,),
        ).fetchone()
    assert batch is not None and batch[1] == "paper-metadata"


def test_unparseable_reply_leaves_row_unstamped(store: Store) -> None:
    _light(store)
    rid = _ask_todo(store, "Approve the carve-out?")
    result = asks_triage.run_asks_triage_pass(
        store, llm=_llm({rid: "not json at all, sorry"})
    )
    assert (result.claimed, result.ok) == (1, 0)
    assert not _meta(store, rid).get("asks_triage")
    assert "asks-triage:kept" not in _tags(store, rid)
    # And the row still sits on the asks list, untriaged.
    model = asks_view.asks_model(store)
    row = next(r for r in model.rows if r.ref_id == rid)
    assert row.triage == ""


def test_resolved_without_proof_downgrades_to_kept(store: Store) -> None:
    """A ``resolved`` verdict with no verifiable proof must not close
    the row — it stays with the owner, stamped kept with the note."""
    _light(store)
    rid = _ask_todo(store, "Approve the carve-out?")
    result = asks_triage.run_asks_triage_pass(
        store,
        llm=_llm({rid: {"outcome": "resolved", "reason": "trust me", "proof": ""}}),
    )
    assert (result.claimed, result.ok) == (1, 1)
    assert _status(store, rid) == "open"
    tags = _tags(store, rid)
    assert "asks-triage:kept" in tags
    assert "asks-triage:resolved" not in tags
    assert "proof" in _meta(store, rid)["asks_triage"]["note"]


def test_uncertain_halt_rearmed(store: Store) -> None:
    """An ``ask-user:uncertain-halt-outcome`` row is a halt park, not a
    question: the pass releases it through halt_recovery's release
    helper once the prior attempt VERIFIES (the job's own
    ``retry_safe`` receipt here), stamps ``asks-triage:rearmed``, and
    never calls the model."""
    _light(store)
    rid = _todo(store, "Slice parked on an uncertain halt", meta={"llm_tier": None})
    jid = _job(store, rid)
    receipt = {
        "class": "infra",
        "job_ref": jid,
        "retry_safe": True,
        "tag": "waiting-for:infra",
    }
    store.stamp_ref_meta(jid, {"halt": receipt})
    store.stamp_ref_meta(rid, {"halt": receipt})
    store.add_tag(rid, Tag.open("ask-user:uncertain-halt-outcome"), set_by="system")
    called = []

    def llm(prompt: str) -> LlmResult:
        called.append(prompt)
        raise AssertionError("rearmed rows never reach the model")

    result = asks_triage.run_asks_triage_pass(store, llm=llm)
    assert (result.claimed, result.ok, result.failed) == (1, 1, 0)
    assert not called
    tags = _tags(store, rid)
    assert "asks-triage:rearmed" in tags
    assert "ask-user:uncertain-halt-outcome" not in tags
    assert _meta(store, rid)["halt"]["retry_released"] is True
    # Off the owner's list entirely — the park marker is gone.
    assert all(r.ref_id != rid for r in asks_view.asks_model(store).rows)


def test_uncertain_halt_rearmed_on_transcript_audit(store: Store) -> None:
    """No halt receipt on the job, but its stored ``meta.transcript``
    re-audits read-only — the release is verified and re-arms."""
    _light(store)
    rid = _todo(store, "Slice parked on an uncertain halt", meta={"llm_tier": None})
    jid = _job(store, rid, meta={"transcript": _SAFE_TRANSCRIPT})
    store.stamp_ref_meta(
        rid, {"halt": {"class": "infra", "job_ref": jid, "retry_safe": False}}
    )
    store.add_tag(rid, Tag.open("ask-user:uncertain-halt-outcome"), set_by="system")
    result = asks_triage.run_asks_triage_pass(store)
    assert (result.claimed, result.ok) == (1, 1)
    tags = _tags(store, rid)
    assert "asks-triage:rearmed" in tags
    assert "ask-user:uncertain-halt-outcome" not in tags


def test_uncertain_halt_audit_refusal_routes_to_coordinator(store: Store) -> None:
    """A park whose prior attempt can't verify read-only is never
    blindly re-armed — the post-execution stream audit can't un-write a
    duplicate (review td676676). The release holds, the park marker
    sheds for ``waiting-for:coordinator`` (a lane decision, not an
    owner question), the row stamps ``asks-triage:routed`` with the
    audit attached, and ``retry_released`` is never stamped."""
    _light(store)
    rid = _todo(store, "Slice parked on an uncertain halt", meta={"llm_tier": None})
    jid = _job(store, rid, meta={"transcript": _WRITE_TRANSCRIPT})
    store.stamp_ref_meta(
        rid, {"halt": {"class": "infra", "job_ref": jid, "retry_safe": False}}
    )
    store.add_tag(rid, Tag.open("ask-user:uncertain-halt-outcome"), set_by="system")
    called = []

    def llm(prompt: str) -> LlmResult:
        called.append(prompt)
        raise AssertionError("uncertain-halt rows never reach the model")

    result = asks_triage.run_asks_triage_pass(store, llm=llm)
    assert (result.claimed, result.ok) == (1, 1)
    assert not called
    tags = _tags(store, rid)
    assert "asks-triage:routed" in tags
    assert "waiting-for:coordinator" in tags
    assert "ask-user:uncertain-halt-outcome" not in tags
    assert "retry_released" not in _meta(store, rid).get("halt", {})
    assert "re-arm refused" in _meta(store, rid)["asks_triage"]["note"]
    # On the coordinator queue, off the owner's asks list.
    assert any(r.ref_id == rid for r in asks_view.coordinator_model(store))
    assert all(r.ref_id != rid for r in asks_view.asks_model(store).rows)


def test_chore_rows_fold_into_one_batch(store: Store) -> None:
    """Two rows routed ``chore:paper-metadata`` land on the SAME batch
    todo — one worker todo per kind (spec slice 5)."""
    _light(store)
    a = _ask_todo(store, "Fix title of pa1")
    b = _ask_todo(store, "Fix authors of pa2")
    llm = _llm(
        {
            a: {"outcome": "routed", "route": "chore:paper-metadata", "reason": "r"},
            b: {"outcome": "routed", "route": "chore:paper-metadata", "reason": "r"},
        }
    )
    result = asks_triage.run_asks_triage_pass(store, llm=llm)
    assert (result.claimed, result.ok) == (2, 2)
    with store.pool.connection() as conn:
        batches = conn.execute(
            "SELECT DISTINCT dst_ref_id FROM links WHERE relation = 'part-of' "
            "AND src_ref_id = ANY(%s)",
            ([a, b],),
        ).fetchall()
    assert len(batches) == 1
    batch_id = int(batches[0][0])
    assert _meta(store, batch_id)["chore_batch"] == "paper-metadata"
    assert "chore:paper-metadata" in _tags(store, batch_id)
    # Each routed row lands a comment line on the batch.
    with store.pool.connection() as conn:
        comments = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s "
            "AND chunk_kind = 'todo_comment' ORDER BY ord",
            (batch_id,),
        ).fetchall()
    body = "\n".join(str(c[0]) for c in comments)
    assert f"td{a}" in body and f"td{b}" in body


def test_chore_routed_member_parks_off_dispatch(store: Store) -> None:
    """A member routed ``chore:<kind>`` that kept a dispatch signal
    (``meta.llm_tier``, ``meta.executor`` or an ``executor:*`` tag)
    must NOT stay a dispatch candidate: ``chore:`` is no exclusion —
    the batch todo carries the same tag and must stay runnable — and
    the ``part-of`` link doesn't block dispatch either, so an unparked
    member mints its own job AND sits in the batch's worklist: the
    same chore executed twice (review td676676). The member parks on
    ``waiting-for:chore-batch``; the batch stays a candidate."""
    from precis.workers.dispatch import _candidate_parent_ids

    _light(store)
    tiered = _ask_todo(store, "llm_tier chore", meta={"llm_tier": "sonnet"})
    execd = _ask_todo(store, "executor chore", meta={"executor": "claude_inproc"})
    tagged = _ask_todo(store, "executor-tag chore")
    store.add_tag(tagged, Tag.open("executor:fetch"), set_by="agent")
    llm = _llm(
        {
            rid: {
                "outcome": "routed",
                "route": "chore:paper-metadata",
                "reason": "mechanical",
            }
            for rid in (tiered, execd, tagged)
        }
    )
    result = asks_triage.run_asks_triage_pass(store, llm=llm)
    assert (result.claimed, result.ok) == (3, 3)
    ids = _candidate_parent_ids(store, limit=50)
    batch_ids: set[int] = set()
    for rid in (tiered, execd, tagged):
        tags = _tags(store, rid)
        assert "chore:paper-metadata" in tags
        assert "waiting-for:chore-batch" in tags
        assert rid not in ids
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT dst_ref_id FROM links WHERE src_ref_id = %s "
                "AND relation = 'part-of'",
                (rid,),
            ).fetchone()
        assert row is not None
        batch_ids.add(int(row[0]))
    # One shared batch, and the batch itself IS the dispatch candidate
    # the chore queue drains — ``chore:`` stays runnable by design.
    assert len(batch_ids) == 1
    assert batch_ids.pop() in ids


def test_malformed_verdict_leaves_row_unstamped(store: Store) -> None:
    """A reply that isn't a well-formed verdict — a bare ``{}``, an
    ``outcome`` outside the closed set, a missing ``reason``, or a
    supplied field of the wrong type — is REJECTED unstamped, never
    laundered into a permanent ``kept`` stamp and never silently shorn
    of a rewrite it claimed to carry (review td676676)."""
    _light(store)
    empty = _ask_todo(store, "Ask the model answers with an empty object")
    bogus = _ask_todo(store, "Ask the model answers with a bogus outcome")
    noreason = _ask_todo(store, "Ask the model answers with no reason")
    badask = _ask_todo(store, "Ask the model answers with a list ask")
    badroute = _ask_todo(store, "Ask the model answers with a numeric route")
    result = asks_triage.run_asks_triage_pass(
        store,
        llm=_llm(
            {
                empty: {},
                bogus: {"outcome": "bogus", "reason": "made up"},
                noreason: {"outcome": "kept"},
                badask: {"outcome": "kept", "reason": "real", "ask": []},
                badroute: {"outcome": "routed", "reason": "x", "route": 5},
            }
        ),
    )
    assert (result.claimed, result.ok) == (5, 0)
    for rid in (empty, bogus, noreason, badask, badroute):
        assert "asks-triage:kept" not in _tags(store, rid)
        assert not _meta(store, rid).get("asks_triage")
        row = next(r for r in asks_view.asks_model(store).rows if r.ref_id == rid)
        assert row.triage == ""


def test_kept_question_replaces_ask_user_tag(store: Store) -> None:
    """A kept rewrite's new question must replace the ``ask-user:<q>``
    tag — retitling alone leaves the OLD question on the asks view's
    ``?`` line and the web answer form (review td676676). The rewrite
    goes through the same ``see-chunk-N`` overflow redirect every tag
    path uses."""
    _light(store)
    rid = _ask_todo(store, "Approve the old plan?")
    # Trade the bare marker for a legacy question-bearing tag.
    store.remove_tag(rid, Tag.open("ask-user"))
    store.add_tag(rid, Tag.open("ask-user:ship-the-old-plan"), set_by="agent")
    result = asks_triage.run_asks_triage_pass(
        store,
        llm=_llm(
            {
                rid: {
                    "outcome": "kept",
                    "reason": "real decision, sharpened",
                    "question": "Approve the staged rollout?",
                }
            }
        ),
    )
    assert result.ok == 1
    tags = _tags(store, rid)
    assert "ask-user:ship-the-old-plan" not in tags
    # The asks model resolves the NEW question through the tag (the
    # whitespace question overflowed to a see-chunk-N handle).
    row = next(r for r in asks_view.asks_model(store).rows if r.ref_id == rid)
    assert row.question == "Approve the staged rollout?"
    with store.pool.connection() as conn:
        title = conn.execute(
            "SELECT title FROM refs WHERE ref_id = %s", (rid,)
        ).fetchone()
    assert title is not None and "Approve the staged rollout?" in str(title[0])


def test_answered_during_model_call_is_not_overwritten(store: Store) -> None:
    """The verdict applies under a lock that revalidates the row is
    still an open asks row: an answer landed between the model call and
    the write (the ask marker removed) must not be overwritten by a
    stale verdict (review td676676)."""
    _light(store)
    rid = _ask_todo(store, "Approve the carve-out?")

    def llm(prompt: str) -> LlmResult:
        # The owner answers while the model is thinking.
        store.remove_tag(rid, Tag.open("ask-user"))
        return LlmResult(
            text=json.dumps({"outcome": "resolved", "reason": "x", "proof": "gr1"}),
            cost_usd=0.0,
            turns_used=None,
            model="stub",
            tier=Tier.FRONTIER,
            data=None,
        )

    result = asks_triage.run_asks_triage_pass(store, llm=llm)
    assert (result.claimed, result.ok) == (1, 0)
    assert "asks-triage:resolved" not in _tags(store, rid)
    assert not _meta(store, rid).get("asks_triage")
    assert _status(store, rid) == "open"


def test_prompt_carries_second_hop_context(store: Store) -> None:
    """``build_prompt`` hands the verdict real evidence: a linked
    gripe's own body lead and its latest comment, not bare titles and
    statuses (review td676676)."""
    rid = _ask_todo(store, "Is the fix landed?")
    with store.tx() as conn:
        gripe = int(
            store.insert_ref(
                kind="gripe", slug=None, title="gripe about it", conn=conn
            ).id
        )
        store.add_tag(gripe, Tag.parse_strict("STATUS:open", kind="gripe"), conn=conn)
    store.add_link(src_ref_id=rid, dst_ref_id=gripe, relation="cites")
    _chunk(store, gripe, 0, "gripe_body", "Body: the fix landed in sha abc123.")
    _chunk(store, gripe, 1, "gripe_comment", "resolved: verified on main")
    row = next(r for r in asks_view.asks_model(store).rows if r.ref_id == rid)
    prompt = asks_triage.build_prompt(store, row, ["ask-user"])
    assert "gripe about it" in prompt
    assert "the fix landed in sha abc123" in prompt
    assert "resolved: verified on main" in prompt


def test_prompt_carries_neighbour_second_hop(store: Store) -> None:
    """The second hop: evidence linked FROM the row's parent or its
    blocked slice reaches the verdict — the deciding gripe usually
    hangs off a neighbour, not the ask itself, so bodies/comments alone
    miss it (review td676676)."""
    parent = _todo(store, "the parent epic")
    with store.tx() as conn:
        gripe = int(
            store.insert_ref(
                kind="gripe", slug=None, title="deciding gripe", conn=conn
            ).id
        )
        store.add_tag(gripe, Tag.parse_strict("STATUS:open", kind="gripe"), conn=conn)
    store.add_link(src_ref_id=parent, dst_ref_id=gripe, relation="cites")
    _chunk(store, gripe, 0, "gripe_body", "parent-hop proof: landed in sha def456")

    slice_id = _todo(store, "the blocked slice")
    mem = int(store.insert_ref(kind="memory", slug=None, title="slice memory").id)
    store.add_link(src_ref_id=slice_id, dst_ref_id=mem, relation="cites")
    _chunk(store, mem, 0, "memory_body", "slice-hop note: the ruling was yes")

    rid = int(
        store.insert_ref(
            kind="todo",
            slug=None,
            title="Is it done?",
            parent_id=parent,
            meta={
                "ask": {
                    "kind": "ruling",
                    "options": ["yes", "no"],
                    "blocks": f"td{slice_id}",
                    "context": "waits on the owner",
                }
            },
        ).id
    )
    store.add_tag(rid, Tag.open("ask-user"), set_by="agent")
    row = next(r for r in asks_view.asks_model(store).rows if r.ref_id == rid)
    assert row.blocks == slice_id
    prompt = asks_triage.build_prompt(store, row, ["ask-user"])
    # The parent's own linked evidence — headline plus body lead.
    assert f"parent linked: gr{gripe}" in prompt
    assert "deciding gripe" in prompt
    assert "parent-hop proof: landed in sha def456" in prompt
    # The blocked slice's own linked evidence.
    assert f"slice linked: me{mem}" in prompt
    assert "slice memory" in prompt
    assert "slice-hop note: the ruling was yes" in prompt
