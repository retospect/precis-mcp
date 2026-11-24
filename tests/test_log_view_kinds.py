"""``view='log'`` serves the ``ref_events`` trail for more kinds.

draft, cad, structure, pcb, news, markdown and plaintext route through the
shared :func:`precis.handlers._event_log_render.render_event_log`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers.cad import CadHandler
from precis.handlers.draft import DraftHandler
from precis.handlers.markdown import MarkdownHandler
from precis.handlers.news import NewsHandler
from precis.handlers.pcb import PcbHandler
from precis.handlers.plaintext import PlaintextHandler
from precis.handlers.structure import StructureHandler
from precis.handlers.tex import TexHandler
from precis.protocol import Handler

_Made = tuple[Handler, int, str]


def _insert(hub: Hub, kind: str, slug: str, cls: Callable[..., Handler]) -> _Made:
    ref = hub.live_store.insert_ref(kind=kind, slug=slug, title=f"{kind} probe")
    return cls(hub=hub), ref.id, slug


def _draft(hub: Hub, tmp_path: Path) -> _Made:
    return _insert(hub, "draft", "logdraft", DraftHandler)


def _cad(hub: Hub, tmp_path: Path) -> _Made:
    return _insert(hub, "cad", "logcad", CadHandler)


def _structure(hub: Hub, tmp_path: Path) -> _Made:
    return _insert(hub, "structure", "logstruct", StructureHandler)


def _pcb(hub: Hub, tmp_path: Path) -> _Made:
    return _insert(hub, "pcb", "logpcb", PcbHandler)


def _news(hub: Hub, tmp_path: Path) -> _Made:
    ref, _entry = hub.live_store.put_cache_entry(
        kind="news",
        slug="log-news-1",
        title="news probe",
        body_blocks=[],
        provider="news",
        request_hash="log-news-hash",
        ttl_seconds=None,
    )
    return NewsHandler(hub=hub), ref.id, "log-news-1"


def _plaintext(hub: Hub, tmp_path: Path) -> _Made:
    root = tmp_path / "pt"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "log.txt").write_text("first paragraph\n", encoding="utf-8")
    handler = PlaintextHandler(hub=hub, root=root)
    ref = handler.ensure_ingested("notes--log")
    assert ref is not None
    return handler, ref.id, "notes/log"


def _markdown(hub: Hub, tmp_path: Path) -> _Made:
    root = tmp_path / "md"
    root.mkdir()
    (root / "doc.md").write_text("# Doc\n\nbody\n", encoding="utf-8")
    handler = MarkdownHandler(hub=hub, root=root)
    ref = handler.ensure_ingested("doc")
    assert ref is not None
    return handler, ref.id, "doc"


_MAKERS: dict[str, Callable[[Hub, Path], _Made]] = {
    "draft": _draft,
    "cad": _cad,
    "structure": _structure,
    "pcb": _pcb,
    "news": _news,
    "plaintext": _plaintext,
    "markdown": _markdown,
}


@pytest.mark.parametrize("kind", sorted(_MAKERS))
def test_log_view_shows_event(
    hub: Hub, tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, ref_id, ident = _MAKERS[kind](hub, tmp_path)
    if kind == "structure":

        def fail_load(_store: object, _ref_id: int) -> None:
            pytest.fail("structure log attempted to load the scene")

        monkeypatch.setattr(type(hub.live_store), "structure_load", fail_load)
    hub.live_store.append_event(
        ref_id, source="probe", event="probe_evt", payload={"k": "v"}
    )
    assert "probe_evt" in handler.get(id=ident, view="log").body


def test_cad_log_view_skips_scene_load(
    hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``view='log'`` renders stored events even when ``cad_load`` would fail."""
    handler, ref_id, ident = _cad(hub, tmp_path)

    def fail_load(_store: object, _ref_id: int) -> None:
        pytest.fail("cad log attempted to load the scene")

    monkeypatch.setattr(type(hub.live_store), "cad_load", fail_load)
    hub.live_store.append_event(
        ref_id, source="probe", event="probe_evt", payload={"k": "v"}
    )
    assert "probe_evt" in handler.get(id=ident, view="log").body


@pytest.mark.parametrize("kind", ["plaintext", "markdown"])
def test_file_log_view_never_ingests(
    hub: Hub, tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``view='log'`` resolves the stored ref without lazy re-ingest."""
    handler, ref_id, ident = _MAKERS[kind](hub, tmp_path)

    def fail_ingest(*_a: object, **_k: object) -> None:
        pytest.fail(f"{kind} log attempted to ingest")

    monkeypatch.setattr(handler, "ensure_ingested", fail_ingest)
    hub.live_store.append_event(
        ref_id, source="probe", event="probe_evt", payload={"k": "v"}
    )
    assert "probe_evt" in handler.get(id=ident, view="log").body


def test_file_log_view_unstored_file_is_not_found(hub: Hub, tmp_path: Path) -> None:
    """A file on disk with no stored ref yields NotFound — never an ingest."""
    root = tmp_path / "pt"
    root.mkdir()
    (root / "fresh.txt").write_text("body\n", encoding="utf-8")
    handler = PlaintextHandler(hub=hub, root=root)
    with pytest.raises(NotFound):
        handler.get(id="fresh", view="log")
    assert handler.store.get_ref(kind="plaintext", id="fresh") is None


@pytest.mark.parametrize("via_args", [True, False])
def test_structure_log_rejects_external_source(hub: Hub, via_args: bool) -> None:
    source = "catalysis-hub"
    with pytest.raises(BadInput, match="without source="):
        StructureHandler(hub=hub).get(
            id="logstruct",
            view="log",
            args={"source": source} if via_args else None,
            source=None if via_args else source,
        )


def test_log_view_empty_ref_renders_placeholder(hub: Hub, tmp_path: Path) -> None:
    handler, _ref_id, ident = _cad(hub, tmp_path)
    body = handler.get(id=ident, view="log").body
    assert "no events" in body.lower()


def test_log_view_missing_ref_is_not_found(hub: Hub) -> None:
    with pytest.raises(NotFound):
        NewsHandler(hub=hub).get(id="no-such-news", view="log")


@pytest.mark.parametrize(
    "handler_cls",
    [
        DraftHandler,
        CadHandler,
        StructureHandler,
        PcbHandler,
        NewsHandler,
        PlaintextHandler,
        MarkdownHandler,
    ],
)
def test_log_advertised(handler_cls: type[Handler]) -> None:
    assert "log" in handler_cls.spec.views


def test_tex_still_refuses_log(hub: Hub, tmp_path: Path) -> None:
    (tmp_path / "doc.tex").write_text(r"\section{Doc}", encoding="utf-8")
    handler = TexHandler(hub=hub, root=tmp_path)
    with pytest.raises(Unsupported, match="unknown tex view 'log'"):
        handler.get(id="doc", view="log")
