"""Shared voice guidance must remain discoverable from the narration skill."""

from pathlib import Path


def test_voice_section_markers() -> None:
    skills = Path(__file__).resolve().parents[1] / "src/precis/data/skills"
    text = (skills / "precis-word-handles-help.md").read_text(encoding="utf-8")
    assert "## Talking by voice: name things, keep handles recoverable" in text
    assert "never read" in text
    assert "precis-word-handles-help" in (skills / "precis-voice.md").read_text(
        encoding="utf-8"
    )
