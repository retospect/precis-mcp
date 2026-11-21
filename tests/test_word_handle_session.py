"""Session word-handle switch and the surfaces it turns on (gr674040)."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from precis import principal, serve_ledger, word_handle_session
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.handle import HandleHandler
from precis.utils.search_merge import SearchHit, merge_and_render


class _Session:
    """Minimal session stand-in: ``client_params.clientInfo`` is all
    ``tool_ledger.current_client`` reads for the env auto-match."""

    def __init__(self, name: str | None = None) -> None:
        self.client_params = (
            SimpleNamespace(clientInfo=SimpleNamespace(name=name, version="1"))
            if name
            else None
        )


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(word_handle_session.CLIENTS_ENV, raising=False)
    word_handle_session._clear_for_tests()
    yield
    word_handle_session._clear_for_tests()


def _handler() -> HandleHandler:
    return HandleHandler(hub=Hub())


def test_switch_per_session() -> None:
    h = _handler()
    a, b = _Session(), _Session()
    with serve_ledger.session_scope(a):
        assert not word_handle_session.enabled()
        h.put(text="on")
        assert word_handle_session.enabled()
    # off again outside the session — nothing leaks sessionless
    assert not word_handle_session.enabled()
    with serve_ledger.session_scope(b):
        assert not word_handle_session.enabled()  # no leak to a second session
    with serve_ledger.session_scope(a):
        assert word_handle_session.enabled()
        h.put(text="off")
        assert not word_handle_session.enabled()


def test_switch_rejects_bad_text() -> None:
    with serve_ledger.session_scope(_Session()):
        with pytest.raises(BadInput):
            _handler().put(text="banana")


def test_switch_sessionless_does_not_take() -> None:
    with pytest.raises(BadInput, match="live MCP session") as exc:
        _handler().put(text="on")
    assert "get(kind='handle', id=648264)" in str(exc.value.next)
    assert not word_handle_session.enabled()


def test_env_client_automatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(word_handle_session.CLIENTS_ENV, "voicebox, radio")
    with serve_ledger.session_scope(_Session(name="VoiceBox App")):
        assert word_handle_session.enabled()
        # an explicit off beats the env rule for that session
        _handler().put(text="off")
        assert not word_handle_session.enabled()
    with serve_ledger.session_scope(_Session(name="term-cli")):
        assert not word_handle_session.enabled()
    # sessionless: no client label to match, so the env turns nothing on
    assert not word_handle_session.enabled()


def test_unbound_http_switch_uses_the_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(word_handle_session.CLIENTS_ENV, "VoiceBox")
    with word_handle_session.http_scope("", "VoiceBox/1"):
        assert word_handle_session.enabled()
        with principal.bind("oauth:1"):
            _handler().put(text="off")
        with principal.bind("oauth:2"):
            assert word_handle_session.enabled()
        with principal.bind("install"), pytest.raises(BadInput):
            _handler().put(text="off")
    with word_handle_session.http_scope("", ""):
        with principal.bind("oauth:1"):
            assert not word_handle_session.enabled()
            _handler().put(text="on")
        with principal.bind("oauth:2"):
            assert not word_handle_session.enabled()
        with principal.bind("oauth:1"):
            assert word_handle_session.enabled()
    assert not word_handle_session.enabled()


def _hits() -> list[SearchHit]:
    return [
        SearchHit(
            score=1.0,
            kind="memory",
            title="alpha",
            preview="alpha body",
            ref_id=674030,
        ),
        SearchHit(
            score=0.9,
            kind="memory",
            title="beta",
            preview="beta body",
            ref_id=674040,
        ),
    ]


def test_search_rows_word_column() -> None:
    """TOON rows gain a ``word`` column only while the session switch is on."""
    off = merge_and_render([_hits()], page_size=10, output_shape="toon")
    assert "gentle beacon" not in off.body
    # the {col\tcol…} header — "\tword\t" can't match inside "remaining_words"
    assert "\tword\t" not in off.body.splitlines()[1]

    session = _Session()
    with serve_ledger.session_scope(session):
        _handler().put(text="on")
        on = merge_and_render([_hits()], page_size=10, output_shape="toon")
    assert "\tword\t" in on.body.splitlines()[1]
    assert "gentle beacon" in on.body
    assert "gentle lizard" in on.body


def test_skill_markers() -> None:
    from precis.handlers.skill import _load_skill

    body = _load_skill("precis-word-handles-help")
    assert body is not None
    assert "never read the number" in body
    assert "put(kind='handle'" in body


def test_pair_candidates_need_store() -> None:
    """q= on a pair without a store: BadInput, not a silent guess."""
    with pytest.raises(BadInput):
        _handler().get(q="gentle beacon")


def test_voice_section_markers() -> None:
    """Shared voice guidance stays discoverable from the narration skill."""
    from pathlib import Path

    skills = Path(__file__).resolve().parents[1] / "src/precis/data/skills"
    text = (skills / "precis-word-handles-help.md").read_text(encoding="utf-8")
    assert "## Talking by voice: name things, keep handles recoverable" in text
    assert "never read" in text
    assert "precis-word-handles-help" in (skills / "precis-voice.md").read_text(
        encoding="utf-8"
    )
