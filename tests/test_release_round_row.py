"""The shared ``release.round`` record: row shape and the store twin
(``precis.plans.release`` mirrors ``scripts/round``'s publish, td573429)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from precis import settings as psettings
from precis.plans import release as plans_release
from precis.store import Store
from precis.store.types import Tag

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
OWNER = {"host": "fake-node", "checkout": "/fake/checkout"}
BASE = "ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12"


@pytest.fixture(autouse=True)
def _fresh_settings_cache() -> None:
    psettings.invalidate()


def _rel(**over: object) -> dict:
    rel = {
        "n": 1,
        "branch": "release/r1",
        "base": BASE,
        "state": "cut",
        "cut_at": "2026-11-06T11:00:00Z",
    }
    rel.update(over)
    return rel


def test_row_from_open_record_has_no_deploy() -> None:
    row = plans_release.round_record(_rel(), n=1, owner=OWNER, now=NOW)
    assert row == {
        "n": 1,
        "branch": "release/r1",
        "base": BASE,
        "state": "cut",
        "owner": OWNER,
        "cut_at": "2026-11-06T11:00:00Z",
    }


def test_row_from_deploying_record_maps_the_journal() -> None:
    dep = {
        "sha": BASE,
        "started_at": "2026-11-06T11:30:00Z",
        "rollout_at": "2026-11-06T11:40:00Z",
        "runtime_confirmed_at": "2026-11-06T11:55:00Z",
        "runtime_evidence": "health green",
        "skipped_hosts": {"fake-host": "aa" * 20},
    }
    row = plans_release.round_record(
        _rel(state="confirmed", deployment=dep), n=1, owner=OWNER, now=NOW
    )
    assert row["deploy"] == {
        "sha": BASE,
        "rollout_at": "2026-11-06T11:40:00Z",
        "confirmed_at": "2026-11-06T11:55:00Z",
        "evidence": "health green",
    }


def test_row_omits_absent_deploy_keys() -> None:
    row = plans_release.round_record(
        _rel(state="deploying", deployment={"sha": BASE, "started_at": "x"}),
        n=1,
        owner=OWNER,
        now=NOW,
    )
    assert row["deploy"] == {"sha": BASE}


def test_row_maps_started_epoch_into_deploy() -> None:
    """``started_epoch`` is the live-rollout marker ``is_terminal`` reads
    on the row — it rides the publish when the journal carries it, and
    stays absent (never null) when it does not."""
    row = plans_release.round_record(
        _rel(
            state="failed",
            deployment={
                "sha": BASE,
                "started_at": "2026-11-06T11:30:00Z",
                "started_epoch": 1762000000,
            },
        ),
        n=1,
        owner=OWNER,
        now=NOW,
    )
    assert row["deploy"] == {"sha": BASE, "started_epoch": 1762000000}
    row = plans_release.round_record(
        _rel(state="failed", deployment={"sha": BASE, "started_at": "x"}),
        n=1,
        owner=OWNER,
        now=NOW,
    )
    assert "started_epoch" not in row["deploy"]


def test_row_carries_state_at_when_present() -> None:
    row = plans_release.round_record(
        _rel(state="deploying", state_at="2026-11-06T11:20:00Z"),
        n=1,
        owner=OWNER,
        now=NOW,
    )
    assert row["state_at"] == "2026-11-06T11:20:00Z"


def test_row_omits_state_at_when_absent() -> None:
    """A record cut before state_at existed publishes no key — never a
    null — so the watchdog knows not to judge it."""
    row = plans_release.round_record(_rel(), n=1, owner=OWNER, now=NOW)
    assert "state_at" not in row


def test_record_round_then_read_round(store: Store) -> None:
    row = plans_release.round_record(_rel(), n=1, owner=OWNER, now=NOW)
    line = plans_release.record_round(store, row)
    assert line == "round record recorded: r1 cut"
    assert plans_release.read_round(store) == row


def test_read_round_tolerates_unset_and_garbage(store: Store) -> None:
    assert plans_release.read_round(store) is None
    for bad in ("{not json", "[1, 2]", '"just a string"', "null", "7"):
        psettings.set_setting(plans_release.ROUND_KEY, bad, store=store)
        assert plans_release.read_round(store) is None, bad


# ── update_round: the job types' writer (gr575362) ──────────────────
#
# ``release_cut`` creates (or replaces a lower-n row), ``release_deploy``
# advances the same-n row's state. A write never lowers the state's rank,
# never touches a higher ``n``, and never overwrites a row it cannot
# parse — the script's own publish wins where it wrote first.


def _fields(n: int = 1, state: str = "cut", **over: object) -> dict:
    fields: dict[str, object] = {
        "n": n,
        "branch": f"release/r{n}",
        "base": BASE,
        "state": state,
        "owner": OWNER,
    }
    fields.update(over)
    return fields


def _seed(store: Store, row: dict) -> dict:
    plans_release.record_round(store, row)
    return row


def test_update_round_create_writes_a_stamped_row(store: Store) -> None:
    notes = plans_release.update_round(store, _fields(), create=True, now=NOW)
    assert notes == ["release.round: wrote r1 cut"]
    assert plans_release.read_round(store) == {
        "n": 1,
        "branch": "release/r1",
        "base": BASE,
        "state": "cut",
        "owner": OWNER,
        "state_at": "2026-11-06T12:00:00Z",
        "cut_at": "2026-11-06T12:00:00Z",
    }


def test_update_round_create_keeps_a_caller_stamp(store: Store) -> None:
    plans_release.update_round(
        store,
        _fields(state_at="2026-11-06T09:00:00Z"),
        create=True,
        now=NOW,
    )
    row = plans_release.read_round(store)
    assert row is not None
    assert row["state_at"] == "2026-11-06T09:00:00Z"
    assert row["cut_at"] == "2026-11-06T12:00:00Z"


def test_update_round_same_n_create_leaves_the_row(store: Store) -> None:
    """A same-n row is the script's own publish; the job leaves it."""
    seeded = _seed(store, _fields(state="deployed"))
    notes = plans_release.update_round(store, _fields(), create=True, now=NOW)
    assert "already on record (deployed)" in notes[0]
    assert plans_release.read_round(store) == seeded


def test_update_round_same_n_create_stamps_the_pass(store: Store) -> None:
    """The script's publish wrote the same-n ``cut`` row first — it has
    no ``pass``: the job stamps just that key so the dropped-pass
    abandon in ``release_poll`` can still fire."""
    seeded = _seed(store, _fields(state="cut"))
    fields = _fields()
    fields["pass"] = "td42"
    notes = plans_release.update_round(store, fields, create=True, now=NOW)
    assert "stamped td42" in notes[0]
    assert plans_release.read_round(store) == {**seeded, "pass": "td42"}


def test_update_round_same_n_create_keeps_a_recorded_pass(store: Store) -> None:
    seeded = _seed(store, {**_fields(state="cut"), "pass": "td41"})
    fields = _fields()
    fields["pass"] = "td99"
    notes = plans_release.update_round(store, fields, create=True, now=NOW)
    assert "already on record (cut)" in notes[0]
    assert plans_release.read_round(store) == seeded


def test_update_round_same_n_create_stamps_no_pass_on_a_later_state(
    store: Store,
) -> None:
    seeded = _seed(store, _fields(state="deployed"))
    fields = _fields()
    fields["pass"] = "td42"
    notes = plans_release.update_round(store, fields, create=True, now=NOW)
    assert "already on record (deployed)" in notes[0]
    assert plans_release.read_round(store) == seeded


@pytest.mark.parametrize("state", ["abandoned", "superseded", "failed"])
def test_update_round_create_replaces_a_dead_same_n_row_of_another_pass(
    store: Store, state: str
) -> None:
    """gr714067: r1 died without shipping under pass td41 and its number
    was minted again for td99. The cut replaces the row whole — left
    alone it stayed ``abandoned`` through the new release's deploy and
    confirm, and every reader saw a dead release."""
    _seed(store, {**_fields(state=state), "pass": "td41", "abandoned_why": "x"})
    fields = _fields(base="c" * 40)
    fields["pass"] = "td99"
    notes = plans_release.update_round(store, fields, create=True, now=NOW)
    assert notes == [f"release.round: wrote r1 cut over the dead row ({state}, td41)"]
    assert plans_release.read_round(store) == {
        **fields,
        "state_at": "2026-11-06T12:00:00Z",
        "cut_at": "2026-11-06T12:00:00Z",
    }


@pytest.mark.parametrize(
    ("row", "why"),
    [
        pytest.param({"state": "confirmed", "pass": "td41"}, "confirmed", id="shipped"),
        pytest.param({"state": "abandoned", "pass": "td99"}, "abandoned", id="own"),
        pytest.param(
            {"state": "failed", "pass": "td41", "deploy": {"started_epoch": 5}},
            "failed",
            id="partial-rollout",
        ),
    ],
)
def test_update_round_create_leaves_a_same_n_row_that_is_not_dead(
    store: Store, row: dict, why: str
) -> None:
    """A release that shipped, this pass's own row, and a ``failed`` row
    whose rollout launched (a live partial rollout) are never replaced."""
    seeded = _seed(store, {**_fields(), **row})
    fields = _fields(base="c" * 40)
    fields["pass"] = "td99"
    notes = plans_release.update_round(store, fields, create=True, now=NOW)
    assert f"already on record ({why})" in notes[0]
    assert plans_release.read_round(store) == seeded


def test_update_round_recut_moves_a_lower_n_row(store: Store) -> None:
    _seed(store, _fields(n=1, state="confirmed"))
    notes = plans_release.update_round(store, _fields(n=2), create=True, now=NOW)
    assert notes == ["release.round: wrote r2 cut"]
    row = plans_release.read_round(store)
    assert row is not None
    assert row["n"] == 2 and row["state"] == "cut"
    assert row["cut_at"] == "2026-11-06T12:00:00Z"


def test_update_round_never_touches_a_higher_n_row(store: Store) -> None:
    seeded = _seed(store, _fields(n=2, state="cut"))
    notes = plans_release.update_round(store, _fields(n=1), create=True, now=NOW)
    assert "the row is r2, not r1" in notes[0]
    assert plans_release.read_round(store) == seeded


def test_update_round_never_overwrites_an_unreadable_row(store: Store) -> None:
    for bad in ("{not json", "[1,2]"):
        psettings.set_setting(plans_release.ROUND_KEY, bad, store=store)
        notes = plans_release.update_round(store, _fields(), create=True, now=NOW)
        assert "nothing written" in notes[0], bad
        assert plans_release.read_round(store) is None
        assert psettings.get_str(plans_release.ROUND_KEY, store=store) == bad


def test_update_round_survives_a_db_error(store: Store, monkeypatch) -> None:
    def boom(s: Store) -> str | None:
        raise OSError("db gone")

    monkeypatch.setattr(plans_release, "_round_raw", boom)
    notes = plans_release.update_round(store, _fields(), create=True, now=NOW)
    assert notes == ["release.round: unreadable (OSError); nothing written"]


def test_update_round_needs_an_n(store: Store) -> None:
    notes = plans_release.update_round(store, {"state": "cut"}, create=True, now=NOW)
    assert "nothing written" in notes[0]
    assert plans_release.read_round(store) is None


def test_update_round_non_create_never_creates(store: Store) -> None:
    notes = plans_release.update_round(store, _fields(state="deployed"), now=NOW)
    assert "no row on record" in notes[0]
    assert plans_release.read_round(store) is None


def test_update_round_advance_merges_deploy_and_records_keys_win(
    store: Store,
) -> None:
    """The deploy merge: state moves forward, ``state_at`` is stamped,
    ``deploy`` keys the script already published are kept (setdefault)."""
    _seed(
        store,
        _fields(
            state="deploying",
            state_at="2026-11-06T10:00:00Z",
            deploy={"sha": "a" * 40, "rollout_at": "2026-11-06T10:30:00Z"},
        ),
    )
    notes = plans_release.update_round(
        store,
        {
            "n": 1,
            "state": "deployed",
            "deploy": {"sha": "b" * 40, "evidence": "job evidence"},
        },
        now=NOW,
    )
    assert notes == ["release.round: r1 deploying -> deployed"]
    row = plans_release.read_round(store)
    assert row is not None
    assert row["state"] == "deployed"
    assert row["state_at"] == "2026-11-06T12:00:00Z"
    assert row["deploy"] == {
        "sha": "a" * 40,  # the script's publish wins
        "rollout_at": "2026-11-06T10:30:00Z",
        "evidence": "job evidence",  # an absent key is filled
    }


@pytest.mark.parametrize("state", ["confirmed", "superseded", "abandoned"])
def test_update_round_never_lowers_a_terminal_state(store: Store, state: str) -> None:
    seeded = _seed(store, _fields(state=state))
    notes = plans_release.update_round(store, {"n": 1, "state": "deployed"}, now=NOW)
    assert "not a move forward" in notes[0]
    assert plans_release.read_round(store) == seeded


def test_update_round_same_state_is_not_a_move_forward(store: Store) -> None:
    seeded = _seed(store, _fields(state="deployed"))
    notes = plans_release.update_round(
        store,
        {"n": 1, "state": "deployed", "deploy": {"sha": "b" * 40}},
        now=NOW,
    )
    assert "not a move forward" in notes[0]
    assert plans_release.read_round(store) == seeded


def test_update_round_needs_a_known_want_state(store: Store) -> None:
    seeded = _seed(store, _fields(state="cut"))
    notes = plans_release.update_round(store, {"n": 1, "state": "bogus"}, now=NOW)
    assert "not a move forward" in notes[0]
    assert plans_release.read_round(store) == seeded


@pytest.mark.parametrize("state", ["deploying", "deployed"])
def test_update_round_a_terminal_write_needs_a_pre_deploy_row(
    store: Store, state: str
) -> None:
    """The dropped-pass abandon goes through the same rank rules: a
    job-side terminal write may take a rank-0 row, never a row that
    reads as rolled out — a concurrent ``deployed`` write survives."""
    seeded = _seed(store, _fields(state=state))
    notes = plans_release.update_round(store, {"n": 1, "state": "abandoned"}, now=NOW)
    assert "terminal write" in notes[0]
    assert plans_release.read_round(store) == seeded


def test_update_round_advance_merges_extra_fields(store: Store) -> None:
    """The abandon's ``abandoned_why`` rides along: non-reserved fields
    merge with the recorded keys winning, like ``deploy``."""
    _seed(store, _fields(state="cut"))
    notes = plans_release.update_round(
        store,
        {"n": 1, "state": "abandoned", "abandoned_why": "pass td9 ended"},
        now=NOW,
    )
    assert notes == ["release.round: r1 cut -> abandoned"]
    row = plans_release.read_round(store)
    assert row is not None
    assert row["state"] == "abandoned"
    assert row["state_at"] == "2026-11-06T12:00:00Z"
    assert row["abandoned_why"] == "pass td9 ended"


def test_update_round_a_string_n_is_not_this_round(store: Store) -> None:
    seeded = _seed(store, {**_fields(), "n": "1"})
    notes = plans_release.update_round(store, _fields(), create=True, now=NOW)
    assert "not r1" in notes[0]
    assert plans_release.read_round(store) == seeded


# ── round_confirmed / live_deploy_job: release_cut's stale-local reads ──
# (td575911 — the cut supersedes the clone's own deploying/deployed
# round.json record only when the shared row proves prod moved past it
# and no release_deploy job may still write.)


def test_round_confirmed_needs_confirmed_at_n_or_later() -> None:
    """``confirmed`` is the only terminal state that proves a rollout;
    a later confirmed round proves prod moved past r<n> too, a smaller
    number never does."""
    assert plans_release.round_confirmed({"n": 7, "state": "confirmed"}, 7)
    assert plans_release.round_confirmed({"n": 8, "state": "confirmed"}, 7)
    assert not plans_release.round_confirmed({"n": 6, "state": "confirmed"}, 7)
    assert not plans_release.round_confirmed(None, 7)
    assert not plans_release.round_confirmed({"n": "7", "state": "confirmed"}, 7)
    assert not plans_release.round_confirmed({"n": True, "state": "confirmed"}, 7)
    assert not plans_release.round_confirmed({"n": 7, "state": "confirmed"}, True)
    for state in (
        "cut",
        "gating",
        "fixing",
        "green",
        "deploying",
        "deployed",
        "superseded",
        "abandoned",
    ):
        assert not plans_release.round_confirmed({"n": 7, "state": state}, 7), state


def _deploy_job(store: Store, job_type: str = "release_deploy") -> int:
    return int(
        store.insert_ref(
            kind="job",
            slug=None,
            title=f"{job_type} job",
            meta={"job_type": job_type},
            parent_id=None,
        ).id
    )


def test_live_deploy_job(store: Store) -> None:
    assert plans_release.live_deploy_job(store) is False
    job = _deploy_job(store)
    assert plans_release.live_deploy_job(store) is True  # no STATUS tag = live
    store.add_tag(
        job, Tag.closed("STATUS", "succeeded"), set_by="system", replace_prefix=True
    )
    assert plans_release.live_deploy_job(store) is False
    _deploy_job(store, job_type="release_cut")  # another type is not a deploy
    assert plans_release.live_deploy_job(store) is False
    gone = _deploy_job(store)
    store.retire_ref(gone)
    assert plans_release.live_deploy_job(store) is False


def test_live_deploy_job_plan_uses_partial_index(store: Store) -> None:
    """EXPLAIN of the check's own statement picks
    ``refs_job_release_deploy_idx``.

    The newest-50 ``release_deploy`` read had the gr685472 shape — a
    full walk of the job rows; the partial index starts from the handful
    of rows ever stamped with that job_type. On a tiny test table the
    planner could still prefer a seq scan or ``refs_kind_idx``, so both
    alternatives are forbidden rather than weakening the assert.
    """
    _deploy_job(store)
    _deploy_job(store, job_type="release_cut")
    with store.pool.connection() as conn:
        conn.execute("ANALYZE refs")
        conn.execute("SET LOCAL enable_seqscan = off")
        conn.execute("SET LOCAL enable_bitmapscan = off")
        plan = "\n".join(
            str(r[0])
            for r in conn.execute(
                "EXPLAIN " + plans_release._LIVE_DEPLOY_JOB_SQL
            ).fetchall()
        )
    assert "refs_job_release_deploy_idx" in plan, plan
    assert "refs_kind_idx" not in plan, plan
    assert "refs_ref_id_kind_key" not in plan, plan


def test_release_deploy_index_predicate_matches(store: Store) -> None:
    """The index predicate is the statement's WHERE, kept in step — the
    planner only uses a partial index when the query implies it, so an
    edit to ``_LIVE_DEPLOY_JOB_SQL`` that breaks the implication turns
    the index dead weight."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT i.indisvalid, pg_get_expr(i.indpred, i.indrelid) "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = 'refs_job_release_deploy_idx'"
        ).fetchone()
    assert row is not None, (
        "refs_job_release_deploy_idx missing — migration not applied"
    )
    valid, pred = row
    assert valid
    for fragment in (
        "kind = 'job'::text",
        "retired_at IS NULL",
        "job_type",
        "release_deploy",
    ):
        assert fragment in pred, pred
