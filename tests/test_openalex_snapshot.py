"""OpenAlex snapshot loader (slice 1): filter, staging, checkpoint resume.

No network — the part file is a few synthetic work records gzipped in the
test and fed through the same ``_iter_gz_lines`` decode the streaming GET
uses. Covers the three load-bearing behaviours: only edges with a held end
are kept (OpenAlex-id match first, DOI fallback), a re-run skips a finished
file and inserts nothing, and a file cut mid-stream rolls back cleanly and
completes on the next run.
"""

from __future__ import annotations

import gzip
import io
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from precis.store import Store
from precis.workers import openalex_snapshot as oas

_SNAPSHOT = "2026-10-14"
_PART = oas.PartFile("data/jsonl/works/updated_date=2016-07-22/part_0000.gz", size=7523)


def _work(
    oa: str, doi: str | None = None, refs: tuple[str, ...] = ()
) -> dict[str, object]:
    rec: dict[str, object] = {
        "id": f"https://openalex.org/{oa}",
        "type": "article",
        "referenced_works": [f"https://openalex.org/{r}" for r in refs],
    }
    if doi:
        rec["doi"] = f"https://doi.org/{doi}"
    return rec


def _gz_bytes(works: list[dict[str, object]]) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        for w in works:
            gz.write(json.dumps(w).encode("utf-8") + b"\n")
    return buf.getvalue()


def _supply(payload: bytes):
    def lines_for(part: oas.PartFile) -> Iterator[str]:
        return oas._iter_gz_lines(iter([payload]))

    return lines_for


def _held(
    store: Store, slug: str, *, oa: str | None = None, doi: str | None = None
) -> int:
    """A paper ref carrying the given external identifier(s)."""
    ref = store.insert_ref(kind="paper", slug=slug, title="T", meta={})
    with store.pool.connection() as conn:
        for kind, value in (("openalex", oa), ("doi", doi)):
            if value is not None:
                conn.execute(
                    "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
                    "VALUES (%s, %s, %s, 'test')",
                    (ref.id, kind, value),
                )
    return ref.id


def _edges(store: Store) -> set[tuple[str, str, int | None, int | None]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_oa_id, dst_oa_id, src_ref_id, dst_ref_id "
            "FROM openalex_cite_edges"
        ).fetchall()
    return {(r[0], r[1], r[2], r[3]) for r in rows}


def _checkpoint(store: Store, s3_key: str) -> tuple | None:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT status, records_seen, edges_kept, error "
            "FROM openalex_snapshot_files WHERE s3_key = %s",
            (s3_key,),
        ).fetchone()


def _require_checkpoint(store: Store, s3_key: str) -> tuple:
    row = _checkpoint(store, s3_key)
    assert row is not None
    return row


def _works() -> list[dict[str, object]]:
    return [
        # held by openalex id — both outbound edges kept, one dst held too
        _work("W111", doi="10.9/Held", refs=("W999", "W333")),
        # held only by DOI (record DOI mixed-case, stored lowercase)
        _work("W222", doi="10.1/X", refs=("W999",)),
        # unheld src, held dst — kept for the dst end
        _work("W444", refs=("W333",)),
        # neither end held — dropped
        _work("W555", refs=("W999",)),
        # no referenced_works at all
        _work("W666"),
    ]


def _expected(a: int, b: int, c: int) -> set[tuple[str, str, int | None, int | None]]:
    return {
        ("W111", "W999", a, None),
        ("W111", "W333", a, c),
        ("W222", "W999", b, None),
        ("W444", "W333", None, c),
    }


def _seed(store: Store) -> tuple[int, int, int, oas.HeldIndex]:
    # `w111` is the ref_identifiers canonical form (lowercase — every
    # mixin write normalises, gr514198); `W333` is a legacy mixed-case
    # row left by a raw-SQL write. Both must resolve against the API's
    # W… records — the index lowers both sides.
    a = _held(store, "a", oa="w111")
    b = _held(store, "b", doi="10.1/x")
    c = _held(store, "c", oa="W333")
    return a, b, c, oas.load_held_index(store)


def test_filter_keeps_only_held_edges(store: Store) -> None:
    a, b, c, held = _seed(store)
    summary = oas.run_load(
        store,
        [_PART],
        snapshot_date=_SNAPSHOT,
        held=held,
        lines_for=_supply(_gz_bytes(_works())),
    )
    assert summary.files_done == 1
    assert summary.records_seen == 5
    assert summary.edges_kept == 4
    assert _edges(store) == _expected(a, b, c)

    with store.pool.connection() as conn:
        dois: dict[str, str | None] = dict(
            conn.execute(
                "SELECT src_oa_id, src_doi FROM openalex_cite_edges"
            ).fetchall()
        )
    # record DOIs normalise to the stored lowercase/prefix-free form
    assert dois["W111"] == "10.9/held"
    assert dois["W222"] == "10.1/x"
    assert dois["W444"] is None

    status, records, edges, error = _require_checkpoint(store, _PART.s3_key)
    assert (status, records, edges, error) == ("done", 5, 4, None)


def test_dry_run_counts_and_writes_nothing(store: Store) -> None:
    a, b, c, held = _seed(store)
    summary = oas.run_load(
        store,
        [_PART],
        snapshot_date=_SNAPSHOT,
        held=held,
        lines_for=_supply(_gz_bytes(_works())),
        dry_run=True,
    )
    assert summary.records_seen == 5
    assert summary.edges_kept == 4
    assert _edges(store) == set()
    assert _checkpoint(store, _PART.s3_key) is None


def test_rerun_skips_and_inserts_nothing(store: Store) -> None:
    a, b, c, held = _seed(store)
    payload = _gz_bytes(_works())
    for expected_status in ("done", "skipped"):
        summary = oas.run_load(
            store,
            [_PART],
            snapshot_date=_SNAPSHOT,
            held=held,
            lines_for=_supply(payload),
        )
        assert summary.outcomes[0].status == expected_status
    assert _edges(store) == _expected(a, b, c)


def test_interrupted_file_resumes_clean(store: Store) -> None:
    a, b, c, held = _seed(store)
    payload = _gz_bytes(_works())
    calls = {"n": 0}

    def flaky(part: oas.PartFile) -> Iterator[str]:
        calls["n"] += 1
        for i, line in enumerate(oas._iter_gz_lines(iter([payload]))):
            if calls["n"] == 1 and i == 2:
                raise RuntimeError("stream cut mid-file")
            yield line

    first = oas.run_load(
        store,
        [_PART],
        snapshot_date=_SNAPSHOT,
        held=held,
        lines_for=flaky,
    )
    assert first.outcomes[0].status == "failed"
    # the per-file transaction rolled back: no partial edges survive
    assert _edges(store) == set()
    status, records, _edges_n, error = _require_checkpoint(store, _PART.s3_key)
    assert status == "failed" and records == 2 and error

    second = oas.run_load(
        store,
        [_PART],
        snapshot_date=_SNAPSHOT,
        held=held,
        lines_for=flaky,
    )
    assert second.outcomes[0].status == "done"
    assert _edges(store) == _expected(a, b, c)
    assert _require_checkpoint(store, _PART.s3_key)[:3] == ("done", 5, 4)


# ── Slice 2: staged edges → cites links ──────────────────────────────────


def _stage(store: Store, held: oas.HeldIndex) -> None:
    oas.run_load(
        store,
        [_PART],
        snapshot_date=_SNAPSHOT,
        held=held,
        lines_for=_supply(_gz_bytes(_works())),
    )


def _cites_links(store: Store) -> list[tuple[int, int, dict]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_ref_id, dst_ref_id, meta FROM links "
            "WHERE relation = 'cites' ORDER BY src_ref_id, dst_ref_id"
        ).fetchall()
    return [(int(r[0]), int(r[1]), dict(r[2])) for r in rows]


def _minted(store: Store) -> dict[tuple[str, str], bool]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_oa_id, dst_oa_id, minted_at IS NOT NULL "
            "FROM openalex_cite_edges"
        ).fetchall()
    return {(r[0], r[1]): bool(r[2]) for r in rows}


def test_mint_links_both_held_edges(store: Store) -> None:
    a, _b, c, held = _seed(store)
    _stage(store, held)

    summary = oas.mint_staged_cites(store)
    # exactly one staging row is both-held: W111 → W333 (a → c)
    assert summary.rows == 1 and summary.links == 1 and summary.papers == 1
    links = _cites_links(store)
    assert [(s, d) for s, d, _m in links] == [(a, c)]
    meta = links[0][2]
    assert meta["source"] == "openalex_snapshot"
    assert meta["openalex_edge"] == "W111>W333"
    # minted_at stamps consumption: the both-held row is stamped, the
    # one-end-held rows are untouched (they can't mint — only BOTH ref
    # ids set becomes a cites link).
    assert _minted(store) == {
        ("W111", "W999"): False,
        ("W111", "W333"): True,
        ("W222", "W999"): False,
        ("W444", "W333"): False,
    }


def test_mint_cited_by_is_the_read_reverse(store: Store) -> None:
    a, _b, c, held = _seed(store)
    _stage(store, held)
    oas.mint_staged_cites(store)

    # ``cited-by`` is the read-time inverse: asking for c's outbound
    # ``cited-by`` edges returns the ``cites`` rows landing on c.
    inbound = store.links_for(c, direction="out", relation="cited-by")
    assert [(l.src_ref_id, l.dst_ref_id) for l in inbound] == [(a, c)]


def test_mint_rerun_mints_nothing_new(store: Store) -> None:
    _a, _b, _c, held = _seed(store)
    _stage(store, held)
    first = oas.mint_staged_cites(store)
    second = oas.mint_staged_cites(store)
    assert first.links == 1
    assert second.rows == 0 and second.links == 0 and second.papers == 0
    assert len(_cites_links(store)) == 1


def test_mint_outbound_count_matches_staged_src_rows(store: Store) -> None:
    """Slice-2 acceptance: a held paper's outbound count equals its
    both-held staging rows (every src_ref_id edge whose dst is held too)."""
    a, _b, c, held = _seed(store)
    # a second both-held edge: W333 cites W111 back (c → a).
    works = _works() + [_work("W333", refs=("W111",))]
    oas.run_load(
        store,
        [_PART],
        snapshot_date=_SNAPSHOT,
        held=held,
        lines_for=_supply(_gz_bytes(works)),
    )
    oas.mint_staged_cites(store)

    assert {(s, d) for s, d, _m in _cites_links(store)} == {(a, c), (c, a)}
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM openalex_cite_edges "
            "WHERE src_ref_id = %s AND dst_ref_id IS NOT NULL",
            (a,),
        ).fetchone()
    assert row is not None
    staged_out = int(row[0])
    a_out = [l for l in store.links_for(a, direction="out", relation="cites")]
    assert len(a_out) == staged_out == 1


def test_mint_limit_bounds_rows_per_call(store: Store) -> None:
    a, _b, c, held = _seed(store)
    works = _works() + [_work("W333", refs=("W111",))]
    oas.run_load(
        store,
        [_PART],
        snapshot_date=_SNAPSHOT,
        held=held,
        lines_for=_supply(_gz_bytes(works)),
    )
    first = oas.mint_staged_cites(store, limit=1)
    assert first.rows == 1
    rest = oas.mint_staged_cites(store)
    assert rest.rows == 1
    assert {(s, d) for s, d, _m in _cites_links(store)} == {(a, c), (c, a)}


def test_mint_resolves_late_held_refs(store: Store) -> None:
    """A paper held AFTER its edges staged still mints: the insert's
    ON CONFLICT DO NOTHING never upgrades a NULL ref id, so each mint
    first re-resolves — citing side by OpenAlex id, then DOI."""
    _a, _b, c, held = _seed(store)
    works = _works() + [_work("W777", doi="10.5/Late", refs=("W333",))]
    oas.run_load(
        store,
        [_PART],
        snapshot_date=_SNAPSHOT,
        held=held,
        lines_for=_supply(_gz_bytes(works)),
    )
    assert ("W444", "W333", None, c) in _edges(store)
    assert ("W777", "W333", None, c) in _edges(store)

    # The citers arrive later — W444 by openalex id, W777 by doi only.
    d = _held(store, "d", oa="w444")
    e = _held(store, "e", doi="10.5/late")
    summary = oas.mint_staged_cites(store)
    assert summary.resolved == 2  # one fill per resolution path
    assert {(s, dd) for s, dd, _m in _cites_links(store)} >= {(d, c), (e, c)}
    minted = _minted(store)
    assert minted[("W444", "W333")] and minted[("W777", "W333")]


def test_mint_stamps_retired_citer_without_linking(store: Store) -> None:
    """A citing ref retired between staging and minting: its rows stamp
    minted_at (the scan converges) but write no cites link — and the
    held index never resolves a retired ref at load time either."""
    a, _b, _c, held = _seed(store)
    _stage(store, held)
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET retired_at = now() WHERE ref_id = %s", (a,))

    assert a not in oas.load_held_index(store).by_oa.values()
    summary = oas.mint_staged_cites(store)
    assert summary.rows == 1 and summary.links == 0 and summary.papers == 1
    assert _cites_links(store) == []
    assert _minted(store)[("W111", "W333")] is True


def test_resolve_covers_every_held_key_not_a_front_window(store: Store) -> None:
    """R1: the resolve drives from the held identifiers, not the staging
    NULL set — >_RESOLVE_BATCH distinct unresolvable dst keys cannot mask
    a late-held one."""
    a, _b, _c, held = _seed(store)
    dsts = tuple(f"W9{n:07d}" for n in range(oas._RESOLVE_BATCH + 100))
    works = [_work("W111", refs=(*dsts, "W99999999"))]
    oas.run_load(
        store,
        [_PART],
        snapshot_date=_SNAPSHOT,
        held=held,
        lines_for=_supply(_gz_bytes(works)),
    )
    late = _held(store, "late", oa="w99999999")
    summary = oas.mint_staged_cites(store)
    assert summary.resolved == 1
    assert ("W111", "W99999999", a, late) in _edges(store)
    assert (a, late) in {(s, d) for s, d, _m in _cites_links(store)}


# ── Slice 3: release refresh, deletion log, the ~30d timer ───────────────

_SNAPSHOT_2 = "2027-01-13"
_NEW_PART = oas.PartFile(
    "data/jsonl/works/updated_date=2027-01-13/part_0000.gz", size=9000
)


def _refresh(store: Store, files: list[oas.PartFile], payload: bytes):
    """A release-diff refresh with the network seams replaced by fixtures."""
    return oas.refresh_snapshot(
        store,
        fetch_date=lambda: _SNAPSHOT_2,
        list_files=lambda: iter(files),
        lines_for=_supply(payload),
        deletion_lines=lambda: iter(
            ["work_id,deleted_date", "https://openalex.org/W444,2027-01-13"]
        ),
    )


def test_plan_refresh_skips_done_files_from_any_snapshot(store: Store) -> None:
    _seed(store)
    _stage(store, oas.load_held_index(store))  # _PART done under _SNAPSHOT
    changed = oas.PartFile(_PART.s3_key, size=999)  # same key, new size
    plan = oas.plan_refresh(store, iter([_PART, changed, _NEW_PART]))
    # unchanged _PART skips (checkpoint matched under the OLD snapshot);
    # a size-changed same-key file and the new release's file both plan.
    assert [p.s3_key for p in plan] == [_PART.s3_key, _NEW_PART.s3_key]


def test_refresh_processes_only_new_files_deletes_and_mints(
    store: Store,
) -> None:
    a, _b, c, held = _seed(store)
    _stage(store, held)
    oas.mint_staged_cites(store)

    # New release: the old part lists unchanged, the new partition carries
    # one record adding edge W333 → W111 (c → a); W444 is deleted.
    works = [_work("W333", refs=("W111",))]
    summary = _refresh(store, [_PART, _NEW_PART], _gz_bytes(works))

    assert summary is not None and summary.snapshot_date == _SNAPSHOT_2
    assert summary.planned == 1  # the unchanged _PART never re-streamed
    assert summary.load.records_seen == 1 and summary.load.edges_kept == 1
    # deletion log src-side only: W444's outbound row is dropped; the
    # minted a→c link survives untouched (retires via the link path).
    assert summary.deleted == 1
    assert summary.minted.rows == 1 and summary.minted.links == 1
    assert {(s, d) for s, d, _m in _cites_links(store)} == {(a, c), (c, a)}
    staged = {(s, d) for s, d, _sr, _dr in _edges(store)}
    assert ("W444", "W333") not in staged
    assert ("W333", "W111") in staged

    # Same release a second time: the manifest diff is the whole cost —
    # nothing re-plans, nothing re-reads.
    assert _refresh(store, [_PART, _NEW_PART], _gz_bytes(works)) is None


def test_refresh_dry_run_reports_plan_without_streaming(store: Store) -> None:
    """``--refresh --dry-run``: manifest + listing only — the plan's file
    count and bytes are reported with no streams and no writes."""
    _seed(store)
    del_obj = oas.PartFile(oas.DELETED_KEY, size=42)
    calls = {"streamed": 0, "deleted": 0}

    def lines_for(_part: oas.PartFile) -> Iterator[str]:
        calls["streamed"] += 1
        yield "{}\n"

    def deletion_lines() -> Iterator[str]:
        calls["deleted"] += 1
        return iter([])

    summary = oas.refresh_snapshot(
        store,
        fetch_date=lambda: _SNAPSHOT_2,
        list_files=lambda: iter([_PART, _NEW_PART, del_obj]),
        lines_for=lines_for,
        deletion_lines=deletion_lines,
        dry_run=True,
    )
    assert summary is not None
    assert summary.planned == 2
    assert summary.planned_bytes == _PART.size + _NEW_PART.size
    assert calls == {"streamed": 0, "deleted": 0}
    assert _edges(store) == set() and _checkpoint(store, _PART.s3_key) is None
    assert store.get_setting("openalex_snapshot:last_release") is None


def test_refresh_noop_when_release_unchanged(store: Store) -> None:
    _seed(store)
    store.set_setting("openalex_snapshot:last_release", _SNAPSHOT_2)
    calls = {"listed": 0}

    def count_files():
        calls["listed"] += 1
        return iter([])

    summary = oas.refresh_snapshot(
        store,
        fetch_date=lambda: _SNAPSHOT_2,
        list_files=count_files,
        lines_for=_supply(b""),
        deletion_lines=lambda: iter([]),
    )
    assert summary is None
    assert calls["listed"] == 0  # never even listed the bucket


def test_deletion_log_drops_src_side_only(store: Store) -> None:
    _a, _b, _c, held = _seed(store)
    _stage(store, held)
    result = oas.apply_deletion_log(
        store,
        iter(
            [
                "work_id,deleted_date",
                "W111,2027-01-13",  # drops both W111-sourced rows
                "https://openalex.org/W333,2027-01-13",  # dst only: no-op
                "W9999999,2027-01-13",  # unknown id: no-op
            ]
        ),
    )
    assert result.ids == 3 and result.deleted == 2
    assert {(s, d) for s, d, _sr, _dr in _edges(store)} == {
        ("W222", "W999"),
        ("W444", "W333"),
    }


def test_refresh_pass_throttles_via_app_state(store: Store) -> None:
    _seed(store)
    calls = {"n": 0}

    def fake_refresh() -> None:
        calls["n"] += 1
        return None

    first = oas.run_snapshot_refresh_pass(store, refresh=fake_refresh)
    assert first["release"] == "unchanged" and calls["n"] == 1
    # last_run stamped: a second tick the same hour is a pure no-op.
    second = oas.run_snapshot_refresh_pass(store, refresh=fake_refresh)
    assert second == {"claimed": 0, "ok": 0, "failed": 0}
    assert calls["n"] == 1


# ── Review hardening: retries, checkpoints, and the tx window ────────────


def test_refresh_failed_file_retries_only_that_file(store: Store) -> None:
    """A failed part leaves last_release unset; the next refresh re-plans
    and the done checkpoints confine the retry to the failed file."""
    _a, _b, _c, held = _seed(store)
    good = oas.PartFile(
        "data/jsonl/works/updated_date=2027-01-13/part_0001.gz", size=100
    )
    bad = oas.PartFile(
        "data/jsonl/works/updated_date=2027-01-13/part_0002.gz", size=100
    )
    del_obj = oas.PartFile(oas.DELETED_KEY, size=42)
    payload = _gz_bytes([_work("W333", refs=("W111",))])
    streamed: list[str] = []
    bad_ok = {"v": False}

    def lines_for(part: oas.PartFile) -> Iterator[str]:
        streamed.append(part.s3_key)
        if part.s3_key == bad.s3_key and not bad_ok["v"]:
            yield "{}\n"  # one decoded record, then the stream dies
            raise RuntimeError("s3 reset mid-stream")
        yield from oas._iter_gz_lines(iter([payload]))

    def refresh() -> oas.RefreshSummary | None:
        return oas.refresh_snapshot(
            store,
            fetch_date=lambda: _SNAPSHOT_2,
            list_files=lambda: iter([good, bad, del_obj]),
            lines_for=lines_for,
            deletion_lines=lambda: iter(["work_id,deleted_date"]),
        )

    first = refresh()
    assert first is not None and first.load.files_failed == 1
    assert store.get_setting("openalex_snapshot:last_release") is None

    bad_ok["v"] = True
    streamed.clear()
    second = refresh()
    assert second is not None and second.load.files_failed == 0
    # only the failed file re-streamed — `good` is done, the deletion log
    # is checkpointed by size, and minting is idempotent.
    assert streamed == [bad.s3_key]
    assert store.get_setting("openalex_snapshot:last_release") == _SNAPSHOT_2


def test_refresh_checkpoints_deletion_log_by_size(store: Store) -> None:
    """An unchanged-size deletion log is never re-streamed; a grown one is."""
    _a, _b, _c, held = _seed(store)
    _stage(store, held)  # stages the W444-src edge under _SNAPSHOT
    calls = {"n": 0}

    def deletion_lines() -> Iterator[str]:
        calls["n"] += 1
        return iter(["work_id,deleted_date", "W444,2027-01-13"])

    def refresh(date: str, del_size: int) -> oas.RefreshSummary | None:
        return oas.refresh_snapshot(
            store,
            fetch_date=lambda: date,
            list_files=lambda: iter([oas.PartFile(oas.DELETED_KEY, size=del_size)]),
            lines_for=_supply(b""),
            deletion_lines=deletion_lines,
        )

    first = refresh(_SNAPSHOT_2, 200)
    assert first is not None and first.deleted == 1 and calls["n"] == 1

    # Release moved but the log's size didn't: the apply is skipped.
    second = refresh("2027-04-15", 200)
    assert second is not None and second.deleted == 0 and calls["n"] == 1

    # A grown cumulative log streams again (the apply itself is idempotent).
    third = refresh("2027-07-08", 250)
    assert third is not None and calls["n"] == 2


def test_refresh_pass_stamps_error_backoff_on_raise(store: Store) -> None:
    """A tick that raises backs off ~6 h via last_error instead of
    re-pulling the listing + deletion log on every hourly idle tick."""
    calls = {"n": 0}

    def boom() -> None:
        calls["n"] += 1
        raise RuntimeError("pgbouncer restart")

    with pytest.raises(RuntimeError):
        oas.run_snapshot_refresh_pass(store, refresh=boom)
    assert calls["n"] == 1
    assert store.get_setting("openalex_snapshot:last_error")
    assert store.get_setting("openalex_snapshot:last_run") is None

    # Inside the backoff window the tick never calls refresh.
    out = oas.run_snapshot_refresh_pass(store, refresh=boom)
    assert out == {"claimed": 0, "ok": 0, "failed": 0}
    assert calls["n"] == 1

    # Past the window it retries; a completed tick clears the marker and
    # stamps last_run, restoring the ~30d cadence.
    store.set_setting(
        "openalex_snapshot:last_error",
        (datetime.now(UTC) - timedelta(hours=7)).isoformat(),
    )
    out = oas.run_snapshot_refresh_pass(store, refresh=lambda: None)
    assert out["release"] == "unchanged"
    assert store.get_setting("openalex_snapshot:last_error") == ""
    assert store.get_setting("openalex_snapshot:last_run")


def test_refresh_pass_backs_off_failed_files_then_gives_up(store: Store) -> None:
    """R2: a deterministically-failed file gets the ~6h error backoff plus
    a failed_ticks counter; after the give-up bound the lane stamps
    last_run and parks the file until the next cadence."""
    _a, _b, _c, _held = _seed(store)
    bad = oas.PartFile(
        "data/jsonl/works/updated_date=2027-01-13/part_9999.gz", size=100
    )
    calls = {"n": 0}

    def lines_for(_part: oas.PartFile) -> Iterator[str]:
        yield "{}\n"
        raise RuntimeError("corrupt object")

    def refresh() -> oas.RefreshSummary | None:
        calls["n"] += 1
        return oas.refresh_snapshot(
            store,
            fetch_date=lambda: _SNAPSHOT_2,
            list_files=lambda: iter([bad]),
            lines_for=lines_for,
            deletion_lines=lambda: iter([]),
        )

    def age_backoff() -> None:
        store.set_setting(
            "openalex_snapshot:last_error",
            (datetime.now(UTC) - timedelta(hours=7)).isoformat(),
        )

    out = oas.run_snapshot_refresh_pass(store, refresh=refresh)
    assert out["failed"] == 1 and calls["n"] == 1
    assert store.get_setting("openalex_snapshot:failed_ticks") == "1"
    assert store.get_setting("openalex_snapshot:last_run") is None
    # inside the 6h backoff the same-hour tick is a pure no-op
    assert oas.run_snapshot_refresh_pass(store, refresh=refresh) == {
        "claimed": 0,
        "ok": 0,
        "failed": 0,
    }
    assert calls["n"] == 1

    for expect in ("2", "3"):
        age_backoff()
        oas.run_snapshot_refresh_pass(store, refresh=refresh)
        assert store.get_setting("openalex_snapshot:failed_ticks") == expect

    # the 4th consecutive failure gives up: last_run stamped, counter reset
    age_backoff()
    out = oas.run_snapshot_refresh_pass(store, refresh=refresh)
    assert out["failed"] == 1 and calls["n"] == 4
    assert store.get_setting("openalex_snapshot:failed_ticks") == "0"
    assert store.get_setting("openalex_snapshot:last_run")
    assert oas.run_snapshot_refresh_pass(store, refresh=refresh) == {
        "claimed": 0,
        "ok": 0,
        "failed": 0,
    }
    assert calls["n"] == 4


def test_process_file_holds_no_connection_while_streaming(store: Store) -> None:
    """The part stream runs with no pool connection checked out — a ~1 GB
    file can take >300 s to pull, and prod agent_rw kills sessions
    idle-in-transaction at 300 s."""
    _a, _b, _c, held = _seed(store)
    open_conns = {"n": 0}
    real_connection = store.pool.connection

    @contextmanager
    def spy() -> Iterator[object]:
        open_conns["n"] += 1
        try:
            with real_connection() as conn:
                yield conn
        finally:
            open_conns["n"] -= 1

    peak = {"n": 0}
    payload = _gz_bytes(_works())

    def lines_for(_part: oas.PartFile) -> Iterator[str]:
        for line in oas._iter_gz_lines(iter([payload])):
            peak["n"] = max(peak["n"], open_conns["n"])
            yield line

    with patch.object(store.pool, "connection", spy):
        summary = oas.run_load(
            store,
            [_PART],
            snapshot_date=_SNAPSHOT,
            held=held,
            lines_for=lines_for,
        )
    assert summary.files_done == 1
    assert peak["n"] == 0
