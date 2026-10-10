"""``precis taproot classify`` -- the claim_type backfill and the human
``--set`` door (:mod:`precis.cli.taproot` ``_run_classify``). DB-backed via
the ``store`` fixture; no LLM (``classify_sentence`` is monkeypatched)."""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest

from precis.cli import taproot as cli_taproot
from precis.store.store import Store
from precis.taproot import claim_type as ct
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub


@pytest.fixture
def cli_store(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    """The fixture store, handed to the runner in place of a fresh connect
    (and kept open across the runner's ``store.close()``)."""
    monkeypatch.setattr(Store, "connect", classmethod(lambda cls, *a, **k: store))
    monkeypatch.setattr(store, "close", lambda: None)
    monkeypatch.setattr(cli_taproot, "resolve_dsn", lambda override: "unused")
    return store


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="precis")
    sub = parser.add_subparsers(dest="cmd")
    cli_taproot.add_parser(sub)
    return parser.parse_args(["taproot", "classify", *argv])


def _run(argv: list[str]) -> None:
    cli_taproot.run(_parse(argv))


def _mint(store: Store, sentence: str) -> int:
    return mint_hub(store, CanonicalClaim(sentence=sentence, scope={}))


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def test_set_without_apply_writes_nothing_and_exits_2(cli_store: Store) -> None:
    hub = _mint(cli_store, "Perovskite solar cells convert sunlight to power.")
    with pytest.raises(SystemExit) as ei:
        _run(["--set", "landscape", "--hub", f"fi{hub}"])
    assert ei.value.code == 2
    assert _meta(cli_store, hub).get("claim_type") is None


def test_set_apply_marks_human(
    cli_store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = _mint(cli_store, "Perovskite solar cells convert sunlight to power.")
    _run(["--set", "landscape", "--hub", f"fi{hub}", "--apply"])
    meta = _meta(cli_store, hub)
    assert meta["claim_type"] == "landscape"
    assert meta["claim_type_by"] == "human"
    out = json.loads(capsys.readouterr().out)
    assert out["hub_ref_id"] == hub
    assert out["claim_type"] == "landscape"


def test_set_requires_hub(cli_store: Store) -> None:
    with pytest.raises(SystemExit) as ei:
        _run(["--set", "landscape", "--apply"])
    assert ei.value.code == 2


def test_set_bogus_type_rejected() -> None:
    with pytest.raises(SystemExit) as ei:
        _parse(["--set", "bogus", "--hub", "fi1", "--apply"])
    assert ei.value.code == 2


def test_backfill_dry_run_apply_and_idempotent(
    cli_store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    a = _mint(cli_store, "Perovskite solar cells convert sunlight to power.")
    b = _mint(cli_store, "Lithium-ion cells store charge in graphite anodes.")
    monkeypatch.setattr(
        ct,
        "classify_sentence",
        lambda sentence, scope: (
            "landscape" if "Perovskite" in sentence else "mechanism"
        ),
    )

    _run([])  # dry-run default
    cap = capsys.readouterr()
    rows = [json.loads(line) for line in cap.out.splitlines()]
    assert {r["hub_ref_id"] for r in rows} == {a, b}
    assert all(r["applied"] is False for r in rows)
    assert "DRY-RUN" in cap.err
    assert _meta(cli_store, a).get("claim_type") is None
    assert _meta(cli_store, b).get("claim_type") is None

    _run(["--apply"])
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {r["hub_ref_id"]: r["claim_type"] for r in rows} == {
        a: "landscape",
        b: "mechanism",
    }
    assert all(r["applied"] is True for r in rows)
    assert _meta(cli_store, a)["claim_type"] == "landscape"
    assert _meta(cli_store, a)["claim_type_by"] == "llm"
    assert _meta(cli_store, b)["claim_type"] == "mechanism"

    _run(["--apply"])  # second run: nothing left to classify
    assert capsys.readouterr().out.strip() == ""
