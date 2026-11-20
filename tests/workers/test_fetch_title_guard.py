"""Readable fetched PDFs must match the stub title before publication."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from precis.ingest.fetch_sidecar import read_sidecar
from precis.workers import fetch_oa

TITLE = "Spiking Neural Networks with Improved Inherent Recurrence Dynamics for Sequential Learning"


def _pdf(path: Path, text: str) -> None:
    import fitz

    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(30, 30, 550, 750), text)
        doc.save(str(path))


@pytest.mark.parametrize(
    "text,accepted",
    [
        (TITLE, True),
        (
            "BPMSegNet: Brachial Plexus Segmentation Network for Ultrasound Images "
            "Abstract: We segment nerves in medical ultrasound scans using deep learning.",
            False,
        ),
        ("", True),
        ("Page 1", True),
    ],
)
@pytest.mark.parametrize("fallback", [False, True])
def test_cascade_validates_before_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    accepted: bool,
    fallback: bool,
) -> None:
    stub = fetch_oa.StubRef(1, None, "2012.12012", None, None, title=TITLE)
    path = tmp_path / (fetch_oa._stub_filename(stub) + ".pdf")
    events: list[str] = []
    gate = fetch_oa._ProviderGate()
    note_outcome = MagicMock(wraps=gate.note_outcome)
    monkeypatch.setattr(gate, "note_outcome", note_outcome)
    monkeypatch.setattr(
        fetch_oa, "_timed_event", lambda *a, **kw: events.append(kw["event"])
    )
    for name in ("publisher", "acl", "zenodo", "openalex", "europepmc", "s2"):
        monkeypatch.setattr(fetch_oa, "_try_" + name, lambda *a, **kw: None)

    def download(*args: object, **kwargs: object) -> fetch_oa.FetchOutcome:
        _pdf(path, text)
        return fetch_oa.FetchOutcome("fetch_ok", {}, 60000)

    monkeypatch.setattr(fetch_oa, "_try_arxiv", download)
    if fallback and not accepted:

        def next_provider(*args: object, **kwargs: object) -> fetch_oa.FetchOutcome:
            assert not path.exists()
            assert read_sidecar(path) is None
            _pdf(path, TITLE)
            return fetch_oa.FetchOutcome("fetch_ok", {}, 60000)

        monkeypatch.setattr(fetch_oa, "_try_s2", next_provider)
    result = fetch_oa._run_cascade(
        MagicMock(), stub, tmp_path, "", "", "", "", gate=gate
    )
    assert (result == path) is (accepted or fallback)
    assert path.exists() is (accepted or fallback)
    assert (read_sidecar(path) is not None) is (accepted or fallback)
    expected = ["fetch_ok"] if accepted else ["fetch_failed"]
    if fallback and not accepted:
        expected.append("fetch_ok")
    assert events == expected
    assert note_outcome.call_args_list
    assert all(call.args[1] == "fetch_ok" for call in note_outcome.call_args_list)
    assert gate._lane("arxiv").fails == 0


def test_unreadable_pdf_is_unverifiable(tmp_path: Path) -> None:
    stub = fetch_oa.StubRef(1, None, None, None, None, title=TITLE)
    assert fetch_oa._pdf_title_warnings(stub, tmp_path / "missing.pdf") == []
