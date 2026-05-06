"""RandomHandler — random pick from the corpus.

Default ``get(kind='random')`` picks a single undeleted embedded
block at random and returns its canonical handle with a drill-down
hint. Useful for discovery, inspiration, stumbling-into-content, or
agent warmup after a long dry spell.

``get(kind='random', view='slug')`` returns a freshly minted random
short slug (default 4 chars, Crockford-style alphabet — lowercase
letters and digits with visually ambiguous chars 0/o/1/l excluded).
Useful when an agent needs an opaque unique handle for a tag, a
correlation id, or any "I need a unique identifier, don't care
what it says" case. Length and alphabet are configurable via
``args={'len': N, 'alphabet': '...'}``.

Design notes:

- No DSL on the corpus-pick path. Earlier iterations exposed a
  dice / int / choice / neighbor / chunk mini-language; the user
  collapsed it back to this one shape. Dice rolls are cheap to
  do in calc or any other tool.
- The ``slug`` view is a deliberate exception: it's a stateless
  string-generation helper that doesn't touch the corpus at all,
  and adding it as a separate verb / kind would be more friction
  than value. View-on-existing-kind keeps the surface narrow.
- No filter on the corpus pick. ``kind=`` / ``tag=`` filtering is
  deferred — adding it later is a pure additive change.
- Blocks, not refs. A ref without embedded blocks is skipped.
- CSPRNG random everywhere — both block picks and slug minting
  use :mod:`secrets`. The MCP surface is deliberately
  non-deterministic.
- Store-backed for the corpus pick (Boot puts this in the
  ``if store is not None:`` block); the slug view has no
  dependency on the store and works even on a stateless deploy.
"""

from __future__ import annotations

import secrets
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import Store
from precis.utils.next_block import render_next_section

# Crockford-style: lowercase letters + digits, minus visually
# ambiguous characters (0/o/1/l) so a hand-typed slug is harder to
# mis-read or mis-type. 32 characters total — exactly 5 bits of
# entropy per char, so a 4-char slug carries 20 bits ≈ 1M space,
# plenty for "unique within a sortie's lifetime."
_CROCKFORD_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
_ALPHABETS: dict[str, str] = {
    "crockford": _CROCKFORD_ALPHABET,
    "lower": "abcdefghijklmnopqrstuvwxyz",
    "alnum": "abcdefghijklmnopqrstuvwxyz0123456789",
}
_SLUG_LEN_MIN = 1
_SLUG_LEN_MAX = 64
_SLUG_LEN_DEFAULT = 4


class RandomHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="random",
        title="Random",
        description=(
            "Random pick from the corpus (default) or random short "
            "slug minting (view='slug'). Default: returns the "
            "canonical handle of one undeleted embedded block so "
            "you can fetch it with a follow-up get(). With "
            "view='slug': returns a fresh random alphanumeric "
            "string (default 4 chars, Crockford alphabet) for use "
            "as a tag, correlation id, or opaque handle."
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
        *,
        view: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        # ``id=`` / ``q=`` are deliberately ignored — accepting
        # ``**_kw`` keeps us lenient for agents that pass defaults
        # through every call.
        if view == "slug":
            return self._mint_slug(args or {})
        if view not in (None, "", "block"):
            raise BadInput(
                f"unknown random view {view!r}",
                next=(
                    "supported views: '' (default — random corpus block), "
                    "'slug' (random short identifier)"
                ),
            )
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

    def _mint_slug(self, args: dict[str, Any]) -> Response:
        """Render a fresh random short identifier.

        ``args``:
            ``len``      — slug length, default 4, range 1–64.
            ``alphabet`` — named alphabet, default ``'crockford'``.
                           Other choices: ``'lower'`` (a-z),
                           ``'alnum'`` (a-z + 0-9). A literal string
                           is also accepted as a custom alphabet.

        Returns the slug as the response body — no markdown, no
        trailer. Callers compose it into tag values, correlation
        ids, or whatever else.
        """
        length = args.get("len", _SLUG_LEN_DEFAULT)
        try:
            length = int(length)
        except (TypeError, ValueError):
            raise BadInput(
                f"random slug 'len' must be an integer, got {length!r}"
            ) from None
        if length < _SLUG_LEN_MIN or length > _SLUG_LEN_MAX:
            raise BadInput(
                f"random slug 'len' must be in "
                f"[{_SLUG_LEN_MIN}, {_SLUG_LEN_MAX}], got {length}"
            )
        alphabet_arg = args.get("alphabet", "crockford")
        if isinstance(alphabet_arg, str) and alphabet_arg in _ALPHABETS:
            alphabet = _ALPHABETS[alphabet_arg]
        elif isinstance(alphabet_arg, str) and len(alphabet_arg) >= 2:
            # Treat as a custom alphabet literal. Need ≥ 2 distinct
            # characters or every slug collapses to a single repeated
            # char which defeats the purpose.
            alphabet = alphabet_arg
            if len(set(alphabet)) < 2:
                raise BadInput(
                    "random slug custom alphabet must contain at least 2 "
                    f"distinct characters, got {alphabet!r}"
                )
        else:
            raise BadInput(
                f"random slug 'alphabet' must be one of "
                f"{sorted(_ALPHABETS)} or a string of length ≥ 2, "
                f"got {alphabet_arg!r}"
            )
        slug = "".join(secrets.choice(alphabet) for _ in range(length))
        return Response(body=slug)


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
