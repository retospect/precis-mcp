"""Tests for :mod:`precis.startup_skills` + the banner integration.

Phase 3 of the cold-start token budget design
(``docs/design/mcp-cold-start-token-budget.md``). Covers:

- :func:`precis.startup_skills.parse` parsing of the comma-list env
  value (whitespace, empties, duplicates).
- :func:`precis.startup_skills.resolve` cap-and-lookup behaviour
  (unknown drop-with-notice, drop-tail truncation, cap disabling).
- :func:`precis.startup_skills.format_banner` rendering of the
  banner notice lines.
- Integration with :func:`precis.server._build_instructions` so a
  configured env var surfaces in ``serverInfo.instructions``.
- Integration with the prompt tagger so pinned skills land with a
  ``pinned`` tag in ``prompts/list``.
"""

from __future__ import annotations

import pytest

from precis import startup_skills
from precis.startup_skills import Resolution

# ---------------------------------------------------------------------------
# parse()
# ---------------------------------------------------------------------------


def test_parse_handles_none() -> None:
    """Operator hasn't set the env var → empty list, zero allocation."""
    assert startup_skills.parse(None) == []


def test_parse_handles_empty_string() -> None:
    """An explicitly empty env value is treated the same as unset."""
    assert startup_skills.parse("") == []


def test_parse_handles_single_slug() -> None:
    assert startup_skills.parse("precis-paper-help") == ["precis-paper-help"]


def test_parse_handles_multiple_slugs_preserves_order() -> None:
    """Operator-stated order survives parsing — drop-tail truncation
    relies on this.
    """
    out = startup_skills.parse("precis-search-help,precis-paper-help,precis-tag-help")
    assert out == ["precis-search-help", "precis-paper-help", "precis-tag-help"]


def test_parse_tolerates_whitespace_around_commas() -> None:
    out = startup_skills.parse(" precis-search-help ,  precis-paper-help  ")
    assert out == ["precis-search-help", "precis-paper-help"]


def test_parse_drops_empty_entries() -> None:
    """``a,,b`` (typo / trailing comma) becomes ``[a, b]``."""
    assert startup_skills.parse("precis-paper-help,,precis-tag-help,") == [
        "precis-paper-help",
        "precis-tag-help",
    ]


def test_parse_dedupes_preserving_first_occurrence() -> None:
    """First occurrence wins so the operator's priority order survives."""
    out = startup_skills.parse("precis-paper-help,precis-tag-help,precis-paper-help")
    assert out == ["precis-paper-help", "precis-tag-help"]


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------


def _fake_loader_factory(catalogue: dict[str, str]):
    def _loader(slug: str) -> str | None:
        return catalogue.get(slug)

    def _known() -> list[str]:
        return list(catalogue)

    return _loader, _known


def test_resolve_passes_through_known_slugs_under_cap() -> None:
    loader, known = _fake_loader_factory(
        {"a": "x" * 100, "b": "y" * 200, "c": "z" * 50}
    )
    result = startup_skills.resolve(
        ["a", "b", "c"], cap_kb=1, loader=loader, known=known
    )
    assert result.pinned == ("a", "b", "c")
    assert result.unknown == ()
    assert result.truncated == ()
    assert result.cap_kb == 1


def test_resolve_separates_unknown_slugs() -> None:
    """Unknown slugs land in ``unknown`` without affecting the
    cap-accounting for known ones.
    """
    loader, known = _fake_loader_factory({"a": "x" * 50})
    result = startup_skills.resolve(
        ["a", "missing-one", "b"], cap_kb=10, loader=loader, known=known
    )
    assert result.pinned == ("a",)
    assert result.unknown == ("missing-one", "b")
    assert result.truncated == ()


def test_resolve_drops_tail_when_over_cap() -> None:
    """Once we trip the cap, every remaining valid slug joins
    ``truncated`` — we don't backfill from later smaller entries
    (would silently invert operator priority).
    """
    # 600 B + 600 B + 200 B = 1400 B. Cap = 1 KB = 1024 B.
    # a fits (600 used). b would push to 1200 > 1024 → truncated.
    # c is small but order-after-b → also truncated (drop-tail).
    loader, known = _fake_loader_factory(
        {"a": "x" * 600, "b": "y" * 600, "c": "z" * 200}
    )
    result = startup_skills.resolve(
        ["a", "b", "c"], cap_kb=1, loader=loader, known=known
    )
    assert result.pinned == ("a",)
    assert result.truncated == ("b", "c")
    assert result.unknown == ()


def test_resolve_cap_zero_disables_truncation() -> None:
    """Operator opt-out: cap_kb=0 → no truncation, any body size accepted."""
    loader, known = _fake_loader_factory(
        {"big": "x" * 1_000_000, "huge": "y" * 1_000_000}
    )
    result = startup_skills.resolve(
        ["big", "huge"], cap_kb=0, loader=loader, known=known
    )
    assert result.pinned == ("big", "huge")
    assert result.truncated == ()


def test_resolve_records_cap_in_result() -> None:
    """The cap-in-force flows back into Resolution so the banner can
    cite it without re-reading config.
    """
    loader, known = _fake_loader_factory({"a": "x"})
    result = startup_skills.resolve(["a"], cap_kb=42, loader=loader, known=known)
    assert result.cap_kb == 42


# ---------------------------------------------------------------------------
# format_banner()
# ---------------------------------------------------------------------------


def test_format_banner_empty_resolution_renders_nothing() -> None:
    """Operator with no env var → zero unconditional banner bytes."""
    assert startup_skills.format_banner(Resolution()) == ""


def test_format_banner_pinned_only() -> None:
    out = startup_skills.format_banner(
        Resolution(pinned=("precis-search-help", "precis-paper-help"))
    )
    assert out == "Pinned skills: precis-search-help, precis-paper-help."


def test_format_banner_surfaces_unknown_slugs() -> None:
    """Operator typo → warning notice, even with no successful pins."""
    out = startup_skills.format_banner(Resolution(unknown=("foo", "bar")))
    assert "unknown skill ids: foo, bar." in out


def test_format_banner_surfaces_truncation_with_cap() -> None:
    """Cap was hit → notice cites the cap so operator can recalibrate."""
    out = startup_skills.format_banner(
        Resolution(truncated=("precis-tex-help",), cap_kb=50)
    )
    assert "truncated (50 KB cap)" in out
    assert "precis-tex-help" in out


def test_format_banner_combines_all_three_sections() -> None:
    """Pinned + unknown + truncated: every section gets its own line."""
    out = startup_skills.format_banner(
        Resolution(
            pinned=("precis-search-help",),
            unknown=("typo-skill",),
            truncated=("precis-python-help",),
            cap_kb=25,
        )
    )
    lines = out.split("\n")
    assert len(lines) == 3
    assert lines[0].startswith("Pinned skills")
    assert "typo-skill" in lines[1]
    assert "precis-python-help" in lines[2]
    assert "25 KB" in lines[2]


# ---------------------------------------------------------------------------
# integration with the cold-start banner
# ---------------------------------------------------------------------------


def _runtime_with_startup_skills(
    *, startup_skills_value: str | None = None, cap_kb: int = 50
):
    """Build a PrecisRuntime with a configured PRECIS_STARTUP_SKILLS
    value and a fake hub. The hub's ``kinds`` is irrelevant to the
    pinned-skill rendering, so we leave it empty.
    """
    from precis.config import PrecisConfig
    from precis.runtime import PrecisRuntime

    config = PrecisConfig(
        startup_skills=startup_skills_value,
        startup_skills_cap_kb=cap_kb,
    )

    class _FakeHub:
        kinds: set[str] = set()

    return PrecisRuntime(config=config, hub=_FakeHub())  # type: ignore[arg-type]


def test_build_instructions_omits_banner_when_env_var_unset() -> None:
    """Operator who hasn't set PRECIS_STARTUP_SKILLS pays zero
    unconditional banner bytes — neither pinned-line nor warning.
    """
    from precis import server

    runtime = _runtime_with_startup_skills(startup_skills_value=None)
    out = server._build_instructions(runtime)
    assert "Pinned skills" not in out
    assert "PRECIS_STARTUP_SKILLS" not in out


def test_build_instructions_includes_pinned_skills_when_configured() -> None:
    """A valid env var surfaces the pinned-skills banner line so the
    agent learns about the operator's curated starting set on the
    very first message.
    """
    from precis import server

    runtime = _runtime_with_startup_skills(
        startup_skills_value="precis-search-help,precis-overview"
    )
    out = server._build_instructions(runtime)
    assert "Pinned skills: precis-search-help, precis-overview." in out


def test_build_instructions_surfaces_unknown_slugs_in_banner() -> None:
    """Operator typo gets surfaced inline so they don't have to
    grep stderr — the agent can advise them on next connect.
    """
    from precis import server

    runtime = _runtime_with_startup_skills(
        startup_skills_value="precis-search-help,does-not-exist"
    )
    out = server._build_instructions(runtime)
    assert "unknown skill ids: does-not-exist" in out
    # The valid one still pins.
    assert "Pinned skills: precis-search-help." in out


# ---------------------------------------------------------------------------
# integration with the prompt tagger
# ---------------------------------------------------------------------------


def test_skill_prompt_tags_marks_pinned_slugs() -> None:
    """Pinned slugs land with a ``pinned`` tag so modern MCP clients
    can prioritise them in their prompt picker.
    """
    from precis.mcp_modalities import _skill_prompt_tags

    pinned = frozenset({"precis-search-help"})

    tags_pinned = _skill_prompt_tags("precis-search-help", pinned_slugs=pinned)
    assert "pinned" in tags_pinned

    tags_unpinned = _skill_prompt_tags("precis-overview", pinned_slugs=pinned)
    assert "pinned" not in tags_unpinned


def test_skill_prompt_tags_default_excludes_pinned() -> None:
    """Default behaviour (no env var, no pinned set) gives no slug
    the ``pinned`` tag.
    """
    from precis.mcp_modalities import _skill_prompt_tags

    tags = _skill_prompt_tags("precis-search-help")
    assert "pinned" not in tags


# ---------------------------------------------------------------------------
# guard rails: actual shipped skills resolve
# ---------------------------------------------------------------------------


def test_resolve_buckets_kind_unavailable_slugs() -> None:
    """Phase 4 cross-check: a pinned slug whose front-matter targets
    an unavailable kind moves from ``pinned`` to ``kind_unavailable``.
    """
    body_with_kind = (
        "---\ntitle: Patent help\napplies-to: search (kind='patent', q=...)\n---\nbody"
    )
    loader, known = _fake_loader_factory(
        {
            "precis-patent-help": body_with_kind,
            "precis-search-help": "---\ntitle: Search\n---\nbody",
        }
    )
    result = startup_skills.resolve(
        ["precis-patent-help", "precis-search-help"],
        cap_kb=10,
        unavailable_kinds=frozenset({"patent"}),
        loader=loader,
        known=known,
    )
    assert result.pinned == ("precis-search-help",)
    assert result.kind_unavailable == ("precis-patent-help",)


def test_resolve_no_cross_check_when_kind_set_empty() -> None:
    """No unavailable_kinds → all valid slugs flow into ``pinned``
    regardless of their front-matter. Backwards-compatible default."""
    body_with_kind = "---\napplies-to: get (kind='patent', id='ep1234567b1')\n---\n"
    loader, known = _fake_loader_factory({"precis-patent-help": body_with_kind})
    result = startup_skills.resolve(
        ["precis-patent-help"], cap_kb=10, loader=loader, known=known
    )
    assert result.pinned == ("precis-patent-help",)
    assert result.kind_unavailable == ()


def test_format_banner_surfaces_kind_unavailable() -> None:
    """The banner gets a fourth notice line when a pinned skill
    targets an unavailable kind."""
    out = startup_skills.format_banner(
        Resolution(kind_unavailable=("precis-patent-help",))
    )
    assert "skills for unavailable kinds: precis-patent-help" in out


def test_build_instructions_surfaces_pinned_skill_kind_unavailable() -> None:
    """End-to-end: PRECIS_STARTUP_SKILLS pins a skill whose subject
    kind is in PRECIS_KINDS_DISABLED → banner carries the warning."""
    from precis import server
    from precis.config import PrecisConfig
    from precis.kind_gate import Loadability
    from precis.runtime import PrecisRuntime

    config = PrecisConfig(
        startup_skills="precis-patent-help",
        startup_skills_cap_kb=50,
        kinds_disabled="patent",
    )

    class _FakeHub:
        kinds: set[str] = set()
        loadabilities = {
            "patent": Loadability("patent", False, "prohibited"),
        }

    rt = PrecisRuntime(config=config, hub=_FakeHub())  # type: ignore[arg-type]
    out = server._build_instructions(rt)
    assert "Kinds unavailable: patent (prohibited)." in out
    assert "skills for unavailable kinds: precis-patent-help" in out


@pytest.mark.parametrize(
    "slug",
    [
        "precis-overview",
        "precis-search-help",
        "precis-get-help",
        "precis-edit-help",
        "precis-startup-skills-help",
    ],
)
def test_real_skill_slugs_resolve(slug: str) -> None:
    """Smoke test: the slugs we recommend in
    ``precis-startup-skills-help`` actually resolve in this build.
    Catches a rename / delete that would silently break the example.
    """
    result = startup_skills.resolve([slug], cap_kb=0)
    assert result.pinned == (slug,), (
        f"slug {slug!r} no longer resolves; update precis-startup-skills-help "
        f"and the design doc"
    )
