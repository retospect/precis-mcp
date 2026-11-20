"""The containment prompt must not treat a shared campaign as COMPONENT."""

from precis.taproot.canon import _CONTAINMENT_PROMPT


def test_prompt_separates_agreement_from_dataset_size() -> None:
    prompt = " ".join(_CONTAINMENT_PROMPT.split())
    assert "If each claim could be true while the other is false" in prompt
    assert (
        "were simulated are DIFFERENT, even for the same simulation campaign" in prompt
    )
    assert (
        "asserts both the reproduction and the count, reproduction alone is a COMPONENT"
        in prompt
    )
