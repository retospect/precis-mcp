"""Kind-agnostic agent-write secret gate (dispatch boundary)."""

from __future__ import annotations

import importlib
import types
from typing import Any, ClassVar

import pytest

from precis.errors import BadInput
from precis.protocol import Handler
from precis.runtime.dispatch import _refuse_agent_secrets

# Runtime-assembled so the source holds no literal credential.
SECRET = "gh" + "p_" + ("aB3dE5fG7hJ9kL1mN3pQ" + "5rS7tU9vW1xY3zA5")


class _NewKind(Handler):
    """A hypothetical future kind that never heard of the secret gate."""


class _OptOut(Handler):
    stores_opaque_text: ClassVar[bool] = True


def _hub(**handlers: Any) -> Any:
    return types.SimpleNamespace(handler_for=lambda k: handlers.get(k))


def test_default_is_gated() -> None:
    assert Handler.stores_opaque_text is False
    assert _NewKind.stores_opaque_text is False


def test_new_kind_without_opt_out_is_gated_by_default() -> None:
    hub = _hub(newkind=object.__new__(_NewKind))
    with pytest.raises(BadInput) as ei:
        _refuse_agent_secrets("put", {"kind": "newkind", "text": SECRET}, hub)
    assert SECRET not in str(ei.value)


def test_unresolvable_kind_is_gated() -> None:
    with pytest.raises(BadInput):
        _refuse_agent_secrets("put", {"kind": "nope", "text": SECRET}, _hub())


def test_opt_out_kind_is_skipped() -> None:
    hub = _hub(opaque=object.__new__(_OptOut))
    _refuse_agent_secrets("put", {"kind": "opaque", "text": SECRET}, hub)


@pytest.mark.parametrize(
    "arg",
    [
        "text",
        "body",
        "title",
        "new_text",
        "new_title",
        "rule",
        "warrant",
        "reason",
        "note",
        "comment",
        "caption",
        "motivation",
        "testable_by",
        "source_quote",
    ],
)
def test_every_text_arg_is_scanned(arg: str) -> None:
    hub = _hub(k=object.__new__(_NewKind))
    for verb in ("put", "edit", "supersede"):
        with pytest.raises(BadInput, match=arg):
            _refuse_agent_secrets(verb, {"kind": "k", arg: SECRET}, hub)


def test_meta_and_nested_strings_are_scanned() -> None:
    hub = _hub(k=object.__new__(_NewKind))
    with pytest.raises(BadInput, match=r"meta\[hook\]"):
        _refuse_agent_secrets("put", {"kind": "k", "meta": {"hook": SECRET}}, hub)
    with pytest.raises(BadInput, match=r"params\[env\]\[0\]"):
        _refuse_agent_secrets(
            "put", {"kind": "k", "__extras__": {"params": {"env": [SECRET]}}}, hub
        )


def test_clean_text_and_other_verbs_pass() -> None:
    hub = _hub(k=object.__new__(_NewKind))
    _refuse_agent_secrets(
        "put", {"kind": "k", "text": "ordinary prose", "meta": {}}, hub
    )
    # get/search never write: not gated.
    _refuse_agent_secrets("get", {"kind": "k", "text": SECRET}, hub)
    _refuse_agent_secrets("search", {"kind": "k", "q": SECRET}, hub)


# kind -> (module, class, opaque?) -- the per-kind decision table, pinned so a
# change is a deliberate edit here. Kinds not listed use the default (gated).
_OPAQUE = {
    "python": ("precis.handlers.python", "PythonHandler"),
    "fleet": ("precis.handlers.fleet", "FleetHandler"),
    "component": ("precis.handlers.component", "ComponentHandler"),
    "material": ("precis.handlers.material", "MaterialHandler"),
    "rxn": ("precis.handlers.rxn", "RxnHandler"),
    "part": ("precis.handlers.part", "PartHandler"),
    "pcb": ("precis.handlers.pcb", "PcbHandler"),
    "structure": ("precis.handlers.structure", "StructureHandler"),
    "paper": ("precis.handlers.paper", "PaperHandler"),
    "cfp": ("precis.handlers.cfp", "CfpHandler"),
    "datasheet": ("precis.handlers.datasheet", "DatasheetHandler"),
    "patent": ("precis.handlers.patent", "PatentHandler"),
    "edgar": ("precis.handlers.edgar", "EdgarHandler"),
    "web": ("precis.handlers.web", "WebHandler"),
    "wikipedia": ("precis.handlers.wikipedia", "WikipediaHandler"),
    "youtube": ("precis.handlers.youtube", "YouTubeHandler"),
    "semanticscholar": ("precis.handlers.semanticscholar", "SemanticScholarHandler"),
    "news": ("precis.handlers.news", "NewsHandler"),
    "orcid": ("precis.handlers.orcid", "OrcidHandler"),
    "oracle": ("precis.handlers.oracle", "OracleHandler"),
    "calc": ("precis.handlers.calc", "CalcHandler"),
    "math": ("precis.handlers.math", "MathHandler"),
    "random": ("precis.handlers.random", "RandomHandler"),
    "time": ("precis.handlers.time", "TimeHandler"),
    "provenance": ("precis.handlers.provenance", "ProvenanceHandler"),
    "estimate": ("precis_estimate.handler", "EstimateHandler"),
    "protein": ("precis_bio.protein", "ProteinHandler"),
    "route": ("precis_chem.route", "RouteHandler"),
}
_GATED = {
    "agentlog": ("precis.handlers.agentlog", "AgentLogHandler"),
    "alert": ("precis.handlers.alert", "AlertHandler"),
    "anki": ("precis.handlers.anki", "AnkiHandler"),
    "cad": ("precis.handlers.cad", "CadHandler"),
    "checklist": ("precis.handlers.checklist", "ChecklistHandler"),
    "citation": ("precis.handlers.citation", "CitationHandler"),
    "concept": ("precis.handlers.concept", "ConceptHandler"),
    "conv": ("precis.handlers.conversation", "ConversationHandler"),
    "draft": ("precis.handlers.draft", "DraftHandler"),
    "email": ("precis.handlers.email", "EmailHandler"),
    "figure": ("precis.handlers.figure", "FigureHandler"),
    "finding": ("precis.handlers.finding", "FindingHandler"),
    "folder": ("precis.handlers.folder", "FolderHandler"),
    "gripe": ("precis.handlers.gripe", "GripeHandler"),
    "job": ("precis.handlers.job", "JobHandler"),
    "llm": ("precis.handlers.llm", "LlmHandler"),
    "make": ("precis.handlers.make", "MakeHandler"),
    "markdown": ("precis.handlers.markdown", "MarkdownHandler"),
    "md": ("precis.handlers.md", "MdHandler"),
    "measure": ("precis.handlers.measure", "MeasureHandler"),
    "memory": ("precis.handlers.memory", "MemoryHandler"),
    "mermaid": ("precis.handlers.mermaid", "MermaidHandler"),
    "message": ("precis.handlers.message", "MessageHandler"),
    "plaintext": ("precis.handlers.plaintext", "PlaintextHandler"),
    "plan": ("precis.handlers.plan", "PlanHandler"),
    "pres": ("precis.handlers.presentation", "PresentationHandler"),
    "quest": ("precis.handlers.quest", "QuestHandler"),
    "skill": ("precis.handlers.skill", "SkillHandler"),
    "tag": ("precis.handlers.tag", "TagHandler"),
    "taxon": ("precis.handlers.taxon", "TaxonHandler"),
    "tex": ("precis.handlers.tex", "TexHandler"),
    "todo": ("precis.handlers.todo", "TodoHandler"),
    "websearch": ("precis.handlers.perplexity", "WebsearchHandler"),
    "perplexity-reasoning": ("precis.handlers.perplexity", "ThinkHandler"),
    "perplexity-research": ("precis.handlers.perplexity", "ResearchHandler"),
    "pathway": ("precis_pathway.handler", "PathwayHandler"),
    "se": ("precis_se.handler", "SeHandler"),
}


def _cls(mod: str, name: str) -> Any:
    return getattr(importlib.import_module(mod), name)


@pytest.mark.parametrize("kind", sorted(_OPAQUE))
def test_opaque_kinds_opt_out(kind: str) -> None:
    assert _cls(*_OPAQUE[kind]).stores_opaque_text is True


@pytest.mark.parametrize("kind", sorted(_GATED))
def test_agent_authored_kinds_stay_gated(kind: str) -> None:
    assert _cls(*_GATED[kind]).stores_opaque_text is False


def test_runtime_dispatch_gates_a_non_memory_kind(runtime_with_store: Any) -> None:
    out = runtime_with_store.dispatch(
        "put", {"kind": "folder", "title": "f", "text": "x " + SECRET}
    )
    assert "refusing to store what looks like a credential" in out
    assert SECRET not in out
