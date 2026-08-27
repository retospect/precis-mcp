"""Parts catalog — normalizer (pure) + import /
turnover / selector / footprint cache / auto-stamp / sqlite refresh (DB) /
staging+swap bulk reload / the `precis pcb refresh-parts` CLI verb / the
`parts_refresh` worker pass (gr264357: wiring the catalog ingest at all).
"""

from __future__ import annotations

import sqlite3

import pytest

from precis.cli import pcb as pcb_cli
from precis.dispatch import Hub
from precis.handlers.part import PartHandler
from precis.handlers.pcb import PcbHandler
from precis.pcb import catalog, footprint


# ── pure normalizer ──────────────────────────────────────────────────
def test_normalize_maps_jlcparts_row():
    raw = {
        "lcsc": 25804,
        "manufacturer": "Samsung",
        "mfr": "CL05B104KO5NNNC",
        "description": "100nF 16V X7R 0402",
        "basic": 1,
        "stock": 500000,
        "package": "0402",
        "datasheet": "http://x/ds.pdf",
        "price": [{"qFrom": 1, "qTo": 100, "price": 0.0023}],
        "extra": {"attributes": {"Capacitance": "100nF"}},
    }
    n = catalog.normalize_jlcparts_row(raw)
    assert n is not None
    assert n["lcsc"] == "C25804"  # C-prefixed
    assert n["mfr_part"] == "CL05B104KO5NNNC" and n["mfr"] == "Samsung"
    assert n["jlcpcb_assemblable"] is True and n["basic"] is True
    assert n["stock"] == 500000 and n["package"] == "0402"


def test_normalize_lcsc_forms_and_missing():
    n1 = catalog.normalize_jlcparts_row({"lcsc": "C7"})
    assert n1 is not None
    assert n1["lcsc"] == "C7"
    n2 = catalog.normalize_jlcparts_row({"lcsc": 7})
    assert n2 is not None
    assert n2["lcsc"] == "C7"
    assert catalog.normalize_jlcparts_row({"description": "x"}) is None  # no C-number


def test_min_unit_price():
    assert (
        catalog.min_unit_price([{"price": 0.01}, {"price": 0.004}, {"price": 0.007}])
        == 0.004
    )
    assert catalog.min_unit_price(None) is None


# ── DB: import + selector + turnover ─────────────────────────────────
@pytest.fixture
def part(store):
    return PartHandler(hub=Hub(store=store))


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _rows():
    return [
        catalog.normalize_jlcparts_row(r)
        for r in [
            {
                "lcsc": 1001,
                "description": "100nF 16V X7R 0402 capacitor",
                "basic": 1,
                "stock": 800000,
                "package": "0402",
                "price": [{"price": 0.002}],
            },
            {
                "lcsc": 1002,
                "description": "100nF 50V X7R 0402 capacitor",
                "basic": 0,
                "stock": 5000,
                "package": "0402",
                "price": [{"price": 0.01}],
            },
        ]
    ]


def test_parts_import_and_basic_first_selector(store, part):
    store.parts_import([r for r in _rows() if r])
    resp = part.search(q="100nF 0402 capacitor")
    body = resp.body
    assert "C1001" in body and "C1002" in body
    # Basic (C1001) ranks before Extended (C1002)
    assert body.index("C1001") < body.index("C1002")


def test_turnover_ranks_restocked_first(store, part):
    # two equally-Basic parts; one gets restocked across dumps → ranks first
    base = [
        catalog.normalize_jlcparts_row(r)
        for r in [
            {
                "lcsc": 2001,
                "description": "10k 0402 resistor",
                "basic": 1,
                "stock": 1000,
                "package": "0402",
            },
            {
                "lcsc": 2002,
                "description": "10k 0402 resistor",
                "basic": 1,
                "stock": 1000,
                "package": "0402",
            },
        ]
    ]
    store.parts_import([r for r in base if r])
    # second dump: 2001 restocked (stock rose), 2002 drained
    nxt = [
        catalog.normalize_jlcparts_row(r)
        for r in [
            {
                "lcsc": 2001,
                "description": "10k 0402 resistor",
                "basic": 1,
                "stock": 5000,
                "package": "0402",
            },
            {
                "lcsc": 2002,
                "description": "10k 0402 resistor",
                "basic": 1,
                "stock": 200,
                "package": "0402",
            },
        ]
    ]
    counts = store.parts_import([r for r in nxt if r])
    assert counts["restocked"] == 1  # only 2001 rose
    body = part.search(q="10k 0402 resistor").body
    assert body.index("C2001") < body.index("C2002")  # higher turnover first


def test_part_get(store, part):
    store.parts_import([r for r in _rows() if r])
    resp = part.get(id="C1001")
    assert "C1001" in resp.body and "0402" in resp.body


# ── footprint cache (Flow B, fake fetcher) ───────────────────────────
def test_footprint_cache_fetches_once(store):
    calls = []

    def fake(lcsc):
        calls.append(lcsc)
        return {
            "pads": [{"n": "1"}],
            "pin_map": {"1": "A"},
            "courtyard": {"w": 1.0, "h": 0.5},
            "source": "fake",
        }

    f1 = footprint.ensure_footprint(store, "C9001", fetcher=fake)
    assert f1 is not None and f1["source"] == "fake"
    f2 = footprint.ensure_footprint(store, "C9001", fetcher=fake)
    assert f2 is not None
    assert calls == ["C9001"]  # second call hit the cache, no re-fetch


# ── auto-stamp a catalog part onto a component ───────────────────────
def test_component_auto_stamps_footprint_from_catalog(store, pcb):
    store.parts_import(
        [
            catalog.normalize_jlcparts_row(
                {
                    "lcsc": 3003,
                    "description": "100nF 0402",
                    "basic": 1,
                    "stock": 9000,
                    "package": "0402",
                }
            )
        ]
    )
    # the LLM picks the part by C-number only — footprint/height get stamped
    pcb.put(
        id="b",
        args={
            "components": [
                {
                    "refdes": "C1",
                    "label": "100nF",
                    "part": "C3003",
                    "pins": [{"name": "1"}, {"name": "2"}],
                }
            ]
        },
    )
    ref = store.get_ref(kind="pcb", id="b")
    loaded = store.pcb_load(ref.id)
    c1 = next(i for i in loaded["instances"] if i["refdes"] == "C1")
    assert c1["part_lcsc"] == "C3003"
    assert c1["footprint"] == "0402"  # stamped from the catalog


# ── Flow A end-to-end from a jlcparts SQLite fixture ─────────────────
def test_refresh_from_sqlite(store, tmp_path):
    db = tmp_path / "cache.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE components (lcsc INTEGER, manufacturer TEXT, mfr TEXT, "
        "description TEXT, basic INTEGER, stock INTEGER, package TEXT, "
        "datasheet TEXT, price TEXT, extra TEXT)"
    )
    conn.execute(
        "INSERT INTO components VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            44004,
            "Yageo",
            "RC0402",
            "1k 0402 resistor",
            1,
            12345,
            "0402",
            "http://x",
            '[{"price": 0.001}]',
            '{"attributes": {}}',
        ),
    )
    conn.commit()
    conn.close()

    counts = catalog.refresh_parts_from_sqlite(store, str(db))
    assert counts["upserted"] == 1
    row = store.part_row("C44004")
    assert row is not None and row["package"] == "0402" and row["basic"] is True


def _write_jlcparts_sqlite(path, rows: list[tuple]) -> None:
    """Minimal jlcparts ``cache.sqlite3`` fixture — one ``components`` table,
    the columns :func:`test_refresh_from_sqlite` uses. Shared by the
    staging+swap and CLI tests below."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE components (lcsc INTEGER, manufacturer TEXT, mfr TEXT, "
        "description TEXT, basic INTEGER, stock INTEGER, package TEXT, "
        "datasheet TEXT, price TEXT, extra TEXT)"
    )
    conn.executemany("INSERT INTO components VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


# ── staging + atomic swap (0047 design, gr264357 §3) ──────────────────
def test_parts_bulk_replace_preserves_footprints_and_availability(store):
    """The bulk swap replaces `parts` wholesale, but `part_footprints` /
    `part_availability` are FK-free by design (0047) and must survive it —
    including keeping the turnover signal rolling forward for a part that
    drops out of one dump and returns in a later one."""
    store.parts_import(
        [
            catalog.normalize_jlcparts_row(
                {
                    "lcsc": 4004,
                    "description": "seed part",
                    "basic": 1,
                    "stock": 100,
                    "package": "0402",
                }
            )
        ]
    )
    footprint.ensure_footprint(
        store,
        "C4004",
        fetcher=lambda lcsc: {
            "pads": [{"n": "1"}],
            "pin_map": {"1": "A"},
            "source": "fake",
        },
    )
    assert store.part_footprint_get("C4004") is not None

    # A real dump reload replaces the WHOLE catalog — C4004 itself drops
    # out of `parts` here, standing in for "not in this dump anymore".
    new_rows = [
        r
        for r in [
            catalog.normalize_jlcparts_row(
                {
                    "lcsc": 5005,
                    "description": "new catalog",
                    "basic": 1,
                    "stock": 200,
                    "package": "0603",
                }
            )
        ]
        if r
    ]
    counts = store.parts_bulk_replace(new_rows)
    assert counts == {"loaded": 1, "restocked": 0}

    assert store.part_row("C4004") is None  # dropped by the swap, as designed
    assert store.part_row("C5005") is not None  # the new catalog is live
    # FK-free caches survive the drop of their `parts` row untouched.
    assert store.part_footprint_get("C4004") is not None

    # C4004 reappears in a later dump at a higher stock — the turnover
    # signal must have kept rolling across the swap, not reset.
    counts2 = store.parts_bulk_replace(
        [
            catalog.normalize_jlcparts_row(
                {
                    "lcsc": 4004,
                    "description": "seed part",
                    "basic": 1,
                    "stock": 500,
                    "package": "0402",
                }
            )
        ]
    )
    assert counts2 == {"loaded": 1, "restocked": 1}  # 500 > 100 from the first import
    row = store.part_row("C4004")
    assert row is not None and row["restock_count"] == 1


def test_parts_bulk_replace_is_atomic_on_failed_load(store):
    """A raise mid-load must roll back EVERYTHING this call did — the
    staging table's creation, every partial row, all of it — leaving the
    live `parts` table exactly as it was before the call."""
    store.parts_import(
        [
            catalog.normalize_jlcparts_row(
                {
                    "lcsc": 1001,
                    "description": "100nF 16V X7R 0402 capacitor",
                    "basic": 1,
                    "stock": 800000,
                    "package": "0402",
                }
            )
        ]
    )
    before = store.part_row("C1001")
    assert before is not None

    def _rows_then_boom():
        yield catalog.normalize_jlcparts_row(
            {
                "lcsc": 9999,
                "description": "never actually lands",
                "basic": 1,
                "stock": 1,
                "package": "0402",
            }
        )
        raise RuntimeError("simulated load failure")

    with pytest.raises(RuntimeError, match="simulated load failure"):
        store.parts_bulk_replace(_rows_then_boom())

    # The swap never ran — `parts` is untouched, and the half-loaded row
    # from the failed staging load never became visible anywhere.
    assert store.part_row("C1001") == before
    assert store.part_row("C9999") is None


def test_bulk_refresh_parts_from_sqlite_uses_the_swap(store, tmp_path):
    """`catalog.bulk_refresh_parts_from_sqlite` (the CLI's `--from-sqlite`
    path) streams a dump through the staging+swap, not the per-row upsert —
    same end-state as `refresh_parts_from_sqlite`, different mechanism."""
    db = tmp_path / "cache.sqlite3"
    _write_jlcparts_sqlite(
        db,
        [
            (
                66006,
                "Yageo",
                "RC0402",
                "4.7k 0402 resistor",
                1,
                999,
                "0402",
                "http://x",
                '[{"price": 0.002}]',
                "{}",
            )
        ],
    )
    counts = catalog.bulk_refresh_parts_from_sqlite(store, str(db))
    assert counts == {"loaded": 1, "restocked": 0}
    row = store.part_row("C66006")
    assert row is not None and row["package"] == "0402"


# ── the `parts_refresh` worker pass (gr264357 §2) ──────────────────────
class _FakeJlcApiClient:
    """Duck-typed stand-in for :class:`precis.pcb.jlc_api.JlcApiClient` —
    no network, no vault secrets. ``iter_components`` on the real client
    already yields *normalized* rows (``normalize_api_row`` applied
    internally), so pages here use the same ``parts``-column shape
    (``lcsc``/``stock``/…) :func:`precis.pcb.catalog.normalize_jlcparts_row`
    produces — not the raw JLCPCB wire shape. Each page dict may carry a
    ``_cursor`` key (stripped before yielding) that becomes ``last_key``
    after that row, mirroring the real client's per-page cursor advance."""

    def __init__(self, *, store=None, available=True, pages=(), permission_error=None):
        del store
        self.available = available
        self._pages = list(pages)
        self.last_key: str | None = None
        self._permission_error = permission_error
        self.seen_since_keys: list[str | None] = []

    def iter_components(self, *, since_key=None, page_size=100):
        del page_size
        self.seen_since_keys.append(since_key)
        for row in self._pages:
            row = dict(row)
            self.last_key = row.pop("_cursor", self.last_key)
            yield row
        if self._permission_error is not None:
            raise self._permission_error
        self.last_key = None  # walk exhausted — reset for the next full pass


def test_parts_refresh_pass_no_credentials_is_a_clean_noop(store):
    from precis.workers.parts_refresh import run_parts_refresh_pass

    client = _FakeJlcApiClient(available=False)
    result = run_parts_refresh_pass(store, client=client)
    assert result == {"claimed": 0, "ok": 0, "failed": 0}


def test_parts_refresh_pass_imports_and_checkpoints_a_bounded_walk(store):
    from precis.workers.parts_refresh import CURSOR_SETTING_KEY, run_parts_refresh_pass

    pages = [
        {
            "lcsc": "C90001",
            "description": "part one",
            "stock": 10,
            "_cursor": "cursor-1",
        },
        {
            "lcsc": "C90002",
            "description": "part two",
            "stock": 20,
            "_cursor": "cursor-2",
        },
    ]
    client = _FakeJlcApiClient(pages=pages)
    result = run_parts_refresh_pass(store, client=client, row_budget=1)
    assert result == {"claimed": 1, "ok": 1, "failed": 0, "restocked": 0}
    assert store.part_row("C90001") is not None
    assert store.part_row("C90002") is None  # row_budget stopped the walk here
    # Partial walk: the cursor advanced past row 1, so it's checkpointed —
    # a resumed tick continues from here instead of restarting.
    assert store.get_setting(CURSOR_SETTING_KEY) == "cursor-1"


def test_parts_refresh_pass_resumes_from_its_checkpoint(store):
    from precis.workers.parts_refresh import CURSOR_SETTING_KEY, run_parts_refresh_pass

    store.set_setting(CURSOR_SETTING_KEY, "cursor-1")
    client = _FakeJlcApiClient(
        pages=[{"lcsc": "C90003", "stock": 5, "_cursor": "cursor-2"}]
    )
    run_parts_refresh_pass(store, client=client)
    assert client.seen_since_keys == ["cursor-1"]


def test_parts_refresh_pass_surfaces_permission_error(store):
    from precis.pcb.jlc_api import JlcPermissionError
    from precis.workers.parts_refresh import run_parts_refresh_pass

    client = _FakeJlcApiClient(
        permission_error=JlcPermissionError(
            "jlcpcb", "console scope not granted", status=403
        )
    )
    result = run_parts_refresh_pass(store, client=client)
    assert result["failed"] == 1
    assert "console scope not granted" in result["error"]


def test_parts_refresh_service_spec_is_registered_and_discoverable():
    """The registration this whole gripe (gr264357) is about: before this
    change `parts_refresh` had no ServiceSpec at all — see
    tests/test_worker_registry.py for the totality guard that would have
    caught it wired-but-unspecced or specced-but-unwired."""
    from precis.workers.registry import SERVICES_BY_NAME, ServiceKind

    spec = SERVICES_BY_NAME["parts_refresh"]
    assert spec.kind == ServiceKind.PASS
    assert spec.ref_pass is True
    # Cadence-fired (daily, see workers/scheduler.py), not per-cycle.
    assert spec.default_profiles == frozenset()


def test_parts_refresh_scheduler_cadence_is_daily():
    from precis.workers.scheduler import CADENCES

    cad = next(c for c in CADENCES if c.name == "parts_refresh")
    assert cad.interval_s >= 24 * 3600  # daily, not per-minute


# ── the `precis pcb refresh-parts` CLI verb (gr264357 §1) ──────────────
class _NoCredsClient:
    available = False

    def __init__(self, *, store=None):
        del store


class _CredsClient:
    available = True

    def __init__(self, *, store=None):
        del store


def test_cli_refresh_default_falls_back_to_sqlite_without_credentials(
    store, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr("precis.pcb.jlc_api.JlcApiClient", _NoCredsClient)
    db = tmp_path / "cache.sqlite3"
    _write_jlcparts_sqlite(
        db,
        [
            (
                55005,
                "Yageo",
                "RC0603",
                "10k 0603 resistor",
                1,
                42,
                "0603",
                "http://x",
                "[]",
                "{}",
            )
        ],
    )
    monkeypatch.setenv("PRECIS_JLCPARTS_DUMP_PATH", str(db))

    pcb_cli._refresh_default(store, page_size=100, row_limit=None)

    out = capsys.readouterr().out
    assert "no JLCPCB API credentials" in out
    assert "falling back" in out
    assert store.part_row("C55005") is not None


def test_cli_refresh_default_raises_cleanly_with_no_source_configured(
    store, monkeypatch
):
    monkeypatch.setattr("precis.pcb.jlc_api.JlcApiClient", _NoCredsClient)
    monkeypatch.delenv("PRECIS_JLCPARTS_DUMP_PATH", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        pcb_cli._refresh_default(store, page_size=100, row_limit=None)
    assert "no JLCPCB API credentials" in str(excinfo.value)


def test_cli_refresh_from_api_requires_credentials(store, monkeypatch):
    monkeypatch.setattr("precis.pcb.jlc_api.JlcApiClient", _NoCredsClient)

    with pytest.raises(SystemExit) as excinfo:
        pcb_cli._refresh_from_api(store, page_size=100, row_limit=None)
    assert "no JLCPCB API credentials" in str(excinfo.value)


def test_cli_refresh_from_api_surfaces_permission_error_cleanly(store, monkeypatch):
    """A JLCPCB 403 must reach the operator as a clean ``SystemExit``
    message (see ``JlcPermissionError``'s module docstring), never a raw
    traceback that invites re-debugging the signing code."""
    monkeypatch.setattr("precis.pcb.jlc_api.JlcApiClient", _CredsClient)

    def _fake_run_parts_refresh_pass(store, *, row_budget, client, page_size):
        del store, row_budget, client, page_size
        return {
            "claimed": 0,
            "ok": 0,
            "failed": 1,
            "error": "jlcpcb: console scope not granted",
        }

    monkeypatch.setattr(
        "precis.workers.parts_refresh.run_parts_refresh_pass",
        _fake_run_parts_refresh_pass,
    )

    with pytest.raises(SystemExit) as excinfo:
        pcb_cli._refresh_from_api(store, page_size=100, row_limit=None)
    assert "console scope not granted" in str(excinfo.value)


def test_cli_refresh_from_api_prints_source_and_counts(store, monkeypatch, capsys):
    monkeypatch.setattr("precis.pcb.jlc_api.JlcApiClient", _CredsClient)

    def _fake_run_parts_refresh_pass(store, *, row_budget, client, page_size):
        del store, row_budget, client, page_size
        return {"claimed": 3, "ok": 3, "failed": 0, "restocked": 1}

    monkeypatch.setattr(
        "precis.workers.parts_refresh.run_parts_refresh_pass",
        _fake_run_parts_refresh_pass,
    )

    pcb_cli._refresh_from_api(store, page_size=100, row_limit=None)
    out = capsys.readouterr().out
    assert "source=jlcpcb-api" in out and "upserted=3" in out and "restocked=1" in out


def test_cli_refresh_from_sqlite_uses_the_bulk_swap(store, tmp_path, capsys):
    db = tmp_path / "cache.sqlite3"
    _write_jlcparts_sqlite(
        db,
        [
            (
                77007,
                "Murata",
                "GRM1",
                "1uF 0402 capacitor",
                1,
                123,
                "0402",
                "http://x",
                "[]",
                "{}",
            )
        ],
    )
    pcb_cli._refresh_from_sqlite(store, str(db))
    out = capsys.readouterr().out
    assert "source=jlcparts-dump" in out and "loaded=1" in out
    assert store.part_row("C77007") is not None
