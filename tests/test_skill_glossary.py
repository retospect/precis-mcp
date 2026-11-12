"""The glossary convention skill exists and is reachable from the voice skills."""

from pathlib import Path

import precis

_SKILLS = Path(precis.__file__).parent / "data" / "skills"


def test_glossary_skill_names_the_convention() -> None:
    text = (_SKILLS / "precis-glossary-help.md").read_text()
    assert "id: precis-glossary-help" in text
    assert "tags=['glossary']" in text
    for label in ("synonyms:", "heard as:", "say as:", "misconception:"):
        assert label in text


def test_voice_skills_point_at_glossary() -> None:
    for name in ("precis-voice.md", "precis-audio-help.md"):
        assert "precis-glossary-help" in (_SKILLS / name).read_text()
