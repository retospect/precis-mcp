"""The smartdraft export panel's citation-machinery select and research-notes
checkbox: built from the modes the server says it implements, sent on the
.docx download only, notes default following the audience control."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.export import CITATION_MODES, IMPLEMENTED_CITATION_MODES
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.routes.smartdraft import _citation_mode_options

from .conftest import FakeRuntime
from .test_smartdraft_reader import SmartDraftFakeStore

_VIEW = (
    Path(__file__).resolve().parents[2]
    / "src/precis_web/templates/smartdraft/view.html.j2"
)


class _Panel(HTMLParser):
    """Options of the citation-machinery select; the notes checkbox's attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_select = False
        self._option: str | None = None
        self.options: list[tuple[str, str]] = []
        self.notes_box: dict[str, str | None] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "select":
            self._in_select = "data-citation-machinery" in a
        elif tag == "option" and self._in_select:
            self._option = a.get("value") or ""
            self.options.append((self._option, ""))
        elif tag == "input" and "data-research-notes" in a:
            self.notes_box = a

    def handle_data(self, data: str) -> None:
        if self._in_select and self._option is not None and self.options:
            value, text = self.options[-1]
            self.options[-1] = (value, text + data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._in_select = False
        elif tag == "option":
            self._option = None


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            runtime=FakeRuntime(SmartDraftFakeStore()),
            web_config=WebConfig(corpus_dir=tmp_path),
        )
    )


def test_panel_lists_only_implemented_modes_with_their_labels(
    client: TestClient,
) -> None:
    r = client.get("/smartdraft/sdt")
    assert r.status_code == 200
    panel = _Panel()
    panel.feed(r.text)
    assert [(v, t.strip()) for v, t in panel.options] == [
        ("endnote", "EndNote fields"),
        ("word", "Word citations"),
        ("zotero", "Zotero fields"),
        ("plain", "plain text"),
    ]
    assert tuple(v for v, _ in panel.options) == tuple(
        m for m in CITATION_MODES if m in IMPLEMENTED_CITATION_MODES
    )
    assert panel.notes_box is not None
    # The two links "download .docx" / "plain .docx" became one.
    assert "plain .docx" not in r.text
    assert len(re.findall(r">download \.docx</a>", r.text)) == 1


def test_mode_options_follow_the_implemented_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mode's option is switched on by listing it in the constant."""
    import precis.export as export

    monkeypatch.setattr(export, "IMPLEMENTED_CITATION_MODES", ("endnote", "plain"))
    assert _citation_mode_options() == [
        ("endnote", "EndNote fields"),
        ("plain", "plain text"),
    ]
    monkeypatch.setattr(
        export, "IMPLEMENTED_CITATION_MODES", ("endnote", "word", "zotero", "plain")
    )
    assert _citation_mode_options() == [
        ("endnote", "EndNote fields"),
        ("word", "Word citations"),
        ("zotero", "Zotero fields"),
        ("plain", "plain text"),
    ]


def test_notes_checkbox_is_disabled_unless_endnote_and_follows_audience() -> None:
    text = _VIEW.read_text(encoding="utf-8")
    box = re.search(r"<input[^>]*data-research-notes[^>]*>", text)
    assert box is not None
    assert ":disabled=\"citations !== 'endnote'\"" in box.group(0)
    assert ':checked="notesOn()"' in box.group(0)
    # Untouched: on for dev, off for external; touched: the user's choice.
    assert "researchNotes: null" in text
    assert "this.researchNotes === null ? this.audience === 'dev'" in text
    # Only the .docx download carries the two parameters.
    assert text.count("exportQuery(docxExtra())") == 1
    assert "startExport('docx', docxExtra())" in text
    assert "e.notes = this.notesOn() ? '1' : '0'" in text
