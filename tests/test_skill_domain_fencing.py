"""gr572721: skill search and toc default to the bound domain's families.

Child of gr572718 (the domain campaign; the model and resolution live
in ``runtime.scope``, gr572719). On a connection whose domain declares
a ``families`` axis:

* ``search(kind='skill')`` drops hits from families outside the
  domain's serve set — post-rank, like the read fence — and the footer
  counts them and names the ``family=`` door;
* ``get(kind='skill', id='toc')`` and the bare-get index omit those
  rows with the same counted footer;
* an explicit ``args={'family': '<f>'}`` outside the set still works —
  it is the door past the default;
* the "narrow to a family" hint and the empty-search family rows offer
  only in-domain families;
* the cross-cutting five (core/work/evidence/memory/files) are never
  fenced — ``runtime.scope.ALWAYS_IN_FAMILIES`` rides inside
  ``Domain.families`` at construction.

``'all'`` — no ``families`` axis — is byte-identical to the pre-domain
surface.
"""

from __future__ import annotations

import pytest

import precis.runtime.scope as scope_mod
from precis.dispatch import Hub
from precis.handlers.skill import SkillHandler
from precis.runtime.scope import Domain
from tests._skill_corpus import _install_corpus


@pytest.fixture(autouse=True)
def _no_domain_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(scope_mod.DOMAIN_ENV, raising=False)


@pytest.fixture
def pcb_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind ``PRECIS_DOMAIN=pcb`` — declared families ``{pcb}`` plus the
    always-in five; ``se`` sits outside its serve set."""
    monkeypatch.setitem(
        scope_mod.DOMAINS, "pcb", Domain(name="pcb", families=frozenset({"pcb"}))
    )
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "pcb")


@pytest.fixture
def skill() -> SkillHandler:
    return SkillHandler(hub=Hub())


_PCB_SKILL = (
    "---\nid: precis-pcb-boss-help\ntitle: P\nsummary: pcb boss things\n---\n"
    "# P\n\nhow to place a boss pad on a board\n"
)
_FILES_SKILL = (
    "---\nid: precis-files-boss-help\ntitle: F\nsummary: file boss things\n---\n"
    "# F\n\nhow to file away a boss report\n"
)
_SE_SKILL = (
    "---\nid: precis-se-boss-help\ntitle: S\nsummary: se boss things\n---\n"
    "# S\n\nhow to size a screw boss for the load\n"
)
_SE_GIRDER_SKILL = (
    "---\nid: precis-se-girder-help\ntitle: G\nsummary: se girder things\n---\n"
    "# G\n\nhow to read a span table\n"
)


def _install(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_corpus(
        monkeypatch,
        {
            "precis-pcb-boss-help": _PCB_SKILL,
            "precis-files-boss-help": _FILES_SKILL,
            "precis-se-boss-help": _SE_SKILL,
        },
    )


# ── search ───────────────────────────────────────────────────────────


def test_search_hides_out_of_domain_hits_and_names_the_door(
    skill: SkillHandler, pcb_domain: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch)
    body = skill.search(q="boss").body
    assert "precis-se-boss-help" not in body
    assert "precis-pcb-boss-help" in body
    # ``files`` is an always-in family — the domain can't fence it.
    assert "precis-files-boss-help" in body
    assert "1 skill hidden — domain 'pcb'" in body
    # The door: an explicit family arg reaches the hidden family.
    assert "args={'family': 'se'}" in body


def test_search_explicit_family_outside_domain_still_works(
    skill: SkillHandler, pcb_domain: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch)
    body = skill.search(q="boss", family="se").body
    assert "precis-se-boss-help" in body
    assert "hidden" not in body


def test_search_all_domain_output_is_unchanged(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch)
    baseline = skill.search(q="boss").body
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "all")
    assert skill.search(q="boss").body == baseline
    assert "precis-se-boss-help" in baseline


def test_search_narrow_hint_offers_only_in_domain_families(
    skill: SkillHandler, pcb_domain: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch)
    body = skill.search(q="boss").body
    fam_line = next(
        line for line in body.splitlines() if line.startswith("families in these hits:")
    )
    # Families render as ``<fam> <n>`` tokens — 'se' must not appear.
    assert "se" not in fam_line.split(":", 1)[1].split()
    narrow = next(l for l in body.splitlines() if "narrow to a family" in l)
    assert "'se'" not in narrow


def test_empty_search_family_rows_skip_out_of_domain(
    skill: SkillHandler, pcb_domain: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(monkeypatch, {"precis-se-girder-help": _SE_GIRDER_SKILL})
    body = skill.search(q="girder").body
    assert "precis-se-girder-help" not in body
    # The only family the query's slug words match is fenced — no
    # "try this family" offer survives to name it.
    assert "try this family" not in body
    assert "args={'by': 'family'}" in body


# ── toc + index ──────────────────────────────────────────────────────


def test_toc_hides_out_of_domain_and_names_the_door(
    skill: SkillHandler, pcb_domain: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch)
    body = skill.get(id="toc").body
    assert "precis-se-boss-help" not in body
    assert "precis-pcb-boss-help" in body
    assert "precis-files-boss-help" in body
    assert "1 skill hidden — domain 'pcb'" in body
    assert "args={'family': 'se'}" in body


def test_toc_explicit_family_outside_domain_still_works(
    skill: SkillHandler, pcb_domain: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch)
    body = skill.get(id="toc", family="se").body
    assert "precis-se-boss-help" in body
    assert "hidden" not in body


def test_toc_all_domain_output_is_unchanged(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch)
    baseline = skill.get(id="toc").body
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "all")
    assert skill.get(id="toc").body == baseline


def test_index_hides_out_of_domain_and_names_the_door(
    skill: SkillHandler, pcb_domain: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch)
    body = skill.get().body
    assert "precis-se-boss-help" not in body
    assert "precis-pcb-boss-help" in body
    assert "1 skill hidden — domain 'pcb'" in body


# ── helpers ──────────────────────────────────────────────────────────


def test_domain_hidden_note_names_the_single_family_door() -> None:
    from precis.handlers.skill import _domain_hidden_note

    assert _domain_hidden_note({"precis-se-boss-help"}, "pcb") == (
        "1 skill hidden — domain 'pcb' (args={'family': 'se'} shows a hidden family)"
    )
