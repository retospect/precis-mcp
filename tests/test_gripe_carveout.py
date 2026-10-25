"""``public.file_gripe_readonly()`` (migration 0079) — the agent_ro gripe
carve-out.

``GripeHandler._create`` (``handlers/gripe.py``) now routes its base insert
through this SECURITY DEFINER SQL function instead of hand-rolled insert_ref/
insert_chunks/add_tag calls, so the function is exercised by the ordinary
``TestGripe`` suite in ``test_state_kinds.py`` on every gripe creation. These
tests pin the function's own contract directly against the DB: exactly one
gripe (ref + gripe_body chunk + STATUS:open tag) and nothing else, and that a
SECURITY DEFINER function keeps working from a role that holds no direct
INSERT grant on refs/chunks/tags/ref_tags — the whole point (an
``agent_ro``/``write:none`` connection, ``envelope.py::db_role``).

0244 adds the ro-sourced half (gr516603): ``p_source`` stamps
``source:agent_ro`` + ``friction`` + ``agent:<source>`` inside the definer
(the ro caller holds no ``ref_tags`` grant), and the bounds — 4000 chars,
3 per ``agent:<source>`` per hour, 20 ro-sourced per hour — live in SQL so a
client bug or a prompt-injected ro agent cannot bypass them. A caller whose
*effective* role — outer ``SET ROLE`` else ``session_user`` — cannot INSERT
``refs`` takes the stamped+capped path even with ``p_source = NULL`` — no
cap bypass.
"""

from __future__ import annotations

import pytest

from precis.store import Store

pytestmark = pytest.mark.db


def _call(store: Store, text: str, source: str | None = None) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT public.file_gripe_readonly(%s, %s)", (text, source)
        ).fetchone()
        assert row is not None
        conn.commit()
        return int(row[0])


def _tag_strings(store: Store, ref_id: int) -> set[str]:
    return {str(t) for t in store.tags_for(ref_id)}


def test_inserts_ref_body_chunk_and_status_open_tag(store: Store) -> None:
    ref_id = _call(store, "the search page 500s on a percent sign")

    ref = next(r for r in store.list_refs(kind="gripe", limit=50) if r.id == ref_id)
    assert ref.kind == "gripe"
    assert ref.title == "the search page 500s on a percent sign"

    blocks = store.chunks.list_chunks_for_ref(ref_id)
    assert len(blocks) == 1
    assert blocks[0].chunk_kind == "gripe_body"
    assert blocks[0].text == "the search page 500s on a percent sign"

    tags = store.tags_for(ref_id)
    assert any("STATUS:open" in str(t) for t in tags)


def test_rejects_empty_text(store: Store) -> None:
    with (
        store.pool.connection() as conn,
        pytest.raises(Exception, match="must not be empty"),
    ):
        conn.execute("SELECT public.file_gripe_readonly('')")


def test_two_calls_create_two_independent_gripes(store: Store) -> None:
    first = _call(store, "first friction report")
    second = _call(store, "second friction report")
    assert first != second
    assert len(store.chunks.list_chunks_for_ref(first)) == 1
    assert len(store.chunks.list_chunks_for_ref(second)) == 1


def test_security_definer_survives_a_role_with_no_table_grants(store: Store) -> None:
    """The whole point: a connection that can't INSERT into refs/chunks/tags
    directly can still call the function, because SECURITY DEFINER runs it
    with the *owner's* privileges. Proven with the built-in ``pg_monitor``
    predefined role (mirrors ``test_pool_db_role.py``'s precedent for
    exercising a real role switch without creating a new role) — a role
    with no grants at all on the precis tables."""
    with store.pool.connection() as conn:
        conn.execute("SET ROLE pg_monitor")
        try:
            row = conn.execute(
                "SELECT public.file_gripe_readonly(%s)",
                ("filed while running as pg_monitor",),
            ).fetchone()
        finally:
            conn.execute("RESET ROLE")
        conn.commit()
    assert row is not None
    ref_id = int(row[0])
    blocks = store.chunks.list_chunks_for_ref(ref_id)
    assert len(blocks) == 1
    assert blocks[0].chunk_kind == "gripe_body"


# ── 0244: ro-sourced stamps + bounds (gr516603) ─────────────────────


def test_null_source_keeps_the_legacy_shape(store: Store) -> None:
    """A writer's one-arg call (``GripeHandler._insert_gripe`` on the
    ordinary path) files exactly as before — no ro stamps, no caps."""
    ref_id = _call(store, "plain writer-filed gripe")
    assert _tag_strings(store, ref_id) == {"STATUS:open"}


def test_sourced_filing_stamps_ro_tags_and_nothing_else(store: Store) -> None:
    """One sourced call writes exactly one ref + one gripe_body chunk +
    four ref_tags (STATUS:open, source:agent_ro, friction, agent:<src>) —
    no links, no other rows."""
    ref_id = _call(store, "put was denied mid-run", source="run-4242")
    assert _tag_strings(store, ref_id) == {
        "STATUS:open",
        "source:agent_ro",
        "friction",
        "agent:run-4242",
    }
    blocks = store.chunks.list_chunks_for_ref(ref_id)
    assert len(blocks) == 1
    assert blocks[0].chunk_kind == "gripe_body"
    with store.pool.connection() as conn:
        n_links = conn.execute(
            "SELECT count(*) FROM links WHERE src_ref_id = %s", (ref_id,)
        ).fetchone()
        n_rt = conn.execute(
            "SELECT count(*) FROM ref_tags WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert n_links is not None and n_links[0] == 0
    assert n_rt is not None and n_rt[0] == 4


def test_run_cap_refuses_the_fourth_gripe(store: Store) -> None:
    for i in range(3):
        _call(store, f"friction {i}", source="run-cap-a")
    with pytest.raises(Exception, match="run cap"):
        _call(store, "one too many", source="run-cap-a")
    # A different run label is unaffected.
    _call(store, "a different run still files", source="run-cap-b")


def test_hourly_cap_refuses_the_21st_gripe(store: Store) -> None:
    for i in range(20):
        _call(store, f"friction from run {i}", source=f"hourly-{i}")
    with pytest.raises(Exception, match="hourly cap"):
        _call(store, "past the hourly bound", source="hourly-21")


def test_sourced_filing_over_4000_chars_refused(store: Store) -> None:
    with pytest.raises(Exception, match="4000-char"):
        _call(store, "x" * 4001, source="run-big")


def test_invalid_source_label_refused(store: Store) -> None:
    with pytest.raises(Exception, match="source label"):
        _call(store, "x", source="BAD LABEL!")
    with pytest.raises(Exception, match="source label"):
        _call(store, "x", source="a" * 49)


def test_read_only_login_role_is_stamped_and_capped_even_with_null_source(
    store: Store,
) -> None:
    """The cap bypass hole: a ro caller passing ``p_source = NULL`` must
    still take the stamped+capped path. ``session_user`` is the probe —
    ``SET SESSION AUTHORIZATION`` (superuser-only, unlike ``SET ROLE``)
    moves it to ``pg_monitor``, a role with no ``refs`` INSERT grant.

    ``agent_ro`` reads everything, so first give ``pg_monitor`` SELECT —
    the triggers the insert fires (e.g. 0176's ``gripe_status_check``)
    run under the *session* user's grants, not the definer's, and must
    read ``refs``/``tags``/``ref_tags`` just as they will on prod."""
    with store.pool.connection() as conn:
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO pg_monitor")
        conn.commit()
        conn.autocommit = True
        conn.execute("SET SESSION AUTHORIZATION pg_monitor")
        try:
            row = conn.execute(
                "SELECT public.file_gripe_readonly(%s)",
                ("filed while session-authorized as pg_monitor",),
            ).fetchone()
        finally:
            conn.execute("RESET SESSION AUTHORIZATION")
            conn.execute("REVOKE SELECT ON ALL TABLES IN SCHEMA public FROM pg_monitor")
        conn.autocommit = False
    assert row is not None
    ref_id = int(row[0])
    assert _tag_strings(store, ref_id) == {
        "STATUS:open",
        "source:agent_ro",
        "friction",
    }
    blocks = store.chunks.list_chunks_for_ref(ref_id)
    assert len(blocks) == 1
    assert blocks[0].chunk_kind == "gripe_body"


def test_set_role_effective_role_is_stamped_and_capped(store: Store) -> None:
    """Tier-2's real mechanism is ``SET ROLE agent_ro``
    (``pool._apply_db_role``): ``session_user`` stays the rw login, so a
    ``session_user``-keyed probe was dead — NULL-source filings under
    ``SET ROLE`` ran uncapped and unstamped (gr516603 review).
    ``current_setting('role')`` still reports the *outer* role inside
    the SECURITY DEFINER body (verified on the dev DB), so the
    effective role is what is checked."""
    with store.pool.connection() as conn:
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO pg_monitor")
        conn.commit()
        conn.execute("SET ROLE pg_monitor")
        try:
            row = conn.execute(
                "SELECT public.file_gripe_readonly(%s)",
                ("filed under SET ROLE pg_monitor",),
            ).fetchone()
        finally:
            conn.execute("RESET ROLE")
            conn.execute("REVOKE SELECT ON ALL TABLES IN SCHEMA public FROM pg_monitor")
        conn.commit()
    assert row is not None
    ref_id = int(row[0])
    assert _tag_strings(store, ref_id) == {
        "STATUS:open",
        "source:agent_ro",
        "friction",
    }


def test_overload_shape_one_arg_wrapper_two_arg_no_default(store: Store) -> None:
    """0244 keeps 0079's one-arg signature (squawk bans DROP FUNCTION): a
    thin wrapper delegating with ``p_source = NULL``, plus the two-arg
    overload that carries the stamp+cap logic. ``p_source`` must take NO
    default — a default would make one-arg calls ambiguous between the
    overloads (``function is not unique``)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT pronargs, pronargdefaults, prosrc FROM pg_proc "
            "WHERE proname = 'file_gripe_readonly'"
        ).fetchall()
    by_nargs = {int(r[0]): r for r in rows}
    assert set(by_nargs) == {1, 2}
    assert int(by_nargs[2][1]) == 0  # no defaults on the two-arg overload
    assert "file_gripe_readonly(p_text, NULL)" in by_nargs[1][2]


def test_cap_checks_serialize_on_an_advisory_lock(store: Store) -> None:
    """The counts are plain SELECTs: under READ COMMITTED two racing
    runs could each count below the cap and both insert (overshoot).
    Pin that the function takes a transaction-scoped advisory lock
    before counting — the concurrency case below pins the behaviour."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT prosrc FROM pg_proc "
            "WHERE proname = 'file_gripe_readonly' AND pronargs = 2"
        ).fetchone()
    assert row is not None
    assert "pg_advisory_xact_lock" in row[0]


def test_run_cap_holds_under_concurrent_filings(store: Store) -> None:
    """Five threads racing the same run label: exactly three file, the
    rest hit the run cap — the advisory lock serializes each filing's
    count+insert inside its transaction, so the outcome is exact."""
    import threading

    outcomes: list[str] = []
    guard = threading.Lock()

    def file_one(i: int) -> None:
        try:
            _call(store, f"racing filing {i}", source="race-src")
            outcome = "filed"
        except Exception as exc:
            outcome = "run cap" if "run cap" in str(exc) else f"unexpected: {exc}"
        with guard:
            outcomes.append(outcome)

    threads = [threading.Thread(target=file_one, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["filed", "filed", "filed", "run cap", "run cap"]
