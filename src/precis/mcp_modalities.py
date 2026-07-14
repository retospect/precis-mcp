"""Wire skills + refs onto MCP prompts and resources.

Closes the modality gap flagged by the MCP critic's April 2026
re-probe: tools-only servers leave four of the five MCP modalities
silent, even when the underlying data is already addressable.

Scope:

- **Prompts** (``prompts/list`` + ``prompts/get``) — every skill
  that passes :func:`SkillHandler._availability_gap` is registered
  as a prompt.  Body comes from :func:`_load_skill` (or the
  synthesised renderer for ``precis-help`` / ``precis-status``).
  Tags carry the skill's ``tier:`` and any kind it documents so
  modern clients can group / filter.

- **Resources** — two surfaces, both DRY:

  * ``resources/list``: enumerated only for the small bounded sets
    (skills, ~16 entries).  *Never* enumerates papers — there can
    be thousands and the listing would blow client context.
  * ``resources/templates/list``: URI templates for the
    high-cardinality kinds (papers, memories, todos, …).  Modern
    clients use templates for autocomplete; concrete URIs are
    constructed by callers from search hits.

URI scheme is ``precis://<kind>/<id>``.  ``id`` for slug kinds is
the slug; for numeric kinds it's the integer ref id stringified.
Resource reads dispatch through :class:`PrecisRuntime.dispatch` so
every kind that has a ``get`` handler is reachable as a resource
without extra wiring.

Single source of truth: every skill body is read by
:func:`_load_skill`, every ref body is rendered by the kind's
``handler.get``.  No parallel registry.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp.prompts.base import Prompt

from precis.handlers.skill import (
    SkillHandler,
    _availability_gap,
    _list_skills,
    _load_skill,
    _parse_frontmatter,
    _skill_title,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from precis.runtime import PrecisRuntime

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Skills as prompts
# ---------------------------------------------------------------------------


def _enumerate_prompt_skills(runtime: PrecisRuntime) -> list[str]:
    """Return slugs eligible for ``prompts/list``.

    Same gate as the existing skill index: drops skills documenting
    kinds that aren't wired in this build, plus skills whose
    front-matter ``status:`` is ``planned`` or ``aspirational``.
    Synthesised meta-skills (``precis-help``, ``precis-status``) are
    always included so the modality covers the full menu users see
    via ``get(kind='skill')``.

    Programmatically derived — no hand-maintained list.
    """
    out: list[str] = list(SkillHandler._SYNTHESIZED_SKILLS)
    for slug in sorted(_list_skills()):
        if _availability_gap(slug, hub=runtime.hub) is None:
            out.append(slug)
    # Stable order: synthesised first (already in dict order), then
    # sorted on-disk slugs.  Dedup just in case a synthesised slug
    # also happens to land on disk.
    seen: set[str] = set()
    deduped: list[str] = []
    for s in out:
        if s in seen:
            continue
        seen.add(s)
        deduped.append(s)
    return deduped


def _skill_prompt_tags(
    slug: str, *, pinned_slugs: frozenset[str] = frozenset()
) -> set[str]:
    """Pull tier/kind tags from a skill's front-matter for the prompt.

    Synthesised skills get a fixed ``synth`` tag; on-disk skills get
    ``tier-<N>`` (defaulting to ``tier-1``) and a ``kind:<X>`` tag
    for every kind referenced in their slug or ``applies-to:``
    front-matter.  Modern clients use these for grouping / filtering
    in their prompt UI.

    Skills listed in ``PRECIS_STARTUP_SKILLS`` and surviving
    resolution (passed in via ``pinned_slugs``) additionally carry a
    ``pinned`` tag so a modern client can prioritise the operator-
    recommended starting set in its prompt picker.
    """
    base: set[str] = {"precis", "skill"}
    if slug in pinned_slugs:
        base.add("pinned")
    if slug in SkillHandler._SYNTHESIZED_SKILLS:
        base.add("synth")
        return base
    text = _load_skill(slug) or ""
    fm = _parse_frontmatter(text)
    tier = fm.get("tier", "1").strip() or "1"
    base.add(f"tier-{tier}")
    floor = fm.get("floor", "").strip().lower()
    if floor and floor != "any":
        base.add(f"floor-{floor}")
    # Kinds documented by the skill — same logic the gate uses.
    from precis.handlers.skill import _kinds_referenced_by_skill

    for k in _kinds_referenced_by_skill(slug, fm):
        base.add(f"kind:{k}")
    return base


def _make_skill_prompt_fn(runtime: PrecisRuntime, slug: str):
    """Build a zero-arg callable that returns the skill body.

    Synthesised skills (``precis-help``, ``precis-status``) route
    through ``SkillHandler.get`` so the rendering uses the bound
    registry.  On-disk skills are read via :func:`_load_skill`,
    matching the path :class:`SkillHandler` itself uses.
    """

    def _fn() -> str:
        if slug in SkillHandler._SYNTHESIZED_SKILLS:
            response = runtime.dispatch("get", {"kind": "skill", "id": slug})
            return response
        text = _load_skill(slug)
        return text if text is not None else f"(skill {slug!r} not found)"

    _fn.__doc__ = (
        f"Read the precis skill ``{slug}``.\n\nServed verbatim from "
        "the skill data directory; this prompt is a thin wrapper "
        "around `get(kind='skill', id=...)`."
    )
    return _fn


def _resolved_pinned_slugs(runtime: PrecisRuntime) -> frozenset[str]:
    """Resolve ``PRECIS_STARTUP_SKILLS`` to the surviving pinned set.

    Honours the cap and silently drops unknowns — the banner notice
    from :mod:`precis.startup_skills.format_banner` already surfaces
    those errors. Resolution runs once per ``register_skill_prompts``
    call; the returned set is immutable so callers can use it as a
    membership-test only.
    """
    from precis import startup_skills

    config = runtime.config
    slugs = startup_skills.parse(getattr(config, "startup_skills", None))
    if not slugs:
        return frozenset()
    cap_kb = getattr(config, "startup_skills_cap_kb", 50)
    result = startup_skills.resolve(slugs, cap_kb=cap_kb)
    return frozenset(result.pinned)


def register_skill_prompts(mcp: FastMCP, runtime: PrecisRuntime) -> int:
    """Register every available skill as an MCP prompt.

    Returns the number of prompts registered.  Idempotent at
    runtime-build time — registration runs once before
    ``mcp.run()``.

    Each prompt's name is the slug, description is the
    front-matter ``title:`` (or the slug if missing), and the body
    on ``prompts/get`` is the same markdown the corresponding
    ``get(kind='skill', id=<slug>)`` returns.  No skill text is
    duplicated.

    Skills listed in ``PRECIS_STARTUP_SKILLS`` and surviving the
    cap-and-lookup resolution land with a ``pinned`` tag so modern
    MCP clients can prioritise the operator-recommended starting
    set.
    """
    count = 0
    pinned_slugs = _resolved_pinned_slugs(runtime)
    for slug in _enumerate_prompt_skills(runtime):
        title = SkillHandler._SYNTHESIZED_SKILLS.get(slug) or _skill_title(slug) or slug
        prompt = Prompt.from_function(
            _make_skill_prompt_fn(runtime, slug),
            name=slug,
            description=title,
        )
        # FastMCP stores tags on the underlying Pydantic model via
        # construction kwargs; ``Prompt.from_function`` doesn't take
        # tags directly so set them post-hoc.  Skipped on older
        # versions that ignore the field — best-effort.
        tags = _skill_prompt_tags(slug, pinned_slugs=pinned_slugs)
        try:
            object.__setattr__(prompt, "tags", tags)
        except Exception:  # pragma: no cover — pydantic strictness
            log.debug("could not attach tags to prompt %s", slug)
        mcp.add_prompt(prompt)
        count += 1
    log.info("registered %d skill prompt(s)", count)
    return count


# ---------------------------------------------------------------------------
# Refs as resources
# ---------------------------------------------------------------------------


_RESOURCE_URI_SCHEME = "precis://"


def _resource_uri(kind: str, ident: str | int) -> str:
    """Canonical URI for a precis ref.

    Slug kinds get ``precis://<kind>/<slug>``; numeric kinds get
    ``precis://<kind>/<id>``.  Block selectors ride along as
    suffixes on the id (e.g. ``precis://paper/wang2020~38``) so the
    same parser handles both shapes.
    """
    return f"{_RESOURCE_URI_SCHEME}{kind}/{ident}"


def _parse_resource_uri(uri: str) -> tuple[str, str]:
    """Return ``(kind, id)`` for a ``precis://<kind>/<id>`` URI.

    Raises ``ValueError`` for any URI that doesn't match the scheme
    or omits one of the two segments.  ``id`` keeps any block
    selector (``~N``, ``~N..M``) or path view (``/toc``) verbatim;
    parsing those is the kind handler's job.
    """
    if not uri.startswith(_RESOURCE_URI_SCHEME):
        raise ValueError(f"resource URI must start with {_RESOURCE_URI_SCHEME!r}")
    rest = uri[len(_RESOURCE_URI_SCHEME) :]
    kind, sep, ident = rest.partition("/")
    if not sep or not kind or not ident:
        raise ValueError(
            f"resource URI {uri!r} must be {_RESOURCE_URI_SCHEME}<kind>/<id>"
        )
    return kind, ident


#: Kinds whose refs are exposed as ``resources/list`` entries.
#:
#: Bounded sets only — anything that can grow into the thousands
#: belongs in :data:`_TEMPLATE_KINDS` instead.  Skills are the
#: canonical bounded set (~16 entries).  Listed kinds also appear
#: as templates so a caller who already knows the slug can read by
#: URI without first hitting ``resources/list``.
_LIST_KINDS: tuple[str, ...] = ("skill",)


#: Kinds advertised as URI templates.  Each entry is
#: ``(kind, template, description)``.  The template parameter is
#: always ``{id}`` for consistency; the parsing lives on the kind
#: handler so block selectors and path views (``~N``, ``/toc``)
#: ride along inside ``id`` transparently.
_TEMPLATE_KINDS: tuple[tuple[str, str, str], ...] = (
    (
        "paper",
        "precis://paper/{id}",
        "Scientific paper.  id = slug, optionally with ``~N`` "
        "block selector or ``/toc`` / ``/abstract`` / ``/bibtex``.",
    ),
    (
        "memory",
        "precis://memory/{id}",
        "Memory entry.  id = integer ref id.",
    ),
    (
        "todo",
        "precis://todo/{id}",
        "Todo.  id = integer ref id.",
    ),
    (
        "gripe",
        "precis://gripe/{id}",
        "Gripe entry.  id = integer ref id.",
    ),
    (
        "anki",
        "precis://anki/{id}",
        "Anki cloze card.  id = integer ref id.",
    ),
    (
        "concept",
        "precis://concept/{id}",
        "Knowledge-graph concept node.  id = integer ref id.",
    ),
    (
        "conv",
        "precis://conv/{id}",
        "Conversation.  id = slug.",
    ),
    (
        "skill",
        "precis://skill/{id}",
        "Agent skill.  id = slug (also enumerated in resources/list).",
    ),
)


def _read_resource(runtime: PrecisRuntime, uri: str) -> str:
    """Render the body served at ``uri`` by dispatching to the runtime.

    Uses the same code path as ``tools/call get`` — there's no
    parallel rendering pipeline.  The dispatcher's error envelopes
    (``[error:NotFound] …``) come back verbatim; clients see the
    same text the agent would.
    """
    kind, ident = _parse_resource_uri(uri)
    typed_id: Any = ident
    spec = None
    try:
        handler = runtime.hub.handler_for(kind)
        spec = handler.spec if handler is not None else None
    except Exception:  # pragma: no cover — let dispatch raise the proper error
        spec = None
    if spec is not None and spec.is_numeric:
        try:
            typed_id = int(ident)
        except ValueError:
            # Non-int id on a numeric kind — let dispatch handle it.
            typed_id = ident
    return runtime.dispatch("get", {"kind": kind, "id": typed_id})


def register_resources(mcp: FastMCP, runtime: PrecisRuntime) -> tuple[int, int]:
    """Register skills as enumerated resources + every kind as a template.

    Returns ``(n_resources, n_templates)``.  Skills go through
    ``resources/list`` because they're a small bounded set;
    high-cardinality kinds (papers, memories, …) are templates so
    no enumeration cost is paid.  Both surfaces dispatch through
    the runtime, no body is cached.
    """
    n_resources = 0
    n_templates = 0

    # Enumerated resources: only the bounded list-kinds.
    for kind in _LIST_KINDS:
        if kind == "skill":
            for slug in _enumerate_prompt_skills(runtime):
                description = (
                    SkillHandler._SYNTHESIZED_SKILLS.get(slug)
                    or _skill_title(slug)
                    or slug
                )
                _add_skill_resource(mcp, runtime, slug, description)
                n_resources += 1

    # Templates: every reachable kind so the modern client can offer
    # autocomplete on slugs / numeric ids without enumeration.
    registered_kinds = runtime.hub.kinds
    for kind, template, description in _TEMPLATE_KINDS:
        if kind not in registered_kinds:
            continue
        _add_kind_template(mcp, runtime, kind, template, description)
        n_templates += 1

    log.info(
        "registered %d resource(s) + %d template(s)",
        n_resources,
        n_templates,
    )
    return n_resources, n_templates


def _add_skill_resource(
    mcp: FastMCP,
    runtime: PrecisRuntime,
    slug: str,
    description: str,
) -> None:
    """Register one skill as a static ``precis://skill/<slug>`` resource."""
    from mcp.server.fastmcp.resources import FunctionResource

    uri = _resource_uri("skill", slug)

    def _read() -> str:
        return _read_resource(runtime, uri)

    res = FunctionResource(
        uri=uri,  # type: ignore[arg-type]
        name=slug,
        description=description,
        mime_type="text/markdown",
        fn=_read,
    )
    mcp.add_resource(res)


def _add_kind_template(
    mcp: FastMCP,
    runtime: PrecisRuntime,
    kind: str,
    template: str,
    description: str,
) -> None:
    """Register a URI template for one kind via FastMCP's template manager."""

    def _read(id: str) -> str:
        # ``id`` is whatever the client substituted into the
        # template (slug for slug kinds, str-int for numeric).
        # ``_read_resource`` re-builds the canonical URI and
        # dispatches; the str-int → int coercion lives there.
        return _read_resource(runtime, _resource_uri(kind, id))

    _read.__doc__ = description
    mcp._resource_manager.add_template(
        fn=_read,
        uri_template=template,
        name=f"{kind} ref",
        description=description,
        mime_type="text/markdown",
    )


__all__ = [
    "register_resources",
    "register_skill_prompts",
]
