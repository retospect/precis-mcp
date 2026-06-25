"""The academic (sci-*) section styles load as skills and inject into the
editor prompt (ADR 0037/0038 — extends the patent step-3 work to papers)."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.handlers.skill import SkillHandler
from precis.workers.planner_prompt import _render_section_style

_SCI_STYLES = [
    "sci-abstract",
    "sci-introduction",
    "sci-related-work",
    "sci-methods",
    "sci-results",
    "sci-discussion",
    "sci-conclusion",
    "sci-survey-section",
]


@pytest.mark.parametrize("slug", _SCI_STYLES)
def test_sci_style_skill_loads(slug: str) -> None:
    """Each sci-* style is addressable by its slug and has a real body."""
    body = SkillHandler(hub=None).get(id=slug).body  # type: ignore[arg-type]
    assert len(body) > 150
    assert "section style" in body.lower() or "you are writing" in body.lower()


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def test_sci_methods_injects_real_body(draft: DraftHandler, hub: Hub) -> None:
    proj = hub.store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id="rp", title="A paper", project=proj)
    ref = hub.store.get_ref(kind="draft", id="rp")
    title = hub.store.reading_order(ref.id)[0]
    draft.put(
        id="rp",
        chunk_kind="heading",
        text="Methods",
        at={"after": "¶" + title.handle},
    )
    methods = hub.store.reading_order(ref.id)[1]
    draft.put(
        id="rp",
        chunk_kind="paragraph",
        text="We ran it.",
        at={"into": "¶" + methods.handle, "last": True},
    )
    para = hub.store.reading_order(ref.id)[2]
    draft.edit(id=methods.dc, style="sci-methods")
    cr = hub.store.insert_ref(
        kind="todo", slug=None, title="cr", meta={"anchor": para.dc}
    ).id
    block = _render_section_style(hub.store, cr)
    assert "Section style — sci-methods" in block
    assert "reproduce" in block  # body content, not the pointer
    assert "get(kind='skill'" not in block
