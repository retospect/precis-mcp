"""Tests for `precis.taxonomy.config` — campaign YAML loading.

Plain DB-free functions, no ``store`` fixture, no markers. Covers the
threshold sign-off discipline (`taxonomy-bootstrap.md` §Thresholds: a
campaign may only tighten, never loosen), the UTC-timestamp guard, and the
shipped ``norr-her-meta`` campaign's own shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.taxonomy.config import load_campaign

_MINIMAL_SNAPSHOT = """
snapshot:
  source: test
  row_count: 10
  sha256: "{sha}"
  pulled_at: {pulled_at}
""".strip()


def _write_campaign(
    tmp_path: Path,
    *,
    extra: str = "",
    pulled_at: str = "2026-09-27T20:02:11Z",
    sha256: str = "0" * 64,
) -> Path:
    body = f"""
campaign: test-campaign
config_version: 1
{_MINIMAL_SNAPSHOT.format(sha=sha256, pulled_at=pulled_at)}
{extra}
""".strip()
    path = tmp_path / "campaign.yaml"
    path.write_text(body + "\n", encoding="utf-8")
    return path


# ── thresholds: tighten only ────────────────────────────────────────


def test_tightening_a_threshold_is_accepted(tmp_path: Path) -> None:
    path = _write_campaign(tmp_path, extra="thresholds:\n  min_papers: 5\n")
    cfg = load_campaign(path)
    assert cfg.thresholds.min_papers == 5


def test_loosening_a_threshold_raises(tmp_path: Path) -> None:
    path = _write_campaign(tmp_path, extra="thresholds:\n  min_papers: 2\n")
    with pytest.raises(ValueError, match="only tighten"):
        load_campaign(path)


def test_loosening_max_escape_rate_raises(tmp_path: Path) -> None:
    # max_escape_rate is the one "tighter is smaller" knob — a *larger*
    # value is the loosening direction here, unlike the others.
    path = _write_campaign(tmp_path, extra="thresholds:\n  max_escape_rate: 0.5\n")
    with pytest.raises(ValueError, match="only tighten"):
        load_campaign(path)


def test_unknown_threshold_key_raises(tmp_path: Path) -> None:
    path = _write_campaign(tmp_path, extra="thresholds:\n  min_bogus: 5\n")
    with pytest.raises(ValueError, match="unknown threshold"):
        load_campaign(path)


def test_require_both_halves_false_raises(tmp_path: Path) -> None:
    path = _write_campaign(
        tmp_path, extra="thresholds:\n  require_both_halves: false\n"
    )
    with pytest.raises(ValueError, match="require_both_halves"):
        load_campaign(path)


def test_require_single_dimension_false_raises(tmp_path: Path) -> None:
    path = _write_campaign(
        tmp_path, extra="thresholds:\n  require_single_dimension: false\n"
    )
    with pytest.raises(ValueError, match="require_single_dimension"):
        load_campaign(path)


# ── snapshot.pulled_at: UTC only ─────────────────────────────────────


def test_pulled_at_naive_datetime_raises(tmp_path: Path) -> None:
    path = _write_campaign(tmp_path, pulled_at="2026-09-27T20:02:11")
    with pytest.raises(ValueError, match="UTC"):
        load_campaign(path)


def test_pulled_at_non_utc_offset_raises(tmp_path: Path) -> None:
    path = _write_campaign(tmp_path, pulled_at="2026-09-27T20:02:11+02:00")
    with pytest.raises(ValueError, match="UTC"):
        load_campaign(path)


def test_pulled_at_string_without_z_raises(tmp_path: Path) -> None:
    # A quoted string PyYAML does *not* resolve to a datetime still has to
    # carry the Z suffix explicitly.
    path = _write_campaign(tmp_path, pulled_at='"2026-09-27 20:02:11"')
    with pytest.raises(ValueError, match="UTC"):
        load_campaign(path)


def test_pulled_at_pyyaml_datetime_renders_with_z_suffix(tmp_path: Path) -> None:
    # An unquoted ISO timestamp with a Z suffix is resolved by PyYAML to a
    # tz-aware datetime *before* this code ever sees a string — config.py
    # must accept that form too, not just a literal "...Z" string.
    path = _write_campaign(tmp_path, pulled_at="2026-09-27T20:02:11Z")
    cfg = load_campaign(path)
    assert cfg.snapshot.pulled_at == "2026-09-27T20:02:11Z"


# ── shipped norr-her-meta campaign ──────────────────────────────────


def test_norr_her_meta_has_24_domain_classes() -> None:
    cfg = load_campaign("norr-her-meta")
    assert len(cfg.domain_classes) == 24


def test_norr_her_meta_required_conditions_start_with_temperature() -> None:
    cfg = load_campaign("norr-her-meta")
    conditions = cfg.required_conditions("faradaic-efficiency")
    assert conditions[0] == "temperature"
