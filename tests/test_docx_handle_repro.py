"""gr493181 — end-to-end docx fixture: internal-handle token shapes
(``[h~n]``, ``[[…]]``, ``[h1, h2]``, ``kind:id``, display links) never
print raw in the exported body."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.handlers.todo import TodoHandler

docx = pytest.importorskip("docx")

from precis.export.docx import export_docx


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _pid(hub: Hub) -> int:
    return int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )


def test_internal_token_shapes_do_not_leak(
    draft: DraftHandler, hub: Hub, tmp_path: Path
) -> None:
    store = hub.live_store
    draft.put(id="d2", title="The Other Draft", project=_pid(hub))
    ref2 = store.get_ref(kind="draft", id="d2")
    assert ref2 is not None
    dr2 = f"dr{ref2.id}"

    draft.put(id="d1", title="Handle Leak Repro", project=_pid(hub))
    ref1 = store.get_ref(kind="draft", id="d1")
    assert ref1 is not None
    title_h = store.drafts.reading_order(ref1.id)[0].handle
    fig = store.drafts.add_chunks(
        ref_id=ref1.id,
        chunk_kind="figure",
        text="Fig 1. A widget.",
        at={"after": title_h},
        split=False,
    )[0]

    shapes = [
        f"bracketed selector [{dr2}~0] end",
        f"authoring [[{dr2}]] end",
        f"authoring xref [[{fig.dc}]] end",
        f"kind handle draft:{ref2.id} end",
        "bracketed kind [todo:5] end",
        f"display link [the dossier]({dr2}) end",
        f"display sel [spot]({dr2}~1) end",
        "list cite [pa1, pa2] end",  # unresolvable → cite attempt drops
        "authored prose gned-finding-hub stays literal",
    ]
    for s in shapes:
        draft.put(id="d1", chunk_kind="paragraph", text=s, at={"last": True})

    out = tmp_path / "d1.docx"
    export_docx(store, ref1, target_path=out)
    text = "\n".join(p.text for p in docx.Document(str(out)).paragraphs)

    # No raw internal token survives: no leftover brackets, no [[, no
    # kind:id, no ~selector forms.
    assert not re.search(r"\[\[|\]\]|\[[a-z]{2}\d", text), text
    assert f"[{dr2}~0]" not in text and f"draft:{ref2.id}" not in text
    assert "todo:5" not in text
    # An authored display surface is body prose, kept when the record drops.
    assert "the dossier" in text and "spot" in text
    # The authoring link to a live figure chunk resolves to its xref number.
    assert "authoring xref fig. 1 end" in text
    # Authored prose that merely looks handle-ish is untouched.
    assert "gned-finding-hub" in text
