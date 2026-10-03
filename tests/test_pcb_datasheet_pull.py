"""Datasheet auto-pull: the trigger in ``put(kind='pcb')``, the
``datasheet_pull`` job, and the read side on ``get(kind='part')``.

Network is faked at two seams: ``datasheet_pull._jlc_client`` (the JLC API)
and ``datasheet_pull.safe_stream`` (the download). The one test that does NOT
fake the download — the private-address refusal — never leaves the process:
the SSRF backend refuses the loopback address before any connect. The Marker
ingest is faked (``datasheet_pull._ingest``); the real one is the inbox path's
``precis_add``.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.part import PartHandler
from precis.handlers.pcb import PcbHandler
from precis.pcb import catalog
from precis.pcb import datasheets as pcb_datasheets
from precis.pcb._http import VendorError
from precis.workers.job_types import datasheet_pull as dp

_PDF = b"%PDF-1.7\n" + b"datasheet body " * 50


def _design(*parts: str | None) -> dict[str, Any]:
    comps = []
    for i, part in enumerate(parts, start=1):
        c: dict[str, Any] = {
            "refdes": f"R{i}",
            "label": "res",
            "footprint": "0402",
            "x": 10.0 + 3 * i,
            "y": 10.0,
            "pins": [{"name": "1"}, {"name": "2"}],
        }
        if part is not None:
            c["part"] = part
        comps.append(c)
    return {"components": comps}


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _jobs(store, job_type: str = "datasheet_pull") -> list[tuple[str, str]]:
    with store.pool.connection() as conn:
        return [
            (r[0], r[1])
            for r in conn.execute(
                "SELECT meta->'params'->>'lcsc', meta->>'idem_key' FROM refs "
                "WHERE kind = 'job' AND meta->>'job_type' = %s ORDER BY ref_id",
                (job_type,),
            ).fetchall()
        ]


def _catalog_row(n: int, url: str | None) -> dict[str, Any]:
    row = catalog.normalize_jlcparts_row(
        {
            "lcsc": n,
            "manufacturer": "Acme",
            "mfr_part": f"ACME{n}",
            "description": "10k 0402 resistor",
            "basic": 1,
            "stock": 100,
            "package": "0402",
            "datasheet": url,
        }
    )
    assert row is not None
    return row


class _FakeJlc:
    def __init__(
        self, *, available: bool, row: Any = None, exc: Exception | None = None
    ):
        self.available = available
        self._row = row
        self._exc = exc
        self.calls: list[str] = []

    def component_info(self, lcsc: str) -> Any:
        self.calls.append(lcsc)
        if self._exc is not None:
            raise self._exc
        return self._row


@pytest.fixture
def no_jlc(monkeypatch):
    fake = _FakeJlc(available=False)
    monkeypatch.setattr(dp, "_jlc_client", lambda store: fake)
    return fake


class _FakeResp:
    def __init__(
        self, body: bytes, status: int = 200, url: str = "https://x.test/a.pdf"
    ):
        self.status_code = status
        self.headers: dict[str, str] = {}
        self.url = url
        self._body = body

    def iter_bytes(self, chunk_size: int = 0):
        for i in range(0, len(self._body), 1000):
            yield self._body[i : i + 1000]


@pytest.fixture
def serve(monkeypatch):
    """Make the download return ``body`` (and record the URLs asked for)."""
    asked: list[str] = []
    state: dict[str, Any] = {"body": _PDF, "status": 200}

    @contextmanager
    def fake_stream(client, method, url, /, **kw):
        asked.append(url)
        yield _FakeResp(state["body"], state["status"], url)

    monkeypatch.setattr(dp, "safe_stream", fake_stream)
    state["asked"] = asked
    return state


@pytest.fixture
def fake_ingest(monkeypatch, store, request):
    """Stand-in for the Marker ingest: mints a datasheet ref carrying the
    one ``pdf_sha256`` identifier the real ingest writes."""
    calls: list[str] = []
    write_chunks = not getattr(request, "param", False)

    def _ingest(st, path):
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        calls.append(sha)
        ref = st.insert_ref(kind="datasheet", slug=f"ds-{sha[:8]}", title="A datasheet")
        with st.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
                "VALUES ('pdf_sha256', %s, %s, 'test')",
                (sha, ref.id),
            )
            if write_chunks:
                conn.execute(
                    "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
                    "VALUES (%s, 0, 'paragraph', 'body', '{}'::jsonb)",
                    (ref.id,),
                )
            conn.commit()
        return ref.id, True

    monkeypatch.setattr(dp, "_ingest", _ingest)
    return calls


# ── trigger ──────────────────────────────────────────────────────────


def test_put_enqueues_one_pull_per_c_number_and_a_reput_none(pcb, store):
    resp = pcb.put(id="b1", args=_design("C25900", "C1525", "C25900"))
    assert "2 datasheet pull(s) queued" in resp.body
    assert _jobs(store) == [
        ("C1525", "datasheet_pull:C1525"),
        ("C25900", "datasheet_pull:C25900"),
    ]
    again = pcb.put(id="b1", args=_design("C25900", "C1525", "C25900"))
    assert "datasheet pull" not in again.body
    assert len(_jobs(store)) == 2


def test_component_without_c_number_enqueues_nothing(pcb, store):
    resp = pcb.put(id="b2", args=_design(None, None))
    assert "datasheet pull" not in resp.body
    assert _jobs(store) == []


def test_recorded_attempt_or_linked_datasheet_is_not_pending(
    pcb, store, no_jlc, fake_ingest, serve
):
    resp = pcb.put(id="b3", args=_design("C25900"))
    assert "1 datasheet pull(s) queued" in resp.body
    ref = store.get_ref(kind="pcb", id="b3")
    assert pcb_datasheets.pending_lcscs(store, ref.id) == ["C25900"]
    dp.pull(store, "C25900")  # no creds, no row: recorded no_url
    assert pcb_datasheets.pending_lcscs(store, ref.id) == []
    # force re-queues a FAILED pull only
    assert pcb_datasheets.pending_lcscs(store, ref.id, force=True) == ["C25900"]


def test_op_datasheets_force_requeues_failed_pull(pcb, store, no_jlc):
    pcb.put(id="b4", args=_design("C25900"))
    dp.pull(store, "C25900")
    with store.pool.connection() as conn:  # the first job is terminal now
        conn.execute(
            'UPDATE refs SET meta = meta || \'{"idem_key": "done"}\' '
            "WHERE kind = 'job' AND meta->>'idem_key' = 'datasheet_pull:C25900'"
        )
        conn.commit()
    plain = pcb.put(id="b4", args={"op": "datasheets"})
    assert "0 datasheet pull(s) queued" in plain.body
    forced = pcb.put(id="b4", args={"op": "datasheets", "force": True})
    assert "1 datasheet pull(s) queued" in forced.body


# ── the job ──────────────────────────────────────────────────────────


def test_two_c_numbers_with_the_same_bytes_share_one_datasheet(
    store, no_jlc, serve, fake_ingest
):
    store.parts_import(
        [
            _catalog_row(25900, "https://x.test/family.pdf"),
            _catalog_row(1525, "https://x.test/family-mirror.pdf"),
        ]
    )
    a = dp.pull(store, "C25900")
    b = dp.pull(store, "C1525")
    assert a["status"] == b["status"] == "ok"
    assert a["sha256"] == b["sha256"] == hashlib.sha256(_PDF).hexdigest()
    assert len(fake_ingest) == 1  # the second pull only linked
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT s.ref_id, ri.id_value FROM links l "
            "JOIN refs s ON s.ref_id = l.src_ref_id "
            "JOIN ref_identifiers ri ON ri.ref_id = l.dst_ref_id "
            "  AND ri.id_kind = 'lcsc' "
            "WHERE l.relation = 'datasheet-of' AND s.kind = 'datasheet' "
            "ORDER BY ri.id_value"
        ).fetchall()
        n_sha = conn.execute(
            "SELECT count(*) FROM ref_identifiers WHERE id_kind = 'pdf_sha256' "
            "AND ref_id = %s",
            (rows[0][0],),
        ).fetchone()[0]
    assert [r[1] for r in rows] == ["C1525", "C25900"]
    assert rows[0][0] == rows[1][0]
    assert n_sha == 1
    ds = store.get_ref(kind="datasheet", id=rows[0][0])
    assert ds.meta["source_url"] == "https://x.test/family.pdf"
    assert ds.meta["part_lcsc"] == "C25900"


def test_private_address_is_refused_by_safe_fetch(store, no_jlc, fake_ingest):
    store.parts_import([_catalog_row(25900, "http://127.0.0.1:9/ds.pdf")])
    rec = dp.pull(store, "C25900")
    assert rec["status"] == "failed"
    assert rec["reason"] == "fetch_refused"
    assert fake_ingest == []


def test_non_pdf_bytes_fail_not_pdf(store, no_jlc, serve, fake_ingest):
    serve["body"] = b"<html>sign in to download</html>"
    store.parts_import([_catalog_row(25900, "https://x.test/a.pdf")])
    rec = dp.pull(store, "C25900")
    assert (rec["status"], rec["reason"]) == ("failed", "not_pdf")
    assert fake_ingest == []


def test_http_error_and_size_cap(store, no_jlc, serve, fake_ingest, monkeypatch):
    store.parts_import([_catalog_row(25900, "https://x.test/a.pdf")])
    serve["status"] = 404
    assert dp.pull(store, "C25900")["reason"] == "http_404"
    serve["status"] = 200
    monkeypatch.setattr(dp, "MAX_BYTES", 100)
    assert dp.pull(store, "C25900")["reason"] == "too_large"


def test_no_credentials_and_no_parts_row_is_no_url(store, no_jlc):
    rec = dp.pull(store, "C99999")
    assert (rec["status"], rec["reason"]) == ("failed", "no_url")
    assert "no JLCPCB API credentials" in rec["detail"]
    ref = store.get_ref(kind="part", id="C99999")
    assert ref is not None and ref.meta["datasheet_pull"]["reason"] == "no_url"


def test_jlc_api_error_falls_back_to_the_stored_url(
    store, monkeypatch, serve, fake_ingest
):
    store.parts_import([_catalog_row(25900, "https://x.test/stored.pdf")])
    jlc = _FakeJlc(available=True, exc=VendorError("jlcpcb", "HTTP 500", status=500))
    monkeypatch.setattr(dp, "_jlc_client", lambda st: jlc)
    rec = dp.pull(store, "C25900")
    assert rec["status"] == "ok"
    assert serve["asked"] == ["https://x.test/stored.pdf"]
    assert jlc.calls == ["C25900"]


def test_jlc_api_error_without_a_url_is_its_own_reason(store, monkeypatch):
    jlc = _FakeJlc(available=True, exc=VendorError("jlcpcb", "HTTP 500", status=500))
    monkeypatch.setattr(dp, "_jlc_client", lambda st: jlc)
    rec = dp.pull(store, "C77777")
    assert (rec["status"], rec["reason"]) == ("failed", "jlc_api_error:500")
    jlc2 = _FakeJlc(available=True, exc=RuntimeError("boom"))
    monkeypatch.setattr(dp, "_jlc_client", lambda st: jlc2)
    assert dp.pull(store, "C77778")["reason"] == "jlc_api_error:RuntimeError"


def test_jlc_row_is_upserted_and_its_url_used(store, monkeypatch, serve, fake_ingest):
    row = _catalog_row(25900, "https://x.test/from-jlc.pdf")
    jlc = _FakeJlc(available=True, row=row)
    monkeypatch.setattr(dp, "_jlc_client", lambda st: jlc)
    rec = dp.pull(store, "C25900")
    assert rec["status"] == "ok"
    assert store.part_row("C25900")["datasheet_url"] == "https://x.test/from-jlc.pdf"
    assert serve["asked"] == ["https://x.test/from-jlc.pdf"]


# ── read side ────────────────────────────────────────────────────────


def test_get_part_shows_the_failure_reason_then_the_datasheet(
    store, no_jlc, serve, fake_ingest
):
    part = PartHandler(hub=Hub(store=store))
    store.parts_import([_catalog_row(25900, "https://x.test/a.pdf")])
    assert "not pulled yet" in part.get(id="C25900").body
    serve["body"] = b"not a pdf at all"
    dp.pull(store, "C25900")
    assert "pull failed, reason not_pdf" in part.get(id="C25900").body
    serve["body"] = _PDF
    dp.pull(store, "C25900")
    body = part.get(id="C25900").body
    assert "datasheet: ds-" in body


def test_bom_export_lists_datasheet_state(pcb, store, no_jlc, tmp_path):
    pcb.put(id="b5", args=_design("C25900"))
    out = pcb.get(id="b5", view="bom", args={"dir": str(tmp_path)})
    assert "C25900: queued" in out.body


@pytest.mark.parametrize("fake_ingest", [True], indirect=True)
def test_zero_chunk_ingest_is_ingest_empty_but_keeps_ref_and_link(
    store, no_jlc, serve, fake_ingest
):
    store.parts_import([_catalog_row(25900, "https://x.test/a.pdf")])
    rec = dp.pull(store, "C25900")  # this fake ingest writes no chunks
    assert (rec["status"], rec["reason"]) == ("failed", "ingest_empty")
    assert pcb_datasheets.datasheet_state(store, "C25900")["datasheet"] is not None
    assert (
        "reason ingest_empty" in PartHandler(hub=Hub(store=store)).get(id="C25900").body
    )


def test_uncatalogued_part_ref_is_not_called_removed(store, no_jlc):
    dp.pull(store, "C99999")
    body = PartHandler(hub=Hub(store=store)).get(id="C99999").body
    assert "not in the parts catalog (no catalogue row yet)" in body
    assert "no longer" not in body


def test_ingest_exception_is_recorded_not_retried(store, no_jlc, serve, monkeypatch):
    from precis.ingest import add as ingest_add

    store.parts_import([_catalog_row(25900, "https://x.test/a.pdf")])

    def boom(*a, **kw):
        raise ValueError("marker exploded")

    monkeypatch.setattr(ingest_add, "precis_add", boom)
    rec = dp.pull(store, "C25900")
    assert (rec["status"], rec["reason"]) == ("failed", "ingest_failed:ValueError")


def test_db_error_in_ingest_still_raises_for_retry(store, no_jlc, serve):
    import psycopg

    from precis.ingest import add as ingest_add

    store.parts_import([_catalog_row(25900, "https://x.test/a.pdf")])

    def down(*a, **kw):
        raise psycopg.OperationalError("db gone")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ingest_add, "precis_add", down)
        with pytest.raises(psycopg.OperationalError):
            dp.pull(store, "C25900")


def test_download_deadline_is_fetch_timeout(store, no_jlc, serve, monkeypatch):
    store.parts_import([_catalog_row(25900, "https://x.test/a.pdf")])
    monkeypatch.setattr(dp, "_DEADLINE_S", -1.0)
    assert dp.pull(store, "C25900")["reason"] == "fetch_timeout"


def test_pdf_magic_may_follow_leading_junk(store, no_jlc, serve, fake_ingest):
    store.parts_import([_catalog_row(25900, "https://x.test/a.pdf")])
    serve["body"] = b"\r\n\r\n" + _PDF
    assert dp.pull(store, "C25900")["status"] == "ok"
    serve["body"] = b"x" * 3000 + _PDF  # magic beyond the first 1024 bytes
    assert dp.pull(store, "C25900")["reason"] == "not_pdf"


def test_a_paper_with_the_same_bytes_is_not_the_datasheet(
    store, no_jlc, serve, monkeypatch
):
    paper = store.insert_ref(kind="paper", slug="some-paper", title="A paper")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('pdf_sha256', %s, %s, 'test')",
            (hashlib.sha256(_PDF).hexdigest(), paper.id),
        )
        conn.commit()
    # the probe ignores the paper, so the ingest runs; its own dedupe then
    # resolves to the paper, which must not become a part's datasheet
    monkeypatch.setattr(dp, "_ingest", lambda st, path: (paper.id, False))
    store.parts_import([_catalog_row(25900, "https://x.test/a.pdf")])
    rec = dp.pull(store, "C25900")
    assert rec["reason"] == "ingest_failed:sha_matches_paper"
    assert pcb_datasheets.datasheet_state(store, "C25900")["datasheet"] is None


def test_transient_failures_retry_after_a_day_permanent_ones_do_not(pcb, store):
    from datetime import UTC, datetime, timedelta

    pcb.put(id="b6", args=_design("C1111", "C2222", "C3333"))
    ref = store.get_ref(kind="pcb", id="b6")
    old = (datetime.now(UTC) - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.parts_import([_catalog_row(n, None) for n in (1111, 2222, 3333)])
    for lcsc, reason, at in (
        ("C1111", "http_503", old),
        ("C2222", "not_pdf", old),
        ("C3333", "jlc_api_error:500", fresh),
    ):
        pid = store.ensure_part_ref(lcsc)
        store.update_ref(
            pid,
            meta_patch={
                "datasheet_pull": {"status": "failed", "reason": reason, "at": at}
            },
        )
    assert pcb_datasheets.pending_lcscs(store, ref.id) == ["C1111"]
    assert pcb_datasheets.pending_lcscs(store, ref.id, force=True) == [
        "C1111",
        "C2222",
        "C3333",
    ]
    for r in ("http_429", "fetch_error:ReadError", "fetch_timeout", "jlc_api_error:X"):
        assert pcb_datasheets.is_transient(r)
    for r in ("no_url", "http_404", "too_large", "fetch_refused", "ingest_empty"):
        assert not pcb_datasheets.is_transient(r)
