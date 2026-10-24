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
    a = _held(store, "a", oa="W111")
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
