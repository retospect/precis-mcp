"""``precis_se.scratch_import`` — the dev-DB scratch out/ importer
(gr514239): one command from ``scratch/out/<run>`` to ``get`` views."""

from __future__ import annotations

from pathlib import Path

from precis.dispatch import Hub
from precis.store import Store
from precis_se.handler import SeHandler
from precis_se.scratch_import import import_scratch_run, main

FIXTURE = Path(__file__).resolve().parent / "data" / "trace_demo" / "out"


def _trace_body(store: Store, design: str) -> str:
    return (
        SeHandler(hub=Hub(store=store))
        .get(id=design, view="trace", args={"atom": "p/s1/s1"})
        .body
    )


def test_import_scratch_run_creates_design_and_views_work(store: Store) -> None:
    res = import_scratch_run(store, out_dir=FIXTURE, design="scratch-x")
    assert res.trace is not None
    assert store.get_ref(kind="se", id="scratch-x") is not None
    body = _trace_body(store, "scratch-x")
    assert "junction(k=3)#1" in body


def test_reimport_into_existing_design_is_the_dogfood_loop(
    store: Store,
) -> None:
    import_scratch_run(store, out_dir=FIXTURE, design="scratch-x")
    res = import_scratch_run(store, out_dir=FIXTURE, design="scratch-x")
    # The second pass replaces/verifies rather than duplicating or dying.
    assert res.replaced or res.verified or res.unchanged
    assert "junction(k=3)#1" in _trace_body(store, "scratch-x")


def test_main_happy_path(store: Store, monkeypatch, capsys) -> None:
    monkeypatch.setattr(Store, "connect", classmethod(lambda cls, dsn, **kw: store))
    monkeypatch.setattr(Store, "close", lambda self: None)
    rc = main(
        [
            str(FIXTURE),
            "--dsn",
            "postgresql://localhost/precis_dev",
            "--view",
            "trace",
            "--arg",
            'atom="p/s1/s1"',
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    # --design defaults to the out_dir basename.
    assert "workspace import into 'out'" in out
    assert "junction(k=3)#1" in out
    assert "get(kind='se', id='out', view='trace')" in out


def test_main_dev_dsn_guard_refuses_before_connect(monkeypatch, capsys) -> None:
    def _boom(cls, dsn, **kw):  # connect must never run on a prod DSN
        raise AssertionError("Store.connect reached")

    monkeypatch.setattr(Store, "connect", classmethod(_boom))
    rc = main([str(FIXTURE), "--dsn", "postgresql://db.internal/precis"])
    assert rc == 2
    assert "dev-DB only" in capsys.readouterr().err


def test_main_accepts_test_clone_dbname(store: Store, monkeypatch, capsys) -> None:
    monkeypatch.setattr(Store, "connect", classmethod(lambda cls, dsn, **kw: store))
    monkeypatch.setattr(Store, "close", lambda self: None)
    rc = main(
        [
            str(FIXTURE),
            "--design",
            "scratch-clone",
            "--dsn",
            "postgresql://localhost/precis_test_abcd",
        ]
    )
    assert rc == 0
    assert "workspace import into 'scratch-clone'" in capsys.readouterr().out
