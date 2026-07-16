"""Tests for the cron ``/automations`` list view (automations index).

A standing automation is a cron carrying the ``automation`` tag; the view
lists them (by next_fire_at) so the recurring agent behaviours — the
morning/evening podcast casts, the news briefing — are discoverable, as
opposed to one-shot reminders. See docs/design/automations-index.md.
"""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.handlers.cron import CronHandler

_TARGET = "conv:discord/g/c/t"


@pytest.fixture
def cron(hub: Hub) -> CronHandler:
    return CronHandler(hub=hub)


def _id_of(body: str) -> int:
    m = re.search(r"id=(\d+)", body)
    assert m, f"no cron id in ack: {body!r}"
    return int(m.group(1))


def _schedule(cron: CronHandler, text: str, *, recurring: str = "daily@06:00") -> int:
    return _id_of(cron.put(text=text, recurring=recurring, target=_TARGET).body)


class TestAutomationsView:
    def test_lists_only_tagged_crons(self, cron: CronHandler) -> None:
        auto_id = _schedule(cron, "morning cast")
        _schedule(cron, "just a reminder")  # untagged — must not appear
        cron.tag(id=auto_id, add=["automation", "cast-morning"])

        body = cron.get(id="/automations").body
        assert "1 automation" in body
        assert str(auto_id) in body
        assert "morning cast" in body
        assert "just a reminder" not in body

    def test_empty_when_none_tagged(self, cron: CronHandler) -> None:
        _schedule(cron, "a plain reminder")
        body = cron.get(id="/automations").body
        assert "no automations" in body.lower()

    def test_orders_by_next_fire(self, cron: CronHandler) -> None:
        early = _id_of(
            cron.put(text="early", when="2026-01-01T06:00:00Z", target=_TARGET).body
        )
        late = _id_of(
            cron.put(text="late", when="2026-12-31T06:00:00Z", target=_TARGET).body
        )
        cron.tag(id=late, add=["automation"])
        cron.tag(id=early, add=["automation"])

        body = cron.get(id="/automations").body
        assert "2 automations" in body
        # Sorted by next_fire_at ascending — the Jan cron precedes the Dec one.
        assert body.index("early") < body.index("late")

    def test_automations_is_a_supported_view(self, cron: CronHandler) -> None:
        assert "automations" in cron._supported_list_views()
