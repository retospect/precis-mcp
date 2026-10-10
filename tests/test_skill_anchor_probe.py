"""The store's skill-read stamp consults a registered probe, never
:mod:`precis.handlers` (import-linter: "store is the bottom layer"; the
contract went red on main at 403b7c379)."""

from __future__ import annotations

from precis.handlers import skill
from precis.store import _refs_ops


def test_skill_handler_registers_the_store_probe():
    assert _refs_ops._SKILL_PROBE is skill.skill_exists
    assert _refs_ops._SKILL_PROBE("precis-memory-help")
    assert not _refs_ops._SKILL_PROBE("no-such-skill-zzz")


def test_touch_skill_recalled_is_a_noop_without_a_probe(monkeypatch):
    monkeypatch.setattr(_refs_ops, "_SKILL_PROBE", None)
    calls: list[str] = []

    class Fake(_refs_ops.RefsMixin):
        def ensure_skill_ref(self, slug):  # pragma: no cover - must not run
            calls.append(slug)
            return 1

        def _stamp_recalled_sql(self, sql, params):  # pragma: no cover
            calls.append(sql)

    Fake()._touch_skill_recalled("precis-memory-help~2")
    assert calls == []
