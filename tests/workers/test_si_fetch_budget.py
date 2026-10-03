"""SI pass budget, publish order, claim-stamp survival and lane isolation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from precis.ingest.fetch_sidecar import read_sidecar
from precis.ingest.si_discovery import HttpResult
from precis.workers import fetch_oa, si_fetch

DOI = "10.1021/acscatal.3c01963"


def _seed(store) -> int:
    ref = store.insert_ref(kind="paper", slug="smith2023cat", title="Parent", meta={})
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 'doi', %s, 'manual')",
            (ref.id, DOI),
        )
        conn.commit()
    return ref.id


def _flag(store, ref_id: int, requested_at: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object('si_fetch', "
            "jsonb_build_object('requested_at', %s::text, 'by', 'agent')) "
            "WHERE ref_id = %s",
            (requested_at, ref_id),
        )
        conn.commit()


def _meta(store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return row[0]


def _files(n: int):
    def fetch(url: str) -> HttpResult:
        if url.startswith("https://api.figshare.com/v2/articles?resource_doi="):
            return HttpResult(200, json.dumps([{"id": 5, "doi": f"{DOI}.s001"}]))
        if url.endswith("/articles/5/files"):
            return HttpResult(
                200,
                json.dumps(
                    [
                        {
                            "name": f"si_{i:03d}.pdf",
                            "mimetype": "application/pdf",
                            "download_url": f"https://ndownloader.figshare.com/files/{i}",
                        }
                        for i in range(n)
                    ]
                ),
            )
        return HttpResult(404)

    return fetch


def _ok_download(url: str, target: Path, *, extra_headers=None) -> int:
    target.write_bytes(b"%PDF-1.7 si")
    return 11


def test_per_parent_cap_records_cap_misses(store, tmp_path, monkeypatch) -> None:
    pid = _seed(store)
    _flag(store, pid, "2026-10-03T10:00:00.000000Z")
    monkeypatch.setattr(fetch_oa, "_download_pdf", _ok_download)
    res = si_fetch.run_si_pass(store, tmp_path, fetch=_files(10))
    assert si_fetch.MAX_SI_PDFS_PER_PARENT == 8
    assert res["fetched"] == 8
    meta = _meta(store, pid)
    assert meta["si_found"] == 10 and meta["si_fetched"] == 8
    assert [m["reason"] for m in meta["si_misses"] if m["source"] == "figshare"] == [
        "cap",
        "cap",
    ]
    assert len(list(tmp_path.glob("*.pdf"))) == 8


def test_deadline_records_deadline_misses(store, tmp_path, monkeypatch) -> None:
    pid = _seed(store)
    _flag(store, pid, "2026-10-03T10:00:00.000000Z")
    monkeypatch.setattr(fetch_oa, "_download_pdf", _ok_download)
    res = si_fetch.run_si_pass(store, tmp_path, fetch=_files(3), deadline_s=-1.0)
    assert res["fetched"] == 0
    meta = _meta(store, pid)
    assert meta["si_found"] == 3 and meta["si_fetched"] == 0
    assert [m["reason"] for m in meta["si_misses"] if m["source"] == "figshare"] == [
        "deadline"
    ] * 3
    assert list(tmp_path.glob("*.pdf")) == []


def test_sidecar_is_published_before_the_pdf(store, tmp_path, monkeypatch) -> None:
    pid = _seed(store)
    _flag(store, pid, "2026-10-03T10:00:00.000000Z")
    monkeypatch.setattr(fetch_oa, "_download_pdf", _ok_download)
    seen: list[tuple[str, bool]] = []
    real_replace = si_fetch.os.replace

    def spy(src, dst):
        name = Path(dst).name
        if name.endswith(".pdf"):
            # when the PDF becomes watcher-visible its sidecar already exists
            seen.append((name, Path(str(dst) + ".precis-fetch.json").exists()))
        real_replace(src, dst)

    monkeypatch.setattr(si_fetch.os, "replace", spy)
    si_fetch.run_si_pass(store, tmp_path, fetch=_files(1))
    assert seen == [("smith2023cat-si-01.pdf", True)]
    assert list((tmp_path / ".staging").iterdir()) == []
    sc = read_sidecar(tmp_path / "smith2023cat-si-01.pdf")
    assert sc is not None and sc.role == "supplement"


def test_request_during_fetch_survives(store, tmp_path, monkeypatch) -> None:
    pid = _seed(store)
    _flag(store, pid, "2026-10-03T10:00:00.000000Z")

    def download_and_rerequest(url: str, target: Path, *, extra_headers=None) -> int:
        _flag(store, pid, "2999-01-01T00:00:00.000000Z")  # request mid-fetch
        target.write_bytes(b"%PDF-1.7 si")
        return 11

    monkeypatch.setattr(fetch_oa, "_download_pdf", download_and_rerequest)
    si_fetch.run_si_pass(store, tmp_path, fetch=_files(1))
    meta = _meta(store, pid)
    # the final patch did not overwrite the claim stamp, so the newer request
    # is still pending and the next pass re-claims it
    assert meta["si_checked_at"] < meta["si_fetch"]["requested_at"]
    again = si_fetch.run_si_pass(store, tmp_path, fetch=lambda u: HttpResult(404))
    assert again["claimed"] == 1


def test_si_pass_exception_does_not_stop_stub_claim(
    store, tmp_path, monkeypatch
) -> None:
    claimed: list[int] = []

    def boom(*a: Any, **k: Any):
        raise RuntimeError("si pass exploded")

    def spy_claim(conn, *, limit: int):
        claimed.append(limit)
        return []

    monkeypatch.setenv("PRECIS_OA_FETCH", "1")
    monkeypatch.setenv("PRECIS_WATCH_INBOX", str(tmp_path))
    monkeypatch.setattr(si_fetch, "run_si_pass", boom)
    monkeypatch.setattr(fetch_oa, "claim_stubs_to_fetch", spy_claim)
    fetch_oa.run_oa_fetch_pass(store, email="x@example.org")
    assert claimed  # the stub claim still ran
