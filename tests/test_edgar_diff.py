"""Tests for the quarter-to-quarter section diff (pure logic).

The store-backed ``compute_diff`` / ``find_prior_ref`` / ``render_diff``
paths are covered in ``test_edgar_handler.py`` (PG-backed). Here we test
the pure ``diff_sections`` + ``diff_tags`` core.
"""

from __future__ import annotations

from precis.handlers._edgar_diff import (
    FilingDiff,
    SectionDelta,
    diff_sections,
    diff_tags,
)


def _sec(paras: list[str], path: list[str]) -> tuple[list[str], list[str]]:
    return (path, paras)


class TestDiffSections:
    def test_unchanged_section_omitted(self) -> None:
        cur = {"item-1a": _sec(["Risk one.", "Risk two."], ["Item 1A", "Risk Factors"])}
        prior = {
            "item-1a": _sec(["Risk one.", "Risk two."], ["Item 1A", "Risk Factors"])
        }
        assert diff_sections(cur, prior) == []

    def test_added_section(self) -> None:
        cur = {"item-1c": _sec(["Cyber disclosure."], ["Item 1C", "Cybersecurity"])}
        prior: dict = {}
        deltas = diff_sections(cur, prior)
        assert len(deltas) == 1
        assert deltas[0].status == "added"
        assert deltas[0].canonical_id == "item-1c"
        assert deltas[0].added_paras == ["Cyber disclosure."]

    def test_removed_section(self) -> None:
        cur: dict = {}
        prior = {"item-6": _sec(["Selected data."], ["Item 6"])}
        deltas = diff_sections(cur, prior)
        assert deltas[0].status == "removed"
        assert deltas[0].removed_paras == ["Selected data."]

    def test_changed_section_detects_new_paragraph(self) -> None:
        prior = {
            "item-1a": _sec(
                ["Macro risk exists.", "Supply chain risk exists."],
                ["Item 1A", "Risk Factors"],
            )
        }
        cur = {
            "item-1a": _sec(
                [
                    "Macro risk exists.",
                    "Supply chain risk exists.",
                    "New: cyber-attack risk has emerged this quarter.",
                ],
                ["Item 1A", "Risk Factors"],
            )
        }
        deltas = diff_sections(cur, prior)
        assert len(deltas) == 1
        d = deltas[0]
        assert d.status == "changed"
        assert d.similarity < 1.0
        assert "New: cyber-attack risk has emerged this quarter." in d.added_paras
        assert d.removed_paras == []

    def test_body_section_ignored(self) -> None:
        cur = {"body": _sec(["Cover page A."], ["Body"])}
        prior = {"body": _sec(["Cover page B."], ["Body"])}
        assert diff_sections(cur, prior) == []

    def test_whitespace_only_change_not_material(self) -> None:
        prior = {"item-7": _sec(["Net sales rose."], ["Item 7"])}
        cur = {"item-7": _sec(["Net   sales   rose."], ["Item 7"])}
        # SequenceMatcher ratio on near-identical text stays above the
        # material-change floor → no delta.
        assert diff_sections(cur, prior) == []


class TestDiffTags:
    def _diff_with(self, deltas: list[SectionDelta]) -> FilingDiff:
        return FilingDiff(
            current_ref_id=2,
            current_slug="0000320193-24-000010",
            prior_ref_id=1,
            prior_slug="0000320193-23-000106",
            form="10-K",
            current_period="2024-09-30",
            prior_period="2023-09-30",
            deltas=deltas,
        )

    def test_changed_tags_per_section(self) -> None:
        diff = self._diff_with(
            [
                SectionDelta("item-1a", ["Item 1A"], "changed", 0.8, ["new risk"], []),
                SectionDelta("item-7", ["Item 7"], "changed", 0.9, [], []),
            ]
        )
        tags = diff_tags(diff)
        assert "changed:item-1a" in tags
        assert "changed:item-7" in tags

    def test_new_risk_factor_tag(self) -> None:
        diff = self._diff_with(
            [SectionDelta("item-1a", ["Item 1A"], "changed", 0.8, ["new risk"], [])]
        )
        assert "new-risk-factor" in diff_tags(diff)
        assert diff.has_new_risk_factors is True

    def test_no_new_risk_factor_when_only_removed(self) -> None:
        diff = self._diff_with(
            [SectionDelta("item-1a", ["Item 1A"], "changed", 0.8, [], ["dropped risk"])]
        )
        assert "new-risk-factor" not in diff_tags(diff)

    def test_high_signal_flag(self) -> None:
        d = SectionDelta("item-1a", ["Item 1A"], "changed", 0.8, ["x"], [])
        assert d.high_signal is True
        d2 = SectionDelta("item-15", ["Item 15"], "changed", 0.8, [], [])
        assert d2.high_signal is False
