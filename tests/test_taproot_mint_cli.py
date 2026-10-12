"""``taproot mint`` CLI: the ``MintCapped`` skip path in ``_run_mint``.

No DB: ``Store.connect`` and ``seed_claim_hub`` are stubbed.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest

from precis.cli import taproot as cli
from precis.taproot.canon import MintCapped


def test_mint_capped_claim_is_skipped_and_run_continues(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _FakeStore:
        closed = False

        def close(self) -> None:
            self.closed = True

    fake = _FakeStore()
    monkeypatch.setattr("precis.store.Store.connect", lambda dsn: fake)
    monkeypatch.setattr(cli, "resolve_dsn", lambda url: "stub://")
    monkeypatch.setattr(cli, "_preflight_resolve_supporters", lambda s, c: None)

    def _seed(store: Any, *, sentence: str, **_: Any) -> dict[str, Any]:
        if sentence == "first claim":
            raise MintCapped("chunk already seeded 3 hubs")
        return {"ref_id": 2}

    monkeypatch.setattr("precis.taproot.authoring.seed_claim_hub", _seed)

    args = argparse.Namespace(
        spec=None,
        json_spec=json.dumps(
            [{"sentence": "first claim"}, {"sentence": "second claim"}]
        ),
        dry_run=False,
        set_by="tester",
        format="json",
        database_url=None,
    )
    cli._run_mint(args)  # must return, not SystemExit

    captured = capsys.readouterr()
    assert (
        "taproot mint: skipped 'first claim': chunk already seeded 3 hubs"
        in captured.err
    )
    results = json.loads(captured.out)
    assert [r["sentence"] for r in results] == ["second claim"]
    assert fake.closed
