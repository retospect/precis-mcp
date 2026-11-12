"""The deploy ask never asserts a green verdict it has not seen (gr634693)."""

from __future__ import annotations

from precis.plans import release


def test_approval_body_does_not_claim_green() -> None:
    body = release.approval_body("r274")
    assert "plan cut is green" not in body
    assert "only if" in body
    assert "answer: deploy r274" in body
