"""SkillHandler availability gates — loadability marks, requires frontmatter and kind gates (split out of test_skill.py, gr515135)."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.skill import SkillHandler
from tests._skill_corpus import _install_corpus


@pytest.fixture
def skill() -> SkillHandler:
    """SkillHandler doesn't need a store; an empty hub is enough."""
    return SkillHandler(hub=Hub())


# ── search hides unwired skills + surfaces escalation hint ───────────


class _Loadability:
    def __init__(self, loaded: bool) -> None:
        self.loaded = loaded


class _NoFileKindsHub:
    """Duck-typed hub that deliberately omits the file kinds
    (markdown / plaintext / python / tex) so their help skills are
    treated as unwired. The file kinds appear in ``loadabilities``
    (registered, but loaded=False) so the availability gate
    recognises them as known-but-disabled."""

    @property
    def kinds(self) -> list[str]:
        return ["calc", "paper", "memory"]

    loadabilities: dict[str, _Loadability] = {
        "calc": _Loadability(True),
        "paper": _Loadability(True),
        "memory": _Loadability(True),
        "markdown": _Loadability(False),
        "plaintext": _Loadability(False),
        "tex": _Loadability(False),
        "python": _Loadability(False),
    }


_UNWIRED_SLUGS = (
    "precis-markdown-help",
    "precis-plaintext-help",
    "precis-tex-help",
    "precis-python-help",
)


def test_search_marks_unwired_skills_inline(skill: SkillHandler) -> None:
    """Unwired skills stay in the rows, each suffixed
    ``[not configured: ...]`` (gr51206); no row for an unwired slug
    lacks the marker, and the old footer is gone."""
    skill.hub = _NoFileKindsHub()
    body = skill.search(q="edit").body
    rows = [ln for ln in body.splitlines() if ln.startswith("precis-")]
    unwired = [
        ln for ln in rows if ln.split("\t", 1)[0].split(" ")[0] in _UNWIRED_SLUGS
    ]
    assert unwired, body
    assert all("[not configured:" in ln for ln in unwired), unwired
    assert "Also matched in unwired skills" not in body


def test_search_configured_only_drops_unwired(skill: SkillHandler) -> None:
    skill.hub = _NoFileKindsHub()
    body = skill.search(q="edit", configured_only=True).body
    assert "[not configured" not in body
    for ln in body.splitlines():
        assert ln.split("\t", 1)[0].split(" ")[0] not in _UNWIRED_SLUGS, ln


def test_requires_gate_env_and_module(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis.handlers.skill import _unmet_requirements
    from precis.utils.skill_common import parse_frontmatter

    fm = parse_frontmatter(
        "---\nid: x\nrequires: [env=PRECIS_TEST_REQ_XYZ, module=json, "
        "module=no_such_mod_xyz]\n---\n"
    )
    assert fm.requires == (
        "env=PRECIS_TEST_REQ_XYZ",
        "module=json",
        "module=no_such_mod_xyz",
    )
    monkeypatch.delenv("PRECIS_TEST_REQ_XYZ", raising=False)
    assert _unmet_requirements(fm) == [
        "env=PRECIS_TEST_REQ_XYZ",
        "module=no_such_mod_xyz",
    ]
    monkeypatch.setenv("PRECIS_TEST_REQ_XYZ", "1")
    assert _unmet_requirements(fm) == ["module=no_such_mod_xyz"]


def test_gap_label_carries_loadability_reason() -> None:
    from precis.handlers.skill import _availability_gap_parts

    class _L:
        loaded = False
        reason = "missing PRECIS_PATENT_RAW_ROOT"

    class _Hub:
        kinds: list[str] = []
        loadabilities = {"patent": _L()}

    parts = _availability_gap_parts("precis-patent-power", hub=_Hub())
    assert parts is not None
    assert "missing PRECIS_PATENT_RAW_ROOT" in parts[0]
    assert "missing PRECIS_PATENT_RAW_ROOT" in parts[1]


def test_search_omits_escalation_line_when_all_matches_wired(
    skill: SkillHandler,
) -> None:
    """A query that matches only wired skills must not emit the
    escalation line (no signal to convey)."""
    # Default hub from the fixture has no loadabilities, so
    # availability is determined by `kinds`. Hub() registers no
    # kinds, but verb-help slugs are exempt — pick a query that's
    # owned by a verb-help skill (precis-search-help) and confirm
    # the escalation footer is absent.
    out = skill.search(q="search")
    assert "[not configured" not in out.body


def test_availability_gate_prefers_kinds_frontmatter_over_applies_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/backlog/skill-graph.md slice 1: ``kinds:`` frontmatter is
    authoritative when present, even when ``applies-to:`` names a
    different (wired) kind — the legacy regex must not override it."""
    from precis.handlers.skill import _availability_gap

    _install_corpus(
        monkeypatch,
        {
            "precis-widget-help": (
                "---\nflavor: reference\nkinds:\n  - nonexistent-kind\n"
                "applies-to: get (kind='skill')\n---\n# T\nbody\n"
            ),
        },
    )
    # ``nonexistent-kind`` must be *known* to the gate (registered, even
    # if disabled) for it to fire at all — see ``_kind_is_known``.
    hub = _NoFileKindsHub()
    hub.loadabilities = dict(hub.loadabilities)
    hub.loadabilities["nonexistent-kind"] = _Loadability(False)
    gap = _availability_gap("precis-widget-help", hub=hub)
    assert gap is not None
    assert "nonexistent-kind" in gap


def test_availability_gate_empty_kinds_list_means_no_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A present-but-empty ``kinds:`` means "no kind gate" — it must
    not fall back to the legacy applies-to/slug inference."""
    from precis.handlers.skill import _availability_gap

    _install_corpus(
        monkeypatch,
        {
            # Slug looks like a kind-help skill for an unwired kind, and
            # applies-to also names one — but an explicit empty kinds:
            # overrides both. (Authored as a bare ``kinds:`` line
            # immediately followed by another key — see
            # ``_skill_common.parse_frontmatter``'s block-list flush;
            # inline ``kinds: []`` is not the same shape.)
            "precis-nonexistent-kind-help": (
                "---\nflavor: reference\nkinds:\n"
                "applies-to: get (kind='nonexistent-kind')\n---\n# T\nbody\n"
            ),
        },
    )
    hub = _NoFileKindsHub()
    hub.loadabilities = dict(hub.loadabilities)
    hub.loadabilities["nonexistent-kind"] = _Loadability(False)
    assert _availability_gap("precis-nonexistent-kind-help", hub=hub) is None
