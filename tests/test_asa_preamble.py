"""Operator-preferences injection — the generic replacement for baking
deployment-specific user detail into the source.
"""

from __future__ import annotations

from asa_bot.config import PreambleConfig
from asa_bot.preamble import _render_operator_prefs


def test_empty_by_default():
    assert _render_operator_prefs(PreambleConfig()) == ""


def test_inline_prefs_render_as_block():
    cfg = PreambleConfig(operator_prefs="Call me Sam. Terse answers.")
    out = _render_operator_prefs(cfg)
    assert out.startswith("## Operator preferences")
    assert "Call me Sam. Terse answers." in out


def test_path_wins_over_inline(tmp_path):
    p = tmp_path / "prefs.md"
    p.write_text("From the file.", encoding="utf-8")
    cfg = PreambleConfig(operator_prefs="inline", operator_prefs_path=str(p))
    out = _render_operator_prefs(cfg)
    assert "From the file." in out
    assert "inline" not in out


def test_unreadable_path_falls_back_to_inline(tmp_path):
    cfg = PreambleConfig(
        operator_prefs="inline fallback",
        operator_prefs_path=str(tmp_path / "does-not-exist.md"),
    )
    assert "inline fallback" in _render_operator_prefs(cfg)


def test_generic_defaults_have_no_personal_paths():
    # Defaults must not hard-code any deployment's home/user.
    cfg = PreambleConfig()
    assert "/Users/hermes" not in cfg.soul_path
    assert cfg.soul_path.endswith(".asa/SOUL.md")
