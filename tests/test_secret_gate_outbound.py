"""Secret gate on irreversible outbound paths (nanopub, export, Discord)."""

from __future__ import annotations

import dataclasses
import types
from typing import Any, cast

import pytest

from precis.errors import BadInput
from precis.nanopub import assemble
from precis.nanopub.aida import aida_uri, canonical_sentence

# Runtime-assembled so the source holds no literal credential.
SECRET = "gh" + "p_" + ("aB3dE5fG7hJ9kL1mN3pQ" + "5rS7tU9vW1xY3zA5")

_SENT = canonical_sentence("Frameworks show a 400:1 anisotropy between axes")


def _inp(**over: Any) -> assemble.MintInput:
    base: dict[str, Any] = dict(
        artifact_type="claim",
        sentence=_SENT,
        aida_uri=aida_uri(_SENT),
        hub_ref_id=1,
        grounding=[
            assemble.GroundingInput(
                doi="10.1/x",
                pdf_sha256="ab" * 32,
                quote="anisotropy reaches 400:1",
                snip="anisotropy 400",
                role="corroborates",
                source_title="A title",
            )
        ],
    )
    base.update(over)
    return assemble.MintInput(**base)


# ── 1a nanopub ────────────────────────────────────────────────────


def test_nanopub_clean_input_builds() -> None:
    assemble.build_graphs(_inp(), assemble.DRAFT_NS)


def test_nanopub_refuses_secret_in_quote_naming_field() -> None:
    g = dataclasses.replace(_inp().grounding[0], quote="see " + SECRET)
    with pytest.raises(BadInput) as ei:
        assemble.build_graphs(_inp(grounding=[g]), assemble.DRAFT_NS)
    msg = str(ei.value)
    assert "passage 1 quote" in msg
    assert SECRET not in msg


@pytest.mark.parametrize(
    ("over", "label"),
    [
        ({"sentence": "claim " + SECRET}, "assertion sentence"),
        ({"motivation": SECRET, "artifact_type": "hypothesis"}, "motivation"),
        ({"testable_by": SECRET}, "testable_by"),
        ({"fields": {"note": SECRET}}, r"fields\[note\]"),
        ({"software": {"sha": SECRET}}, "pubinfo software sha"),
    ],
)
def test_nanopub_refuses_every_published_field(over: dict, label: str) -> None:
    with pytest.raises(BadInput, match=label):
        assemble.build_graphs(_inp(**over), assemble.DRAFT_NS)


def test_nanopub_publish_refuses_secret_in_trig_before_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.nanopub import registry

    posted: list[bytes] = []
    art = types.SimpleNamespace(
        trusty_uri="https://w3id.org/np/RAx", trig_bytes=("x " + SECRET).encode()
    )
    row = types.SimpleNamespace(id=1, artifact_id=2)
    store = types.SimpleNamespace(
        nanopub_publish_row=lambda _i: row, nanopub_artifact=lambda _i: art
    )
    monkeypatch.setattr(registry, "publish_preflight", lambda *_a, **_k: [])
    with pytest.raises(BadInput):
        registry.publish(
            cast(Any, store),
            1,
            live=True,
            interactive=True,
            post=lambda _u, b: posted.append(b),
        )
    assert posted == []


# ── 1b export ─────────────────────────────────────────────────────


def _draft_store(texts: list[str]) -> Any:
    chunks = [
        types.SimpleNamespace(handle=f"dc{i}", text=t) for i, t in enumerate(texts, 1)
    ]
    return types.SimpleNamespace(
        drafts=types.SimpleNamespace(reading_order=lambda _id: chunks)
    )


def test_export_guard_clean_draft_passes() -> None:
    from precis.export import guard_no_secrets

    guard_no_secrets(
        _draft_store(["plain prose"]), types.SimpleNamespace(id=1, title="T")
    )


def test_export_guard_names_chunk_handle_not_secret() -> None:
    from precis.export import guard_no_secrets

    st = _draft_store(["fine", "token " + SECRET])
    with pytest.raises(BadInput) as ei:
        guard_no_secrets(st, types.SimpleNamespace(id=1, title="T"))
    assert "chunk dc2" in str(ei.value)
    assert SECRET not in str(ei.value)


def test_export_entry_points_call_the_guard() -> None:
    import inspect

    from precis.export import docx, latex

    assert "guard_no_secrets(store, ref)" in inspect.getsource(docx.export_docx)
    assert "guard_no_secrets(store, ref)" in inspect.getsource(latex.export_draft)


# ── 1c discord ────────────────────────────────────────────────────


def test_discord_post_reply_masks_body(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    pytest.importorskip("discord")
    import asa_bot.bot as bot

    sent: list[str] = []

    class _Target:
        async def send(self, content: str = "", **_k: Any) -> None:
            sent.append(content)

    monkeypatch.setattr(bot, "_reply_target", lambda _m: _Target())
    fake = types.SimpleNamespace(
        _cfg=types.SimpleNamespace(
            discord=types.SimpleNamespace(
                max_message_chars=1900, attachment_threshold_chars=10**6
            )
        )
    )
    cls = next(v for v in vars(bot).values() if hasattr(v, "_post_reply"))
    asyncio.run(cls._post_reply(fake, None, None, "here " + SECRET, None))
    assert sent
    assert SECRET not in sent[0]
    assert "<redacted:" in sent[0]
