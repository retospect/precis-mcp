"""Tests for the prompts + resources modality wiring.

Pin the four behaviours the MCP critic's April 2026 re-probe asked
for:

1. **Skills surface as prompts.**  Every skill that passes the
   availability gate (plus the synthesised meta-skills) is reachable
   via ``prompts/list`` / ``prompts/get``.  Body text matches what
   ``get(kind='skill', id=<slug>)`` returns.

2. **Skills surface as enumerated resources.**  ``resources/list``
   contains every available skill at ``precis://skill/<slug>``.

3. **Papers (and other high-cardinality kinds) live behind URI
   templates only — never enumerated.**  ``resources/templates/list``
   advertises ``precis://paper/{id}`` etc.; ``resources/list`` does
   *not* contain individual papers.

4. **`precis-status` synthesised skill probes optional deps.**  The
   body lists each probe with OK / MISSING / ERROR status and an
   install hint per missing entry.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.handlers.skill import SkillHandler
from precis.mcp_modalities import (
    _enumerate_prompt_skills,
    _parse_resource_uri,
    _resource_uri,
    register_resources,
    register_skill_prompts,
)
from precis.runtime import PrecisRuntime
from precis.store import Store

# ---------------------------------------------------------------------------
# Fixture: build a fresh FastMCP for each test so prompts/resources
# don't leak across tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_for_runtime(runtime_with_store: PrecisRuntime):
    """A FastMCP server bound to the runtime, with modalities registered."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("test-precis")
    register_skill_prompts(server, runtime_with_store)
    register_resources(server, runtime_with_store)
    return server


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def test_skill_prompts_register_every_available_skill(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``prompts/list`` carries every skill that passes the
    availability gate, plus the synthesised meta-skills.  No skill
    text is duplicated — bodies come from ``_load_skill`` /
    ``SkillHandler.get`` (the same path the get verb uses).
    """
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("test")
    n = register_skill_prompts(server, runtime_with_store)
    assert n > 0, "expected at least one skill prompt to register"

    listed = asyncio.run(server.list_prompts())
    listed_names = {p.name for p in listed}

    expected = set(_enumerate_prompt_skills(runtime_with_store))
    assert listed_names == expected, (
        f"prompts/list should match the gated skill enumeration; "
        f"diff = {expected.symmetric_difference(listed_names)!r}"
    )

    # Synthesised meta-skills must be reachable through the modality.
    for synth in SkillHandler._SYNTHESIZED_SKILLS:
        assert synth in listed_names, f"synth skill {synth!r} missing"


def test_prompt_get_returns_skill_body(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``prompts/get(name='precis-overview')`` returns the same
    markdown body ``get(kind='skill', id='precis-overview')`` does.
    Single source of truth.
    """
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("test")
    register_skill_prompts(server, runtime_with_store)

    expected = runtime_with_store.dispatch(
        "get", {"kind": "skill", "id": "precis-overview"}
    )
    result = asyncio.run(server.get_prompt("precis-overview", arguments={}))
    rendered = "".join(
        m.content.text for m in result.messages if hasattr(m.content, "text")
    )
    # The prompt-get path wraps the body as a user message; the
    # markdown should be present verbatim.
    assert "precis" in rendered.lower()
    # Sanity: any non-trivial shared substring from the canonical
    # body must appear in the prompt rendering.  We check a stable
    # phrase from the overview rather than full equality, because
    # the prompt machinery may add a wrapper layer.
    overview_lines = [
        line for line in expected.splitlines() if line.strip().startswith("#")
    ]
    assert any(line.strip() in rendered for line in overview_lines)


def test_prompt_get_for_synthesised_status_skill(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``precis-status`` is a synthesised skill — it has no .md file,
    its body is built by probing optional deps at request time.
    The prompt route must hit that synthesised renderer (not return
    "not found")."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("test")
    register_skill_prompts(server, runtime_with_store)

    result = asyncio.run(server.get_prompt("precis-status", arguments={}))
    rendered = "".join(
        m.content.text for m in result.messages if hasattr(m.content, "text")
    )
    assert "precis-status" in rendered.lower()
    assert "Optional-dependency" in rendered or "optional" in rendered.lower()
    # The probe table lists at least sentence-transformers.
    assert "sentence-transformers" in rendered


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


def test_resource_uri_roundtrip() -> None:
    """The URI parser is the inverse of the constructor."""
    assert _resource_uri("paper", "wang2020state") == "precis://paper/wang2020state"
    assert _resource_uri("memory", 42) == "precis://memory/42"
    assert _parse_resource_uri("precis://paper/wang2020state") == (
        "paper",
        "wang2020state",
    )
    assert _parse_resource_uri("precis://memory/42") == ("memory", "42")
    # Block selectors / view paths ride along inside id verbatim.
    assert _parse_resource_uri("precis://paper/wang2020~38") == (
        "paper",
        "wang2020~38",
    )
    with pytest.raises(ValueError):
        _parse_resource_uri("http://wrong-scheme/foo")
    with pytest.raises(ValueError):
        _parse_resource_uri("precis://paper")  # missing id


def test_resources_list_enumerates_skills_only(
    runtime_with_store: PrecisRuntime,
) -> None:
    """Bounded sets in resources/list, high-cardinality kinds in
    templates only.  Specifically: skills appear; papers do not
    (papers can be 1000s — never enumerate)."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("test")
    n_res, n_tpl = register_resources(server, runtime_with_store)
    assert n_res > 0
    assert n_tpl > 0

    resources = asyncio.run(server.list_resources())
    uris = {str(r.uri) for r in resources}

    # Every skill we'd surface as a prompt must also be a resource.
    expected = {
        f"precis://skill/{slug}"
        for slug in _enumerate_prompt_skills(runtime_with_store)
    }
    assert expected == uris, (
        f"resources/list should be exactly the skill set; "
        f"diff = {expected.symmetric_difference(uris)!r}"
    )

    # Critical: NO precis://paper/* URI may appear in
    # resources/list — papers are template-only.
    for uri in uris:
        assert not uri.startswith("precis://paper/"), (
            f"papers must not be enumerated in resources/list; got {uri!r}"
        )


def test_resources_templates_list_advertises_paper_template(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``resources/templates/list`` must surface ``precis://paper/{id}``
    so modern clients can offer slug autocomplete without the server
    enumerating every paper."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("test")
    register_resources(server, runtime_with_store)

    templates = asyncio.run(server.list_resource_templates())
    uri_templates = {t.uriTemplate for t in templates}

    # Paper, memory, todo all need to be reachable as templates.
    assert "precis://paper/{id}" in uri_templates
    assert "precis://memory/{id}" in uri_templates
    assert "precis://todo/{id}" in uri_templates


def test_resource_read_dispatches_to_runtime(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``resources/read`` must round-trip through the runtime so the
    body matches what ``tools/call get(...)`` returns.  Single
    source of truth.
    """
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("test")
    register_resources(server, runtime_with_store)

    expected = runtime_with_store.dispatch(
        "get", {"kind": "skill", "id": "precis-overview"}
    )
    contents = asyncio.run(server.read_resource("precis://skill/precis-overview"))
    bodies = "".join(
        c.content
        for c in contents
        if hasattr(c, "content") and isinstance(c.content, str)
    )
    # Pick a stable substring from the canonical body and assert it
    # surfaces in the resource read.
    assert any(line.strip() in bodies for line in expected.splitlines() if line.strip())


def test_resource_read_numeric_id_kind_coerces(
    runtime_with_store: PrecisRuntime,
) -> None:
    """For numeric-id kinds (memory, todo, …) the URI ``id`` arrives
    as a string from the URI parser; the read path must coerce to
    int before dispatching."""
    from mcp.server.fastmcp import FastMCP

    # Seed a memory so we have a real ref to read.
    runtime_with_store.dispatch("put", {"kind": "memory", "text": "modality probe"})
    # Most-recent ref is what was just inserted.  We don't know its
    # numeric id without a search, so look it up via /recent.
    listing = runtime_with_store.dispatch("get", {"kind": "memory", "id": "/recent"})
    # The listing renders ids as right-aligned integers.  Pull the
    # first integer from the rendered body.
    import re

    m = re.search(r"^\s*(\d+)\s+modality probe", listing, re.MULTILINE)
    assert m is not None, f"expected to find seeded memory in listing: {listing!r}"
    mid = m.group(1)

    server = FastMCP("test")
    register_resources(server, runtime_with_store)

    # Calling the template fn directly: it should str→int coerce.
    contents = asyncio.run(server.read_resource(f"precis://memory/{mid}"))
    bodies = "".join(
        c.content
        for c in contents
        if hasattr(c, "content") and isinstance(c.content, str)
    )
    assert "modality probe" in bodies


# ---------------------------------------------------------------------------
# precis-status synthesised skill — direct render coverage
# ---------------------------------------------------------------------------


def test_precis_status_renders_optional_dep_table(
    runtime_with_store: PrecisRuntime, monkeypatch, tmp_path
) -> None:
    """``get(kind='skill', id='precis-status')`` returns a markdown
    body listing every probe in the optional-deps table with an OK
    / MISSING / ERROR status.  Sentence-transformers must be
    present (we [all]-installed in CI).

    The git lane is pinned to a fresh fake checkout: the test container
    mounts a worktree whose ``.git`` points outside the mount, and an
    unreadable tree is (correctly) a staleness WARN, which is not what
    this test is about.
    """
    sha = "aaaa1111bbbb2222cccc3333dddd4444eeee5555"
    _fake_checkout(tmp_path, sha)
    _clear_build_env(monkeypatch)
    _pin_lane(monkeypatch, source={"git_sha": sha, "source_path": str(tmp_path)})
    body = runtime_with_store.dispatch("get", {"kind": "skill", "id": "precis-status"})
    assert "# precis-status" in body
    assert "sentence-transformers" in body
    # gr343744: fitz (PyMuPDF) is a [paper]-extra dep — the probe table
    # must enumerate it like every other optional dep.
    assert "pymupdf" in body
    assert "**Overall:" in body
    # We test against an env that has [all] installed, so the
    # overall status is OK.  If the test runner ever drops
    # sentence-transformers, this assertion shows where.
    assert "Overall: OK" in body, f"precis-status reports a degraded venv:\n{body}"


def test_precis_status_reports_active_embedder_and_warns_on_mock(
    runtime_with_store: PrecisRuntime,
) -> None:
    """gr249198: the status probe must report the LIVE embedder object's
    backend — not re-derive it from env/config — and must call out
    plainly when that backend is ``mock`` (semantic search is
    non-functional). The shared test fixtures wire ``MockEmbedder``,
    so this exercises the warning branch directly.
    """
    body = runtime_with_store.dispatch("get", {"kind": "skill", "id": "precis-status"})
    assert "**Embedder**" in body
    assert "mock" in body
    assert "non-functional" in body


def test_precis_status_marks_missing_optional_dep(monkeypatch) -> None:
    """When an optional dep is missing, the probe row tags it
    MISSING and surfaces the install hint.  Simulated by
    monkeypatching the probe table to reference a non-existent
    module.
    """
    from precis.handlers import skill as skill_module

    fake_probes = (
        (
            "precis_definitely_does_not_exist_xyz",
            "fake-probe",
            "no kind",
            "pip install nothing",
        ),
    )
    monkeypatch.setattr(skill_module, "_OPTIONAL_DEP_PROBES", fake_probes)

    handler = SkillHandler(hub=Hub())
    body = handler._render_status()
    assert "MISSING" in body
    assert "fake-probe" in body
    assert "pip install nothing" in body
    assert "Overall: DEGRADED" in body


def test_precis_status_build_section_reads_env(monkeypatch) -> None:
    """The Build section surfaces the ``PRECIS_*`` env vars baked in
    at ``docker build`` time. When unset, fields read ``unknown`` so
    a bare ``docker build .`` (or a pip install with no metadata
    plumbing) still produces a well-formed response.
    """
    monkeypatch.setenv("PRECIS_GIT_LAST_TAG", "v8.4.4")
    monkeypatch.setenv("PRECIS_GIT_SHA", "abcdef0123456789")
    monkeypatch.setenv("PRECIS_GIT_DIRTY", "1")
    monkeypatch.setenv("PRECIS_GIT_BRANCH", "main")
    monkeypatch.setenv("PRECIS_BUILD_TIME", "2026-06-05T12:00:00Z")
    # Leave PRECIS_BUILD_HOST / PRECIS_BUILD_USER / etc. untouched —
    # they must render as ``unknown`` without crashing.
    monkeypatch.delenv("PRECIS_BUILD_HOST", raising=False)
    monkeypatch.delenv("PRECIS_GIT_DESCRIBE", raising=False)
    # The git-identity rows come from one lane, and a live checkout
    # outranks the baked env (``_git_identity_lane``). CI runs from a
    # real clone, so blank the checkout lanes: this test is about the
    # image-build lane, which is what the env vars describe.
    from precis.handlers import skill as skill_module

    monkeypatch.setattr(skill_module, "_WATCHED_GIT_INFO", {})
    monkeypatch.setattr(skill_module, "_SOURCE_GIT_INFO", {})
    monkeypatch.setattr(skill_module, "_DIST_GIT_INFO", {})

    handler = SkillHandler(hub=Hub())
    body = handler._render_status()

    assert "**Build**" in body
    assert "v8.4.4" in body
    assert "abcdef0123456789" in body
    assert "2026-06-05T12:00:00Z" in body
    # `version` always populated from precis.__version__ regardless
    # of env state — it's the one Build field a pip install can rely
    # on without a wheel-build hook.
    from precis import __version__

    assert __version__ in body
    # Unset env vars surface honestly as ``unknown`` rather than
    # silently disappearing or crashing the response.
    assert "unknown" in body


def _clear_build_env(monkeypatch) -> None:
    from precis.handlers import skill as skill_mod

    for env_name, _ in skill_mod._BUILD_ENV_KEYS:
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.delenv("PRECIS_CHECKOUT_WATCHDOG", raising=False)
    monkeypatch.delenv("PRECIS_BUILD_AGE_WARN_DAYS", raising=False)


def _pin_lane(monkeypatch, *, source=None, watched=None, dist=None) -> None:
    """Pin every frozen identity lane so a test owns the whole picture."""
    from precis.handlers import skill as skill_mod

    monkeypatch.setattr(skill_mod, "_SOURCE_GIT_INFO", source or {})
    monkeypatch.setattr(skill_mod, "_WATCHED_GIT_INFO", watched or {})
    monkeypatch.setattr(skill_mod, "_DIST_GIT_INFO", dist or {})


def test_precis_status_mounted_checkout_beats_baked_sha(monkeypatch, tmp_path) -> None:
    """gr458061: a baked ``PRECIS_GIT_SHA`` must not win over a live checkout
    under the import path. That is the per-session dev container — image
    with a baked sha, ``/app`` a bind mount of the real checkout — and the
    served files are the mount's. ``served_from`` names the shape
    ``image+mount``; the image's sha stays visible, separately, as
    ``image_sha``.
    """
    from precis.handlers import skill as skill_mod

    live = "1111222233334444555566667777888899990000"
    _fake_checkout(tmp_path, live)
    _clear_build_env(monkeypatch)
    monkeypatch.setenv("PRECIS_GIT_SHA", "bakedsha0000")
    _pin_lane(
        monkeypatch,
        source={
            "git_sha": live,
            "git_sha_short": live[:12],
            "source_path": str(tmp_path),
        },
    )
    rows = dict(skill_mod._collect_build_info())

    assert rows["served_sha"] == live
    assert rows["served_from"] == "image+mount"
    assert rows["git_sha"] == live
    assert rows["git_source"] == "image+mount"
    assert rows["image_sha"] == "bakedsha0000"
    assert rows["source_drift"] == "none"
    assert skill_mod._staleness().warnings() == []


def test_precis_status_image_build_lane_warns_staleness_unknown(monkeypatch) -> None:
    """A baked image with no checkout anywhere: the served sha is the image's,
    ``image_sha`` agrees, and because nothing can be compared the page says
    so as a WARN — overall is not OK. gr458061 comment 6: the stale server
    came back ``Overall: OK`` with no drift row, which read as healthy.
    """
    from precis.handlers import skill as skill_mod

    _clear_build_env(monkeypatch)
    monkeypatch.setenv("PRECIS_GIT_SHA", "bakedsha0000")
    _pin_lane(monkeypatch)
    rows = dict(skill_mod._collect_build_info())

    assert rows["served_sha"] == "bakedsha0000"
    assert rows["served_from"] == "image-build"
    assert rows["image_sha"] == "bakedsha0000"
    assert rows["source_drift"] == "unknown"

    body = SkillHandler(hub=Hub())._render_status()
    assert "WARN staleness unknown" in body
    assert "Overall: WARN" in body
    assert "Overall: OK" not in body


def test_precis_status_working_tree_lane_is_fresh_without_warnings(
    monkeypatch, tmp_path
) -> None:
    """A local checkout at the sha it imported: ``served_from: working-tree``,
    ``image_sha: none``, drift ``none``, no WARN, overall OK."""
    from precis.handlers import skill as skill_mod

    sha = "aaaa1111bbbb2222cccc3333dddd4444eeee5555"
    _fake_checkout(tmp_path, sha)
    _clear_build_env(monkeypatch)
    _pin_lane(monkeypatch, source={"git_sha": sha, "source_path": str(tmp_path)})
    rows = dict(skill_mod._collect_build_info())

    assert rows["served_sha"] == sha
    assert rows["served_from"] == "working-tree"
    assert rows["image_sha"] == "none"
    assert rows["source_drift"] == "none"
    assert rows["build_age"] == "unknown"
    body = SkillHandler(hub=Hub())._render_status()
    assert "WARN" not in body
    assert "Overall: OK" in body


def test_precis_status_unknown_lane_warns_staleness_unknown(monkeypatch) -> None:
    """No git identity at all still gets the WARN, with the reason named."""
    from precis.handlers import skill as skill_mod

    _clear_build_env(monkeypatch)
    _pin_lane(monkeypatch)
    rows = dict(skill_mod._collect_build_info())

    assert rows["served_sha"] == "unknown"
    assert rows["served_from"] == "unknown"
    assert rows["source_drift"] == "unknown"
    warnings = skill_mod._staleness().warnings()
    assert len(warnings) == 1
    assert warnings[0].startswith("WARN staleness unknown")
    assert "no git identity" in warnings[0]


def test_precis_status_served_tree_behind_counts_commits(monkeypatch, tmp_path) -> None:
    """When the mounted checkout's HEAD moves past the import-time sha the
    page says how far behind the served tree is, in commits, from git; and
    says "behind" without a count when git cannot read the tree."""
    from precis.handlers import skill as skill_mod

    booted = "aaaa1111bbbb2222cccc3333dddd4444eeee5555"
    moved_to = "9999888877776666555544443333222211110000"
    _fake_checkout(tmp_path, booted)
    _clear_build_env(monkeypatch)
    _pin_lane(monkeypatch, source={"git_sha": booted, "source_path": str(tmp_path)})
    _fake_checkout(tmp_path, moved_to)

    # No real git history behind the fake .git — the count is unavailable.
    warnings = skill_mod._staleness().warnings()
    assert len(warnings) == 1
    assert warnings[0].startswith("WARN served tree is behind the mounted checkout")
    assert "aaaa1111bbbb→999988887777" in warnings[0]

    monkeypatch.setattr(skill_mod, "_commits_behind", lambda root, old, new: 7)
    warnings = skill_mod._staleness().warnings()
    assert warnings[0].startswith("WARN served tree is 7 commits behind")
    body = SkillHandler(hub=Hub())._render_status()
    assert "7 commits behind the mounted checkout" in body
    assert "Overall: WARN" in body


def test_precis_status_build_age_is_an_age_and_warns_past_threshold(
    monkeypatch, tmp_path
) -> None:
    """``build_time`` renders as an age next to the timestamp, and a build
    older than the threshold (default 3 days) is a WARN of its own — the
    22-day-old image in gr458061 comment 6 was the only discriminating
    signal and had to be date-subtracted by hand."""
    from datetime import UTC, datetime, timedelta

    from precis.handlers import skill as skill_mod

    sha = "aaaa1111bbbb2222cccc3333dddd4444eeee5555"
    _fake_checkout(tmp_path, sha)
    _clear_build_env(monkeypatch)
    _pin_lane(monkeypatch, source={"git_sha": sha, "source_path": str(tmp_path)})

    old = (datetime.now(UTC) - timedelta(days=22, hours=4)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    monkeypatch.setenv("PRECIS_BUILD_TIME", old)
    rows = dict(skill_mod._collect_build_info())
    assert rows["build_age"].startswith("22d 4h (built ")
    warnings = skill_mod._staleness().warnings()
    assert len(warnings) == 1
    assert warnings[0].startswith("WARN build is 22d 4h old")
    assert "Overall: WARN" in SkillHandler(hub=Hub())._render_status()

    fresh = (datetime.now(UTC) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setenv("PRECIS_BUILD_TIME", fresh)
    assert dict(skill_mod._collect_build_info())["build_age"].startswith("5h ")
    assert skill_mod._staleness().warnings() == []

    # The threshold is tunable; a 1-day limit makes the 5h build still fine
    # and a 2-day build a WARN.
    monkeypatch.setenv("PRECIS_BUILD_AGE_WARN_DAYS", "1")
    two_days = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setenv("PRECIS_BUILD_TIME", two_days)
    assert skill_mod._staleness().warnings()[0].startswith("WARN build is 2d 0h old")

    monkeypatch.setenv("PRECIS_BUILD_TIME", "not a timestamp")
    assert dict(skill_mod._collect_build_info())["build_age"] == "unparseable"


def test_dotgit_info_reads_a_checkout_git_refuses(monkeypatch, tmp_path) -> None:
    """The import-time read must see a mounted checkout even where the
    ``git`` binary cannot (read-only bind mount owned by another uid): the
    ``.git`` parser walks up from the import path and yields sha + branch,
    and stays silent inside ``site-packages``."""
    from precis.handlers import skill as skill_mod

    sha = "aaaa1111bbbb2222cccc3333dddd4444eeee5555"
    _fake_checkout(tmp_path, sha)
    src = tmp_path / "src" / "precis"
    src.mkdir(parents=True)
    info = skill_mod._dotgit_info(src)
    assert info == {
        "source_path": str(tmp_path),
        "git_sha": sha,
        "git_sha_short": sha[:12],
        "git_branch": "main",
    }
    venv = tmp_path / ".venv" / "lib" / "site-packages" / "precis"
    venv.mkdir(parents=True)
    assert skill_mod._dotgit_info(venv) == {}


def test_precis_status_build_falls_back_to_live_git(monkeypatch) -> None:
    """With no baked env vars, the Build section reports the frozen
    live-checkout git state and ``git_source`` == ``working-tree``.
    """
    from precis.handlers import skill as skill_mod

    for env_name, _ in skill_mod._BUILD_ENV_KEYS:
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr(
        skill_mod,
        "_SOURCE_GIT_INFO",
        {
            "git_sha": "livesha1111deadbeef",
            "git_sha_short": "livesha1111d",
            "git_branch": "feature-x",
            "source_path": "/live/checkout",
            "git_dirty": "true",
        },
    )
    rows = dict(skill_mod._collect_build_info())

    assert rows["git_sha"] == "livesha1111deadbeef"
    assert rows["git_branch"] == "feature-x"
    assert rows["source_path"] == "/live/checkout"
    assert rows["git_source"] == "working-tree"


def test_precis_status_build_unknown_without_env_or_git(monkeypatch) -> None:
    """An installed wheel with no ``.git`` and no baked env vars renders
    every git field as ``unknown`` — honestly, not by crashing.
    """
    from precis.handlers import skill as skill_mod

    for env_name, _ in skill_mod._BUILD_ENV_KEYS:
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr(skill_mod, "_SOURCE_GIT_INFO", {})
    monkeypatch.setattr(skill_mod, "_DIST_GIT_INFO", {})
    rows = dict(skill_mod._collect_build_info())

    assert rows["git_sha"] == "unknown"
    assert rows["git_source"] == "unknown"
    assert rows["source_path"] == "unknown"
    # ``version`` always populates regardless of git state.
    assert rows["version"] and rows["version"] != "unknown"


def test_precis_status_build_ignores_unknown_literal_env(monkeypatch) -> None:
    """The Dockerfile bakes ``ENV PRECIS_GIT_SHA=unknown`` as its default,
    so a bare ``docker build`` (not run through ``scripts/build-image``)
    yields a truthy-but-empty env var. That must NOT be read as a genuine
    image identity — it falls through to the next source instead of
    reporting ``image-build`` with an all-``unknown`` body.
    """
    from precis.handlers import skill as skill_mod

    monkeypatch.setenv("PRECIS_GIT_SHA", "unknown")
    monkeypatch.setenv("PRECIS_GIT_BRANCH", "  ")  # blank counts as absent too
    monkeypatch.setattr(
        skill_mod,
        "_SOURCE_GIT_INFO",
        {"git_sha": "livesha2222", "source_path": "/live/checkout"},
    )
    rows = dict(skill_mod._collect_build_info())

    assert rows["git_sha"] == "livesha2222"
    assert rows["git_source"] == "working-tree"


def test_precis_status_build_falls_back_to_dist_metadata(monkeypatch) -> None:
    """An installed-from-git wheel (no ``.git``, no real build-args) reports
    the commit recorded in ``direct_url.json`` and ``git_source`` ==
    ``vcs-install`` — the signal the cluster's ``… @main`` venv relies on.
    """
    from precis.handlers import skill as skill_mod

    for env_name, _ in skill_mod._BUILD_ENV_KEYS:
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr(skill_mod, "_SOURCE_GIT_INFO", {})
    monkeypatch.setattr(
        skill_mod,
        "_DIST_GIT_INFO",
        {
            "git_sha": "distsha3333cafebabe",
            "git_sha_short": "distsha3333c",
            "git_branch": "main",
        },
    )
    rows = dict(skill_mod._collect_build_info())

    assert rows["git_sha"] == "distsha3333cafebabe"
    assert rows["git_branch"] == "main"
    assert rows["git_source"] == "vcs-install"
    # No live checkout, so ``source_path`` stays honest.
    assert rows["source_path"] == "unknown"


def _fake_checkout(root: Path, sha: str) -> None:
    """Write the three files ``checkout_fingerprint`` reads.

    No ``git`` binary and no real repo: the reader parses ``.git`` directly
    (it has to — the watched tree is typically a read-only mount owned by
    another uid, where ``git`` refuses to operate), so a checkout it
    accepts is exactly this much on disk.
    """
    git_dir = root / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text(sha + "\n", encoding="utf-8")


def test_precis_status_build_prefers_watched_checkout_over_baked_env(
    monkeypatch, tmp_path
) -> None:
    """gr457361: the sha must name the code the process is EXECUTING.

    The shared session server imports from a snapshot copy of the watched
    tree, taken without ``.git``, inside an image whose baked
    ``PRECIS_GIT_SHA`` describes a venv built weeks earlier. Measured
    before this pin: a reported sha from 2026-09-08 while ``/app`` carried
    a change landed 2026-09-30. The watched tree's HEAD is the only lane
    that can be right there, so it outranks the baked one.
    """
    from precis.handlers import skill as skill_mod

    _fake_checkout(tmp_path, "aaaa1111bbbb2222cccc3333dddd4444eeee5555")
    monkeypatch.setenv("PRECIS_CHECKOUT_WATCHDOG", str(tmp_path))
    monkeypatch.setenv("PRECIS_GIT_SHA", "bakedsha00000000")
    monkeypatch.setattr(
        skill_mod, "_WATCHED_GIT_INFO", skill_mod._watched_checkout_git_info()
    )
    rows = dict(skill_mod._collect_build_info())

    assert rows["git_sha"] == "aaaa1111bbbb2222cccc3333dddd4444eeee5555"
    assert rows["git_sha_short"] == "aaaa1111bbbb"
    assert rows["git_branch"] == "main"
    assert rows["git_source"] == "watched-checkout"
    assert rows["source_path"] == str(tmp_path)


def test_precis_status_build_does_not_mix_git_identity_lanes(
    monkeypatch, tmp_path
) -> None:
    """``git_dirty`` follows the sha's lane or renders ``unknown``.

    The second half of gr457361: a ``git_dirty: 0`` baked into the image
    was rendered beside the winning sha and read as "this tree is clean and
    verified", which made a three-week-old sha look corroborated rather
    than suspect. A field the winning lane cannot answer must say so.
    """
    from precis.handlers import skill as skill_mod

    _fake_checkout(tmp_path, "aaaa1111bbbb2222cccc3333dddd4444eeee5555")
    monkeypatch.setenv("PRECIS_CHECKOUT_WATCHDOG", str(tmp_path))
    monkeypatch.setenv("PRECIS_GIT_DIRTY", "0")
    monkeypatch.setenv("PRECIS_GIT_DESCRIBE", "v8.4.4-2288-gf2cbcb29")
    monkeypatch.setenv("PRECIS_BUILD_TIME", "2026-09-08T13:57:07Z")
    monkeypatch.setattr(
        skill_mod, "_WATCHED_GIT_INFO", skill_mod._watched_checkout_git_info()
    )
    rows = dict(skill_mod._collect_build_info())

    assert rows["git_dirty"] == "unknown"
    assert rows["git_describe"] == "unknown"
    # The image's *build* provenance is a different question and stays.
    assert rows["build_time"] == "2026-09-08T13:57:07Z"


def test_source_drift_reports_a_checkout_that_moved_since_import(
    monkeypatch, tmp_path
) -> None:
    """The check that would have caught gr458061's incident.

    In that container ``stat``, ``grep`` and a fresh ``python -c`` import
    all reported the new code while the process served 16-hour-old modules
    out of memory — three agreeing, wrong answers. A sha frozen at import
    compared against the tree's HEAD read now cannot agree by construction.
    """
    from precis.handlers import skill as skill_mod

    booted = "aaaa1111bbbb2222cccc3333dddd4444eeee5555"
    moved_to = "9999888877776666555544443333222211110000"
    _fake_checkout(tmp_path, booted)
    monkeypatch.setenv("PRECIS_CHECKOUT_WATCHDOG", str(tmp_path))
    monkeypatch.setattr(skill_mod, "_WATCHED_GIT_INFO", {"git_sha": booted})

    assert skill_mod._source_drift() == "none"

    _fake_checkout(tmp_path, moved_to)
    assert skill_mod._source_drift() == "moved aaaa1111bbbb→999988887777"
    assert dict(skill_mod._collect_build_info())["source_drift"] == (
        "moved aaaa1111bbbb→999988887777"
    )


def test_source_drift_is_unknown_not_clean_without_a_watched_tree(
    monkeypatch,
) -> None:
    """No watched tree means nothing was checked — never ``none``.

    ``none`` is a positive claim that the code is current. Reporting it
    where no comparison happened is the same class of false reassurance as
    the baked ``git_dirty``.
    """
    from precis.handlers import skill as skill_mod

    monkeypatch.delenv("PRECIS_CHECKOUT_WATCHDOG", raising=False)
    _pin_lane(monkeypatch)

    assert skill_mod._source_drift() == "unknown"
    assert dict(skill_mod._collect_build_info())["source_drift"] == "unknown"


def test_watched_checkout_lane_absent_when_tree_is_unreadable(
    monkeypatch, tmp_path
) -> None:
    """A named-but-unreadable tree falls through to the next lane.

    Same rule the watchdog itself follows: a fingerprint source that can
    fail for reasons unrelated to the checkout moving must not be treated
    as an answer.
    """
    from precis.handlers import skill as skill_mod

    monkeypatch.setenv("PRECIS_CHECKOUT_WATCHDOG", str(tmp_path / "nope"))
    monkeypatch.setenv("PRECIS_GIT_SHA", "bakedsha00000000")
    _pin_lane(monkeypatch, watched=skill_mod._watched_checkout_git_info())
    rows = dict(skill_mod._collect_build_info())

    assert rows["git_source"] == "image-build"
    assert rows["git_sha"] == "bakedsha00000000"


def test_live_git_info_reads_the_running_checkout() -> None:
    """When the code runs from a git checkout (the dev/gate case),
    ``_live_git_info`` returns a plausible 40-hex sha and a real path.
    On an installed wheel it returns ``{}``; a checkout whose ``.git``
    points outside the mount (a worktree inside the test container) names
    its ``source_path`` and nothing else — all three are acceptable.
    """
    from precis.handlers import skill as skill_mod

    info = skill_mod._live_git_info()
    assert isinstance(info, dict)
    if info:  # inside a git checkout
        assert "source_path" in info
    if "git_sha" in info:
        assert len(info["git_sha"]) == 40
        assert all(c in "0123456789abcdef" for c in info["git_sha"])
        # ``git_dirty`` needs the git binary; the ``.git`` parser omits it.
        assert info.get("git_dirty", "false") in ("true", "false")


def test_precis_status_runtime_section_present() -> None:
    """The Runtime section reports live process facts (hostname,
    pid, cwd, uptime). Asserts presence + structural fields; values
    are machine-dependent so we don't pin them.
    """
    handler = SkillHandler(hub=Hub())
    body = handler._render_status()

    assert "**Runtime**" in body
    assert "hostname" in body
    assert "platform" in body
    assert "python" in body
    assert "pid" in body
    assert "cwd" in body
    assert "uptime_seconds" in body


def test_precis_status_database_unreachable_renders_inline(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the DB roundtrip raises, the Database section reports
    ``unreachable: <type>: <msg>`` inline rather than crashing the
    whole status response. The status surface is the first thing
    called when something is wrong — it must keep working when the
    DB is the thing wrong.
    """

    class _ExplodingPool:
        def connection(self) -> None:
            raise RuntimeError("pretend the DB is down")

        def close(self) -> None:
            # The store fixture's teardown calls ``s.close()`` → ``pool.close()``
            # while the monkeypatch is still applied (monkeypatch set up before
            # store → torn down after it).
            pass

    monkeypatch.setattr(
        store, "dsn", "postgresql://precis:secret@db.example.invalid:5432/precis"
    )
    monkeypatch.setattr(store, "pool", _ExplodingPool())

    hub = Hub(store=store)
    handler = SkillHandler(hub=hub)
    handler._register_with(hub)

    body = handler._render_status()
    assert "**Database**" in body
    assert "unreachable" in body
    assert "RuntimeError" in body
    # The DSN's password component must never leak into the rendered
    # response — only host / port / name / user are echoed back.
    assert "secret" not in body
    # The rest of the response still renders end-to-end.
    assert "**Overall:" in body


def test_precis_status_database_stateless_when_no_store() -> None:
    """A hub with no store wired renders the Database section as a
    one-line ``stateless build`` note rather than erroring or omitting
    the heading.
    """
    handler = SkillHandler(hub=Hub())
    body = handler._render_status()
    assert "**Database**" in body
    assert "stateless build" in body
