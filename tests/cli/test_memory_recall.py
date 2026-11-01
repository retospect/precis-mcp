"""The curve CLI and morning brief consume the same recorded recall costs."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from precis.budget import settings as app_settings
from precis.cli import _build_parser
from precis.cli.memory import SPACE_TAG, run
from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.reading.briefing_cast import _lane_system_activity
from precis.store import Store
from precis.workers.memory_recall import LAST_SCORE_KEY, run_memory_recall_pass
from tests.conftest import id_of


def test_set_curve_prints_four_cutoffs(store: Store, hub: Hub, monkeypatch, capsys):
    rid = id_of(
        MemoryHandler(hub=hub)
        .put(text="zebra quorum", title="Zebra quorum", tags=[SPACE_TAG])
        .body
    )
    monkeypatch.setattr("precis.cli.memory.resolve_dsn", lambda *a, **k: "unused")
    monkeypatch.setattr(Store, "connect", classmethod(lambda cls, dsn: store))
    monkeypatch.setattr(store, "close", lambda: None)
    monkeypatch.setattr("precis.cli._common.cli_embedder", lambda *a, **k: hub.embedder)
    monkeypatch.setattr(
        "precis.cli.memory.load_recall_set",
        lambda path: [{"query": "zebra quorum", "want": f"me{rid}"}],
    )
    run(_build_parser().parse_args(["memory", "index", "--set", "--curve"]))
    output = capsys.readouterr().out
    assert "chars/4" in output and "index + fisheye+1hop" in output
    rows = [line.split() for line in output.splitlines() if line.strip()[:1].isdigit()]
    assert [int(row[0]) for row in rows] == [3, 5, 8, 12]
    assert all(row[1:3] == ["1/1", "(100.0%)"] for row in rows)
    assert all(float(row[3]) > 0 and row[4] == "1" for row in rows)


def test_curve_requires_set():
    with pytest.raises(SystemExit, match="--curve requires --set"):
        run(_build_parser().parse_args(["memory", "index", "--curve"]))


def test_morning_brief_reports_recorded_recall_cost(store: Store, hub: Hub):
    rid = id_of(
        MemoryHandler(hub=hub)
        .put(text="zebra quorum", title="Zebra quorum", tags=[SPACE_TAG])
        .body
    )
    now = datetime(2026, 11, 1, tzinfo=UTC)
    run_memory_recall_pass(
        store,
        entries=[{"query": "zebra quorum", "want": f"me{rid}"}],
        embedder=hub.embedder,
        now=now,
    )
    raw = app_settings.get_setting(store, LAST_SCORE_KEY)
    assert raw is not None
    doc = json.loads(raw)
    output = _lane_system_activity(store, cutoff=now, now=now)
    lines = [line for line in output.splitlines() if "Memory recall:" in line]
    assert len(lines) == 1
    assert "1/1 at k=5" in lines[0]
    assert f"median {doc['median_tokens_k5']:.0f} tokens" in lines[0]
    assert "chars/4" in lines[0] and now.isoformat() in lines[0]
