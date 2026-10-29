"""Tests for the tool-call ledger's write path at the dispatch chokepoint
(``DispatchMixin.dispatch_with_status`` / ``_record_tool_call``, migration
0133) — as opposed to ``test_tool_ledger.py``, which tests the
``tool_ledger`` module's SQL directly.

DB-backed. Tag every row with a uuid so assertions survive the shared
``precis_test`` DB (filter by the tag, never absolute counts).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from precis import serve_ledger
from precis.runtime import PrecisRuntime


def test_dispatch_writes_one_ledger_row(runtime_with_store: PrecisRuntime) -> None:
    tag = uuid4().hex[:8]
    title = f"tool-ledger-title-{tag}"
    body, is_error = runtime_with_store.dispatch_with_status(
        "put", {"kind": "memory", "text": f"body-{tag}", "title": title}
    )
    assert is_error is False

    store = runtime_with_store.store
    assert store is not None
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT verb, kind, outcome, input_keys, error_type, latency_ms "
            "FROM tool_calls WHERE kind='memory' AND ts > now() - interval '1 minute' "
            "AND input_keys @> '[\"text\"]' ORDER BY call_id DESC LIMIT 5"
        ).fetchall()
    # At least one row from this call landed; find it by input_keys shape
    # (kind/text/title) rather than assuming it's the very last row, since
    # sibling tests may write concurrently to the shared test DB.
    matches = [r for r in rows if sorted(r[3]) == ["kind", "text", "title"]]
    assert matches, f"no matching ledger row among {rows!r}"
    row = matches[0]
    assert row[0] == "put"
    assert row[1] == "memory"
    assert row[2] == "ok"
    assert row[4] is None
    assert row[5] is None or row[5] >= 0


def test_dispatch_ledger_drops_none_valued_keys(
    runtime_with_store: PrecisRuntime,
) -> None:
    """None-valued kwargs are wrapper defaults, not caller input.

    tools/core.py's put/edit/get pass EVERY declared kwarg defaulted to
    None, so recording bare key presence would log the same static
    ~70-key list for every put — useless for arg-shape mining. Only keys
    the caller actually set (non-None) may land in input_keys.
    """
    tag = uuid4().hex[:8]
    title = f"tool-ledger-none-{tag}"
    body, is_error = runtime_with_store.dispatch_with_status(
        "put",
        {
            "kind": "memory",
            "text": f"body-{tag}",
            "title": title,
            "id": None,
            "mode": None,
            "url": None,
        },
    )
    assert is_error is False

    store = runtime_with_store.store
    assert store is not None
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT input_keys FROM tool_calls "
            "WHERE kind='memory' AND ts > now() - interval '1 minute' "
            "ORDER BY call_id DESC LIMIT 10"
        ).fetchall()
    key_sets = [sorted(r[0]) for r in rows]
    assert ["kind", "text", "title"] in key_sets, key_sets
    # The pre-fix signature: this call's row carrying its None-padded kwargs.
    assert ["id", "kind", "mode", "text", "title", "url"] not in key_sets


def test_dispatch_ledger_raw_args_keep_explicit_nulls(
    runtime_with_store: PrecisRuntime,
) -> None:
    """gr504415: with the raw wire arguments bound (the live MCP path),
    a caller-sent ``key=None`` lands in input_keys — only a genuinely
    omitted key is absent. ``arg_sample`` still skips the None value, so
    ``key ∈ input_keys ∧ key ∉ arg_sample`` reads as "sent as null"."""
    tag = uuid4().hex[:8]
    raw = {
        "kind": "memory",
        "text": f"body-{tag}",
        "title": f"tool-ledger-raw-{tag}",
        "id": None,  # explicitly sent null — indistinguishable downstream
    }
    token = serve_ledger.bind(None, raw)
    try:
        body, is_error = runtime_with_store.dispatch_with_status(
            "put",
            {
                "kind": "memory",
                "text": f"body-{tag}",
                "title": f"tool-ledger-raw-{tag}",
                "id": None,
            },
        )
    finally:
        serve_ledger.unbind(token)
    assert is_error is False, body

    store = runtime_with_store.store
    assert store is not None
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT input_keys, arg_sample FROM tool_calls "
            "WHERE kind='memory' AND ts > now() - interval '1 minute' "
            "ORDER BY call_id DESC LIMIT 10"
        ).fetchall()
    key_sets = [sorted(r[0]) for r in rows]
    assert ["id", "kind", "text", "title"] in key_sets, key_sets


def test_dispatch_ledger_never_leaks_payload_values(
    runtime_with_store: PrecisRuntime,
) -> None:
    tag = uuid4().hex[:8]
    secret_text = f"super-secret-body-value-{tag}"
    secret_title = f"super-secret-title-value-{tag}"
    _body, is_error = runtime_with_store.dispatch_with_status(
        "put", {"kind": "memory", "text": secret_text, "title": secret_title}
    )
    assert is_error is False

    store = runtime_with_store.store
    assert store is not None
    with store.pool.connection() as conn:
        # Scan the ENTIRE tool_calls table cast to text for the distinctive
        # secret strings — belt & suspenders against any column (now or
        # added later) accidentally carrying prose instead of key names.
        text_row = conn.execute(
            "SELECT count(*) FROM tool_calls WHERE tool_calls::text LIKE %s",
            (f"%{secret_text}%",),
        ).fetchone()
        title_row = conn.execute(
            "SELECT count(*) FROM tool_calls WHERE tool_calls::text LIKE %s",
            (f"%{secret_title}%",),
        ).fetchone()
    assert text_row is not None and title_row is not None
    hit_text = text_row[0]
    hit_title = title_row[0]
    assert hit_text == 0
    assert hit_title == 0


def test_dispatch_ledger_records_error_outcome(
    runtime_with_store: PrecisRuntime,
) -> None:
    tag = uuid4().hex[:8]
    # A bogus kind produces a BadInput/NotFound — is_error True.
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": f"no-such-kind-{tag}", "id": "1"}
    )
    assert is_error is True

    store = runtime_with_store.store
    assert store is not None
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT outcome, error_type FROM tool_calls "
            "WHERE kind=%s ORDER BY call_id DESC LIMIT 1",
            (f"no-such-kind-{tag}",),
        ).fetchone()
    assert row is not None
    assert row[0] == "error"
    assert row[1] is not None  # a PrecisError subclass name, e.g. NotFound/BadInput


def test_dispatch_ledger_0209_session_seq_sample(
    runtime_with_store: PrecisRuntime,
) -> None:
    """0209: session-bound dispatches record session_id + a per-session
    seq, the rendered body's byte length, a masked error head, and a
    bounded arg_sample (masked scalars, payload keys by length)."""
    from precis import serve_ledger

    class _FakeSession:  # weakrefable stand-in for ctx.session
        pass

    session = _FakeSession()
    with serve_ledger.session_scope(session):
        body1, _ = runtime_with_store.dispatch_with_status(
            "get", {"kind": "skill", "id": "toc"}
        )
        body2, is_error = runtime_with_store.dispatch_with_status(
            "get", {"kind": "no-such-kind-xyz", "id": "1"}
        )
    assert is_error is True

    store = runtime_with_store.store
    assert store is not None
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT session_id, seq, result_bytes, error_head, arg_sample, kind "
            "FROM tool_calls "
            "WHERE ts > now() - interval '1 minute' AND session_id IS NOT NULL "
            "ORDER BY call_id DESC LIMIT 2"
        ).fetchall()
    assert len(rows) == 2
    # Both bound calls share the one minted session_id, seq ordered 1,2.
    assert rows[0][0] == rows[1][0]
    assert sorted(r[1] for r in rows) == [1, 2]
    # The error call carries a first-line head; both carry result bytes
    # and a bounded arg_sample.
    err_row = next(r for r in rows if r[5] == "no-such-kind-xyz")
    ok_row = next(r for r in rows if r[5] == "skill")
    assert err_row[3] is not None and "error" in err_row[3]
    assert all(r[2] and r[2] > 0 for r in rows)
    assert ok_row[4] is not None and "skill" in str(ok_row[4])


def test_dispatch_ledger_stamps_run_context_from_env(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr537773: ``agentlog_id`` and ``source`` come from the run-context env
    (``PRECIS_CURRENT_AGENTLOG`` / ``PRECIS_CURRENT_SOURCE``) — ``source`` is the
    producing JOB, not the model tier, even with ``PRECIS_CURRENT_MODEL`` set;
    with neither var set both columns are NULL."""
    store = runtime_with_store.store
    assert store is not None
    tag = uuid4().hex[:8]
    kind_set, kind_unset = f"ctx-set-{tag}", f"ctx-unset-{tag}"

    monkeypatch.setenv("PRECIS_CURRENT_AGENTLOG", "123")
    monkeypatch.setenv("PRECIS_CURRENT_SOURCE", "plan_tick")
    monkeypatch.setenv("PRECIS_CURRENT_MODEL", "sonnet")
    runtime_with_store.dispatch_with_status("get", {"kind": kind_set, "id": "1"})

    monkeypatch.delenv("PRECIS_CURRENT_AGENTLOG")
    monkeypatch.delenv("PRECIS_CURRENT_SOURCE")
    monkeypatch.delenv("PRECIS_CURRENT_MODEL")
    runtime_with_store.dispatch_with_status("get", {"kind": kind_unset, "id": "1"})

    with store.pool.connection() as conn:
        rows = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                "SELECT kind, agentlog_id, source FROM tool_calls WHERE kind = ANY(%s)",
                ([kind_set, kind_unset],),
            ).fetchall()
        }
    assert rows[kind_set] == (123, "plan_tick")
    assert rows[kind_unset] == (None, None)


def test_dispatch_ledger_records_result_count(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``Response.result_count`` lands in ``tool_calls.result_count`` (the
    "reserved" column of migration 0133): a search records its hit count,
    a bare get records 1, a non-enumerating verb records NULL."""
    tag = uuid4().hex[:8]
    marker = f"rc-{tag}"
    _, is_error = runtime_with_store.dispatch_with_status(
        "put",
        {"kind": "memory", "text": f"body-{tag}", "title": marker, "tags": [marker]},
    )
    assert is_error is False

    store = runtime_with_store.store
    assert store is not None
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind='memory' AND title=%s "
            "ORDER BY ref_id DESC LIMIT 1",
            (marker,),
        ).fetchone()
    assert row is not None
    ref_id = int(row[0])

    _b, err_get = runtime_with_store.dispatch_with_status(
        "get", {"kind": "memory", "id": str(ref_id)}
    )
    assert err_get is False
    _b, err_hit = runtime_with_store.dispatch_with_status(
        "search", {"kind": "memory", "tags": [marker]}
    )
    assert err_hit is False
    _b, err_miss = runtime_with_store.dispatch_with_status(
        "search", {"kind": "memory", "tags": [f"{marker}-none"]}
    )
    assert err_miss is False

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT verb, result_count FROM tool_calls "
            "WHERE ts > now() - interval '1 minute' "
            "AND arg_sample::text LIKE %s "
            "ORDER BY call_id DESC",
            (f"%{tag}%",),
        ).fetchall()
        get_row = conn.execute(
            "SELECT result_count FROM tool_calls "
            "WHERE verb='get' AND ts > now() - interval '1 minute' "
            "AND arg_sample::text LIKE %s "
            "ORDER BY call_id DESC LIMIT 1",
            (f'%"{ref_id}"%',),
        ).fetchone()
    by_verb = {(r[0], str(r[1])) for r in rows}
    # The tag value rides arg_sample on both searches; the put too (its
    # tags= list is sampled, its payload scalars length-summarized).
    assert ("search", "1") in by_verb  # tagged hit
    assert ("search", "0") in by_verb  # the -none tag found nothing
    assert ("put", "None") in by_verb  # an ack enumerates no collection
    assert get_row is not None and get_row[0] == 1  # a resolved single ref


def test_dispatch_ledger_result_bytes_equals_body_length(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``result_bytes`` is the UTF-8 byte length of the exact body the
    caller got back (block-navigation slice 2 meter — the roll-up sums
    it, so off-by-rendering bugs would corrupt per-session bytes)."""
    tag = uuid4().hex[:8]
    _, is_error = runtime_with_store.dispatch_with_status(
        "put",
        {"kind": "memory", "text": f"body-{tag}", "title": f"rb-{tag}"},
    )
    assert is_error is False

    store = runtime_with_store.store
    assert store is not None
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind='memory' AND title=%s "
            "ORDER BY ref_id DESC LIMIT 1",
            (f"rb-{tag}",),
        ).fetchone()
    assert row is not None
    ref_id = int(row[0])

    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "memory", "id": str(ref_id)}
    )
    assert is_error is False, body

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT result_bytes FROM tool_calls "
            "WHERE verb='get' AND kind='memory' "
            "AND arg_sample::text LIKE %s "
            "AND ts > now() - interval '1 minute' "
            "ORDER BY call_id DESC LIMIT 1",
            (f'%"{ref_id}"%',),
        ).fetchone()
    assert row is not None
    assert row[0] == len(body.encode("utf-8"))


def test_dispatch_survives_ledger_write_failure(
    runtime_with_store: PrecisRuntime, monkeypatch: Any
) -> None:
    # Fail-open: a raising ledger writer must never break the verb call
    # itself — the dispatch still succeeds and returns the normal body.
    import precis.tool_ledger as tool_ledger

    def _boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("ledger write exploded")

    monkeypatch.setattr(tool_ledger, "record_call", _boom)

    tag = uuid4().hex[:8]
    body, is_error = runtime_with_store.dispatch_with_status(
        "put", {"kind": "memory", "text": f"resilient-{tag}", "title": f"t-{tag}"}
    )
    assert is_error is False
    assert isinstance(body, str) and body  # normal ack body, call was unaffected


def test_dispatch_ledger_noop_without_store(runtime: PrecisRuntime) -> None:
    # ``runtime`` (stateless, no store) must not raise when dispatching —
    # ``_record_tool_call`` no-ops cleanly when ``self.store`` is None.
    body, is_error = runtime.dispatch_with_status("get", {"kind": "skill", "id": "toc"})
    assert isinstance(body, str)


def test_tool_ledger_is_the_only_tool_calls_writer() -> None:
    """One chokepoint: no module but ``tool_ledger`` inserts into
    ``tool_calls`` (an empty-error_head row would come from a second path)."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    pat = re.compile(r"insert\s+into\s+tool_calls|copy\s+tool_calls", re.I)
    hits = sorted(
        str(p.relative_to(src))
        for p in src.rglob("*.py")
        if pat.search(p.read_text(encoding="utf-8"))
    )
    assert hits == ["precis/tool_ledger.py"]


def test_every_error_path_fills_error_head_source_client(
    runtime_with_store: PrecisRuntime, monkeypatch: Any
) -> None:
    """gr494831 (b): all tool_calls rows come from one writer
    (``tool_ledger._write``) behind ``_record_tool_call``. An error row
    reaches it by three dispatch branches (typed PrecisError, boundary-
    mapped library fault, Internal fallback); each must carry error_head,
    and source + client when known."""
    import psycopg.errors as pge

    from precis import serve_ledger
    from precis.runtime import dispatch as d

    monkeypatch.setenv("PRECIS_CURRENT_SOURCE", "plan_tick")
    monkeypatch.setattr(d, "_retry_sleep", lambda _a: None)

    class _Info:
        name = "ledger-test"
        version = "9"

    class _Params:
        clientInfo = _Info()
        meta = None

    class _Session:
        client_params = _Params()

    tag = uuid4().hex[:8]
    calc = runtime_with_store.hub.handler_for("calc")
    assert calc is not None
    original = calc.get
    with serve_ledger.session_scope(_Session()):
        for exc in (pge.LockNotAvailable("x"), KeyError("y")):

            def boom(_e: BaseException = exc, **_kw: object) -> None:
                raise _e

            calc.get = boom
            try:
                _, is_error = runtime_with_store.dispatch_with_status(
                    "get", {"kind": "calc", "id": f"typed-{tag}"}
                )
            finally:
                calc.get = original
            assert is_error
        _, is_error = runtime_with_store.dispatch_with_status(
            "get", {"kind": f"no-such-kind-{tag}", "id": "1"}
        )
        assert is_error

    store = runtime_with_store.store
    assert store is not None
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT error_type, error_head, source, client FROM tool_calls "
            "WHERE outcome='error' AND ts > now() - interval '1 minute' "
            "AND client = 'ledger-test/9' ORDER BY call_id DESC LIMIT 3"
        ).fetchall()
    assert len(rows) == 3
    for error_type, head, source, client in rows:
        assert error_type and head and "error" in head
        assert source == "plan_tick"
        assert client == "ledger-test/9"


def test_put_through_tool_boundary_records_session_and_client(
    runtime_with_store: PrecisRuntime, monkeypatch: Any
) -> None:
    """gr504485 contract: a ``put`` dispatched through the tool boundary
    (``tools.core.put``) with a bound MCP session yields a ``tool_calls``
    row with non-NULL ``session_id`` and ``client``. Write verbs used to
    never bind the session, so ~30% of the ledger was unattributable."""
    from typing import cast

    from precis.tools import core

    class _Info:
        name = "boundary-test"
        version = "3"

    class _Params:
        clientInfo = _Info()
        meta = None

    class _Session:
        client_params = _Params()

    class _Ctx:
        def __init__(self, session: object) -> None:
            self.session = session

    monkeypatch.setattr(core, "_runtime", runtime_with_store)
    result = core.put(
        kind="memory",
        text="session-boundary-probe",
        title="boundary-probe",
        ctx=cast(Any, _Ctx(_Session())),
    )
    assert getattr(result, "isError", False) is False
    assert "created" in str(result)

    store = runtime_with_store.store
    assert store is not None
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT session_id, seq, client FROM tool_calls "
            "WHERE verb='put' AND client='boundary-test/3' "
            "AND ts > now() - interval '1 minute' "
            "ORDER BY call_id DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "no ledger row for the boundary put"
    assert row[0] is not None, "session_id is NULL on the write path"
    assert row[1] is not None and row[1] >= 1
    assert row[2] == "boundary-test/3"
