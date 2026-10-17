"""file-mirror in-scope 5, gate half: recall over the synthetic memory tree.

``search(kind='markdown', q=…, tags=['mirror:memory'])`` must put the file
holding each answer in its top 3 for at least 8 of the 10 questions. This
floor uses the lexical leg and the test embedder; the live-root number is
the report half in ``docs/backlog/file-mirror.md``, never run here.
"""

from __future__ import annotations

import re
from pathlib import Path

from precis.dispatch import Hub
from precis.handlers._roots import MirrorRoot
from precis.handlers.markdown import MarkdownHandler
from precis.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "file_mirror" / "harness"

#: question → the topic file that answers it.
#: Phrased as an agent's search query: the lexical leg ANDs its terms
#: (``websearch_to_tsquery``), so a full sentence matches nothing.
QUESTIONS: dict[str, str] = {
    "ship red failure": "ship-gate-red",
    "concurrent test slot queue": "gate-slots",
    "test passes on rerun seed": "flaky-rerun",
    "migration number landing": "migration-numbering",
    "UTC shell python": "utc-timestamps",
    "who fills embedding vector": "embedding-worker",
    "web user vault": "prod-web-ui",
    "builder tmux window": "tmux-launch",
    "tailnet address tracked file": "secret-gate",
    "rebase squash file list": "squash-audit",
}

_HANDLE = re.compile(r"^## (\S+)", re.MULTILINE)


def _top_files(handler: MarkdownHandler, store: Store, q: str, k: int = 3) -> list[str]:
    body = handler.search(q=q, tags=["mirror:memory"], page_size=20).body
    out: list[str] = []
    for handle in _HANDLE.findall(body):
        resolved = store.resolve_handle(handle)
        assert resolved is not None, handle
        ref = store.get_ref(kind="markdown", id=resolved.ref_id)
        assert ref is not None
        slug = str(ref.slug)
        assert slug.startswith("memory/"), f"tag scope leaked {slug!r}"
        slug = slug.removeprefix("memory/")
        if slug not in out:
            out.append(slug)
    return out[:k]


def test_recall_fixture_scores_eight_of_ten(
    hub: Hub, store: Store, tmp_path: Path
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    # A workspace file on the same topic must not leak past the tag scope.
    (ws / "ship-gate-red.md").write_text(
        "# gate red failure printed\n", encoding="utf-8"
    )
    mirror = MirrorRoot(path=FIXTURE.resolve(), ro=True)
    handler = MarkdownHandler(hub=hub, root=ws, mirrors={"memory": mirror})
    handler.ensure_ingested("ship-gate-red")
    for slug in handler.mirror_files("memory"):
        handler.ensure_ingested(slug)

    misses: dict[str, tuple[str, list[str]]] = {}
    for q, want in QUESTIONS.items():
        got = _top_files(handler, store, q)
        if want not in got:
            misses[q] = (want, got)
    print(f"RECALL file-mirror {len(QUESTIONS) - len(misses)}/{len(QUESTIONS)}")
    assert len(misses) <= 2, misses
