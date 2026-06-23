"""Universal handle registry — the SSOT for ADR 0036 handles.

A *handle* is the one address form for every persistent ref and every
addressable body chunk: a 2-char lowercase **type code** + a 7-char
**Crockford base32** body (case-folded, lowercase-canonical), e.g.
``pa4m8p1rz`` (a paper), ``dc7k9q2mx`` (a draft chunk). 9 chars, flat,
globally unique, minted at insert, immutable for life.

This module owns the *codes* and the *alphabet*; minting uniqueness is
the DB's job (a ``UNIQUE`` constraint + retry, like the existing
``utils/handles.py`` draft minter this generalises). See
``docs/decisions/0036-universal-handles.md``.

Why code-as-SSOT (not a hand-maintained ADR table): kinds slip through
manual diligence (``news``/``message``/``cron`` all did). The totality
test in ``tests/test_handle_registry.py`` asserts every persistent ref
kind has a code, so adding a kind without a code fails CI, not review.
"""

from __future__ import annotations

import secrets

# --- alphabet -------------------------------------------------------------

#: Crockford base32 — excludes the confusable glyphs ``i l o u`` and folds
#: case on decode, so a handle survives lowercasing / OCR / read-aloud /
#: transcription. Lowercase is canonical (token-friendly). 32 symbols.
CROCKFORD32 = "0123456789abcdefghjkmnpqrstvwxyz"

#: Confusable folds applied to the *body* on normalisation (never to the
#: 2-char code prefix, whose letters legitimately include i/l/o — e.g.
#: ``ci``/``al``/``co``). Per the Crockford spec.
_BODY_FOLDS = {"i": "1", "l": "1", "o": "0"}

BODY_LEN = 7
CODE_LEN = 2
HANDLE_LEN = CODE_LEN + BODY_LEN  # 9

# --- record codes (the 25 addressable persistent-ref kinds) ---------------
# Authoritative kind list: dispatch.boot() composition root. Providers
# (web/youtube/wikipedia/semanticscholar/perplexity-*) and stateless tools
# (calc/math/provenance) are addressed by URL/query/compute, not handles.

KIND_CODES: dict[str, str] = {
    # corpus / documents
    "paper": "pa",
    "patent": "pt",
    "news": "nw",
    "draft": "dr",
    "conv": "co",
    "pres": "pr",
    "markdown": "md",
    "plaintext": "pl",
    "tex": "tx",
    "python": "py",
    # thoughts / generated
    "memory": "me",
    "oracle": "or",
    "finding": "fi",
    "citation": "ci",
    "flashcard": "fc",
    "random": "rn",
    # operational
    "todo": "td",
    "job": "jo",
    "alert": "al",
    "agentlog": "ag",
    "cron": "cr",
    "message": "ms",
    "gripe": "gr",
    # system / meta
    "skill": "sk",
    "tag": "tg",
}

# --- chunk codes (kinds that expose addressable body chunks) --------------
# A chunk gets its own flat handle (ADR 0036 §2 — flat, not parent-prefixed);
# the doc relationship lives in a column. Convention: ``<initial>c`` where
# free, else a free mnemonic. Disjoint from KIND_CODES.

CHUNK_CODES: dict[str, str] = {
    "paper": "pc",
    "patent": "pk",
    "plaintext": "lc",
    "markdown": "mc",
    "tex": "xc",
    "news": "nc",
    "draft": "dc",
    "conv": "cc",
    "pres": "ps",
    "gripe": "gc",
    "message": "mb",
    "cron": "cp",
    "finding": "fb",
    "job": "jc",
}

# Reverse maps (code -> kind), tagged record vs chunk.
_CODE_TO_KIND: dict[str, tuple[str, bool]] = {
    **{c: (k, False) for k, c in KIND_CODES.items()},
    **{c: (k, True) for k, c in CHUNK_CODES.items()},
}


# --- lookups --------------------------------------------------------------


def code_for_kind(kind: str, *, chunk: bool = False) -> str:
    """Return the 2-char code for ``kind`` (its chunk code if ``chunk``)."""
    table = CHUNK_CODES if chunk else KIND_CODES
    try:
        return table[kind]
    except KeyError:
        which = "chunk" if chunk else "record"
        raise KeyError(f"no {which} handle code for kind {kind!r}") from None


def kind_for_code(code: str) -> tuple[str, bool]:
    """Resolve a 2-char code to ``(kind, is_chunk)``."""
    try:
        return _CODE_TO_KIND[code.lower()]
    except KeyError:
        raise KeyError(f"unknown handle type code {code!r}") from None


# --- well-formedness & normalisation -------------------------------------


def normalize(handle: str) -> str:
    """Canonicalise a handle: lowercase, fold body confusables.

    The 2-char code prefix is only lowercased (its letters may be
    ``i/l/o``); the body has ``i/l -> 1`` and ``o -> 0`` applied so a
    mistyped/OCR'd body still resolves. Does not validate.
    """
    s = handle.strip().lower()
    if len(s) <= CODE_LEN:
        return s
    code, body = s[:CODE_LEN], s[CODE_LEN:]
    body = "".join(_BODY_FOLDS.get(ch, ch) for ch in body)
    return code + body


def is_well_formed(handle: str) -> bool:
    """True iff ``handle`` is a known code + a valid Crockford body."""
    s = normalize(handle)
    if len(s) != HANDLE_LEN:
        return False
    code, body = s[:CODE_LEN], s[CODE_LEN:]
    if code not in _CODE_TO_KIND:
        return False
    return all(ch in CROCKFORD32 for ch in body)


# --- minting --------------------------------------------------------------


def mint_body() -> str:
    """A random 7-char Crockford body. Uniqueness is enforced at the DB
    layer (``UNIQUE`` + retry); 32^7 ≈ 34.4 B makes a clash a rounding
    error at corpus scale (ADR 0036 §1)."""
    return "".join(secrets.choice(CROCKFORD32) for _ in range(BODY_LEN))


def mint(kind: str, *, chunk: bool = False) -> str:
    """Mint a fresh (pre-uniqueness-check) handle for ``kind``."""
    return code_for_kind(kind, chunk=chunk) + mint_body()
