"""gr676930: ``get(view='full')`` — the family-wide complete-read spelling.

The get description promises ``view='full'`` on every kind; the base
dispatch refused it as an unknown view on the numeric-ref kinds
(``unknown view 'full' for kind='finding'`` and friends). Adding
``'full'`` to ``_BASE_VIEWS`` makes the base dispatch render the bare
get (card + body) for it — the fullest read a base kind has. Kinds with
their own fuller ``full`` (todo, gripe, job, alert's ``'full'→'detail'``,
memory's alias fold) intercept it before the base dispatch and keep
their reading.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Unsupported
from precis.handlers.agentlog import AgentLogHandler
from precis.handlers.alias import AliasHandler
from precis.handlers.anki import AnkiHandler
from precis.handlers.citation import CitationHandler
from precis.handlers.concept import ConceptHandler
from precis.handlers.finding import FindingHandler
from precis.handlers.fleet import FleetHandler
from precis.handlers.folder import FolderHandler
from precis.handlers.llm import LlmHandler
from precis.handlers.memory import MemoryHandler
from precis.handlers.message import MessageHandler
from precis.handlers.profile import ProfileHandler
from precis.handlers.quest import QuestHandler
from precis.handlers.taxon import TaxonHandler
from precis.store import Store
from tests.conftest import denonce


def _seed(
    store: Store,
    kind: str,
    title: str,
    *,
    slug: str | None = None,
    meta: dict[str, Any] | None = None,
) -> int:
    return store.insert_ref(kind=kind, slug=slug, title=title, meta=meta or {}).id


# Kinds whose complete read is the base dispatch's bare get: no own
# ``view='full'`` interception, so the spelling must render exactly the
# bare get. (todo/gripe/job/alert keep their own fuller reading — their
# folds predate this slice and are covered by their own view tests.)
_BASE_KIND_CASES: list[tuple[str, Any, dict[str, Any]]] = [
    ("finding", FindingHandler, {}),
    ("quest", QuestHandler, {}),
    ("agentlog", AgentLogHandler, {}),
    ("alias", AliasHandler, {}),
    ("anki", AnkiHandler, {}),
    ("citation", CitationHandler, {}),
    ("concept", ConceptHandler, {}),
    ("folder", FolderHandler, {}),
    ("message", MessageHandler, {}),
    ("taxon", TaxonHandler, {}),
    ("llm", LlmHandler, {}),
    ("fleet", FleetHandler, {"meta": {"key": "github:x:y:z"}}),
    ("profile", ProfileHandler, {"meta": {"name": "fullview"}}),
]


@pytest.mark.parametrize(
    "kind,handler_cls,seed_kw",
    _BASE_KIND_CASES,
    ids=[kind for kind, _, _ in _BASE_KIND_CASES],
)
def test_full_accepted_on_every_numeric_ref_kind(
    kind: str, handler_cls: Any, seed_kw: dict[str, Any], hub: Hub, store: Store
) -> None:
    # Boot upserts the kinds row itself; a bare test store only bakes
    # in the core set (test_profile_handler.py's fixture precedent).
    spec = getattr(handler_cls, "spec", None)
    if spec is not None:
        store.upsert_kinds([spec])
    ref_id = _seed(store, kind, f"{kind} full-view fixture", **seed_kw)
    handler = handler_cls(hub=hub)
    bare = handler.get(id=ref_id).body
    full = handler.get(id=ref_id, view="full").body
    # the record frame's b= nonce is per response (gr739333)
    assert denonce(full) == denonce(bare)


def test_memory_keeps_its_alias_fold(hub: Hub) -> None:
    """Memory accepted ``view='full'`` before this slice via its alias
    map (an ``interpreted:`` banner over the default view) — that
    reading stays; the base change must not double-annotate it."""
    from tests.conftest import id_of

    memory = MemoryHandler(hub=hub)
    mid = id_of(memory.put(text="full-view fixture memory").body)
    body = memory.get(id=mid, view="full").body
    assert "interpreted: view='full' read as the default view" in body
    assert body.count("interpreted:") == 1


def test_unknown_view_refusal_lists_full(hub: Hub, store: Store) -> None:
    """Slice 3 follows from the base gate: the refusal envelope on a
    kind without its own view gate enumerates ``_BASE_VIEWS`` — the
    complete-read spelling must be in the options."""
    ref_id = _seed(store, "finding", "full-view options fixture")
    handler = FindingHandler(hub=hub)
    with pytest.raises(Unsupported) as excinfo:
        handler.get(id=ref_id, view="nonesuch")
    assert "full" in (excinfo.value.options or [])


def test_alias_handle_full_renders_pair(hub: Hub, store: Store) -> None:
    store.upsert_kinds([AliasHandler.spec])
    ref_id = _seed(
        store,
        "alias",
        "full-view alias",
        meta={"alias": {"a": "short name", "b": "long name", "rel": "same-as"}},
    )
    alias = AliasHandler(hub=hub)
    handle = f"as{ref_id}"
    bare = alias.get(id=handle).body
    assert "a: short name" in bare
    assert "b: long name" in bare
    assert denonce(alias.get(id=handle, view="full").body) == denonce(bare)


@pytest.mark.parametrize(
    "selectors", [{"meta_key": "name"}, {"depth": 2}, {"reason": "missing"}]
)
def test_profile_full_validates_selectors(
    selectors: dict[str, Any], hub: Hub, store: Store
) -> None:
    store.upsert_kinds([ProfileHandler.spec])
    ref_id = _seed(store, "profile", "full-view profile")
    with pytest.raises(BadInput):
        ProfileHandler(hub=hub).get(id=ref_id, view="full", **selectors)
