"""``audit_hubs`` ranking + the ``taproot numeral-audit`` parser (gr250034)."""

from __future__ import annotations

from precis.cli import taproot as taproot_cli
from precis.taproot.numeral_audit import audit_hubs


def test_audit_hubs_ranks_by_unmatched_count_and_skips_clean_and_ungrounded() -> None:
    hubs = [
        (1, "Cells reach quantum efficiencies of 1-5% under illumination."),
        (2, "The framework conducts 0.3 W/m·K."),
        (3, "Uses 7 cycles, 9 volts and 11 cells."),
        (4, "Has 42 nodes."),
    ]
    grounding = {
        1: [(10, "The device shows strong photocurrent.")],
        2: [(11, "Conductivity is 0.3 W/m·K at room temperature.")],
        3: [(12, "Nothing numeric here."), (13, "Still none.")],
    }
    rows = audit_hubs(hubs, grounding)
    assert [r["ref_id"] for r in rows] == [3, 1]
    assert rows[0]["hub_id"] == "fi3"
    assert len(rows[0]["unmatched"]) == 3
    assert rows[0]["chunk_ids"] == [12, 13]
    assert "1" in rows[1]["unmatched"][0]


def test_numeral_audit_subcommand_registered() -> None:
    import argparse

    root = argparse.ArgumentParser()
    taproot_cli.add_parser(root.add_subparsers(dest="cmd"))
    ns = root.parse_args(
        ["taproot", "numeral-audit", "--sample", "5", "--seed", "1", "--format", "json"]
    )
    assert ns.taproot_cmd == "numeral-audit"
    assert ns.sample == 5
    assert ns.format == "json"
