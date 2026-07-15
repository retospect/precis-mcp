"""The shared diagram core is language-generic (ADR 0057, slice 3).

The behavioural parity guard for the figure↔core factoring is the full
existing figure suite (test_figure_turn / _bindings / _svg / _handler /
precis_web/test_figure), which now runs *through* this core unchanged. These
tests add the other half: that the core does not secretly depend on SVG — a
throwaway non-SVG ``DiagramLang`` drives the pure prompt assembler correctly,
and the SVG instance structurally satisfies the port.
"""

from __future__ import annotations

from typing import Any

from precis.diagram.lang import DiagramLang, Element, LintFinding
from precis.diagram.turn import build_prompt
from precis.figure.svg import SVG_LANG


def test_svg_lang_conforms_to_the_port() -> None:
    assert isinstance(SVG_LANG, DiagramLang)
    # the config surface the core reads
    assert SVG_LANG.kind == "figure"
    assert SVG_LANG.source_kind == "figure_node"
    assert SVG_LANG.source_key == "svg"


class _ToyLang:
    """A minimal non-SVG diagram language: the source is plain text, an element
    is a line ``@id …``, nothing ever fails to compile. Proves the core is
    generic — it drives the prompt assembler with no SVG anywhere."""

    kind = "toy"
    source_kind = "toy_node"
    vocab_kind = "toy_vocab"
    notes_kind = "toy_notes"
    turn_kind = "toy_turn"
    skill_name = "precis-toy"
    source_key = "src"
    bounds_meta_key = "frame"

    def parse_error(self, source: str) -> str | None:
        return None

    def sanitize(self, source: str) -> str:
        return source

    def lint(self, source: str, bounds: Any) -> list[LintFinding]:
        return []

    def elements(self, source: str) -> list[Element]:
        return [
            Element(id=ln[1:].split()[0], tag="node", coords="")
            for ln in source.splitlines()
            if ln.startswith("@") and len(ln) > 1
        ]

    def lint_bindings(self, source: str, bound_ids: set[str]) -> list[LintFinding]:
        present = {e.id for e in self.elements(source)}
        return [LintFinding("binding", i, f"{i} missing") for i in bound_ids - present]

    def default_source(self, bounds: Any) -> str:
        return "(empty)"

    def read_bounds(self, source: str) -> Any | None:
        return None

    def default_bounds(self) -> Any:
        return None

    def bounds_from_meta(self, raw: Any) -> Any | None:
        return None

    def bounds_to_meta(self, bounds: Any) -> Any:
        return bounds

    def floor_guidance(self) -> str:
        return "TOY-FLOOR-GUIDANCE"

    def canvas_section(self, bounds: Any) -> str:
        return "## Frame\nunbounded"

    def json_contract(self) -> str:
        return 'reply with {"src": "…"}'


def test_toy_lang_conforms_and_drives_the_prompt() -> None:
    lang = _ToyLang()
    assert isinstance(lang, DiagramLang)

    p = build_prompt(
        lang,
        message="make a widget",
        source="@a first\n@b second",
        vocab="a toy graph",
        notes="a→b",
        findings=[LintFinding("binding", "ghost", "ghost missing")],
        bounds=None,
        skills="SKILLZ",
        context="## Diagram elements ↔ linked context\n- a → dc9",
    )
    # every language-specific fragment came from the toy lang, no SVG leaked
    assert "TOY-FLOOR-GUIDANCE" in p
    assert "## Frame\nunbounded" in p
    assert '{"src": "…"}' in p
    assert "viewBox" not in p and "<svg" not in p
    # the generic scaffolding carried the data
    assert "make a widget" in p
    assert "@a first" in p
    assert "SKILLZ" in p
    assert "Diagram elements ↔ linked context" in p
    assert "[binding] ghost missing" in p
