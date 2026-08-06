"""Deploy-lint for the four planner-coroutine cost caps.

``planner_guardrails.check_parent`` is the only thing standing between a
spinning planner coroutine and an unbounded Claude bill, and every one of
its four caps is read from the environment the deploy templates render.
That makes ``deploy/`` a place where a cap can be switched off without a
single test going red — which is what happened: ``PRECIS_MAX_TICKS`` was
hardcoded to **10000** in every worker template from the day the deploy
tree was authored (92311750). The tick cap is the *first* check, so it
never fired in production for the life of the system; two todos reached
50 and 62 ticks unremarked, and a soft-deleted project burned $291 over
five days before anything objected.

``PRECIS_MAX_TREE_USD`` had the quieter version of the same problem: it
appeared in no template at all, so it ran at an implicit code default no
host could tune and no one reading the deploy could see.

These tests assert the shape, not one blessed number — the values are a
cost/utility call. What they refuse is a cap that is *absent* (invisible
code default) or *effectively disabled* (a number so large it can never
fire), in any of the four render sites.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_DEPLOY = _REPO / "deploy"

#: Every site that renders worker env — both unit flavours for both
#: profiles, plus the collapsed playbook that bypasses the roles.
_RENDER_SITES = (
    "roles/precis_worker/templates/precis-worker.plist.j2",
    "roles/precis_worker/templates/precis-worker.service.j2",
    "roles/precis_worker_agent/templates/precis-worker-agent.plist.j2",
    "roles/precis_worker_agent/templates/precis-worker-agent.service.j2",
    "playbooks/20b-precis-worker-collapsed.yml",
)

#: The four caps in ``check_parent``'s own order. A worker missing any one
#: of them runs that cap at its code default.
_CAPS = (
    "PRECIS_MAX_TICKS",
    "PRECIS_MAX_TODO_USD",
    "PRECIS_MAX_TREE_USD",
    "PRECIS_DAILY_COST_CEILING",
)

#: Above these a cap is disabled in practice, not merely generous. The
#: tick ceiling sits well above observed real convergence (5-13 ticks);
#: the dollar ceiling is "nobody meant to authorise this per day".
_MAX_SANE = {
    "PRECIS_MAX_TICKS": 500.0,
    "PRECIS_MAX_TODO_USD": 100.0,
    "PRECIS_MAX_TREE_USD": 500.0,
    "PRECIS_DAILY_COST_CEILING": 500.0,
}


def _text(site: str) -> str:
    return (_DEPLOY / site).read_text(encoding="utf-8")


#: Chars after a cap name that can hold its rendered value. Wide enough to
#: cross the plist's ``</key>\n<string>`` line break, tight enough not to
#: run into the next cap's block.
_WINDOW = 120


def _windows(text: str, cap: str) -> list[str]:
    """Every render of ``cap`` with the text that follows it.

    The three sites spell the same assignment three ways — systemd
    ``KEY={{ v }}``, plist ``<key>KEY</key><string>{{ v }}</string>``, and
    the playbook's ``'KEY': (v) | string`` — so match on proximity rather
    than on any one syntax.
    """
    return [text[m.end() : m.end() + _WINDOW] for m in re.finditer(cap, text)]


@pytest.mark.parametrize("site", _RENDER_SITES)
@pytest.mark.parametrize("cap", _CAPS)
def test_every_render_site_sets_every_cap(site: str, cap: str) -> None:
    """An omitted cap is not "the default is fine" — it is a brake whose
    setting is invisible from the deploy and untunable per host."""
    assert cap in _text(site), (
        f"{site} does not set {cap}: that cap would run at its code "
        "default, invisible to the deploy layer and untunable in host_vars"
    )


@pytest.mark.parametrize("site", _RENDER_SITES)
@pytest.mark.parametrize("cap", _CAPS)
def test_no_cap_is_defaulted_to_an_unreachable_value(site: str, cap: str) -> None:
    """The regression: ``default(10000)`` on the tick cap.

    A cap set so high it can never trip reads like a configured brake in
    review while behaving exactly like no brake at all.
    """
    found = [
        m.group(1)
        for w in _windows(_text(site), cap)
        if (m := re.search(r"\|\s*default\(\s*([0-9.]+)\s*\)", w))
    ]
    assert found, f"{site}: could not parse a default for {cap}"
    for raw in found:
        value = float(raw)
        assert value <= _MAX_SANE[cap], (
            f"{site} defaults {cap} to {raw}, at or above the "
            f"{_MAX_SANE[cap]} disabled-in-practice threshold — a cap that "
            "cannot fire is not a cap. If this is deliberate, move it to "
            "host_vars and say why."
        )


@pytest.mark.parametrize("site", _RENDER_SITES)
def test_caps_are_host_overridable(site: str) -> None:
    """Each cap must render from a jinja var, not a literal — otherwise
    a per-host budget change means editing the template."""
    text = _text(site)
    for cap in _CAPS:
        var = cap.lower()
        assert any(var in w for w in _windows(text, cap)), (
            f"{site}: {cap} must render from the `{var}` jinja var so "
            "host_vars can override it, not from a literal"
        )
