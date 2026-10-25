"""SkillHandler state — client-held rev freshness, status surface, breadcrumbs and fleet utilisation (split out of test_skill.py, gr515135)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.handlers.skill import SkillHandler
from tests._skill_corpus import _install_corpus, _md


@pytest.fixture
def skill() -> SkillHandler:
    """SkillHandler doesn't need a store; an empty hub is enough."""
    return SkillHandler(hub=Hub())


_NEEDS_SIGHUP = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: precis.install_watchdog reads signal.SIGHUP at import",
)


# ── client-held rev tag (gr477798) ──────────────────────────────────────


def _rev_of(body: str) -> str:
    last = body.rstrip().splitlines()[-1]
    assert last.startswith("skill rev "), last
    return last.removeprefix("skill rev ")


def test_first_read_is_full_with_trailing_rev(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(monkeypatch, {"a": _md(body="body")})
    first = skill.get(id="a")
    assert "body" in first.body
    rev = _rev_of(first.body)
    assert len(rev) == 8 and int(rev, 16) >= 0


def test_repeat_read_without_have_is_full(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr477798 regression: shared session, repeat read -> still full."""
    from precis import serve_ledger

    _install_corpus(monkeypatch, {"a": _md(body="body")})
    with serve_ledger.session_scope(object.__new__(type("S", (), {}))):
        skill.get(id="a")
        second = skill.get(id="a")
    assert "unchanged" not in second.body
    assert "body" in second.body
    assert second.body.rstrip().splitlines()[-1].startswith("skill rev ")


def test_have_current_rev_returns_stub(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(monkeypatch, {"a": _md(body="body")})
    rev = _rev_of(skill.get(id="a").body)
    out = skill.get(id="a", have=rev)
    assert f"you passed have={rev}: unchanged since you read it" in out.body
    assert "get(kind='skill', id='a', full=true)" in out.body
    assert "Subagents share" not in out.body
    assert "body" not in out.body.replace("unchanged", "")


def test_have_stale_or_malformed_returns_full(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(monkeypatch, {"a": _md(body="body")})
    for have in ("deadbeef", "not-a-rev", "  "):
        out = skill.get(id="a", have=have)
        assert "unchanged" not in out.body
        assert "body" in out.body


def test_full_overrides_have(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(monkeypatch, {"a": _md(body="body")})
    rev = _rev_of(skill.get(id="a").body)
    out = skill.get(id="a", have=rev, full=True)
    assert "unchanged" not in out.body
    assert "body" in out.body


def test_content_change_changes_rev(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    import precis.handlers.skill as skill_mod

    _install_corpus(monkeypatch, {"a": _md(body="body v1")})
    rev1 = _rev_of(skill.get(id="a").body)
    monkeypatch.setattr(skill_mod, "_SKILLS_MAP_CACHE", {"a": _md(body="body v2")})
    monkeypatch.setattr(skill_mod, "_SKILL_GRAPH_CACHE", None)
    out = skill.get(id="a", have=rev1)
    assert "body v2" in out.body
    assert _rev_of(out.body) != rev1


def test_serve_logs_have_outcome(
    skill: SkillHandler,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _install_corpus(monkeypatch, {"a": _md(body="body")})
    with caplog.at_level("INFO", logger="precis.handlers.skill"):
        rev = _rev_of(skill.get(id="a").body)
        skill.get(id="a", have="deadbeef")
        skill.get(id="a", have=rev)
    msgs = [r.getMessage() for r in caplog.records if "skill serve" in r.getMessage()]
    assert [m.rsplit("have=", 1)[1] for m in msgs] == ["absent", "stale", "matched"]


# ── precis-status: previous-exit breadcrumb (gr341515) ─────────────────


@_NEEDS_SIGHUP
def test_status_silent_when_no_prior_breadcrumb(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common case: no previous server ended notably, so the status
    body carries no "previous server exited" line at all."""
    from precis import install_watchdog

    monkeypatch.setattr(install_watchdog, "consume_last_exit_breadcrumb", lambda: None)
    out = skill.get(id="precis-status")
    assert "previous server exited" not in out.body


@_NEEDS_SIGHUP
def test_status_surfaces_install_swapped_breadcrumb(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watchdog's exit must read as one line naming both
    fingerprints — matching gr341515's worked example verbatim."""
    from precis import install_watchdog

    monkeypatch.setattr(
        install_watchdog,
        "consume_last_exit_breadcrumb",
        lambda: {
            "written_at": "2026-09-14T22:19:41Z",
            "reason": "install-swapped",
            "old_fingerprint": "0e3eac8b",
            "new_fingerprint": "0e15f509",
            "git_sha": "abc123",
        },
    )
    out = skill.get(id="precis-status")
    assert (
        "previous server exited 2026-09-14T22:19:41Z — "
        "install swapped 0e3eac8b→0e15f509" in out.body
    )


@_NEEDS_SIGHUP
def test_status_surfaces_crash_breadcrumb_with_detail(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine crash must read distinguishably from a watchdog exit —
    the exception detail carries through into the rendered line."""
    from precis import install_watchdog

    monkeypatch.setattr(
        install_watchdog,
        "consume_last_exit_breadcrumb",
        lambda: {
            "written_at": "2026-09-14T23:00:00Z",
            "reason": "crash",
            "detail": "ValueError: boom",
        },
    )
    out = skill.get(id="precis-status")
    assert "previous server exited 2026-09-14T23:00:00Z — crashed" in out.body
    assert "ValueError: boom" in out.body


@_NEEDS_SIGHUP
def test_status_surfaces_normal_exit_breadcrumb(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis import install_watchdog

    monkeypatch.setattr(
        install_watchdog,
        "consume_last_exit_breadcrumb",
        lambda: {"written_at": "2026-09-14T23:00:00Z", "reason": "exit"},
    )
    out = skill.get(id="precis-status")
    assert "previous server exited 2026-09-14T23:00:00Z — exited normally" in out.body


@_NEEDS_SIGHUP
def test_status_breadcrumb_is_consumed_not_repeated(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The renderer must call through to the real consume-on-read
    contract (not cache its own copy) — a second ``precis-status`` call
    against the real breadcrumb store must not repeat the line. This
    exercises the actual
    :func:`install_watchdog.consume_last_exit_breadcrumb` (not a stub)
    against a tmp breadcrumb path."""
    import json

    from precis import install_watchdog

    target = tmp_path / "last-exit.json"
    target.write_text(
        json.dumps({"written_at": "2026-09-14T23:00:00Z", "reason": "exit"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(install_watchdog, "_breadcrumb_path", lambda: target)

    # The real consumer takes the breadcrumb off disk once per PROCESS and
    # then serves it once per session, so what this test sees depends on
    # what else already ran in this xdist worker: any earlier test that
    # reached the real consumer leaves ``_breadcrumb_taken`` True with a
    # cached None, and this one then reads an empty body. That is why it
    # passed in isolation and failed on whichever CI shard happened to pair
    # it with such a test. Reset on the way in, and again on the way out so
    # this test does not do to a later one what was being done to it.
    install_watchdog._reset_breadcrumb_state_for_tests()
    try:
        first = skill.get(id="precis-status")
        assert "previous server exited" in first.body

        second = skill.get(id="precis-status")
        assert "previous server exited" not in second.body
    finally:
        install_watchdog._reset_breadcrumb_state_for_tests()


@_NEEDS_SIGHUP
def test_status_names_kinds_that_failed_their_gate(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plugin that would not import is absent from the registered list,
    and the CLI path never shows the MCP banner — so status must name it
    with its reason, or an operator cannot tell a kind went dark
    (2026-10-02: pathway vanished from a local status without a word)."""
    from precis import install_watchdog
    from precis.kind_gate import Loadability

    monkeypatch.setattr(install_watchdog, "consume_last_exit_breadcrumb", lambda: None)
    hub = Hub()
    # Plant the hub manually since we bypassed Handler._register_with.
    skill.hub = hub
    assert "Kinds unavailable" not in skill.get(id="precis-status").body
    hub.loadabilities["pathway"] = Loadability(
        kind="pathway", loaded=False, reason="plugin failed to import: autocatpath"
    )
    hub.loadabilities["route"] = Loadability(kind="route", loaded=True)
    body = skill.get(id="precis-status").body
    assert (
        "**Kinds unavailable: pathway (plugin failed to import: autocatpath).**" in body
    )
    assert "route (" not in body


@_NEEDS_SIGHUP
def test_status_renders_dark_switches_table(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr477799: precis-status names each dark service's lit/dark state."""
    from unittest.mock import MagicMock

    from precis import install_watchdog
    from precis.handlers import skill as skill_mod
    from precis.workers import service_config

    monkeypatch.setattr(install_watchdog, "consume_last_exit_breadcrumb", lambda: None)
    monkeypatch.setattr(
        service_config,
        "dark_switch_states",
        lambda store: [
            {"service": "conflict_search", "state": "dark", "setting": "no row"}
        ],
    )
    out = "\n".join(skill_mod._render_dark_switches(MagicMock()))
    assert "conflict_search" in out and "dark" in out
    assert skill_mod._render_dark_switches(None) == [
        "_stateless build — no service_config to read_"
    ]
    assert "**Dark switches**" in skill.get(id="precis-status").body


def test_fleet_utilisation_renders_seeded_pass_durations(store) -> None:
    """precis-status shows busy/polling shares from worker_logs duration_ms."""
    import json

    from precis.handlers import skill as skill_mod

    with store.pool.connection() as conn:
        for claimed, ms in ((3, 360_000), (0, 36_000)):
            conn.execute(
                "INSERT INTO worker_logs "
                "(ts, host, process, pass, level, message, payload) "
                "VALUES (now(), 'fu-host', 'fu-proc', 'runner', 'INFO', 'm', "
                "%s::jsonb)",
                (json.dumps({"handler": "x", "claimed": claimed, "duration_ms": ms}),),
            )
        conn.commit()
    out = "\n".join(skill_mod._render_fleet_utilisation(store))
    row = next(line for line in out.splitlines() if "fu-host" in line)
    assert "fu-proc" in row and "10%" in row and "1%" in row and "2" in row
    assert "Lanes idle beside queued work:" in out
    assert "busy = time in passes that claimed work" in out
    assert skill_mod._render_fleet_utilisation(None) == [
        "_stateless build — no worker_logs to read_"
    ]
