"""RandomHandler — random pick from the corpus.

One verb, no arguments: ``get(kind='random')`` picks a single
undeleted embedded block at random and returns its canonical
handle with a drill-down hint. Useful for discovery,
inspiration, stumbling-into-content, or agent warmup after a
long dry spell.

Design notes (critic R3, revised):

- No DSL. Earlier iterations exposed a dice / int / choice /
  neighbor / chunk mini-language; the user collapsed it back
  to this one shape. Dice rolls are cheap to do in calc or any
  other tool; the genuinely MCP-native thing is "show me
  something from the corpus I might have forgotten about".
- No filter. ``kind=`` / ``tag=`` filtering is deferred — adding
  it later is a pure additive change.
- Blocks, not refs. A ref without embedded blocks is skipped.
  This keeps the result pointing at something with real content
  (an abstract paragraph, an oracle entry, a memory body) rather
  than a ref shell whose body lives elsewhere.
- CSPRNG random — consistent with :class:`OracleHandler`. No
  ``seed=``; the MCP surface is deliberately non-deterministic.
- Store-backed (not stateless). Boot puts this in the
  ``if store is not None:`` block alongside the other corpus-
  reading kinds.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import NotFound
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import Store
from precis.utils.next_block import render_next_section


class RandomHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="random",
        title="Random",
        description=(
            "Random pick from the corpus. Returns the canonical "
            "handle of one undeleted embedded block so you can "
            "fetch it with a follow-up get(). No arguments: "
            "every call rolls a fresh pick."
        ),
        supports_get=True,
        is_numeric=False,
        id_required=False,
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("random: store required")
        self.hub = hub
        self.store: Store = hub.store

    def get(  # type: ignore[override]
        self,
        **_kw: Any,
    ) -> Response:
        # ``id=`` / ``q=`` / ``view=`` are deliberately ignored —
        # accepting ``**_kw`` keeps us lenient for agents that
        # pass defaults through every call, without teaching the
        # DSL a shape we don't actually support. If we ever grow
        # kind= filtering this is where it lands; revisit the
        # signature then.
        picked = self.store.random_embedded_block()
        if picked is None:
            raise NotFound(
                "corpus has no embedded blocks to pick from",
                next=(
                    "ingest papers / oracles / memories first; "
                    "random needs at least one block with an embedding"
                ),
            )
        block, ref = picked

        handle = _handle(ref, block)
        drill = _drill_down(ref, block)
        preview = _preview(block.text)

        body = f"# random\n`{handle}`"
        if preview:
            body += f"\n\n{preview}"
        body += render_next_section(
            [
                (drill, "read this block"),
                ("get(kind='random')", "another random pick"),
            ]
        )
        return Response(body=body)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _handle(ref: Any, block: Any) -> str:
    """Canonical ``kind:identifier~pos`` handle for the picked block.

    Slug kinds use ``ref.slug``; numeric kinds fall back to the
    ref's integer id (which is the public identifier there). Either
    form is a valid ``link=`` target and a valid ``id=`` for the
    owning handler's ``get``.
    """
    ident = ref.slug if ref.slug else str(ref.id)
    return f"{ref.kind}:{ident}~{block.pos}"


def _drill_down(ref: Any, block: Any) -> str:
    """Build the ``get(...)`` call that fetches the picked block.

    For numeric kinds (``memory`` / ``todo`` / ``gripe`` / ``fc``)
    the ref id alone is enough — these kinds render the whole ref
    body on ``get`` and don't split it by block. For slug kinds
    with multi-block content (``paper`` / ``oracle`` / ``conv``)
    we include the ``~pos`` selector so the agent lands on the
    exact block we picked.
    """
    if ref.slug is None:
        # Numeric kind — ``id=`` is an int literal, not a quoted slug.
        return f"get(kind={ref.kind!r}, id={ref.id})"
    # Slug kind — address the specific block via ``slug~pos``.
    selector = f"{ref.slug}~{block.pos}"
    return f"get(kind={ref.kind!r}, id={selector!r})"


def _preview(text: str, *, max_chars: int = 160) -> str:
    """First non-empty line clipped to ``max_chars``.

    Keeps the response body short; the full block content is one
    ``get()`` away via the drill-down hint. An empty preview is
    rendered as an empty string so the caller's f-string can
    skip it cleanly.
    """
    for line in text.splitlines():
        s = line.strip()
        if s:
            if len(s) > max_chars:
                return s[: max_chars - 1].rstrip() + "…"
            return s
    return ""
