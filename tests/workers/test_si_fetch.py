"""SI pass: claim bookkeeping, download + sidecar, lane ordering, put(mode)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from precis.errors import BadInput
from precis.ingest.fetch_sidecar import read_sidecar
from precis.ingest.si_discovery import HttpResult
from precis.workers import fetch_oa, si_fetch

DOI = "10.1021/acscatal.3c01963"


def _seed(store, slug="smith2023cat", doi: str | None = DOI) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title="Parent", meta={})
    if doi:
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
                "VALUES (%s, 'doi', %s, 'manual')",
                (ref.id, doi),
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


def _fetch_with_figshare(url: str) -> HttpResult:
    if url.startswith("https://api.figshare.com/v2/articles?resource_doi="):
        return HttpResult(
            200, json.dumps([{"id": 5, "doi": f"{DOI}.s001", "title": "SI"}])
        )
    if url.endswith("/articles/5/files"):
        return HttpResult(
            200,
            json.dumps(
                [
                    {
                        "name": "si_001.pdf",
                        "mimetype": "application/pdf",
                        "download_url": "https://ndownloader.figshare.com/files/9",
                    },
                    {
                        "name": "data.zip",
                        "mimetype": "application/zip",
                        "download_url": "https://ndownloader.figshare.com/files/10",
                    },
                ]
            ),
        )
    return HttpResult(404)


def _no_si(url: str) -> HttpResult:
    return HttpResult(404)


def test_found_si_downloads_sidecar_and_records(store, tmp_path, monkeypatch) -> None:
    pid = _seed(store)
    _flag(store, pid, "2026-10-03T10:00:00.000000Z")

    def fake_download(url: str, target: Path, *, extra_headers=None) -> int:
        target.write_bytes(b"%PDF-1.7 si")
        return 11

    monkeypatch.setattr(fetch_oa, "_download_pdf", fake_download)
    res = si_fetch.run_si_pass(store, tmp_path, fetch=_fetch_with_figshare)
    assert res["claimed"] == 1 and res["fetched"] == 1 and res["found"] == 2

    pdfs = sorted(tmp_path.glob("*.pdf"))
    assert [p.name for p in pdfs] == ["smith2023cat-si-01.pdf"]
    sc = read_sidecar(pdfs[0])
    assert sc is not None
    assert sc.role == "supplement" and sc.ref_id == pid
    assert sc.si is not None and sc.si["source"] == "figshare"
    assert sc.si["component_doi"] == f"{DOI}.s001"

    meta = _meta(store, pid)
    assert meta["si_found"] == 2 and meta["si_fetched"] == 1
    assert [s["filename"] for s in meta["si_skipped"]] == ["data.zip"]
    assert meta["si_checked_at"] > meta["si_fetch"]["requested_at"]
    with store.pool.connection() as conn:
        ev = conn.execute(
            "SELECT source, event FROM ref_events WHERE ref_id = %s", (pid,)
        ).fetchall()
    assert ("si_fetch", "si_found") in ev


def test_cloudflare_download_is_a_recorded_miss(store, tmp_path, monkeypatch) -> None:
    pid = _seed(store)
    _flag(store, pid, "2026-10-03T10:00:00.000000Z")

    def blocked(url: str, target: Path, *, extra_headers=None) -> int:
        req = httpx.Request("GET", url)
        raise httpx.HTTPStatusError(
            "403",
            request=req,
            response=httpx.Response(
                403, request=req, headers={"cf-mitigated": "challenge"}
            ),
        )

    monkeypatch.setattr(fetch_oa, "_download_pdf", blocked)
    si_fetch.run_si_pass(store, tmp_path, fetch=_fetch_with_figshare)
    meta = _meta(store, pid)
    assert meta["si_fetched"] == 0
    assert {
        "url": "https://ndownloader.figshare.com/files/9",
        "source": "figshare",
        "reason": "cloudflare_403",
    } in meta["si_misses"]
    assert list(tmp_path.glob("*.pdf")) == []
    with store.pool.connection() as conn:
        ev = conn.execute(
            "SELECT event FROM ref_events WHERE ref_id = %s AND source = 'si_fetch'",
            (pid,),
        ).fetchall()
    assert [e[0] for e in ev] == ["si_blocked"]


def test_no_si_records_one_check_and_is_not_reclaimed(store, tmp_path) -> None:
    pid = _seed(store)
    _flag(store, pid, "2026-10-03T10:00:00.000000Z")
    first = si_fetch.run_si_pass(store, tmp_path, fetch=_no_si)
    assert first["claimed"] == 1 and first["found"] == 0
    meta = _meta(store, pid)
    assert meta["si_found"] == 0 and meta["si_fetched"] == 0
    with store.pool.connection() as conn:
        n_ref = conn.execute(
            "SELECT count(*) FROM refs WHERE pdf_role = 'supplement'"
        ).fetchone()
        ev = conn.execute(
            "SELECT event FROM ref_events WHERE ref_id = %s AND source = 'si_fetch'",
            (pid,),
        ).fetchall()
    assert n_ref is not None and n_ref[0] == 0
    assert [e[0] for e in ev] == ["si_none"]

    # attention again: no re-check
    again = si_fetch.run_si_pass(store, tmp_path, fetch=_no_si)
    assert again["claimed"] == 0

    # a NEWER request re-claims it
    _flag(store, pid, "2999-01-01T00:00:00.000000Z")
    third = si_fetch.run_si_pass(store, tmp_path, fetch=_no_si)
    assert third["claimed"] == 1


def test_unflagged_paper_is_never_claimed(store, tmp_path) -> None:
    _seed(store)
    assert si_fetch.run_si_pass(store, tmp_path, fetch=_no_si)["claimed"] == 0


def test_si_pass_claims_flagged_parents_before_stubs(
    store, tmp_path, monkeypatch
) -> None:
    pid = _seed(store, slug="flagged2023", doi=DOI)
    _flag(store, pid, "2026-10-03T10:00:00.000000Z")
    order: list[str] = []

    real_si = si_fetch.run_si_pass

    def spy_si(*a: Any, **k: Any):
        order.append("si")
        return real_si(*a, fetch=_no_si, **{x: y for x, y in k.items() if x != "fetch"})

    def spy_claim(conn, *, limit: int):
        order.append("stubs")
        return []

    monkeypatch.setenv("PRECIS_OA_FETCH", "1")
    monkeypatch.setenv("PRECIS_WATCH_INBOX", str(tmp_path))
    monkeypatch.setattr(si_fetch, "run_si_pass", spy_si)
    monkeypatch.setattr(fetch_oa, "claim_stubs_to_fetch", spy_claim)
    fetch_oa.run_oa_fetch_pass(store, email="x@example.org")
    assert order == ["si", "stubs"]
    assert _meta(store, pid)["si_checked_at"]


def _handler(store):
    from precis.dispatch import Hub
    from precis.embedder import MockEmbedder
    from precis.handlers.paper import PaperHandler

    return PaperHandler(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))


def test_put_mode_fetch_si_sets_flag_and_dedupes(store) -> None:
    pid = _seed(store)
    handler = _handler(store)
    out = handler.put(id="smith2023cat", mode="fetch-si")
    assert "queued" in out.body
    first = _meta(store, pid)["si_fetch"]
    assert first["by"] == "agent" and first["requested_at"].endswith("Z")
    # pending request: second call is a no-op, requested_at unchanged
    again = handler.put(id="smith2023cat", mode="fetch-si")
    assert "already queued" in again.body
    assert _meta(store, pid)["si_fetch"] == first


def test_put_bad_mode_raises_bad_input(store) -> None:
    _seed(store)
    with pytest.raises(BadInput):
        _handler(store).put(id="smith2023cat", mode="bogus")
    with pytest.raises(BadInput):
        _handler(store).put(mode="fetch-si")  # id required
