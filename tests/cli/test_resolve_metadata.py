"""``precis resolve-metadata``'s CLI summary must show every verdict lane.

``resolve_triage`` (``precis/ingest/metadata_resolve.py``) can return a
``parked`` verdict — ``park_mismatch`` writes :data:`MISMATCH_TAG` for a
human under ``--apply`` — but the CLI only printed/counted ``auto``,
``review`` and ``discard``, so an operator inspecting the dry-run lanes
before ``--apply`` never saw how many papers would be silently parked.
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from precis.cli import resolve_metadata as cli_resolve_metadata
from precis.ingest.metadata_resolve import Resolution


class _FakeConfig:
    def model_copy(self, update: dict[str, Any] | None = None) -> _FakeConfig:
        return self


class _FakeRuntime:
    store = object()


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="precis")
    sub = parser.add_subparsers(dest="cmd")
    cli_resolve_metadata.add_parser(sub)
    return parser.parse_args(["resolve-metadata", *argv])


@pytest.fixture
def stub_resolve_triage(monkeypatch: pytest.MonkeyPatch) -> list[Resolution]:
    results = [
        Resolution(1, "auto", "doi", "crossref-hit"),
        Resolution(2, "parked", "doi", "registry-title-zero-overlap"),
        Resolution(3, "review", "title", "below-auto-threshold"),
    ]
    monkeypatch.setattr(
        cli_resolve_metadata, "resolve_dsn", lambda override, cfg=None: "unused"
    )
    monkeypatch.setattr(cli_resolve_metadata, "get_secret", lambda *a, **k: "")
    monkeypatch.setattr("precis.config.load_config", lambda: _FakeConfig())
    monkeypatch.setattr("precis.runtime.build_runtime", lambda cfg: _FakeRuntime())
    monkeypatch.setattr(
        "precis.ingest.metadata_resolve.resolve_triage",
        lambda store, **kwargs: results,
    )
    return results


def test_dry_run_prints_and_counts_the_parked_lane(
    stub_resolve_triage: list[Resolution], capsys: pytest.CaptureFixture[str]
) -> None:
    cli_resolve_metadata.run(_parse([]))
    out = capsys.readouterr()
    assert "[parked:doi] #2 registry-title-zero-overlap" in out.out
    assert "would park 1 for human review" in out.err


def test_apply_reports_parked_as_parked_not_applied(
    stub_resolve_triage: list[Resolution], capsys: pytest.CaptureFixture[str]
) -> None:
    cli_resolve_metadata.run(_parse(["--apply"]))
    out = capsys.readouterr()
    assert "parked 1 for human review" in out.err
    assert "would park" not in out.err
