"""``call_errors`` — gr484070's daily failed-call mining pass.

DB-backed where the pass touches the store; the cluster/classify helpers
are exercised on fabricated ``_Row`` lists so the deterministic rules
(fingerprint, threshold, cap, tag) are pinned without depending on which
sibling tests ran on the same shared ``precis_test`` clone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from precis import tool_ledger
from precis.tool_ledger import ToolCallRecord
from precis.workers import call_errors


def _row(
    *,
    verb: str = "put",
    kind: str | None = "memory",
    head: str = "[error:BadInput] unknown key 'bogus'",
    session: str = "s1",
    client: str | None = "test-client/1.0",
    arg_sample: Any = None,
    result_bytes: int | None = None,
    ts: datetime | None = None,
) -> call_errors._Row:
    return call_errors._Row(
        verb=verb,
        kind=kind,
        error_head=head,
        arg_sample=arg_sample,
        result_bytes=result_bytes,
        client=client,
        session=session,
        ts=ts or datetime.now(UTC),
    )


def _fp(verb: str, kind: str | None, head: str) -> str:
    return call_errors._fingerprint(verb, kind, call_errors.normalize_err_head(head))


def _cluster_of(rows: list[call_errors._Row]) -> call_errors.Cluster:
    clusters = call_errors._cluster(rows)
    assert len(clusters) == 1
    return clusters[0]


def _seed_calls(
    store: Any,
    *,
    verb: str,
    kind: str | None,
    head: str,
    sessions: list[str],
    client: str | None = None,
    arg_sample: str | None = None,
    result_bytes: int | None = None,
    error_type: str = "BadInput",
) -> None:
    """One ledger error row per entry in ``sessions``."""
    for i, sess in enumerate(sessions):
        tool_ledger.record_call(
            store,
            ToolCallRecord(
                verb=verb,
                kind=kind,
                input_keys=["kind"],
                outcome="error",
                error_type=error_type,
                session_id=sess,
                seq=i + 1,
                result_bytes=result_bytes,
                error_head=head,
                arg_sample=arg_sample,
                client=client,
            ),
        )


def _cluster_gripes(store: Any, fp: str) -> list[tuple[int, str]]:
    with store.pool.connection() as conn:
        return [
            (int(r[0]), str(r[1]))
            for r in conn.execute(
                "SELECT ref_id, title FROM refs WHERE kind='gripe' "
                "AND retired_at IS NULL AND meta->>'call_cluster' = %s",
                (fp,),
            ).fetchall()
        ]


def _gripe_comments(store: Any, gripe_id: int) -> list[str]:
    with store.pool.connection() as conn:
        return [
            str(r[0])
            for r in conn.execute(
                "SELECT text FROM chunks WHERE ref_id=%s "
                "AND chunk_kind='gripe_comment' ORDER BY ord",
                (gripe_id,),
            ).fetchall()
        ]


def _gripe_body(store: Any, gripe_id: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE ref_id=%s AND chunk_kind='gripe_body' "
            "AND ord=0",
            (gripe_id,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _gripe_tags(store: Any, gripe_id: int) -> list[str]:
    with store.pool.connection() as conn:
        return [
            str(r[0])
            for r in conn.execute(
                "SELECT CASE WHEN t.namespace='STATUS' THEN 'STATUS:'||t.value "
                "ELSE t.value END FROM tags t "
                "JOIN ref_tags rt ON rt.tag_id=t.tag_id WHERE rt.ref_id=%s",
                (gripe_id,),
            ).fetchall()
        ]


def _status(store: Any, gripe_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM tags t JOIN ref_tags rt ON rt.tag_id=t.tag_id "
            "WHERE rt.ref_id=%s AND t.namespace='STATUS'",
            (gripe_id,),
        ).fetchone()
    return str(row[0]) if row else None


# ── pure: clustering / fingerprint / classification ─────────────────────


def test_normalize_err_head_collapses_ids_paths_digits() -> None:
    a = call_errors.normalize_err_head("[error:BadInput] no memory 'aa11' at /x/y")
    b = call_errors.normalize_err_head("[error:BadInput] no memory 'bb22' at /p/q")
    assert a == b == "[error:BadInput] no memory <id> at <path>"


def test_fingerprint_stable_and_distinct() -> None:
    rows = [
        _row(head="[error:BadInput] no memory 'aa'", session="s1"),
        _row(head="[error:BadInput] no memory 'bb'", session="s2"),
    ]
    cl = _cluster_of(rows)
    assert cl.fingerprint == _fp("put", "memory", "[error:BadInput] no memory 'zz'")
    other = _cluster_of([_row(head="[error:BadInput] different")])
    assert other.fingerprint != cl.fingerprint


def test_cluster_counts_sessions_and_clients() -> None:
    cl = _cluster_of(
        [
            _row(session="s1", client="a/1"),
            _row(session="s1", client="a/1"),
            _row(session="s2", client=None),
        ]
    )
    assert cl.n == 3
    assert cl.sessions == {"s1", "s2"}
    assert cl.clients["a/1"] == 2 and cl.clients["unattributed"] == 1
    assert cl.repeated_in_session is True


def test_classify_mcp_param_gap() -> None:
    # 3 sessions × 1 call, multi-line body (next: present), no quoted
    # token any skill could document → schema/param gap.
    head = "[error:BadInput] unknown key 'zz-no-such-key' for put"
    cl = _cluster_of(
        [
            _row(head=head, session=f"s{i}", result_bytes=len(head) + 60)
            for i in range(3)
        ]
    )
    cls, skill, reason = call_errors.classify(cl, {})
    assert cls == "mcp" and skill is None and reason


def test_classify_skill_documented_form() -> None:
    head = "[error:BadInput] unknown key 'body' for put(kind='zzkind')"
    rows = [
        _row(kind="zzkind", head=head, session=f"s{i}", result_bytes=len(head) + 60)
        for i in range(3)
    ]
    cl = _cluster_of(rows)
    texts = {"zz-skill": "file it via `put(kind='zzkind', body='x')`"}
    cls, skill, _ = call_errors.classify(cl, texts)
    assert cls == "skill" and skill == "zz-skill"
    # Same cluster, but no skill documents the shape → falls through.
    cls2, skill2, _ = call_errors.classify(cl, {})
    assert cls2 != "skill" and skill2 is None


def test_classify_hint_single_line_body() -> None:
    head = "[error:BadInput] whatever"
    cl = _cluster_of(
        [_row(head=head, session=f"s{i}", result_bytes=len(head)) for i in range(3)]
    )
    cls, _, reason = call_errors.classify(cl, {})
    assert cls == "hint" and "single line" in reason


def test_classify_hint_in_session_repeat() -> None:
    head = "[error:BadInput] whatever"  # no param-gap token, multi-line body
    cl = _cluster_of(
        [
            _row(head=head, session="s1", result_bytes=len(head) + 60),
            _row(head=head, session="s1", result_bytes=len(head) + 60),
        ]
    )
    cls, _, reason = call_errors.classify(cl, {})
    assert cls == "hint" and "repeated" in reason


def test_classify_needs_triage_when_no_signal() -> None:
    head = "[error:BadInput] something vague"
    cl = _cluster_of([_row(head=head, session="s1", result_bytes=len(head) + 60)])
    assert call_errors.classify(cl, {})[0] == "needs-triage"


# ── DB: filing, bumping, threshold, cap ──────────────────────────────────


def test_pass_files_one_gripe_over_threshold(store: Any) -> None:
    tag = uuid4().hex[:8]
    head = f"[error:BadInput] unknown key 'zz{tag}' for put"
    _seed_calls(
        store,
        verb="put",
        kind=f"zz{tag}",
        head=head,
        sessions=["a", "b", "c"],
        arg_sample='{"kind":"zz"}',
        result_bytes=len(head) + 40,
    )
    fp = _fp("put", f"zz{tag}", head)
    result = call_errors.run_call_errors_pass(store, skill_texts={})
    assert result.claimed >= 1
    gripes = _cluster_gripes(store, fp)
    assert len(gripes) == 1
    gid, _ = gripes[0]
    body = _gripe_body(store, gid)
    assert fp in body and "Calls (24h): 3" in body
    assert "STATUS:open" in _gripe_tags(store, gid)
    assert "origin:call-errors" in _gripe_tags(store, gid)


def test_pass_under_threshold_files_nothing(store: Any) -> None:
    tag = uuid4().hex[:8]
    head = f"[error:BadInput] unknown key 'zz{tag}' for put"
    _seed_calls(store, verb="put", kind=f"zz{tag}", head=head, sessions=["a", "a"])
    call_errors.run_call_errors_pass(store, skill_texts={})
    assert _cluster_gripes(store, _fp("put", f"zz{tag}", head)) == []


def test_pass_two_sessions_meet_threshold(store: Any) -> None:
    tag = uuid4().hex[:8]
    head = f"[error:BadInput] unknown key 'zz{tag}' for put"
    _seed_calls(store, verb="put", kind=f"zz{tag}", head=head, sessions=["a", "b"])
    call_errors.run_call_errors_pass(store, skill_texts={})
    assert len(_cluster_gripes(store, _fp("put", f"zz{tag}", head))) == 1


def test_pass_bumps_existing_gripe(store: Any) -> None:
    tag = uuid4().hex[:8]
    head = f"[error:BadInput] unknown key 'zz{tag}' for put"
    _seed_calls(store, verb="put", kind=f"zz{tag}", head=head, sessions=["a", "b", "c"])
    fp = _fp("put", f"zz{tag}", head)
    call_errors.run_call_errors_pass(store, skill_texts={})
    _seed_calls(store, verb="put", kind=f"zz{tag}", head=head, sessions=["d"])
    call_errors.run_call_errors_pass(store, skill_texts={})
    gripes = _cluster_gripes(store, fp)
    assert len(gripes) == 1
    comments = _gripe_comments(store, gripes[0][0])
    assert len(comments) == 1 and "still recurring" in comments[0]
    assert "+4 calls" in comments[0]


def test_pass_refires_after_gripe_closed(store: Any) -> None:
    tag = uuid4().hex[:8]
    head = f"[error:BadInput] unknown key 'zz{tag}' for put"
    _seed_calls(store, verb="put", kind=f"zz{tag}", head=head, sessions=["a", "b", "c"])
    fp = _fp("put", f"zz{tag}", head)
    call_errors.run_call_errors_pass(store, skill_texts={})
    gid = _cluster_gripes(store, fp)[0][0]
    from precis.store.types import Tag

    store.add_tag(
        gid, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )
    _seed_calls(store, verb="put", kind=f"zz{tag}", head=head, sessions=["d"])
    call_errors.run_call_errors_pass(store, skill_texts={})
    gripes = _cluster_gripes(store, fp)
    assert len(gripes) == 2


def test_pass_caps_new_gripes(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    tag = uuid4().hex[:8]
    rows = [
        _row(
            kind=f"zz{tag}{i}",
            head=f"[error:BadInput] unknown key 'k{i}' for put",
            session=f"s{i}",
            result_bytes=200,
        )
        for i in range(4)
    ] * 3  # 4 clusters × 3 calls each — all over MIN_CALLS
    monkeypatch.setattr(call_errors, "_scan", lambda s, h: rows)
    result = call_errors.run_call_errors_pass(store, max_new=2, skill_texts={})
    assert result.claimed == 4 and result.ok == 2


def test_non_reject_errors_ignored(store: Any) -> None:
    tag = uuid4().hex[:8]
    head = f"[error:NotFound] no such ref 'zz{tag}'"
    _seed_calls(
        store,
        verb="get",
        kind=f"zz{tag}",
        head=head,
        sessions=["a", "b", "c"],
        error_type="NotFound",
    )
    call_errors.run_call_errors_pass(store, skill_texts={})
    assert _cluster_gripes(store, _fp("get", f"zz{tag}", head)) == []


def test_gripe_body_carries_masked_samples_only(store: Any) -> None:
    tag = uuid4().hex[:8]
    head = f"[error:BadInput] unknown key 'zz{tag}' for put"
    # Assembled at runtime — the credential gate
    # (tests/test_deploy_tree_no_secrets.py) flags credential-shaped
    # literals even in fixtures. Seeded RAW into arg_sample (bypassing
    # the write-path mask) to prove the gripe body re-masks on render.
    secret = "sk-" + "ant-" + "FAKETESTKEY" + uuid4().hex[:8]
    _seed_calls(
        store,
        verb="put",
        kind=f"zz{tag}",
        head=head,
        sessions=["a", "b", "c"],
        client="claude-test/9 (model:x)",
        arg_sample='{"kind":"zz","token":"' + secret + '"}',
        result_bytes=len(head) + 40,
    )
    call_errors.run_call_errors_pass(store, skill_texts={})
    gid = _cluster_gripes(store, _fp("put", f"zz{tag}", head))[0][0]
    body = _gripe_body(store, gid)
    assert secret not in body
    assert "<redacted:" in body
    assert "claude-test/9 (model:x)" in body


def test_gripe_tags_classification(store: Any) -> None:
    tag = uuid4().hex[:8]
    head = f"[error:BadInput] unknown key 'zz{tag}' for put"
    _seed_calls(
        store,
        verb="put",
        kind=f"zz{tag}",
        head=head,
        sessions=["a", "b", "c"],
        result_bytes=len(head) + 40,
    )
    call_errors.run_call_errors_pass(store, skill_texts={})
    gid = _cluster_gripes(store, _fp("put", f"zz{tag}", head))[0][0]
    tags = _gripe_tags(store, gid)
    assert "callerr:mcp" in tags
