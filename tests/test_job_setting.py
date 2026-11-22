"""``put(kind='job', mode='setting')`` + ``id='/claim'`` — the typed,
audited setter and read view for the ``claim.*`` ``app_state`` operator
overrides (gr556919). Before this door an override needed a raw
``INSERT INTO app_state`` through prod-psql.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Denied
from precis.handlers.job import JobHandler
from precis.store import Store
from precis.store._salience import as_background_actor


@pytest.fixture
def jobs(hub: Hub) -> JobHandler:
    return JobHandler(hub=hub)


def _set(jobs: JobHandler, key: str, value: Any, **kw: Any) -> str:
    kw.setdefault("reason", "gr551639: drain is starved")
    return jobs.put(mode="setting", key=key, value=value, **kw).body


def _audit_rows(store: Store) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT payload FROM worker_logs "
            "WHERE payload->>'event' = 'claim-setting' ORDER BY log_id"
        ).fetchall()
    return [r[0] if isinstance(r[0], dict) else json.loads(str(r[0])) for r in rows]


def test_set_read_unset_round_trip(jobs: JobHandler, store: Store) -> None:
    key = "claim.max_running.derived_drain"

    body = _set(jobs, key, 4)

    assert "set claim.max_running.derived_drain" in body
    assert "4" in body
    assert store.get_setting(key) == "4"

    body = jobs.put(mode="setting", key=key, unset=True, reason="back to default").body
    assert "cleared" in body
    assert store.get_setting(key) is None


def test_set_writes_the_audit_row(jobs: JobHandler, store: Store) -> None:
    _set(jobs, "claim.max_running.derived_drain", 4)

    [row] = _audit_rows(store)
    assert row["key"] == "claim.max_running.derived_drain"
    assert row["old"] is None
    assert row["new"] == "4"
    assert row["reason"] == "gr551639: drain is starved"
    assert row["actor"]

    jobs.put(
        mode="setting",
        key="claim.max_running.derived_drain",
        unset=True,
        reason="done",
    )
    rows = _audit_rows(store)
    assert len(rows) == 2
    assert rows[1]["new"] is None
    assert rows[1]["old"] == "4"


def test_value_types_per_family(jobs: JobHandler, store: Store) -> None:
    _set(jobs, "claim.slicing.job_inproc", True)
    assert store.get_setting("claim.slicing.job_inproc") == "true"
    _set(jobs, "claim.slicing.job_inproc", False)
    assert store.get_setting("claim.slicing.job_inproc") == "false"
    _set(jobs, "claim.slice_s.job_inproc", 1800)
    assert store.get_setting("claim.slice_s.job_inproc") == "1800"


def test_unknown_prefix_refused(jobs: JobHandler) -> None:
    with pytest.raises(BadInput, match="unknown claim override key"):
        _set(jobs, "claim.bogus.x", 1)


def test_unknown_job_type_refused_naming_accepted(jobs: JobHandler) -> None:
    with pytest.raises(BadInput, match="unknown job_type"):
        _set(jobs, "claim.max_running.no_such_type", 1)


def test_unknown_executor_refused(jobs: JobHandler) -> None:
    with pytest.raises(BadInput, match="unknown executor"):
        _set(jobs, "claim.slicing.no_such_executor", True)


@pytest.mark.parametrize("value", [65, -1, "4", True, 2.5])
def test_max_running_type_and_range(jobs: JobHandler, value: Any) -> None:
    with pytest.raises(BadInput):
        _set(jobs, "claim.max_running.derived_drain", value)


@pytest.mark.parametrize("value", ["true", 1, 0])
def test_slicing_needs_a_bool(jobs: JobHandler, value: Any) -> None:
    with pytest.raises(BadInput, match="bool"):
        _set(jobs, "claim.slicing.job_inproc", value)


@pytest.mark.parametrize(
    "value", [0, -1, "1200", False, float("inf"), float("-inf"), float("nan")]
)
def test_slice_s_needs_a_positive_finite_number(jobs: JobHandler, value: Any) -> None:
    with pytest.raises(BadInput):
        _set(jobs, "claim.slice_s.job_inproc", value)


def test_missing_reason_refused(jobs: JobHandler) -> None:
    with pytest.raises(BadInput, match="reason"):
        jobs.put(mode="setting", key="claim.max_running.derived_drain", value=4)


def test_unset_and_value_are_mutually_exclusive(jobs: JobHandler) -> None:
    with pytest.raises(BadInput, match="mutually exclusive"):
        jobs.put(
            mode="setting",
            key="claim.max_running.derived_drain",
            value=4,
            unset=True,
            reason="x",
        )


def test_setting_takes_no_mint_kwargs(jobs: JobHandler) -> None:
    with pytest.raises(BadInput, match="mints no job row"):
        jobs.put(
            mode="setting",
            id=42,
            key="claim.max_running.derived_drain",
            value=4,
            reason="x",
        )
    with pytest.raises(BadInput, match="mints no job row"):
        jobs.put(
            mode="setting",
            job_type="fix_gripe",
            key="claim.max_running.derived_drain",
            value=4,
            reason="x",
        )


def test_setting_kwargs_refused_on_other_modes(jobs: JobHandler) -> None:
    with pytest.raises(BadInput, match="only apply to put"):
        jobs.put(job_type="fix_gripe", key="claim.max_running.derived_drain")


@pytest.mark.parametrize(
    "source",
    ["asa-worker", "precis-worker-agent", "precis-worker-drain-x", "mcp", "web:owner"],
)
def test_disallowed_source_denied(
    jobs: JobHandler, store: Store, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    """Every source outside the ``cli`` allowlist is
    Denied — the deployed ``precis-worker*`` lanes included (``is_owner``
    would wave them through)."""
    monkeypatch.setenv("PRECIS_SOURCE", source)
    with pytest.raises(Denied, match=source):
        _set(jobs, "claim.max_running.derived_drain", 4)
    assert store.get_setting("claim.max_running.derived_drain") is None


@pytest.mark.parametrize("source", ["cli"])
def test_allowed_sources_may_write(
    jobs: JobHandler, store: Store, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    monkeypatch.setenv("PRECIS_SOURCE", source)
    _set(jobs, "claim.max_running.derived_drain", 4)
    assert store.get_setting("claim.max_running.derived_drain") == "4"


def test_background_actor_denied(jobs: JobHandler, store: Store) -> None:
    with (
        as_background_actor("plan_tick"),
        pytest.raises(Denied),
    ):
        _set(jobs, "claim.max_running.derived_drain", 4)


def test_claim_view_lists_overrides_next_to_defaults(
    jobs: JobHandler, store: Store
) -> None:
    _set(jobs, "claim.max_running.derived_drain", 4)
    _set(jobs, "claim.slice_s.job_inproc", 900)

    body = jobs.get(id="/claim").body

    assert "claim.max_running.<job_type>" in body
    assert "derived_drain" in body
    # the override, the code default (_MAX_RUNNING_DEFAULTS) and the
    # effective value all show on the row
    assert "4" in body
    assert "claim.slicing.<executor>" in body
    assert "job_inproc" in body
    assert "900" in body


def test_claim_view_shows_a_stale_override(jobs: JobHandler, store: Store) -> None:
    """An override whose leaf names no current job_type still renders —
    stale keys stay visible rather than vanishing."""
    store.set_setting("claim.max_running.renamed_away", "2")
    body = jobs.get(id="/claim").body
    assert "renamed_away" in body
    assert "uncapped" in body


def test_claim_view_takes_no_filters(jobs: JobHandler) -> None:
    with pytest.raises(BadInput, match="no filters"):
        jobs.get(id="/claim?since=1")


def test_footprint_cpu_override_round_trip(jobs: JobHandler, store: Store) -> None:
    """gr741695: the spawn-ledger CPU of a run_slice work class is an
    audited claim.* setting."""
    key = "claim.footprint.slice.cpu_milli"
    _set(jobs, key, 750)
    assert store.get_setting(key) == "750"
    _set(jobs, key, 1250.0)
    assert store.get_setting(key) == "1250"
    jobs.put(mode="setting", key=key, unset=True, reason="back to default")
    assert store.get_setting(key) is None


@pytest.mark.parametrize("value", [0, -1, "1000", True, 2.5, 256_001])
def test_footprint_cpu_needs_a_positive_int(jobs: JobHandler, value: Any) -> None:
    with pytest.raises(BadInput, match="positive int of millicores"):
        _set(jobs, "claim.footprint.slice.cpu_milli", value)


@pytest.mark.parametrize(
    "key",
    [
        "claim.footprint.no_such_work.cpu_milli",
        "claim.footprint.slice.mem_mb",
        "claim.footprint.slice",
        "claim.footprint.",
    ],
)
def test_footprint_key_must_name_a_work_class_and_cpu(
    jobs: JobHandler, key: str
) -> None:
    with pytest.raises(BadInput, match="run_slice work class"):
        _set(jobs, key, 1000)


def _claim_row(body: str, first: str) -> list[str]:
    section = body.split("## claim.footprint.<work>.cpu_milli", 1)[1]
    for line in section.splitlines():
        cells = line.split("\t")
        if cells[0] == first:
            return cells
    raise AssertionError(f"no {first!r} row in the footprint section")


def test_claim_view_shows_the_footprint_rows(jobs: JobHandler, store: Store) -> None:
    from precis.workers.job_types.run_slice import _WORK_FOOTPRINTS

    body = jobs.get(id="/claim").body
    assert _claim_row(body, "slice")[:4] == ["slice", "-", "1000", "1000"]
    assert _claim_row(body, "land")[:4] == [
        "land",
        "-",
        str(_WORK_FOOTPRINTS["land"].cpu_milli),
        str(_WORK_FOOTPRINTS["land"].cpu_milli),
    ]

    _set(jobs, "claim.footprint.slice.cpu_milli", 750)
    body = jobs.get(id="/claim").body
    row = _claim_row(body, "slice")
    assert row[:4] == ["slice", "750", "1000", "750"]
    assert row[4] != "-"
    # A recognised family, not an "unrecognised claim.* keys" entry.
    assert "claim.footprint.slice.cpu_milli='750'" not in body


def test_claim_view_flags_an_unusable_footprint_override(
    jobs: JobHandler, store: Store
) -> None:
    store.set_setting("claim.footprint.slice.cpu_milli", "lots")
    row = _claim_row(jobs.get(id="/claim").body, "slice")
    assert row[:4] == ["slice", "lots", "1000", "1000 (override ignored)"]


def test_claim_view_footprint_rows_are_cpu_milli_only(
    jobs: JobHandler, store: Store
) -> None:
    """A claim.footprint.* key written around the setter that is not a
    ``<work>.cpu_milli`` leaf is no table row; it lists as unrecognised."""
    store.set_setting("claim.footprint.slice.mem_mb", "9999")
    body = jobs.get(id="/claim").body
    section = body.split("## claim.footprint.<work>.cpu_milli", 1)[1].split(
        "unrecognised claim.* keys", 1
    )[0]
    assert "9999" not in section
    assert [c for c in section.splitlines() if c.startswith("slice\t")] == [
        "slice\t-\t1000\t1000\t-\tclaim probe + spawn"
    ]
    assert "claim.footprint.slice.mem_mb='9999'" in body


def test_claim_view_marks_where_each_footprint_applies(jobs: JobHandler) -> None:
    body = jobs.get(id="/claim").body
    assert _claim_row(body, "dep_check")[5] == "detached launch only"
    assert _claim_row(body, "boundary_probe")[5] == "detached launch only"
    assert _claim_row(body, "land")[5] == "spawn"
