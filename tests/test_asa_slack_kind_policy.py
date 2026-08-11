"""asa-slack's kind-allowlist policy — the hard enforcement behind "Slack
users can't kick off compute jobs" (not just prompt language)."""

from __future__ import annotations

import importlib

import pytest

from asa_slack.kind_policy import ALLOWED_KINDS, slack_kinds_disabled
from precis.runtime.core import PrecisRuntime


def _known_kind_roster(runtime: PrecisRuntime) -> set[str]:
    """Every kind slug this build's boot considered, loaded or not.

    ``hub.kinds`` alone only counts kinds that actually got constructed —
    in this sandboxed test env, credential-gated built-ins like ``patent``
    (EPO_OPS_*) and ``orcid`` never load, so they'd wrongly read as "not a
    real kind". ``hub.loadabilities`` (populated by ``precis.dispatch._try``
    for every kind it *considered*, per-kind verdict either way) is the
    broader, environment-independent ground truth for "is this a real kind
    in this build's code" that a stale/renamed ``ALLOWED_KINDS`` entry
    should be diffed against.
    """
    return set(runtime.hub.loadabilities) | runtime.hub.kinds


def test_allowed_is_subset_of_live_kind_roster(
    runtime_with_store: PrecisRuntime,
) -> None:
    """A real diff, not a documented gap: every entry in ALLOWED_KINDS must
    still be a kind this build actually registers — a stale/renamed entry
    (e.g. the old ``cron``, never a real kind) fails the build rather than
    silently doing nothing."""
    assert _known_kind_roster(runtime_with_store) >= ALLOWED_KINDS


def test_compute_kinds_are_disabled(runtime_with_store: PrecisRuntime) -> None:
    disabled = set(slack_kinds_disabled().split(","))
    roster = _known_kind_roster(runtime_with_store)
    for kind in ("job", "quest", "cron", "todo", "sandbox_run"):
        # sandbox_run isn't a registered kind (it's a job_type), but a
        # forward-compatible check costs nothing; the real guards are job/
        # quest/todo (cron isn't live either — kept here in case it's ever
        # reintroduced).
        if kind in roster:
            assert kind in disabled, f"{kind} must be disabled for Slack turns"


def test_research_kinds_stay_enabled():
    disabled = set(slack_kinds_disabled().split(","))
    for kind in (
        "paper",
        "patent",
        "citation",
        "semanticscholar",
        "web",
        "websearch",
        "wikipedia",
        "perplexity-research",
        "perplexity-reasoning",
        "memory",
    ):
        assert kind not in disabled, f"{kind} must stay enabled for Slack turns"


def test_disabled_value_is_sorted_and_comma_joined():
    value = slack_kinds_disabled()
    parts = value.split(",")
    assert parts == sorted(parts)
    assert all(parts)  # no empty entries


def test_drifted_kinds_stay_blocked():
    """The four kinds that motivated this fix (gr, 2026-08-11): added to
    the live registry after ALLOWED_KINDS' 2026-07-22 hand-cross-check, so
    the old ``KNOWN_KINDS - ALLOWED_KINDS`` scheme silently enabled them.
    They're deliberately not in ALLOWED_KINDS and must stay disabled."""
    disabled = set(slack_kinds_disabled().split(","))
    for kind in ("pathway", "material", "component", "python"):
        assert kind not in ALLOWED_KINDS
        assert kind in disabled, f"{kind} must be disabled for Slack turns"


def test_a_kind_the_registry_has_never_heard_of_is_not_silently_enabled():
    # A kind this module can't even discover (never registered anywhere in
    # this build) is, by construction, never loadable regardless of
    # PRECIS_KINDS_DISABLED — it simply isn't in the disabled string. That's
    # fine: the fail-closed guarantee is about kinds that *do* exist in the
    # live registry but aren't in ALLOWED_KINDS (see
    # test_drifted_kinds_stay_blocked), not about kinds nobody has coded yet.
    assert "totally-made-up-kind" not in ALLOWED_KINDS
    assert "totally-made-up-kind" not in slack_kinds_disabled().split(",")


def test_builtin_import_failure_refuses_to_compute_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """precis.kind_gate treats absence from PRECIS_KINDS_DISABLED as
    *enabled*, so a built-in handler module that fails to import must not
    be silently skipped (its kinds would silently un-block for Slack) —
    discovery refuses to compute the policy instead. Failures aren't
    cached, so a healthy later call still succeeds."""
    from asa_slack import kind_policy

    kind_policy._discover_live_kinds.cache_clear()
    real_import = importlib.import_module

    def _boom(name: str, package: str | None = None) -> object:
        if name == "precis.handlers.material":
            raise ImportError("simulated missing optional dep")
        return real_import(name, package)

    monkeypatch.setattr(kind_policy.importlib, "import_module", _boom)
    with pytest.raises(RuntimeError, match="precis.handlers.material"):
        kind_policy.slack_kinds_disabled()

    monkeypatch.undo()
    kind_policy._discover_live_kinds.cache_clear()
    assert "material" in slack_kinds_disabled().split(",")
