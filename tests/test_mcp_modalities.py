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
    runtime_with_store: PrecisRuntime,
) -> None:
    """``get(kind='skill', id='precis-status')`` returns a markdown
    body listing every probe in the optional-deps table with an OK
    / MISSING / ERROR status.  Sentence-transformers must be
    present (we [all]-installed in CI).
    """
    body = runtime_with_store.dispatch("get", {"kind": "skill", "id": "precis-status"})
    assert "# precis-status" in body
    assert "sentence-transformers" in body
    assert "**Overall:" in body
    # We test against an env that has [all] installed, so the
    # overall status is OK.  If the test runner ever drops
    # sentence-transformers, this assertion shows where.
    assert "Overall: OK" in body, f"precis-status reports a degraded venv:\n{body}"


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


def test_precis_status_runtime_section_present() -> None:
    """The Runtime section reports live process facts (hostname,
    pid, uptime). Asserts presence + structural fields; values are
    machine-dependent so we don't pin them.
    """
    handler = SkillHandler(hub=Hub())
    body = handler._render_status()

    assert "**Runtime**" in body
    assert "hostname" in body
    assert "platform" in body
    assert "python" in body
    assert "pid" in body
    assert "uptime_seconds" in body


def test_precis_status_database_unreachable_renders_inline() -> None:
    """When the DB roundtrip raises, the Database section reports
    ``unreachable: <type>: <msg>`` inline rather than crashing the
    whole status response. The status surface is the first thing
    called when something is wrong — it must keep working when the
    DB is the thing wrong.
    """

    class _ExplodingPool:
        def connection(self) -> None:
            raise RuntimeError("pretend the DB is down")

    class _BrokenStore:
        dsn = "postgresql://precis:secret@db.example.invalid:5432/precis"
        pool = _ExplodingPool()

    hub = Hub(store=_BrokenStore())  # type: ignore[arg-type]
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
