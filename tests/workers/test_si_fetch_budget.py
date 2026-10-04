"""SI pass budget, publish order, claim-stamp survival and lane isolation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from precis.ingest.fetch_sidecar import read_sidecar
from precis.ingest.si_discovery import HttpResult
from precis.workers import fetch_oa, si_fetch

DOI = "10.1021/acscatal.3c01963"


def _expire_after_discovery(monkeypatch) -> None:
    """Budget alive for the pass start + pre-discovery check, dead afterwards."""
    ticks = iter([0.0, 0.0])
    monkeypatch.setattr(si_fetch.time, "monotonic", lambda: next(ticks, 1e9))


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
    _expire_after_discovery(monkeypatch)
    res = si_fetch.run_si_pass(store, tmp_path, fetch=_files(3), deadline_s=10.0)
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


def _no_si(url: str) -> HttpResult:
    return HttpResult(404)


def test_deadline_miss_rearms_and_next_claim_reclaims(
    store, tmp_path, monkeypatch
) -> None:
    pid = _seed(store)
    _flag(store, pid, "2026-10-03T10:00:00.000000Z")
    monkeypatch.setattr(fetch_oa, "_download_pdf", _ok_download)
    _expire_after_discovery(monkeypatch)
    si_fetch.run_si_pass(store, tmp_path, fetch=_files(2), deadline_s=10.0)
    monkeypatch.undo()
    monkeypatch.setattr(fetch_oa, "_download_pdf", _ok_download)
    meta = _meta(store, pid)
    sf = meta["si_fetch"]
    assert sf["deadline_retries"] == 1 and sf["by"] == "agent"
    assert sf["requested_at"] > meta["si_checked_at"]
    # the miss is still recorded; the next pass (budget intact) fetches it
    again = si_fetch.run_si_pass(store, tmp_path, fetch=_files(2))
    assert again["claimed"] == 1 and again["fetched"] == 2


def test_deadline_event_payload_says_rearmed(store, tmp_path, monkeypatch) -> None:
    pid = _seed(store)
    _flag(store, pid, "2026-10-03T10:00:00.000000Z")
    monkeypatch.setattr(fetch_oa, "_download_pdf", _ok_download)
    _expire_after_discovery(monkeypatch)
    si_fetch.run_si_pass(store, tmp_path, fetch=_files(1), deadline_s=10.0)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT event, payload FROM ref_events WHERE ref_id = %s AND source = %s",
            (pid, si_fetch.SI_EVENT_SOURCE),
        ).fetchone()
    assert row is not None
    assert row[0] == "si_found"
    assert row[1]["rearmed"] is True and row[1]["deadline_retries"] == 1


def test_deadline_retries_cap_at_three(store, tmp_path, monkeypatch) -> None:
    pid = _seed(store)
    _flag(store, pid, "2026-10-03T10:00:00.000000Z")
    monkeypatch.setattr(fetch_oa, "_download_pdf", _ok_download)
    for n in (1, 2, 3):
        res = si_fetch.run_si_pass(store, tmp_path, fetch=_files(1), deadline_s=-1.0)
        assert res["claimed"] == 1
        assert _meta(store, pid)["si_fetch"]["deadline_retries"] == n
    res = si_fetch.run_si_pass(store, tmp_path, fetch=_files(1), deadline_s=-1.0)
    assert res["claimed"] == 1  # 4th claim: misses stand, no re-arm
    meta = _meta(store, pid)
    assert meta["si_fetch"]["deadline_retries"] == 3
    assert meta["si_checked_at"] > meta["si_fetch"]["requested_at"]
    assert si_fetch.run_si_pass(store, tmp_path, fetch=_files(1))["claimed"] == 0


def test_parent_claimed_after_deadline_runs_no_discovery(
    store, tmp_path, monkeypatch
) -> None:
    pid = _seed(store)
    _flag(store, pid, "2026-10-03T10:00:00.000000Z")
    calls: list[str] = []

    def spy(url: str) -> HttpResult:
        calls.append(url)
        return HttpResult(404)

    res = si_fetch.run_si_pass(store, tmp_path, fetch=spy, deadline_s=-1.0)
    assert calls == [] and res["claimed"] == 1
    meta = _meta(store, pid)
    assert meta["si_fetch"]["deadline_retries"] == 1
    assert meta["si_fetch"]["requested_at"] > meta["si_checked_at"]
    assert "si_misses" not in meta
    assert si_fetch.run_si_pass(store, tmp_path, fetch=_no_si)["claimed"] == 1


def test_complete_parent_without_deadline_miss_not_rearmed(
    store, tmp_path, monkeypatch
) -> None:
    pid = _seed(store)
    _flag(store, pid, "2026-10-03T10:00:00.000000Z")
    monkeypatch.setattr(fetch_oa, "_download_pdf", _ok_download)
    si_fetch.run_si_pass(store, tmp_path, fetch=_files(2))
    meta = _meta(store, pid)
    assert "deadline_retries" not in meta["si_fetch"]
    assert si_fetch.run_si_pass(store, tmp_path, fetch=_files(2))["claimed"] == 0


def test_late_claim_at_retry_cap_records_miss_and_event(store, tmp_path) -> None:
    pid = _seed(store)
    _flag(store, pid, "2026-10-03T10:00:00.000000Z")
    for _ in range(3):
        si_fetch.run_si_pass(store, tmp_path, fetch=_no_si, deadline_s=-1.0)
    assert "si_misses" not in _meta(store, pid)
    si_fetch.run_si_pass(store, tmp_path, fetch=_no_si, deadline_s=-1.0)
    meta = _meta(store, pid)
    assert meta["si_misses"] == [
        {"url": None, "source": "si_pass", "reason": "deadline"}
    ]
    assert meta["si_found"] == 0 and meta["si_fetched"] == 0
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT event, payload FROM ref_events WHERE ref_id = %s AND source = %s",
            (pid, si_fetch.SI_EVENT_SOURCE),
        ).fetchall()
    assert [r[0] for r in rows] == ["si_blocked"]
    assert rows[0][1]["deadline_retries"] == 3
    assert si_fetch.run_si_pass(store, tmp_path, fetch=_no_si)["claimed"] == 0


def _raising(name: str) -> Exception:
    return type(name, (Exception,), {})("boom")


def _flaky(exc_name: str):
    """Every discovery leg raises ``exc_name``."""

    def fetch(url: str) -> HttpResult:
        raise _raising(exc_name)

    return fetch


def _events(store, pid: int) -> list[Any]:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT event, payload FROM ref_events WHERE ref_id = %s AND source = %s",
            (pid, si_fetch.SI_EVENT_SOURCE),
        ).fetchall()


def test_connect_timeout_with_nothing_queued_rearms(store, tmp_path) -> None:
    pid = _seed(store)
    _flag(store, pid, "2026-10-03T10:00:00.000000Z")
    res = si_fetch.run_si_pass(store, tmp_path, fetch=_flaky("ConnectTimeout"))
    assert res["fetched"] == 0
    meta = _meta(store, pid)
    assert any(m["reason"] == "error:ConnectTimeout" for m in meta["si_misses"])
    assert meta["si_fetch"]["deadline_retries"] == 1
    assert meta["si_fetch"]["requested_at"] > meta["si_checked_at"]
    assert _events(store, pid)[0][1]["rearmed"] is True
    again = si_fetch.run_si_pass(store, tmp_path, fetch=_no_si)
    assert again["claimed"] == 1


def test_cloudflare_403_does_not_rearm(store, tmp_path) -> None:
    pid = _seed(store)
    _flag(store, pid, "2026-10-03T10:00:00.000000Z")

    def blocked(url: str) -> HttpResult:
        return HttpResult(403, headers={"cf-mitigated": "challenge"})

    si_fetch.run_si_pass(store, tmp_path, fetch=blocked)
    meta = _meta(store, pid)
    assert {m["reason"] for m in meta["si_misses"]} == {"cloudflare_403"}
    assert "deadline_retries" not in meta["si_fetch"]
    assert si_fetch.run_si_pass(store, tmp_path, fetch=blocked)["claimed"] == 0


def test_queued_pdf_with_other_leg_timeout_not_rearmed(
    store, tmp_path, monkeypatch
) -> None:
    pid = _seed(store)
    _flag(store, pid, "2026-10-03T10:00:00.000000Z")
    monkeypatch.setattr(fetch_oa, "_download_pdf", _ok_download)
    figshare = _files(1)

    def fetch(url: str) -> HttpResult:
        if "figshare" in url:
            return figshare(url)
        raise _raising("ConnectTimeout")

    res = si_fetch.run_si_pass(store, tmp_path, fetch=fetch)
    assert res["fetched"] == 1
    meta = _meta(store, pid)
    assert any(m["reason"] == "error:ConnectTimeout" for m in meta["si_misses"])
    assert "deadline_retries" not in meta["si_fetch"]
    assert si_fetch.run_si_pass(store, tmp_path, fetch=fetch)["claimed"] == 0


def test_retry_cap_holds_across_mixed_deadline_and_network(store, tmp_path) -> None:
    pid = _seed(store)
    _flag(store, pid, "2026-10-03T10:00:00.000000Z")
    flaky = _flaky("ReadTimeout")
    for n, deadline_s in ((1, -1.0), (2, 600.0), (3, -1.0)):
        res = si_fetch.run_si_pass(store, tmp_path, fetch=flaky, deadline_s=deadline_s)
        assert res["claimed"] == 1
        assert _meta(store, pid)["si_fetch"]["deadline_retries"] == n
    res = si_fetch.run_si_pass(store, tmp_path, fetch=flaky)
    assert res["claimed"] == 1  # 4th claim: misses stand
    assert _meta(store, pid)["si_fetch"]["deadline_retries"] == 3
    assert si_fetch.run_si_pass(store, tmp_path, fetch=flaky)["claimed"] == 0
