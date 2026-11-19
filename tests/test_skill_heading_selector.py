"""Heading addresses resolve to the served numeric skill sections (gr676916)."""

import pytest

from precis.config import PrecisConfig
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.skill import SkillHandler, _load_skill, _resolve_skill_heading
from precis.runtime import PrecisRuntime
from precis.skill_index import chunk_by_h2


def test_heading_selector_matches_numeric() -> None:
    handler = SkillHandler(hub=Hub())
    slug = "precis-memory-help"
    heading = "Working a memory-review todo"
    text = _load_skill(slug)
    assert text is not None
    number = next(i for i, c in enumerate(chunk_by_h2(text)) if heading in c.heading)
    expected = handler.get(id=f"{slug}~{number}").body
    for selector in (
        heading,
        "  WORKING  a memory-review todo",
        "Working a memory-review",
    ):
        assert handler.get(id=f"{slug}~{selector}").body == expected


def test_aliases_exact_prefix_and_refusals() -> None:
    text = "# Demo\n\n## Alpha\n## First\nbody\n\n## Alphabet\nother\n\n## Beta / Second\nlast\n"
    for selector in ("alpha", "First", "fir"):
        assert _resolve_skill_heading("demo", text, selector) == (1, 1)
    assert _resolve_skill_heading("demo", text, "second") == (3, 3)
    for selector, message in (("al", "ambiguous"), ("missing", "unknown")):
        with pytest.raises(BadInput, match=message) as caught:
            _resolve_skill_heading("demo", text, selector)
        assert "demo~1" in str(caught.value)
        assert "demo~2" in str(caught.value)


def test_find_hit_names_section() -> None:
    handler = SkillHandler(hub=Hub())
    slug = "precis-memory-help"
    heading = "Working a memory-review todo"
    text = _load_skill(slug)
    assert text is not None
    number = next(i for i, c in enumerate(chunk_by_h2(text)) if heading in c.heading)
    body = handler.find_in_ref(id=slug, q=heading, context=0).body
    assert f"{slug}~{number} {heading}" in body
    line = next(
        i for i, value in enumerate(text.splitlines(), 1) if value == f"## {heading}"
    )
    assert f"lines {line}" in body


def test_worktree_heading_with_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = SkillHandler(hub=Hub())
    text = (
        "# Demo\n\n## Alpha / First\n\nneedle\n\n## Alphabet\nother\n\n## Beta\nlast\n"
    )
    monkeypatch.setattr(
        handler, "_worktree_alias_claimed", lambda alias: alias == "wt-demo"
    )
    monkeypatch.setattr(handler, "_worktree_corpus", lambda alias: {"demo": text})
    numeric = handler.get(id="wt-demo/demo~1").body
    assert handler.get(id="wt-demo/demo~Alpha / First").body == numeric
    assert handler.get(id="wt-demo/demo~First").body == numeric
    assert (
        "wt-demo/demo~1 Alpha / First"
        in handler.find_in_ref(id="wt-demo/demo", q="needle").body
    )
    with pytest.raises(BadInput, match="ambiguous") as caught:
        handler.get(id="wt-demo/demo~Al")
    assert "wt-demo/demo~1" in str(caught.value)
    assert "wt-demo/demo~2" in str(caught.value)
    with pytest.raises(BadInput, match="unknown") as caught:
        handler.get(id="wt-demo/demo~missing")
    assert all(f"wt-demo/demo~{i}" in str(caught.value) for i in (1, 2, 3))


@pytest.mark.parametrize("key", ["section", "heading", "lines", "offset"])
@pytest.mark.parametrize("nested", [True, False])
def test_guessed_param_points_at_heading_selector(key: str, nested: bool) -> None:
    hub = Hub()
    SkillHandler(hub=hub)._register_with(hub)
    runtime = PrecisRuntime(config=PrecisConfig(), hub=hub)
    payload = {"kind": "skill", "id": "precis-memory-help"}
    out = runtime.dispatch(
        "get",
        {
            **payload,
            **({"__extras__": {key: "example"}} if nested else {key: "example"}),
        },
    )
    assert "BadInput" in out
    assert "id='precis-memory-help~<heading>'" in out
    assert "id='precis-memory-help/toc'" in out
